from __future__ import annotations

from typing import Any

from .bootstrap import bootstrap
from .route_cache import RouteData

bootstrap()

import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

from opendbc.car.structs import CarParams  # noqa: E402
from opendbc.safety.tests.libsafety import libsafety_py as LS  # noqa: E402

_LIB = None

# safety.c plus a loop that feeds a whole frame array through the hooks, so replay doesn't pay a
# python -> cffi round trip per frame. mirrors the python loop: timer, 10ms safety_tick, rx/tx hook
REPLAY_C = r"""
#include "opendbc/safety/tests/libsafety/safety.c"

static unsigned char len_to_dlc(int len) {
  static const int lens[16] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64};
  for (int i = 0; i < 16; i++) {
    if (lens[i] >= len) return (unsigned char)i;
  }
  return 15;
}

long replay_frames(long n, const long *t_ns, const long *kind, const long *addr, const long *bus, const long *off,
                   const long *len, const unsigned char *buf, long t0, long start_rel, long end_rel, long *last_tick,
                   unsigned char *ok) {
  for (long i = 0; i < n; i++) {
    long rel = t_ns[i] - t0;
    if (rel < start_rel) continue;
    if (rel > end_rel) return i;
    set_timer((uint32_t)(rel / 1000));
    if (t_ns[i] - *last_tick > 10000000) {
      safety_tick();
      *last_tick = t_ns[i];
    }
    CANPacket_t pkt = {0};
    pkt.extended = addr[i] >= 0x800;
    pkt.addr = (uint32_t)addr[i];
    pkt.bus = (unsigned char)bus[i];
    pkt.data_len_code = len_to_dlc((int)len[i]);
    for (long j = 0; j < len[i]; j++) pkt.data[j] = buf[off[i] + j];
    if (kind[i] == 0) {
      safety_rx_hook(&pkt);
    } else {
      ok[i] = safety_tx_hook(&pkt);
    }
  }
  return n;
}
"""

REPLAY_CDEF = """
void init_tests(void);
int set_safety_hooks(uint16_t mode, uint16_t param);
void set_alternative_experience(int mode);
long replay_frames(long n, const long *t_ns, const long *kind, const long *addr, const long *bus, const long *off,
                   const long *len, const unsigned char *buf, long t0, long start_rel, long end_rel, long *last_tick,
                   unsigned char *ok);
"""


# optimized build in a temp dir. libsafety_py builds -O0 with UBSan and leaves its object
# file inside the opendbc tree
def _libsafety():
  global _LIB
  if _LIB is None:
    from cffi import FFI
    include_root = Path(LS.libsafety_dir).parents[3]
    build = Path(tempfile.mkdtemp(prefix="logreader_mcp_safety_"))
    src, so = build / "replay.c", build / "libreplay.so"
    src.write_text(REPLAY_C)
    subprocess.check_call(["cc", "-fPIC", "-shared", "-O2", "-std=gnu11", "-nostdlib", "-fno-builtin", "-DALLOW_DEBUG",
                           "-I", str(include_root), str(src), "-o", str(so)])
    ffi = FFI()
    ffi.cdef(REPLAY_CDEF)
    _LIB = (ffi, ffi.dlopen(str(so)))
    shutil.rmtree(build, ignore_errors=True)
  return _LIB


_SKIP_MODES = {"silent", "noOutput", "elm327", "elm"}


def _safety_enum() -> dict[str, int]:
  return {str(k): int(v) for k, v in CarParams.SafetyModel.schema.enumerants.items()}


def active_safety_config(rd: RouteData) -> dict[str, Any]:
  cp = rd.car_params
  if cp is None:
    return {"error": "no carParams in route; cannot determine safety model"}
  enum = _safety_enum()
  configs = []
  for sc in cp.safetyConfigs:
    name = str(sc.safetyModel)
    configs.append({"model": name, "mode_int": enum.get(name), "param": int(sc.safetyParam)})
  chosen = None
  for c in reversed(configs):
    if c["model"] not in _SKIP_MODES and c["mode_int"] is not None:
      chosen = c
      break
  if chosen is None and configs:
    chosen = configs[-1]
  return {"carFingerprint": cp.carFingerprint, "alternativeExperience": int(cp.alternativeExperience),
          "configs": configs, "active": chosen}


