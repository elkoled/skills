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
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from PIL import Image as PILImage
from Xlib import X, display
from Xlib.ext import xtest

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

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
UI_DIFF_RUNNER = Path(__file__).resolve().parent / "ui_diff_runner.py"
UI_DIFF_FPS = 60
UI_DIFF_GAP = 30  # changed frames closer than this belong to one screen


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
    self._disp: display.Display | None = None
    self._root_win = None
    self.ui_log = ""
    self.replay_log = ""
    self.publish_log = ""

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
    os.makedirs(f"/dev/shm/msgq_{self.prefix}", exist_ok=True)
    home = Path.home() / f".comma{self.prefix}"
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
    res = subprocess.run([*self._python(), "-c", code], cwd=str(self.root), env=self._env(), capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
      raise UISessionError(f"setting up prefix {self.prefix} failed: {res.stderr.strip()}")
    self._prefix_ready = True

  def cleanup_prefix(self) -> None:
    shutil.rmtree(f"/dev/shm/msgq_{self.prefix}", ignore_errors=True)
    shutil.rmtree(Path.home() / f".comma{self.prefix}", ignore_errors=True)
    self._prefix_ready = False

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

  def start(self, mode: str = "small", show_touches: bool = False,
            show_fps: bool = False, extra_env: dict | None = None, instant_onroad: bool | None = None) -> dict:
    if mode not in SIZES:
      raise UISessionError(f"unknown mode {mode!r}; use one of {list(SIZES)}")
    if self.is_running():
      raise UISessionError("UI already running; call stop_ui first or use restart_ui")
    if not _is_checkout(self.root):
      raise UISessionError(f"{self.root} is not an openpilot checkout; pass root= or set OPENPILOT_ROOT")
    if not _is_built(self.root):
      raise UISessionError(f"{self.root} is not built (no msgq ipc_pyx); run `scons -j$(nproc)` there or pass root=")
    self.stop(quiet=True)
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

  def stop(self, quiet: bool = False) -> dict:
    self.stop_replay(quiet=True)
    self.stop_publish(quiet=True)
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
        shots.append((name, self.settle()))
        log.append(f"[{i}] capture -> {name}")
      else:
        raise UISessionError(f"[{i}] unknown step: {op!r}")
    return log, shots

  def set_param(self, name: str, value: bool | int | float | str) -> str:
    self.ensure_prefix()
    # put routes by the value's python type with no coercion, so pass it through as-is.
    # str(v) would break INT/FLOAT params. a bool uses put_bool to be explicit.
    # json carries the value so nan/inf survive, repr would emit bare NameError tokens
    code = (
      "import json;"
      "from openpilot.common.params import Params;"
      f"v = json.loads({json.dumps(value)!r});"
      "p = Params();"
      f"p.put_bool({name!r}, v) if isinstance(v, bool) else p.put({name!r}, v);"
      f"print('set', {name!r})"
    )
    res = subprocess.run([*self._python(), "-c", code], cwd=str(self.root),
                         env=self._env(), capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
      raise UISessionError(f"set_param failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()

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
    m = re.fullmatch(r"([0-9a-f]{16})[/|]([0-9a-f]{8}--[0-9a-f]{10}|\d{4}-\d{2}-\d{2}--\d{2}-\d{2}-\d{2})", route)
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
    "After editing UI code, call restart_ui to reload it. Use start_replay to feed a route "
    "so the onroad UI shows real driving data."
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
def run(script: str) -> list:
  """Execute a multi-step touch chain in one call and return each captured screenshot.

  Steps are separated by ';' or newlines; '#' starts a comment. Coordinates are
  screenshot pixels. Steps:
    tap X Y [HOLD]            # tap; optional hold seconds (default 0.08)
    swipe X1 Y1 X2 Y2 [DUR]   # swipe over DUR seconds (default 0.4)
    hold X Y [DUR]            # long-press (default 0.8)
    wait S                    # sleep S seconds (capture already waits for the UI to settle)
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
def set_param(name: str, value: bool | int | float | str, restart: bool = False) -> str:
  """Write an openpilot Param (e.g. ShowDebugInfo=true for the touch/widget overlay).

  value type must match the param type (no coercion): bool for BOOL, int for INT,
  float for FLOAT, str for STRING (so pass true/false for a BOOL param, not 1/0).
  Params are read at UI start, so pass restart=True to relaunch the UI afterwards.
  """
  msg = SESSION.set_param(name, value)
  if restart and SESSION.is_running():
    SESSION.stop(quiet=True)
    SESSION.start(mode=SESSION.mode)
    msg += " (UI restarted)"
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


@mcp.tool()
def render_clip(route: str = "", start: int | None = None, end: int | None = None, compare_ref: str = "",
                big: bool = False, qcam: bool = False, overlays: bool = False, output: str = "") -> list:
  """Render the onroad UI over a route to an mp4, offline and faster than realtime (tools/clip/run.py).

  Independent of start_ui, no running UI needed. Empty route uses the demo route (90s-105s unless
  start/end given). start/end are seconds into the route, required with a route unless it is
  dongle/route/start/end. An end past the last camera frame still writes the clip (reported in warnings).
  compare_ref: a git ref (e.g. 'master', 'HEAD~1', a sha). Renders that ref's UI code on top and the
  working tree on the bottom, in parallel, stacked into one video. Only python under selfdrive/ui and
  system/ui is swapped, assets and native code come from the working tree.
  big: tici layout instead of mici. qcam: low-res road camera, faster. overlays: time and route
  metadata text (off by default, UI only). output: mp4 path, default /tmp/op_ui_mcp_clips/.
  Returns the result paths and a preview of the middle frame.
  """
  result, out = SESSION.render_clip(route, start, end, compare_ref, big, qcam, overlays, output)
  content: list = [str(result)]
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
