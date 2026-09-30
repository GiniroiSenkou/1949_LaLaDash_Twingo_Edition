"""Trip recorder: one trip's distance, fuel, cost, eco scores and checks.

Fed from `_EngineStatePoll.poll()` (main.py) every 350 ms with the telemetry
snapshot. Everything per tick is arithmetic on a handful of floats; the only
I/O is a ledger checkpoint every 30 s and at engine transitions.

A trip starts at engine-on and pauses at engine-off. Restarting within
`[Fuel] trip_merge_min` (default 10 min — a fuel stop, a shop) continues it;
a longer stop finalises it into the fuel ledger. Across a Pi reboot the gap
is only trusted when the clock is synchronised — with no RTC the boot
restores an old time, so an unsynced reboot always starts a new trip.

Fuel is estimated speed-density style from MAP, intake temperature and RPM
for the profile's engine (default: 0.9 TCe, 898 cc), scaled by the ledger's `k_flow`, which
every refuel re-learns from the gauge. The scores and checks are heuristics
on OBD data — "worth checking", never a diagnosis.

Refuel: at engine-off the unsmoothed tank level is stored. Around a stop the
hub reads the tank every 5 s (`set_fuel_watch`); a rise past
`[Fuel] refuel_min_pct` held for two readings is a refuel.
"""
from __future__ import annotations

import math
import os
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional

from Modules.Settings import fuel_ledger

# ── engine model (from the vehicle profile; reference: 0.9 L turbo petrol) ──
from Modules.Runtime import vehicle_profile as _vp

_DISPLACEMENT_M3 = float(_vp.get("engine.displacement_m3"))
_VE = float(_vp.get("engine.volumetric_efficiency"))
_R_AIR = 287.05
_AFR_STOICH = float(_vp.get("engine.afr_stoich"))
_PETROL_KG_PER_L = float(_vp.get("engine.petrol_kg_per_l"))

_CHECKPOINT_S = 30.0
_WATCH_AFTER_START_S = 180.0
_WATCH_AFTER_STOP_S = 1200.0    # a refuel stop; past that the ECU is asleep anyway
_REFUEL_HOLD_S = 12.0           # > two 5 s fuel-watch samples
_RECAP_MIN_KM = 0.5

# eco thresholds
_HARSH_ACC_KMHS = 7.0
_HARD_BRAKE_KMHS = -10.0
_HIGH_SHIFT_RPM = 3000.0
_LUG_RPM = 1300.0
_PEDAL_HIGH = 70.0

NOTE_TEXT = {
    "lean":       ("Air intake / lean mixture",
                   "Long-term fuel trim stayed above +10 %: the ECU keeps adding fuel. "
                   "Worth checking for an intake air leak, the MAP sensor or the hoses."),
    "rich":       ("Rich mixture",
                   "Long-term fuel trim stayed below −10 %: the ECU keeps removing fuel. "
                   "Worth checking injectors, the purge valve or the O₂ sensor."),
    "idle":       ("Unsteady idle",
                   "Warm idle RPM wandered by more than ±80 rpm. Often a dirty throttle "
                   "body or a small intake leak."),
    "volt_low":   ("Low charging voltage",
                   "System voltage averaged under 12.4 V with the engine running. "
                   "Worth checking the battery and alternator."),
    "volt_high":  ("High charging voltage",
                   "System voltage averaged over 15.0 V with the engine running. "
                   "Worth checking the alternator's regulator."),
    "warmup":     ("Slow warm-up",
                   "Coolant was still under 80 °C after 15 minutes of driving. "
                   "In mild weather that points at the thermostat."),
    "hot":        ("Coolant ran hot",
                   "Coolant peaked above 105 °C. Check the level and the radiator fan."),
    "mil":        ("Engine warning light",
                   "The check-engine light came on during this trip. Read the stored codes."),
}

TIPS = {
    "acc":   "Accelerate more gently: most fuel goes into hard pulls.",
    "gear":  "Change up earlier — around 2 000–2 500 rpm — and avoid labouring in a high gear.",
    "antic": "Look further ahead: lift off early and let the car roll instead of braking late.",
    "idle":  "Switch the engine off during long waits.",
}


def _boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except Exception:
        return ""


def _clock_synced() -> bool:
    return os.path.exists("/run/systemd/timesync/synchronized")


