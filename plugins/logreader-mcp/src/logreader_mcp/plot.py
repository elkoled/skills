from __future__ import annotations

import io
import re

import numpy as np

from . import analysis
from .route_cache import RouteData

MAX_POINTS = 4000  # per line, more doesn't show at plot resolution

# can[/sendcan]:<addr>:<signal>[@bus][#dbc], e.g. can:0x415:VehYawNonLin_W_Rq@0
_SIGNAL = re.compile(r"(can|sendcan):(0x[0-9a-fA-F]+|\d+):([\w.]+)(?:@(\d+))?(?:#([\w.]+))?")


# one series: 'service/field/path' for cereal, or the can: syntax above for a DBC signal.
# returns (t relative to route start, values, category names for enum fields) or an error string
def series(rd: RouteData, spec: str) -> tuple[np.ndarray, np.ndarray, list[str] | None] | str:
  m = _SIGNAL.fullmatch(spec)
  if m:
    stream, addr, sig, bus, dbc = m.groups()
    res = analysis.decode_series(rd, int(addr, 0), sig, int(bus or 0), dbc, stream)
    if isinstance(res, dict):
      return res["error"]
    t, v, _ = res
    cats = None
  else:
    service, _, field = spec.partition("/")
    if not field:
      return f"'{spec}': use service/field (e.g. carState/vEgo) or can:0xADDR:SIGNAL[@bus]"
    t, v, err = analysis._scalar_series(rd, service, field)
    if err:
      return f"'{spec}': {err['error']}"
    cats = None
    try:
      v = v.astype(float)
    except (TypeError, ValueError):
      # enums and strings plot as their category index, the names become y tick labels
      cats = list(dict.fromkeys(v.tolist()))
      code = {c: i for i, c in enumerate(cats)}
      v = np.array([code[x] for x in v.tolist()], dtype=float)
  return t - rd.span()[0], v, cats


def render(lines: list[tuple[str, str, np.ndarray, np.ndarray, list[str] | None]], t_start: float | None, t_end: float | None,
           title: str, height: float = 2.2) -> bytes:
  """lines: (panel, label, t, v, categories). One panel per distinct name, labels overlaid in it."""
  import matplotlib
  matplotlib.use("Agg")
  import matplotlib.pyplot as plt

  panels = list(dict.fromkeys(p for p, *_ in lines))
  fig, axes = plt.subplots(len(panels), 1, sharex=True, figsize=(12, max(3.0, height * len(panels))), squeeze=False)
  for ax, panel in zip(axes[:, 0], panels, strict=True):
    for p, label, t, v, cats in lines:
      if p != panel:
        continue
      if cats:
        ax.set_yticks(range(len(cats)), cats, fontsize=7)
      mask = np.ones(len(t), dtype=bool)
      if t_start is not None:
        mask &= t >= t_start
      if t_end is not None:
        mask &= t <= t_end
      t, v = t[mask], v[mask]
      if len(t) > MAX_POINTS:
        idx = np.linspace(0, len(t) - 1, MAX_POINTS).astype(int)
        t, v = t[idx], v[idx]
      ax.plot(t, v, linewidth=1.0, label=label)
    ax.set_ylabel(panel, fontsize=8)
    ax.grid(True, alpha=0.3)
    if sum(1 for p, *_ in lines if p == panel) > 1:
      ax.legend(fontsize=8, loc="upper right")
  axes[-1, 0].set_xlabel("time since route start (s)")
  fig.suptitle(title, fontsize=9)
  fig.tight_layout()
  buf = io.BytesIO()
  fig.savefig(buf, format="png", dpi=100)
  plt.close(fig)
  return buf.getvalue()
