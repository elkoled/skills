# run with the checkout's python: mapsd_runner.py [TILES_DIR]
# the checkout's mapsd, live on the session's msgq, so a replayed navigating drive shows the map.
# TILES_DIR serves tiles from a cache copied off a device, a PC has no device key for the tile API
import sys
from pathlib import Path

from map_frames_runner import offline_tiles


def main() -> None:
  try:
    from openpilot.selfdrive.navigation import mapsd as mapsd_mod
  except ImportError:
    raise SystemExit("this checkout has no openpilot.selfdrive.navigation (mapsd), check out a branch with navigation") from None
  if len(sys.argv) > 1 and sys.argv[1]:
    offline_tiles(mapsd_mod, Path(sys.argv[1]).expanduser())
  mapsd_mod.main()


if __name__ == "__main__":
  main()
