"""Fuel ledger: what the fuel in the tank cost, refuels, and finished trips.

Persisted to FUEL_LEDGER_PATH (~/Documents, outside the repo — deploys never
touch it), written atomically. All public functions are thread-safe; the
Trip recorder calls them from the engine poll, the Trip bridge from the Qt
thread.

Price model: the tank holds a blend. A refuel from `before`% to `after`% at
`price` €/L makes the tank's average
    (before * old_avg + (after - before) * price) / after
so half a tank at 2.00 topped up with half at 2.40 averages 2.20, and a
trip's cost is litres used x that average.

Litres are an engine-side estimate (trip_recorder) scaled by `k_flow`, which
each refuel re-learns from how far the gauge really dropped since the last
one.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from Modules.Runtime import settings_cache
from Modules.Runtime import vehicle_profile as _vp
from preferences import DATA_SETTINGS, FUEL_LEDGER_PATH

_DEFAULT_TANK_L = float(_vp.get("tank_litres"))  # vehicle profile
_K_MIN, _K_MAX = 0.6, 1.6
_K_MIN_SPAN_PCT = 20.0          # re-learn k only over >= this much tank
_TRIPS_KEEP_S = 2 * 366 * 86400
_FILLS_KEEP = 200
_CFG_TTL_S = 5.0

_lock = threading.RLock()
_state: Optional[Dict[str, Any]] = None
_cfg_cache: Dict[str, float] = {}
_cfg_ts: float = 0.0


def _blank() -> Dict[str, Any]:
    return {
        "version": 1,
        "tank_avg_price": None,
        "last_price": None,
        "k_flow": 1.0,
        "est_raw_since_fill": 0.0,
        "fuel_at_stop_pct": None,
        "pending_refuel": None,
        "fills": [],
        "trips": [],
        "open_trip": None,
    }


def _cfg(key: str, default: float, lo: float, hi: float) -> float:
    global _cfg_ts
    now = time.monotonic()
    if now - _cfg_ts > _CFG_TTL_S:
        _cfg_cache.clear()
        _cfg_ts = now
    if key not in _cfg_cache:
        try:
            v = settings_cache.getfloat("Fuel", key, fallback=default, path=DATA_SETTINGS)
        except Exception:
            v = default
        _cfg_cache[key] = v if lo <= v <= hi else default
    return _cfg_cache[key]


def tank_litres() -> float:
    return _cfg("tank_litres", _DEFAULT_TANK_L, 10.0, 120.0)


def refuel_min_pct() -> float:
    return _cfg("refuel_min_pct", 8.0, 2.0, 50.0)


def trip_merge_s() -> float:
    return 60.0 * _cfg("trip_merge_min", 10.0, 0.0, 240.0)


# ── persistence ──────────────────────────────────────────────────────────
def _load() -> Dict[str, Any]:
    global _state
    if _state is not None:
        return _state
    st = _blank()
    try:
        with open(FUEL_LEDGER_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in st:
                if k in data:
                    st[k] = data[k]
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"[fuel] ledger unreadable ({exc}); starting empty", flush=True)
    if not isinstance(st.get("fills"), list):
        st["fills"] = []
    if not isinstance(st.get("trips"), list):
        st["trips"] = []
    _state = st
    return st


def save() -> None:
    with _lock:
        st = _load()
        tmp = FUEL_LEDGER_PATH + ".tmp"
        try:
            os.makedirs(os.path.dirname(FUEL_LEDGER_PATH), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, FUEL_LEDGER_PATH)
        except Exception as exc:
            print(f"[fuel] ledger save failed: {exc}", flush=True)


# ── prices ───────────────────────────────────────────────────────────────
def tank_avg_price() -> Optional[float]:
    with _lock:
        v = _load().get("tank_avg_price")
        return float(v) if v is not None else None


def last_price() -> Optional[float]:
    with _lock:
        v = _load().get("last_price")
        return float(v) if v is not None else None


def k_flow() -> float:
    with _lock:
        return float(_load().get("k_flow") or 1.0)


def add_estimated_raw(litres: float) -> None:
    """Uncorrected estimated litres burnt, for the next k_flow re-learn."""
    if litres <= 0:
        return
    with _lock:
        st = _load()
        st["est_raw_since_fill"] = float(st.get("est_raw_since_fill") or 0.0) + float(litres)


# ── refuel ───────────────────────────────────────────────────────────────
def set_fuel_at_stop(pct: Optional[float]) -> None:
    with _lock:
        _load()["fuel_at_stop_pct"] = None if pct is None else float(pct)
        save()


def fuel_at_stop() -> Optional[float]:
    with _lock:
        v = _load().get("fuel_at_stop_pct")
        return float(v) if v is not None else None


def pending_refuel() -> Optional[Dict[str, Any]]:
    with _lock:
        p = _load().get("pending_refuel")
        return dict(p) if isinstance(p, dict) else None


def set_pending_refuel(pct_before: float, pct_after: float, ts: Optional[float] = None) -> None:
    with _lock:
        st = _load()
        st["pending_refuel"] = {
            "ts": float(ts if ts is not None else time.time()),
            "pct_before": round(float(pct_before), 1),
            "pct_after": round(float(pct_after), 1),
        }
        # The level is now the new one: a later check must compare with it.
        st["fuel_at_stop_pct"] = float(pct_after)
        save()


def dismiss_refuel() -> None:
    with _lock:
        _load()["pending_refuel"] = None
        save()


def confirm_refuel(price: float, odo_km: Optional[float] = None) -> Dict[str, Any]:
    """Apply the pending refuel at `price` €/L. Returns the fill record."""
    price = round(max(0.0, float(price)), 3)
    with _lock:
        st = _load()
        p = st.get("pending_refuel")
        if not isinstance(p, dict) or price <= 0:
            return {}
        before = max(0.0, float(p["pct_before"]))
        after = max(before, float(p["pct_after"]))
        tank = tank_litres()
        added_pct = after - before

        old = st.get("tank_avg_price")
        if old is None or after <= 0:
            avg = price
        else:
            avg = (before * float(old) + added_pct * price) / after
        st["tank_avg_price"] = round(avg, 4)
        st["last_price"] = price

        # Re-learn the estimate's scale from the gauge drop since the last fill.
        fills = st["fills"]
        est = float(st.get("est_raw_since_fill") or 0.0)
        if fills:
            span_pct = float(fills[-1]["pct_after"]) - before
            gauge_l = span_pct * tank / 100.0
            if span_pct >= _K_MIN_SPAN_PCT and est > 1.0:
                ratio = max(_K_MIN, min(_K_MAX, gauge_l / est))
                k_old = float(st.get("k_flow") or 1.0)
                st["k_flow"] = round(max(_K_MIN, min(_K_MAX, 0.5 * k_old + 0.5 * ratio)), 4)
                print(f"[fuel] k_flow {k_old:.3f} -> {st['k_flow']:.3f} "
                      f"(gauge {gauge_l:.1f} L vs estimate {est:.1f} L)", flush=True)
        st["est_raw_since_fill"] = 0.0

        from Modules.Settings.trips_store import _today_key
        fill = {
            "ts": float(p.get("ts") or time.time()),
            "day_key": _today_key(float(p.get("ts") or time.time())),
            "pct_before": before,
            "pct_after": after,
            "litres": round(added_pct * tank / 100.0, 2),
            "price": price,
            "odo_km": None if odo_km is None else round(float(odo_km), 1),
        }
        fills.append(fill)
        del fills[:-_FILLS_KEEP]
        st["pending_refuel"] = None
        save()
        print(f"[fuel] refuel {before:.0f}% -> {after:.0f}% ({fill['litres']:.1f} L) "
              f"at {price:.3f} €/L; tank average now {avg:.3f} €/L", flush=True)
        return fill


def manual_refuel(pct_after: float) -> None:
    """The user says they refuelled and the detector missed it."""
    with _lock:
        before = fuel_at_stop()
        if before is None:
            before = max(0.0, float(pct_after) - refuel_min_pct())
        set_pending_refuel(min(before, float(pct_after)), float(pct_after))


# ── trips ────────────────────────────────────────────────────────────────
def open_trip() -> Optional[Dict[str, Any]]:
    with _lock:
        t = _load().get("open_trip")
        return dict(t) if isinstance(t, dict) else None


def checkpoint_open_trip(trip: Optional[Dict[str, Any]]) -> None:
    with _lock:
        _load()["open_trip"] = trip
        save()


def add_trip(trip: Dict[str, Any]) -> None:
    with _lock:
        st = _load()
        trips = st["trips"]
        trips.append(trip)
        cutoff = time.time() - _TRIPS_KEEP_S
        st["trips"] = [t for t in trips if float(t.get("end_ts") or 0) >= cutoff] or [trip]
        st["open_trip"] = None
        save()


def recent_trips(n: int = 5) -> List[Dict[str, Any]]:
    with _lock:
        return [dict(t) for t in _load()["trips"][-n:]]


def last_trip() -> Optional[Dict[str, Any]]:
    t = recent_trips(1)
    return t[0] if t else None


# ── periods ──────────────────────────────────────────────────────────────
def _day(day_key: str) -> Optional[date]:
    try:
        return datetime.strptime(str(day_key), "%Y-%m-%d").date()
    except Exception:
        return None


def _bucket() -> Dict[str, float]:
    return {"km": 0.0, "litres": 0.0, "cost": 0.0, "trips": 0, "priced_litres": 0.0}


def _finish(b: Dict[str, float]) -> Dict[str, Any]:
    km, litres, cost = b["km"], b["litres"], b["cost"]
    return {
        "km": round(km, 1),
        "litres": round(litres, 2),
        "cost": round(cost, 2),
        "trips": int(b["trips"]),
        "l100": round(100.0 * litres / km, 1) if km >= 1.0 else None,
        "avg_price": round(cost / b["priced_litres"], 3) if b["priced_litres"] > 0.05 else None,
    }


def periods(extra: Optional[Dict[str, Any]] = None, today_key: Optional[str] = None) -> Dict[str, Any]:
    """Today / this week / last week / this month / last month.

    `extra` is the open trip (not yet in the ledger), counted in today.
    Weeks are ISO weeks; days follow the 04:00 day key.
    """
    from Modules.Settings.trips_store import _today_key
    today = _day(today_key or _today_key()) or date.today()
    week0 = today - timedelta(days=today.weekday())
    lastweek0 = week0 - timedelta(days=7)
    month0 = today.replace(day=1)
    lastmonth0 = (month0 - timedelta(days=1)).replace(day=1)
    out = {k: _bucket() for k in ("today", "week", "last_week", "month", "last_month")}

    with _lock:
        rows = list(_load()["trips"])
    if extra:
        rows.append(extra)
    for t in rows:
        d = _day(t.get("day_key", ""))
        if d is None:
            continue
        keys = []
        if d == today:
            keys.append("today")
        if week0 <= d <= today:
            keys.append("week")
        elif lastweek0 <= d < week0:
            keys.append("last_week")
        if month0 <= d <= today:
            keys.append("month")
        elif lastmonth0 <= d < month0:
            keys.append("last_month")
        for k in keys:
            b = out[k]
            b["km"] += float(t.get("km") or 0.0)
            b["litres"] += float(t.get("litres") or 0.0)
            b["cost"] += float(t.get("cost") or 0.0)
            b["priced_litres"] += float(t.get("priced_litres") or 0.0)
            b["trips"] += 1
    return {k: _finish(v) for k, v in out.items()}
