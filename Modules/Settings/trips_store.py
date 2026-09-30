from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from Modules.Runtime import settings_cache
from preferences import (
    DATA_SETTINGS,
    KM_INITIAL_TOTAL_KM,
    KM_STORE_PATH,
    KM_TOTAL_TXT_PATH,
)

_MAX_DT_SECONDS = 10.0
_SAVE_INTERVAL_SECONDS = 5.0
# How often _ensure_state() re-stats the total-km txt file to notice a manual
# hand-edit. Not every call — this runs on the 350ms engine poll plus every
# telemetry-driven read, so a stat() on every single call would be needless
# syscall traffic for a file that changes only when a human edits it.
_TOTAL_KM_STAT_TTL_S = 1.0

# A "day" ends at 04:00, not midnight: driving home at 01:30 is still the
# same evening out, and a trip readout that zeroes itself halfway through
# the drive home is worse than useless. [Trips] day_start_hour overrides it.
_DEFAULT_DAY_START_HOUR = 4
_DAY_START_TTL_S = 5.0

# The Pi has no RTC and no network in the car, so a boot restores whatever
# time was last saved to disk. If the clock comes back up *behind* where we
# left it, the date is not a usable day key and "today" will keep stacking
# across real days — say so once in the log rather than fail silently.
_CLOCK_BACKSTEP_TOLERANCE_S = 60.0

_state: Dict[str, Any] | None = None
_dirty: bool = False
_last_save_ts: float = 0.0

# Separate from `_state`/`_dirty`: the total-km txt file is written on its own
# schedule (immediately on a deliberate edit, throttled like everything else
# for the continuous odometer accumulation) and read back with its own mtime
# check, independent of the JSON blob's dirty-tracking.
_total_km_mtime_ns: int = -1
_total_km_checked_ts: float = 0.0

_day_start_hour_cache: int = _DEFAULT_DAY_START_HOUR
_day_start_checked_ts: float = 0.0
_clock_warned: bool = False


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _day_start_hour() -> int:
    """Hour the trip day rolls over at, 0-12, default 4.

    Cached for a few seconds: _today_key() runs on the 350ms engine poll, and
    settings_cache re-stats the INI on its own anyway - this just keeps the
    parser lookup off a hot path for a value that changes about never.
    """
    global _day_start_hour_cache, _day_start_checked_ts
    now = time.monotonic()
    if (now - _day_start_checked_ts) < _DAY_START_TTL_S:
        return _day_start_hour_cache
    _day_start_checked_ts = now
    try:
        hour = int(
            settings_cache.getint(
                "Trips",
                "day_start_hour",
                fallback=_DEFAULT_DAY_START_HOUR,
                path=DATA_SETTINGS,
            )
        )
    except Exception:
        hour = _DEFAULT_DAY_START_HOUR
    if not (0 <= hour <= 12):
        hour = _DEFAULT_DAY_START_HOUR
    _day_start_hour_cache = hour
    return hour


def _today_key(now_ts: Optional[float] = None, hour: Optional[int] = None) -> str:
    """Which trip-day a timestamp belongs to.

    Shifted back by _day_start_hour() so the boundary is 04:00 rather than
    midnight - 00:30 on Tuesday still keys as Monday, which is what a driver
    coming home late actually means by "today". `hour` overrides the setting,
    which is how _roll_day_if_needed() re-evaluates the stored key under the
    boundary it was originally written with.
    """
    ts = time.time() if now_ts is None else float(now_ts)
    offset = timedelta(hours=_day_start_hour() if hour is None else int(hour))
    try:
        return (datetime.fromtimestamp(ts) - offset).strftime("%Y-%m-%d")
    except Exception:
        return (datetime.now() - offset).strftime("%Y-%m-%d")


