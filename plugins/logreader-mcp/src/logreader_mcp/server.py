from __future__ import annotations

import threading
from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from . import analysis, compare, expr, panda, plot
from .route_cache import clear_cache, get_route

mcp = MCPServer(
  "logreader",
  instructions=(
    "Analyze openpilot routes: cereal fields, CAN/DBC, events, engagement, an anomaly scan and a panda "
    "safety debugger. Call load_route once, later tools reuse the parsed route from memory. Routes are cached on "
    "disk after their first load, so loading one again (also after a restart) takes milliseconds. "
    "For a derived signal or 'when does X happen' use eval_expr and find_windows instead of a custom script."
  ),
)


READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)


# ToolError so the client sees the message, mcp 2 hides other exceptions
def _rd(route: str, mode: str = "r"):
  try:
    return get_route(route, mode)
  except Exception as e:
    raise ToolError(f"could not load route {route!r}: {e}") from e


@mcp.tool(annotations=READ_ONLY)
def load_route(route: str, mode: str = "r") -> dict[str, Any]:
  """Warm-load a route and return a quick overview (car, duration, top services).

  route accepts any LogReader id: 'dongle|time', '...|time/0:3' (segment range),
  '...|time/0:3/q' (force qlogs), local rlog paths, connect.comma.ai URLs, or a local directory
  of segments (<dir>/<n>/rlog.zst, <dir>/<route>--<n>/rlog.bz2 or <dir>/<route>--<n>--rlog.bz2).

  mode: 'r' rlog, 'q' qlog, 'a' auto. Call once up front; later tools reuse the cache."""
  rd = _rd(route, mode)
  cp = rd.car_params
  svc = analysis.list_services(rd)
  # build the CAN index in the background on a first load, CAN and panda tools then usually find it ready
  if "can" in rd.service_counts and not rd.can_cached:
    threading.Thread(target=lambda: rd.can, daemon=True).start()
  return {
    "route": route,
    "car": cp.carFingerprint if cp is not None else None,
    "duration_s": svc["duration_s"],
    "n_services": svc["n_services"],
    "top_services": svc["services"][:15],
    "has_can": "can" in rd.service_counts,
    "has_sendcan": "sendcan" in rd.service_counts,
  }


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True))
def clear_route_cache() -> dict[str, Any]:
  """Drop all cached routes from memory. The disk cache (~/.cache/logreader_mcp) stays."""
  return {"cleared": clear_cache()}


@mcp.tool(annotations=READ_ONLY)
def eval_expr(route: str, expression: str, t_start: float | None = None, t_end: float | None = None,
              max_points: int = 500) -> dict[str, Any]:
  """A derived signal from a numpy expression over cereal fields, with stats (min/max and when, mean).

  Fields are service.field with dots and [i] for list elements: 'modelV2.action.desiredCurvature * carState.vEgo**2',
  'modelV2.laneLines[1].y[0]', 'selfdriveState.enabled and carState.vEgo > 20', 'rad2deg(carState.steeringAngleDeg)'.
  All fields are sampled at the fastest referenced service's times (last value at or before).
  Functions: abs sqrt sign clip minimum maximum where hypot deg2rad rad2deg sin cos tan arctan2 log exp isnan
  round float int, deriv(x) (d/dt), smooth(x, tau_s) (lowpass). 't' is the time. Enums compare as strings
  (selfdriveState.state == 'enabled'). Times are seconds since route start, the same as render_clip uses.
  Returns up to max_points samples (evenly spaced), restricted to [t_start, t_end] seconds."""
  try:
    return expr.series(_rd(route), expression, t_start, t_end, max_points)
  except expr.ExprError as e:
    raise ToolError(str(e)) from e


