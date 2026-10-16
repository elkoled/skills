# run with the checkout's python, repo root on PYTHONPATH:
#   fast_clip.py --route DONGLE/ROUTE --start S --end E -o OUT.mp4 [--data-dir D] [--big] [--qcam] [--speed N]
#                [--map NPZ] [--workers N] [--overlay DIR] [--verbose]
# renders the onroad UI over a route window to an mp4, cold, as fast as the UI code itself allows:
#   - a helper process forked before the UI imports decompresses and frames the logs and starts
#     the camera decode, so none of that competes with the imports for the GIL
#   - the parent imports the UI once, rasterizes fonts in a thread, then forks the render workers
#     (no GL context exists before the fork, every worker opens its own)
#   - each worker renders a slice of frames after a one second pre-roll, so filters and fades settle.
#     log messages are parsed only when the UI reads them
#   - the camera is decoded once: NVDEC in GOP order matching the workers when there is an NVIDIA GPU,
#     else one single threaded ffmpeg per GOP in parallel. Workers read frames from /dev/shm as they land
#   - each worker encodes its own h264 part, the parts are joined without re-encoding
# --map serves recorded map frames (npz with t: modelV2 logMonoTime, maps: (n, 256, 256) uint8) on
# VisionIpc 'mapsd' and enables navigation, so the nav view renders like onroad
import argparse
import functools
import json
import mmap
import os
import struct
import subprocess
import sys
import threading
import time

T_START = time.perf_counter()
FPS = 20
SEGMENT_FRAMES = FPS * 60
SM_START = 2  # first SubMaster frame count of a worker
# UI clock at the clip start. Pre-roll frames are negative, and a clock near 0 reads as fresh to timers
# that start at 0 (the map's reconnect wait, set speed and chestnut fades), like a device that just booted
CLOCK_START = 10.0
PREROLL = 4 * FPS  # frames before a worker's slice: onroad starts on a 2 Hz deviceState, then the longest timers (set speed, chestnut fade) are 2.5s and a fade
F_SETPIPE_SZ = 1031


class Timer:
  def __init__(self):
    self.last = time.perf_counter()
    self.laps: list[str] = []

  def lap(self, name: str):
    now = time.perf_counter()
    self.laps.append(f"{name}={now - self.last:.2f}@{now - T_START:.2f}")
    self.last = now


def parse_args():
  p = argparse.ArgumentParser()
  p.add_argument("--route", required=True)
  p.add_argument("--start", type=int, required=True)
  p.add_argument("--end", type=int, required=True)
  p.add_argument("-o", "--output", required=True)
  p.add_argument("--data-dir", default=None)
  p.add_argument("--big", action="store_true")
  p.add_argument("--qcam", action="store_true")
  p.add_argument("--speed", type=float, default=1.0)
  p.add_argument("--map", default=None)
  p.add_argument("--workers", type=int, default=0)
  p.add_argument("--overlay", default="")
  p.add_argument("--crf", type=int, default=23)
  p.add_argument("--decode", choices=("auto", "gpu", "cpu"), default="auto")
  p.add_argument("--verbose", action="store_true")
  return p.parse_args()


class Background:
  """Runs fn in a thread, result() joins and re-raises."""
  def __init__(self, fn, *args):
    self.value, self.error = None, None
    self.thread = threading.Thread(target=self._run, args=(fn, *args), daemon=True)
    self.thread.start()

  def _run(self, fn, *args):
    try:
      self.value = fn(*args)
    except BaseException as e:  # noqa: BLE001
      self.error = e

  def result(self):
    self.thread.join()
    if self.error is not None:
      raise self.error
    return self.value


# ---- logs -------------------------------------------------------------------------------------

def read_cached(url: str) -> bytes | None:
  """A downloaded file straight from openpilot's chunked download cache, joined once (its reader appends
  chunk by chunk, quadratic for a 40 MB camera file). None if any chunk is missing."""
  try:
    from openpilot.common.hardware.hw import Paths
    from openpilot.tools.lib.url_file import CHUNK_SIZE, hash_url
  except ImportError:
    return None
  root, key = Paths.download_cache_root(), hash_url(url)
  try:
    with open(os.path.join(root, key + "_length")) as f:
      length = int(f.read())
    chunks = []
    for pos in range(0, length, CHUNK_SIZE):
      with open(os.path.join(root, f"{key}_{pos / CHUNK_SIZE}"), "rb") as f:
        chunks.append(f.read())
  except (OSError, ValueError):
    return None
  dat = b"".join(chunks)
  return dat if len(dat) == length else None


def read_file(path: str) -> bytes:
  if os.path.exists(path):
    with open(path, "rb") as f:
      return f.read()
  if path.startswith(("http://", "https://")) and (dat := read_cached(path)) is not None:
    return dat
  from openpilot.tools.lib.filereader import FileReader
  with FileReader(path) as f:
    return f.read()


def read_log(path: str) -> bytes:
  dat = read_file(path)
  if dat.startswith(b"\x28\xb5\x2f\xfd"):
    import zstandard
    return zstandard.ZstdDecompressor().decompressobj().decompress(dat)
  if dat.startswith(b"BZh"):
    import bz2
    return bz2.decompress(dat)
  return dat


def frame_messages(dat: bytes, disc_offset: int):
  """Offset, size, logMonoTime and union discriminant of every message, from the capnp stream framing alone."""
  offs, sizes, monos, discs = [], [], [], []
  unpack_from = struct.unpack_from
  pos, n = 0, len(dat)
  while pos + 8 <= n:
    nseg = unpack_from("<I", dat, pos)[0] + 1
    header = (4 * (nseg + 1) + 7) & ~7
    size = header + 8 * sum(unpack_from(f"<{nseg}I", dat, pos + 4))
    seg0 = pos + header
    data = seg0 + 8 + 8 * (unpack_from("<i", dat, seg0)[0] >> 2)
    offs.append(pos)
    sizes.append(size)
    monos.append(unpack_from("<Q", dat, data)[0])
    discs.append(unpack_from("<H", dat, data + disc_offset)[0])
    pos += size
  return offs, sizes, monos, discs


