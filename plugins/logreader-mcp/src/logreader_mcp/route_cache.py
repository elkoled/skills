from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from .bootstrap import bootstrap

bootstrap()

from openpilot.tools.lib.logreader import LogReader, ReadMode  # noqa: E402
from openpilot.tools.lib.log_time_series import msgs_to_time_series  # noqa: E402


@dataclass
class CanIndex:
  t_ns: list[int] = field(default_factory=list)
  addr: list[int] = field(default_factory=list)
  bus: list[int] = field(default_factory=list)
  dat: list[bytes] = field(default_factory=list)

  def __len__(self):
    return len(self.t_ns)

  def add(self, t_ns: int, address: int, src: int, data: bytes):
    self.t_ns.append(t_ns)
    self.addr.append(address)
    self.bus.append(src)
    self.dat.append(data)


class RouteData:
  def __init__(self, identifier: str, mode: ReadMode = ReadMode.RLOG):
    self.identifier = identifier
    self.lr = LogReader(identifier, default_mode=mode, sort_by_time=True)
    self._lock = threading.Lock()
    self._ts: dict[str, Any] | None = None
    self._can: CanIndex | None = None
    self._sendcan: CanIndex | None = None
    self._counts: dict[str, int] | None = None
    self._car_params = None
    self._car_params_loaded = False

  @property
  def time_series(self) -> dict[str, Any]:
    if self._ts is None:
      with self._lock:
        if self._ts is None:
          self._ts = msgs_to_time_series(self.lr)
    return self._ts

  @property
  def service_counts(self) -> dict[str, int]:
    if self._counts is None:
      counts: dict[str, int] = {}
      for msg in self.lr:
        try:
          w = msg.which()
        except Exception:
          continue
        counts[w] = counts.get(w, 0) + 1
      self._counts = counts
    return self._counts

  @property
  def car_params(self):
    if not self._car_params_loaded:
      self._car_params = self.lr.first("carParams")
      self._car_params_loaded = True
    return self._car_params

  def _build_can(self):
    can = CanIndex()
    sendcan = CanIndex()
    for msg in self.lr:
      try:
        w = msg.which()
      except Exception:
        continue
      if w == "can":
        t = msg.logMonoTime
        for c in msg.can:
          can.add(t, c.address, c.src, bytes(c.dat))
      elif w == "sendcan":
        t = msg.logMonoTime
        for c in msg.sendcan:
          sendcan.add(t, c.address, c.src, bytes(c.dat))
    self._can, self._sendcan = can, sendcan

  @property
  def can(self) -> CanIndex:
    if self._can is None:
      with self._lock:
        if self._can is None:
          self._build_can()
    return self._can

  @property
  def sendcan(self) -> CanIndex:
    if self._sendcan is None:
      _ = self.can
    return self._sendcan

  def duration_s(self) -> float:
    ts = self.time_series
    starts, ends = [], []
    for g in ts.values():
      t = g.get("t")
      if t is not None and len(t):
        starts.append(float(t[0]))
        ends.append(float(t[-1]))
    if not starts:
      return 0.0
    return max(ends) - min(starts)


_ROUTES: dict[str, RouteData] = {}
_ROUTES_LOCK = threading.Lock()


def get_route(identifier: str, mode: str = "r") -> RouteData:
  key = f"{identifier}::{mode}"
  with _ROUTES_LOCK:
    rd = _ROUTES.get(key)
    if rd is None:
      rd = RouteData(identifier, ReadMode(mode))
      _ROUTES[key] = rd
    return rd


def clear_cache() -> int:
  with _ROUTES_LOCK:
    n = len(_ROUTES)
    _ROUTES.clear()
    return n
