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
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image as PILImage
from Xlib import X, display
from Xlib.ext import xtest

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

SIZES = {
  "small": (536, 240),
  "big": (2160, 1080),
}

UI_READY_TIMEOUT = 40.0
UI_RUNNER = Path(__file__).resolve().parent / "ui_runner.py"
# after an input, poll until the screen holds still instead of sleeping a fixed time.
# a moving camera (replay) never holds still, so that case ends at SETTLE_MAX
SETTLE_MIN = 0.08
SETTLE_STABLE = 0.1
SETTLE_MAX = 0.5
SWIPE_STEPS = 24
REPLAY_READY_TIMEOUT = 40.0
ONROAD_MOTION = 0.3  # seconds of continuous change, with instant onroad only a playing camera does that
DEMO_START = 90  # the demo route is engaged and moving here, it only engages ~20s in
DEMO_ROUTE = "5beb9b58bd12b691/0000010a--a51155e496"
ROUTE_CACHE = Path("/tmp/op_ui_mcp_routes")
ROUTE_CACHE_SEGMENTS = 3
ROUTE_CACHE_SCRIPT = Path(__file__).resolve().parent / "route_cache.py"

CLIP_DIR = Path("/tmp/op_ui_mcp_clips")
CLIP_RUNNER = Path(__file__).resolve().parent / "clip_runner.py"
FAST_CLIP = Path(__file__).resolve().parent / "fast_clip.py"
UI_DIFF_RUNNER = Path(__file__).resolve().parent / "ui_diff_runner.py"
MAP_FRAMES_RUNNER = Path(__file__).resolve().parent / "map_frames_runner.py"
MAPSD_RUNNER = Path(__file__).resolve().parent / "mapsd_runner.py"
MAP_DIR = Path("/tmp/op_ui_mcp_maps")
# where a device keeps its tile cache (/data/navigation/tiles), copied here it serves maps offline
DEFAULT_TILES = Path.home() / ".comma" / "navigation" / "tiles"
NAV_DESTINATION = {"latitude": 0.0, "longitude": 0.0}  # the nav view only needs one set, mapsd follows navRoute
ROUTE_RE = r"([0-9a-f]{16})[/|]([0-9a-f]{8}--[0-9a-f]{10}|\d{4}-\d{2}-\d{2}--\d{2}-\d{2}-\d{2})"
UI_DIFF_FPS = 60
OFFROAD_ALERT_REFRESH = 5.0  # REFRESH_INTERVAL in selfdrive/ui/mici/layouts/offroad_alerts.py
UI_DIFF_GAP = 30  # changed frames closer than this belong to one screen
RECORD_FPS = 30


# a pip/venv ffmpeg on PATH can lack filters like pad, prefer the system build
def _ff(name: str) -> str:
  return f"/usr/bin/{name}" if os.path.exists(f"/usr/bin/{name}") else (shutil.which(name) or name)


# nested layout keeps SConstruct at root but moves the source under openpilot/
# return the prefix to build paths from, or None if this is not a checkout
def _pkg_prefix(root: Path) -> str | None:
  for prefix in ("", "openpilot"):
    if (root / prefix / "selfdrive" / "ui" / "ui.py").exists():
      return prefix
  return None


def _is_checkout(p: Path) -> bool:
  return (p / "SConstruct").exists() and _pkg_prefix(p) is not None


# $OPENPILOT_ROOT, else the checkout the client was started in, else ~/openpilot
def _find_openpilot_root() -> Path:
  env = os.getenv("OPENPILOT_ROOT")
  if env:
    return Path(env).expanduser().resolve()
  for start in (Path.cwd().resolve(), Path(__file__).resolve()):
    for p in [start, *start.parents]:
      if _is_checkout(p):
        return p
  return Path.home() / "openpilot"


# msgq's cython extension only exists after scons, the UI can't import without it
def _is_built(root: Path) -> bool:
  return any(any((root / d).glob("ipc_pyx*.so")) for d in ("msgq_repo/msgq", "msgq"))


# an explicit tile cache, else the default one if present, else "" for the online tile API
def _tiles_dir(tiles_dir: str) -> str:
  if tiles_dir:
    p = Path(tiles_dir).expanduser().resolve()
    if not (p / "version").exists():
      raise ToolError(f"no tile cache at {p}: copy a device's /data/navigation/tiles there (it has a 'version' file)")
    return str(p)
  return str(DEFAULT_TILES) if (DEFAULT_TILES / "version").exists() else ""


OPENPILOT_ROOT = _find_openpilot_root()

# replay's TUI fills logs with escape codes. strip CSI seqs and stray controls
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()#][0-9A-Za-z]|[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _strip_ansi(s: str) -> str:
  return _ANSI.sub("", s)


# ToolError so the client sees the message, mcp 2 hides other exceptions
class UISessionError(ToolError):
  pass