def _load_file() -> Optional[Dict[str, Any]]:
    if not os.path.exists(KM_STORE_PATH):
        return None
    try:
        with open(KM_STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_file(state: Dict[str, Any]) -> None:
    # total_km is deliberately NOT in this payload — it lives in its own
    # plain-text file (see _load_total_km_txt/_write_total_km_txt) so it can
    # be hand-edited without touching this JSON's other bookkeeping fields.
    try:
        os.makedirs(os.path.dirname(KM_STORE_PATH), exist_ok=True)
    except Exception:
        pass
    last_fuel = state.get("last_fuel_level_pct")
    payload = {
        "today_km": float(state.get("today_km", 0.0)),
        "today_key": str(state.get("today_key", _today_key())),
        "today_started_ts": float(state.get("today_started_ts", 0.0)),
        "day_start_hour": int(state.get("day_start_hour", _DEFAULT_DAY_START_HOUR)),
        "today_fuel_consumption": str(state.get("today_fuel_consumption", "N/A") or "N/A"),
        "today_fuel_used_pct_hook": float(state.get("today_fuel_used_pct_hook", 0.0)),
        "last_speed_kmh": float(state.get("last_speed_kmh", 0.0)),
        "last_update_ts": float(state.get("last_update_ts", time.time())),
        "last_fuel_level_pct": None if last_fuel is None else float(last_fuel),
    }
    tmp = KM_STORE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, KM_STORE_PATH)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _total_km_mtime_now() -> int:
    try:
        return os.stat(KM_TOTAL_TXT_PATH).st_mtime_ns
    except OSError:
        return -1


