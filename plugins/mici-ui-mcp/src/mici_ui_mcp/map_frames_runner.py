# run with the checkout's python: map_frames_runner.py ROUTE DATA_DIR START END OUT [TILES_DIR]
# replays the route's GPS and navRoute through the checkout's own mapsd and saves the map frames
# render_clip's map_frames takes: t (modelV2 logMonoTime) and maps ((n, 256, 256) uint8), uncompressed
# so fast_clip can memory map them. TILES_DIR serves tiles from a cache copied off a device
import sys
import types
from pathlib import Path

import numpy as np

RATE = 2  # every 2nd modelV2 frame, 10 Hz is plenty for a map that moves at driving speed


def offline_tiles(mapsd_mod, tiles_dir: Path):
  import requests
  from openpilot.common.api import Api

  version = tiles_dir / "version"
  if not version.exists():
    raise SystemExit(f"no tile cache at {tiles_dir} (needs the 'version' file next to the cached ranges)")

  class CachedTiles(mapsd_mod.RemoteTiles):
    def __init__(self):
      self.cache = tiles_dir
      self.session = requests.Session()
      self.api = Api(None)
      self.url, self.expires = "", 0
      etag, size = version.read_text().rsplit(" ", 1)
      self.etag, self.size = etag, int(size)

    def _fetch(self, offset: int, length: int) -> bytes:
      raise OSError(f"tile range {offset}-{length} is not in the cache at {tiles_dir}")

  mapsd_mod.RemoteTiles = CachedTiles


class LogSubMaster:
  """The parts of SubMaster that MapsD.update reads, fed from log messages."""

  def __init__(self):
    self.msgs, self.recv_time, self.logMonoTime = {}, {}, {}
    self.updated = {"navRoute": False, "gpsLocation": False, "gpsLocationExternal": False}
    self.valid = {"gpsLocation": False, "gpsLocationExternal": False}

  def __getitem__(self, k):
    return self.msgs[k]

  def feed(self, msg) -> None:
    w = msg.which()
    self.msgs[w] = getattr(msg, w)
    self.updated[w] = True
    self.recv_time[w] = msg.logMonoTime / 1e9
    self.logMonoTime[w] = msg.logMonoTime
    if w != "navRoute":
      self.valid[w] = msg.valid


def log_messages(path: str, services: tuple[str, ...]):
  """The messages of these services in time order, parsing only those of a segment's ~85k."""
  import numpy as np
  from openpilot.cereal import log
  from fast_clip import frame_messages, read_log
  node = log.Event.schema.node.struct
  wanted = [f.discriminantValue for f in node.fields if f.name in services]
  dat = read_log(path)
  offs, sizes, monos, discs = (np.array(a, dtype=np.int64) for a in frame_messages(dat, node.discriminantOffset * 2))
  sel = np.flatnonzero(np.isin(discs, wanted))
  for i in sel[np.argsort(monos[sel], kind="stable")].tolist():
    yield next(iter(log.Event.read_multiple_bytes(dat[offs[i]:offs[i] + sizes[i]])))


def render_segment(job):
  """Map frames for one segment, its own MapsD, so segments render in parallel. The previous segment's
  log only supplies the route and a GPS fix (navd resends navRoute every 10s), nothing is drawn for it."""
  import signal
  signal.signal(signal.SIGINT, signal.SIG_IGN)
  seg, lead_path, path, lo, hi = job
  sm, maps_d = LogSubMaster(), MAPSD.MapsD()
  feeds = ("gpsLocation", "gpsLocationExternal", "navRoute")
  if lead_path:
    for msg in log_messages(lead_path, feeds):
      sm.feed(msg)
  t0, ts, frames, n, seen_route = None, [], [], 0, "navRoute" in sm.msgs
  for msg in log_messages(path, (*feeds, "initData", "modelV2")):
    w = msg.which()
    if w == "initData" and t0 is None:  # route seconds count from initData, the same clock as logreader and fast_clip
      t0 = msg.logMonoTime
    elif w in ("gpsLocation", "gpsLocationExternal", "navRoute"):
      sm.feed(msg)
      seen_route |= w == "navRoute"
    elif w == "modelV2":
      if t0 is None:  # very old logs without initData
        t0 = msg.logMonoTime - seg * 60 * 1_000_000_000
      n += 1
      # a little wider than asked, for the render's pre-roll
      if n % RATE or not lo - 5 <= (msg.logMonoTime - t0) / 1e9 <= hi + 2:
        continue
      LOG_CLOCK[0] = msg.logMonoTime / 1e9  # mapsd renders at the frame's time, like onroad
      frame, _ = maps_d.update(sm)
      sm.updated = dict.fromkeys(sm.updated, False)
      ts.append(msg.logMonoTime)
      frames.append(frame)
  maps_d.close()
  return seen_route, ts, frames


MAPSD = None
LOG_CLOCK = [0.0]


def main() -> None:
  global MAPSD
  import multiprocessing
  import os
  route_name, data_dir, start, end, out = sys.argv[1], sys.argv[2] or None, int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
  tiles_dir = Path(sys.argv[6]).expanduser() if len(sys.argv) > 6 and sys.argv[6] else None

  try:
    from openpilot.selfdrive.navigation import mapsd as mapsd_mod
  except ImportError:
    raise SystemExit("this checkout has no openpilot.selfdrive.navigation (mapsd), check out a branch with navigation") from None
  from argparse import Namespace
  from fast_clip import get_route  # its route listing cache, the API call is most of a cold build otherwise

  if tiles_dir is not None:
    offline_tiles(mapsd_mod, tiles_dir)
  MAPSD = mapsd_mod
  # mapsd and its GPS freshness check read the log's clock, so a frame doesn't depend on how fast this runs
  clock = types.SimpleNamespace(monotonic=lambda: LOG_CLOCK[0])
  mapsd_mod.time = clock
  sys.modules[mapsd_mod.latest_gps_source.__module__].time = clock

  logs = get_route(Namespace(route=route_name, data_dir=data_dir)).log_paths()
  jobs = [(seg, logs[seg - 1] if seg > 0 else None, logs[seg], start, end)
          for seg in range(max(0, start - 7) // 60, min(len(logs) - 1, end // 60) + 1) if logs[seg]]
  if not jobs:
    raise SystemExit(f"no rlogs for {start}-{end}s of {route_name}")

  with multiprocessing.get_context("fork").Pool(min(len(jobs), os.cpu_count() or 1)) as pool:
    results = pool.map(render_segment, jobs, chunksize=1)
  ts = [t for _, seg_ts, _ in results for t in seg_ts]
  frames = [f for _, _, seg_frames in results for f in seg_frames]

  if not any(seen for seen, _, _ in results):
    raise SystemExit("the log has no navRoute, the drive was not navigating so there is no route to draw")
  if not frames:
    raise SystemExit(f"no modelV2 frames between {start}s and {end}s")
  maps = np.stack(frames)
  Path(out).parent.mkdir(parents=True, exist_ok=True)
  np.savez(out, t=np.array(ts, dtype=np.uint64), maps=maps)
  print(f"frames={len(ts)} with_map={int(sum(m.any() for m in maps))} segments={len(jobs)}")


if __name__ == "__main__":
  main()