# rx and tx frames in time order (rx first on ties) as arrays over one data buffer.
# drops panda TX echoes (src=bus+0x80), CANPacket bus is 3-bit
def _merged(rd: RouteData) -> dict[str, Any]:
  c, s = rd.can, rd.sendcan
  rx, tx = np.flatnonzero(c.bus < 8), np.flatnonzero(s.bus < 8)
  cols = {
    "t": np.concatenate([c.t_ns[rx], s.t_ns[tx]]),
    "kind": np.concatenate([np.zeros(len(rx), dtype=np.int64), np.ones(len(tx), dtype=np.int64)]),
    "addr": np.concatenate([c.addr[rx], s.addr[tx]]),
    "bus": np.concatenate([c.bus[rx], s.bus[tx]]),
    "off": np.concatenate([c.off[rx], s.off[tx] + len(c.buf)]),
    "len": np.concatenate([c.ln[rx], s.ln[tx]]),
  }
  order = np.lexsort((cols["kind"], cols["t"]))
  out = {k: np.ascontiguousarray(v[order], dtype=np.int64) for k, v in cols.items()}
  out["buf"] = c.buf + s.buf
  return out


def _interleave(rd: RouteData):
  m = _merged(rd)
  buf = m["buf"]
  return [(t, k, a, b, buf[o:o + n]) for t, k, a, b, o, n in
          zip(*(m[c].tolist() for c in ("t", "kind", "addr", "bus", "off", "len")), strict=True)]


def replay(rd: RouteData, t_start: float | None = None, t_end: float | None = None) -> dict[str, Any]:
  cfg = active_safety_config(rd)
  if "error" in cfg or not cfg.get("active"):
    return {"error": cfg.get("error", "no usable safety config")}
  active = cfg["active"]
  ffi, lib = _libsafety()
  lib.init_tests()
  set_rc = lib.set_safety_hooks(active["mode_int"], active["param"])
  lib.set_alternative_experience(cfg["alternativeExperience"])
  if set_rc != 0:
    return {"error": f"set_safety_hooks failed rc={set_rc} for {active}"}

  m = _merged(rd)
  n = len(m["t"])
  if not n:
    return {"error": "no can/sendcan frames in route"}
  t0 = int(m["t"][0])
  ok = np.full(n, 2, dtype=np.uint8)  # 2 = not run (rx, or outside the window)
  last_tick = ffi.new("long *", 0)
  ptr = {k: ffi.cast("long *", ffi.from_buffer(m[k])) for k in ("t", "kind", "addr", "bus", "off", "len")}
  lib.replay_frames(n, ptr["t"], ptr["kind"], ptr["addr"], ptr["bus"], ptr["off"], ptr["len"], ffi.from_buffer(m["buf"]), t0,
                    int(t_start * 1e9) if t_start is not None else -(1 << 62),
                    int(t_end * 1e9) if t_end is not None else (1 << 62), last_tick, ffi.from_buffer(ok))

  ran_tx = (m["kind"] == 1) & (ok != 2)
  blocked_i = np.flatnonzero(ran_tx & (ok == 0))
  allowed = ran_tx & (ok == 1)
  allowed_keys = set(zip(m["bus"][allowed].tolist(), m["addr"][allowed].tolist(), strict=True))
  blocked: dict[tuple[int, int], dict[str, Any]] = {}
  for i in blocked_i:
    b, a = int(m["bus"][i]), int(m["addr"][i])
    e = blocked.get((b, a))
    if e is None:
      o, ln = int(m["off"][i]), int(m["len"][i])
      e = blocked[(b, a)] = {"bus": b, "address": a, "address_hex": hex(a), "blocked": 0,
                             "first_t": round((int(m["t"][i]) - t0) / 1e9, 3), "sample_dat": m["buf"][o:o + ln].hex()}
    e["blocked"] += 1
  groups = sorted(blocked.values(), key=lambda g: -g["blocked"])
  for g in groups:
    g["also_allowed_sometimes"] = (g["bus"], g["address"]) in allowed_keys
  return {"route": rd.identifier, "safety": active, "n_tx": int(ran_tx.sum()),
          "n_blocked": len(blocked_i), "n_blocked_addresses": len(groups),
          "note": "replay applies the route's final safety mode to the whole log; "
                  "diagnostic/UDS frames sent during early fingerprinting (when panda "
                  "was still in ELM/allOutput) may be over-flagged. Cross-check with "
                  "panda_blocked_messages for hardware truth.",
          "blocked": groups}


_RETURNED_OFFSET = 0x80   # panda CAN_RETURNED_BUS_OFFSET: accepted TX echoed back
_REJECTED_OFFSET = 0xC0   # panda CAN_REJECTED_BUS_OFFSET: TX panda refused to send


