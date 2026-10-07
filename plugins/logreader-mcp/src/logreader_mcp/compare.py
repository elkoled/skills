from __future__ import annotations

from typing import Any

import numpy as np

from . import analysis, plot
from .route_cache import RouteData

# tuning-relevant defaults, missing ones are reported and skipped
DEFAULT_FIELDS = [
  "carState/vEgo", "carState/aEgo", "carState/steeringAngleDeg", "carState/steeringTorque",
  "carControl/actuators/accel", "carControl/actuators/torque", "carControl/actuators/curvature",
  "controlsState/curvature", "controlsState/desiredCurvature", "longitudinalPlan/aTarget",
]
STATS = ("mean", "std", "min", "max")


def _delta(a, b):
  if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
    return round(b - a, 4)
  return None


def _row(metric: str, a, b) -> dict[str, Any]:
  row = {"metric": metric, "a": a, "b": b}
  d = _delta(a, b)
  if d is not None:
    row["delta"] = d
  return row


def _field_stats(rd: RouteData, spec: str) -> dict[str, float] | None:
  res = plot.series(rd, spec)
  if isinstance(res, str) or not len(res[1]):
    return None
  v = res[1][np.isfinite(res[1])]
  if not len(v):
    return None
  return {"mean": float(np.mean(v)), "std": float(np.std(v)), "min": float(np.min(v)), "max": float(np.max(v)),
          "abs_mean": float(np.mean(np.abs(v)))}


def _event_counts(rd: RouteData) -> dict[str, int]:
  counts: dict[str, int] = {}
  for row in analysis.events_timeline(rd, 1 << 30)["timeline"]:
    for name in row["events"]:
      counts[name] = counts.get(name, 0) + 1
  return counts


def compare(a: RouteData, b: RouteData, fields: list[str] | None = None) -> dict[str, Any]:
  rows = []
  cp_a, cp_b = a.car_params, b.car_params
  rows.append(_row("car", cp_a.carFingerprint if cp_a else None, cp_b.carFingerprint if cp_b else None))
  rows.append(_row("duration_s", round(a.duration_s(), 2), round(b.duration_s(), 2)))

  ea, eb = analysis.engagement_summary(a), analysis.engagement_summary(b)
  for k in ("engaged_s", "n_engagements", "n_disengagements"):
    rows.append(_row(f"engagement.{k}", ea.get(k), eb.get(k)))
  if ea.get("total_s") and eb.get("total_s"):
    rows.append(_row("engagement.engaged_frac", round(ea["engaged_s"] / ea["total_s"], 3), round(eb["engaged_s"] / eb["total_s"], 3)))

  ha, hb = analysis.health_scan(a), analysis.health_scan(b)
  rows.append(_row("health.n_findings", ha["n_findings"], hb["n_findings"]))

  da, db = a.duration_s() or 1.0, b.duration_s() or 1.0
  for svc in analysis.EXPECTED_HZ:
    ca, cb = a.service_counts.get(svc), b.service_counts.get(svc)
    if ca or cb:
      rows.append(_row(f"rate_hz.{svc}", round(ca / da, 1) if ca else None, round(cb / db, 1) if cb else None))

  skipped = []
  for spec in fields or DEFAULT_FIELDS:
    sa, sb = _field_stats(a, spec), _field_stats(b, spec)
    if sa is None and sb is None:
      skipped.append(spec)
      continue
    for k in (*STATS, "abs_mean"):
      rows.append(_row(f"{spec}.{k}", round(sa[k], 4) if sa else None, round(sb[k], 4) if sb else None))

  ev_a, ev_b = _event_counts(a), _event_counts(b)
  event_rows = [{"event": e, "a": ev_a.get(e, 0), "b": ev_b.get(e, 0), "delta": ev_b.get(e, 0) - ev_a.get(e, 0)}
                for e in sorted(set(ev_a) | set(ev_b))]
  event_rows.sort(key=lambda r: -abs(r["delta"]))

  return {
    "a": a.identifier, "b": b.identifier,
    "metrics": rows,
    "event_transitions": event_rows[:40],
    "health_findings": {"a": ha["findings"], "b": hb["findings"]},
    "skipped_fields": skipped,
  }
