from __future__ import annotations

import bz2
import hashlib
import json
import mmap
import multiprocessing
import os
import re
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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


# decompressed segments and their indexes persist here, so a route loads from disk in milliseconds
# after its first load, also across server restarts. LRU trimmed to LOGREADER_MCP_CACHE_GB
CACHE_DIR = Path(os.environ.get("LOGREADER_MCP_CACHE", Path.home() / ".cache" / "logreader_mcp"))
CACHE_GB = float(os.environ.get("LOGREADER_MCP_CACHE_GB", "30"))
RESOLVE_TTL = 24 * 3600


# signed urls change per request, the path names the file. local files also key on size and mtime
def _seg_key(identifier: str) -> str:
  path = identifier.split("?")[0]
  if os.path.exists(path):
    st = os.stat(path)
    path = f"{os.path.realpath(path)}:{st.st_size}:{st.st_mtime_ns}"
  return hashlib.sha1(path.encode()).hexdigest()[:20]


def _seg_paths(identifier: str) -> tuple[Path, Path, Path]:
  base = CACHE_DIR / "segments" / _seg_key(identifier)
  return base.with_suffix(".log"), base.with_suffix(".idx"), base.with_suffix(".can")


def _atomic_write(path: Path, write) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
  with open(tmp, "wb") as f:
    write(f)
  os.replace(tmp, path)


# named arrays as back to back .npy records, np.load on an npz spends most of its time in zip crcs
def _save_arrays(path: Path, arrays: dict[str, np.ndarray]) -> None:
  def write(f):
    np.save(f, np.array(list(arrays)))
    for a in arrays.values():
      np.save(f, a)
  _atomic_write(path, write)


def _load_arrays(path: Path) -> dict[str, np.ndarray]:
  with open(path, "rb") as f:
    keys = np.load(f).tolist()
    return {k: np.load(f) for k in keys}


def _mmap(path: Path):
  with open(path, "rb") as f:
    if os.fstat(f.fileno()).st_size == 0:
      return b""
    return mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)


# worker: service, time and location of every message, no message objects cross the process boundary.
# the first load writes the decompressed log and the index to the cache, later loads read only the index
def _scan_segment(identifier: str) -> dict:
  log_path, idx_path, _ = _seg_paths(identifier)
  if idx_path.exists() and log_path.exists():
    out = _load_arrays(idx_path)
    out["names"] = out["names"].tolist()
    return out
  dat = _read_segment(identifier)
  _atomic_write(log_path, lambda f: f.write(dat))
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
  out = {"names": names, "which": np.array([code[w] for w in which], dtype=np.int32), "t": np.array(t, dtype=np.int64),
         "off": np.array(offs[:n], dtype=np.int64), "size": np.array(sizes[:n], dtype=np.int64)}
  _save_arrays(idx_path, {**out, "names": np.array(names)})
  return out


# worker: one segment's can and sendcan frames, data packed into one buffer with offsets, cached on disk
def _segment_can(identifier: str) -> dict[str, tuple]:
  log_path, _, can_path = _seg_paths(identifier)
  if can_path.exists():
    z = _load_arrays(can_path)
    return {k: (z[f"{k}_t"], z[f"{k}_addr"], z[f"{k}_bus"], z[f"{k}_len"], z[f"{k}_buf"].tobytes()) for k in ("can", "sendcan")}
  cols: dict[str, tuple[list, list, list, list]] = {"can": ([], [], [], []), "sendcan": ([], [], [], [])}
  dat = _mmap(log_path) if log_path.exists() else _read_segment(identifier)
  for e in capnp_log.Event.read_multiple_bytes(dat[:]):
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
  arrays = {}
  for k, (t, a, b, ln, buf) in out.items():
    arrays.update({f"{k}_t": t, f"{k}_addr": a, f"{k}_bus": b, f"{k}_len": ln, f"{k}_buf": np.frombuffer(buf, dtype=np.uint8)})
  _save_arrays(can_path, arrays)
  return out


# least recently used segments go first once the cache is over its size
def _trim_cache() -> None:
  files = []
  for p in [*(CACHE_DIR / "segments").glob("*"), *(CACHE_DIR / "fields").glob("*")]:
    try:
      st = p.stat()
    except FileNotFoundError:
      continue
    files.append((st.st_atime, st.st_size, p))
  total, limit = sum(f[1] for f in files), CACHE_GB * 1e9
  for _, size, p in sorted(files):
    if total <= limit:
      break
    p.unlink(missing_ok=True)
    total -= size