# route time is logMonoTime since initData, which every segment repeats with the same time. logreader's
# times use the same clock, so its find_windows moments land on the same frames here
FRAME_NS = 10**9 // FPS
SEGMENT_SLACK = 3 * FPS  # covers a segment's data starting after its 60 s mark
ENCODE_IDX = ("narrowRoadEncodeIdx", "roadEncodeIdx", "qNarrowRoadEncodeIdx", "qRoadEncodeIdx")


def camera_frame(frame: int, offsets: dict) -> int:
  """The camera frame (segment * SEGMENT_FRAMES + frame in the segment) shown at a route frame. A segment's video
  starts a little after its 60 s mark on the route clock and drifts, offsets holds where per segment."""
  seg = max((int(s) for s, o in offsets.items() if int(s) * SEGMENT_FRAMES + o <= frame), default=min(map(int, offsets)))
  return frame - offsets[str(seg)]


# ---- camera -----------------------------------------------------------------------------------

def scan_camera(path: str, hevc: bool = True):
  """The file, its parameter sets and (frame, byte offset) of each keyframe (comma cameras send parameter sets once)."""
  import numpy as np
  raw = read_file(path)
  if not hevc:
    # qcameras are h264 in mpegts, its packets can't be cut at keyframes, so a segment decodes whole (it's small)
    return raw, b"", [(0, 0)]
  b = np.frombuffer(raw, dtype=np.uint8)
  i = np.flatnonzero((b[:-5] == 0) & (b[1:-4] == 0) & (b[2:-3] == 1))
  typ = (b[i + 3] >> 1) & 0x3f
  vcl = typ <= 21
  first_slice = vcl & ((b[i + 5] & 0x80) > 0)
  pics, pic_types = i[first_slice], typ[first_slice]
  keys = [(int(k), int(pics[k])) for k in np.flatnonzero(pic_types >= 16)]  # IRAP pictures start a GOP
  return raw, raw[:int(i[vcl][0])], keys


def hevc_size(header: bytes) -> tuple[int, int] | None:
  """Frame size from the HEVC sequence parameter set, cropped to the conformance window like ffprobe."""
  i = 0
  while True:
    i = header.find(b"\x00\x00\x01", i)
    if i < 0 or i + 3 >= len(header):
      return None
    if (header[i + 3] >> 1) & 0x3f == 33:
      break
    i += 3
  end = header.find(b"\x00\x00\x01", i + 3)
  rbsp = header[i + 5:end if end > 0 else len(header)].replace(b"\x00\x00\x03", b"\x00\x00")
  bits, n, pos = int.from_bytes(rbsp, "big"), len(rbsp) * 8, [0]

  def u(k):
    pos[0] += k
    return (bits >> (n - pos[0])) & ((1 << k) - 1)

  def ue():
    zeros = 0
    while u(1) == 0:
      zeros += 1
    return (1 << zeros) - 1 + u(zeros)

  u(4)
  max_sub_layers = u(3)
  u(1)
  u(96)  # general profile, tier and level
  present = [(u(1), u(1)) for _ in range(max_sub_layers)]
  if max_sub_layers:
    u(2 * (8 - max_sub_layers))
  for profile, level in present:
    u(88 * profile + 8 * level)
  ue()
  chroma = ue()
  if chroma == 3:
    u(1)
  w, h = ue(), ue()
  if u(1):
    left, right, top, bottom = ue(), ue(), ue(), ue()
    sub_w, sub_h = {1: (2, 2), 2: (2, 1)}.get(chroma, (1, 1))
    w, h = w - sub_w * (left + right), h - sub_h * (top + bottom)
  return w, h


def probe_size(path: str, header: bytes) -> tuple[int, int]:
  size = hevc_size(header) if header else None
  if size is None:
    from openpilot.tools.lib.framereader import ffprobe
    st = ffprobe(path)["streams"][0]
    size = st["width"], st["height"]
  return size


def start_decode(tag: str, scans: dict, ranges: list[tuple[int, int]], hevc: bool, mode: str) -> dict:
  """Starts decoding the route camera frames the workers need into /dev/shm. mode "gpu": one NVDEC ffmpeg
  (parallel NVDEC sessions serialize) over the GOPs in the order the workers consume them, "cpu": one single
  threaded ffmpeg per GOP, all in parallel. Returns where each frame lands."""
  first, last = min(r[0] for r in ranges), max(r[1] for r in ranges)
  gops = []  # (segment, first route frame, frame count, byte range)
  for seg in sorted(scans):
    raw, _, keys = scans[seg]
    bounds = keys + [(SEGMENT_FRAMES, len(raw))]
    for (k, off), (k_next, off_next) in zip(bounds[:-1], bounds[1:], strict=True):
      lo, hi = seg * SEGMENT_FRAMES + k, seg * SEGMENT_FRAMES + k_next
      if lo < last and hi > first and any(a < hi and b > lo for a, b in ranges):
        gops.append((seg, lo, hi - lo, off, off_next))
  if mode == "gpu":
    # GOPs decode independently: every worker's first GOP, then every worker's second, ...
    per_range = [[g for g in gops if g[1] < b and g[1] + g[2] > a] for a, b in ranges]
    order, seen = [], set()
    for step in range(max(map(len, per_range), default=0)):
      for gs in per_range:
        if step < len(gs) and gs[step][1] not in seen:
          seen.add(gs[step][1])
          order.append(gs[step])
    groups = [order]
  else:
    groups = [[g] for g in gops]

  files, index, pids = [], {}, []
  for n, group in enumerate(groups):
    src, dst = f"/dev/shm/{tag}_{n}.hevc", f"/dev/shm/{tag}_{n}.raw"
    i, prev = 0, None
    with open(src, "wb") as f:
      for seg, lo, count, off, off_next in group:
        raw, header, _ = scans[seg]
        if seg != prev:  # parameter sets again whenever the segment (and so maybe the encoder) changes
          f.write(header)
          prev = seg
        f.write(memoryview(raw)[off:off_next])
        for frame in range(lo, lo + count):
          index[frame] = (dst, i)
          i += 1
    open(dst, "wb").close()
    hw = ["-hwaccel", "cuda"] if mode == "gpu" else ["-threads", "1"]
    in_args = [*hw, "-c:v", "hevc", "-f", "hevc"] if hevc else []
    # passthrough timing: GOPs out of order have jumping timestamps, frames must not be dropped or duplicated
    pids.append(subprocess.Popen(["ffmpeg", "-v", "quiet", *in_args, "-i", src, "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "nv12",
                                  "-y", dst],
                                 stdin=subprocess.DEVNULL, start_new_session=True).pid)
    files += [src, dst]
  return {"files": files, "index": index, "pids": pids}


