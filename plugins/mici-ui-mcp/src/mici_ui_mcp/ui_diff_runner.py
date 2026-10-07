# run with the checkout's python: ui_diff_runner.py OVERLAY_DIR VARIANT OUTPUT_MP4
# records openpilot's scripted UI tour (selfdrive/ui/tests/diff/replay.py: home, settings panels,
# onroad, alerts, with a frame-based clock) losslessly. OVERLAY_DIR (may be empty) swaps in a git
# ref's UI modules, the tour script itself always comes from the working tree
import os
import sys

from clip_runner import OverlayFinder


def main() -> None:
  overlay_dir, variant, output = sys.argv[1], sys.argv[2], sys.argv[3]
  if overlay_dir:
    sys.meta_path.insert(0, OverlayFinder(overlay_dir))
  if variant == "tizi":
    os.environ["BIG"] = "1"
  os.environ["RECORD"] = "1"
  os.environ["RECORD_QUALITY"] = "0"  # lossless, so unchanged frames hash identically
  os.environ["RECORD_OUTPUT"] = output

  from openpilot.common.prefix import OpenpilotPrefix
  with OpenpilotPrefix():
    from openpilot.selfdrive.ui.tests.diff.replay import run_replay
    run_replay(variant)


if __name__ == "__main__":
  main()
