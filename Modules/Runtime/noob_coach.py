"""Noob Mode: beginner driving hints (Settings → Driving Mode).

Four assistants, each behind its own toggle and all behind the Noob Mode
master switch ([SoundFeatures] noob_*):

    Starting  pulling away with too few revs, and a stall right after moving off
    Rpm       shift up / shift down from the vehicle's gear bands
    Speeding  over the limit set in Driving Mode (noob_speed_limit, km/h)
    UpHill    the uphill detector is on and the revs are low for the gear

NoobCoach.update() is pure (numbers in, alert states out) so the rules can be
tested without Qt; main.py's 350 ms engine poll feeds it and forwards the
result to AlertsBridge. Alert texts are fixed strings on purpose: a live value
in the text would defeat set_alert's no-op-refresh guard.
"""
from __future__ import annotations

from typing import Dict, Optional

# key → (text, Dash icon, priority)
ALERTS: Dict[str, tuple] = {
    "noob_low_revs":   ("Low revs pulling away — add a little throttle", "engine.png",        "warning"),
    "noob_stalled":    ("Engine stalled — clutch in, restart",           "engine.png",        "warning"),
    "noob_shift_up":   ("Shift up",                                      "",                  "info"),
    "noob_shift_down": ("Shift down — the engine is lugging",            "",                  "info"),
    "noob_speeding":   ("Over your speed limit",                         "speed_limiter.png", "warning"),
    "noob_uphill":     ("Uphill — shift down, keep the revs up",         "",                  "info"),
}

# Fallbacks when [GearBands] has no row for a gear (same as telemetry.py).
_DEFAULT_DOWN_ORANGE = 1100
_DEFAULT_UP_ORANGE = 3000


class NoobCoach:
    SHIFT_HOLD_S = 1.5       # a shift hint needs the condition this long
    STALL_WINDOW_S = 1.0     # running → stopped within this = a stall
    STALL_SHOW_S = 6.0       # how long "stalled" stays up
    SPEED_HYST_KMH = 3.0     # speeding clears this far under the limit

    def __init__(self, gear_bands: Optional[Dict[int, Dict[str, int]]] = None):
        self._bands = gear_bands or {}
        self._reset()

    def _reset(self) -> None:
        self._shift_up_since: Optional[float] = None
        self._shift_down_since: Optional[float] = None
        self._running_at: Optional[float] = None    # last time rpm was clearly running
        self._stalled_until = 0.0
        self._speeding = False

    def _band(self, gear: int, key: str, default: int) -> int:
        b = self._bands.get(int(gear or 0))
        return int(b.get(key, default)) if b else default

    @staticmethod
    def _held(since: Optional[float], cond: bool, now: float, hold: float):
        """(new_since, held_long_enough)."""
        if not cond:
            return None, False
        since = now if since is None else since
        return since, (now - since) >= hold

    def update(self, now: float, *, rpm: float, speed: float, gear: int,
               uphill: bool, cfg: Dict[str, object]) -> Dict[str, bool]:
        """Alert states for this tick. `cfg` holds the toggles and the limit."""
        out = {k: False for k in ALERTS}
        if not cfg.get("enabled"):
            self._reset()                       # forget partial timers
            return out

        # ── Starting ──
        if cfg.get("starting"):
            out["noob_low_revs"] = bool(gear == 1 and 1.0 <= speed <= 10.0 and 0 < rpm < 1000)
            if rpm > 500:
                self._running_at = now
            elif (rpm < 250 and speed < 15.0 and self._running_at is not None
                    and now - self._running_at <= self.STALL_WINDOW_S):
                self._stalled_until = now + self.STALL_SHOW_S
                self._running_at = None
            out["noob_stalled"] = now < self._stalled_until

        # ── Rpm: shift hints from the gear bands ──
        if cfg.get("rpm") and gear >= 1:
            up = rpm > self._band(gear, "up_orange", _DEFAULT_UP_ORANGE) and gear < 5
            down = (rpm < self._band(gear, "down_orange", _DEFAULT_DOWN_ORANGE)
                    and speed > 10.0 and gear > 1)
            self._shift_up_since, out["noob_shift_up"] = self._held(
                self._shift_up_since, up, now, self.SHIFT_HOLD_S)
            self._shift_down_since, out["noob_shift_down"] = self._held(
                self._shift_down_since, down, now, self.SHIFT_HOLD_S)
        else:
            self._shift_up_since = self._shift_down_since = None

        # ── Speeding, with hysteresis ──
        if cfg.get("speeding"):
            limit = float(cfg.get("limit") or 90.0)
            if speed > limit:
                self._speeding = True
            elif speed < limit - self.SPEED_HYST_KMH:
                self._speeding = False
            out["noob_speeding"] = self._speeding
        else:
            self._speeding = False

        # ── UpHill ──
        if cfg.get("uphill") and uphill and gear >= 1:
            out["noob_uphill"] = rpm < self._band(gear, "down_orange", _DEFAULT_DOWN_ORANGE) + 300

        return out
