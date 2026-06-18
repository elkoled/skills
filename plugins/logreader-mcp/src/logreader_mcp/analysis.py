from __future__ import annotations

import math
from typing import Any

import numpy as np

from .route_cache import RouteData


def _downsample(t: np.ndarray, v: np.ndarray, max_points: int):
  n = len(t)
  max_points = max(1, max_points)
  if n <= max_points:
    idx = np.arange(n)
  else:
    idx = np.linspace(0, n - 1, max_points).astype(int)
  return t[idx], v[idx]


def _to_list(a):
  out = []
  for x in a:
    if isinstance(x, (np.floating, float)):
      out.append(None if math.isnan(float(x)) else float(x))
    elif isinstance(x, (np.integer, int)):
      out.append(int(x))
    elif isinstance(x, (np.bool_, bool)):
      out.append(bool(x))
    else:
      out.append(x.tolist() if isinstance(x, np.ndarray) else x)
  return out


def list_services(rd: RouteData) -> dict[str, Any]:
  counts = rd.service_counts
  dur = rd.duration_s()
  rows = []
  for svc, c in sorted(counts.items(), key=lambda kv: -kv[1]):
    rows.append({"service": svc, "count": c, "avg_hz": round(c / dur, 2) if dur else None})
  return {"route": rd.identifier, "duration_s": round(dur, 2), "n_services": len(rows), "services": rows}


def list_fields(rd: RouteData, service: str) -> dict[str, Any]:
  ts = rd.time_series
  if service not in ts:
    return {"error": f"service '{service}' not in route", "available": sorted(ts.keys())[:60]}
  fields = [k for k in ts[service].keys() if k != "t"]
  return {"service": service, "n_samples": len(ts[service]["t"]), "fields": sorted(fields)}


def _scalar_series(rd: RouteData, service: str, field: str,
                   t_start: float | None = None, t_end: float | None = None):
  ts = rd.time_series
  if service not in ts:
    return None, None, {"error": f"service '{service}' not found", "available": sorted(ts.keys())[:60]}
  group = ts[service]
  if field not in group:
    return None, None, {"error": f"field '{field}' not in '{service}'",
                        "available": sorted(k for k in group if k != "t")}
  t = np.asarray(group["t"], dtype=float)
  v = np.asarray(group[field])
  if t_start is not None:
    m = t >= t_start
    t, v = t[m], v[m]
  if t_end is not None:
    m = t <= t_end
    t, v = t[m], v[m]
  if v.ndim > 1 or v.dtype == object:
    shape = v.shape if v.ndim > 1 else "ragged/non-numeric"
    return None, None, {"error": f"field '{field}' is non-scalar ({shape}); pick a scalar field"}
  return t, v, None


def get_field(rd: RouteData, service: str, field: str, max_points: int = 2000,
              t_start: float | None = None, t_end: float | None = None) -> dict[str, Any]:
  t, v, err = _scalar_series(rd, service, field, t_start, t_end)
  if err:
    return err
  td, vd = _downsample(t, v, max_points)
  return {"service": service, "field": field, "n": int(len(t)),
          "returned": int(len(td)), "t": _to_list(td), "v": _to_list(vd)}


def summarize_field(rd: RouteData, service: str, field: str) -> dict[str, Any]:
  t, v, err = _scalar_series(rd, service, field)
  if err:
    return err
  v = v.astype(float)
  finite = v[np.isfinite(v)]
  dt = np.diff(t)
  out = {
    "service": service, "field": field, "n": int(len(v)),
    "n_nan": int(np.sum(~np.isfinite(v))),
    "min": float(np.min(finite)) if len(finite) else None,
    "max": float(np.max(finite)) if len(finite) else None,
    "mean": float(np.mean(finite)) if len(finite) else None,
    "std": float(np.std(finite)) if len(finite) else None,
    "first": float(v[0]) if len(v) else None,
    "last": float(v[-1]) if len(v) else None,
    "avg_hz": round(1.0 / float(np.mean(dt)), 2) if len(dt) and np.mean(dt) > 0 else None,
    "max_gap_s": float(np.max(dt)) if len(dt) else None,
  }
  return out


def _platform(rd: RouteData):
  from opendbc.car.values import PLATFORMS
  cp = rd.car_params
  if cp is None:
    return None, None
  fp = cp.carFingerprint
  return fp, PLATFORMS.get(fp)


def route_dbcs(rd: RouteData) -> dict[str, Any]:
  fp, plat = _platform(rd)
  if plat is None:
    return {"carFingerprint": fp, "error": "platform not found in opendbc PLATFORMS"}
  dbc = {str(k): v for k, v in plat.config.dbc_dict.items()}
  return {"carFingerprint": fp, "dbc": dbc}


