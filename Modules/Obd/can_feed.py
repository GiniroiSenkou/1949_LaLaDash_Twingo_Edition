"""Turn decoded CAN signals into app state.

Runs on the STN monitor thread (StnSession.on_frames, ≤ 20×/s). Decodes the
*confirmed* signals of the map, and only when a value actually changed:

  metric:<name> → TelemetryHub.ingest_can()      (the PID for it stops polling)
  light:<key>   → DashLightsHub.set_can_active() (icons + dl_* alerts)
  clutch        → TelemetryHub clutch state       (AudioEngine shift detection)
  state:<key>   → TelemetryHub "CAN <key>" snapshot entries

Candidates are decoded too, but only exposed to the debugger (live column),
never to the app — that is what "confirm" means.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from Modules.Obd.can_signals import CATALOG_BY_KEY, CanSignalMap, signal_map


class CanFeed:
    def __init__(self, smap: Optional[CanSignalMap] = None) -> None:
        self.map = smap or signal_map()
        self._lock = threading.Lock()
        self._last_confirmed: Dict[str, Any] = {}
        self._last_all: Dict[str, Any] = {}
        self.pushes = 0

    @property
    def live(self) -> Dict[str, Any]:
        """Latest decoded value of every bound signal (confirmed + candidate)."""
        with self._lock:
            return dict(self._last_all)

    def on_frames(self, store, now: float) -> None:
        values = self.map.decode(store, statuses=("confirmed", "candidate"))
        with self._lock:
            self._last_all = values
        confirmed_keys = {k for k, s in self.map.signals.items() if s.get("status") == "confirmed"}
        confirmed = {k: v for k, v in values.items() if k in confirmed_keys}
        if confirmed == self._last_confirmed:
            return
        changed = {k: v for k, v in confirmed.items() if self._last_confirmed.get(k, object()) != v}
        gone = [k for k in self._last_confirmed if k not in confirmed]
        self._last_confirmed = confirmed
        self.pushes += 1
        self._push(changed, gone, confirmed, now)

    def _push(self, changed: Dict[str, Any], gone, confirmed: Dict[str, Any], now: float) -> None:
        metrics: Dict[str, Any] = {}
        states: Dict[str, Any] = {}
        clutch: Optional[bool] = None
        lights_dirty = bool(gone)
        for key, v in changed.items():
            target = CATALOG_BY_KEY[key][4]
            if target.startswith("metric:"):
                metrics[target[7:]] = v
            elif target.startswith("state:"):
                states[target[6:]] = v
            elif target == "clutch":
                clutch = bool(v)
            elif target.startswith("light:"):
                lights_dirty = True
        try:
            from Modules.Obd.telemetry import TelemetryHub
            if metrics or states or clutch is not None:
                TelemetryHub.instance().ingest_can(metrics, states or None, clutch, ts=now)
        except Exception:
            pass
        if lights_dirty:
            active = []
            for key, v in confirmed.items():
                target = CATALOG_BY_KEY[key][4]
                if target.startswith("light:") and v is True:
                    active.append(target[6:])
            try:
                from Modules.Layout.dash_lights_hub import DashLightsHub
                DashLightsHub.instance().set_can_active(active)
            except Exception:
                pass
