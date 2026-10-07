from __future__ import annotations

import bz2
import multiprocessing
import struct
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .bootstrap import bootstrap

bootstrap()

from openpilot.cereal import log as capnp_log  # noqa: E402
from openpilot.tools.lib.filereader import FileReader  # noqa: E402
from openpilot.tools.lib.logreader import LogReader, ReadMode, decompress_stream  # noqa: E402


def _read_segment(identifier: str) -> bytes:
  with FileReader(identifier) as f:
    dat = f.read()
  path = identifier.split("?")[0]
  if path.endswith(".bz2") or dat.startswith(b"BZh9"):
    return bz2.decompress(dat)
  if path.endswith(".zst") or dat.startswith(b"\x28\xB5\x2F\xFD"):
    return decompress_stream(dat)
  return dat


# byte offset and size of each message in an unpacked capnp stream
def _framing(dat: bytes) -> tuple[list[int], list[int]]:
  offs, sizes = [], []
  pos, end = 0, len(dat)
  while pos + 4 <= end:
    nseg = struct.unpack_from("<I", dat, pos)[0] + 1
    hdr = (4 + 4 * nseg + 7) // 8 * 8
    size = hdr + 8 * sum(struct.unpack_from(f"<{nseg}I", dat, pos + 4))
    offs.append(pos)
    sizes.append(size)
    pos += size
  return offs, sizes


# worker: service, time and location of every message, no message objects cross the process boundary
def _scan_segment(identifier: str) -> dict:
  dat = _read_segment(identifier)
  offs, sizes = _framing(dat)
  which, t = [], []
  try:
    for e in capnp_log.Event.read_multiple_bytes(dat):
      try:
        which.append(e.which())
      except Exception:  # noqa: BLE001
        which.append("")
      t.append(e.logMonoTime)
  except Exception:  # noqa: BLE001
    pass  # corrupted tail, keep what parsed
  n = len(which)
  names = sorted(set(which))
  code = {w: i for i, w in enumerate(names)}
  return {"names": names, "which": np.array([code[w] for w in which], dtype=np.int32), "t": np.array(t, dtype=np.int64),
          "off": np.array(offs[:n], dtype=np.int64), "size": np.array(sizes[:n], dtype=np.int64)}


# worker: one segment's can and sendcan frames, data packed into one buffer with offsets
def _segment_can(identifier: str) -> dict[str, tuple]:
  cols: dict[str, tuple[list, list, list, list]] = {"can": ([], [], [], []), "sendcan": ([], [], [], [])}
  for e in capnp_log.Event.read_multiple_bytes(_read_segment(identifier)):
    try:
      stream = e.which()
    except Exception:  # noqa: BLE001
      continue
    if stream not in cols:
      continue
    t, a, b, d = cols[stream]
    tm = e.logMonoTime
    for c in getattr(e, stream):
      t.append(tm)
      a.append(c.address)
      b.append(c.src)
      d.append(c.dat)
  out = {}
  for k, (t, a, b, d) in cols.items():
    lens = np.fromiter((len(x) for x in d), dtype=np.int64, count=len(d))
    out[k] = (np.array(t, dtype=np.int64), np.array(a, dtype=np.int64), np.array(b, dtype=np.int64), lens, b"".join(d))
  return out


def _pool(n: int):
  return multiprocessing.get_context("fork").Pool(max(1, min(n, multiprocessing.cpu_count())))


class CanIndex:
  """CAN frames as parallel numpy arrays, frame bytes in one shared buffer."""

  def __init__(self, t_ns, addr, bus, off, ln, buf: bytes):
    self.t_ns, self.addr, self.bus, self.off, self.ln, self.buf = t_ns, addr, bus, off, ln, buf

  def __len__(self):
    return len(self.t_ns)

  def select(self, mask: np.ndarray) -> CanIndex:
    i = np.flatnonzero(mask)
    return CanIndex(self.t_ns[i], self.addr[i], self.bus[i], self.off[i], self.ln[i], self.buf)

  @property
  def dat(self) -> list[bytes]:
    buf = self.buf
    return [buf[o:o + n] for o, n in zip(self.off.tolist(), self.ln.tolist(), strict=True)]


def _get_path(msg, parts: list[str]):
  for p in parts:
    msg = getattr(msg, p)
  return msg


