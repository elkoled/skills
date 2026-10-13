# run with the checkout's python: map_frames_runner.py ROUTE DATA_DIR START END OUT [TILES_DIR]
# replays the route's GPS and navRoute through the checkout's own mapsd and saves the map frames
# render_clip's map_frames takes: t (modelV2 logMonoTime) and maps ((n, 256, 256) uint8), uncompressed
# so fast_clip can memory map them. TILES_DIR serves tiles from a cache copied off a device
import sys
import time
from pathlib import Path

import numpy as np

LEAD_IN = 15  # seconds before START to replay, navd resends navRoute every 10s
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
    # latest_gps_source wants a fix newer than 2s of wall time, logs replay faster than that
    self.recv_time[w] = time.monotonic()
    self.logMonoTime[w] = msg.logMonoTime
    if w != "navRoute":
      self.valid[w] = msg.valid


def main() -> None:
  route_name, data_dir, start, end, out = sys.argv[1], sys.argv[2] or None, int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
  tiles_dir = Path(sys.argv[6]).expanduser() if len(sys.argv) > 6 and sys.argv[6] else None

  try:
    from openpilot.selfdrive.navigation import mapsd as mapsd_mod
  except ImportError:
    raise SystemExit("this checkout has no openpilot.selfdrive.navigation (mapsd), check out a branch with navigation") from None
  from openpilot.tools.lib.logreader import LogReader
  from openpilot.tools.lib.route import Route

  if tiles_dir is not None:
    offline_tiles(mapsd_mod, tiles_dir)

  route = Route(route_name, data_dir=data_dir)
  logs = route.log_paths()
  first, last = max(0, (start - LEAD_IN) // 60), min(len(logs) - 1, end // 60)
  paths = [p for p in logs[first:last + 1] if p]
  if not paths:
    raise SystemExit(f"no rlogs for segments {first}-{last} of {route_name}")

  sm, maps_d = LogSubMaster(), mapsd_mod.MapsD()
  t0, ts, frames, n, seen_route = None, [], [], 0, False
  for msg in LogReader(paths, sort_by_time=True):
    w = msg.which()
    if t0 is None and w == "modelV2":  # some early messages carry no real time
      t0 = msg.logMonoTime - first * 60 * 1_000_000_000
    if w in ("gpsLocation", "gpsLocationExternal", "navRoute"):
      sm.feed(msg)
      seen_route |= w == "navRoute"
    elif w == "modelV2":
      n += 1
      if n % RATE:
        continue
      frame, _ = maps_d.update(sm)
      sm.updated = dict.fromkeys(sm.updated, False)
      # route seconds from the segment start, a little wider since segments start a bit before modelV2
      if start - 2 <= (msg.logMonoTime - t0) / 1e9 <= end + 2:
        ts.append(msg.logMonoTime)
        frames.append(frame)
  maps_d.close()

  if not seen_route:
    raise SystemExit("the log has no navRoute, the drive was not navigating so there is no route to draw")
  if not frames:
    raise SystemExit(f"no modelV2 frames between {start}s and {end}s")
  maps = np.stack(frames)
  Path(out).parent.mkdir(parents=True, exist_ok=True)
  np.savez(out, t=np.array(ts, dtype=np.uint64), maps=maps)
  print(f"frames={len(ts)} with_map={int(sum(m.any() for m in maps))}")


if __name__ == "__main__":
  main()