class CameraReader:
  """Worker side of the decode: waits for a frame to land in its file and reads it."""
  def __init__(self, decode: dict):
    self.index, self.frame_size = decode["index"], decode["frame_size"]
    self.fds: dict[str, int] = {}

  def get(self, frame: int) -> bytes | None:
    if frame not in self.index:
      return None  # not decoded, the view can't be showing this camera now
    path, n = self.index[frame]
    if path not in self.fds:
      self.fds[path] = os.open(path, os.O_RDONLY)
    fd, deadline = self.fds[path], time.perf_counter() + 30
    while os.fstat(fd).st_size < (n + 1) * self.frame_size:
      if time.perf_counter() > deadline:
        raise RuntimeError(f"camera frame {frame} was never decoded")
      time.sleep(0.0005)
    return os.pread(fd, self.frame_size, n * self.frame_size)


# ---- helper processes: route, logs and camera, forked first ------------------------------------

def run_helper(fn, out: str, *args) -> int:
  """Forks fn(*args), which returns a JSON-able result written to out (renamed into place when complete)."""
  pid = os.fork()
  if pid == 0:
    try:
      result = fn(*args)
      with open(out + ".tmp", "w") as f:
        json.dump(result, f)
      os.rename(out + ".tmp", out)
      os._exit(0)
    except BaseException:
      import traceback
      traceback.print_exc()
      os._exit(1)
  return pid


def wait_json(path: str, timeout: float = 600):
  deadline = time.perf_counter() + timeout
  while not os.path.exists(path):
    if time.perf_counter() > deadline:
      raise RuntimeError(f"{path} never appeared")
    time.sleep(0.002)
  with open(path) as f:
    return json.load(f)


ROUTE_LISTING_TTL = 3600  # seconds, the listing's signed URLs last longer and the download cache ignores the signature


class RouteFiles:
  """The file lists of a route. Remote listings (an API call, most of a cold render otherwise) are kept
  next to the download cache, files already downloaded stay readable after their URLs expire."""
  KINDS = ("log_paths", "camera_paths", "ecamera_paths", "qcamera_paths")

  def __init__(self, files: dict):
    self.files = files

  def __getattr__(self, kind):
    if kind in RouteFiles.KINDS:
      return lambda: self.files[kind]
    raise AttributeError(kind)


def route_listing(args) -> str | None:
  if args.data_dir:
    return None
  from openpilot.common.hardware.hw import Paths
  return os.path.join(Paths.download_cache_root(), "fast_clip_routes", args.route.replace("/", "_").replace("|", "_") + ".json")


def get_route(args) -> RouteFiles:
  listing = route_listing(args)
  if listing and os.path.exists(listing) and time.time() - os.path.getmtime(listing) < ROUTE_LISTING_TTL:  # noqa: TID251
    with open(listing) as f:
      return RouteFiles(json.load(f))
  from openpilot.tools.lib.route import Route
  route = Route(args.route, data_dir=args.data_dir)
  files = {kind: getattr(route, kind)() for kind in RouteFiles.KINDS}
  if listing:
    os.makedirs(os.path.dirname(listing), exist_ok=True)
    with open(listing + f".{os.getpid()}", "w") as f:
      json.dump(files, f)
    os.replace(listing + f".{os.getpid()}", listing)
  return RouteFiles(files)


def with_fresh_listing(fn):
  """Runs a helper, once more with a fresh route listing if it fails (an expired URL for a file not cached yet)."""
  def run(args, *rest):
    try:
      return fn(args, *rest)
    except Exception:
      listing = route_listing(args)
      if not listing or not os.path.exists(listing):
        raise
      os.unlink(listing)
      return fn(args, *rest)
  return run


