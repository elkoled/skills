from __future__ import annotations

from typing import Any

from .bootstrap import bootstrap
from .route_cache import RouteData

bootstrap()

from opendbc.car.structs import CarParams  # noqa: E402
from opendbc.safety.tests.libsafety import libsafety_py as LS  # noqa: E402

_SKIP_MODES = {"silent", "noOutput", "elm327", "elm"}


def _safety_enum() -> dict[str, int]:
  return {str(k): int(v) for k, v in CarParams.SafetyModel.schema.enumerants.items()}


def active_safety_config(rd: RouteData) -> dict[str, Any]:
  cp = rd.car_params
  if cp is None:
    return {"error": "no carParams in route; cannot determine safety model"}
  enum = _safety_enum()
  configs = []
  for sc in cp.safetyConfigs:
    name = str(sc.safetyModel)
    configs.append({"model": name, "mode_int": enum.get(name), "param": int(sc.safetyParam)})
  chosen = None
  for c in reversed(configs):
    if c["model"] not in _SKIP_MODES and c["mode_int"] is not None:
      chosen = c
      break
  if chosen is None and configs:
    chosen = configs[-1]
  return {"carFingerprint": cp.carFingerprint, "alternativeExperience": int(cp.alternativeExperience),
          "configs": configs, "active": chosen}


def _interleave(rd: RouteData):
  events = []
  c = rd.can
  for t, a, b, d in zip(c.t_ns, c.addr, c.bus, c.dat):
    if b < 8:  # drop panda TX echoes (src=bus+0x80); CANPacket bus is 3-bit
      events.append((t, 0, a, b, d))
  s = rd.sendcan
  for t, a, b, d in zip(s.t_ns, s.addr, s.bus, s.dat):
    if b < 8:
      events.append((t, 1, a, b, d))
  events.sort(key=lambda e: (e[0], e[1]))
  return events