class UISession:
  def __init__(self, root: Path):
    self.root = root
    self.pkg_prefix = _pkg_prefix(root) or ""
    self.mode = "small"
    self.instant_onroad = True
    # own msgq/params namespace, so concurrent sessions (or a running openpilot) don't collide
    self.prefix = f"op_ui_mcp_{os.getpid()}"
    self._prefix_ready = False
    self.width = 0
    self.height = 0
    self.display_num: int | None = None
    self.display_name = ""
    self._xvfb: subprocess.Popen | None = None
    self._ui: subprocess.Popen | None = None
    self._replay: subprocess.Popen | None = None
    self._publisher: subprocess.Popen | None = None
    self._mapsd: subprocess.Popen | None = None
    self._render_xvfb: subprocess.Popen | None = None
    self._render_display = ""
    self._render_prefix_ready = False
    self._disp: display.Display | None = None
    self._root_win = None
    self._pointer = (0, 0)
    self._pressed = False
    self.ui_log = ""
    self.replay_log = ""
    self.publish_log = ""
    self.mapsd_log = ""

  def is_running(self) -> bool:
    return self._ui is not None and self._ui.poll() is None

  def set_root(self, root: str) -> None:
    p = Path(root).expanduser().resolve()
    if p == self.root:
      return
    if not (p / "SConstruct").exists():
      raise UISessionError(f"not an openpilot checkout: {p}")
    prefix = _pkg_prefix(p)
    if prefix is None:
      raise UISessionError(f"no selfdrive/ui/ui.py under {p}")
    if self.is_running():
      raise UISessionError("stop the UI before changing root")
    self.root = p
    self.pkg_prefix = prefix

  # path under the checkout, prefixed for the nested layout
  # first use: create the msgq dir, start from a copy of the default params (so no onboarding)
  # and link the comma auth, which lives under the prefixed home too
  def ensure_prefix(self) -> None:
    if self._prefix_ready:
      return
    self._setup_prefix(self.prefix)
    self._prefix_ready = True

  def _setup_prefix(self, prefix: str) -> None:
    os.makedirs(f"/dev/shm/msgq_{prefix}", exist_ok=True)
    home = Path.home() / f".comma{prefix}"
    home.mkdir(exist_ok=True)
    auth = Path.home() / ".comma" / "auth.json"
    if auth.exists() and not (home / "auth.json").exists():
      (home / "auth.json").symlink_to(auth)
    code = (
      "import os, shutil\n"
      "from openpilot.common.params import Params\n"
      "src, dst = os.path.expanduser('~/.comma/params/d'), Params().get_param_path()\n"
      "for f in os.listdir(src) if os.path.isdir(src) else []:\n"
      "  if os.path.isfile(os.path.join(src, f)):\n"
      "    shutil.copy2(os.path.join(src, f), dst)\n"
    )
    env = self._env()
    env["OPENPILOT_PREFIX"] = prefix
    res = subprocess.run([*self._python(), "-c", code], cwd=str(self.root), env=env, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
      raise UISessionError(f"setting up prefix {prefix} failed: {res.stderr.strip()}")

  # renders get their own prefix, so a clip's navigation params never leak into the live UI
  @property
  def render_prefix(self) -> str:
    return f"{self.prefix}_clip"

  # renders draw offscreen but GLFW still needs an X server: the UI's when it runs, else a small
  # private one that stays up for later renders
  def render_display(self) -> str:
    if self.is_running() and self.display_name:
      return self.display_name
    if self._render_xvfb is not None and self._render_xvfb.poll() is None:
      return self._render_display
    num = self._free_display(200)
    self._render_display = f":{num}"
    with open(f"/tmp/op_ui_mcp_xvfb{num}.log", "w") as xvfb_log:
      self._render_xvfb = subprocess.Popen(["Xvfb", self._render_display, "-screen", "0", "640x480x24", "-nolisten", "tcp"],
                                           stdout=xvfb_log, stderr=subprocess.STDOUT, start_new_session=True)
    self._wait_for_xserver(self._render_display)
    return self._render_display

  def cleanup_prefix(self) -> None:
    for prefix in (self.prefix, self.render_prefix):
      shutil.rmtree(f"/dev/shm/msgq_{prefix}", ignore_errors=True)
      shutil.rmtree(Path.home() / f".comma{prefix}", ignore_errors=True)
    self._prefix_ready = self._render_prefix_ready = False
    if self._render_xvfb is not None and self._render_xvfb.poll() is None:
      os.killpg(os.getpgid(self._render_xvfb.pid), signal.SIGTERM)

  def _rel(self, *parts: str) -> str:
    return str(Path(self.pkg_prefix, *parts))

  # nested layout imports openpilot.* so the repo root must be on PYTHONPATH
  def _env(self) -> dict:
    env = dict(os.environ)
    # replay shells out to python3 to download segments, it must find the checkout's venv,
    # not the plugin's own uv env that run.sh puts first on PATH
    venv_bin = self.root / ".venv" / "bin"
    if venv_bin.exists():
      env["PATH"] = str(venv_bin) + os.pathsep + env.get("PATH", "")
    env["OPENPILOT_PREFIX"] = self.prefix
    env.setdefault("COMMA_CACHE", "/tmp/comma_download_cache")  # share downloads across prefixes
    if self.pkg_prefix:
      existing = env.get("PYTHONPATH")
      env["PYTHONPATH"] = str(self.root) + (os.pathsep + existing if existing else "")
    return env

  # call the checkout's venv python directly, uv run would sync the venv on every launch
  def _python(self) -> list[str]:
    venv_python = self.root / ".venv" / "bin" / "python"
    if venv_python.exists():
      return [str(venv_python)]
    return ["uv", "run", "--no-sync", "python3"]

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
        time.sleep(0.02)
    raise UISessionError(f"X server {name} did not come up: {last}")

  def start(self, mode: str = "small", show_touches: bool = False, show_fps: bool = False,
            extra_env: dict | None = None, instant_onroad: bool | None = None, keep_feeds: bool = False) -> dict:
    if mode not in SIZES:
      raise UISessionError(f"unknown mode {mode!r}; use one of {list(SIZES)}")
    if self.is_running():
      raise UISessionError("UI already running; call stop_ui first or use restart_ui")
    if not _is_checkout(self.root):
      raise UISessionError(f"{self.root} is not an openpilot checkout; pass root= or set OPENPILOT_ROOT")
    if not _is_built(self.root):
      raise UISessionError(f"{self.root} is not built (no msgq ipc_pyx); run `scons -j$(nproc)` there or pass root=")
    self.stop(quiet=True, keep_feeds=keep_feeds)
    self.ensure_prefix()

    self.mode = mode
    if instant_onroad is not None:
      self.instant_onroad = instant_onroad
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

      env = self._env()
      env.update({
        "DISPLAY": self.display_name,
        "SCALE": "1.0",
        "BIG": "1" if mode == "big" else "0",
        "SHOW_TOUCHES": "1" if show_touches else "0",
        "SHOW_FPS": "1" if show_fps else "0",
        "QT_QPA_PLATFORM": "offscreen",
        "OP_UI_MCP_INSTANT_ONROAD": "1" if self.instant_onroad else "0",
      })
      if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items()})

      self.ui_log = f"/tmp/op_ui_mcp_ui{self.display_num}.log"
      with open(self.ui_log, "w") as ui_log_f:
        self._ui = subprocess.Popen(
          [*self._python(), str(UI_RUNNER), self._rel("selfdrive", "ui", "ui.py")],
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

  # relaunch the UI only, replay, publish and mapsd keep feeding the same msgq
  def restart(self, mode: str | None = None, show_touches: bool = False, show_fps: bool = False) -> dict:
    self.stop(quiet=True, keep_feeds=True)
    return self.start(mode=mode or self.mode, show_touches=show_touches, show_fps=show_fps, keep_feeds=True)

  def _wait_until_rendered(self, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
      if not self.is_running():
        return False
      try:
        img = self._grab()
        bbox = img.convert("L").point(lambda v: 255 if v > 40 else 0).getbbox()
        if bbox is not None:
          self.settle()
          return True
      except Exception:  # noqa: BLE001
        pass
      time.sleep(0.05)
    return False

  # keep_feeds leaves replay, publish and mapsd running, they share the msgq not the display
  def stop(self, quiet: bool = False, keep_feeds: bool = False) -> dict:
    if not keep_feeds:
      self.stop_replay(quiet=True)
      self.stop_publish(quiet=True)
      self.stop_mapsd(quiet=True)
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
      "instant_onroad": self.instant_onroad,
      "resolution": f"{self.width}x{self.height}" if self.width else None,
      "display": self.display_name or None,
      "openpilot_root": str(self.root),
      "pkg_prefix": self.pkg_prefix or None,
      "openpilot_prefix": self.prefix,
      "replay_running": self._replay is not None and self._replay.poll() is None,
      "publish_running": self._publisher is not None and self._publisher.poll() is None,
      "mapsd_running": self._mapsd is not None and self._mapsd.poll() is None,
      "ui_log": self.ui_log or None,
    }

  def _grab(self) -> PILImage.Image:
    if self._disp is None or self._root_win is None:
      raise UISessionError("no display; start the UI first")
    geo = self._root_win.get_geometry()
    raw = self._root_win.get_image(0, 0, geo.width, geo.height, X.ZPixmap, 0xffffffff)
    return PILImage.frombytes("RGB", (geo.width, geo.height), raw.data, "raw", "BGRX")

  # returns the settled frame so callers don't grab again
  def settle(self) -> PILImage.Image:
    start = time.monotonic()
    img = self._grab()
    last_change = start
    while True:
      time.sleep(1 / 60)
      now = time.monotonic()
      nxt = self._grab()
      if nxt.tobytes() != img.tobytes():
        last_change = now
      img = nxt
      if now - start >= SETTLE_MAX or (now - start >= SETTLE_MIN and now - last_change >= SETTLE_STABLE):
        return img

  def screenshot(self) -> PILImage.Image:
    if not self.is_running():
      raise UISessionError("UI is not running; call start_ui first")
    return self._grab()

  def settled_screenshot(self) -> PILImage.Image:
    if not self.is_running():
      raise UISessionError("UI is not running; call start_ui first")
    return self.settle()

  def _clamp(self, x: int, y: int) -> tuple[int, int]:
    return max(0, min(self.width - 1, int(x))), max(0, min(self.height - 1, int(y)))

  def _move(self, x: int, y: int) -> None:
    xtest.fake_input(self._disp, X.MotionNotify, x=x, y=y)
    self._disp.sync()
    self._pointer = (x, y)

  def _btn(self, press: bool) -> None:
    xtest.fake_input(self._disp, X.ButtonPress if press else X.ButtonRelease, 1)
    self._disp.sync()
    self._pressed = press

  # grabs on its own X connection, xlib connections aren't thread safe. a dot marks the finger while pressed
  def _record(self, out: Path, stop: threading.Event) -> None:
    from PIL import ImageDraw
    disp = display.Display(self.display_name)
    root = disp.screen().root
    geo = root.get_geometry()
    enc = subprocess.Popen([_ff("ffmpeg"), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{geo.width}x{geo.height}",
                            "-r", str(RECORD_FPS), "-i", "pipe:0", "-vf", "format=yuv420p", "-c:v", "libx264", "-crf", "20",
                            "-preset", "veryfast", "-movflags", "+faststart", str(out)], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    r = max(6, geo.height // 24)
    start, written = time.monotonic(), 0
    try:
      while not stop.is_set():
        raw = root.get_image(0, 0, geo.width, geo.height, X.ZPixmap, 0xffffffff)
        img = PILImage.frombytes("RGB", (geo.width, geo.height), raw.data, "raw", "BGRX")
        if self._pressed:
          x, y = self._pointer
          ImageDraw.Draw(img).ellipse((x - r, y - r, x + r, y + r), fill=(255, 40, 40))
        # constant frame rate: repeat the frame for every tick it covers, so the video plays in real time
        due = int((time.monotonic() - start) * RECORD_FPS) + 1
        for _ in range(max(due - written, 1 if written == 0 else 0)):
          enc.stdin.write(img.tobytes())
          written += 1
        time.sleep(max(0.0, (written / RECORD_FPS) - (time.monotonic() - start)))
    finally:
      enc.stdin.close()
      enc.wait(timeout=60)
      disp.close()

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

  def run_chain(self, script: str, record: bool = False) -> tuple[list[str], list[tuple[str, PILImage.Image]], Path | None]:
    if not record:
      return (*self._run_steps(script), None)
    if not self.is_running():
      raise UISessionError("UI is not running; call start_ui first")
    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    out = CLIP_DIR / f"run_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    stop = threading.Event()
    rec = threading.Thread(target=self._record, args=(out, stop), daemon=True)
    rec.start()
    try:
      log, shots = self._run_steps(script)
      time.sleep(0.5)  # let the last screen land in the video
    finally:
      stop.set()
      rec.join(timeout=90)
    return log, shots, out

  def _run_steps(self, script: str) -> tuple[list[str], list[tuple[str, PILImage.Image]]]:
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
        shots.append((name, self.settle()))
        log.append(f"[{i}] capture -> {name}")
      else:
        raise UISessionError(f"[{i}] unknown step: {op!r}")
    return log, shots

  def get_param(self, name: str):
    self.ensure_prefix()
    code = (
      "import json\n"
      "from openpilot.common.params import Params\n"
      f"v = Params().get({name!r})\n"
      "print(json.dumps(v.decode(errors='replace') if isinstance(v, bytes) else v, default=str))\n"
    )
    res = subprocess.run([*self._python(), "-c", code], cwd=str(self.root),
                         env=self._env(), capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
      raise UISessionError(f"get_param failed: {res.stderr.strip() or res.stdout.strip()}")
    return json.loads(res.stdout.strip().splitlines()[-1])

  # returns whether the key feeds the offroad alert list
  def set_param(self, name: str, value: bool | int | float | str | dict | list | None) -> bool:
    self.ensure_prefix()
    # put routes by the value's python type with no coercion, so pass it through as-is.
    # str(v) would break INT/FLOAT params. a bool uses put_bool to be explicit.
    # json carries the value so nan/inf survive, repr would emit bare NameError tokens.
    # block=True returns only once the write is on disk, older Params have no block
    code = (
      "import json\n"
      "from openpilot.common.params import Params\n"
      f"name, v = {name!r}, json.loads({json.dumps(value)!r})\n"
      "p = Params()\n"
      "try:\n"
      "  from openpilot.selfdrive.selfdrived.alertmanager import OFFROAD_ALERTS\n"
      "except ImportError:\n"
      "  OFFROAD_ALERTS = {}\n"
      "alert = name in OFFROAD_ALERTS or name.startswith('Offroad_') or name in ('UpdaterNewDescription', 'UpdateAvailable')\n"
      "if name in OFFROAD_ALERTS and isinstance(v, bool):\n"
      "  # like set_offroad_alert: true shows the alert's own config, false clears it\n"
      "  v = dict(OFFROAD_ALERTS[name], extra='') if v else None\n"
      "if v is None:\n"
      "  p.remove(name)\n"
      "else:\n"
      "  put = p.put_bool if isinstance(v, bool) else p.put\n"
      "  try:\n"
      "    put(name, v, block=True)\n"
      "  except TypeError:\n"
      "    put(name, v)\n"
      "print(json.dumps({'alert': alert}))\n"
    )
    res = subprocess.run([*self._python(), "-c", code], cwd=str(self.root),
                         env=self._env(), capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
      raise UISessionError(f"set_param failed: {res.stderr.strip() or res.stdout.strip()}")
    return bool(json.loads(res.stdout.strip().splitlines()[-1])["alert"])

  def publish(self, service: str, fields: dict, hz: float = 0.0, secs: float = 0.0,
              background: bool = False) -> str:
    # run in the checkout's env so it uses that cereal schema. fields set by dotted path.
    # nested layout puts cereal under the openpilot package, flat keeps it top-level
    if not self.is_running():
      raise UISessionError("start the UI before publish so they share the same msgq")
    if hz <= 0 and secs > 0:
      raise UISessionError("secs needs hz > 0")
    if background and hz <= 0:
      raise UISessionError("background publish needs hz > 0")
    if hz > 0 and secs <= 0 and not background:
      raise UISessionError("a foreground repeat needs secs > 0; use background=True for indefinite")
    cereal_pkg = f"{self.pkg_prefix}.cereal" if self.pkg_prefix else "cereal"
    code = (
      "import sys, time, json\n"
      f"from {cereal_pkg} import messaging\n"
      f"service = {service!r}\n"
      f"fields = json.loads({json.dumps(fields)!r})\n"
      f"hz, secs = {hz!r}, {secs!r}\n"
      "pm = messaging.PubMaster([service])\n"
      "msg = messaging.new_message(service)\n"
      "for path, val in fields.items():\n"
      "  obj = getattr(msg, service)\n"
      "  parts = path.split('.')\n"
      "  for p in parts[:-1]:\n"
      "    obj = getattr(obj, p)\n"
      "  setattr(obj, parts[-1], val)\n"
      "pm.send(service, msg)\n"
      "print('READY', file=sys.stderr, flush=True)\n"  # built and sent once, background waits on this
      "if hz > 0:\n"
      "  end = None if secs <= 0 else time.monotonic() + secs\n"
      "  n = 1\n"
      "  while end is None or time.monotonic() < end:\n"
      "    time.sleep(1.0 / hz)\n"
      "    pm.send(service, msg)\n"
      "    n += 1\n"
      "  print('published', service, n, 'times')\n"
      "else:\n"
      "  print('published', service, 'once')\n"
    )
    cmd = [*self._python(), "-c", code]
    if background:
      # detach so the UI can be screenshotted mid-publish. a new background publish
      # replaces any prior one. stop_publish or stop_ui ends it
      self.stop_publish(quiet=True)
      self.publish_log = f"/tmp/op_ui_mcp_publish{self.display_num}.log"
      with open(self.publish_log, "w") as log_f:
        self._publisher = subprocess.Popen(cmd, cwd=str(self.root), env=self._env(),
                                            stdout=log_f, stderr=subprocess.STDOUT,
                                            stdin=subprocess.DEVNULL, start_new_session=True)
      # wait through uv/cereal cold start for the first send (READY) or an early crash
      deadline = time.monotonic() + 15.0
      while time.monotonic() < deadline:
        rc = self._publisher.poll()
        if rc is not None and rc != 0:
          err = self._read_log(self.publish_log)
          self._publisher = None
          raise UISessionError(f"publish failed: {err}")
        if rc == 0 or "READY" in self._read_log(self.publish_log):
          break
        time.sleep(0.1)
      return f"publishing {service} in background (pid {self._publisher.pid}); stop with stop_publish"
    res = subprocess.run(cmd, cwd=str(self.root), env=self._env(),
                         capture_output=True, text=True, timeout=max(120.0, secs + 30))
    if res.returncode != 0:
      raise UISessionError(f"publish failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()

  def stop_publish(self, quiet: bool = False) -> dict:
    self._kill_proc("_publisher")
    return {} if quiet else {"publish_stopped": True}

  # ui_state.started = deviceState.started, so publishing it false drops the UI to the home page
  def go_offroad(self) -> str:
    return self.publish("deviceState", {"started": False}, hz=20, secs=0.5)

  # a published alert sticks via _prev_alert until a none-size alert fades out, so stop any
  # background publisher first (else it keeps re-sending the alert) then send the clear
  def clear_alerts(self) -> str:
    self.stop_publish(quiet=True)
    return self.publish("selfdriveState",
                        {"alertText1": "", "alertText2": "", "alertSize": "none", "alertStatus": "normal"},
                        hz=25, secs=1.5)

  def start_replay(self, route: str = "", extra_args: list[str] | None = None) -> dict:
    if not self.is_running():
      raise UISessionError("start the UI before replay so they share the same msgq")
    self.stop_replay(quiet=True)
    args = [self._rel("tools", "replay", "replay")]
    if route:
      args.append(route)
    else:
      args.append("--demo")
    if extra_args:
      args += list(extra_args)
    self.replay_log = f"/tmp/op_ui_mcp_replay{self.display_num}.log"
    env = self._env()
    env["DISPLAY"] = self.display_name
    with open(self.replay_log, "w") as log_f:
      self._replay = subprocess.Popen(args, cwd=str(self.root), env=env,
                                      stdout=log_f, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL, start_new_session=True)
    time.sleep(0.1)
    alive = self._replay.poll() is None
    return {"replay_started": alive, "args": args, "replay_log": self.replay_log}

  # copy the segments replay needs into a local dir once, so replay -d skips the route API lookup
  # and the python downloader (streaming starts in ~0.3s instead of ~1.1s). returns the dir and
  # the first cached segment, or None when the route string isn't a plain dongle/route
  def cache_route(self, route: str, first_seg: int, dcam: bool, ecam: bool) -> Path | None:
    m = re.fullmatch(ROUTE_RE, route)
    if m is None:
      return None
    self.ensure_prefix()
    cache_dir = ROUTE_CACHE / f"{m.group(1)}_{m.group(2)}"
    wanted = ["fcamera.hevc"] + (["dcamera.hevc"] if dcam else []) + (["ecamera.hevc"] if ecam else [])
    seg_dirs = [cache_dir / f"{m.group(2)}--{seg}" for seg in range(first_seg, first_seg + ROUTE_CACHE_SEGMENTS)]
    have = all((d / f).exists() for d in seg_dirs for f in wanted) and all(any(d.glob("?log*")) for d in seg_dirs)
    if not have:
      cmd = [*self._python(), str(ROUTE_CACHE_SCRIPT), f"{m.group(1)}/{m.group(2)}", str(cache_dir), str(first_seg),
             str(ROUTE_CACHE_SEGMENTS), *(["dcam"] if dcam else []), *(["ecam"] if ecam else [])]
      res = subprocess.run(cmd, cwd=str(self.root), env=self._env(), capture_output=True, text=True, timeout=600)
      if res.returncode != 0 or not (seg_dirs[0] / "fcamera.hevc").exists():
        return None
    return cache_dir

  # onroad shows the camera, so the screen changes every frame while offroad is static. without
  # instant onroad the UI holds offroad ~2.5s then scrolls over, so wait out the scroll too
  def wait_onroad(self, timeout: float = REPLAY_READY_TIMEOUT) -> bool:
    start = time.monotonic()
    prev = self._grab().tobytes()
    moving_since = None
    while time.monotonic() - start < timeout:
      if self._replay is None or self._replay.poll() is not None:
        return False
      time.sleep(0.1)
      cur = self._grab().tobytes()
      if cur != prev:
        moving_since = moving_since or time.monotonic()
        if time.monotonic() - moving_since >= (ONROAD_MOTION if self.instant_onroad else 1.5):
          return True
      else:
        moving_since = None
      prev = cur
    return False

  def start_mapsd(self, tiles_dir: str = "") -> dict:
    if not self.is_running():
      raise UISessionError("start the UI before start_nav so they share the same msgq")
    if not (self.root / self._rel("selfdrive", "navigation", "mapsd.py")).exists():
      raise UISessionError(f"{self.root} has no selfdrive/navigation/mapsd.py, check out a branch with navigation")
    tiles = _tiles_dir(tiles_dir)
    self.stop_mapsd(quiet=True)
    self.set_param("NavigationEnabled", True)
    self.set_param("NavDestination", NAV_DESTINATION)
    self.mapsd_log = f"/tmp/op_ui_mcp_mapsd{self.display_num}.log"
    with open(self.mapsd_log, "w") as log_f:
      self._mapsd = subprocess.Popen([*self._python(), str(MAPSD_RUNNER), tiles], cwd=str(self.root), env=self._env(),
                                     stdout=log_f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    time.sleep(1.0)
    if self._mapsd.poll() is not None:
      raise UISessionError(f"mapsd exited: {self._read_log(self.mapsd_log)}")
    return {"mapsd_started": True, "tiles": tiles or "online (needs a device key, fails on a PC)", "mapsd_log": self.mapsd_log}

  def stop_mapsd(self, quiet: bool = False) -> dict:
    self._kill_proc("_mapsd")
    return {} if quiet else {"mapsd_stopped": True}

  # Route(data_dir) wants segment dirs named dongle|route--N. start_replay's cache and a plain copy off a
  # device use route--N, so link those under the expected names
  def resolve_data_dir(self, route: str, data_dir: str) -> str:
    if not data_dir:
      return ""
    d = Path(data_dir).expanduser().resolve()
    m = re.fullmatch(ROUTE_RE, route.rsplit("/", 2)[0] if route.count("/") == 3 else route)
    if m is None or not d.is_dir():
      return str(d)
    if d.name == f"{m.group(1)}|{m.group(2)}":
      return str(d.parent)
    plain = [c for c in d.iterdir() if c.is_dir() and re.fullmatch(rf"{re.escape(m.group(2))}--\d+", c.name)]
    if not plain:
      return str(d)
    links = ROUTE_CACHE / f"{m.group(1)}_{m.group(2)}_links"
    links.mkdir(parents=True, exist_ok=True)
    for c in plain:
      link = links / f"{m.group(1)}|{c.name}"
      if not link.exists():
        link.symlink_to(c)
    return str(links)

  def make_map_frames(self, route: str, start: int, end: int, data_dir: str = "", tiles_dir: str = "",
                      output: str = "", refresh: bool = False) -> dict:
    m = re.fullmatch(ROUTE_RE, route)
    if m is None:
      raise UISessionError(f"route must be dongle/route, got {route!r}")
    if end <= start:
      raise UISessionError(f"end ({end}) must be after start ({start})")
    tiles = _tiles_dir(tiles_dir)
    out = Path(output).expanduser().resolve() if output else MAP_DIR / f"{m.group(1)}_{m.group(2)}_{start}_{end}.npz"
    if out.exists() and not refresh:
      return {"map_frames": str(out), "cached": True}
    out.parent.mkdir(parents=True, exist_ok=True)
    self.ensure_prefix()
    t0 = time.monotonic()
    cmd = [*self._python(), str(MAP_FRAMES_RUNNER), f"{m.group(1)}/{m.group(2)}", self.resolve_data_dir(route, data_dir),
           str(start), str(end), str(out), tiles]
    res = subprocess.run(cmd, cwd=str(self.root), env=self._env(), capture_output=True, text=True, timeout=3600)
    if res.returncode != 0 or not out.exists():
      raise UISessionError(f"map frames failed:\n{_strip_ansi((res.stderr or res.stdout).strip())[-2000:]}")
    return {"map_frames": str(out), "cached": False, "seconds": round(time.monotonic() - t0, 1),
            "tiles": tiles or "online", "result": res.stdout.strip().splitlines()[-1]}

  def stop_replay(self, quiet: bool = False) -> dict:
    self._kill_proc("_replay")
    return {} if quiet else {"replay_stopped": True}

  # SIGTERM the process group, escalate to kill, then drop the handle
  def _kill_proc(self, attr: str) -> None:
    proc = getattr(self, attr)
    if proc is not None and proc.poll() is None:
      try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
      except Exception:  # noqa: BLE001
        try:
          proc.kill()
          proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
          pass
    setattr(self, attr, None)

  def _read_log(self, path: str, lines: int = 20) -> str:
    try:
      with open(path) as f:
        return _strip_ansi("".join(f.readlines()[-lines:])).strip()
    except OSError:
      return ""

  def _git(self, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True)
    if res.returncode != 0:
      raise UISessionError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout

  # write the ref's version of every UI python file that differs from the working tree,
  # named by module so clip_runner.py can import them in place of the checkout's copy
  def _ref_overlay(self, ref: str, overlay_dir: Path) -> list[str]:
    self._git("rev-parse", "--verify", f"{ref}^{{commit}}")
    ui_dirs = [self._rel("selfdrive", "ui"), self._rel("system", "ui")]
    modules = []
    for path in self._git("diff", "--name-only", ref, "--", *ui_dirs).split():
      if not path.endswith(".py") or path.endswith("__init__.py") or "/tests/" in path:
        continue
      res = subprocess.run(["git", "-C", str(self.root), "show", f"{ref}:{path}"], capture_output=True)
      if res.returncode != 0:  # new in the working tree, only new code imports it
        continue
      rel = Path(path).relative_to(self.pkg_prefix) if self.pkg_prefix else Path(path)
      module = "openpilot." + ".".join(rel.with_suffix("").parts)
      (overlay_dir / f"{module}.py").write_bytes(res.stdout)
      modules.append(module)
    return modules

  def render_clip(self, route: str = "", start: int | None = None, end: int | None = None,
                  compare_ref: str = "", big: bool = False, qcam: bool = False,
                  overlays: bool = False, output: str = "", speed: float = 1.0,
                  map_frames: str = "", data_dir: str = "", env: dict | None = None) -> tuple[dict, Path]:
    # the text overlays only exist in the checkout's clip tool, everything else renders with fast_clip
    if overlays:
      if speed != 1.0 or map_frames or data_dir:
        raise UISessionError("overlays render with the checkout's clip tool, which has no speed, map_frames or data_dir")
      return self._render_clip_tool(route, start, end, compare_ref, big, qcam, overlays, output)
    if not _is_built(self.root):
      raise UISessionError(f"{self.root} is not built (no msgq ipc_pyx); run `scons -j$(nproc)` there")
    if route.count("/") == 3:
      route, s, e = route.rsplit("/", 2)
      start, end = int(s) if start is None else start, int(e) if end is None else end
    elif not route:
      route = DEMO_ROUTE
      start = DEMO_START if start is None else start
      end = start + 15 if end is None else end
    if start is None or end is None:
      raise UISessionError("pass start and end (seconds) with a route, or route as dongle/route/start/end")
    if end <= start:
      raise UISessionError(f"end ({end}) must be after start ({start})")
    if map_frames and not Path(map_frames).expanduser().exists():
      raise UISessionError(f"no map frames at {map_frames}")
    if not self._render_prefix_ready:
      self._setup_prefix(self.render_prefix)
      self._render_prefix_ready = True
    run_env = self._env()
    run_env.update({str(k): str(v) for k, v in (env or {}).items()})
    run_env.update({"OPENPILOT_PREFIX": self.render_prefix, "DISPLAY": self.render_display()})

    args = ["--route", route, "--start", str(start), "--end", str(end), "--speed", str(speed)]
    if big:
      args.append("--big")
    if qcam:
      args.append("--qcam")
    if map_frames:
      args += ["--map", str(Path(map_frames).expanduser().resolve())]
    if data_dir:
      args += ["--data-dir", self.resolve_data_dir(route, data_dir)]

    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(output).expanduser().resolve() if output else CLIP_DIR / f"clip_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="clip_", dir=CLIP_DIR))
    renders = [("current", "", out if not compare_ref else work / "current.mp4")]
    overlay_modules: list[str] = []
    if compare_ref:
      overlay_dir = work / "overlay"
      overlay_dir.mkdir()
      overlay_modules = self._ref_overlay(compare_ref, overlay_dir)
      renders.insert(0, (compare_ref, str(overlay_dir), work / "ref.mp4"))

    t0 = time.monotonic()
    procs = []
    for i, (label, overlay, dst) in enumerate(renders):
      log = work / f"render{i}.log"
      cmd = [*self._python(), str(FAST_CLIP), *args, "-o", str(dst)] + (["--overlay", overlay] if overlay else [])
      with open(log, "w") as log_f:
        procs.append((label, dst, log, subprocess.Popen(cmd, cwd=str(self.root), env=run_env, stdout=log_f, stderr=subprocess.STDOUT,
                                                        stdin=subprocess.DEVNULL, start_new_session=True)))
    timings = {}
    for label, dst, log, proc in procs:
      rc = proc.wait()
      if rc != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise UISessionError(f"render of {label} failed (code {rc}):\n{self._read_log(str(log), 15)}")
      timings[label] = next((line.strip() for line in open(log) if line.startswith("rendered")), "")

    if compare_ref:
      res = subprocess.run([_ff("ffmpeg"), "-y", "-v", "error", "-i", str(renders[0][2]), "-i", str(renders[1][2]), "-filter_complex",
                            "[0:v]pad=w=iw:h=ih+4:x=0:y=0:color=white[top];[top][1:v]vstack=inputs=2:shortest=1",
                            "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-an", str(out)], capture_output=True, text=True)
      if res.returncode != 0:
        raise UISessionError(f"stacking failed: {res.stderr.strip()}")

    result = {
      "output": str(out),
      "start": start, "end": end, "speed": speed,
      "seconds": round(time.monotonic() - t0, 2),
      "layout": f"top={compare_ref}, bottom=working tree" if compare_ref else "working tree",
      "render": timings,
      "work_dir": str(work),
    }
    if compare_ref:
      result["ref_modules"] = overlay_modules
    return result, out

  def _render_clip_tool(self, route: str = "", start: int | None = None, end: int | None = None,
                        compare_ref: str = "", big: bool = False, qcam: bool = False,
                        overlays: bool = False, output: str = "") -> tuple[dict, Path]:
    clip_script = self.root / self._rel("tools", "clip", "run.py")
    if not clip_script.exists():
      raise UISessionError(f"no clip tool at {clip_script}")
    if not _is_built(self.root):
      raise UISessionError(f"{self.root} is not built (no msgq ipc_pyx); run `scons -j$(nproc)` there")
    if route and (start is None or end is None) and route.count("/") != 3:
      raise UISessionError("pass start and end (seconds) with a route, or route as dongle/route/start/end")
    self.ensure_prefix()

    clip_args = [route] if route else ["--demo"]
    if start is not None:
      clip_args += ["-s", str(start)]
    if end is not None:
      clip_args += ["-e", str(end)]
    clip_args += ["-f", "0"]  # constant quality, not squeezed into a target size
    if big:
      clip_args.append("--big")
    if qcam:
      clip_args.append("--qcam")
    if not overlays:
      clip_args += ["--no-metadata", "--no-time-overlay"]

    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(output).expanduser().resolve() if output else CLIP_DIR / f"clip_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="clip_", dir=CLIP_DIR))

    # (label, overlay dir, output) per render, all run in parallel through clip_runner.py, which
    # adds streaming decode and the frame clock when the checkout's clip tool lacks them
    renders = [("current", "", out if not compare_ref else work / "current.mp4")]
    overlay_modules: list[str] = []
    if compare_ref:
      overlay_dir = work / "overlay"
      overlay_dir.mkdir()
      overlay_modules = self._ref_overlay(compare_ref, overlay_dir)
      renders.insert(0, (compare_ref, str(overlay_dir), work / "ref.mp4"))

    t0 = time.monotonic()
    procs = []
    for i, (label, overlay, dst) in enumerate(renders):
      log = work / f"render{i}.log"
      cmd = [*self._python(), str(CLIP_RUNNER), overlay, str(clip_script), *clip_args, "-o", str(dst)]
      with open(log, "w") as log_f:
        procs.append((label, dst, log, subprocess.Popen(cmd, cwd=str(self.root), env=self._env(),
                                                        stdout=log_f, stderr=subprocess.STDOUT,
                                                        stdin=subprocess.DEVNULL, start_new_session=True)))
    warnings = []
    for label, dst, log, proc in procs:
      rc = proc.wait()
      # an end past the last camera frame exits nonzero after the clip is already written
      if not dst.exists() or dst.stat().st_size == 0:
        raise UISessionError(f"render of {label} failed (code {rc}):\n{self._read_log(str(log), 15)}")
      if rc != 0:
        warnings.append(f"{label} exited {rc} after writing the clip: {self._read_log(str(log), 1)}")

    if compare_ref:
      res = subprocess.run([_ff("ffmpeg"), "-y", "-v", "error", "-i", str(renders[0][2]), "-i", str(renders[1][2]), "-filter_complex",
                            "[0:v]pad=w=iw:h=ih+4:x=0:y=0:color=white[top];[top][1:v]vstack=inputs=2:shortest=1",
                            "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-an", str(out)], capture_output=True, text=True)
      if res.returncode != 0:
        raise UISessionError(f"stacking failed: {res.stderr.strip()}")

    result = {
      "output": str(out),
      "seconds": round(time.monotonic() - t0, 1),
      "layout": f"top={compare_ref}, bottom=working tree" if compare_ref else "working tree",
      "work_dir": str(work),
    }
    if compare_ref:
      result["ref_modules"] = overlay_modules
    if warnings:
      result["warnings"] = warnings
    return result, out

  # record openpilot's scripted UI tour with the ref's UI code and with the working tree, in
  # parallel, then compare every frame. returns changed frame runs and the two videos
  def ui_diff(self, ref: str, big: bool = False) -> dict:
    replay = self.root / self._rel("selfdrive", "ui", "tests", "diff", "replay.py")
    if not replay.exists():
      raise UISessionError(f"no UI tour at {replay}")
    if not _is_built(self.root):
      raise UISessionError(f"{self.root} is not built (no msgq ipc_pyx); run `scons -j$(nproc)` there")
    self.ensure_prefix()
    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="ui_diff_", dir=CLIP_DIR))
    overlay_dir = work / "overlay"
    overlay_dir.mkdir()
    modules = self._ref_overlay(ref, overlay_dir)
    variant = "tizi" if big else "mici"

    t0 = time.monotonic()
    procs = []
    for label, overlay in ((ref, str(overlay_dir)), ("working tree", "")):
      video = work / ("ref.mp4" if overlay else "new.mp4")
      log = work / ("ref.log" if overlay else "new.log")
      with open(log, "w") as log_f:
        procs.append((label, video, log, subprocess.Popen([*self._python(), str(UI_DIFF_RUNNER), overlay, variant, str(video)],
                                                          cwd=str(self.root), env=self._env(), stdout=log_f, stderr=subprocess.STDOUT,
                                                          stdin=subprocess.DEVNULL, start_new_session=True)))
    for label, video, log, proc in procs:
      if proc.wait() != 0 or not video.exists():
        raise UISessionError(f"UI tour for {label} failed:\n{self._read_log(str(log), 15)}")

    ref_video, new_video = procs[0][1], procs[1][1]
    h_ref, h_new = _frame_hashes(ref_video), _frame_hashes(new_video)
    changed = [i for i, (a, b) in enumerate(zip(h_ref, h_new, strict=False)) if a != b]
    runs: list[list[int]] = []
    for i in changed:
      if runs and i - runs[-1][1] <= UI_DIFF_GAP:
        runs[-1][1] = i
      else:
        runs.append([i, i])
    return {
      "ref": ref, "variant": variant, "seconds": round(time.monotonic() - t0, 1),
      "frames": [len(h_ref), len(h_new)], "changed_frames": len(changed), "ref_modules": modules,
      "runs": runs, "ref_video": str(ref_video), "new_video": str(new_video), "work_dir": str(work),
    }

  def logs(self, lines: int = 40) -> str:
    return self._read_log(self.ui_log, lines) if self.ui_log else "(no UI log yet)"


SESSION = UISession(OPENPILOT_ROOT)


def _img(pil: PILImage.Image) -> Image:
  buf = io.BytesIO()
  pil.save(buf, format="PNG")
  return Image(data=buf.getvalue(), format="png")


mcp = MCPServer(
  "mici-ui-mcp",
  instructions=(
    "Drive the openpilot UI locally to build and validate UI changes without a device.\n"
    "Typical loop: start_ui -> screenshot (find the target in the image) -> "
    "tap/swipe/hold at those coordinates -> the tool returns a fresh screenshot to confirm.\n"
    "Coordinates are the pixels you see in the screenshot: small UI is 536x240, big UI is "
    "2160x1080, origin top-left. For multi-step flows prefer the 'run' tool (one chain).\n"
    "After editing UI code, call restart_ui to reload it (a running replay keeps going). Use start_replay "
    "to feed a route so the onroad UI shows real driving data.\n"
    "Videos: render_clip renders a route offline in seconds, use frames=[...] to see moments without "
    "opening the mp4. Find moments with logreader's find_windows. To screen-record an interactive flow "
    "(taps, typing, settings), use run(script, record=True).\n"
    "Params: get_param reads, set_param writes (null removes). Fake any message (GPS, carState, ...) "
    "with publish.\n"
    "Navigation view (arrow + map): render_clip(nav=True) for videos, start_nav before start_replay for "
    "live. Both build the map from the drive's own log, the drive must have been navigating. Maps come from "
    "a tile cache copied off a device to ~/.comma/navigation/tiles (or tiles_dir=), a PC can't use the "
    "online tile API.\n"
    "Local drive copies: pass the folder as data_dir, any layout (dongle|route, route--N dirs, "
    "/tmp/op_ui_mcp_routes). Without data_dir, routes download from comma.\n"
    "Comparing UI variants: compare_ref for git refs, or env={...} per render_clip call for variants your "
    "code reads from os.environ.\n"
    "These tools cover the whole loop. Don't write scripts to run mapsd, build map frames, rename route "
    "folders, switch variants, record the screen, or read and write params, a tool does each of those. "
    "Load all of this server's tools before deciding one is missing."
  ),
)


@mcp.tool()
def start_ui(mode: str = "small", show_touches: bool = False, show_fps: bool = False,
             root: str | None = None, instant_onroad: bool = True) -> list:
  """Launch the openpilot UI on a private headless display and return a screenshot.

  mode: 'small' (536x240, comma four/mici layout) or 'big' (2160x1080, tici layout).
  show_touches: draw a red dot + trail at injected touches plus red debug outlines on every widget. off by default so screenshots show the real UI.
  root: point at a different checkout for this and later calls (defaults to $OPENPILOT_ROOT).
  instant_onroad: go onroad as soon as replay starts (default). False keeps the device's ~2.5s
  offroad hold and scroll animation, for testing that transition.
  Idempotent-ish: errors if a UI is already running (use restart_ui to reload code).
  """
  if root:
    SESSION.set_root(root)
  status = SESSION.start(mode=mode, show_touches=show_touches, show_fps=show_fps, instant_onroad=instant_onroad)
  out: list = [f"started: {status}"]
  if SESSION.is_running():
    out.append(_img(SESSION.screenshot()))
  return out


@mcp.tool()
def restart_ui(mode: str | None = None, show_touches: bool = False, show_fps: bool = False) -> list:
  """Stop and relaunch the UI to pick up code changes. Keeps the same mode unless given.

  A running replay, background publish and mapsd (start_nav) keep running, so the relaunched UI
  picks the drive and the map straight back up.
  """
  status = SESSION.restart(mode=mode, show_touches=show_touches, show_fps=show_fps)
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
  return [f"tapped ({x},{y}) hold={hold}", _img(SESSION.settled_screenshot())]


@mcp.tool()
def swipe(x1: int, y1: int, x2: int, y2: int, dur: float = 0.4) -> list:
  """Swipe from (x1,y1) to (x2,y2) over dur seconds (stepped so it scrolls, not jumps).

  To scroll a list up, swipe from a lower y to a higher y. Horizontal swipes move
  between screens/cards. Returns a fresh screenshot.
  """
  SESSION.swipe(x1, y1, x2, y2, dur)
  return [f"swiped ({x1},{y1})->({x2},{y2}) dur={dur}", _img(SESSION.settled_screenshot())]


@mcp.tool()
def hold(x: int, y: int, dur: float = 0.8) -> list:
  """Long-press at (x, y) for dur seconds (default 0.8). Returns a fresh screenshot."""
  SESSION.hold(x, y, dur)
  return [f"held ({x},{y}) dur={dur}", _img(SESSION.settled_screenshot())]


@mcp.tool()
def run(script: str, record: bool = False) -> list:
  """Execute a multi-step touch chain in one call and return each captured screenshot.

  record=True also screen-records the whole chain to an mp4 in real time (red dot marks each touch)
  and returns its path. Use this for a screen recording of any interactive flow, check it with clip_frames.

  Steps are separated by ';' or newlines; '#' starts a comment. Coordinates are
  screenshot pixels. Steps:
    tap X Y [HOLD]            # tap; optional hold seconds (default 0.08)
    swipe X1 Y1 X2 Y2 [DUR]   # swipe over DUR seconds (default 0.4)
    hold X Y [DUR]            # long-press (default 0.8)
    wait S                    # sleep S seconds (capture already waits for the UI to settle)
    capture [NAME]            # screenshot; NAME labels it (default = step index)

  Example: 'tap 268 120; wait 0.6; capture settings; tap 150 120; wait 0.6; capture toggles'
  """
  log, shots, video = SESSION.run_chain(script, record)
  out: list = ["\n".join(log)]
  if video is not None:
    out.append(f"recording: {video}")
  for name, pil in shots:
    out.append(f"--- {name} ---")
    out.append(_img(pil))
  return out


@mcp.tool()
def get_param(name: str) -> str:
  """Read an openpilot Param from the UI session, as JSON (null when unset)."""
  return json.dumps(SESSION.get_param(name))


@mcp.tool()
def set_param(name: str, value: bool | int | float | str | dict | list | None, restart: bool = False) -> str:
  """Write an openpilot Param (e.g. ShowDebugInfo=true for the touch/widget overlay). null removes it.

  value type must match the param type (no coercion): bool for BOOL, int for INT,
  float for FLOAT, str for STRING, dict/list for JSON (so pass true/false for a BOOL param, not 1/0).
  For an offroad alert key (e.g. Offroad_TemperatureTooHigh), true shows the alert with its standard
  text and false clears it, like openpilot's set_offroad_alert.
  The write blocks until it is on disk. Most params are read at UI start, so pass restart=True
  to relaunch the UI afterwards. Offroad alert params (Offroad_*, UpdateAvailable, ...) are
  polled by the UI every 5s, so without a restart this waits that long for them to show.
  """
  alert = SESSION.set_param(name, value)
  msg = f"set {name}" if value is not None else f"removed {name}"
  if restart and SESSION.is_running():
    SESSION.restart()
    msg += " (UI restarted)"
  elif alert and SESSION.is_running():
    time.sleep(OFFROAD_ALERT_REFRESH + 0.5)
    msg += f" (offroad alert param, waited {OFFROAD_ALERT_REFRESH + 0.5:.1f}s for the UI's alert refresh)"
  return msg


@mcp.tool()
def publish(service: str, fields: dict, hz: float = 0.0, secs: float = 0.0,
            background: bool = False) -> str:
  """Publish a cereal message so the UI sees data not in any recorded route.

  fields are keyed by dotted path under the message, e.g.
  publish('selfdriveState', {'alertText1': 'hi', 'alertStatus': 'userPrompt'}) or
  publish('carState', {'vEgo': 12.5, 'cruiseState.enabled': True}). Enums take their str
  name, numbers take plain ints/floats. Struct-root services only (not list-root ones like
  'can'). hz>0 with secs>0 repeats the send (blocking) so a SubMaster stays fresh.
  background=True returns at once and keeps sending (until secs elapses, or forever if
  secs=0), so you can screenshot mid-publish; call stop_publish to end it. One background
  publisher at a time: a new background publish replaces the previous one.
  """
  return SESSION.publish(service, fields, hz, secs, background)


@mcp.tool()
def stop_publish() -> str:
  """Stop a background publisher started with publish(background=True)."""
  return str(SESSION.stop_publish())


@mcp.tool()
def go_offroad() -> list:
  """Force the UI offroad to the home page (publishes deviceState.started=False).

  After stop_replay the UI freezes on the last onroad frame and the scroller snaps back
  to onroad, so settings can't be reached; call this to land on home. Returns a screenshot.
  """
  SESSION.go_offroad()
  out: list = ["went offroad"]
  if SESSION.is_running():
    out.append(_img(SESSION.settled_screenshot()))
  return out


@mcp.tool()
def clear_alerts() -> list:
  """Clear a sticky onroad alert left on screen after a publish (fades out _prev_alert).

  A published fullscreen alert stays up after the publisher stops because the demo route
  never sends a clearing alert. Returns a screenshot.
  """
  SESSION.clear_alerts()
  out: list = ["cleared alerts"]
  if SESSION.is_running():
    out.append(_img(SESSION.settled_screenshot()))
  return out


@mcp.tool()
def start_replay(route: str = "", dcam: bool = False, ecam: bool = False, start: int | None = None,
                 speed: float = 1.0, wait: bool = True, local: bool = True) -> list:
  """Replay a route so the onroad UI shows real data. Empty route uses --demo.

  Start the UI first; replay shares its msgq. dcam/ecam load driver/wide cameras.
  start: seconds into the route. Defaults to 90 for the demo (already engaged and driving,
  it only engages ~20s in) and 0 for other routes. speed: playback speed multiplier.
  wait: block until the onroad camera is playing (about 5s, the UI itself holds offroad
  for ~2.5s) and return a screenshot, so no screenshot polling is needed.
  local: replay from a local copy of the 3 segments from start (downloaded on first use,
  then instant), so it starts ~1s faster and works offline. Playback then covers those 3
  minutes. False streams the whole route from the server.
  For offline video or before/after comparisons use render_clip instead.
  """
  extra = []
  if dcam:
    extra.append("--dcam")
  if ecam:
    extra.append("--ecam")
  if start is None and not route:
    start = DEMO_START
  start = start or 0
  cache_dir = SESSION.cache_route(route or DEMO_ROUTE, start // 60, dcam, ecam) if local else None
  if cache_dir is not None:
    # replay counts time from the first segment it finds in the dir
    route, start = route or DEMO_ROUTE, start % 60
    extra += ["-d", str(cache_dir)]
  if start:
    extra += ["-s", str(start)]
  if speed != 1.0:
    extra += ["-x", str(speed)]
  status = SESSION.start_replay(route, extra)
  out: list = [str(status)]
  if wait and status["replay_started"]:
    if not SESSION.wait_onroad():
      out.append(f"onroad camera not playing after {REPLAY_READY_TIMEOUT}s; see {SESSION.replay_log}")
    out.append(_img(SESSION.screenshot()))
  return out


@mcp.tool()
def stop_replay() -> str:
  """Stop the running replay."""
  return str(SESSION.stop_replay())


@mcp.tool()
def start_nav(tiles_dir: str = "") -> list:
  """Show the live navigation view: run the checkout's mapsd on the session's msgq and turn navigation on.

  Use it for live replays (start_replay) of a drive that was navigating (its log has navRoute), so the
  UI shows the arrow view and the map instead of the camera. For videos use render_clip(nav=True) instead.
  Order: start_ui, start_nav, start_replay. The UI is relaunched once so it connects to mapsd fresh, and
  a replay that is already running keeps going.
  tiles_dir: a tile cache copied off a device (scp the device's /data/navigation/tiles). Defaults to
  ~/.comma/navigation/tiles when that exists. A PC has no device key for the online tile API, so
  without a cache the map stays black and the panel lists 'Map frames: no'.
  Needs a checkout with selfdrive/navigation (pass root= to start_ui for a branch worktree).
  """
  status = SESSION.start_mapsd(tiles_dir)
  SESSION.restart()
  return [f"nav: {status}", _img(SESSION.screenshot())]


@mcp.tool()
def stop_nav() -> str:
  """Stop mapsd and turn navigation off, the onroad UI goes back to the camera view."""
  SESSION.stop_mapsd()
  SESSION.set_param("NavDestination", None)
  return "mapsd stopped, navigation off (restart_ui to drop the nav view now)"


@mcp.tool()
def make_map_frames(route: str, start: float, end: float, data_dir: str = "", tiles_dir: str = "",
                    output: str = "", refresh: bool = False) -> str:
  """Build the map frames render_clip needs for the nav view, from a navigating drive's log.

  Usually not needed directly: render_clip(nav=True) calls this. Use it to build frames once and pass
  the file to several render_clip calls, or to check a drive has a route before rendering.
  Replays the route's GPS and navRoute through the checkout's own mapsd offline, one segment per core (about 7s for a 15 min drive)
  and writes an npz (t, maps) to /tmp/op_ui_mcp_maps, reused on later calls unless refresh=True.
  route: dongle/route. start/end: route seconds, same as render_clip.
  data_dir: local copy of the route (any layout render_clip takes), else rlogs download from comma.
  tiles_dir: tile cache copied off a device, defaults to ~/.comma/navigation/tiles when present.
  Fails clearly when the drive has no navRoute (it wasn't navigating) or a tile is missing from the cache.
  """
  return str(SESSION.make_map_frames(route, math.floor(start), math.ceil(end), data_dir, tiles_dir, output, refresh))


def _frame_hashes(video: Path) -> list[str]:
  res = subprocess.run([_ff("ffmpeg"), "-nostdin", "-v", "error", "-i", str(video), "-map", "0:v:0", "-fps_mode", "passthrough",
                        "-f", "framehash", "-hash", "md5", "-"], capture_output=True, text=True, check=False)
  return [line.split(",")[-1].strip() for line in res.stdout.splitlines() if line and not line.startswith("#")]


def _frame(video: Path, idx: int) -> PILImage.Image | None:
  res = subprocess.run([_ff("ffmpeg"), "-nostdin", "-v", "error", "-i", str(video), "-vf", f"select=eq(n\\,{idx})", "-fps_mode", "passthrough",
                        "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"], capture_output=True, check=False)
  return PILImage.open(io.BytesIO(res.stdout)).convert("RGB") if res.returncode == 0 and res.stdout else None


# ref | new | changed pixels in red over a dimmed new frame
def _diff_panel(a: PILImage.Image, b: PILImage.Image) -> PILImage.Image:
  from PIL import ImageChops
  mask = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 8 else 0)
  dim = PILImage.blend(b, PILImage.new("RGB", b.size), 0.6)
  hl = PILImage.composite(PILImage.new("RGB", b.size, (255, 0, 0)), dim, mask)
  gap = 4
  out = PILImage.new("RGB", (a.width * 3 + gap * 2, a.height), (255, 255, 255))
  for i, im in enumerate((a, b, hl)):
    out.paste(im, (i * (a.width + gap), 0))
  return out


# middle frame of a clip as a quick look, so the caller doesn't need to open the video
def _clip_preview(path: Path) -> Image | None:
  probe = subprocess.run([_ff("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True)
  try:
    mid = float(probe.stdout.strip()) / 2
  except ValueError:
    mid = 0.0
  res = subprocess.run([_ff("ffmpeg"), "-v", "error", "-ss", f"{mid:.2f}", "-i", str(path), "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"],
                       capture_output=True)
  return Image(data=res.stdout, format="png") if res.returncode == 0 and res.stdout else None


# frames of a video at the given seconds, optionally cropped, as one labeled grid image
def _contact_sheet(video: Path, times: list[float], labels: list[str], crop: list[int] | None, columns: int) -> Image:
  from concurrent.futures import ThreadPoolExecutor
  from PIL import ImageDraw

  def grab(t: float) -> PILImage.Image | None:
    res = subprocess.run([_ff("ffmpeg"), "-nostdin", "-v", "error", "-ss", f"{max(t, 0.0):.3f}", "-i", str(video), "-frames:v", "1",
                          "-f", "image2pipe", "-c:v", "png", "-"], capture_output=True, check=False)
    if res.returncode != 0 or not res.stdout:
      return None
    im = PILImage.open(io.BytesIO(res.stdout)).convert("RGB")
    if crop:
      x, y, w, h = crop
      im = im.crop((x, y, x + w, y + h))
    return im

  with ThreadPoolExecutor(min(16, max(1, len(times)))) as ex:
    ims = list(ex.map(grab, times))
  pairs = [(im, label) for im, label in zip(ims, labels, strict=True) if im is not None]
  if not pairs:
    raise ToolError(f"no frames at {times} in {video}")
  w, h = max(im.width for im, _ in pairs), max(im.height for im, _ in pairs)
  cols = max(1, min(columns or len(pairs), len(pairs)))
  rows = (len(pairs) + cols - 1) // cols
  gap, bar = 4, 14
  sheet = PILImage.new("RGB", (cols * w + (cols - 1) * gap, rows * (h + bar) + (rows - 1) * gap), (255, 255, 255))
  draw = ImageDraw.Draw(sheet)
  for i, (im, label) in enumerate(pairs):
    x, y = (i % cols) * (w + gap), (i // cols) * (h + bar + gap)
    draw.text((x + 2, y + 1), label, fill=(0, 0, 0))
    sheet.paste(im, (x, y + bar))
  return _img(sheet)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False))
def clip_frames(path: str, times: list[float], crop: list[int] | None = None, columns: int = 0) -> Image:
  """Frames of any video at the given seconds as one labeled grid image, to check a render without ffmpeg.

  times: seconds into the video. crop: [x, y, w, h] in video pixels, e.g. [0, 0, 180, 240] for the left third
  of a small UI clip. columns: frames per row, default all in one row."""
  video = Path(path).expanduser()
  if not video.exists():
    raise ToolError(f"no video at {video}")
  return _contact_sheet(video, times, [f"{t:.2f}s" for t in times], crop, columns)


@mcp.tool()
def render_clip(route: str = "", start: float | None = None, end: float | None = None, compare_ref: str = "",
                big: bool = False, qcam: bool = False, overlays: bool = False, output: str = "",
                speed: float = 1.0, map_frames: str = "", data_dir: str = "", root: str = "",
                frames: list[float] | None = None, crop: list[int] | None = None, columns: int = 0,
                nav: bool = False, tiles_dir: str = "", env: dict | None = None) -> list:
  """Render the onroad UI over a route to an mp4, offline, cold in about a second for 20s of mici UI.

  Independent of start_ui, no running UI needed. Empty route uses the demo route (90s-105s unless
  start/end given). start/end are seconds into the route, required with a route unless it is
  dongle/route/start/end. Renders run in parallel workers, each after a 3s pre-roll so the UI's timers
  and fades match a continuous render, with the camera decoded once (NVDEC when available).
  compare_ref: a git ref (e.g. 'master', 'HEAD~1', a sha). Renders that ref's UI code on top and the
  working tree on the bottom, stacked into one video. Only python under selfdrive/ui and system/ui is
  swapped, assets and native code come from the working tree.
  big: tici layout instead of mici. qcam: low-res road camera. speed: playback speed of the mp4 (2 = twice
  as fast, every frame kept).
  nav: render the navigation view (arrow view + map) instead of the camera view. The map frames are built
  from the drive's own log by make_map_frames (cached), so the drive must have been navigating (navRoute in
  its log). tiles_dir: tile cache for that, defaults to ~/.comma/navigation/tiles (a device's
  /data/navigation/tiles copied over). map_frames: a prebuilt npz from make_map_frames, skips building.
  data_dir: local route data: a dongle|route dir, the openpilot segment layout, or plain <route>--<n> dirs
  (start_replay's cache in /tmp/op_ui_mcp_routes, or segments copied off a device). Empty downloads from comma.
  env: extra environment for the render workers, e.g. {"MY_UI_VARIANT": "2"} to compare variants your code
  reads from os.environ, one render_clip call per variant (compare_ref covers git refs). overlays: time and route metadata text,
  rendered with the checkout's slower tools/clip/run.py. output: mp4 path, default /tmp/op_ui_mcp_clips/.
  root: openpilot checkout to render (for this and later calls), e.g. a branch worktree.
  Fractional start/end (e.g. from logreader's find_windows) widen to whole seconds.
  frames: route seconds (e.g. find_windows' t_peak) to return as one labeled grid image instead of the middle
  frame. crop: [x, y, w, h] for those frames, columns: frames per row.
  Returns the result paths and the frames.
  """
  if root:
    SESSION.set_root(root)
  start_i = math.floor(start) if start is not None else None
  end_i = math.ceil(end) if end is not None else None
  if nav and not map_frames:
    r, s_i, e_i = route, start_i, end_i
    if route.count("/") == 3:
      r, a, b = route.rsplit("/", 2)
      s_i, e_i = int(a) if s_i is None else s_i, int(b) if e_i is None else e_i
    if not r or s_i is None or e_i is None:
      raise UISessionError("nav needs a route with start and end")
    map_frames = SESSION.make_map_frames(r, s_i, e_i, data_dir, tiles_dir)["map_frames"]
  result, out = SESSION.render_clip(route, start_i, end_i, compare_ref, big, qcam, overlays, output, speed, map_frames, data_dir, env)
  content: list = [str(result)]
  if frames:
    t0, sp = result.get("start", start_i) or 0, result.get("speed", 1.0) or 1.0
    content.append(_contact_sheet(out, [(f - t0) / sp for f in frames], [f"{f:.2f}s" for f in frames], crop, columns))
  else:
    preview = _clip_preview(out)
    if preview is not None:
      content.append(preview)
  return content


@mcp.tool()
def ui_diff(ref: str = "master", big: bool = False, max_screens: int = 10) -> list:
  """UI regression review in one call: record openpilot's scripted UI tour (home, settings panels,
  keyboard, onroad, alerts; selfdrive/ui/tests/diff) with ref's UI code and with the working tree,
  compare every frame, and return only the screens that changed.

  Independent of start_ui. ref: git ref to compare against. big: tici layout instead of mici.
  Each changed run of frames comes back as one image: ref | working tree | changed pixels in red.
  Only python under selfdrive/ui and system/ui is swapped, the tour script itself always comes from
  the working tree. Full videos of both tours are kept (paths in the result).
  """
  res = SESSION.ui_diff(ref, big)
  runs = res["runs"]
  summary = {k: v for k, v in res.items() if k != "runs"}
  summary["changed_screens"] = [{"frames": f"{a}-{b}", "t": f"{a / UI_DIFF_FPS:.1f}s-{b / UI_DIFF_FPS:.1f}s"} for a, b in runs]
  if res["frames"][0] != res["frames"][1]:
    summary["note"] = "tours differ in length, frames after the first change in timing may all differ"
  out: list = [str(summary) if runs else f"no pixel changes in {res['frames'][1]} frames: {summary}"]
  ref_video, new_video = Path(res["ref_video"]), Path(res["new_video"])
  for a, b in runs[:max_screens]:
    mid = (a + b) // 2
    fa, fb = _frame(ref_video, mid), _frame(new_video, mid)
    if fa is None or fb is None:
      continue
    out.append(f"--- frames {a}-{b} ({a / UI_DIFF_FPS:.1f}s-{b / UI_DIFF_FPS:.1f}s), showing {mid} ---")
    out.append(_img(_diff_panel(fa, fb)))
  if len(runs) > max_screens:
    out.append(f"{len(runs) - max_screens} more changed runs not shown, raise max_screens")
  return out


@mcp.tool()
def logs(lines: int = 40) -> str:
  """Return the last N lines of the UI process log (stdout+stderr) for debugging."""
  return SESSION.logs(lines)


def main() -> None:
  import atexit
  import sys
  atexit.register(lambda: (SESSION.stop(quiet=True), SESSION.cleanup_prefix()))

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