# a local directory of segments: <dir>/<n>/rlog.zst, <dir>/<route>--<n>/rlog.bz2 or <dir>/<route>--<n>--rlog.bz2
def _local_dir_ids(directory: str, mode: ReadMode) -> list[str]:
  found: dict[str, dict[int, str]] = {"rlog": {}, "qlog": {}}
  for root, dirs, files in os.walk(directory, followlinks=True):
    if len(Path(root).relative_to(directory).parts) >= 2:
      dirs.clear()
    for name in sorted(files):
      m = re.search(r"(rlog|qlog)(\.bz2|\.zst)?$", name)
      if not m:
        continue
      rel = os.path.relpath(os.path.join(root, name), directory)
      num = re.findall(r"(\d+)", rel[:-len(m.group(0))])
      found[m.group(1)].setdefault(int(num[-1]) if num else 0, os.path.join(root, name))
  rlogs, qlogs = found["rlog"], found["qlog"]
  if mode == ReadMode.QLOG:
    files = qlogs
  elif mode == ReadMode.RLOG:
    files = rlogs
  else:
    files = {**qlogs, **rlogs}
  if not files:
    raise FileNotFoundError(f"no {'qlog' if mode == ReadMode.QLOG else 'rlog'} files under {directory}")
  return [files[k] for k in sorted(files)]


def _resolve_remote(identifier: str, mode: ReadMode) -> list[str]:
  return LogReader(identifier, default_mode=mode, sort_by_time=True).logreader_identifiers


# route name to its segment files, remembered for a day since a remote listing costs about a second.
# returns the files and whether they came from that memory
def _resolve(identifier: str, mode: ReadMode) -> tuple[list[str], bool]:
  if os.path.isdir(identifier):
    return _local_dir_ids(identifier, mode), False
  if os.path.exists(identifier) or identifier.startswith(("http://", "https://", "cd:/")):
    return [identifier], False
  path = CACHE_DIR / "routes" / (hashlib.sha1(f"{identifier}::{mode}".encode()).hexdigest()[:20] + ".json")
  try:
    if time.time() - path.stat().st_mtime < RESOLVE_TTL:  # noqa: TID251
      return json.loads(path.read_text()), True
  except (FileNotFoundError, ValueError):
    pass
  ids = _resolve_remote(identifier, mode)
  _atomic_write(path, lambda f: f.write(json.dumps(ids).encode()))
  return ids, False


HEADER_SERVICES = {"initData", "sentinel"}


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


def _is_list(v) -> bool:
  return type(v).__name__ in ("_DynamicListReader", "_DynamicListBuilder")


# 'a/b/c' through structs. on a list (list-type services like pandaStates or onroadEvents, or
# list fields) a numeric part picks that element, any other part reads from the first element.
# a missing element reads as None
def _get_path(msg, parts: list[str]):
  for p in parts:
    if _is_list(msg):
      i = int(p) if p.isdigit() else 0
      if i >= len(msg):
        return None
      msg = msg[i]
      if p.isdigit():
        continue
    msg = getattr(msg, p)
  return msg