def blocked_via_echo(rd: RouteData, window_ms: float = 100.0) -> dict[str, Any]:
  can, s = rd.can, rd.sendcan
  win = window_ms * 1e6
  # nearest echo per tx frame, searched within each (addr, bus). only a handful of tx addresses
  em = (can.bus >= _RETURNED_OFFSET) & (can.bus < _REJECTED_OFFSET)
  ekey, et = (can.addr[em] << 8) | (can.bus[em] - _RETURNED_OFFSET), can.t_ns[em]
  skey = (s.addr << 8) | s.bus
  near = np.full(len(s), np.inf)
  for k in np.unique(skey):
    si = np.flatnonzero(skey == k)
    echoes = np.sort(et[ekey == k])
    if not len(echoes):
      continue
    st = s.t_ns[si]
    p = np.searchsorted(echoes, st)
    for q in (p - 1, p):
      ok = (q >= 0) & (q < len(echoes))
      near[si[ok]] = np.minimum(near[si[ok]], np.abs(echoes[q[ok]] - st[ok]))
  miss = np.flatnonzero(near > win)
  blocked: dict[tuple[int, int], dict[str, Any]] = {}
  n_blocked = len(miss)
  t0 = s.t_ns[0] if len(s) else 0
  for i in miss:
    b, a = int(s.bus[i]), int(s.addr[i])
    e = blocked.get((b, a))
    if e is None:
      e = blocked[(b, a)] = {"bus": b, "address": a, "address_hex": hex(a),
                             "no_echo": 0, "first_t": round((int(s.t_ns[i]) - t0) / 1e9, 3)}
    e["no_echo"] += 1
  return {"route": rd.identifier, "method": "echo_diff",
          "note": "no_echo strongly implies a panda block; cross-check with panda_replay",
          "n_sendcan": len(s), "n_no_echo": n_blocked,
          "blocked": sorted(blocked.values(), key=lambda g: -g["no_echo"])}


def root_cause(rd: RouteData, address: int | None = None, bus: int | None = None,
               t_start: float | None = None, t_end: float | None = None,
               max_groups: int = 10) -> dict[str, Any]:
  from .panda_trace import get_trace_lib
  cfg = active_safety_config(rd)
  if "error" in cfg or not cfg.get("active"):
    return {"error": cfg.get("error", "no usable safety config")}
  active = cfg["active"]
  trace = get_trace_lib()
  if not trace.ok:
    return {"error": f"gcov trace unavailable ({trace.reason}); use panda_replay for verdicts",
            "safety": active}
  trace.setup(active["mode_int"], active["param"], cfg["alternativeExperience"])

  events = _interleave(rd)
  if not events:
    return {"error": "no can/sendcan frames"}
  t0 = events[0][0]
  last_tick = 0
  blocked_cov: dict[tuple[int, int], dict] = {}
  allowed_cov: dict[tuple[int, int], dict] = {}
  for t_ns, kind, addr, b, dat in events:
    tsec = (t_ns - t0) / 1e9
    if t_start is not None and tsec < t_start:
      continue
    if t_end is not None and tsec > t_end:
      break
    trace.set_timer((t_ns - t0) // 1000)
    if t_ns - last_tick > 10_000_000:
      trace.lib.safety_tick()
      last_tick = t_ns
    if kind == 0:
      trace.rx(addr, b, dat)
      continue
    if address is not None and addr != address:
      continue
    if bus is not None and b != bus:
      continue
    k = (b, addr)
    need_blocked = k not in blocked_cov
    need_allowed = k not in allowed_cov
    if not (need_blocked or need_allowed):
      continue
    # tx_hook mutates rate-limit state, so use this call's verdict, never re-call
    allowed, cov = trace.executed_lines(addr, b, dat)
    if not allowed and need_blocked:
      blocked_cov[k] = {"t": round(tsec, 3), "dat": dat.hex(), "cov": cov}
    elif allowed and need_allowed:
      allowed_cov[k] = {"t": round(tsec, 3), "dat": dat.hex(), "cov": cov}

  results = []
  for k, info in list(blocked_cov.items())[:max_groups]:
    bnum, addr = k
    blk_lines = {(f, ln) for f, lines in info["cov"].items() for ln in lines}
    allow = allowed_cov.get(k)
    diff = blk_lines
    if allow:
      allow_lines = {(f, ln) for f, lines in allow["cov"].items() for ln in lines}
      diff = blk_lines - allow_lines
    culprits = []
    for f, ln in sorted(diff):
      src = trace.source_line(f, ln)
      if not src:
        continue
      low = src.lower()
      decisive = any(s in low for s in ("false", "violation", "= tx", "return", "_check(", "_limit", "block"))
      culprits.append({"file": f.split("/opendbc/")[-1], "line": ln, "code": src, "decisive": decisive})
    results.append({
      "bus": bnum, "address": addr, "address_hex": hex(addr),
      "first_blocked_t": info["t"], "sample_dat": info["dat"],
      "has_allowed_baseline": allow is not None,
      "decisive_lines": [c for c in culprits if c["decisive"]][:20],
      "root_cause_lines": culprits[:60],
    })
  return {"route": rd.identifier, "safety": active,
          "n_blocked_addresses": len(blocked_cov), "results": results}
