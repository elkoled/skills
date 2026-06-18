"""MCP server that drives the openpilot UI locally for fast build/validate loops.

Non-obvious mechanics worth knowing:
  - The UI runs on a private Xvfb display sized to the UI with no window manager, so the
    GLFW window fills the screen at 0,0, and SCALE=1.0 makes window pixels map 1:1 to UI
    coordinates (the coords you pass match what you see in a screenshot).
  - Capture is XGetImage on the root window via python-xlib, so there is no ffmpeg/scrot
    dependency.
  - Touch is XTEST pointer events, which raylib/GLFW reads as mouse and the UI treats as
    touch. Swipes step the motion so the UI classifies them as scrolls, not teleports.

Coordinates are the upright capture frame: small UI 536x240, big (tici) UI 2160x1080,
origin top-left. The UI process persists across tool calls, so only the first start_ui
pays startup cost.
"""
from __future__ import annotations

import io
import os
import signal
import subprocess
import time
from pathlib import Path

from PIL import Image as PILImage
from Xlib import X, display
from Xlib.ext import xtest

from mcp.server.fastmcp import FastMCP, Image

SIZES = {
  "small": (536, 240),
  "big": (2160, 1080),
}

UI_READY_TIMEOUT = 40.0
UI_SETTLE = 0.4
SWIPE_STEPS = 24


def _find_openpilot_root() -> Path:
  env = os.getenv("OPENPILOT_ROOT")
  if env:
    return Path(env).expanduser().resolve()
  here = Path(__file__).resolve()
  for p in [here, *here.parents]:
    if (p / "SConstruct").exists() and (p / "selfdrive" / "ui" / "ui.py").exists():
      return p
  return Path.home() / "openpilot"


OPENPILOT_ROOT = _find_openpilot_root()


class UISessionError(RuntimeError):
  pass