@mcp.tool(annotations=READ_ONLY)
def find_windows(route: str, condition: str, score: str = "", min_duration: float = 0.0, merge_gap: float = 0.5,
                 pad: float = 0.0, sort: str = "time", max_windows: int = 50) -> dict[str, Any]:
  """Time windows where a condition holds, e.g. to find moments to look at, plot or render.

  condition: boolean expression in eval_expr syntax, e.g. 'abs(modelV2.action.desiredCurvature * carState.vEgo**2) > 2',
  'carState.leftBlinker or carState.rightBlinker', "selfdriveState.state == 'overriding'".
  score: optional expression reported per window as peak (largest magnitude, signed), t_peak and mean.
  Runs closer than merge_gap seconds merge, windows shorter than min_duration drop, pad widens each side.
  sort: 'time' or 'score' (largest |peak| first). Times are seconds since route start, the same as render_clip."""
  try:
    return expr.windows(_rd(route), condition, score, min_duration, merge_gap, pad, sort, max_windows)
  except expr.ExprError as e:
    raise ToolError(str(e)) from e


@mcp.tool(annotations=READ_ONLY)
def list_services(route: str) -> dict[str, Any]:
  """List every cereal service in the route with message counts and average rate."""
  return analysis.list_services(_rd(route))


@mcp.tool(annotations=READ_ONLY)
def list_fields(route: str, service: str) -> dict[str, Any]:
  """List the scalar fields available for a cereal service (e.g. service='carState')."""
  return analysis.list_fields(_rd(route), service)


@mcp.tool(annotations=READ_ONLY)
def get_field(route: str, service: str, field: str, max_points: int = 2000,
              t_start: float | None = None, t_end: float | None = None) -> dict[str, Any]:
  """Time series of a cereal scalar field, e.g. service='carState', field='vEgo'.

  Use 'a/b/c' for nested fields. Optionally clip to [t_start, t_end] seconds."""
  return analysis.get_field(_rd(route), service, field, max_points, t_start, t_end)


@mcp.tool(annotations=READ_ONLY)
def summarize_field(route: str, service: str, field: str) -> dict[str, Any]:
  """min/max/mean/std/rate/gaps/NaNs for a cereal scalar field over the whole route."""
  return analysis.summarize_field(_rd(route), service, field)


@mcp.tool(annotations=READ_ONLY)
def route_dbcs(route: str) -> dict[str, Any]:
  """DBC file names for this car (by bus), resolved from the route's carFingerprint."""
  return analysis.route_dbcs(_rd(route))


@mcp.tool(annotations=READ_ONLY)
def can_summary(route: str, stream: str = "can", bus: int | None = None) -> dict[str, Any]:
  """Per-address CAN traffic summary (count, dlc, rate). stream='can' or 'sendcan'."""
  return analysis.can_summary(_rd(route), stream, bus)


@mcp.tool(annotations=READ_ONLY)
def decode_signal(route: str, address: int, signal: str, bus: int = 0,
                  dbc_name: str | None = None, stream: str = "can",
                  max_points: int = 2000) -> dict[str, Any]:
  """Decode a DBC signal to a time series. address is an int (e.g. 0x1d2 -> 466).

  dbc_name defaults to the car's powertrain DBC; override for radar/body buses."""
  return analysis.decode_signal(_rd(route), address, signal, bus, dbc_name, stream, max_points)


@mcp.tool(annotations=READ_ONLY)
def changing_bits(route: str, address: int, bus: int = 0, stream: str = "can") -> dict[str, Any]:
  """Find which bits of a raw CAN address ever toggle, for reverse engineering."""
  return analysis.changing_bits(_rd(route), address, bus, stream)


@mcp.tool(annotations=READ_ONLY)
def events_timeline(route: str, max_events: int = 500) -> dict[str, Any]:
  """Collapsed timeline of onroadEvents (alerts) with timestamps."""
  return analysis.events_timeline(_rd(route), max_events)


@mcp.tool(annotations=READ_ONLY)
def engagement_summary(route: str) -> dict[str, Any]:
  """Engaged time and every engage/disengage transition timestamp."""
  return analysis.engagement_summary(_rd(route))


@mcp.tool(annotations=READ_ONLY)
def health_scan(route: str) -> dict[str, Any]:
  """One-call anomaly scan: low service rates, gaps, NaNs, missing CAN, car meta."""
  return analysis.health_scan(_rd(route))


@mcp.tool(annotations=READ_ONLY)
def panda_safety_config(route: str) -> dict[str, Any]:
  """The safety model(s), param, and alternativeExperience this route ran with."""
  return panda.active_safety_config(_rd(route))


