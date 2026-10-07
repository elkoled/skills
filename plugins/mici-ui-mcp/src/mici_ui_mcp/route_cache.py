# run with the checkout's python: route_cache.py ROUTE OUT_DIR FIRST_SEG COUNT [ecam] [dcam]
# copies segments into the local layout replay's -d expects (OUT_DIR/<route>--<seg>/rlog.zst,
# fcamera.hevc, ...), so later replays skip the route API lookup and the python downloader
import os
import sys

from openpilot.tools.lib.filereader import FileReader
from openpilot.tools.lib.route import Route


def main() -> None:
  route_name, out_dir, first, count = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
  extra = set(sys.argv[5:])
  route = Route(route_name)
  # fall back to the qlog where a segment has no rlog uploaded
  logs = [r or q for r, q in zip(route.log_paths(), route.qlog_paths(), strict=False)]
  sources = {"log": logs, "fcamera.hevc": route.camera_paths()}
  if "ecam" in extra:
    sources["ecamera.hevc"] = route.ecamera_paths()
  if "dcam" in extra:
    sources["dcamera.hevc"] = route.dcamera_paths()

  for seg in range(first, min(first + count, len(sources["log"]))):
    seg_dir = os.path.join(out_dir, f"{route.name.time_str}--{seg}")
    os.makedirs(seg_dir, exist_ok=True)
    for name, paths in sources.items():
      src = paths[seg] if seg < len(paths) else None
      if not src:
        continue
      if name == "log":  # keep the log's own compression suffix
        name = ("rlog" if "rlog" in src else "qlog") + next((ext for ext in (".zst", ".bz2") if src.split("?")[0].endswith(ext)), "")
      dst = os.path.join(seg_dir, name)
      if os.path.exists(dst):
        continue
      with FileReader(src) as f:
        data = f.read()
      with open(dst + ".tmp", "wb") as f:
        f.write(data)
      os.replace(dst + ".tmp", dst)
    print(seg_dir)


if __name__ == "__main__":
  main()
