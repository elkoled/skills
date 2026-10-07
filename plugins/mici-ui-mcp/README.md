# mici-ui-mcp

Drive the **openpilot UI locally** from an MCP client to build and validate UI changes
fast, with no device, no network and no shared cursor. It runs the real UI
(`selfdrive/ui/ui.py` with the checkout's `.venv` python) on a private headless Xvfb display, lets the client see
the screen (screenshots returned as images) and inject touch (tap, swipe, long-press),
and can replay a route so the onroad UI shows real driving data.

This is the desktop counterpart to the `mici` skill, which drives a physical comma four.

## Install

This plugin is published in the `elkoled-skills` marketplace:

```
/plugin marketplace add elkoled/skills
/plugin install mici-ui-mcp@elkoled-skills
```

## Requirements

- A built openpilot checkout (`uv run scons -j$(nproc)`, which builds `msgq`, `cereal`
  and `tools/replay/replay`).
- `uv` on PATH and `Xvfb` (`apt install xvfb`). Capture uses python-xlib (XGetImage) and
  touch uses XTEST, so no `xdotool` or `scrot` is needed. Only `render_clip` needs `ffmpeg`.

`run.sh` runs the server in its own small uv env (`mcp>=2.3,<3`, pillow, python-xlib),
independent of the openpilot venv, so it never syncs or rebuilds the checkout and starts
in about half a second once cached. The UI and helpers run with the checkout's
`.venv/bin/python` directly. The checkout is `$OPENPILOT_ROOT` if set, else the openpilot
checkout the client was started in, else `~/openpilot`. Pass `root=` to `start_ui` to
switch checkout at runtime. `start_ui` fails fast with a clear message if the checkout is
not built. Both the flat layout and the nested one (source under
`openpilot/`) are detected automatically, and `status` reports the resolved
`openpilot_root` and `pkg_prefix`.

## Tools

| tool | what it does |
|------|--------------|
| `start_ui(mode, show_touches, show_fps, root?)` | launch the UI (`mode` = `small` 536x240 or `big` 2160x1080), optionally pointing at another checkout via `root`. Returns a screenshot. |
| `restart_ui(mode?)` | stop and relaunch to pick up code changes |
| `stop_ui()` | tear down UI, replay and the display |
| `screenshot()` | capture the current screen as a PNG |
| `tap(x, y, hold?)` | tap at screenshot pixels; returns a fresh screenshot |
| `swipe(x1, y1, x2, y2, dur?)` | stepped swipe (scrolls, not jumps) |
| `hold(x, y, dur?)` | long-press |
| `run(script)` | run a multi-step touch chain in one call |
| `set_param(name, value, restart?)` | write an openpilot Param like `ShowDebugInfo=true`. value type matches the param (bool/int/float/str) |
| `publish(service, fields, hz?, secs?, background?)` | publish a cereal message (fields by dotted path) so the UI sees data not in a recorded route. `background=True` keeps sending so you can screenshot mid-publish |
| `stop_publish()` | stop a background publisher |
| `go_offroad()` | drop the UI to the home page (publishes `deviceState.started=False`) so settings is reachable after a replay |
| `clear_alerts()` | clear a sticky onroad alert left on screen after a publish |
| `start_replay(route, dcam?, ecam?, start?, speed?, wait?)` | replay a route (empty = demo route, from 90s where it is engaged) into the running UI. Waits until the onroad camera plays (~5s) and returns a screenshot |
| `stop_replay()` | stop replay |
| `render_clip(route?, start?, end?, compare_ref?, big?, qcam?, overlays?, output?)` | render the onroad UI over a route to an mp4 offline, faster than realtime, via `tools/clip/run.py`, always with streaming decode and a frame clock (patched in by `clip_runner.py` when the checkout lacks them). `compare_ref` renders that git ref's UI on top and the working tree below, in parallel. Returns the path and a middle-frame preview |
| `status()` / `logs(lines?)` | session state / tail the UI log |

### Coordinates

Whatever you see in a screenshot is what you pass: origin top-left, x to the right,
y down. The display is sized to the UI and scaled 1:1, so window pixels map 1:1 to UI
coordinates.

### Chain language (`run`)

Steps separated by `;` or newlines; `#` starts a comment.

```
tap X Y [HOLD]            # tap; optional hold seconds (default 0.08)
swipe X1 Y1 X2 Y2 [DUR]   # swipe over DUR seconds (default 0.4)
hold X Y [DUR]            # long-press (default 0.8)
wait S                    # sleep S seconds
capture [NAME]            # screenshot; NAME labels it
```

Example, open Settings then a toggle panel, capturing each:

```
tap 268 120; wait 0.6; capture settings; tap 150 120; wait 0.6; capture toggles
```

## Notes

- The home screen is one big button: tapping almost anywhere opens Settings.
- `show_touches=True` draws a red dot and trail where touches land, plus red debug outlines
  on every widget. Off by default so screenshots match the real UI.
- If `start_ui` reports `rendered: false`, call `logs`. The UI likely failed to import a
  compiled module (rebuild with `scons`) or `Xvfb` is missing.
- `restart_ui` reloads Python only. A change to C++, Cython or a param key (like a new key
  in `common/params_keys.h`) needs a `scons` rebuild from the repo root first.
- One UI runs at a time per server process, and is killed on server exit.