def _write_total_km_txt(value: float) -> None:
    global _total_km_mtime_ns
    try:
        os.makedirs(os.path.dirname(KM_TOTAL_TXT_PATH), exist_ok=True)
    except Exception:
        pass
    tmp = KM_TOTAL_TXT_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(f"{max(0.0, float(value)):.3f}\n")
        os.replace(tmp, KM_TOTAL_TXT_PATH)
        _total_km_mtime_ns = _total_km_mtime_now()
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _load_total_km_txt() -> Optional[float]:
    """Tolerant read: whatever a human left in the file, as long as a float
    is in there somewhere — a hand edit shouldn't have to be byte-perfect."""
    try:
        with open(KM_TOTAL_TXT_PATH, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return None
        return max(0.0, float(raw.split()[0]))
    except Exception:
        return None


def _sync_total_km_from_disk(state: Dict[str, Any], *, force: bool = False) -> None:
    """Refresh state['total_km'] from KM_TOTAL_TXT_PATH if it changed on disk.

    Rate-limited like settings_cache's own stat cache: this runs on every
    telemetry-driven read and the 350ms engine poll, and the file only ever
    changes because a human edited it, so re-stat'ing on every single call
    would be pure syscall overhead for no benefit.
    """
    global _total_km_mtime_ns, _total_km_checked_ts
    now = time.monotonic()
    if not force and (now - _total_km_checked_ts) < _TOTAL_KM_STAT_TTL_S:
        return
    _total_km_checked_ts = now
    mtime = _total_km_mtime_now()
    if mtime == -1:
        # File missing (first run, or deleted) — seed it from current state.
        _write_total_km_txt(state.get("total_km", KM_INITIAL_TOTAL_KM))
        return
    if not force and mtime == _total_km_mtime_ns:
        return
    _total_km_mtime_ns = mtime
    value = _load_total_km_txt()
    if value is not None:
        state["total_km"] = value


def _warn_if_clock_went_backwards(now_ts: float, last_ts: float) -> None:
    """Say so, once, when the system clock came back up behind where we left it.

    The Pi has no RTC and usually no network in the car, so a boot restores
    whatever time was last written to disk. When that time is older than our
    own last_update_ts, the calendar date is not advancing between drives and
    _roll_day_if_needed() can never fire - "today" then stacks across real
    days, which looks like a trip counter that never resets. There is nothing
    this module can do about a clock that does not move, but it should not be
    a silent mystery: Settings -> Trips shows the stored day key, and this line
    in the log names the cause.
    """
    global _clock_warned
    if _clock_warned or last_ts <= 0.0:
        return
    behind = last_ts - now_ts
    if behind <= _CLOCK_BACKSTEP_TOLERANCE_S:
        return
    _clock_warned = True
    print(
        f"[trips] system clock is {behind / 3600.0:.1f} h behind the last "
        f"recorded update (now {_fmt_ts(now_ts)}, last {_fmt_ts(last_ts)}) - "
        "no RTC / no time sync. The day key cannot roll over while the clock "
        "stands still, so today's distance will keep accumulating.",
        flush=True,
    )


def _fmt_ts(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


def _ensure_state(now_ts: Optional[float] = None) -> Dict[str, Any]:
    global _state
    ts = time.time() if now_ts is None else float(now_ts)
    if _state is not None:
        _sync_total_km_from_disk(_state)
        return _state
    data = _load_file()
    if data is None:
        # First run on this device: seed with the real car odometer so the
        # dashboard total mirrors the cluster instead of starting at 0.
        _state = {
            "today_km": 0.0,
            "today_key": _today_key(ts),
            "today_started_ts": ts,
            "day_start_hour": _day_start_hour(),
            "today_fuel_consumption": "N/A",
            "today_fuel_used_pct_hook": 0.0,
            "last_speed_kmh": 0.0,
            "last_update_ts": ts,
            "last_fuel_level_pct": None,
            # True until the first telemetry sample lands - see the load
            # branch below for why a fresh state must never integrate.
            "paused_no_real": True,
        }
        _write_file(_state)
    else:
        raw_fuel = data.get("last_fuel_level_pct")
        last_fuel = None
        if raw_fuel is not None and str(raw_fuel).strip() != "":
            last_fuel = max(0.0, min(100.0, _to_float(raw_fuel, 0.0)))
        last_ts = max(0.0, _to_float(data.get("last_update_ts", ts), ts))
        _state = {
            "today_km": max(0.0, _to_float(data.get("today_km", 0.0), 0.0)),
            "today_key": str(data.get("today_key", _today_key(ts)) or _today_key(ts)),
            "today_started_ts": max(0.0, _to_float(data.get("today_started_ts", 0.0), 0.0)),
            "day_start_hour": int(_to_float(data.get("day_start_hour", _day_start_hour()), _DEFAULT_DAY_START_HOUR)),
            "today_fuel_consumption": str(data.get("today_fuel_consumption", "N/A") or "N/A"),
            "today_fuel_used_pct_hook": max(0.0, _to_float(data.get("today_fuel_used_pct_hook", 0.0), 0.0)),
            "last_speed_kmh": max(0.0, _to_float(data.get("last_speed_kmh", 0.0), 0.0)),
            "last_update_ts": last_ts,
            "last_fuel_level_pct": last_fuel,
            # Re-anchor instead of integrating across the gap. last_speed_kmh
            # and last_update_ts come off disk from whenever the app last ran;
            # integrating them against the first live sample credited up to
            # _MAX_DT_SECONDS at the old speed - a phantom ~0.08 km added to
            # today *and* the odometer on every start and every brownout
            # reboot. The first sample now only re-anchors the pair.
            "paused_no_real": True,
        }
        _warn_if_clock_went_backwards(ts, last_ts)
    if not _state.get("today_started_ts"):
        _state["today_started_ts"] = ts
    # total_km comes from its own txt file, not the JSON above — seed it
    # (creating the file if this is a first run) before anything reads it.
    _state["total_km"] = float(KM_INITIAL_TOTAL_KM)
    _sync_total_km_from_disk(_state, force=True)
    return _state


def _roll_day_if_needed(now_ts: Optional[float] = None) -> bool:
    global _dirty
    s = _ensure_state(now_ts)
    cur_key = _today_key(now_ts)
    if s.get("today_key") == cur_key:
        return False
    ts = time.time() if now_ts is None else float(now_ts)

    # Changing [Trips] day_start_hour shifts the key without a real day having
    # passed - at 02:00, moving the boundary from 04:00 to midnight flips the
    # key to tomorrow and would wipe the distance of the drive in progress.
    # If the stored key is still "now" under the boundary it was written with,
    # this is a re-label, not a new day.
    prev_hour = s.get("day_start_hour")
    cur_hour = _day_start_hour()
    if prev_hour is not None and int(prev_hour) != cur_hour:
        if s.get("today_key") == _today_key(ts, int(prev_hour)):
            s["today_key"] = cur_key
            s["day_start_hour"] = cur_hour
            _dirty = True
            return False
    s["day_start_hour"] = cur_hour

    print(
        f"[trips] day rolled {s.get('today_key', '?')} -> {cur_key} at "
        f"{_fmt_ts(ts)} (day starts {_day_start_hour():02d}:00); "
        f"{_to_float(s.get('today_km', 0.0), 0.0):.1f} km banked",
        flush=True,
    )
    s["today_key"] = cur_key
    s["today_started_ts"] = ts
    s["today_km"] = 0.0
    s["today_fuel_consumption"] = "N/A"
    s["today_fuel_used_pct_hook"] = 0.0
    _dirty = True
    return True


def _persist_if_due(now_ts: Optional[float] = None, force: bool = False):
    global _dirty
    global _last_save_ts
    if not _dirty and not force:
        return
    ts = time.time() if now_ts is None else float(now_ts)
    if (not force) and (ts - _last_save_ts < _SAVE_INTERVAL_SECONDS):
        return
    s = _state if _state is not None else _ensure_state(ts)
    _write_file(s)
    _write_total_km_txt(s.get("total_km", KM_INITIAL_TOTAL_KM))
    _dirty = False
    _last_save_ts = ts


def _format_km(value: Any) -> str:
    km = max(0.0, _to_float(value, 0.0))
    return f"{km:.1f} km"


def _snapshot() -> Dict[str, Any]:
    s = _ensure_state()
    hook = max(0.0, _to_float(s.get("today_fuel_used_pct_hook", 0.0), 0.0))
    fuel_display = "N/A" if hook <= 0.0 else f"N/A (hook {hook:.1f}% tank drop)"
    return {
        "total_km": max(0.0, _to_float(s.get("total_km", 0.0), 0.0)),
        "today_km": max(0.0, _to_float(s.get("today_km", 0.0), 0.0)),
        "today_key": str(s.get("today_key", "") or ""),
        "today_fuel_consumption": str(s.get("today_fuel_consumption", "N/A") or "N/A"),
        "today_fuel_hook_pct": hook,
        "display_total_km": _format_km(s.get("total_km", 0.0)),
        "display_today_km": _format_km(s.get("today_km", 0.0)),
        "display_fuel_today": fuel_display,
        "today_started_ts": max(0.0, _to_float(s.get("today_started_ts", 0.0), 0.0)),
        "day_start_hour": _day_start_hour(),
    }


def read_trips_snapshot() -> Dict[str, Any]:
    _roll_day_if_needed()
    _persist_if_due()
    return _snapshot()


def peek_trips_snapshot() -> Dict[str, Any]:
    """Read-only formatted snapshot: no day-rollover, no dirty-flag persist.

    `read_trips_snapshot()` above chains into `_persist_if_due()`, which —
    roughly every 5s while `_dirty` — does a synchronous file write. That is
    fine on the 350ms engine-poll thread that already owns the accumulation
    (`update_trips_from_telemetry_snapshot`), but `TelemetryBridge.totalKm`/
    `todayKm` are `pyqtProperty` getters read directly by QML bindings at
    ~12.5 Hz — a property getter must never risk a blocking disk write, and
    this was exactly what made the driving screensaver's trip readout
    stutter. Callers that only need the formatted string for display (i.e.
    everything reachable from QML) should use this instead; only
    `_EngineStatePoll`'s poll loop should call the persisting version.
    """
    _ensure_state()  # syncs total_km from disk internally, rate-limited
    return _snapshot()


def update_trips_from_telemetry_snapshot(
    snapshot: Optional[Dict[str, Dict[str, Any]]],
    *,
    allow_update: bool = True,
) -> Dict[str, Any]:
    global _dirty

    now_ts = time.time()
    _roll_day_if_needed(now_ts)
    s = _ensure_state(now_ts)

    if not bool(allow_update):
        s["paused_no_real"] = True
        _persist_if_due(now_ts)
        return _snapshot()

    speed_val = None
    fuel_val = None
    if isinstance(snapshot, dict):
        try:
            speed_val = snapshot.get("Speed", {}).get("value", None)
        except Exception:
            speed_val = None
        try:
            fuel_val = snapshot.get("Fuel Level", {}).get("value", None)
        except Exception:
            fuel_val = None

    try:
        speed_kmh = max(0.0, float(speed_val)) if speed_val is not None else None
    except Exception:
        speed_kmh = None
    try:
        fuel_pct = max(0.0, min(100.0, float(fuel_val))) if fuel_val is not None else None
    except Exception:
        fuel_pct = None

    if s.get("paused_no_real"):
        if speed_kmh is not None:
            s["last_speed_kmh"] = speed_kmh
        s["last_update_ts"] = now_ts
        s["paused_no_real"] = False
        _dirty = True
        _persist_if_due(now_ts)
        return _snapshot()

    if speed_kmh is not None:
        prev_ts = max(0.0, _to_float(s.get("last_update_ts", now_ts), now_ts))
        dt = max(0.0, min(_MAX_DT_SECONDS, now_ts - prev_ts))
        prev_speed = max(0.0, _to_float(s.get("last_speed_kmh", speed_kmh), speed_kmh))
        avg_speed = max(0.0, (prev_speed + speed_kmh) * 0.5)
        delta_km = (avg_speed * dt) / 3600.0
        if delta_km > 0.0:
            s["total_km"] = max(0.0, _to_float(s.get("total_km", 0.0), 0.0) + delta_km)
            s["today_km"] = max(0.0, _to_float(s.get("today_km", 0.0), 0.0) + delta_km)
            _dirty = True
        s["last_speed_kmh"] = speed_kmh
        s["last_update_ts"] = now_ts
        _dirty = True

    if fuel_pct is not None:
        last_fuel = s.get("last_fuel_level_pct")
        if last_fuel is not None:
            drop = float(last_fuel) - fuel_pct
            if 0.0 < drop < 20.0:
                s["today_fuel_used_pct_hook"] = max(
                    0.0,
                    _to_float(s.get("today_fuel_used_pct_hook", 0.0), 0.0) + drop,
                )
                _dirty = True
        s["last_fuel_level_pct"] = fuel_pct
        _dirty = True

    _persist_if_due(now_ts)
    return _snapshot()


def set_total_km(value: float) -> Dict[str, Any]:
    """Hand-set the car's total odometer — the Settings editor's entry point.

    Writes the txt file immediately (force, not the usual 5s throttle): this
    is a deliberate one-off correction, not a hot-path accumulation, and the
    user should see it take effect the moment they save.
    """
    s = _ensure_state()
    km = max(0.0, _to_float(value, s.get("total_km", KM_INITIAL_TOTAL_KM)))
    s["total_km"] = km
    _write_total_km_txt(km)
    return _snapshot()


def reset_today() -> Dict[str, Any]:
    """Zero today's distance and fuel-tracking hook. Settings -> Trips ->
    Reset Today's entry point — see the comment at its QML call site for why
    this needed a real function: it used to write into Data/settings.ini,
    a store this module never reads, so the button did nothing."""
    global _dirty
    now_ts = time.time()
    s = _ensure_state(now_ts)
    # Re-key the bucket as well as zeroing it: a manual reset means "today
    # starts now", so if the clock is stale the next real rollover is still
    # measured from this moment rather than from a day key we never left.
    s["today_key"] = _today_key(now_ts)
    s["today_started_ts"] = now_ts
    s["day_start_hour"] = _day_start_hour()
    s["today_km"] = 0.0
    s["today_fuel_consumption"] = "N/A"
    s["today_fuel_used_pct_hook"] = 0.0
    _dirty = True
    _persist_if_due(force=True)
    return _snapshot()
