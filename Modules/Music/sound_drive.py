# Modules/Music/sound_drive.py
"""
will.i.am-style SoundDrive: shapes music CHARACTER via per-stem EQ, not volume.

Real MBUX Sound Drive behaviour (from Mercedes-AMG / will.i.am):
  - Stopped / idle  → simple bass + muffled percussion
  - Accelerator     → directly mapped to bass intensity ("the beating heart")
  - Speed builds    → layers appear in succession: drums → melody → vocals
  - Braking         → vocals/melody retreat, sound pulls back
  - RPM             → overall energy / clarity of every stem
  - Gear shifts     → brief clarity dip then recovery

All stems play at 100 % VLC volume at all times.
Only EQ shaping controls how "present" or "muffled" each stem sounds.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Optional, Protocol

from PyQt6.QtCore import QTimer

from Modules.Obd.telemetry import TelemetryHub

_DEBUG = os.environ.get("DEBUG_SOUND_DRIVE", "").strip() not in ("", "0")


class SoundDriveMusicIface(Protocol):
    """Minimal contract SoundDrive expects from the music layer."""
    def get_current_title(self) -> Optional[str]: ...
    def has_stems_for(self, title: str) -> bool: ...
    def ensure_stems_playback(self, title: str) -> None: ...
    def set_stem_volumes(self, drums: float, bass: float, other: float, vocals: float) -> None: ...
    def ensure_normal_playback(self) -> None: ...
    def set_sounddrive_distance(self, distance: float) -> None: ...
    def set_main_player_openness(self, openness: float) -> None: ...
    def set_stem_dynamic_params(self, stereo_width: float, vocal_pan: float,
                                brake_duck: float, drive: float) -> None: ...


@dataclass
class DriveConfig:
    interval_ms: int = 100              # 10 Hz

    # RPM zones (tuned for a ~6 500 rpm redline)
    rpm_idle: float = 950.0
    rpm_low:  float = 1500.0
    rpm_sweet_lo: float = 2000.0
    rpm_sweet_hi: float = 3500.0
    rpm_full: float = 3500.0            # RPM where stems are fully open (town driving)

    # Moving gate
    speed_moving: float = 5.0           # km/h — above = driving

    # Throttle (% pedal)
    throttle_noise_floor: float = 10.0  # ignore ECU auto-throttle below this
    throttle_push: float = 100.0        # full push
    throttle_curve_gamma: float = 1.20  # progressive pedal shaping

    # Gear-shift accent
    gear_shift_drop_rpm: float = 400.0
    gear_shift_accent_ticks: int = 4

    # Brake accent (detected via speed delta when throttle is low)
    brake_drop_kmh: float = 2.0         # km/h drop per tick → brake event
    brake_accent_ticks: int = 5         # ~500 ms at 10 Hz

    # Clarity — mapped to speed
    speed_clarity_full: float = 26.0    # km/h where clarity reaches 1.0

    # Vocals — mapped to throttle
    vocals_fade_off: float = 0.30       # per tick decay when off throttle (fast ~0.3s fade)

    # Smoothing (fraction toward target, per tick)
    attack:  float = 0.30
    release: float = 0.12
    vocals_attack: float = 0.18
    brake_release: float = 0.30
    throttle_attack: float = 0.34
    throttle_release: float = 0.12


# ── helpers ──────────────────────────────────────────────────────────────

def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x

def _norm01(x: float, lo: float, hi: float) -> float:
    return _clamp((x - lo) / (hi - lo)) if hi > lo else 0.0

def _smooth_ar(prev: float, target: float, attack: float, release: float) -> float:
    alpha = _clamp(attack if target >= prev else release, 0.0, 1.0)
    return prev + (target - prev) * alpha


def _smooth_throttle(prev: float, target: float, attack: float, release: float) -> float:
    """Throttle smoothing: decay is slower from high load, but zero is instant."""
    target = _clamp(target)
    if target <= 0.0:
        return 0.0
    if target >= prev:
        alpha = _clamp(attack, 0.0, 1.0)
    else:
        # High current throttle decays slower; low throttle decays quicker.
        alpha = _clamp(release * (1.0 - 0.75 * _clamp(prev)), 0.01, 1.0)
    return prev + (target - prev) * alpha


def _throttle_build_step(pedal: float) -> float:
    """Build rate per tick: 100% ≈ instant, 20% ≈ ~5s to full."""
    p = _clamp(pedal)
    return _clamp((0.18 * p) + (0.82 * (p ** 8)))


def _throttle_percent(raw_value: float) -> float:
    """Clamp throttle percent from telemetry."""
    value = max(0.0, float(raw_value))
    return min(100.0, value)


# ── SoundDrive ───────────────────────────────────────────────────────────

class SoundDrive:
    """
    Reactive audio shaping driven by live OBD telemetry.

    Every tick computes an *openness* value (0 = muffled, 1 = clear) per stem.
    These are passed to the music layer which applies per-stem volume + EQ shaping.
    """

    def __init__(self, music_iface: SoundDriveMusicIface,
                 *, cfg: Optional[DriveConfig] = None) -> None:
        self._music = music_iface
        self._cfg = cfg or DriveConfig()

        self._timer = QTimer()
        self._timer.setInterval(self._cfg.interval_ms)
        self._timer.timeout.connect(self._tick)

        self._enabled = False
        self._last_title: Optional[str] = None
        self._last_had_stems: bool = False
        self._mix_mode: str = "A"      # "A", "B", "C", "D", or "E"

        # Smoothed openness per stem (0 = muffled, 1 = clear)
        self._o_drums:  float = 0.1
        self._o_bass:   float = 0.35   # bass always slightly present
        self._o_other:  float = 0.0
        self._o_vocals: float = 0.0
        self._throttle_state: float = 0.0
        self._throttle_build: float = 0.0

        # Gear-shift state
        self._prev_rpm: float = 0.0
        self._shift_ticks: int = 0

        # Brake state
        self._prev_speed: float = 0.0
        self._brake_ticks: int = 0


    # ── public API ───────────────────────────────────────────────────────

    def start(self) -> None:
        if self._enabled:
            return
        self._enabled = True
        # Pre-populate so the first tick does NOT see a "track change"
        try:
            self._last_title = self._music.get_current_title()
            self._last_had_stems = True
        except Exception:
            pass
        self._timer.start()

    def stop(self) -> None:
        if not self._enabled:
            return
        self._enabled = False
        self._timer.stop()
        self._last_title = None
        self._last_had_stems = False
        self._throttle_state = 0.0
        self._throttle_build = 0.0
        try:
            self._music.ensure_normal_playback()
        except Exception:
            pass

    def is_enabled(self) -> bool:
        return self._enabled

    def set_mix_mode(self, mode: str) -> None:
        """Set mix mode: 'A', 'B', 'C', 'D', or 'E' (or 'Mix A', etc.)."""
        mode = mode.replace("Mode ", "").replace("Mix ", "").strip().upper()
        if mode in ("A", "B", "C", "D", "E"):
            self._mix_mode = mode

    # ── tick ─────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        if not self._enabled:
            return

        # ── current track ────────────────────────────────────────────────
        try:
            title = self._music.get_current_title()
        except Exception:
            return
        if not title:
            return

        if title != self._last_title:
            self._last_title = title
            self._last_had_stems = False
            self._shift_ticks = 0
            self._throttle_state = 0.0
            self._throttle_build = 0.0

        # ── telemetry ────────────────────────────────────────────────────
        snap = TelemetryHub.instance().snapshot() if TelemetryHub else {}

        def _num(*keys: str) -> float:
            for k in keys:
                try:
                    v = snap.get(k)
                    if isinstance(v, dict):
                        v = v.get("value")
                    if v is not None:
                        return float(v)
                except Exception:
                    continue
            return 0.0

        rpm      = _num("RPM_smooth", "RPM", "rpm", "engine_rpm")
        if _DEBUG:
            print(f"[SD-DBG] snap keys={list(snap.keys())} rpm={rpm:.0f}")
        speed    = _num("Speed", "speed_kmh", "speed", "vehicle_speed_kmh")
        throttle_raw = _num("Throttle Pos", "Accel Pedal D", "Rel. Throttle",
                            "throttle_pos", "accel_pedal", "throttle")
        steering = _num("Steering", "steering")   # -1..+1 pseudo

        c = self._cfg

        # ── derived signals ──────────────────────────────────────────────
        moving     = speed > c.speed_moving
        rpm_t      = _norm01(rpm, c.rpm_idle, c.rpm_full)       # 0→1  (1.0 at 3500 rpm)
        throttle_pct = _throttle_percent(throttle_raw)
        if throttle_pct < c.throttle_noise_floor:
            throttle_pct = 0.0
        throttle_t = _norm01(throttle_pct, c.throttle_noise_floor, c.throttle_push)  # 0→1
        throttle_t = _clamp(throttle_t ** max(0.1, float(c.throttle_curve_gamma)))
        if throttle_t <= 0.0:
            self._throttle_build = 0.0
        else:
            self._throttle_build = _clamp(self._throttle_build + _throttle_build_step(throttle_t))
        # keep a tiny smoothing layer so instantaneous 100% still sounds clean
        self._throttle_state = _smooth_throttle(self._throttle_state, self._throttle_build, c.throttle_attack, c.throttle_release)
        throttle_t = self._throttle_state
        coasting   = throttle_pct <= c.throttle_noise_floor and rpm > c.rpm_low

        # RPM sweet-spot bell (peaks in power band)
        rpm_mid   = (c.rpm_sweet_lo + c.rpm_sweet_hi) / 2.0
        rpm_sigma = (c.rpm_sweet_hi - c.rpm_sweet_lo) / 2.0
        sweet     = math.exp(-0.5 * ((rpm - rpm_mid) / max(rpm_sigma, 1.0)) ** 2)

        # ── gear-shift detection ─────────────────────────────────────────
        rpm_drop = self._prev_rpm - rpm
        if rpm_drop > c.gear_shift_drop_rpm and moving:
            self._shift_ticks = c.gear_shift_accent_ticks
        if self._shift_ticks > 0:
            self._shift_ticks -= 1
        self._prev_rpm = rpm

        shift_amount = 0.0   # 0..1, faded out over accent_ticks
        if self._shift_ticks > 0:
            shift_amount = self._shift_ticks / c.gear_shift_accent_ticks

        # Mode B flips the gear-shift accent from a dip to a bright flash
        if self._mix_mode == "B":
            shift_dip = 0.0
            shift_boost = 0.22 * shift_amount
        else:
            shift_dip = 0.35 * shift_amount
            shift_boost = 0.0

        # ── brake detection (speed drop while throttle low) ──────────────
        speed_drop = self._prev_speed - speed
        if (speed_drop > c.brake_drop_kmh
                and throttle_pct <= c.throttle_noise_floor
                and moving):
            self._brake_ticks = c.brake_accent_ticks
        if self._brake_ticks > 0:
            self._brake_ticks -= 1
        self._prev_speed = speed
        brake_amount = (self._brake_ticks / c.brake_accent_ticks
                        if self._brake_ticks > 0 else 0.0)

        # =================================================================
        # TARGET OPENNESS PER STEM
        # =================================================================

        clarity = _norm01(speed, 0.0, c.speed_clarity_full)

        if self._mix_mode == "B":
            # ── Mode B ──────────────────────────────────────────────
            # Clarity = throttle (all stems), Vocals = speed
            throttle_clarity = throttle_t

            # BASS — throttle clarity
            bass_target = 0.28 + 0.72 * throttle_clarity
            if coasting:
                bass_target = min(bass_target, 0.24 + 0.10 * throttle_clarity)

            # DRUMS — throttle clarity, gear-up flash boosts
            drums_target = 0.18 + throttle_clarity * 0.62 + sweet * 0.12
            drums_target = _clamp(drums_target + shift_boost)

            # OTHER / MELODY — throttle clarity
            other_target = 0.12 + throttle_clarity * 0.58 + sweet * 0.12
            if coasting:
                other_target *= 0.72
            other_target = _clamp(other_target + shift_boost * 0.7)

            # VOCALS — speed-based
            if moving:
                vocals_target = 0.08 + 0.70 * _norm01(speed, c.speed_moving, c.speed_clarity_full)
            else:
                vocals_target = 0.05
            vocals_target = _clamp(vocals_target + shift_boost * 0.4)

        else:
            # ── Mix A / C / D ─────────────────────────────────────
            # Clarity = speed, Vocals = throttle

            mode_clarity = clarity

            # BASS — speed opens it up, always somewhat present
            bass_target = 0.28 + 0.72 * mode_clarity
            if coasting:
                bass_target = min(bass_target, 0.24 + 0.14 * mode_clarity)

            # DRUMS — speed-driven
            drums_target = 0.18 + mode_clarity * 0.62 + sweet * 0.14
            drums_target = _clamp(drums_target - shift_dip)

            # OTHER / MELODY — speed-driven
            other_target = 0.12 + mode_clarity * 0.56 + sweet * 0.14
            if coasting:
                other_target *= 0.72
            other_target = _clamp(other_target - shift_dip * 0.7)

            # VOCALS — throttle-driven
            if throttle_t > 0.01:
                vocals_target = 0.08 + 0.70 * throttle_t
            else:
                vocals_target = 0.05 if moving else 0.03
            vocals_target = _clamp(vocals_target - shift_dip * 0.4)

            if self._mix_mode in ("D", "E"):
                # Mode D/E = Mode C response, but with stronger distant character
                # as the car slows to a stop.
                stop_factor = 1.0 - _norm01(speed, c.speed_moving, c.speed_moving + 10.0)
                if stop_factor > 0.0:
                    bass_target = _clamp(bass_target * (1.0 - 0.35 * stop_factor))
                    drums_target = _clamp(drums_target * (1.0 - 0.64 * stop_factor))
                    other_target = _clamp(other_target * (1.0 - 0.68 * stop_factor))
                    vocals_target = _clamp(vocals_target * (1.0 - 0.62 * stop_factor))
                    if stop_factor > 0.95:
                        vocals_target = min(vocals_target, 0.05)

        # =================================================================
        # SMOOTHING
        # =================================================================
        rel = c.brake_release if coasting else c.release
        self._o_drums  = _smooth_ar(self._o_drums,  drums_target,  c.attack, rel)
        self._o_bass   = _smooth_ar(self._o_bass,   bass_target,   c.attack, rel)
        self._o_other  = _smooth_ar(self._o_other,  other_target,  c.attack, rel)
        vocals_rel = c.vocals_fade_off
        self._o_vocals = _smooth_ar(self._o_vocals, vocals_target, c.vocals_attack, vocals_rel)

        # =================================================================
        # APPLY
        # =================================================================
        try:
            has_stems = self._music.has_stems_for(title)
        except Exception:
            has_stems = False

        if not has_stems:
            if self._last_had_stems:
                try:
                    self._music.ensure_normal_playback()
                except Exception:
                    pass
            self._last_had_stems = False
            return  # no stems — leave main player completely untouched

        if not self._last_had_stems:
            try:
                self._music.ensure_stems_playback(title)
                self._last_had_stems = True
            except Exception as e:
                print(f"[SoundDrive-tick] ensure_stems_playback failed: {e}")
                self._last_had_stems = False
                return

        try:
            self._music.set_stem_volumes(
                _clamp(self._o_drums),
                _clamp(self._o_bass),
                _clamp(self._o_other),
                _clamp(self._o_vocals),
            )
        except Exception as e:
            print(f"[SoundDrive] set_stem_volumes failed: {e}")

        # ── per-mode dynamic params ─────────────────────────────────────
        # Mode A: stereo width widens with speed
        # Mode B: no spatial effects (pedal-pressure focus)
        # Mode C: brake duck + vocal pan by steering
        # Mode D: same spatial effects as Mode C with 0.8x→1.0x speed ramp
        # Mode E: same as Mode D, but without speed slowdown
        stereo_width = 1.0
        vocal_pan = 0.0
        brake_duck = 0.0
        drive = 1.0

        if self._mix_mode == "A":
            stereo_width = 0.4 + 0.6 * clarity
        elif self._mix_mode in ("C", "D", "E"):
            brake_duck = brake_amount
            vocal_pan = max(-1.0, min(1.0, float(steering))) * 0.6
            if self._mix_mode == "D":
                # Mode D playback rate: 0 km/h -> 0.85x, progressively to 1.0x.
                drive = 0.85 + 0.15 * _norm01(speed, 0.0, 10.0)

        try:
            setter = getattr(self._music, "set_stem_dynamic_params", None)
            if setter is not None:
                setter(
                    stereo_width=stereo_width,
                    vocal_pan=vocal_pan,
                    brake_duck=brake_duck,
                    drive=drive,
                )
        except Exception as e:
            print(f"[SoundDrive] set_stem_dynamic_params failed: {e}")
