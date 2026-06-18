from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import analysis, panda
from .route_cache import clear_cache, get_route

mcp = FastMCP("logreader")


def _rd(route: str, mode: str = "r"):
  return get_route(route, mode)


@mcp.tool()
def load_route(route: str, mode: str = "r") -> dict[str, Any]:
  """Warm-load a route and return a quick overview (car, duration, top services).

  route accepts any LogReader id: 'dongle|time', '...|time/0:3' (segment range),
  '...|time/0:3/q' (force qlogs), local rlog paths, or connect.comma.ai URLs.

  mode: 'r' rlog, 'q' qlog, 'a' auto. Call once up front; later tools reuse the cache."""
  rd = _rd(route, mode)
  cp = rd.car_params
  svc = analysis.list_services(rd)
  return {
    "route": route,
    "car": cp.carFingerprint if cp is not None else None,
    "duration_s": svc["duration_s"],
    "n_services": svc["n_services"],
    "top_services": svc["services"][:15],
    "has_can": "can" in rd.service_counts,
    "has_sendcan": "sendcan" in rd.service_counts,
  }


@mcp.tool()
def clear_route_cache() -> dict[str, Any]:
  """Drop all cached routes from memory."""
  return {"cleared": clear_cache()}


@mcp.tool()
def list_services(route: str) -> dict[str, Any]:
  """List every cereal service in the route with message counts and average rate."""
  return analysis.list_services(_rd(route))


@mcp.tool()
def list_fields(route: str, service: str) -> dict[str, Any]:
  """List the scalar fields available for a cereal service (e.g. service='carState')."""
  return analysis.list_fields(_rd(route), service)


@mcp.tool()
def get_field(route: str, service: str, field: str, max_points: int = 2000,
              t_start: float | None = None, t_end: float | None = None) -> dict[str, Any]:
  """Time series of a cereal scalar field, e.g. service='carState', field='vEgo'.

  Use 'a/b/c' for nested fields. Optionally clip to [t_start, t_end] seconds."""
  return analysis.get_field(_rd(route), service, field, max_points, t_start, t_end)


@mcp.tool()
def summarize_field(route: str, service: str, field: str) -> dict[str, Any]:
  """min/max/mean/std/rate/gaps/NaNs for a cereal scalar field over the whole route."""
  return analysis.summarize_field(_rd(route), service, field)


@mcp.tool()
def route_dbcs(route: str) -> dict[str, Any]:
  """DBC file names for this car (by bus), resolved from the route's carFingerprint."""
  return analysis.route_dbcs(_rd(route))


@mcp.tool()
def can_summary(route: str, stream: str = "can", bus: int | None = None) -> dict[str, Any]:
  """Per-address CAN traffic summary (count, dlc, rate). stream='can' or 'sendcan'."""
  return analysis.can_summary(_rd(route), stream, bus)


@mcp.tool()
def decode_signal(route: str, address: int, signal: str, bus: int = 0,
                  dbc_name: str | None = None, stream: str = "can",
                  max_points: int = 2000) -> dict[str, Any]:
  """Decode a DBC signal to a time series. address is an int (e.g. 0x1d2 -> 466).

  dbc_name defaults to the car's powertrain DBC; override for radar/body buses."""
  return analysis.decode_signal(_rd(route), address, signal, bus, dbc_name, stream, max_points)


@mcp.tool()
def changing_bits(route: str, address: int, bus: int = 0, stream: str = "can") -> dict[str, Any]:
  """Find which bits of a raw CAN address ever toggle, for reverse engineering."""
  return analysis.changing_bits(_rd(route), address, bus, stream)


@mcp.tool()
def events_timeline(route: str, max_events: int = 500) -> dict[str, Any]:
  """Collapsed timeline of onroadEvents (alerts) with timestamps."""
  return analysis.events_timeline(_rd(route), max_events)


@mcp.tool()
def engagement_summary(route: str) -> dict[str, Any]:
  """Engaged time and every engage/disengage transition timestamp."""
  return analysis.engagement_summary(_rd(route))


@mcp.tool()
def health_scan(route: str) -> dict[str, Any]:
  """One-call anomaly scan: low service rates, gaps, NaNs, missing CAN, car meta."""
  return analysis.health_scan(_rd(route))


@mcp.tool()
def panda_safety_config(route: str) -> dict[str, Any]:
  """The safety model(s), param, and alternativeExperience this route ran with."""
  return panda.active_safety_config(_rd(route))


@mcp.tool()
def panda_blocked_messages(route: str, window_ms: float = 100.0) -> dict[str, Any]:
  """Hardware truth: sendcan frames whose echo never hit the bus (panda blocked them)."""
  return panda.blocked_via_echo(_rd(route), window_ms)


@mcp.tool()
def panda_replay(route: str, t_start: float | None = None, t_end: float | None = None) -> dict[str, Any]:
  """Replay sendcan through the real opendbc safety model; report which TX it blocks."""
  return panda.replay(_rd(route), t_start, t_end)


@mcp.tool()
def panda_root_cause(route: str, address: int | None = None, bus: int | None = None,
                     t_start: float | None = None, t_end: float | None = None,
                     max_groups: int = 10) -> dict[str, Any]:
  """Pin the exact safety source line(s) that block each TX address (gcov line trace).

  Filter to one address/bus and/or a time window to keep it fast and focused."""
  return panda.root_cause(_rd(route), address, bus, t_start, t_end, max_groups)


def main():
  mcp.run()


if __name__ == "__main__":
  main()
