---
name: mici-ui-mcp
description: >-
  Build and validate the openpilot UI locally without a device. Use whenever a task
  touches the openpilot offroad/onroad UI on a PC: launch the UI, see what is on screen,
  inject touch (tap, swipe, long-press) to test a UI change or drive settings/onboarding,
  and replay a route so the onroad UI shows real driving data. Cues like "run the
  openpilot UI", "screenshot the UI", "tap the toggles card", "test my UI change",
  "what does the settings screen look like", "show the onroad UI for this route",
  "render this drive", "show the nav / arrow / map view", "compare UI variants". This
  drives a desktop UI on a private headless display, not a physical comma device (for a
  wired comma four use the mici skill instead).
---

# mici-ui-mcp - drive the openpilot UI on a PC

This plugin ships an MCP server (`mici-ui-mcp`) that runs the real UI
(`selfdrive/ui/ui.py` with the checkout's `.venv` python) on a private headless Xvfb display, captures the screen,
and injects touch. Prefer these MCP tools over launching the UI by hand: they return a
screenshot in the tool result, so you see the effect of each action immediately.

## Requirements

- A built openpilot checkout (`uv run scons -j$(nproc)` builds `msgq`, `cereal` and
  `tools/replay/replay`). Without the build the UI will not import its native modules.
- `Xvfb` on PATH (`apt install xvfb`). Capture and touch need no other system tools.
- The server runs against `$OPENPILOT_ROOT`, else the checkout the session started in,
  else `~/openpilot`. Or pass `root=`
  to `start_ui` to switch checkout at runtime. It detects both the flat layout and the
  nested one (source under `openpilot/`), and `status` reports the resolved
  `openpilot_root` and `pkg_prefix`.

## Workflow

1. `start_ui` to launch the UI. It returns a screenshot. Use `mode="small"` (536x240,
   comma four layout) or `mode="big"` (2160x1080, tici layout).
2. Read the screenshot, find the target, then `tap`/`swipe`/`hold` at those pixel
   coordinates. Each returns a fresh screenshot to confirm the result.
3. For a multi-step flow use `run` with a chain script (one call, all captures returned).
4. After editing UI code, `restart_ui` to reload it. A running replay, background publish and
   `start_nav` keep running across the restart.
5. `start_replay` (empty route uses the demo route from 90s, already engaged) to drive the onroad UI with real
   data; `stop_replay` when done. `stop_ui` frees everything.

`start_replay` blocks until the onroad camera is playing (about 2s) and returns a screenshot,
so don't poll with `screenshot`. By default it replays a local copy of the 3 segments from
`start` (downloaded to `/tmp/op_ui_mcp_routes` on first use), which skips the route API lookup
and starts in ~1.2s. `local=False` streams the whole route from the server instead. The UI is launched with instant onroad: it skips the device's
~2.5s offroad hold and scroll into onroad. Pass `instant_onroad=False` to `start_ui` when
testing that transition itself. Pass `start`
seconds to jump into a route and `speed` to play faster.

## Videos and before/after comparisons

For a video of the onroad UI, use `render_clip` instead of screenshotting a live replay. It
renders offline with the plugin's `fast_clip.py` (no `start_ui` needed): parallel workers that each
render a slice after a 3s pre-roll, the camera decoded once (NVDEC when there is an NVIDIA GPU), log
messages parsed only when the UI reads them. 20s of mici UI renders cold in about 1.0-1.3s, the
output matching a continuous render. `speed=2` makes a 2x video (every frame kept).
`nav=True` renders the navigation view, see below. `data_dir` renders a local copy of a route.
`root` renders another checkout (e.g. a branch worktree). `env={...}` sets environment variables in
the render workers.
`frames=[...]` (route seconds) returns those frames as one labeled grid instead of the middle frame,
`crop=[x, y, w, h]` zooms into a region. To check a moment, find it with logreader's `find_windows`
(e.g. a high steering effort curve, a lane change, an override), then pass its start/end and
`t_peak` straight into `render_clip`. `clip_frames(path, times, crop)` does the same for any video.
`compare_ref="master"` (or any ref) renders that ref's UI python on top and the working
tree below in one stacked mp4. Empty route is the demo route (90s-105s), pass `start`/`end`
seconds for another range. `overlays=True` adds the time and route text and renders with the
checkout's slower `tools/clip/run.py`.

## Navigation view (arrow view + map)

The nav view shows when navigation is on and `mapsd` sends map frames. The tools set all of that up
from the drive's own log, so the drive must have been navigating (its log has `navRoute`).

- Video: `render_clip(route, start, end, nav=True)`. It builds the map frames with
  `make_map_frames` (cached in `/tmp/op_ui_mcp_maps`, about 25s per 5 min of drive the first time)
  and renders. To render the same window several times, call `make_map_frames` once and pass its
  file as `map_frames=`.
- Live: `start_ui`, then `start_nav`, then `start_replay(route)`. `start_nav` runs the checkout's
  `mapsd` on the session's msgq and turns navigation on. `stop_nav` turns it off.
- Map tiles: a PC has no device key for the online tile API, so maps come from a tile cache copied
  off a device. Copy it once, then every nav call finds it by default:
  `scp -r comma@<device>:/data/navigation/tiles ~/.comma/navigation/` (or pass `tiles_dir=`).
  A cache only holds the area that device has driven. A missing tile fails with a clear error.
- Needs a checkout with `selfdrive/navigation` (pass `root=` for a branch worktree), built with
  `scons` (the nav params are compiled in, a stale build raises `UnknownKeyName: NavigationEnabled`).

## Local route copies

`data_dir` takes any of these, no renaming needed: a `dongle|route` directory, segment dirs named
`dongle|route--N`, plain `route--N` dirs (what you get from `scp` off a device's
`/data/media/0/realdata`, and what `start_replay` caches in `/tmp/op_ui_mcp_routes/<dongle>_<route>`).
Without `data_dir`, `render_clip` downloads the route from comma, which only works for uploaded drives.
When copying segments off a device, copy each segment's `rlog.zst` plus `qcamera.ts` (with
`qcam=True`) or `fcamera.hevc`, and check every segment arrived: a missing rlog fails the render.

