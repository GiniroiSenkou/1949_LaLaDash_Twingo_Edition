"""`Trip` — the Trip page, the refuel price prompt and the cost lines.

State lives in `Modules/Settings/fuel_ledger.py` (prices, refuels, finished
trips) and `Modules/Runtime/trip_recorder.py` (the trip in progress). This
bridge only caches what QML binds to: every getter returns a prepared value,
never touches disk — the driving screensaver and Home read `todayCostText`,
and a getter doing I/O at the telemetry rate is what made the screensaver
flicker once already.

Refresh cadence: 1 s while the Trip page is showing, 5 s while the engine
runs (the screensaver / Home cost line), otherwise only on events.
"""
from __future__ import annotations

import time
from datetime import datetime

from PyQt6.QtCore import QObject, QTimer, pyqtProperty, pyqtSignal, pyqtSlot

from Modules.Runtime import trip_recorder as tr
from Modules.Settings import fuel_ledger


def _eur(v) -> str:
    return "—" if v is None else f"{float(v):.2f} €"


def _hm(seconds: float) -> str:
    m = int(round(float(seconds or 0) / 60.0))
    return f"{m // 60} h {m % 60:02d}" if m >= 60 else f"{m} min"


class TripBridge(QObject):
    changed = pyqtSignal()
    refuelChanged = pyqtSignal()

    def __init__(self, recorder: "tr.TripRecorder", parent=None) -> None:
        super().__init__(parent)
        self._rec = recorder
        self._page_visible = False
        self._engine_on = False
        self._draft = 0.0
        self._trip: dict = {}
        self._trip_state = "none"
        self._notes: list = []
        self._periods: dict = {}
        self._today_cost = "—"
        self._tank_avg = "—"
        self._last_price = "—"
        self._k = "1.00"
        self._refuel: dict = {}

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        recorder.on_trip_paused = lambda _s: self.refresh()
        recorder.on_trip_finished = lambda _s: self.refresh()
        self._load_refuel()
        self.refresh()

    # ── lifecycle ────────────────────────────────────────────────────────
    def _retime(self) -> None:
        interval = 1000 if self._page_visible else (5000 if self._engine_on else 0)
        if interval == 0:
            self._timer.stop()
        else:
            self._timer.setInterval(interval)
            if not self._timer.isActive():
                self._timer.start()

    @pyqtSlot(bool)
    def setPageVisible(self, visible) -> None:
        self._page_visible = bool(visible)
        self._retime()
        if visible:
            self.refresh()

    def noteEngine(self, on: bool) -> None:
        self._engine_on = bool(on)
        self._retime()
        self.refresh()

    def noteRefuel(self) -> None:
        self._load_refuel()
        self.refresh()

    # ── data ─────────────────────────────────────────────────────────────
    def _load_refuel(self) -> None:
        p = fuel_ledger.pending_refuel()
        if p:
            added = max(0.0, p["pct_after"] - p["pct_before"])
            p["litres"] = round(added * fuel_ledger.tank_litres() / 100.0, 1)
            p["added_pct"] = round(added, 0)
            last = fuel_ledger.last_price()
            if self._draft <= 0:
                self._draft = last if last is not None else 1.80
        self._refuel = p or {}
        self.refuelChanged.emit()

    @pyqtSlot()
    def refresh(self) -> None:
        live = self._rec.live_summary()
        if live is not None:
            recent = fuel_ledger.recent_trips(4)
            live["recurring"] = tr._recurring(live["notes"], recent)
            paused = bool(self._rec.trip and self._rec.trip.get("off_since"))
            state, trip = ("paused" if paused else "live"), live
        else:
            last = fuel_ledger.last_trip()
            state, trip = ("last", last) if last else ("none", {})
        self._trip_state = state
        self._trip = self._format_trip(trip) if trip else {}
        self._notes = [
            {"key": k, "title": tr.NOTE_TEXT[k][0], "body": tr.NOTE_TEXT[k][1],
             "recurring": k in (trip.get("recurring") or [])}
            for k in (trip.get("notes") or []) if k in tr.NOTE_TEXT
        ]
        per = fuel_ledger.periods(extra=self._rec.open_trip_for_periods())
        self._periods = {k: self._format_period(v) for k, v in per.items()}
        self._today_cost = _eur(per["today"]["cost"]) if fuel_ledger.tank_avg_price() is not None else "—"
        avg = fuel_ledger.tank_avg_price()
        last = fuel_ledger.last_price()
        self._tank_avg = "—" if avg is None else f"{avg:.3f} €/L"
        self._last_price = "—" if last is None else f"{last:.3f} €/L"
        self._k = f"{fuel_ledger.k_flow():.2f}"
        self.changed.emit()

    @staticmethod
    def _format_trip(t: dict) -> dict:
        sc = t.get("scores") or {}
        km = float(t.get("km") or 0)
        cost = float(t.get("cost") or 0)
        priced = float(t.get("priced_litres") or 0) > 0
        start = datetime.fromtimestamp(float(t.get("start_ts") or time.time()))
        return {
            "km": f"{km:.1f}",
            "time": _hm(t.get("drive_s") or 0),
            "litres": f"{float(t.get('litres') or 0):.2f}",
            "l100": "—" if t.get("l100") is None else f"{t['l100']:.1f}",
            "cost": _eur(cost) if priced else "—",
            "perKm": (f"{100 * cost / km:.1f} c" if (priced and km >= 1) else "—"),
            "avgSpeed": f"{int(t.get('avg_speed') or 0)}",
            "maxSpeed": f"{int(t.get('max_speed') or 0)}",
            "idle": _hm(t.get("idle_s") or 0),
            "started": start.strftime("%a %d %b · %H:%M"),
            "stops": int(t.get("segments", 1)) - 1,
            "acc": int(sc.get("acc", 0)), "gear": int(sc.get("gear", 0)),
            "antic": int(sc.get("antic", 0)), "idleScore": int(sc.get("idle", 0)),
            "overall": int(sc.get("overall", 0)),
            "accEvents": int(t.get("acc_events") or 0),
            "brakeEvents": int(t.get("brake_events") or 0),
            "upshifts": int(t.get("upshifts") or 0),
            "highUpshifts": int(t.get("high_upshifts") or 0),
            "tip": t.get("tip") or "",
        }

    @staticmethod
    def _format_period(p: dict) -> dict:
        return {
            "km": f"{p['km']:.0f}",
            "litres": f"{p['litres']:.1f}",
            "cost": f"{p['cost']:.2f} €" if p.get("avg_price") is not None else "—",
            "l100": "—" if p.get("l100") is None else f"{p['l100']:.1f}",
            "trips": int(p["trips"]),
        }

    # ── properties ───────────────────────────────────────────────────────
    @pyqtProperty("QVariantMap", notify=changed)
    def trip(self):
        return self._trip

    @pyqtProperty(str, notify=changed)
    def tripState(self):
        """none / live / paused / last."""
        return self._trip_state

    @pyqtProperty(list, notify=changed)
    def notes(self):
        return self._notes

    @pyqtProperty("QVariantMap", notify=changed)
    def periods(self):
        return self._periods

    @pyqtProperty(str, notify=changed)
    def todayCostText(self):
        return self._today_cost

    @pyqtProperty(str, notify=changed)
    def tankAvgPriceText(self):
        return self._tank_avg

    @pyqtProperty(str, notify=changed)
    def lastPriceText(self):
        return self._last_price

    @pyqtProperty(str, notify=changed)
    def calibrationText(self):
        return self._k

    @pyqtProperty(bool, notify=refuelChanged)
    def refuelPending(self):
        return bool(self._refuel)

    @pyqtProperty(str, notify=refuelChanged)
    def refuelText(self):
        p = self._refuel
        if not p:
            return ""
        return (f"Tank went from {p['pct_before']:.0f} % to {p['pct_after']:.0f} % "
                f"(+{p['added_pct']:.0f} %, about {p['litres']:.1f} L)")

    @pyqtProperty(str, notify=refuelChanged)
    def draftPriceText(self):
        return f"{self._draft:.3f}"

    # ── slots ────────────────────────────────────────────────────────────
    @pyqtSlot(int)
    def stepPrice(self, direction) -> None:
        self._draft = round(max(0.0, self._draft + 0.01 * (1 if int(direction) > 0 else -1)), 3)
        self.refuelChanged.emit()

    @pyqtSlot(str)
    def setDraftPrice(self, text) -> None:
        try:
            v = float(str(text).replace(",", "."))
        except ValueError:
            return
        if 0.0 < v < 10.0:
            self._draft = round(v, 3)
            self.refuelChanged.emit()

    @pyqtSlot()
    def confirmRefuel(self) -> None:
        if self._draft <= 0:
            return
        try:
            from Modules.Settings.trips_store import peek_trips_snapshot
            odo = float(peek_trips_snapshot().get("total_km") or 0) or None
        except Exception:
            odo = None
        fuel_ledger.confirm_refuel(self._draft, odo_km=odo)
        self._load_refuel()
        self.refresh()

    @pyqtSlot()
    def dismissRefuel(self) -> None:
        fuel_ledger.dismiss_refuel()
        self._load_refuel()

    @pyqtSlot(float)
    def manualRefuel(self, level_pct) -> None:
        """"I refuelled" — `level_pct` is the gauge's current reading."""
        if level_pct <= 0:
            return
        fuel_ledger.manual_refuel(float(level_pct))
        self._load_refuel()
