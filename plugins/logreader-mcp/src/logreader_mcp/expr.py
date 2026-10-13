from __future__ import annotations

import ast
from typing import Any

import numpy as np

from .route_cache import RouteData

# numpy expressions over cereal fields: 'modelV2.action.desiredCurvature * carState.vEgo**2',
# 'selfdriveState.enabled and abs(carState.steeringAngleDeg) > 90', 'modelV2.laneLines[1].y[0]'.
# every field is sampled at the times of the fastest referenced service (last value at or before)


def _deriv(t):
  return lambda x: np.gradient(np.asarray(x, dtype=float), t) if len(t) > 1 else np.zeros(len(t))


def _smooth(t):
  # first order lowpass with time constant tau seconds
  def f(x, tau):
    x = np.asarray(x, dtype=float)
    out = np.empty_like(x)
    acc = x[0] if len(x) else 0.0
    dt = np.diff(t, prepend=t[0] if len(t) else 0.0)
    k = dt / (tau + dt) if tau > 0 else np.ones_like(dt)
    for i in range(len(x)):
      acc = acc + (x[i] - acc) * k[i] if np.isfinite(x[i]) else acc
      out[i] = acc
    return out
  return f


FUNCS = {
  "abs": np.abs, "sqrt": np.sqrt, "sign": np.sign, "clip": np.clip, "minimum": np.minimum, "maximum": np.maximum,
  "where": np.where, "hypot": np.hypot, "deg2rad": np.deg2rad, "rad2deg": np.rad2deg, "sin": np.sin, "cos": np.cos,
  "tan": np.tan, "arctan2": np.arctan2, "log": np.log, "exp": np.exp, "isnan": np.isnan, "round": np.round,
  "float": lambda x: np.asarray(x, dtype=float), "int": lambda x: np.asarray(x, dtype=float).astype(np.int64),
}
TIME_FUNCS = {"deriv": _deriv, "smooth": _smooth}
CONSTS = {"pi": np.pi, "inf": np.inf, "nan": np.nan}
_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Compare, ast.BoolOp, ast.Call, ast.Name, ast.Constant, ast.Load,
          ast.IfExp, ast.operator, ast.unaryop, ast.cmpop, ast.boolop, ast.Tuple)


class ExprError(Exception):
  pass


# service.a.b[1].c -> (service, 'a/b/1/c'), None when the node is not a field reference
def _field_ref(node: ast.AST) -> tuple[str, str] | None:
  parts: list[str] = []
  while isinstance(node, (ast.Attribute, ast.Subscript)):
    if isinstance(node, ast.Attribute):
      parts.append(node.attr)
    else:
      if not (isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int)):
        raise ExprError("list indexes must be integer constants, e.g. laneLines[1]")
      parts.append(str(node.slice.value))
    node = node.value
  if not isinstance(node, ast.Name) or not parts:
    return None
  if any(p.startswith("_") for p in parts):
    raise ExprError("fields don't start with '_'")
  return node.id, "/".join(reversed(parts))