def decode_camera(args, tag: str, paths: list, seg_start: int, seg_end: int, cam_ranges, mode: str, offsets: dict | str) -> dict:
  scans = {seg: Background(scan_camera, paths[seg], not args.qcam) for seg in range(seg_start, seg_end) if paths[seg]}
  scans = {seg: bg.result() for seg, bg in scans.items()}
  if isinstance(offsets, str):  # the log helper finds them while the files download and scan
    offsets = wait_json(offsets, timeout=120)
  cam_ranges = [(camera_frame(a, offsets), camera_frame(b - 1, offsets) + 1) for a, b in cam_ranges]
  w, h = probe_size(paths[seg_start], None if args.qcam else scans[seg_start][1])
  d = start_decode(tag, scans, cam_ranges, not args.qcam, mode)
  d.update(frame_size=w * h * 3 // 2, w=w, h=h, index={str(k): v for k, v in d["index"].items()})
  if args.verbose:
    print(f"helper {tag} decode started @{time.perf_counter() - T_START:.2f}", flush=True)
  return d


def road_camera(args, tag: str, seg_start: int, seg_end: int, cam_ranges, mode: str, offsets_json: str) -> dict:
  route = get_route(args)
  paths = route.qcamera_paths() if args.qcam else route.camera_paths()
  return decode_camera(args, f"{tag}_road", paths, seg_start, seg_end, cam_ranges, mode, offsets_json)


@functools.cache
def migration_table() -> tuple[list[str], dict[str, tuple[list[str], str | None]]]:
  """migrate_all's migrations in order, with their inputs and product, read from the checkout's source so
  modern logs (nothing to migrate) never import it."""
  import re
  for root in sys.path:
    for pkg in ("openpilot", ""):
      fn = os.path.join(root, pkg, "selfdrive/test/process_replay/migration.py")
      if os.path.exists(fn):
        with open(fn) as f:
          src = f.read()
        table = {name: ([i.strip().strip("'\"") for i in inputs.split(",") if i.strip()], product or None)
                 for inputs, product, name in re.findall(r'@migration\(inputs=\[([^\]]*)\](?:,\s*product="(\w+)")?\)\s*\ndef (\w+)', src)}
        body = re.search(r"def migrate_all\(.*?migrations = \[(.*?)\]", src, re.S)
        order = re.findall(r"\w+", body[1]) if body else []
        return [m for m in order if m in table], table
  return [], {}


def migrations_for(discs, names_by_disc: dict) -> list[str]:
  """The migrations an old log needs, none for a modern one."""
  import numpy as np
  present = {names_by_disc.get(d) for d in np.unique(discs).tolist()}
  order, table = migration_table()
  applicable = [m for m in order if not (table[m][1] and table[m][1] in present) and any(i in present for i in table[m][0])]
  old = any(i.endswith("DEPRECATED") for m in applicable for i in table[m][0]) or any(table[m][1] for m in applicable)
  return applicable if old else []


def import_migration():
  import openpilot.selfdrive.test.process_replay.migration  # noqa: F401


def migrate_window(dat: bytes, arrays, names_by_disc: dict, window_mono: tuple[int, int]):
  """Applies the checkout's log migrations (migrate_all) to the messages in the clip window, for old logs whose
  services were renamed or derived since. Returns dat and the framing arrays, unchanged for modern logs."""
  import numpy as np
  offs, sizes, monos, discs = arrays
  applicable = migrations_for(discs, names_by_disc)
  if not applicable:
    return dat, arrays
  table = migration_table()[1]

  from openpilot.cereal import log
  import openpilot.selfdrive.test.process_replay.migration as migration
  disc_of = {v: k for k, v in names_by_disc.items()}
  lo, hi = window_mono
  # stateful lookups (carParams, initData) take every message, everything else only the window
  keep_all = {"carParams", "initData"}

  def parse(i):
    return next(iter(log.Event.read_multiple_bytes(dat[offs[i]:offs[i] + sizes[i]])))
  replaced, added, deleted = {}, [], set()
  for name in applicable:
    inputs = table[name][0]
    idx = np.flatnonzero(np.isin(discs, [disc_of[i] for i in inputs if i in disc_of]))
    idx = [int(i) for i in idx[np.argsort(monos[idx], kind="stable")]
           if lo <= monos[i] <= hi or names_by_disc.get(int(discs[i])) in keep_all]
    r_ops, a_ops, d_ops = getattr(migration, name)([(i, replaced.get(i) or parse(i)) for i in idx])
    replaced.update(r_ops)
    added += a_ops
    deleted.update(d_ops)

  extra, new_rows, pos = [], [], len(dat)
  for msg in [*replaced.values(), *added]:
    b = msg.as_builder().to_bytes()
    new_rows.append((pos, len(b), msg.logMonoTime, disc_of[msg.which()]))
    extra.append(b)
    pos += len(b)
  keep = np.ones(len(offs), dtype=bool)
  keep[list(deleted | set(replaced))] = False
  new = np.array(new_rows, dtype=np.int64).reshape(-1, 4).T
  offs, sizes, monos, discs = (np.concatenate([a[keep], n]) for a, n in zip((offs, sizes, monos, discs), new, strict=True))
  order = np.argsort(monos, kind="stable")
  return dat + b"".join(extra), (offs[order], sizes[order], monos[order], discs[order])


def wide_speeds(args) -> tuple[float, float]:
  """The checkout's speeds for switching to the wide camera in experimental mode and back."""
  import re
  ui = "selfdrive/ui/onroad" if args.big else "selfdrive/ui/mici/onroad"
  for root in sys.path:
    for pkg in ("openpilot", ""):
      fn = os.path.join(root, pkg, ui, "augmented_road_view.py")
      if os.path.exists(fn):
        with open(fn) as f:
          text = f.read()
        wide, road = (re.search(rf"^{name}\s*=\s*([0-9.]+)", text, re.M) for name in ("WIDE_CAM_MAX_SPEED", "ROAD_CAM_MIN_SPEED"))
        if wide and road:
          return float(wide[1]), float(road[1])
  return 5.0, 10.0


def read_logs(args, tag: str, seg_start: int, seg_end: int, cam_ranges, window: tuple[int, int], mode: str, offsets_json: str) -> dict:
  import numpy as np
  from openpilot.cereal import log
  route = get_route(args)
  node = log.Event.schema.node.struct
  disc_offset = node.discriminantOffset * 2
  disc = {f.name: f.discriminantValue for f in node.fields}
  names_by_disc = {f.discriminantValue: f.name for f in node.fields}
  result: dict = {"logs": [], "msgs": [], "cameras": {}}
  want_wide = not args.map and not args.qcam and any(route.ecamera_paths()[seg_start:seg_end])
  segs = []
  for seg, path in enumerate(route.log_paths()[seg_start:seg_end], start=seg_start):
    if path is None:
      raise RuntimeError(f"segment {seg} has no log")
    dat = read_log(path)
    segs.append((seg, dat, *(np.array(a, dtype=np.int64) for a in frame_messages(dat, disc_offset))))
    if len(segs) == 1 and migrations_for(segs[0][5], names_by_disc):
      Background(import_migration)  # an old log, its migrations import while the rest reads

  # the route clock and where each segment's video sits on it
  t_init, offsets = None, {}
  for seg, dat, offs, sizes, monos, discs in segs:
    if t_init is None and (init := np.flatnonzero(discs == disc["initData"])).size:
      t_init = int(monos[init[0]])
  if t_init is None:
    t_init = int(min(monos.min() for *_, monos, _ in segs if monos.size))  # very old logs, no initData
  for seg, dat, offs, sizes, monos, discs in segs:
    enc = next((d for name in ENCODE_IDX if (d := disc.get(name)) is not None and (discs == d).any()), None)
    if enc is not None:
      i = int(np.flatnonzero(discs == enc)[0])
      msg = next(iter(log.Event.read_multiple_bytes(dat[offs[i]:offs[i] + sizes[i]])))
      idx = getattr(msg, msg.which())
      offsets[str(seg)] = int((monos[i] - t_init) // FRAME_NS) - (seg * SEGMENT_FRAMES + idx.segmentId)
    else:  # no encode index, the video starts with the segment's first message
      offsets[str(seg)] = int((monos[discs != disc["initData"]].min() - t_init) // FRAME_NS) - seg * SEGMENT_FRAMES
  with open(offsets_json + ".tmp", "w") as f:
    json.dump(offsets, f)
  os.rename(offsets_json + ".tmp", offsets_json)
  result["t_init"], result["offsets"] = t_init, offsets

  window_mono = (t_init + window[0] * FRAME_NS - 10**9, t_init + window[1] * FRAME_NS + 10**9)
  wide_msgs = []
  for n, (seg, dat, offs, sizes, monos, discs) in enumerate(segs):
    dat, (offs, sizes, monos, discs) = migrate_window(dat, (offs, sizes, monos, discs), names_by_disc, window_mono)
    with open(f"/dev/shm/{tag}_log{n}", "wb") as f:
      f.write(dat)
    np.save(f"/dev/shm/{tag}_msgs{n}.npy", np.stack([offs, sizes, monos, discs]))
    result["logs"].append(f"/dev/shm/{tag}_log{n}")
    result["msgs"].append(f"/dev/shm/{tag}_msgs{n}.npy")
    if want_wide:
      for name in ("selfdriveState", "carState"):
        for i in np.flatnonzero((discs == disc[name]) & (monos < window_mono[1])).tolist():
          wide_msgs.append((int(monos[i]), name, dat, int(offs[i]), int(sizes[i])))
  if args.verbose:
    print(f"helper logs done @{time.perf_counter() - T_START:.2f}", flush=True)

  if want_wide:
    # (experimental mode, vEgo) per frame of the window, for which frames can show the wide camera
    wide_msgs.sort(key=lambda m: m[0])
    times = np.array([m[0] for m in wide_msgs], dtype=np.int64)
    exp, v, j, needed = False, 0.0, 0, []
    _, road_min = wide_speeds(args)
    for f in range(*window):
      end_ns = t_init + (f + 1) * FRAME_NS
      last = {}
      while j < len(times) and times[j] < end_ns:
        last[wide_msgs[j][1]] = wide_msgs[j]
        j += 1
      for name, (_, _, dat, off, size) in last.items():
        msg = getattr(next(iter(log.Event.read_multiple_bytes(dat[off:off + size]))), name)
        exp, v = (msg.experimentalMode, v) if name == "selfdriveState" else (exp, msg.vEgo)
      if exp and v < road_min:
        needed.append(f)
    # the view switches to the wide camera below one speed and back above another, decode a superset
    if needed:
      margin, spans = FPS, []
      for f in needed:
        if spans and f - margin <= spans[-1][1]:
          spans[-1][1] = f + margin + 1
        else:
          spans.append([f - margin, f + margin + 1])
      ranges = [(max(a, s0), min(b, s1)) for a, b in cam_ranges for s0, s1 in spans if max(a, s0) < min(b, s1)]
      result["cameras"]["wide"] = decode_camera(args, f"{tag}_wide", route.ecamera_paths(), seg_start, seg_end, ranges, mode, offsets)
  return result


# ---- map frames -------------------------------------------------------------------------------

def load_map(path: str):
  """Map frame times and frames, the frames memory mapped when the npz stores them uncompressed."""
  import zipfile
  import numpy as np
  with zipfile.ZipFile(path) as z:
    t = np.load(z.open("t.npy"))
    info = z.getinfo("maps.npy")
    if info.compress_type == zipfile.ZIP_STORED:
      with open(path, "rb") as f:
        f.seek(info.header_offset)
        name_len, extra_len = struct.unpack_from("<HH", f.read(30), 26)
        f.seek(info.header_offset + 30 + name_len + extra_len)
        version = np.lib.format.read_magic(f)
        read_header = np.lib.format.read_array_header_1_0 if version == (1, 0) else np.lib.format.read_array_header_2_0
        shape, fortran, dtype = read_header(f)
        return t, np.memmap(path, dtype=dtype, mode="r", offset=f.tell(), shape=shape, order="F" if fortran else "C")
    return t, np.load(z.open("maps.npy"))


# ---- fonts ------------------------------------------------------------------------------------

def rasterize_fonts(app):
  """CPU half of the checkout's GuiApplication._load_fonts: glyphs and atlas images, no GL needed, so it
  runs once in the parent before the fork. Rendered output is bit-identical to the checkout's loader."""
  import pyray as rl
  import raylib
  from importlib.resources import as_file
  base_chars = set(map(chr, range(32, 127))) | set(app.EXTRA_FONT_CHARS)
  unifont_chars = set(base_chars)
  for language, code in app.multilang.languages.items():
    unifont_chars.update(language)
    if code not in app.FONT_FALLBACK_LANGUAGES:
      base_chars.update(app.TRANSLATIONS_DIR.joinpath(f"app_{code}.po").read_text(encoding="utf-8"))
  fonts, keep = {}, []
  for weight in app.FontWeight:
    with as_file(app.FONT_DIR) as fspath:
      data = (fspath / weight).read_bytes()
    unifont = weight == app.FontWeight.UNIFONT
    size = 16 if unifont else 200
    codepoints = sorted(map(ord, unifont_chars if unifont else base_chars))
    cp = rl.ffi.new("int[]", codepoints)
    buf = rl.ffi.from_buffer(data)
    found = rl.ffi.new("int *")
    glyphs = raylib.LoadFontData(rl.ffi.cast("unsigned char *", buf), len(data), size, cp, len(codepoints), raylib.FONT_DEFAULT, found)
    recs = rl.ffi.new("Rectangle **")
    atlas = raylib.GenImageFontAtlas(glyphs, recs, found[0], size, 4, 0)
    fonts[weight] = (size, found[0], glyphs, recs[0], atlas, unifont)
    keep += [cp, buf, recs, found]
  return fonts, keep


def install_fonts(app, gui_app, prepared: Background) -> None:
  """GPU half: the checkout's _load_fonts, from atlases rasterized in a thread while the window opens."""
  import pyray as rl

  def load_fonts():
    fonts, _ = prepared.result()
    for weight, (size, count, glyphs, recs, atlas, unifont) in fonts.items():
      font = rl.Font(size, count, 4, rl.load_texture_from_image(atlas), recs, glyphs)
      if not unifont:
        rl.gen_texture_mipmaps(font.texture)
        rl.set_texture_filter(font.texture, rl.TextureFilter.TEXTURE_FILTER_TRILINEAR)
      gui_app._fonts[weight] = font
    if app.multilang.requires_font_fallback():
      gui_app.fallback_font()
  gui_app._load_fonts = load_fonts


# ---- worker -----------------------------------------------------------------------------------

def worker(wid: int, args, plan: dict, part: str, done_fd: int) -> None:
  timer = Timer()
  first, last = plan["slices"][wid]
  pre = max(first - PREROLL, plan["min_frame"])
  frames = range(pre, last)

  import numpy as np
  import pyray as rl
  from openpilot.system.ui.lib.application import gui_app

  # the rest of the UI imports in a thread while this one opens the window and loads the fonts
  # (raylib releases the GIL). Still under the shared prefix, so ui_state reads the render's params
  def import_ui():
    from openpilot.selfdrive.ui.ui_state import ui_state
    if args.big:
      from openpilot.selfdrive.ui.onroad.augmented_road_view import AugmentedRoadView
    else:
      from openpilot.selfdrive.ui.mici.onroad.augmented_road_view import AugmentedRoadView
    return ui_state, AugmentedRoadView
  ui_bg = Background(import_ui)
  map_bg = Background(load_map, args.map) if args.map else None
  import openpilot.system.ui.lib.application as app
  if all(hasattr(app, a) for a in ("EXTRA_FONT_CHARS", "multilang", "FONT_FALLBACK_LANGUAGES", "TRANSLATIONS_DIR", "FONT_DIR")):
    install_fonts(app, gui_app, Background(rasterize_fonts, app))

  rl.set_config_flags(rl.ConfigFlags.FLAG_WINDOW_HIDDEN)
  gui_app.init_window("clip", fps=FPS)
  target = rl.load_render_texture(gui_app.width, gui_app.height)
  timer.lap("window")
  ui_state, view_cls = ui_bg.result()
  timer.lap("import")

  from msgq.visionipc import VisionIpcServer
  from openpilot.cereal import log
  from openpilot.cereal.visionipc import VisionStreamType
  # everything with a socket from here on is this worker's own
  # unique per render too: compare_ref runs two renders at once and their cameras must not cross
  os.environ["OPENPILOT_PREFIX"] = f"{plan['tag']}_{wid}"
  os.makedirs(f"/dev/shm/msgq_{os.environ['OPENPILOT_PREFIX']}", exist_ok=True)

  prep = wait_json(plan["logs_json"])
  cameras = dict(prep["cameras"])
  if plan["road_json"]:
    cameras["road"] = wait_json(plan["road_json"])
  logs = {}
  for n, (log_path, msgs_path) in enumerate(zip(prep["logs"], prep["msgs"], strict=True)):
    with open(log_path, "rb") as f:
      dat = mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ)
    logs[n] = (dat, np.load(msgs_path))
  offsets = prep["offsets"]

  fields = log.Event.schema.node.struct.fields
  names, disc_names = {f.name: f.discriminantValue for f in fields}, {f.discriminantValue: f.name for f in fields}
  # per frame, the last message of each service the UI subscribes to, as (segment, message index).
  # messages are parsed only when the UI reads them, most services are never read on most frames
  services = np.array([names[s] for s in ui_state.sm.services if s in names])
  cols = []
  for n, (_, (_, _, monos, discs)) in logs.items():
    keep = np.flatnonzero(np.isin(discs, services))
    cols.append((monos[keep], np.full(len(keep), n), keep, discs[keep]))
  monos, segs, idxs, discs = (np.concatenate(c) for c in zip(*cols, strict=True))
  order = np.argsort(monos, kind="stable")
  msg_frames = (monos[order] - prep["t_init"]) // FRAME_NS
  if not len(msg_frames) or msg_frames[-1] < plan["route_frame"] + plan["nframes"] - 1:
    raise RuntimeError(f"the logs in these segments end at {(msg_frames[-1] if len(msg_frames) else 0) / FPS:.0f}s, the clip ends past that")
  wanted = plan["route_frame"] + np.array(frames)
  bounds = zip(np.searchsorted(msg_frames, wanted, "left").tolist(), np.searchsorted(msg_frames, wanted, "right").tolist(), strict=True)
  segs, idxs, discs = segs[order].tolist(), idxs[order].tolist(), discs[order].tolist()
  chunks = []
  for lo, hi in bounds:
    d = {}
    for j in range(lo, hi):
      d[discs[j]] = (segs[j], idxs[j])
    chunks.append({disc_names[disc]: v for disc, v in d.items()})

  class Raw:
    __slots__ = ("seg", "i")

    def __init__(self, seg, i):
      self.seg, self.i = seg, i

    def parse(self, svc):
      dat, (offs, sizes, _, _) = logs[self.seg]
      return getattr(next(iter(log.Event.read_multiple_bytes(dat[offs[self.i]:offs[self.i] + sizes[self.i]]))), svc)

  class LazyData(dict):
    def __getitem__(self, svc):
      v = dict.__getitem__(self, svc)
      if type(v) is Raw:
        v = v.parse(svc)
        dict.__setitem__(self, svc, v)
      return v

    def get(self, svc, default=None):
      return self[svc] if svc in self else default

    def values(self):
      return [self[k] for k in self]

    def items(self):
      return [(k, self[k]) for k in self]

  cams = {}
  for key, stream in (("road", VisionStreamType.VISION_STREAM_NARROW_ROAD), ("wide", VisionStreamType.VISION_STREAM_WIDE_ROAD)):
    if key in cameras:
      decode = cameras[key]
      decode["index"] = {int(f): tuple(v) for f, v in decode["index"].items()}
      cams[stream] = (CameraReader(decode), decode["w"], decode["h"])
  vipc = None
  if cams:
    vipc = VisionIpcServer("camerad")
    for stream, (_, w, h) in cams.items():
      vipc.create_buffers(stream, 4, w, h)
    vipc.start_listener()
  map_server = None
  sent_map = [-1, 0]  # map index and frame last sent
  if map_bg is not None:
    map_t, map_frames = map_bg.result()
    map_server = VisionIpcServer("mapsd")
    map_server.create_buffers_with_sizes(VisionStreamType.VISION_STREAM_MAP, 4, 256, 256, 256 * 256, 256, 256 * 256)
    map_server.start_listener()

  # frame clock: UI timers follow the frame index
  clock = [CLOCK_START + pre / FPS]
  t0 = time.monotonic()
  time.monotonic = lambda: t0 + clock[0]
  rl.get_time = lambda: clock[0]
  ui_state.started_frame = 0
  ui_state.started_time = time.monotonic()

  # no background threads in a render: prime status polls the API and the params thread refreshes on a
  # timer, both only fight the render loop for the GIL. Params are read once, the same for every worker
  if hasattr(ui_state, "prime_state"):
    ui_state.prime_state.start = lambda: None
  if hasattr(ui_state, "_params_thread"):
    ui_state._params_thread = threading.current_thread()
    ui_state.update_params()
  ui_module = sys.modules[type(ui_state).__module__]
  if hasattr(getattr(ui_module, "device", None), "_start_brightness_thread"):
    ui_module.device._start_brightness_thread = lambda: None

  road_view = view_cls()
  rect = rl.Rectangle(0, 0, gui_app.width, gui_app.height)
  road_view.set_rect(rect)

  sm = ui_state.sm
  sm.data = LazyData(sm.data)

  def mock_update(timeout=None):
    t = time.monotonic()
    sm.updated = dict.fromkeys(sm.services, False)
    k = sm.frame - SM_START
    if 0 <= k < len(chunks):
      for svc, (seg, i) in chunks[k].items():
        if svc in sm.data:
          sm.seen[svc] = sm.updated[svc] = sm.alive[svc] = sm.valid[svc] = True
          dict.__setitem__(sm.data, svc, Raw(seg, i))
          sm.logMonoTime[svc], sm.recv_time[svc], sm.recv_frame[svc] = int(logs[seg][1][2][i]), t, sm.frame
    sm.frame += 1
  # ui_state treats sm.frame == 1 as startup and replays the onroad transition, so the count never passes 1
  sm.frame = SM_START
  sm.update = mock_update
  timer.lap("setup")

  preset = "ultrafast" if args.big else "veryfast"  # big is 4.5x the pixels, encoding would dominate
  enc = subprocess.Popen(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{gui_app.width}x{gui_app.height}",
                          "-r", str(FPS * args.speed), "-i", "pipe:0", "-vf", "vflip,format=yuv420p", "-c:v", "libx264", "-preset", preset,
                          "-crf", str(args.crf), "-f", "mpegts", "-y", part], stdin=subprocess.PIPE)
  try:
    import fcntl
    fcntl.fcntl(enc.stdin.fileno(), F_SETPIPE_SZ, 1 << 20)
  except OSError:
    pass
  nbytes, fd = gui_app.width * gui_app.height * 4, enc.stdin.fileno()

  # pre-roll frames only advance the UI's state: its python runs as usual, but raylib's draw calls are
  # skipped (their cost is cffi plus software rasterization). The output is bit-identical either way
  real_draw = {n: getattr(rl, n) for n in dir(rl)
               if n.startswith(("draw_", "begin_", "end_", "clear_background", "update_texture", "rl_", "set_shader_value")) and callable(getattr(rl, n))}

  def skip_draws(skip: bool):
    for n, f in real_draw.items():
      setattr(rl, n, (lambda *a, **k: None) if skip else f)
  skip_preroll = first > pre
  if skip_preroll:
    skip_draws(True)

  map_idx = 0
  for k, frame in enumerate(frames):
    if skip_preroll and frame == first:
      skip_draws(False)
      sent_map[0] = -1  # resend, a map received in the pre-roll never reached the skipped update_texture
    clock[0] = CLOCK_START + frame / FPS
    frame_id = frame - plan["min_frame"]  # pre-roll frames before the clip have negative indices
    for stream, (cam, _, _) in cams.items():
      data = cam.get(camera_frame(plan["route_frame"] + frame, offsets))
      if data is not None:
        vipc.send(stream, data, frame_id, int(frame_id * 5e7), int(frame_id * 5e7))
    if map_server is not None:
      if "modelV2" in chunks[k]:
        seg, i = chunks[k]["modelV2"]
        map_idx = int(np.clip(np.searchsorted(map_t, logs[seg][1][2][i]), 0, len(map_t) - 1))
      # only send a changed map, the UI recolors every frame it receives and maps are 10 Hz to its 20.
      # resend each second so the UI never counts the map as stale. a zero start of frame reads as "no map"
      if map_idx != sent_map[0] or frame - sent_map[1] >= FPS:
        map_server.send(VisionStreamType.VISION_STREAM_MAP, map_frames[map_idx].tobytes(), frame_id, frame_id + 1, frame_id + 1)
        sent_map[:] = [map_idx, frame]
    ui_state.update()
    rl.begin_texture_mode(target)
    rl.clear_background(rl.BLACK)
    road_view.render(rect)
    rl.end_texture_mode()
    if frame >= first:
      image = rl.load_image_from_texture(target.texture)
      view = memoryview(rl.ffi.buffer(image.data, nbytes))
      while view:
        view = view[os.write(fd, view):]
      rl.unload_image(image)
  enc.stdin.close()
  enc.wait()
  # report before exiting, tearing down a process with a GL context takes a while
  os.write(done_fd, b"1" if enc.returncode == 0 else b"0")
  timer.lap("render")
  if args.verbose:
    print(f"worker {wid} frames {first}-{last} (+{first - pre} pre) | {' '.join(timer.laps)}", flush=True)
  os._exit(0 if enc.returncode == 0 else 1)


# ---- main -------------------------------------------------------------------------------------

def cleanup(paths: list[str], pids: list[int]) -> None:
  """Kills leftover decoders and frees the /dev/shm files from a detached child, freeing hundreds of MB takes a while."""
  if os.fork() == 0:
    os.setsid()
    import signal
    for pid in pids:
      try:
        os.killpg(pid, signal.SIGKILL)
      except (ProcessLookupError, PermissionError):
        pass
    import shutil
    for p in paths:
      if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
      elif os.path.exists(p):
        os.unlink(p)
    os._exit(0)


def main() -> None:
  args = parse_args()
  timer = Timer()
  os.environ["SCALE"] = "1"  # skips the throwaway window GuiApplication opens to measure the monitor
  os.environ["OFFSCREEN"] = "1"
  if args.big:
    os.environ["BIG"] = "1"
  if args.overlay:
    import importlib.util
    from importlib.abc import MetaPathFinder

    class OverlayFinder(MetaPathFinder):
      def find_spec(self, name, path, target=None):
        fn = os.path.join(args.overlay, name + ".py")
        return importlib.util.spec_from_file_location(name, fn) if os.path.exists(fn) else None
    sys.meta_path.insert(0, OverlayFinder())

  prefix = os.environ.setdefault("OPENPILOT_PREFIX", "fastclip")
  os.makedirs(f"/dev/shm/msgq_{prefix}", exist_ok=True)
  # a segment's data starts about a second after its 60 s mark on the route clock, so the segment before covers
  # the first moments after a boundary and the pre-roll
  seg_start = max(0, args.start * FPS - PREROLL - SEGMENT_SLACK) // SEGMENT_FRAMES
  seg_end = (args.end * FPS - 1) // SEGMENT_FRAMES + 1
  route = get_route(args)
  logs = route.log_paths()
  seg_end = min(seg_end, len(logs))
  cams = None if args.map else route.qcamera_paths() if args.qcam else route.camera_paths()
  if cams is not None and not args.qcam and not all(cams[max(seg_start, args.start // 60):seg_end]):
    print("no fcamera for this clip, rendering the qcamera", flush=True)  # fcameras only upload on request
    args.qcam, cams = True, route.qcamera_paths()
  # the extra segments before the clip only pre-roll, skip them when their files are missing
  while seg_start < args.start // 60 and (logs[seg_start] is None or (cams is not None and cams[seg_start] is None)):
    seg_start += 1
  nframes = (args.end - args.start) * FPS

  min_frame = -min(args.start * FPS, PREROLL)  # pre-roll, none before the route starts
  per_worker = 66 if args.map else 100  # camera clips decode and draw more per frame, fewer workers contend less
  workers = args.workers or max(1, min((os.cpu_count() or 4) // 3, round(nframes / per_worker)))
  bounds = [round(nframes * i / workers) for i in range(workers + 1)]
  slices = list(zip(bounds[:-1], bounds[1:], strict=True))
  cam_ranges = [(args.start * FPS + max(a - PREROLL, min_frame), args.start * FPS + b) for a, b in slices]

  # helpers first: route, logs and camera decode run in their own processes from the start
  tag = f"fastclip{os.getpid()}"
  mode = args.decode
  if mode == "auto":
    mode = "gpu" if os.path.exists("/dev/nvidiactl") and not args.qcam else "cpu"
  plan = {"tag": tag, "route_frame": args.start * FPS, "min_frame": min_frame, "slices": slices, "nframes": nframes,
          "logs_json": f"/dev/shm/{tag}_logs.json", "road_json": None if args.map else f"/dev/shm/{tag}_road.json"}
  offsets_json = f"/dev/shm/{tag}_offsets.json"
  helpers = []
  if plan["road_json"]:
    helpers.append(run_helper(with_fresh_listing(road_camera), plan["road_json"], args, tag, seg_start, seg_end, cam_ranges, mode, offsets_json))
  helpers.append(run_helper(with_fresh_listing(read_logs), plan["logs_json"], args, tag, seg_start, seg_end, cam_ranges,
                            (args.start * FPS + min_frame, args.start * FPS + nframes), mode, offsets_json))

  # the nav view shows while navigating, with map frames to show; without them it's the camera view
  from openpilot.common.params import Params
  params = Params()
  if args.map:
    params.put_bool("NavigationEnabled", True, block=True)
    params.put("NavDestination", {"latitude": 0.0, "longitude": 0.0}, block=True)
  elif params.get("NavDestination") is not None:
    params.remove("NavDestination")
  # shared by the workers, the rest of the UI imports in each worker while its window opens
  import numpy  # noqa: F401
  import pyray  # noqa: F401
  import openpilot.system.ui.lib.application  # noqa: F401
  timer.lap("import")

  out_dir = os.path.dirname(os.path.abspath(args.output))
  parts = [os.path.join(out_dir, f".part{os.getpid()}_{i}.ts") for i in range(workers)]
  sys.stdout.flush()
  sys.stderr.flush()
  done_r, worker_pids = [], []
  for wid in range(workers):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
      os.close(r)
      try:
        worker(wid, args, plan, parts[wid], w)
      except BaseException:
        import traceback
        traceback.print_exc()
        os._exit(1)
    os.close(w)
    done_r.append(r)
    worker_pids.append(pid)

  def watch_helpers():
    # a failed helper never writes its result, so stop the workers waiting on it
    for pid in helpers:
      if os.waitpid(pid, 0)[1] != 0:
        import signal
        for wpid in worker_pids:
          os.kill(wpid, signal.SIGKILL)
  threading.Thread(target=watch_helpers, daemon=True).start()

  # a worker is done when it reports, or when it dies without reporting (EOF). Their exit (GL teardown)
  # runs while the parts are joined
  failed = [wid for wid, r in enumerate(done_r) if os.read(r, 1) != b"1"]
  timer.lap("render")

  owned = [os.path.join("/dev/shm", f) for f in os.listdir("/dev/shm") if f.startswith((tag, f"msgq_{tag}_"))]
  owned += [os.path.expanduser(f"~/.comma{tag}_{wid}") for wid in range(workers)]  # each worker's Params home
  decoder_pids = []
  for j in (plan["logs_json"], plan["road_json"]):
    if j and os.path.exists(j):
      with open(j) as f:
        result = json.load(f)
      for cam in [result, *result.get("cameras", {}).values()]:
        decoder_pids += cam.get("pids", [])
  try:
    if failed:
      sys.exit(f"{len(failed)} of {workers} render workers failed")
    listing = os.path.join(out_dir, f".parts{os.getpid()}.txt")
    with open(listing, "w") as f:
      f.writelines(f"file '{p}'\n" for p in parts)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i", listing, "-c", "copy", "-movflags", "+faststart", "-y", args.output],
                   check=True)
    os.unlink(listing)
    timer.lap("join")
    print(f"rendered {nframes} frames with {workers} workers in {time.perf_counter() - T_START:.2f}s | {' '.join(timer.laps)}", flush=True)
  finally:
    cleanup(owned + [p for p in parts if os.path.exists(p)], decoder_pids)
  os._exit(0)  # skip interpreter teardown


if __name__ == "__main__":
  main()
