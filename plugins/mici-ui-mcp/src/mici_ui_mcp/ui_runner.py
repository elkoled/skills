# run with the checkout's python: ui_runner.py UI_SCRIPT
# launches selfdrive/ui/ui.py. With OP_UI_MCP_INSTANT_ONROAD=1 the mici UI goes onroad as soon as
# replay starts, instead of holding offroad for ONROAD_DELAY and then smooth scrolling over
import os
import runpy
import sys


def instant_onroad() -> None:
  try:
    from openpilot.selfdrive.ui.mici.layouts import main as mici_main
  except ImportError:
    return
  mici_main.ONROAD_DELAY = 0
  layout = getattr(mici_main, "MiciMainLayout", None)
  if layout is None or not hasattr(layout, "_scroll_to"):
    return
  orig_scroll_to = layout._scroll_to

  # only the jump to onroad is made instant, other scrolls keep their animation
  def _scroll_to(self, target):
    if target is getattr(self, "_onroad_layout", None):
      self._scroller.scroll_to(int(target.rect.x), smooth=False)
    else:
      orig_scroll_to(self, target)
  layout._scroll_to = _scroll_to


def main() -> None:
  script = sys.argv[1]
  if os.getenv("OP_UI_MCP_INSTANT_ONROAD") == "1":
    instant_onroad()
  sys.argv = sys.argv[1:]
  runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
  main()
