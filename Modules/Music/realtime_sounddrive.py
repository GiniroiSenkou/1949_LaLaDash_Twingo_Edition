from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Optional, Protocol

from PyQt6.QtCore import QTimer

from Modules.Obd.telemetry import TelemetryHub
from Modules.Music.realtime_analysis import (
    AnalysisConfig,
    TrackAnalysis,
    analyze_audio_file,
    dump_metadata_json,
    load_metadata_json,
)

THROTTLE_NOISE_FLOOR_PCT = 10.0
THROTTLE_CURVE_GAMMA = 1.20
THROTTLE_ATTACK = 0.34
THROTTLE_RELEASE = 0.14


class RealtimeSoundDriveMusicIface(Protocol):
    def get_current_track_key(self) -> str: ...
    def get_current_track_path(self) -> str: ...
    def get_time_ms(self) -> int: ...
    def set_main_player_openness(self, openness: float) -> None: ...
    def apply_main_player_reactive_mix(self, profile: dict[str, float]) -> None: ...


class RealtimeSoundDrive:
    def __init__(
        self,
        music_iface: RealtimeSoundDriveMusicIface,
        *,
        metadata_dir: str | os.PathLike[str],
    ) -> None:
        self._music = music_iface
        self._metadata_dir = Path(metadata_dir)

        self._timer = QTimer()
        self._timer.timeout.connect(self._tick)

        self._enabled = False
        self._performance = "rpi4_safe"
        self._metadata_cache_enabled = False
        self._latest_frame: dict[str, object] | None = None
        self._analysis_cache: dict[tuple[str, str], TrackAnalysis] = {}
        self._analysis: TrackAnalysis | None = None
        self._analysis_key = ""
        self._analysis_perf = self._performance
        self._load_thread: Optional[threading.Thread] = None
        self._load_requested: tuple[str, str, str] | None = None
        self._pending_result: tuple[str, str, TrackAnalysis | None] | None = None
        self._lock = threading.Lock()
        self._drive_state = 0.0
        self._bass_state = 0.38
        self._presence_state = 0.02
        self._air_state = 0.0
        self._energy_state = 0.10
        self._kick_env = 0.0
        self._snare_env = 0.0
        self._hat_env = 0.0
        self._drop_env = 0.0
        self._rpm_state = 0.0
        self._speed_state = 0.0
        self._throttle_state = 0.0
        self._brake_state = 0.0
        self._prev_speed = 0.0
        self._prev_rpm = 0.0
        self._update_interval()

    def start(self) -> None:
        if self._enabled:
            return
        self._enabled = True
        self._reset_profile_state()
        self._update_interval()
        self._timer.start()

    def stop(self) -> None:
        if not self._enabled:
            return
        self._enabled = False
        self._timer.stop()
        self._latest_frame = None
        self._analysis = None
        self._analysis_key = ""
        self._reset_profile_state()
        try:
            self._music.set_main_player_openness(1.0)
        except Exception:
            pass

    def is_enabled(self) -> bool:
        return self._enabled

    def set_performance_mode(self, mode: str) -> None:
        clean = str(mode or "").strip().lower()
        self._performance = "full_power" if clean == "full_power" else "rpi4_safe"
        self._analysis = None
        self._analysis_key = ""
        self._analysis_perf = self._performance
        self._update_interval()

    def set_metadata_cache_enabled(self, enabled: bool) -> None:
        self._metadata_cache_enabled = bool(enabled)

    def latest_frame(self) -> dict[str, object] | None:
        return dict(self._latest_frame) if isinstance(self._latest_frame, dict) else None

    def _update_interval(self) -> None:
        self._timer.setInterval(40 if self._performance == "full_power" else 100)

    def _analysis_config(self, performance: str | None = None) -> AnalysisConfig:
        active = performance or self._performance
        if active == "full_power":
            return AnalysisConfig(sample_rate=32000, frame_size=1024, hop_size=512)
        return AnalysisConfig(sample_rate=22050, frame_size=2048, hop_size=1024)

    def _metadata_path(self, audio_path: str) -> Path:
        digest = hashlib.sha1(f"{audio_path}|{self._performance}".encode("utf-8")).hexdigest()
        return self._metadata_dir / f"{digest}.json"

    def _reset_profile_state(self) -> None:
        self._drive_state = 0.0
        self._bass_state = 0.38
        self._presence_state = 0.02
        self._air_state = 0.0
        self._energy_state = 0.10
        self._kick_env = 0.0
        self._snare_env = 0.0
        self._hat_env = 0.0
        self._drop_env = 0.0
        self._rpm_state = 0.0
        self._speed_state = 0.0
        self._throttle_state = 0.0
        self._brake_state = 0.0
        self._prev_speed = 0.0
        self._prev_rpm = 0.0

    def _tick(self) -> None:
        if not self._enabled:
            return

        self._consume_pending_result()

        try:
            track_key = self._music.get_current_track_key()
            track_path = self._music.get_current_track_path()
        except Exception:
            return

        if not track_key or not track_path or not os.path.isfile(track_path):
            self._latest_frame = None
            try:
                self._music.set_main_player_openness(1.0)
            except Exception:
                pass
            return

        telemetry = self._telemetry_features(self._telemetry_snapshot())
        profile = self._telemetry_only_profile(telemetry)

        cache_key = (track_key, self._performance)
        if self._analysis is None or self._analysis_key != track_key or self._analysis_perf != self._performance:
            cached = self._analysis_cache.get(cache_key)
            if cached is not None:
                self._analysis = cached
                self._analysis_key = track_key
                self._analysis_perf = self._performance
            else:
                self._request_analysis(track_key, track_path)
                self._latest_frame = {
                    "time": round(float(self._music.get_time_ms()) / 1000.0, 4),
                    "analysis_ready": False,
                    **{k: round(float(v), 4) for k, v in profile.items()},
                }
                self._apply_profile(profile)
                return

        time_s = max(0.0, float(self._music.get_time_ms()) / 1000.0)
        frame = self._analysis.frame_at(time_s) if self._analysis else None
        if not frame:
            self._latest_frame = {
                "time": round(time_s, 4),
                "analysis_ready": False,
                **{k: round(float(v), 4) for k, v in profile.items()},
            }
            self._apply_profile(profile)
            return

        profile = self._profile_from_sources(frame, telemetry)
        self._latest_frame = dict(frame)
        self._latest_frame["analysis_ready"] = True
        self._latest_frame.update({k: round(float(v), 4) for k, v in profile.items()})
        self._apply_profile(profile)

    def _apply_profile(self, profile: dict[str, float]) -> None:
        try:
            apply_profile = getattr(self._music, "apply_main_player_reactive_mix", None)
            if callable(apply_profile):
                apply_profile(profile)
            else:
                self._music.set_main_player_openness(float(profile.get("drive", 1.0)))
        except Exception:
            pass

    def _consume_pending_result(self) -> None:
        with self._lock:
            pending = self._pending_result
            self._pending_result = None
        if pending is None:
            return
        track_key, performance, analysis = pending
        if analysis is None:
            return
        self._analysis_cache[(track_key, performance)] = analysis
        if track_key == self._music.get_current_track_key() and performance == self._performance:
            self._analysis = analysis
            self._analysis_key = track_key
            self._analysis_perf = performance

    def _request_analysis(self, track_key: str, track_path: str) -> None:
        requested = (track_key, track_path, self._performance)
        if self._load_requested == requested and self._load_thread and self._load_thread.is_alive():
            return
        self._load_requested = requested

        def _worker(key: str, path: str, performance: str, metadata_enabled: bool) -> None:
            analysis: TrackAnalysis | None = None
            try:
                metadata_path = self._metadata_path(path)
                if metadata_enabled and metadata_path.is_file():
                    cached = load_metadata_json(str(metadata_path))
                    if (
                        cached.source_path == path
                        and cached.source_mtime_ns == os.stat(path).st_mtime_ns
                    ):
                        analysis = cached
                if analysis is None:
                    analysis = analyze_audio_file(path, config=self._analysis_config(performance))
                    if metadata_enabled:
                        dump_metadata_json(analysis, str(metadata_path))
            except Exception:
                analysis = None
            with self._lock:
                self._pending_result = (key, performance, analysis)

        self._load_thread = threading.Thread(
            target=_worker,
            args=(track_key, track_path, self._performance, self._metadata_cache_enabled),
            daemon=True,
        )
        self._load_thread.start()

    @staticmethod
    def _telemetry_snapshot() -> dict[str, object]:
        # Read through the coordinator: it hands back a lock-protected defensive
        # copy, so we never iterate TelemetryHub.snapshot_cache while the
        # coordinator thread is rebuilding it (the previous direct call could
        # race with that rebuild). Falls back to the hub if the coordinator
        # hasn't produced a snapshot yet.
        try:
            from Modules.Runtime.telemetry_coordinator import TelemetryCoordinator
            return TelemetryCoordinator.instance().snapshot()
        except Exception:
            try:
                return dict(TelemetryHub.instance().snapshot())
            except Exception:
                return {}

    @staticmethod
    def _snapshot_metric(snapshot: dict[str, object], *keys: str) -> tuple[float, bool]:
        for key in keys:
            entry = snapshot.get(key)
            if isinstance(entry, dict):
                value = entry.get("value", None)
                if value is not None:
                    try:
                        return float(value), True
                    except Exception:
                        continue
            elif entry is not None:
                try:
                    return float(entry), True
                except Exception:
                    continue
        return 0.0, False

    @staticmethod
    def _normalized_throttle(snapshot_value: float) -> float:
        """Return progressive throttle 0..1, with <10% ignored."""
        value = max(0.0, float(snapshot_value))
        value = min(100.0, value)
        if value < THROTTLE_NOISE_FLOOR_PCT:
            return 0.0
        linear = max(
            0.0,
            min(1.0, (value - THROTTLE_NOISE_FLOOR_PCT) / (100.0 - THROTTLE_NOISE_FLOOR_PCT)),
        )
        return max(0.0, min(1.0, linear ** THROTTLE_CURVE_GAMMA))

    def _telemetry_features(self, snapshot: dict[str, object]) -> dict[str, float]:
        rpm, _ = self._snapshot_metric(snapshot, "RPM_smooth", "RPM", "rpm", "engine_rpm")
        speed, _ = self._snapshot_metric(snapshot, "Speed", "speed", "speed_kmh", "vehicle_speed_kmh")
        throttle, _ = self._snapshot_metric(
            snapshot,
            "Throttle Pos",
            "Accel Pedal D",
            "Rel. Throttle",
            "throttle_pos",
            "accel_pedal",
            "throttle",
        )

        rpm_t = max(0.0, min(1.0, (rpm - 900.0) / 2500.0))
        speed_t = max(0.0, min(1.0, speed / 30.0))
        throttle_t = self._normalized_throttle(throttle)
        moving = max(0.0, min(1.0, speed / 6.0))

        speed_drop = max(0.0, self._prev_speed - speed)
        rpm_drop = max(0.0, self._prev_rpm - rpm)
        braking = max(
            0.0,
            min(
                1.0,
                (
                    max(speed_drop / 7.5, rpm_drop / 1200.0)
                    * (1.0 - min(1.0, throttle_t * 1.15))
                    * (0.35 + (moving * 0.65))
                ),
            ),
        )

        self._prev_speed = speed
        self._prev_rpm = rpm

        bass = max(0.0, min(1.0, 0.22 + (rpm_t * 0.58) + (throttle_t * 0.26) - (braking * 0.10)))
        presence = max(0.0, min(1.0, 0.02 + (speed_t * 0.90) + (throttle_t * 0.14) - (braking * 0.58)))
        air = max(0.0, min(1.0, (speed_t * 0.96) + (throttle_t * 0.10) - (braking * 0.64)))
        drive = max(
            0.0,
            min(
                1.0,
                max((speed_t * 0.94), (throttle_t * 1.02), (rpm_t * 0.72))
                + (min(1.0, speed_t + throttle_t) * 0.10)
                - (braking * 0.70),
            ),
        )
        return {
            "rpm": rpm_t,
            "speed": speed_t,
            "throttle": throttle_t,
            "brake": braking,
            "drive": drive,
            "bass": bass,
            "presence": presence,
            "air": air,
        }

    @staticmethod
    def _smooth_state(current: float, target: float, attack: float, release: float) -> float:
        alpha = attack if target >= current else release
        alpha = max(0.0, min(1.0, float(alpha)))
        return current + ((target - current) * alpha)

    @staticmethod
    def _smooth_throttle_state(current: float, target: float) -> float:
        target = max(0.0, min(1.0, float(target)))
        if target <= 0.0:
            return 0.0
        if target >= current:
            alpha = THROTTLE_ATTACK
        else:
            alpha = max(0.01, min(1.0, THROTTLE_RELEASE * (1.0 - 0.75 * max(0.0, min(1.0, current)))))
        return current + ((target - current) * alpha)

    @staticmethod
    def _step_envelope(current: float, triggered: bool, attack: float, decay: float, peak: float = 1.0) -> float:
        if triggered:
            current = current + ((peak - current) * max(0.0, min(1.0, attack)))
        else:
            current *= max(0.0, min(1.0, decay))
        return max(0.0, min(1.0, current))

    def _telemetry_only_profile(self, telemetry: dict[str, float]) -> dict[str, float]:
        self._rpm_state = self._smooth_state(self._rpm_state, telemetry["rpm"], 0.28, 0.08)
        self._speed_state = self._smooth_state(self._speed_state, telemetry["speed"], 0.24, 0.05)
        self._throttle_state = self._smooth_throttle_state(self._throttle_state, telemetry["throttle"])
        self._brake_state = self._smooth_state(self._brake_state, telemetry["brake"], 0.36, 0.18)

        drive_target = max(
            0.0,
            min(
                1.0,
                max(
                    self._speed_state * 0.98,
                    self._throttle_state * 1.00,
                    self._rpm_state * 0.72,
                )
                + (min(1.0, self._speed_state + self._throttle_state) * 0.08)
                - (self._brake_state * 0.78),
            ),
        )
        bass_target = max(
            0.22,
            min(
                1.0,
                0.24
                + (self._rpm_state * 0.62)
                + (self._throttle_state * 0.24)
                - (self._brake_state * 0.10),
            ),
        )
        presence_target = max(
            0.0,
            min(
                1.0,
                (self._speed_state * 0.96)
                + (self._throttle_state * 0.12)
                - (self._brake_state * 0.60),
            ),
        )
        air_target = max(
            0.0,
            min(
                1.0,
                (self._speed_state * 1.00)
                + (self._throttle_state * 0.08)
                - (self._brake_state * 0.68),
            ),
        )

        self._drive_state = self._smooth_state(self._drive_state, drive_target, 0.34, 0.10)
        self._bass_state = self._smooth_state(self._bass_state, bass_target, 0.22, 0.08)
        self._presence_state = self._smooth_state(self._presence_state, presence_target, 0.28, 0.10)
        self._air_state = self._smooth_state(self._air_state, air_target, 0.24, 0.10)

        return {
            "drive": max(0.0, min(1.0, self._drive_state)),
            "bass": max(0.20, min(1.0, self._bass_state)),
            "presence": max(0.0, min(1.0, self._presence_state)),
            "sparkle": max(0.0, min(1.0, self._air_state * 0.94)),
            "air": max(0.0, min(1.0, self._air_state)),
            "punch": max(0.0, min(1.0, (self._throttle_state * 0.40) - (self._brake_state * 0.18))),
            "flash": max(0.0, min(1.0, (self._speed_state * 0.16) + (self._brake_state * 0.08))),
        }

    def _profile_from_sources(self, frame: dict[str, object], telemetry: dict[str, float]) -> dict[str, float]:
        energy = max(0.0, min(1.0, float(frame.get("energy", 0.0) or 0.0)))
        low = max(0.0, min(1.0, float(frame.get("low_energy", 0.0) or 0.0)))
        mid = max(0.0, min(1.0, float(frame.get("mid_energy", 0.0) or 0.0)))
        high = max(0.0, min(1.0, float(frame.get("high_energy", 0.0) or 0.0)))

        self._energy_state = self._smooth_state(self._energy_state, energy, 0.18, 0.08)
        self._kick_env = self._step_envelope(self._kick_env, bool(frame.get("kick")), 0.75, 0.72)
        self._snare_env = self._step_envelope(self._snare_env, bool(frame.get("snare")), 0.68, 0.74)
        self._hat_env = self._step_envelope(self._hat_env, bool(frame.get("hihat")), 0.58, 0.78)
        self._drop_env = self._step_envelope(
            self._drop_env,
            bool(frame.get("drop")),
            0.86,
            0.88 if not frame.get("breakdown") else 0.60,
        )

        self._rpm_state = self._smooth_state(self._rpm_state, telemetry["rpm"], 0.28, 0.08)
        self._speed_state = self._smooth_state(self._speed_state, telemetry["speed"], 0.24, 0.05)
        self._throttle_state = self._smooth_throttle_state(self._throttle_state, telemetry["throttle"])
        self._brake_state = self._smooth_state(self._brake_state, telemetry["brake"], 0.36, 0.18)

        base_drive = max(
            0.0,
            min(
                1.0,
                max(
                    self._speed_state * 0.98,
                    self._throttle_state * 1.00,
                    self._rpm_state * 0.72,
                )
                + (min(1.0, self._speed_state + self._throttle_state) * 0.08)
                - (self._brake_state * 0.78),
            ),
        )
        bass_target = max(
            0.22,
            min(
                1.0,
                0.24
                + (self._rpm_state * 0.56)
                + (self._throttle_state * 0.20)
                + (low * 0.10)
                + (self._kick_env * 0.26)
                - (self._brake_state * 0.10),
            ),
        )
        presence_target = max(
            0.0,
            min(
                1.0,
                (self._speed_state * 0.88)
                + (self._throttle_state * 0.10)
                + (mid * 0.12)
                + (self._snare_env * 0.18)
                - (self._brake_state * 0.54),
            ),
        )
        air_target = max(
            0.0,
            min(
                1.0,
                (self._speed_state * 0.92)
                + (high * 0.12)
                + (self._hat_env * 0.22)
                - (self._brake_state * 0.60),
            ),
        )
        drive_target = max(
            0.0,
            min(
                1.0,
                (base_drive * 0.86)
                + (self._energy_state * 0.08)
                + (self._kick_env * 0.10)
                + (self._drop_env * 0.10),
            ),
        )
        sparkle_target = max(
            0.0,
            min(
                1.0,
                (air_target * 0.84)
                + (high * 0.14)
                + (self._hat_env * 0.18)
                + (self._drop_env * 0.08),
            ),
        )
        punch = max(
            0.0,
            min(
                1.0,
                (self._throttle_state * 0.34)
                + (self._kick_env * 0.48)
                + (self._snare_env * 0.18)
                + (self._drop_env * 0.16),
            ),
        )
        flash = max(
            0.0,
            min(1.0, (self._drop_env * 0.70) + (self._hat_env * 0.12) + (self._speed_state * 0.12)),
        )

        if frame.get("breakdown"):
            drive_target *= 0.78
            presence_target *= 0.74
            sparkle_target *= 0.66

        self._drive_state = self._smooth_state(self._drive_state, min(1.0, drive_target), 0.34, 0.14)
        self._bass_state = self._smooth_state(self._bass_state, min(1.0, bass_target), 0.24, 0.10)
        self._presence_state = self._smooth_state(self._presence_state, min(1.0, presence_target), 0.30, 0.14)
        self._air_state = self._smooth_state(self._air_state, min(1.0, air_target), 0.26, 0.14)

        return {
            "drive": max(0.0, min(1.0, self._drive_state)),
            "bass": max(0.20, min(1.0, self._bass_state)),
            "presence": max(0.0, min(1.0, self._presence_state)),
            "sparkle": max(0.0, min(1.0, sparkle_target)),
            "air": max(0.0, min(1.0, self._air_state)),
            "punch": punch,
            "flash": flash,
        }