class _Rewrite(ast.NodeTransformer):
  def __init__(self):
    self.refs: dict[tuple[str, str], str] = {}

  def _ref(self, node):
    ref = _field_ref(node)
    if ref is None:
      raise ExprError(f"unsupported expression: {ast.unparse(node)}")
    name = self.refs.setdefault(ref, f"_f{len(self.refs)}")
    return ast.copy_location(ast.Name(id=name, ctx=ast.Load()), node)

  visit_Attribute = _ref
  visit_Subscript = _ref

  def visit_Call(self, node):
    if not isinstance(node.func, ast.Name) or node.func.id not in {*FUNCS, *TIME_FUNCS}:
      raise ExprError(f"unknown function {ast.unparse(node.func)}, available: {sorted({*FUNCS, *TIME_FUNCS})}")
    if node.keywords:
      raise ExprError("pass function arguments by position")
    node.args = [self.visit(a) for a in node.args]
    return node

  def visit_Name(self, node):
    if node.id.startswith("_"):
      raise ExprError(f"unknown name {node.id!r}")
    return node

  # python's and/or/not and chained comparisons don't broadcast over arrays
  def visit_BoolOp(self, node):
    self.generic_visit(node)
    fn = "_and" if isinstance(node.op, ast.And) else "_or"
    out = node.values[0]
    for v in node.values[1:]:
      out = ast.Call(func=ast.Name(id=fn, ctx=ast.Load()), args=[out, v], keywords=[])
    return ast.copy_location(out, node)

  def visit_UnaryOp(self, node):
    self.generic_visit(node)
    if isinstance(node.op, ast.Not):
      return ast.copy_location(ast.Call(func=ast.Name(id="_not", ctx=ast.Load()), args=[node.operand], keywords=[]), node)
    return node

  def visit_Compare(self, node):
    self.generic_visit(node)
    if len(node.ops) == 1:
      return node
    pairs, left = [], node.left
    for op, right in zip(node.ops, node.comparators, strict=True):
      pairs.append(ast.Compare(left=left, ops=[op], comparators=[right]))
      left = right
    out = pairs[0]
    for p in pairs[1:]:
      out = ast.Call(func=ast.Name(id="_and", ctx=ast.Load()), args=[out, p], keywords=[])
    return ast.copy_location(out, node)

  def visit_IfExp(self, node):
    self.generic_visit(node)
    return ast.copy_location(ast.Call(func=ast.Name(id="where", ctx=ast.Load()), args=[node.test, node.body, node.orelse], keywords=[]), node)


def _sample(src_t: np.ndarray, v: np.ndarray, t: np.ndarray) -> np.ndarray:
  if src_t is t:
    return v
  i = np.searchsorted(src_t, t, side="right") - 1
  out = v[np.clip(i, 0, len(v) - 1)]
  if v.dtype.kind == "f":
    out = np.where(i < 0, np.nan, out)
  return out


def evaluate(rd: RouteData, expr: str) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
  """Returns (route time in seconds, values, info). Raises ExprError with a usable message."""
  try:
    tree = ast.parse(expr.strip(), mode="eval")
  except SyntaxError as e:
    raise ExprError(f"syntax error in {expr!r}: {e.msg}") from e
  rw = _Rewrite()
  tree = ast.fix_missing_locations(rw.visit(tree))
  allowed = {*FUNCS, *TIME_FUNCS, *CONSTS, "t", "_and", "_or", "_not", *rw.refs.values()}
  for node in ast.walk(tree):
    if not isinstance(node, _NODES):
      raise ExprError(f"not allowed in expressions: {type(node).__name__}")
    if isinstance(node, ast.Name) and node.id not in allowed:
      raise ExprError(f"unknown name {node.id!r}. fields are service.field, e.g. carState.vEgo")
  counts = rd.service_counts
  for svc, _ in rw.refs:
    if svc not in counts:
      raise ExprError(f"service {svc!r} not in route")
  if not rw.refs:
    raise ExprError("the expression references no field, e.g. carState.vEgo")

  base = max({svc for svc, _ in rw.refs}, key=lambda s: counts[s])
  t_abs = rd.times(base)
  env: dict[str, Any] = {**FUNCS, **CONSTS, "_and": np.logical_and, "_or": np.logical_or, "_not": np.logical_not,
                         "t": rd.route_time(t_abs)}
  env.update({k: f(t_abs) for k, f in TIME_FUNCS.items()})
  for (svc, path), name in rw.refs.items():
    try:
      v = rd.field(svc, path)
    except (AttributeError, KeyError, IndexError) as e:
      raise ExprError(f"field {path.replace('/', '.')!r} not in {svc!r}: {e}") from e
    if v.dtype == object:
      raise ExprError(f"{svc}.{path.replace('/', '.')} is not a scalar, index into it (e.g. [0]) or pick a leaf field")
    env[name] = _sample(rd.times(svc), v, t_abs)
  try:
    with np.errstate(all="ignore"):
      out = eval(compile(tree, "<expr>", "eval"), {"__builtins__": {}}, env)  # noqa: S307 names and nodes are whitelisted above
  except Exception as e:  # noqa: BLE001
    raise ExprError(f"could not evaluate {expr!r}: {e}") from e
  out = np.broadcast_to(np.asarray(out), t_abs.shape)
  return rd.route_time(t_abs), out, {"base_service": base, "fields": [f"{s}.{p.replace('/', '.')}" for s, p in rw.refs]}