## Comparing UI variants

- Two git refs: `render_clip(..., compare_ref="HEAD~1")` stacks that ref above the working tree.
- More variants in the working tree: have your code read a variable such as
  `os.getenv("ARROW_STYLE", "1")`, then call `render_clip(..., env={"ARROW_STYLE": "2"}, output=...)`
  once per variant. Use the same `frames=[...]` on each call to compare the same moments side by side.
- Find the moments first with logreader's `find_windows` (turns: `abs(modelV2.action.desiredCurvature) > 0.02`,
  lane changes: `modelV2.meta.laneChangeState == 'laneChangeStarting'`).

## Don't write scripts for these

Every step of the loop has a tool. Reach for it instead of a hand-written script:

| need | tool |
|------|------|
| nav view in a video | `render_clip(nav=True)` |
| map frames npz | `make_map_frames` |
| live map during a replay | `start_nav` |
| a local copy with plain `route--N` dirs | `render_clip(data_dir=...)` as is |
| switch between code variants | `render_clip(env={...})` |
| see frames of a render | `render_clip(frames=...)` or `clip_frames` |
| find turns, lane changes, overrides | logreader `find_windows` |

## UI regression review

`ui_diff(ref="master")` answers "what did my change do to the UI" in one call (~10s). It records
openpilot's scripted UI tour (home, settings panels, keyboard, onroad, alerts; frame-based clock,
lossless) with the ref's UI python and with the working tree, compares every frame and returns
one ref | new | changed-pixels image per run of changed frames. No `start_ui` needed. Only what
the tour exercises is covered, onroad without model data or longitudinal car params.

## Coordinates

Coordinates are the pixels you see in a screenshot: origin top-left, x to the right,
y down. Small UI is 536x240, big UI is 2160x1080. The display is sized to the UI and
scaled 1:1, so a point in the screenshot is the point you pass.

## Behavior to know

- `set_param` blocks until the write is on disk. Offroad alerts: `set_param("Offroad_TemperatureTooHigh", true)`
  shows the alert with its standard text, `false` clears it. The UI polls alert params every 5s,
  so these calls wait that long before returning (`restart=True` is faster if UI state doesn't matter).

- Each server process runs the UI, replay and publishers under its own `OPENPILOT_PREFIX`
  (`status` shows it), so concurrent sessions don't collide on msgq. Params start as a copy of
  the default `~/.comma/params` and are removed when the server exits.

- The home screen is one big button: tapping almost anywhere opens Settings.
- `show_touches=True` draws a red dot and trail where touches land, plus red debug outlines
  on every widget. Off by default so screenshots show the real UI. Turn it on to confirm a
  tap hit its target.
- Touch tools return once the screen stops changing (capped at 0.5s), so no `wait` is
  needed before a screenshot or `capture`.
- A horizontal swipe drives whichever scroller is in focus. Inside a panel it scrolls
  that panel; on the top level it moves between screens.
- If a tool reports the UI is not running or did not render, call `logs` to see the UI
  stdout/stderr (a missing `scons` build or a missing `Xvfb` are the usual causes).
- `restart_ui` reloads Python only. A change to C++, Cython or a param key (like a new key
  in `common/params_keys.h`) needs a `scons` rebuild from the repo root first, or the UI
  imports a stale module and `Params().put_bool(<newkey>)` raises `UnknownKeyName`.
- `stop_replay` leaves the UI frozen on the last onroad frame, so settings stays
  unreachable. Call `go_offroad` to land on the home page. A published alert is sticky
  (it stays after the publisher stops), so `clear_alerts` to remove it.
- To catch a transient onroad state (a forced alert, a set-speed flash), use
  `publish(..., background=True)` and `screenshot` while it keeps sending, then
  `stop_publish`. Reaching a specific settings panel is still manual swiping.

## Tools

`start_ui`, `restart_ui`, `stop_ui`, `status`, `screenshot`, `tap`, `swipe`, `hold`,
`run`, `set_param`, `publish`, `stop_publish`, `go_offroad`, `clear_alerts`,
`start_replay`, `stop_replay`, `start_nav`, `stop_nav`, `render_clip`, `make_map_frames`,
`clip_frames`, `ui_diff`, `logs`. Each tool documents its own arguments; read the
tool descriptions for details.
