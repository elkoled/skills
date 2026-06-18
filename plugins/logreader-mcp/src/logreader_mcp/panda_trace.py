from __future__ import annotations

import gzip
import json
import os
import subprocess
import tempfile
from pathlib import Path

from .bootstrap import bootstrap

bootstrap()

from opendbc.safety import LEN_TO_DLC  # noqa: E402
from opendbc.safety.tests.libsafety import libsafety_py  # noqa: E402

from cffi import FFI  # noqa: E402

_CDEF = """
typedef struct {
  unsigned char fd : 1;
  unsigned char bus : 3;
  unsigned char data_len_code : 4;
  unsigned char rejected : 1;
  unsigned char returned : 1;
  unsigned char extended : 1;
  unsigned int addr : 29;
  unsigned char checksum;
  unsigned char data[64];
} CANPacket_t;

bool safety_rx_hook(CANPacket_t *msg);
bool safety_tx_hook(CANPacket_t *msg);
int set_safety_hooks(uint16_t mode, uint16_t param);
void set_controls_allowed(bool c);
void set_alternative_experience(int mode);
void set_timer(uint32_t t);
void safety_tick_current_safety_config(void);
void init_tests(void);
void op_gcov_reset(void);
void op_gcov_dump(void);
"""

# re-export libgcov's __gcov_dump/__gcov_reset (hidden in a .so) so cffi can call them
_GCOV_SHIM = """
extern void __gcov_dump(void);
extern void __gcov_reset(void);
void op_gcov_dump(void) { __gcov_dump(); }
void op_gcov_reset(void) { __gcov_reset(); }
"""


def _safety_paths():
  libdir = Path(os.path.dirname(os.path.abspath(libsafety_py.__file__)))
  safety_c = libdir / "safety.c"
  include_root = libdir.parents[3]  # dir containing the `opendbc` package
  safety_dir = libdir.parents[1]    # opendbc/safety
  return safety_c, include_root, safety_dir


class SafetyTraceLib:
  def __init__(self):
    self.ok = False
    self.reason = None
    self.ffi = FFI()
    self.ffi.cdef(_CDEF, packed=True)
    self.build = Path(tempfile.mkdtemp(prefix="safety_trace_"))
    self.safety_c, self.include_root, self.safety_dir = _safety_paths()
    self.gcov = os.environ.get("GCOV", "gcov")
    try:
      self._build()
      self.lib = self.ffi.dlopen(str(self.build / "libsafety_trace.so"))
      self.ok = True
    except Exception as e:  # noqa: BLE001
      self.reason = f"trace build failed: {e!r}"

  def _build(self):
    obj = self.build / "safety.o"
    shim = self.build / "gcov_shim.c"
    shim_obj = self.build / "gcov_shim.o"
    so = self.build / "libsafety_trace.so"
    shim.write_text(_GCOV_SHIM)
    cflags = ["-fPIC", "-nostdlib", "-fno-builtin", "-std=gnu11",
              "-g", "-O0", "-fno-omit-frame-pointer", "-DALLOW_DEBUG",
              "-fprofile-arcs", "-ftest-coverage"]
    subprocess.check_call(["cc", *cflags, "-I", str(self.include_root),
                           "-c", str(self.safety_c), "-o", str(obj)],
                          stderr=subprocess.DEVNULL)
    subprocess.check_call(["cc", "-fPIC", "-O0", "-c", str(shim), "-o", str(shim_obj)],
                          stderr=subprocess.DEVNULL)
    subprocess.check_call(["cc", "-shared", str(obj), str(shim_obj), "-o", str(so),
                           "-fprofile-arcs", "-ftest-coverage"],
                          stderr=subprocess.DEVNULL)

  def _packet(self, addr, bus, dat):
    p = self.ffi.new("CANPacket_t *")
    p[0].extended = 1 if addr >= 0x800 else 0
    p[0].addr = addr
    p[0].data_len_code = LEN_TO_DLC[len(dat)]
    p[0].bus = bus
    p[0].data = bytes(dat)
    return p

  def setup(self, mode: int, param: int, alt_exp: int = 0):
    self.lib.init_tests()
    self.lib.set_safety_hooks(mode, param)
    self.lib.set_alternative_experience(alt_exp)

  def rx(self, addr, bus, dat):
    return bool(self.lib.safety_rx_hook(self._packet(addr, bus, dat)))

  def set_timer(self, micros: int):
    self.lib.set_timer(micros & 0xFFFFFFFF)

  def _coverage(self) -> dict[str, dict[int, int]]:
    try:
      out = subprocess.run([self.gcov, "--json-format", "--stdout", "-o",
                            str(self.build), str(self.safety_c)],
                           capture_output=True, cwd=str(self.build), timeout=30).stdout
    except Exception:  # noqa: BLE001
      return {}
    if not out:
      return {}
    try:
      data = json.loads(gzip.decompress(out))
    except Exception:  # noqa: BLE001
      try:
        data = json.loads(out)
      except Exception:  # noqa: BLE001
        return {}
    res: dict[str, dict[int, int]] = {}
    for f in data.get("files", []):
      lines = {}
      for ln in f.get("lines", []):
        c = ln.get("count", 0)
        if c:
          lines[ln.get("line_number")] = c
      if lines:
        res[f.get("file", "")] = lines
    return res

  def executed_lines(self, addr, bus, dat) -> tuple[bool, dict[str, dict[int, int]]]:
    self.lib.op_gcov_reset()
    allowed = bool(self.lib.safety_tx_hook(self._packet(addr, bus, dat)))
    self.lib.op_gcov_dump()
    cov = self._coverage()
    sdir = str(self.safety_dir)
    return allowed, {f: lines for f, lines in cov.items() if sdir in os.path.abspath(f)}

  def source_line(self, path: str, line: int) -> str:
    try:
      with open(path) as fh:
        return fh.readlines()[line - 1].strip()
    except Exception:  # noqa: BLE001
      return ""


_TRACE: SafetyTraceLib | None = None


def get_trace_lib() -> SafetyTraceLib:
  global _TRACE
  if _TRACE is None:
    _TRACE = SafetyTraceLib()
  return _TRACE
