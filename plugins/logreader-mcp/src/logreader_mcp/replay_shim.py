# C loop that feeds a frame array through the safety hooks, so replay doesn't pay a python ->
# cffi round trip per frame. Linked next to a compiled safety.c (plain or gcov-instrumented).
# Per frame, like the python loop it replaces: skip before the window, stop after it, set_timer,
# safety_tick every 10ms, then the rx hook, or the tx hook when run_tx (ok[i] gets the verdict)
import subprocess
from pathlib import Path

SOURCE = r"""
#include <stdbool.h>
#include <stdint.h>
#include "opendbc/safety/can.h"

void set_timer(uint32_t t);
void safety_tick(void);
bool safety_rx_hook(const CANPacket_t *msg);
bool safety_tx_hook(CANPacket_t *msg);

static unsigned char len_to_dlc(long len) {
  for (unsigned char i = 0; i < 16U; i++) {
    if (dlc_to_len[i] >= len) return i;
  }
  return 15U;
}

long replay_frames(long n, const long *t_ns, const long *kind, const long *addr, const long *bus, const long *off,
                   const long *len, const unsigned char *buf, long t0, long start_rel, long end_rel, long *last_tick,
                   int run_tx, unsigned char *ok) {
  for (long i = 0; i < n; i++) {
    long rel = t_ns[i] - t0;
    if (rel < start_rel) continue;
    if (rel > end_rel) return i;
    set_timer((uint32_t)(rel / 1000));
    if (t_ns[i] - *last_tick > 10000000) {
      safety_tick();
      *last_tick = t_ns[i];
    }
    if (kind[i] != 0 && !run_tx) continue;
    CANPacket_t pkt = {0};
    pkt.extended = addr[i] >= 0x800;
    pkt.addr = (uint32_t)addr[i];
    pkt.bus = (unsigned char)bus[i];
    pkt.data_len_code = len_to_dlc(len[i]);
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

CDEF = """
long replay_frames(long n, const long *t_ns, const long *kind, const long *addr, const long *bus, const long *off,
                   const long *len, const unsigned char *buf, long t0, long start_rel, long end_rel, long *last_tick,
                   int run_tx, unsigned char *ok);
"""

COLUMNS = ("t", "kind", "addr", "bus", "off", "len")


def build_object(build: Path, include_root: Path) -> Path:
  src, obj = build / "replay_shim.c", build / "replay_shim.o"
  src.write_text(SOURCE)
  subprocess.check_call(["cc", "-fPIC", "-O2", "-std=gnu11", "-nostdlib", "-fno-builtin", "-I", str(include_root),
                         "-c", str(src), "-o", str(obj)])
  return obj


class Frames:
  """Merged frame columns (int64 numpy arrays plus one data buffer) as cffi pointers."""

  def __init__(self, ffi, merged: dict):
    self.m = merged
    self.n = len(merged["t"])
    self.t0 = int(merged["t"][0]) if self.n else 0
    self._ptr = {k: ffi.cast("long *", ffi.from_buffer(merged[k])) for k in COLUMNS}
    self._buf = ffi.from_buffer(merged["buf"])
    self.last_tick = ffi.new("long *", 0)

  # run frames [i, j). returns the index it stopped at, < j when the window ended
  def run(self, lib, i: int, j: int, start_rel: int, end_rel: int, run_tx: bool, ok_ptr) -> int:
    p = self._ptr
    stop = lib.replay_frames(j - i, p["t"] + i, p["kind"] + i, p["addr"] + i, p["bus"] + i, p["off"] + i, p["len"] + i,
                             self._buf, self.t0, start_rel, end_rel, self.last_tick, int(run_tx), ok_ptr + i)
    return i + stop