def replay(rd: RouteData, t_start: float | None = None, t_end: float | None = None) -> dict[str, Any]:
  cfg = active_safety_config(rd)
  if "error" in cfg or not cfg.get("active"):
    return {"error": cfg.get("error", "no usable safety config")}
  active = cfg["active"]
  lib = LS.libsafety
  lib.init_tests()
  set_rc = lib.set_safety_hooks(active["mode_int"], active["param"])
  lib.set_alternative_experience(cfg["alternativeExperience"])
  if set_rc != 0:
    return {"error": f"set_safety_hooks failed rc={set_rc} for {active}"}

  events = _interleave(rd)
  if not events:
    return {"error": "no can/sendcan frames in route"}
  t0 = events[0][0]
  last_tick = 0
  blocked: dict[tuple[int, int], dict[str, Any]] = {}
  allowed_keys: set[tuple[int, int]] = set()
  n_tx = n_blocked = 0
  for t_ns, kind, addr, bus, dat in events:
    tsec = (t_ns - t0) / 1e9
    if t_start is not None and tsec < t_start:
      continue
    if t_end is not None and tsec > t_end:
      break
    lib.set_timer(((t_ns - t0) // 1000) & 0xFFFFFFFF)
    if t_ns - last_tick > 10_000_000:
      lib.safety_tick_current_safety_config()
      last_tick = t_ns
    pkt = LS.make_CANPacket(addr, bus, dat)
    if kind == 0:
      lib.safety_rx_hook(pkt)
    else:
      n_tx += 1
      ok = bool(lib.safety_tx_hook(pkt))
      k = (bus, addr)
      if ok:
        allowed_keys.add(k)
      else:
        n_blocked += 1
        e = blocked.get(k)
        if e is None:
          e = blocked[k] = {"bus": bus, "address": addr, "address_hex": hex(addr),
                            "blocked": 0, "first_t": round(tsec, 3), "sample_dat": dat.hex()}
        e["blocked"] += 1
  groups = sorted(blocked.values(), key=lambda g: -g["blocked"])
  for g in groups:
    g["also_allowed_sometimes"] = (g["bus"], g["address"]) in allowed_keys
  return {"route": rd.identifier, "safety": active, "n_tx": n_tx,
          "n_blocked": n_blocked, "n_blocked_addresses": len(groups),
          "note": "replay applies the route's final safety mode to the whole log; "
                  "diagnostic/UDS frames sent during early fingerprinting (when panda "
                  "was still in ELM/allOutput) may be over-flagged. Cross-check with "
                  "panda_blocked_messages for hardware truth.",
          "blocked": groups}


_RETURNED_OFFSET = 0x80   # panda CAN_RETURNED_BUS_OFFSET: accepted TX echoed back
_REJECTED_OFFSET = 0xC0   # panda CAN_REJECTED_BUS_OFFSET: TX panda refused to send


def blocked_via_echo(rd: RouteData, window_ms: float = 100.0) -> dict[str, Any]:
  can = rd.can
  echo: dict[tuple[int, int], list[int]] = {}
  for t, a, b, d in zip(can.t_ns, can.addr, can.bus, can.dat):
    if _RETURNED_OFFSET <= b < _REJECTED_OFFSET:
      echo.setdefault((a, b - _RETURNED_OFFSET), []).append(t)
  win = window_ms * 1e6
  s = rd.sendcan
  blocked: dict[tuple[int, int], dict[str, Any]] = {}
  n_blocked = 0
  t0 = s.t_ns[0] if len(s) else 0
  for t, a, b, d in zip(s.t_ns, s.addr, s.bus, s.dat):
    times = echo.get((a, b))
    hit = any(abs(et - t) <= win for et in times) if times else False
    if not hit:
      n_blocked += 1
      k = (b, a)
      e = blocked.get(k)
      if e is None:
        e = blocked[k] = {"bus": b, "address": a, "address_hex": hex(a),
                          "no_echo": 0, "first_t": round((t - t0) / 1e9, 3)}
      e["no_echo"] += 1
  return {"route": rd.identifier, "method": "echo_diff",
          "note": "no_echo strongly implies a panda block; cross-check with panda_replay",
          "n_sendcan": len(s), "n_no_echo": n_blocked,
          "blocked": sorted(blocked.values(), key=lambda g: -g["no_echo"])}


def root_cause(rd: RouteData, address: int | None = None, bus: int | None = None,
               t_start: float | None = None, t_end: float | None = None,
               max_groups: int = 10) -> dict[str, Any]:
  from .panda_trace import get_trace_lib
  cfg = active_safety_config(rd)
  if "error" in cfg or not cfg.get("active"):
    return {"error": cfg.get("error", "no usable safety config")}
  active = cfg["active"]
  trace = get_trace_lib()
  if not trace.ok:
    return {"error": f"gcov trace unavailable ({trace.reason}); use panda_replay for verdicts",
            "safety": active}
  trace.setup(active["mode_int"], active["param"], cfg["alternativeExperience"])

  events = _interleave(rd)
  if not events:
    return {"error": "no can/sendcan frames"}
  t0 = events[0][0]
  last_tick = 0
  blocked_cov: dict[tuple[int, int], dict] = {}
  allowed_cov: dict[tuple[int, int], dict] = {}
  for t_ns, kind, addr, b, dat in events:
    tsec = (t_ns - t0) / 1e9
    if t_start is not None and tsec < t_start:
      continue
    if t_end is not None and tsec > t_end:
      break
    trace.set_timer((t_ns - t0) // 1000)
    if t_ns - last_tick > 10_000_000:
      trace.lib.safety_tick_current_safety_config()
      last_tick = t_ns
    if kind == 0:
      trace.rx(addr, b, dat)
      continue
    if address is not None and addr != address:
      continue
    if bus is not None and b != bus:
      continue
    k = (b, addr)
    need_blocked = k not in blocked_cov
    need_allowed = k not in allowed_cov
    if not (need_blocked or need_allowed):
      continue
    # tx_hook mutates rate-limit state, so use this call's verdict, never re-call
    allowed, cov = trace.executed_lines(addr, b, dat)
    if not allowed and need_blocked:
      blocked_cov[k] = {"t": round(tsec, 3), "dat": dat.hex(), "cov": cov}
    elif allowed and need_allowed:
      allowed_cov[k] = {"t": round(tsec, 3), "dat": dat.hex(), "cov": cov}

  results = []
  for k, info in list(blocked_cov.items())[:max_groups]:
    bnum, addr = k
    blk_lines = {(f, ln) for f, lines in info["cov"].items() for ln in lines}
    allow = allowed_cov.get(k)
    diff = blk_lines
    if allow:
      allow_lines = {(f, ln) for f, lines in allow["cov"].items() for ln in lines}
      diff = blk_lines - allow_lines
    culprits = []
    for f, ln in sorted(diff):
      src = trace.source_line(f, ln)
      if not src:
        continue
      low = src.lower()
      decisive = any(s in low for s in ("false", "violation", "= tx", "return", "_check(", "_limit", "block"))
      culprits.append({"file": f.split("/opendbc/")[-1], "line": ln, "code": src, "decisive": decisive})
    results.append({
      "bus": bnum, "address": addr, "address_hex": hex(addr),
      "first_blocked_t": info["t"], "sample_dat": info["dat"],
      "has_allowed_baseline": allow is not None,
      "decisive_lines": [c for c in culprits if c["decisive"]][:20],
      "root_cause_lines": culprits[:60],
    })
  return {"route": rd.identifier, "safety": active,
          "n_blocked_addresses": len(blocked_cov), "results": results}