def can_summary(rd: RouteData, stream: str = "can", bus: int | None = None) -> dict[str, Any]:
  idx = rd.can if stream == "can" else rd.sendcan
  if len(idx) == 0:
    return {"stream": stream, "n_frames": 0, "addresses": []}
  agg: dict[tuple[int, int], dict[str, Any]] = {}
  seen_t0 = seen_t1 = None
  for t, a, b, d in zip(idx.t_ns, idx.addr, idx.bus, idx.dat):
    if bus is not None and b != bus:
      continue
    seen_t0 = t if seen_t0 is None else seen_t0
    seen_t1 = t
    k = (b, a)
    e = agg.get(k)
    if e is None:
      e = agg[k] = {"bus": b, "address": a, "address_hex": hex(a), "count": 0, "dlc": len(d)}
    e["count"] += 1
    e["dlc"] = max(e["dlc"], len(d))
  dur = ((seen_t1 - seen_t0) / 1e9) if seen_t0 is not None and seen_t1 > seen_t0 else 1.0
  rows = []
  for e in agg.values():
    e["avg_hz"] = round(e["count"] / dur, 2)
    rows.append(e)
  rows.sort(key=lambda r: (r["bus"], r["address"]))
  return {"stream": stream, "n_frames": len(idx), "duration_s": round(dur, 2),
          "n_addresses": len(rows), "addresses": rows}


def decode_signal(rd: RouteData, address: int, signal: str, bus: int = 0,
                  dbc_name: str | None = None, stream: str = "can",
                  max_points: int = 2000) -> dict[str, Any]:
  from opendbc.can.parser import CANParser
  if dbc_name is None:
    fp, plat = _platform(rd)
    if plat is None:
      return {"error": "no DBC: car platform unknown, pass dbc_name explicitly"}
    from opendbc.car.values import Bus
    dd = plat.config.dbc_dict
    dbc_name = dd.get(Bus.pt) or next(iter(dd.values()))
  idx = rd.can if stream == "can" else rd.sendcan
  frames = [(t, d) for t, a, b, d in zip(idx.t_ns, idx.addr, idx.bus, idx.dat)
            if a == address and b == bus]
  if not frames:
    return {"error": f"no frames for addr {hex(address)} on bus {bus} in '{stream}'"}
  try:
    cp = CANParser(dbc_name, [(address, 0)], bus)
  except Exception as e:
    return {"error": f"CANParser init failed for dbc '{dbc_name}': {e!r}"}
  out_t, out_v = [], []
  for t, d in frames:
    cp.update([[t, [(address, d, bus)]]])
    vl = cp.vl.get(address) if hasattr(cp.vl, "get") else cp.vl[address]
    if vl is not None and signal in vl:
      try:
        val = float(vl[signal])
      except (TypeError, ValueError):
        return {"error": f"signal '{signal}' is not scalar; pick a single-value signal"}
      out_t.append(t / 1e9)
      out_v.append(val)
  if not out_v:
    return {"error": f"signal '{signal}' not produced; check signal name and dbc '{dbc_name}'"}
  t = np.asarray(out_t)
  v = np.asarray(out_v)
  td, vd = _downsample(t, v, max_points)
  return {"dbc": dbc_name, "address": hex(address), "bus": bus, "signal": signal,
          "n": len(v), "returned": len(td), "t": _to_list(td), "v": _to_list(vd)}


def changing_bits(rd: RouteData, address: int, bus: int = 0, stream: str = "can") -> dict[str, Any]:
  idx = rd.can if stream == "can" else rd.sendcan
  dats = [d for a, b, d in zip(idx.addr, idx.bus, idx.dat) if a == address and b == bus]
  if not dats:
    return {"error": f"no frames for addr {hex(address)} on bus {bus}"}
  maxlen = max(len(d) for d in dats)
  ever_one = bytearray(maxlen)
  ever_zero = bytearray(maxlen)
  for d in dats:
    for i in range(len(d)):
      ever_one[i] |= d[i]
      ever_zero[i] |= (~d[i]) & 0xFF
  changing = []
  for byte_i in range(maxlen):
    toggling = ever_one[byte_i] & ever_zero[byte_i]
    for bit in range(8):
      if toggling & (1 << bit):
        changing.append({"byte": byte_i, "bit": bit, "global_bit": byte_i * 8 + bit})
  return {"address": hex(address), "bus": bus, "n_frames": len(dats),
          "n_changing_bits": len(changing), "changing_bits": changing}


