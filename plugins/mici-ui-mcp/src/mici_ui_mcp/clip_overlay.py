# run with the checkout's python: clip_overlay.py OVERLAY_DIR CLIP_SCRIPT [clip args...]
# imports a module from OVERLAY_DIR/<dotted.module.name>.py when present, so tools/clip/run.py
# renders another git ref's UI code. stdlib only, it runs in the openpilot venv
import importlib.abc
import importlib.util
import os
import runpy
import sys


class OverlayFinder(importlib.abc.MetaPathFinder):
  def __init__(self, overlay_dir: str):
    self.overlay_dir = overlay_dir

  def find_spec(self, name, path, target=None):
    fn = os.path.join(self.overlay_dir, name + ".py")
    if os.path.exists(fn):
      return importlib.util.spec_from_file_location(name, fn)
    return None


def main() -> None:
  overlay_dir, script = sys.argv[1], sys.argv[2]
  sys.meta_path.insert(0, OverlayFinder(overlay_dir))
  sys.argv = sys.argv[2:]
  runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
  main()