def _stats(t: np.ndarray, v: np.ndarray) -> dict[str, Any]:
  if v.dtype.kind not in "fiub":
    vals, counts = np.unique(v, return_counts=True)
    return {"values": {str(a): int(c) for a, c in zip(vals, counts, strict=True)}}
  f = v.astype(float)
  ok = np.isfinite(f)
  if not ok.any():
    return {"n_finite": 0}
  i_max, i_min = int(np.nanargmax(np.where(ok, f, -np.inf))), int(np.nanargmin(np.where(ok, f, np.inf)))
  return {"min": float(f[i_min]), "t_min": round(float(t[i_min]), 2), "max": float(f[i_max]), "t_max": round(float(t[i_max]), 2),
          "mean": float(np.mean(f[ok])), "std": float(np.std(f[ok])), "n_nan": int((~ok).sum())}


def series(rd: RouteData, expr: str, t_start: float | None, t_end: float | None, max_points: int) -> dict[str, Any]:
  t, v, info = evaluate(rd, expr)
  m = np.ones(len(t), dtype=bool)
  if t_start is not None:
    m &= t >= t_start
  if t_end is not None:
    m &= t <= t_end
  t, v = t[m], v[m]
  idx = np.linspace(0, len(t) - 1, min(len(t), max(1, max_points))).astype(int) if len(t) else np.zeros(0, dtype=int)
  vals = v[idx]
  vals = [None if isinstance(x, float) and not np.isfinite(x) else x for x in vals.tolist()]
  return {"expr": expr, **info, "n": int(len(t)), **_stats(t, v), "returned": len(idx),
          "t": [round(x, 3) for x in t[idx].tolist()], "v": vals}


def windows(rd: RouteData, condition: str, score: str = "", min_duration: float = 0.0, merge_gap: float = 0.5,
            pad: float = 0.0, sort: str = "time", max_windows: int = 50) -> dict[str, Any]:
  t, cond, info = evaluate(rd, condition)
  if cond.dtype != bool:
    cond = np.nan_to_num(cond.astype(float)) != 0
  edges = np.diff(np.r_[0, cond.astype(np.int8), 0])
  starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1) - 1
  runs: list[list[int]] = []
  for a, b in zip(starts.tolist(), ends.tolist(), strict=True):
    if runs and t[a] - t[runs[-1][1]] <= merge_gap:
      runs[-1][1] = b
    else:
      runs.append([a, b])
  st = None
  if score:
    st, sv, _ = evaluate(rd, score)
    sv = sv.astype(float)
  t0, t1 = float(t[0]) if len(t) else 0.0, float(t[-1]) if len(t) else 0.0
  out = []
  for a, b in runs:
    start, end = float(t[a]), float(t[b])
    if end - start < min_duration:
      continue
    w: dict[str, Any] = {"start": round(max(t0, start - pad), 2), "end": round(min(t1, end + pad), 2), "duration": round(end - start, 2)}
    if st is not None:
      m = (st >= start) & (st <= end) & np.isfinite(sv)
      if m.any():
        seg_t, seg_v = st[m], sv[m]
        i = int(np.argmax(np.abs(seg_v)))
        w.update({"peak": float(seg_v[i]), "t_peak": round(float(seg_t[i]), 2), "mean": float(seg_v.mean())})
    out.append(w)
  if sort == "score" and score:
    out.sort(key=lambda w: -abs(w.get("peak", 0.0)))
  total = len(out)
  return {"condition": condition, "score": score or None, **info, "route_span": [round(t0, 2), round(t1, 2)],
          "n_windows": total, "total_s": round(sum(w["duration"] for w in out), 2), "windows": out[:max_windows],
          **({"note": f"{total - max_windows} more not shown, raise max_windows"} if total > max_windows else {})}
