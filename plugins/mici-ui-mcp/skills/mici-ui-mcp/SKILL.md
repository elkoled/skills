---
name: mici-ui-mcp
description: >-
  Build and validate the openpilot UI locally without a device. Use whenever a task
  touches the openpilot offroad/onroad UI on a PC: launch the UI, see what is on screen,
  inject touch (tap, swipe, long-press) to test a UI change or drive settings/onboarding,
  and replay a route so the onroad UI shows real driving data. Cues like "run the
  openpilot UI", "screenshot the UI", "tap the toggles card", "test my UI change",
  "what does the settings screen look like", "show the onroad UI for this route". This
  drives a desktop UI on a private headless display, not a physical comma device (for a
  wired comma four use the mici skill instead).
---

# mici-ui-mcp - drive the openpilot UI on a PC

This plugin ships an MCP server (`mici-ui-mcp`) that runs the real UI
(`uv run selfdrive/ui/ui.py`) on a private headless Xvfb display, captures the screen,
and injects touch. Prefer these MCP tools over launching the UI by hand: they return a
screenshot in the tool result, so you see the effect of each action immediately.

## Requirements

- A built openpilot checkout (`uv run scons -j$(nproc)` builds `msgq`, `cereal` and
  `tools/replay/replay`). Without the build the UI will not import its native modules.
- `Xvfb` on PATH (`apt install xvfb`). Capture and touch need no other system tools.
- The server runs against `$OPENPILOT_ROOT` (defaults to `~/openpilot`), or pass `root=`
  to `start_ui` to switch checkout at runtime. It detects both the flat layout and the
  nested one (source under `openpilot/`), and `status` reports the resolved
  `openpilot_root` and `pkg_prefix`.

## Workflow

1. `start_ui` to launch the UI. It returns a screenshot. Use `mode="small"` (536x240,
   comma four layout) or `mode="big"` (2160x1080, tici layout).
2. Read the screenshot, find the target, then `tap`/`swipe`/`hold` at those pixel
   coordinates. Each returns a fresh screenshot to confirm the result.
3. For a multi-step flow use `run` with a chain script (one call, all captures returned).
4. After editing UI code, `restart_ui` to reload it.
5. `start_replay` (empty route uses the demo route) to drive the onroad UI with real
   data; `stop_replay` when done. `stop_ui` frees everything.

## Coordinates

Coordinates are the pixels you see in a screenshot: origin top-left, x to the right,
y down. Small UI is 536x240, big UI is 2160x1080. The display is sized to the UI and
scaled 1:1, so a point in the screenshot is the point you pass.

## Behavior to know

- The home screen is one big button: tapping almost anywhere opens Settings.
- `show_touches` (on by default) draws a red dot and trail where touches land. Use it to
  confirm a tap hit its target.
- A horizontal swipe drives whichever scroller is in focus. Inside a panel it scrolls
  that panel; on the top level it moves between screens.
- If a tool reports the UI is not running or did not render, call `logs` to see the UI
  stdout/stderr (a missing `scons` build or a missing `Xvfb` are the usual causes).
- `restart_ui` reloads Python only. A change to C++, Cython or a param key (like a new key
  in `common/params_keys.h`) needs a `scons` rebuild from the repo root first, or the UI
  imports a stale module and `Params().put_bool(<newkey>)` raises `UnknownKeyName`.

## Tools

`start_ui`, `restart_ui`, `stop_ui`, `status`, `screenshot`, `tap`, `swipe`, `hold`,
`run`, `set_param`, `publish`, `start_replay`, `stop_replay`, `logs`. Each tool documents
its own arguments; read the tool descriptions for details.