def _num(snap: Dict[str, Any], key: str) -> Optional[float]:
    e = snap.get(key)
    v = e.get("value") if isinstance(e, dict) else e
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _fuel_sample(snap: Dict[str, Any]) -> Optional[float]:
    e = snap.get("Fuel Level")
    if isinstance(e, dict):
        v = e.get("calibrated_value")
        if v is None:
            v = e.get("value")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    return None


class _Welford:
    __slots__ = ("n", "mean", "m2")

    def __init__(self, n=0, mean=0.0, m2=0.0):
        self.n, self.mean, self.m2 = int(n), float(mean), float(m2)

    def add(self, x: float) -> None:
        self.n += 1
        d = x - self.mean
        self.mean += d / self.n
        self.m2 += d * (x - self.mean)

    @property
    def std(self) -> float:
        return math.sqrt(self.m2 / (self.n - 1)) if self.n > 1 else 0.0

    def dump(self):
        return [self.n, self.mean, self.m2]


def _new_trip(now: float) -> Dict[str, Any]:
    from Modules.Settings.trips_store import _today_key
    return {
        "start_ts": now, "end_ts": now, "last_ts": now, "off_since": None,
        "day_key": _today_key(now), "boot_id": _boot_id(),
        "km": 0.0, "drive_s": 0.0, "idle_s": 0.0, "moving_s": 0.0,
        "litres": 0.0, "priced_litres": 0.0, "cost": 0.0, "max_speed": 0.0,
        "acc_events": 0, "brake_events": 0, "pedal_high_s": 0.0,
        "upshifts": 0, "high_upshifts": 0, "lug_s": 0.0, "coast_s": 0.0,
        "coolant_peak": None, "cold_after_15": False, "mil": False,
        "trim": [0, 0.0, 0.0], "idle_rpm": [0, 0.0, 0.0], "volt": [0, 0.0, 0.0],
        "segments": 1,
    }