def events_timeline(rd: RouteData, max_events: int = 500) -> dict[str, Any]:
  out = []
  t0 = None
  for msg in rd.lr:
    try:
      w = msg.which()
    except Exception:
      continue
    if w != "onroadEvents":
      continue
    t = msg.logMonoTime / 1e9
    if t0 is None:
      t0 = t
    names = []
    for ev in msg.onroadEvents:
      try:
        names.append(ev.name)
      except Exception:
        pass
    if names:
      out.append({"t": round(t - t0, 3), "events": names})
  collapsed = []
  for row in out:
    if collapsed and collapsed[-1]["events"] == row["events"]:
      continue
    collapsed.append(row)
  return {"route": rd.identifier, "n_transitions": len(collapsed),
          "timeline": collapsed[:max_events]}


def engagement_summary(rd: RouteData) -> dict[str, Any]:
  ts = rd.time_series
  field = None
  for cand_svc in ("selfdriveState", "controlsState"):
    g = ts.get(cand_svc)
    if not g:
      continue
    for cand_field in ("enabled", "deprecated/enabled"):
      if cand_field in g:
        svc, field = cand_svc, cand_field
        break
    if field:
      break
  if field is None:
    avail = {s: sorted(k for k in ts[s] if k != "t") for s in ("selfdriveState", "controlsState") if s in ts}
    return {"error": "no enabled field in selfdriveState/controlsState", "available": avail}
  g = ts[svc]
  t = np.asarray(g["t"], dtype=float)
  en = np.asarray(g[field]).astype(bool)
  trans = np.diff(en.astype(int))
  engage_t = t[1:][trans == 1]
  diseng_t = t[1:][trans == -1]
  dt = np.diff(t)
  engaged_time = float(np.sum(dt[en[:-1]])) if len(dt) else 0.0
  t0 = t[0] if len(t) else 0
  return {
    "source": f"{svc}.{field}",
    "total_s": round(float(t[-1] - t[0]), 2) if len(t) else 0,
    "engaged_s": round(engaged_time, 2),
    "n_engagements": int(np.sum(trans == 1)),
    "n_disengagements": int(np.sum(trans == -1)),
    "engage_times": [round(float(x - t0), 2) for x in engage_t][:200],
    "disengage_times": [round(float(x - t0), 2) for x in diseng_t][:200],
  }


EXPECTED_HZ = {
  "carState": 100, "controlsState": 100, "selfdriveState": 100, "carControl": 100,
  "modelV2": 20, "sendcan": 100, "can": 100, "liveLocationKalman": 20,
  "driverMonitoringState": 20, "longitudinalPlan": 20,
}


def health_scan(rd: RouteData) -> dict[str, Any]:
  ts = rd.time_series
  dur = rd.duration_s() or 1.0
  findings = []
  for svc, exp in EXPECTED_HZ.items():
    if svc in rd.service_counts:
      hz = rd.service_counts[svc] / dur
      if hz < exp * 0.7:
        findings.append({"kind": "low_rate", "service": svc,
                         "expected_hz": exp, "actual_hz": round(hz, 1)})
    g = ts.get(svc)
    if g is not None:
      t = np.asarray(g["t"], dtype=float)
      if len(t) > 1:
        gap = float(np.max(np.diff(t)))
        if gap > max(0.5, 5.0 / exp):
          findings.append({"kind": "gap", "service": svc, "max_gap_s": round(gap, 3)})
  for svc, fields in {"carState": ["vEgo", "aEgo", "steeringAngleDeg"],
                      "controlsState": ["curvature"], "liveLocationKalman": []}.items():
    g = ts.get(svc)
    if not g:
      continue
    for f in fields:
      if f in g:
        try:
          v = np.asarray(g[f], dtype=float)
        except (TypeError, ValueError):
          continue
        nn = int(np.sum(~np.isfinite(v)))
        if nn:
          findings.append({"kind": "nan", "service": svc, "field": f, "count": nn})
  if "can" in rd.service_counts and len(rd.can) == 0:
    findings.append({"kind": "no_can_frames"})
  cp = rd.car_params
  meta = {}
  if cp is not None:
    meta = {"carFingerprint": cp.carFingerprint,
            "safetyConfigs": [{"model": str(sc.safetyModel), "param": int(sc.safetyParam)}
                              for sc in cp.safetyConfigs]}
  return {"route": rd.identifier, "duration_s": round(dur, 2),
          "car": meta, "n_findings": len(findings), "findings": findings}