class RouteData:
  def __init__(self, identifier: str, mode: ReadMode = ReadMode.RLOG):
    self.identifier = identifier
    self.ids = LogReader(identifier, default_mode=mode, sort_by_time=True).logreader_identifiers
    self._lock = threading.Lock()
    self._index: dict | None = None
    self._bufs: list[bytes] | None = None
    self._msgs: dict[str, list] = {}
    self._fields: dict[tuple[str, str], np.ndarray] = {}
    self._can: CanIndex | None = None
    self._sendcan: CanIndex | None = None

  # parallel scan: per service, every message's time and (segment, offset, size), sorted by time.
  # LogReader can list segments out of order (e.g. 1 before 0), the time sort fixes that
  @property
  def index(self) -> dict:
    if self._index is None:
      with self._lock:
        if self._index is None:
          with _pool(len(self.ids)) as pool:
            parts = pool.map(_scan_segment, self.ids)
          index = {}
          for svc in sorted({n for p in parts for n in p["names"]} - {""}):
            cols = []
            for seg, p in enumerate(parts):
              if svc in p["names"]:
                m = p["which"] == p["names"].index(svc)
                cols.append((p["t"][m], np.full(int(m.sum()), seg), p["off"][m], p["size"][m]))
            t, seg, off, size = (np.concatenate(c) for c in zip(*cols, strict=True))
            order = np.argsort(t, kind="stable")
            index[svc] = {"t": t[order] / 1e9, "seg": seg[order], "off": off[order], "size": size[order]}
          self._index = index
    return self._index

  @property
  def service_counts(self) -> dict[str, int]:
    return {svc: len(v["t"]) for svc, v in self.index.items()}

  def times(self, service: str) -> np.ndarray:
    return self.index[service]["t"] if service in self.index else np.zeros(0)

  def _buffers(self) -> list[bytes]:
    if self._bufs is None:
      with ThreadPoolExecutor(len(self.ids)) as ex:  # file reads and zstd release the GIL
        self._bufs = list(ex.map(_read_segment, self.ids))
    return self._bufs

  # message objects of one service in time order, built only when a tool needs them
  def msgs(self, service: str) -> list:
    if service not in self._msgs:
      ix = self.index.get(service)
      if ix is None:
        return []
      bufs = self._buffers()
      # one message per slice. Event.from_bytes is a context manager in pycapnp 2, this reader outlives it
      self._msgs[service] = [next(iter(capnp_log.Event.read_multiple_bytes(bufs[s][o:o + n])))
                             for s, o, n in zip(ix["seg"].tolist(), ix["off"].tolist(), ix["size"].tolist(), strict=True)]
    return self._msgs[service]

  # scalar field by 'a/b/c' path, read straight off the messages
  def field(self, service: str, path: str) -> np.ndarray:
    key = (service, path)
    if key not in self._fields:
      parts = path.split("/")
      vals = [_get_path(m, [service, *parts]) for m in self.msgs(service)]
      if vals and not isinstance(vals[0], (bool, int, float, str)):
        # enums read as their name, lists and structs stay objects so callers see them as non-scalar
        vals = [str(v) if type(v).__name__ == "_DynamicEnum" else v for v in vals]
        if isinstance(vals[0], str):
          self._fields[key] = np.array(vals)
        else:
          # fill one by one, np.array would unpack capnp lists into a 2d array
          arr = np.empty(len(vals), dtype=object)
          for i, v in enumerate(vals):
            arr[i] = v
          self._fields[key] = arr
      else:
        self._fields[key] = np.array(vals)
    return self._fields[key]

  # flattened field names of one message, nested structs joined by '/'
  def field_names(self, service: str) -> list[str]:
    msgs = self.msgs(service)
    if not msgs:
      return []
    names = []

    def walk(d, prefix):
      for k, v in d.items():
        p = f"{prefix}/{k}" if prefix else k
        if isinstance(v, dict):
          walk(v, p)
        else:
          names.append(p)
    walk(getattr(msgs[0], service).to_dict(verbose=True), "")
    return names

  @property
  def car_params(self):
    msgs = self.msgs("carParams")
    return msgs[0].carParams if msgs else None

  def _build_can(self):
    with _pool(len(self.ids)) as pool:
      parts = pool.map(_segment_can, self.ids)
    for stream in ("can", "sendcan"):
      segs = [p[stream] for p in parts]
      t, a, b, ln = (np.concatenate([s[i] for s in segs]) for i in range(4))
      buf = b"".join(s[4] for s in segs)
      off = (np.cumsum(ln) - ln).astype(np.int64)
      order = np.argsort(t, kind="stable")
      setattr(self, f"_{stream}", CanIndex(t[order], a[order], b[order], off[order], ln[order], buf))

  @property
  def can(self) -> CanIndex:
    if self._can is None:
      with self._lock:
        if self._can is None:
          self._build_can()
    return self._can

  @property
  def sendcan(self) -> CanIndex:
    if self._sendcan is None:
      _ = self.can
    return self._sendcan

  def duration_s(self) -> float:
    ts = [v["t"] for v in self.index.values() if len(v["t"])]
    return float(max(t[-1] for t in ts) - min(t[0] for t in ts)) if ts else 0.0


_ROUTES: dict[str, RouteData] = {}
_ROUTES_LOCK = threading.Lock()


def get_route(identifier: str, mode: str = "r") -> RouteData:
  key = f"{identifier}::{mode}"
  with _ROUTES_LOCK:
    rd = _ROUTES.get(key)
    if rd is None:
      rd = RouteData(identifier, ReadMode(mode))
      _ROUTES[key] = rd
    return rd


def clear_cache() -> int:
  with _ROUTES_LOCK:
    n = len(_ROUTES)
    _ROUTES.clear()
    return n