class TripRecorder:
    """Owned by main.py; `feed()` on every engine poll."""

    def __init__(self) -> None:
        self.trip: Optional[Dict[str, Any]] = fuel_ledger.open_trip()
        self.on_trip_paused: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_trip_finished: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_refuel: Optional[Callable[[Dict[str, Any]], None]] = None
        self.fuel_watch_changed: Optional[Callable[[bool], None]] = None

        self._engine_on = False
        self._engine_on_since = 0.0
        self._engine_off_since = time.time()
        self._last_tick: Optional[float] = None
        self._last_checkpoint = 0.0
        self._est_raw_unflushed = 0.0
        self._speed_hist: deque = deque()
        self._acc_armed = True
        self._brake_armed = True
        self._prev_gear: Optional[int] = None
        self._prev_rpm = 0.0
        self._last_speed: Optional[float] = None
        self._refuel_since: Optional[float] = None
        self._watch = False
        self._last_fuel: Optional[float] = None

        if self.trip is not None:
            now = time.time()
            stale = (now - float(self.trip.get("last_ts") or 0)) > fuel_ledger.trip_merge_s()
            rebooted_blind = self.trip.get("boot_id") != _boot_id() and not _clock_synced()
            if stale or rebooted_blind or now < float(self.trip.get("last_ts") or 0) - 60:
                self._finish(now, reason="boot")
            else:
                self.trip["off_since"] = self.trip.get("off_since") or now

    # ── public ──────────────────────────────────────────────────────────
    def feed(self, snap: Dict[str, Any], engine_on: bool, live: bool,
             now: Optional[float] = None, mil: bool = False) -> None:
        """`live`: real OBD data (not the simulator, not disconnected)."""
        now = time.time() if now is None else now
        snap = snap or {}
        dt = 0.0 if self._last_tick is None else max(0.0, min(2.0, now - self._last_tick))
        self._last_tick = now

        fuel = _fuel_sample(snap) if live else None
        if fuel is not None:
            self._last_fuel = fuel

        if engine_on != self._engine_on:
            self._engine_on = engine_on
            if engine_on:
                self._engine_on_since = now
                self._on_engine_start(now)
            else:
                self._engine_off_since = now
                self._on_engine_stop(now)

        self._update_watch(now, live)
        if fuel is not None and self._watch:
            self._check_refuel(fuel, now)

        t = self.trip
        if t is not None and not engine_on and t.get("off_since"):
            if now - float(t["off_since"]) > fuel_ledger.trip_merge_s():
                self._finish(now, reason="stop")
                return

        if t is None or not engine_on or not live or dt <= 0.0:
            return
        self._accumulate(t, snap, dt, now, mil)
        if now - self._last_checkpoint >= _CHECKPOINT_S:
            self._checkpoint(now)

    def live_summary(self) -> Optional[Dict[str, Any]]:
        return summarize(self.trip) if self.trip else None

    def open_trip_for_periods(self) -> Optional[Dict[str, Any]]:
        t = self.trip
        if not t:
            return None
        return {"day_key": t["day_key"], "km": t["km"], "litres": t["litres"],
                "cost": t["cost"], "priced_litres": t["priced_litres"]}

    def flush(self) -> None:
        """App shutdown: keep the open trip for the next start."""
        if self.trip is not None:
            self._checkpoint(time.time())

    # ── engine transitions ──────────────────────────────────────────────
    def _on_engine_start(self, now: float) -> None:
        t = self.trip
        if t is not None and t.get("off_since"):
            if now - float(t["off_since"]) <= fuel_ledger.trip_merge_s():
                t["off_since"] = None
                t["segments"] = int(t.get("segments", 1)) + 1
            else:
                self._finish(now, reason="stop")
                t = None
        if self.trip is None:
            self.trip = _new_trip(now)
        self.trip["off_since"] = None
        self._speed_hist.clear()
        self._prev_gear = None
        self._last_speed = None
        self._checkpoint(now)

    def _on_engine_stop(self, now: float) -> None:
        if self._last_fuel is not None:
            fuel_ledger.set_fuel_at_stop(self._last_fuel)
        t = self.trip
        if t is None:
            return
        t["off_since"] = now
        t["end_ts"] = now
        self._checkpoint(now)
        if self.on_trip_paused and float(t["km"]) >= _RECAP_MIN_KM:
            self.on_trip_paused(summarize(t))

    def _finish(self, now: float, reason: str) -> None:
        t = self.trip
        self.trip = None
        if t is None:
            return
        self._flush_estimate()
        if float(t.get("km") or 0) < 0.05 and float(t.get("litres") or 0) < 0.01:
            fuel_ledger.checkpoint_open_trip(None)
            return
        rec = summarize(t)
        recent = fuel_ledger.recent_trips(4)
        rec["recurring"] = _recurring(rec["notes"], recent)
        fuel_ledger.add_trip(rec)
        print(f"[trip] finished ({reason}): {rec['km']:.1f} km, {rec['litres']:.2f} L, "
              f"{rec['cost']:.2f} €, eco {rec['scores']['overall']}", flush=True)
        if self.on_trip_finished:
            self.on_trip_finished(rec)

    # ── refuel ──────────────────────────────────────────────────────────
    def _update_watch(self, now: float, live: bool) -> None:
        if self._engine_on:
            want = live and (now - self._engine_on_since) < _WATCH_AFTER_START_S
        else:
            want = live and (now - self._engine_off_since) < _WATCH_AFTER_STOP_S
        if want != self._watch:
            self._watch = want
            self._refuel_since = None
            if self.fuel_watch_changed:
                self.fuel_watch_changed(want)

    def _check_refuel(self, fuel: float, now: float) -> None:
        base = fuel_ledger.fuel_at_stop()
        if base is None or fuel_ledger.pending_refuel() is not None:
            return
        if fuel >= base + fuel_ledger.refuel_min_pct():
            if self._refuel_since is None:
                self._refuel_since = now
            elif now - self._refuel_since >= _REFUEL_HOLD_S:
                self._refuel_since = None
                fuel_ledger.set_pending_refuel(base, fuel, now)
                print(f"[fuel] refuel detected {base:.0f}% -> {fuel:.0f}%", flush=True)
                if self.on_refuel:
                    self.on_refuel(fuel_ledger.pending_refuel() or {})
        else:
            self._refuel_since = None

    # ── per tick ────────────────────────────────────────────────────────
    def _accumulate(self, t: Dict[str, Any], snap: Dict[str, Any], dt: float,
                    now: float, mil: bool) -> None:
        rpm = _num(snap, "RPM") or 0.0
        speed = max(0.0, _num(snap, "Speed") or 0.0)
        t["last_ts"] = now
        t["end_ts"] = now

        prev = self._last_speed if self._last_speed is not None else speed
        t["km"] += (prev + speed) * 0.5 * dt / 3600.0
        self._last_speed = speed
        t["drive_s"] += dt
        moving = speed >= 3.0
        if moving:
            t["moving_s"] += dt
        elif rpm > 300:
            t["idle_s"] += dt
        t["max_speed"] = max(float(t["max_speed"]), speed)

        # fuel
        lph_raw, cut = self._fuel_rate(snap, rpm, speed)
        litres_raw = lph_raw * dt / 3600.0
        self._est_raw_unflushed += litres_raw
        litres = litres_raw * fuel_ledger.k_flow()
        t["litres"] += litres
        price = fuel_ledger.tank_avg_price()
        if price is not None:
            t["cost"] += litres * price
            t["priced_litres"] += litres
        if cut and moving:
            t["coast_s"] += dt

        # acceleration from ~1 s of speed history
        self._speed_hist.append((now, speed))
        while self._speed_hist and now - self._speed_hist[0][0] > 1.5:
            self._speed_hist.popleft()
        t0, v0 = self._speed_hist[0]
        if now - t0 >= 0.8:
            acc = (speed - v0) / (now - t0)
            if acc >= _HARSH_ACC_KMHS:
                if self._acc_armed:
                    t["acc_events"] += 1
                    self._acc_armed = False
            elif acc < _HARSH_ACC_KMHS * 0.5:
                self._acc_armed = True
            if acc <= _HARD_BRAKE_KMHS:
                if self._brake_armed:
                    t["brake_events"] += 1
                    self._brake_armed = False
            elif acc > _HARD_BRAKE_KMHS * 0.5:
                self._brake_armed = True

        pedal = _num(snap, "Accel Pedal D")
        if pedal is None:
            pedal = _num(snap, "Throttle Pos")
        if moving and pedal is not None and pedal >= _PEDAL_HIGH:
            t["pedal_high_s"] += dt

        # gears
        gear = None
        g = _num(snap, "Gear")
        if g is not None and g >= 1:
            gear = int(g)
        if gear is not None and self._prev_gear is not None and gear == self._prev_gear + 1:
            t["upshifts"] += 1
            if self._prev_rpm >= _HIGH_SHIFT_RPM:
                t["high_upshifts"] += 1
        if gear is not None:
            self._prev_gear = gear
        mapk = _num(snap, "Manifold Press")
        if gear is not None and gear >= 3 and speed > 15 and rpm < _LUG_RPM and (mapk or 0) >= 90:
            t["lug_s"] += dt
        self._prev_rpm = rpm

        # checks
        cool = _num(snap, "Coolant")
        if cool is not None:
            t["coolant_peak"] = cool if t["coolant_peak"] is None else max(t["coolant_peak"], cool)
            if t["moving_s"] >= 900 and cool < 80 and not t["cold_after_15"] and t["moving_s"] < 905:
                t["cold_after_15"] = True
        warm = cool is not None and cool >= 70
        trim_l = _num(snap, "Fuel Trim L")
        trim_s = _num(snap, "Fuel Trim S")
        if warm and trim_l is not None:
            w = _Welford(*t["trim"])
            w.add(trim_l + (trim_s or 0.0))
            t["trim"] = w.dump()
        if warm and not moving and 500 < rpm < 1400:
            w = _Welford(*t["idle_rpm"])
            w.add(rpm)
            t["idle_rpm"] = w.dump()
        volt = _num(snap, "ECU Voltage")
        if volt is not None and rpm > 500 and t["drive_s"] > 120:
            w = _Welford(*t["volt"])
            w.add(volt)
            t["volt"] = w.dump()
        if mil:
            t["mil"] = True

    @staticmethod
    def _fuel_rate(snap: Dict[str, Any], rpm: float, speed: float):
        """(litres/hour before k_flow, overrun fuel cut?)"""
        mapk = _num(snap, "Manifold Press")
        if rpm < 300 or mapk is None or mapk <= 0:
            return 0.0, False
        phi = _num(snap, "Equiv. Ratio")
        if (phi is not None and phi < 0.2) or (mapk < 28.0 and rpm > 1400 and speed > 20):
            return 0.0, True
        iat = _num(snap, "Intake Air")
        t_k = 273.15 + (iat if iat is not None else 25.0)
        air_kg_s = (mapk * 1000.0) * _DISPLACEMENT_M3 * (rpm / 120.0) * _VE / (_R_AIR * t_k)
        lam = phi if (phi is not None and 0.6 < phi < 1.5) else 1.0
        fuel_kg_s = air_kg_s * lam / _AFR_STOICH
        return fuel_kg_s / _PETROL_KG_PER_L * 3600.0, False

    def _flush_estimate(self) -> None:
        if self._est_raw_unflushed > 0:
            fuel_ledger.add_estimated_raw(self._est_raw_unflushed)
            self._est_raw_unflushed = 0.0

    def _checkpoint(self, now: float) -> None:
        self._last_checkpoint = now
        self._flush_estimate()
        fuel_ledger.checkpoint_open_trip(self.trip)