@mcp.tool(annotations=READ_ONLY)
def panda_blocked_messages(route: str, window_ms: float = 100.0) -> dict[str, Any]:
  """Hardware truth: sendcan frames whose echo never hit the bus (panda blocked them)."""
  return panda.blocked_via_echo(_rd(route), window_ms)


@mcp.tool(annotations=READ_ONLY)
def panda_replay(route: str, t_start: float | None = None, t_end: float | None = None) -> dict[str, Any]:
  """Replay sendcan through the real opendbc safety model; report which TX it blocks."""
  return panda.replay(_rd(route), t_start, t_end)


@mcp.tool(annotations=READ_ONLY)
def panda_root_cause(route: str, address: int | None = None, bus: int | None = None,
                     t_start: float | None = None, t_end: float | None = None,
                     max_groups: int = 10) -> dict[str, Any]:
  """Pin the exact safety source line(s) that block each TX address (gcov line trace).

  Filter to one address/bus and/or a time window to keep it fast and focused."""
  return panda.root_cause(_rd(route), address, bus, t_start, t_end, max_groups)


@mcp.tool(annotations=READ_ONLY)
def compare_routes(route_a: str, route_b: str, fields: list[str] | None = None, mode: str = "r") -> dict[str, Any]:
  """Same metrics on two routes side by side with b - a deltas, e.g. before and after a tune.

  Covers car, duration, engagement (time, fraction, engage/disengage counts), health findings,
  service rates, onroadEvents transition counts, and mean/std/min/max/abs_mean of fields.
  fields: 'service/field' or 'can:0xADDR:SIGNAL[@bus][#dbc]' specs (same as plot). Defaults to
  vEgo, aEgo, steering angle/torque, actuator accel/torque/curvature, curvature and aTarget.
  Both routes load in parallel."""
  from concurrent.futures import ThreadPoolExecutor
  with ThreadPoolExecutor(2) as ex:
    a, b = ex.map(lambda r: _rd(r, mode), (route_a, route_b))
  return compare.compare(a, b, fields)


@mcp.tool(annotations=READ_ONLY)
def plot_route(route: str, series: list[str], t_start: float | None = None, t_end: float | None = None,
               route_b: str | None = None, overlay: bool = False, mode: str = "r") -> list:
  """Plot fields and DBC signals to a PNG instead of returning raw arrays.

  series: 'service/field' (nested with '/', e.g. 'carState/vEgo', 'carControl/actuators/accel'),
  'can:0xADDR:SIGNAL[@bus][#dbc]' / 'sendcan:...' for a decoded DBC signal (dbc defaults to the
  car's powertrain DBC), or an eval_expr expression ('carState.vEgo * 3.6'). Enum fields plot as category indexes. One panel per series unless
  overlay=True. t_start/t_end are seconds since route start. route_b overlays the same series
  from a second route on each panel (time since its own start), for before/after comparisons."""
  rds = [(route, _rd(route, mode))] + ([(route_b, _rd(route_b, mode))] if route_b else [])
  lines, errors = [], []
  for spec in series:
    for name, rd in rds:
      res = plot.series(rd, spec)
      if isinstance(res, str):
        errors.append(f"{name}: {res}")
        continue
      label = spec if not route_b else f"{spec} ({'a' if rd is rds[0][1] else 'b'})"
      lines.append(("all" if overlay else spec, label, *res))
  if not lines:
    raise ToolError("nothing to plot: " + "; ".join(errors))
  title = route if not route_b else f"a: {route}   b: {route_b}"
  out: list = [Image(data=plot.render(lines, t_start, t_end, title), format="png")]
  if errors:
    out.append("skipped: " + "; ".join(errors))
  return out


# matplotlib's import and font setup take longer than drawing a plot, do both before the first one
def _warm_imports():
  import numpy as np
  plot.render([("x", "x", np.arange(2.0), np.arange(2.0), None)], None, None, "")


def main():
  threading.Thread(target=_warm_imports, daemon=True).start()
  mcp.run()


if __name__ == "__main__":
  main()
