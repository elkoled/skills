# logreader-mcp

An MCP server that takes an **openpilot route** as input and analyzes it fast:
cereal messages, CAN/DBC decoding, events & engagement, an anomaly scan, and a
**panda safety debugger** that finds the exact source line and root cause of
every blocked TX message.

Routes are resolved through openpilot's own `LogReader`, so anything it accepts
works: comma-connect route names (using your `~/.comma/auth.json` token),
`connect.comma.ai` share URLs, `commaCarSegments` segments, and local rlog paths.

## Tools

| Tool | What it does |
|------|--------------|
| `load_route` | Warm-load a route, return car + duration + top services |
| `list_services` / `list_fields` | Enumerate cereal services and their fields. List-type services (`pandaStates`, `onroadEvents`) read their first element, prefix an index for others (`1/ignitionLine`) |
| `get_field` / `summarize_field` | Time series / stats for any cereal scalar (e.g. `carState.vEgo`) |
| `route_dbcs` | DBC names for the car, by bus |
| `can_summary` | Per-address CAN traffic (count, dlc, rate) for `can` or `sendcan` |
| `decode_signal` | Decode a DBC signal to a time series |
| `changing_bits` | Which bits of a raw address toggle (reverse engineering) |
| `events_timeline` / `engagement_summary` | Alerts timeline and engage/disengage transitions |
| `health_scan` | One call: low rates, gaps, NaNs, missing CAN, car metadata. Expected rates come from cereal's service list, divided by the qlog decimation for qlogs |
| `compare_routes` | Same metrics on two routes side by side with deltas (engagement, health, rates, event counts, field stats), e.g. before/after a tune |
| `plot_route` | PNG of cereal fields (`carState/vEgo`) and DBC signals (`can:0x415:VehYawNonLin_W_Rq@0`), one panel each or overlaid, optionally with a second route overlaid |
| `panda_safety_config` | Safety model(s) + param + alternativeExperience the route ran with |
| `panda_blocked_messages` | **Hardware truth**: sendcan frames whose echo never hit the bus |
| `panda_replay` | Replay sendcan through the real opendbc safety model; report blocked TX |
| `panda_root_cause` | **Exact safety source line(s)** that block each TX (gcov line trace) |

### How the panda debugger works
- `panda_blocked_messages` diffs `sendcan` (what openpilot tried to send) against
  `can` (what reached the bus, since panda echoes accepted TX). No-echo means blocked.
- `panda_replay` rebuilds safety state from the RX stream and runs each TX frame
  through the **actual** opendbc safety model (`libsafety`), the same C that runs
  on the panda, to get an authoritative blocked/allowed verdict.
- `panda_root_cause` compiles a gcov-instrumented copy of the safety model and,
  for a blocked frame, records exactly which source lines executed, diffing
  against an allowed baseline to isolate the failing check, e.g.
  `toyota.h:332 steer_torque_cmd_checks(...)` then `toyota.h:333 tx = false;`.

## Requirements
- An **openpilot checkout** that has been built (compiled `cereal`, `opendbc`).
  The server imports those native extensions; it does not vendor them.
- `gcc`/`cc` available (used by `libsafety` and the gcov root-cause build).
- `uv`.

## Install (Claude Code plugin)

```
/plugin marketplace add elkoled/skills
/plugin install logreader-mcp@elkoled-skills
```

That registers an MCP server named `logreader` whose command is `run.sh`. The
wrapper runs the server on the checkout's own `.venv` python, so the compiled
`cereal`/`opendbc` extensions resolve, with `mcp` layered on top. It never syncs
the checkout's venv or touches its `uv.lock`:

```bash
ROOT="${OPENPILOT_ROOT:-$HOME/openpilot}"
exec uv run --quiet --no-project --python "$ROOT/.venv/bin/python" --with "mcp>=2.3,<3" python "$DIR/run_server.py"
```

Both checkout layouts work (source at the repo root, or nested under `openpilot/`).

## Speed
- `load_route` scans all segments in parallel worker processes and keeps only each
  message's service, time and byte offset. Messages of a service are built only when
  a tool asks for that service, fields are read straight off them.
- The CAN index is built in parallel as numpy arrays over one data buffer, warmed in
  the background after `load_route`.
- `panda_replay` runs the whole frame array through an `-O2` libsafety in one C loop.
  `panda_root_cause` uses the same loop on the gcov build between the TX frames it
  samples, and only dumps and parses coverage when that frame's verdict is still needed.
- Everything is sorted by time, segments are not assumed to arrive in order.

On a 16 segment route: `load_route` ~1.5s, `health_scan` <1s, CAN index ~2s,
`panda_blocked_messages` <0.1s, `panda_replay` ~1s, `panda_root_cause` ~2s for the whole
route (was ~60s, 10s, n/a, 150s, broken, broken).

If your openpilot checkout is not at `~/openpilot`, set `OPENPILOT_ROOT` in your
environment before launching Claude Code.

## Auth for comma-connect routes
The server uses the token in `~/.comma/auth.json` (the same one `tools/lib/auth.py`
writes). Local rlog paths and public segments need no auth.

## Notes
- The first query on a route pays the download/decompress cost; subsequent
  queries reuse the in-memory cache. Use `clear_route_cache` to free memory.
- Pass a segment range to keep things fast, e.g. `...--13-01-19/0:3`.