# ── summaries ────────────────────────────────────────────────────────────
def _clamp(v: float) -> int:
    return int(round(max(0.0, min(100.0, v))))


def scores(t: Dict[str, Any]) -> Dict[str, int]:
    # Events per 10 min of driving, but never scaled up from a shorter trip:
    # one brisk pull on a 2 min hop is not five.
    moving_min = max(10.0, float(t.get("moving_s") or 0) / 60.0)
    moving_s = max(60.0, float(t.get("moving_s") or 0))
    per10 = 10.0 / moving_min
    acc = 100 - 12 * t["acc_events"] * per10 - 150 * t["pedal_high_s"] / moving_s
    ups = int(t.get("upshifts") or 0)
    high_share = (t["high_upshifts"] / ups) if ups else 0.0
    gear = 100 - 70 * high_share - 150 * t["lug_s"] / moving_s
    antic = 100 - 20 * t["brake_events"] * per10 + 30 * min(0.3, t["coast_s"] / moving_s)
    total = max(1.0, float(t["moving_s"]) + float(t["idle_s"]))
    idle = 100 - 200 * max(0.0, t["idle_s"] / total - 0.10)
    out = {"acc": _clamp(acc), "gear": _clamp(gear), "antic": _clamp(antic), "idle": _clamp(idle)}
    out["overall"] = _clamp(sum(out.values()) / 4.0)
    return out