class UISession:
  def __init__(self, root: Path):
    self.root = root
    self.mode = "small"
    self.width = 0
    self.height = 0
    self.display_num: int | None = None
    self.display_name = ""
    self._xvfb: subprocess.Popen | None = None
    self._ui: subprocess.Popen | None = None
    self._replay: subprocess.Popen | None = None
    self._disp: display.Display | None = None
    self._root_win = None
    self.ui_log = ""
    self.replay_log = ""

  def is_running(self) -> bool:
    return self._ui is not None and self._ui.poll() is None

  def _free_display(self, start: int = 99) -> int:
    for n in range(start, start + 64):
      if not os.path.exists(f"/tmp/.X{n}-lock") and not os.path.exists(f"/tmp/.X11-unix/X{n}"):
        return n
    raise UISessionError("no free X display found in :99..:163")

  def _wait_for_xserver(self, name: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
      if self._xvfb and self._xvfb.poll() is not None:
        raise UISessionError(f"Xvfb exited early (code {self._xvfb.returncode}); "
                             f"see /tmp/op_ui_mcp_xvfb{self.display_num}.log")
      try:
        d = display.Display(name)
        d.close()
        return
      except Exception as e:  # noqa: BLE001
        last = e
        time.sleep(0.2)
    raise UISessionError(f"X server {name} did not come up: {last}")

  def start(self, mode: str = "small", show_touches: bool = True,
            show_fps: bool = False, extra_env: dict | None = None) -> dict:
    if mode not in SIZES:
      raise UISessionError(f"unknown mode {mode!r}; use one of {list(SIZES)}")
    if self.is_running():
      raise UISessionError("UI already running; call stop_ui first or use restart_ui")
    self.stop(quiet=True)

    self.mode = mode
    self.width, self.height = SIZES[mode]
    self.display_num = self._free_display()
    self.display_name = f":{self.display_num}"

    try:
      with open(f"/tmp/op_ui_mcp_xvfb{self.display_num}.log", "w") as xvfb_log:
        self._xvfb = subprocess.Popen(
          ["Xvfb", self.display_name, "-screen", "0", f"{self.width}x{self.height}x24", "-nolisten", "tcp"],
          stdout=xvfb_log, stderr=subprocess.STDOUT,
          start_new_session=True,
        )
      self._wait_for_xserver(self.display_name)

      self._disp = display.Display(self.display_name)
      self._root_win = self._disp.screen().root

      env = dict(os.environ)
      env.update({
        "DISPLAY": self.display_name,
        "SCALE": "1.0",
        "BIG": "1" if mode == "big" else "0",
        "SHOW_TOUCHES": "1" if show_touches else "0",
        "SHOW_FPS": "1" if show_fps else "0",
        "QT_QPA_PLATFORM": "offscreen",
      })
      if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items()})

      self.ui_log = f"/tmp/op_ui_mcp_ui{self.display_num}.log"
      with open(self.ui_log, "w") as ui_log_f:
        self._ui = subprocess.Popen(
          ["uv", "run", "selfdrive/ui/ui.py"],
          cwd=str(self.root), env=env,
          stdout=ui_log_f, stderr=subprocess.STDOUT,
          start_new_session=True,
        )
    except Exception:
      self.stop(quiet=True)
      raise

    ready = self._wait_until_rendered(UI_READY_TIMEOUT)
    status = self.status()
    status["rendered"] = ready
    if not ready:
      status["hint"] = f"UI did not render in {UI_READY_TIMEOUT}s; check logs via the 'logs' tool"
    return status

  def _wait_until_rendered(self, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
      if not self.is_running():
        return False
      try:
        img = self._grab()
        bbox = img.convert("L").point(lambda v: 255 if v > 40 else 0).getbbox()
        if bbox is not None:
          time.sleep(UI_SETTLE)
          return True
      except Exception:  # noqa: BLE001
        pass
      time.sleep(0.4)
    return False

  def stop(self, quiet: bool = False) -> dict:
    self.stop_replay(quiet=True)
    for attr in ("_ui", "_xvfb"):
      proc = getattr(self, attr)
      if proc is not None and proc.poll() is None:
        try:
          os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:  # noqa: BLE001
          try:
            proc.terminate()
          except Exception:  # noqa: BLE001
            pass
        try:
          proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
          try:
            proc.kill()
            proc.wait(timeout=5)
          except Exception:  # noqa: BLE001
            pass
      setattr(self, attr, None)
    if self._disp is not None:
      try:
        self._disp.close()
      except Exception:  # noqa: BLE001
        pass
      self._disp = None
      self._root_win = None
    return {"stopped": True} if not quiet else {}

  def status(self) -> dict:
    return {
      "running": self.is_running(),
      "mode": self.mode,
      "resolution": f"{self.width}x{self.height}" if self.width else None,
      "display": self.display_name or None,
      "openpilot_root": str(self.root),
      "replay_running": self._replay is not None and self._replay.poll() is None,
      "ui_log": self.ui_log or None,
    }

  def _grab(self) -> PILImage.Image:
    if self._disp is None or self._root_win is None:
      raise UISessionError("no display; start the UI first")
    geo = self._root_win.get_geometry()
    raw = self._root_win.get_image(0, 0, geo.width, geo.height, X.ZPixmap, 0xffffffff)
    return PILImage.frombytes("RGB", (geo.width, geo.height), raw.data, "raw", "BGRX")

  def screenshot(self) -> PILImage.Image:
    if not self.is_running():
      raise UISessionError("UI is not running; call start_ui first")
    return self._grab()

  def _clamp(self, x: int, y: int) -> tuple[int, int]:
    return max(0, min(self.width - 1, int(x))), max(0, min(self.height - 1, int(y)))

  def _move(self, x: int, y: int) -> None:
    xtest.fake_input(self._disp, X.MotionNotify, x=x, y=y)
    self._disp.sync()

  def _btn(self, press: bool) -> None:
    xtest.fake_input(self._disp, X.ButtonPress if press else X.ButtonRelease, 1)
    self._disp.sync()

  def tap(self, x: int, y: int, hold: float = 0.08) -> None:
    if not self.is_running():
      raise UISessionError("UI is not running")
    x, y = self._clamp(x, y)
    self._move(x, y)
    time.sleep(0.02)
    self._btn(True)
    time.sleep(max(hold, 0.05))
    self._btn(False)

  def swipe(self, x1: int, y1: int, x2: int, y2: int, dur: float = 0.4) -> None:
    if not self.is_running():
      raise UISessionError("UI is not running")
    x1, y1 = self._clamp(x1, y1)
    x2, y2 = self._clamp(x2, y2)
    self._move(x1, y1)
    time.sleep(0.02)
    self._btn(True)
    for i in range(1, SWIPE_STEPS + 1):
      t = i / SWIPE_STEPS
      self._move(int(x1 + (x2 - x1) * t), int(y1 + (y2 - y1) * t))
      time.sleep(dur / SWIPE_STEPS)
    self._btn(False)

  def hold(self, x: int, y: int, dur: float = 0.8) -> None:
    self.tap(x, y, hold=dur)

  def run_chain(self, script: str) -> tuple[list[str], list[tuple[str, PILImage.Image]]]:
    import re
    import shlex
    log: list[str] = []
    shots: list[tuple[str, PILImage.Image]] = []
    steps = []
    for raw in re.split(r"[;\n]", script):
      line = raw.split("#", 1)[0].strip()
      if line:
        steps.append(shlex.split(line))
    for i, parts in enumerate(steps):
      op, args = parts[0], parts[1:]
      if op == "tap":
        x, y = int(args[0]), int(args[1])
        h = float(args[2]) if len(args) > 2 else 0.08
        self.tap(x, y, h)
        log.append(f"[{i}] tap {x} {y} hold={h}")
      elif op == "swipe":
        a = [int(v) for v in args[:4]]
        dur = float(args[4]) if len(args) > 4 else 0.4
        self.swipe(*a, dur=dur)
        log.append(f"[{i}] swipe {a} dur={dur}")
      elif op == "hold":
        x, y = int(args[0]), int(args[1])
        dur = float(args[2]) if len(args) > 2 else 0.8
        self.hold(x, y, dur)
        log.append(f"[{i}] hold {x} {y} dur={dur}")
      elif op == "wait":
        time.sleep(float(args[0]))
        log.append(f"[{i}] wait {args[0]}")
      elif op == "capture":
        name = args[0] if args else str(i)
        shots.append((name, self._grab()))
        log.append(f"[{i}] capture -> {name}")
      else:
        raise UISessionError(f"[{i}] unknown step: {op!r}")
    return log, shots

  def set_param(self, name: str, value: str) -> str:
    code = (
      "from openpilot.common.params import Params;"
      f"Params().put({name!r}, {value!r});"
      f"print('set', {name!r})"
    )
    res = subprocess.run(["uv", "run", "python3", "-c", code], cwd=str(self.root),
                         capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
      raise UISessionError(f"set_param failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()

  def start_replay(self, route: str = "", extra_args: list[str] | None = None) -> dict:
    if not self.is_running():
      raise UISessionError("start the UI before replay so they share the same msgq")
    self.stop_replay(quiet=True)
    args = ["tools/replay/replay"]
    if route:
      args.append(route)
    else:
      args.append("--demo")
    if extra_args:
      args += list(extra_args)
    self.replay_log = f"/tmp/op_ui_mcp_replay{self.display_num}.log"
    env = dict(os.environ)
    env["DISPLAY"] = self.display_name
    with open(self.replay_log, "w") as log_f:
      self._replay = subprocess.Popen(args, cwd=str(self.root), env=env,
                                      stdout=log_f, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL, start_new_session=True)
    time.sleep(1.0)
    alive = self._replay.poll() is None
    return {"replay_started": alive, "args": args, "replay_log": self.replay_log}

  def stop_replay(self, quiet: bool = False) -> dict:
    if self._replay is not None and self._replay.poll() is None:
      try:
        os.killpg(os.getpgid(self._replay.pid), signal.SIGTERM)
        self._replay.wait(timeout=5)
      except Exception:  # noqa: BLE001
        try:
          self._replay.kill()
          self._replay.wait(timeout=5)
        except Exception:  # noqa: BLE001
          pass
    self._replay = None
    return {} if quiet else {"replay_stopped": True}

  def logs(self, lines: int = 40) -> str:
    if not self.ui_log or not os.path.exists(self.ui_log):
      return "(no UI log yet)"
    with open(self.ui_log) as f:
      return "".join(f.readlines()[-lines:])


SESSION = UISession(OPENPILOT_ROOT)


def _img(pil: PILImage.Image) -> Image:
  buf = io.BytesIO()
  pil.save(buf, format="PNG")
  return Image(data=buf.getvalue(), format="png")


mcp = FastMCP(
  "mici-ui-mcp",
  instructions=(
    "Drive the openpilot UI locally to build and validate UI changes without a device.\n"
    "Typical loop: start_ui -> screenshot (find the target in the image) -> "
    "tap/swipe/hold at those coordinates -> the tool returns a fresh screenshot to confirm.\n"
    "Coordinates are the pixels you see in the screenshot: small UI is 536x240, big UI is "
    "2160x1080, origin top-left. For multi-step flows prefer the 'run' tool (one chain).\n"
    "After editing UI code, call restart_ui to reload it. Use start_replay to feed a route "
    "so the onroad UI shows real driving data."
  ),
)


@mcp.tool()
def start_ui(mode: str = "small", show_touches: bool = True, show_fps: bool = False) -> list:
  """Launch the openpilot UI on a private headless display and return a screenshot.

  mode: 'small' (536x240, comma four/mici layout) or 'big' (2160x1080, tici layout).
  show_touches: draw a red dot + trail at injected touches (great for confirming taps).
  Idempotent-ish: errors if a UI is already running (use restart_ui to reload code).
  """
  status = SESSION.start(mode=mode, show_touches=show_touches, show_fps=show_fps)
  out: list = [f"started: {status}"]
  if SESSION.is_running():
    out.append(_img(SESSION.screenshot()))
  return out


@mcp.tool()
def restart_ui(mode: str | None = None, show_touches: bool = True, show_fps: bool = False) -> list:
  """Stop and relaunch the UI to pick up code changes. Keeps the same mode unless given."""
  m = mode or SESSION.mode
  SESSION.stop(quiet=True)
  status = SESSION.start(mode=m, show_touches=show_touches, show_fps=show_fps)
  out: list = [f"restarted: {status}"]
  if SESSION.is_running():
    out.append(_img(SESSION.screenshot()))
  return out


@mcp.tool()
def stop_ui() -> str:
  """Stop the UI, replay and the private display, freeing all resources."""
  return str(SESSION.stop())


@mcp.tool()
def status() -> str:
  """Report whether the UI is running, its mode/resolution, display and log path."""
  return str(SESSION.status())


@mcp.tool()
def screenshot() -> Image:
  """Capture the current UI screen as a PNG. Read this to see what is on screen."""
  return _img(SESSION.screenshot())


@mcp.tool()
def tap(x: int, y: int, hold: float = 0.08) -> list:
  """Tap at (x, y) in screenshot pixels, then return a fresh screenshot.

  hold: press duration in seconds (default 0.08). On the small UI home screen a
  hold > 0.5s toggles Experimental Mode (only when longitudinal control is available).
  """
  SESSION.tap(x, y, hold)
  time.sleep(0.5)
  return [f"tapped ({x},{y}) hold={hold}", _img(SESSION.screenshot())]


@mcp.tool()
def swipe(x1: int, y1: int, x2: int, y2: int, dur: float = 0.4) -> list:
  """Swipe from (x1,y1) to (x2,y2) over dur seconds (stepped so it scrolls, not jumps).

  To scroll a list up, swipe from a lower y to a higher y. Horizontal swipes move
  between screens/cards. Returns a fresh screenshot.
  """
  SESSION.swipe(x1, y1, x2, y2, dur)
  time.sleep(0.5)
  return [f"swiped ({x1},{y1})->({x2},{y2}) dur={dur}", _img(SESSION.screenshot())]


@mcp.tool()
def hold(x: int, y: int, dur: float = 0.8) -> list:
  """Long-press at (x, y) for dur seconds (default 0.8). Returns a fresh screenshot."""
  SESSION.hold(x, y, dur)
  time.sleep(0.5)
  return [f"held ({x},{y}) dur={dur}", _img(SESSION.screenshot())]


@mcp.tool()
def run(script: str) -> list:
  """Execute a multi-step touch chain in one call and return each captured screenshot.

  Steps are separated by ';' or newlines; '#' starts a comment. Coordinates are
  screenshot pixels. Steps:
    tap X Y [HOLD]            # tap; optional hold seconds (default 0.08)
    swipe X1 Y1 X2 Y2 [DUR]   # swipe over DUR seconds (default 0.4)
    hold X Y [DUR]            # long-press (default 0.8)
    wait S                    # sleep S seconds (put one before a capture so the UI settles)
    capture [NAME]            # screenshot; NAME labels it (default = step index)

  Example: 'tap 268 120; wait 0.6; capture settings; tap 150 120; wait 0.6; capture toggles'
  """
  log, shots = SESSION.run_chain(script)
  out: list = ["\n".join(log)]
  for name, pil in shots:
    out.append(f"--- {name} ---")
    out.append(_img(pil))
  return out


@mcp.tool()
def set_param(name: str, value: str, restart: bool = False) -> str:
  """Write an openpilot Param (e.g. ShowDebugInfo=1 for the touch/widget overlay).

  Params are read at UI start, so pass restart=True to relaunch the UI afterwards.
  """
  msg = SESSION.set_param(name, value)
  if restart and SESSION.is_running():
    SESSION.stop(quiet=True)
    SESSION.start(mode=SESSION.mode)
    msg += " (UI restarted)"
  return msg


@mcp.tool()
def start_replay(route: str = "", dcam: bool = False, ecam: bool = False) -> str:
  """Replay a route so the onroad UI shows real data. Empty route uses --demo.

  Start the UI first; replay shares its msgq. dcam/ecam load driver/wide cameras.
  """
  extra = []
  if dcam:
    extra.append("--dcam")
  if ecam:
    extra.append("--ecam")
  return str(SESSION.start_replay(route, extra))


@mcp.tool()
def stop_replay() -> str:
  """Stop the running replay."""
  return str(SESSION.stop_replay())


@mcp.tool()
def logs(lines: int = 40) -> str:
  """Return the last N lines of the UI process log (stdout+stderr) for debugging."""
  return SESSION.logs(lines)


def main() -> None:
  import atexit
  import sys
  atexit.register(lambda: SESSION.stop(quiet=True))

  def _on_signal(*_):
    SESSION.stop(quiet=True)
    sys.exit(0)

  for sig in (signal.SIGTERM, signal.SIGHUP):
    try:
      signal.signal(sig, _on_signal)
    except (ValueError, OSError):
      pass
  mcp.run()


if __name__ == "__main__":
  main()
