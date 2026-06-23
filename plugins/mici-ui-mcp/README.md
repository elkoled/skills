# mici-ui-mcp

Drive the **openpilot UI locally** from an MCP client to build and validate UI changes
fast, with no device, no network and no shared cursor. It runs the real UI
(`uv run selfdrive/ui/ui.py`) on a private headless Xvfb display, lets the client see
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
  touch uses XTEST, so no `xdotool`, `scrot` or `ffmpeg` is needed.

`run.sh` launches the server inside the openpilot venv via
`uv run --project "$OPENPILOT_ROOT" --with "mcp,pillow,python-xlib"`, so the MCP
dependencies are pulled in on first run. `OPENPILOT_ROOT` defaults to `~/openpilot`. Set
it in the environment if your checkout lives elsewhere, or pass `root=` to `start_ui` to
switch checkout at runtime. Both the flat layout and the nested one (source under
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
| `publish(service, fields, hz?, secs?)` | publish a cereal message (fields by dotted path) so the UI sees data not in a recorded route |
| `start_replay(route, dcam?, ecam?)` | replay a route (empty = demo route) into the running UI |
| `stop_replay()` | stop replay |
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
- `show_touches=True` (default) draws a red dot and trail where touches land.
- If `start_ui` reports `rendered: false`, call `logs`. The UI likely failed to import a
  compiled module (rebuild with `scons`) or `Xvfb` is missing.
- `restart_ui` reloads Python only. A change to C++, Cython or a param key (like a new key
  in `common/params_keys.h`) needs a `scons` rebuild from the repo root first.
- One UI runs at a time per server process, and is killed on server exit.