def notes(t: Dict[str, Any]) -> List[str]:
    found: List[str] = []
    n, mean, _ = t.get("trim") or [0, 0, 0]
    if n >= 20:
        if mean > 10:
            found.append("lean")
        elif mean < -10:
            found.append("rich")
    w = _Welford(*(t.get("idle_rpm") or [0, 0, 0]))
    if w.n >= 60 and w.std > 80:
        found.append("idle")
    n, mean, _ = t.get("volt") or [0, 0, 0]
    if n >= 30:
        if mean < 12.4:
            found.append("volt_low")
        elif mean > 15.0:
            found.append("volt_high")
    if t.get("cold_after_15"):
        found.append("warmup")
    if (t.get("coolant_peak") or 0) > 105:
        found.append("hot")
    if t.get("mil"):
        found.append("mil")
    return found


def _recurring(current: List[str], recent: List[Dict[str, Any]]) -> List[str]:
    out = []
    for k in current:
        seen = 1 + sum(1 for r in recent if k in (r.get("notes") or []))
        if seen >= 3:
            out.append(k)
    return out


def summarize(t: Dict[str, Any]) -> Dict[str, Any]:
    km = float(t.get("km") or 0)
    litres = float(t.get("litres") or 0)
    sc = scores(t)
    worst = min(("acc", "gear", "antic", "idle"), key=lambda k: sc[k])
    moving_h = float(t.get("moving_s") or 0) / 3600.0
    return {
        "start_ts": t["start_ts"], "end_ts": t.get("end_ts") or t["start_ts"],
        "day_key": t["day_key"],
        "km": round(km, 2), "litres": round(litres, 3),
        "priced_litres": round(float(t.get("priced_litres") or 0), 3),
        "cost": round(float(t.get("cost") or 0), 2),
        "l100": round(100.0 * litres / km, 1) if km >= 1.0 else None,
        "drive_s": int(t.get("drive_s") or 0), "idle_s": int(t.get("idle_s") or 0),
        "avg_speed": round(km / moving_h, 0) if moving_h > 0.01 else 0,
        "max_speed": round(float(t.get("max_speed") or 0), 0),
        "acc_events": int(t["acc_events"]), "brake_events": int(t["brake_events"]),
        "upshifts": int(t["upshifts"]), "high_upshifts": int(t["high_upshifts"]),
        "segments": int(t.get("segments", 1)),
        "scores": sc, "tip": TIPS[worst] if sc[worst] < 85 else "",
        "notes": notes(t), "recurring": list(t.get("recurring") or []),
    }