class RouteData:
  def __init__(self, identifier: str, mode: ReadMode = ReadMode.RLOG):
    self.identifier = identifier
    self.mode = mode
    self.ids, self._remembered = _resolve(identifier, mode)
    self._ids_key: str | None = None
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
          merged = self._ids_path(".idx")
          if merged.exists() and all(_seg_paths(sid)[0].exists() for sid in self.ids):
            z = _load_arrays(merged)
          else:
            z = self._merge(self._map(_scan_segment, _seg_paths, 1))
            _save_arrays(merged, z)
          names, b = z["names"].tolist(), z["bounds"]
          self._index = {svc: {k: z[k][b[i]:b[i + 1]] for k in ("t", "seg", "off", "size")}
                         for i, svc in enumerate(names) if svc and b[i + 1] > b[i]}
    return self._index

  # every segment's messages sorted by (service, time), services as contiguous runs between bounds.
  # a stable int16 sort is a radix sort, and logs are nearly time ordered so the per service sort is cheap
  @staticmethod
  def _merge(parts: list[dict]) -> dict[str, np.ndarray]:
    names = sorted({n for p in parts for n in p["names"]})
    code = np.concatenate([np.array([names.index(n) for n in p["names"]], dtype=np.int16)[p["which"]] for p in parts])
    t = np.concatenate([p["t"] for p in parts])
    order = np.argsort(code, kind="stable")
    bounds = np.searchsorted(code[order], np.arange(len(names) + 1))
    for a, b in zip(bounds[:-1], bounds[1:], strict=True):
      order[a:b] = order[a:b][np.argsort(t[order[a:b]], kind="stable")]
    seg = np.concatenate([np.full(len(p["t"]), i) for i, p in enumerate(parts)])
    off, size = np.concatenate([p["off"] for p in parts]), np.concatenate([p["size"] for p in parts])
    return {"names": np.array(names), "bounds": bounds, "t": t[order] / 1e9, "seg": seg[order], "off": off[order], "size": size[order]}

  def _ids_path(self, suffix: str) -> Path:
    if self._ids_key is None:
      self._ids_key = hashlib.sha1("|".join(_seg_key(i) for i in self.ids).encode()).hexdigest()[:24]
    return CACHE_DIR / "fields" / f"{self._ids_key}{suffix}"

  @property
  def service_counts(self) -> dict[str, int]:
    return {svc: len(v["t"]) for svc, v in self.index.items()}

  def times(self, service: str) -> np.ndarray:
    return self.index[service]["t"] if service in self.index else np.zeros(0)

  # cached segments load in this process, the rest in parallel workers that also fill the cache.
  # a cached route list can hold expired signed urls, so a miss re-resolves the route first
  def _map(self, fn, paths, which: int) -> list:
    todo = [i for i, sid in enumerate(self.ids) if not paths(sid)[which].exists()]
    if todo and self._remembered:
      self._remembered = False
      try:
        fresh = _resolve_remote(self.identifier, self.mode)
        if [_seg_key(i) for i in fresh] == [_seg_key(i) for i in self.ids]:
          self.ids = fresh
      except Exception:  # noqa: BLE001
        pass  # offline, try the remembered urls
    out: list = [None] * len(self.ids)
    if todo:
      with _pool(len(todo)) as pool:
        for i, r in zip(todo, pool.map(fn, [self.ids[i] for i in todo]), strict=True):
          out[i] = r
      threading.Thread(target=_trim_cache, daemon=True).start()
    rest = [i for i in range(len(self.ids)) if out[i] is None]
    with ThreadPoolExecutor(max(1, min(len(rest), 16))) as ex:
      for i, r in zip(rest, ex.map(fn, [self.ids[i] for i in rest]), strict=True):
        out[i] = r
    return out

  def _buffers(self) -> list:
    if self._bufs is None:
      bufs = []
      for sid in self.ids:
        log_path = _seg_paths(sid)[0]
        if not log_path.exists():
          _scan_segment(sid)
        bufs.append(_mmap(log_path))
      self._bufs = bufs
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

  # numeric and text fields persist next to the segments, keyed by the exact segment files
  def _field_path(self, service: str, path: str) -> Path:
    return self._ids_path(f"_{hashlib.sha1(f'{service}|{path}'.encode()).hexdigest()[:16]}.npy")

  # scalar field by 'a/b/c' path, read straight off the messages
  def field(self, service: str, path: str) -> np.ndarray:
    key = (service, path)
    if key not in self._fields:
      cached = self._field_path(service, path)
      if cached.exists():
        self._fields[key] = np.load(cached)
        return self._fields[key]
      self._fields[key] = self._read_field(service, path)
      if self._fields[key].dtype != object:
        _atomic_write(cached, lambda f: np.save(f, self._fields[key]))
    return self._fields[key]

  def _read_field(self, service: str, path: str) -> np.ndarray:
    vals = [_get_path(m, [service, *path.split("/")]) for m in self.msgs(service)]
    present = next((v for v in vals if v is not None), None)
    if present is None and vals:
      return np.full(len(vals), np.nan)
    if isinstance(present, (bool, int, float)) and not isinstance(present, str) and None in vals:
      vals = [np.nan if v is None else v for v in vals]  # empty list element, e.g. no panda yet
    elif None in vals:
      vals = ["" if v is None else v for v in vals]
    if vals and not isinstance(present, (bool, int, float, str)):
      # enums read as their name, lists and structs stay objects so callers see them as non-scalar
      vals = [str(v) if type(v).__name__ == "_DynamicEnum" else v for v in vals]
      if all(isinstance(v, str) for v in vals):
        return np.array(vals)
      # fill one by one, np.array would unpack capnp lists into a 2d array
      arr = np.empty(len(vals), dtype=object)
      for i, v in enumerate(vals):
        arr[i] = v
      return arr
    return np.array(vals)

  # the first n messages of a service without parsing the rest
  def first_msgs(self, service: str, n: int = 1) -> list:
    if service in self._msgs:
      return self._msgs[service][:n]
    ix = self.index.get(service)
    if ix is None:
      return []
    bufs = self._buffers()
    return [next(iter(capnp_log.Event.read_multiple_bytes(bufs[s][o:o + sz])))
            for s, o, sz in zip(ix["seg"][:n].tolist(), ix["off"][:n].tolist(), ix["size"][:n].tolist(), strict=True)]

  def is_list_service(self, service: str) -> bool:
    msgs = self.first_msgs(service)
    return bool(msgs) and _is_list(getattr(msgs[0], service))

  # flattened field names of one message, nested structs joined by '/'. for a list-type service,
  # the fields of its elements (from the first message with one)
  def field_names(self, service: str) -> list[str]:
    msgs = self.first_msgs(service)
    if not msgs:
      return []
    if _is_list(getattr(msgs[0], service)) and not len(getattr(msgs[0], service)):
      msgs = self.msgs(service)
    names = []

    def walk(d, prefix):
      for k, v in d.items():
        p = f"{prefix}/{k}" if prefix else k
        if isinstance(v, dict):
          walk(v, p)
        else:
          names.append(p)
    root = getattr(msgs[0], service)
    if _is_list(root):
      root = next((r[0] for r in (getattr(m, service) for m in msgs) if len(r)), None)
      if root is None:
        return []
    walk(root.to_dict(verbose=True), "")
    return names

  # every segment came from qlogs, which keep only every n-th message of a service
  @property
  def is_qlog(self) -> bool:
    return all("qlog" in i.split("?")[0].rsplit("/", 1)[-1] for i in self.ids)

  @property
  def car_params(self):
    msgs = self.msgs("carParams")
    return msgs[0].carParams if msgs else None

  @property
  def can_cached(self) -> bool:
    return self._can is not None or self._ids_path(".can").exists()

  # merged and time sorted once, then kept on disk with the frame bytes memory mapped
  def _build_can(self):
    merged = self._ids_path(".can")
    bufs = {stream: self._ids_path(f".{stream}buf") for stream in ("can", "sendcan")}
    if not (merged.exists() and all(p.exists() for p in bufs.values())):
      parts = self._map(_segment_can, _seg_paths, 2)
      arrays = {}
      for stream in ("can", "sendcan"):
        segs = [p[stream] for p in parts]
        t, a, b, ln = (np.concatenate([s[i] for s in segs]) for i in range(4))
        off = (np.cumsum(ln) - ln).astype(np.int64)
        order = np.argsort(t, kind="stable")
        arrays.update({f"{stream}_{k}": v[order] for k, v in (("t", t), ("addr", a), ("bus", b), ("off", off), ("len", ln))})
        _atomic_write(bufs[stream], lambda f, segs=segs: [f.write(s[4]) for s in segs])
      _save_arrays(merged, arrays)
    z = _load_arrays(merged)
    for stream in ("can", "sendcan"):
      setattr(self, f"_{stream}", CanIndex(*(z[f"{stream}_{k}"] for k in ("t", "addr", "bus", "off", "len")), _mmap(bufs[stream])))

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

  # time span of the logged data. initData is repeated in every segment carrying the route's start
  # time, so it would stretch a partial segment range back to the route start
  def span(self) -> tuple[float, float]:
    ts = [v["t"] for svc, v in self.index.items() if len(v["t"]) and svc not in HEADER_SERVICES]
    return (float(min(t[0] for t in ts)), float(max(t[-1] for t in ts))) if ts else (0.0, 0.0)

  # seconds since the route started: logMonoTime since initData, which every segment repeats with the route's
  # start time, so a segment range like '/3:5' starts near 180 and agrees with a full load and mici-ui-mcp's render_clip
  def route_time(self, t: np.ndarray) -> np.ndarray:
    init = self.times("initData")
    if len(init):
      return t - float(init[0])
    m = re.search(r"(?:/|--)(\d+)(?:/|--)[rq]log", self.ids[0].split("?")[0]) if self.ids else None
    return t - self.span()[0] + (60 * int(m.group(1)) if m else 0)

  def duration_s(self) -> float:
    start, end = self.span()
    return end - start


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
