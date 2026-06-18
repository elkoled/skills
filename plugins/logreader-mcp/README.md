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
| `list_services` / `list_fields` | Enumerate cereal services and their fields |
| `get_field` / `summarize_field` | Time series / stats for any cereal scalar (e.g. `carState.vEgo`) |
| `route_dbcs` | DBC names for the car, by bus |
| `can_summary` | Per-address CAN traffic (count, dlc, rate) for `can` or `sendcan` |
| `decode_signal` | Decode a DBC signal to a time series |
| `changing_bits` | Which bits of a raw address toggle (reverse engineering) |
| `events_timeline` / `engagement_summary` | Alerts timeline and engage/disengage transitions |
| `health_scan` | One call: low rates, gaps, NaNs, missing CAN, car metadata |
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
wrapper launches the server inside openpilot's uv environment so the compiled
`cereal`/`opendbc`/`libsafety` extensions resolve:

```bash
ROOT="${OPENPILOT_ROOT:-$HOME/openpilot}"
exec uv run --project "$ROOT" --with "mcp,numpy" python "$DIR/run_server.py"
```

If your openpilot checkout is not at `~/openpilot`, set `OPENPILOT_ROOT` in your
environment before launching Claude Code.

## Auth for comma-connect routes
The server uses the token in `~/.comma/auth.json` (the same one `tools/lib/auth.py`
writes). Local rlog paths and public segments need no auth.

## Notes
- The first query on a route pays the download/decompress cost; subsequent
  queries reuse the in-memory cache. Use `clear_route_cache` to free memory.
- Pass a segment range to keep things fast, e.g. `...--13-01-19/0:3`.
