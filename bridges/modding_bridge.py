from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot, QTimer

import os
import subprocess
import time

from preferences import DATA_SETTINGS, BASE_DIR, icons_path, resolve_car_asset, get_selected_car
from Modules.Runtime import settings_cache
from Modules.Runtime import vehicle_profile as _vp
from Modules.Runtime.telemetry_coordinator import TelemetryCoordinator


PRESETS = ["Quiet", "Normal", "Sport", "JDM", "Extreme"]

PRESET_FLAGS = {
    "Quiet":   {"turbo": False, "flutter": False, "pops": False, "gears": False, "transmission": False, "volume": 0},
    "Normal":  {"turbo": False, "flutter": False, "pops": False, "gears": False, "transmission": False, "volume": 50},
    "Sport":   {"turbo": True,  "flutter": False, "pops": True,  "gears": True,  "transmission": True,  "volume": 80},
    "JDM":     {"turbo": True,  "flutter": True,  "pops": True,  "gears": True,  "transmission": True,  "volume": 80},
    "Extreme": {"turbo": True,  "flutter": True,  "pops": True,  "gears": True,  "transmission": True,  "volume": 100},
}


class ModdingBridge(QObject):
    """Exposes engine sound modding controls to QML."""

    presetChanged = pyqtSignal()
    featuresChanged = pyqtSignal()
    telemetryChanged = pyqtSignal()
    hybridChanged = pyqtSignal()
    outputChanged = pyqtSignal()
    soundAvailabilityChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._engine = None  # AudioEngine, lazy
        self._preset = settings_cache.get(
            "SoundFeatures", "last_preset", fallback="Quiet", path=DATA_SETTINGS
        ) or "Quiet"
        self._master_volume = 0
        # See duckForAlert(). No saved-volume slot needed the way MusicBridge
        # needs one: self._master_volume already IS the value to restore to,
        # since duck/release never touch it — only the engine's live gain.
        self._alert_ducked = False
        self._turbo = False
        self._flutter = False
        self._pops = False
        self._gears = False
        self._transmission = False
        self._hybrid_active = settings_cache.getboolean(
            "SoundFeatures", "hybrid_active", fallback=False, path=DATA_SETTINGS
        )
        self._hybrid_state = settings_cache.get(
            "SoundFeatures", "hybrid_state", fallback="ELECTRIC", path=DATA_SETTINGS
        ) or "ELECTRIC"
        self._engine_output_channel = settings_cache.get(
            "SoundFeatures", "engine_default_output", fallback="", path=DATA_SETTINGS
        ) or ""
        self._engine_output_options = [{"value": "", "label": "System Default"}]

        self._rpm = 0.0
        self._speed = 0.0
        self._gear = 0
        self._throttle = 0.0
        # Coalescing state for the QML-facing telemetryChanged emit.
        self._last_display: tuple = ()
        self._last_emit_ts: float = 0.0
        self._sound_availability = {
            "engine": False,
            "coast": False,
            "engine_start": False,
            "turbo": False,
            "transmission": False,
            "pops": False,
            "flutter": False,
        }

        # Telemetry feed for audio processing.
        #
        # Default (event-driven): subscribe to the TelemetryCoordinator so the
        # audio engine is fed the instant a fresh OBD/SIM sample lands, on the
        # coordinator thread. This removes the old 50 ms timer's quantisation
        # AND the GUI-thread scheduling jitter that made engine sound lag while
        # the UI was busy rendering OBD data.
        #
        # Fallback: set MODDING_TIMER_FALLBACK=1 to keep the original 50 ms
        # GUI-thread QTimer (kept for one release as an in-car rollback).
        self._use_timer = os.environ.get("MODDING_TIMER_FALLBACK") == "1"
        self._telem_timer = QTimer(self)
        self._telem_timer.setInterval(50)
        self._telem_timer.timeout.connect(self._process_telemetry)

        # Settings → Debugger. Sound Log prints the engine's mix state to the
        # runtime log; Test Mode drives the engine from a synthetic drive
        # instead of telemetry. Test Mode is session-only on purpose: a car
        # that boots into fake engine sound is worse than a toggle that resets.
        self._sound_log = settings_cache.getboolean(
            "SoundFeatures", "sound_log_enabled", fallback=False, path=DATA_SETTINGS
        )
        self._sound_log_last = 0.0
        self._test_mode = False
        settings_cache.set_value("SoundFeatures", "test_mode_enabled", "false", path=DATA_SETTINGS)
        self._test_t0 = 0.0
        self._test_timer = QTimer(self)
        self._test_timer.setInterval(50)
        self._test_timer.timeout.connect(self._test_tick)

        # The preset's defaults, then whatever was adjusted after it was
        # chosen (volume slider, feature toggles) — those are saved under
        # [SoundFeatures] and used to be thrown away here at every boot.
        self._apply_preset_flags()
        self._restore_saved_flags()
        self._refresh_engine_output_options()
        self._refresh_sound_availability()

    def _selected_car_folder(self) -> str:
        brand = settings_cache.get("SoundFeatures", "last_car", fallback="", path=DATA_SETTINGS) or ""
        parts = str(brand).split("/", 1)
        if len(parts) == 2:
            folder = os.path.join(BASE_DIR, "Cars", parts[0].strip(), parts[1].strip())
        else:
            folder = os.path.join(BASE_DIR, "Cars", str(brand).strip())
        return folder

    @staticmethod
    def _scan_sound_availability(folder: str) -> dict:
        out = {
            "engine": False,
            "coast": False,
            "engine_start": False,
            "turbo": False,
            "transmission": False,
            "pops": False,
            "flutter": False,
        }
        if not folder or not os.path.isdir(folder):
            return out
        try:
            stems = [
                os.path.splitext(name.lower())[0]
                for name in os.listdir(folder)
                if name.lower().endswith((".wav", ".mp3"))
            ]
        except Exception:
            return out

        for stem in stems:
            if stem.startswith("engine_on_"):
                out["engine"] = True
            if stem.startswith("coast_") or stem.startswith("engine_off_"):
                out["coast"] = True
            if stem.startswith("engine_start"):
                out["engine_start"] = True
            if stem.startswith("turbo"):
                out["turbo"] = True
            if stem.startswith("transmission"):
                out["transmission"] = True
            if stem.startswith("pop_"):
                out["pops"] = True
            if stem.startswith("flutter"):
                out["flutter"] = True
        return out

    def _refresh_sound_availability(self):
        new_state = self._scan_sound_availability(self._selected_car_folder())
        if new_state != self._sound_availability:
            self._sound_availability = new_state
            self.soundAvailabilityChanged.emit()

    # ── engine lifecycle ──────────────────────────────────────────────────
    def _ensure_engine(self):
        if self._engine is not None:
            return self._engine
        if self._preset == "Quiet":
            return None
        try:
            from Modules.Modding.audio_engine import AudioEngine
            sink = self._resolve_engine_sink()
            print(f"[engine] output sink: {sink or 'system default'}"
                  f"{'' if self._engine_output_channel else ' (auto)'}", flush=True)
            self._engine = AudioEngine(settings_path=DATA_SETTINGS, output_sink=sink)
            brand = settings_cache.get("SoundFeatures", "last_car", fallback="", path=DATA_SETTINGS)
            if brand:
                import os
                from preferences import BASE_DIR
                parts = brand.split("/", 1)
                if len(parts) == 2:
                    folder = os.path.join(BASE_DIR, "Cars", parts[0].strip(), parts[1].strip())
                else:
                    folder = os.path.join(BASE_DIR, "Cars", brand.strip())
                if os.path.isdir(folder):
                    self._engine.load_car_folder(folder)
        except Exception:
            self._engine = None
        return self._engine

    def _stop_engine(self):
        if self._engine is not None:
            try:
                self._engine.stop_all()
                self._engine.close()
            except Exception:
                pass
            self._engine = None

    # ── engine sound toggle ───────────────────────────────────────────────
    @pyqtProperty(bool, notify=presetChanged)
    def engineSoundOn(self):
        """True when engine sound is active (preset is not Quiet)."""
        return self._preset != "Quiet"

    @pyqtSlot()
    def toggleEngineSound(self):
        """Toggle engine sound on/off."""
        if self._preset != "Quiet":
            # Sound is ON → turn it OFF: save current preset, switch to Quiet
            if self._preset != "Quiet":
                settings_cache.set_value(
                    "SoundFeatures", "last_non_quiet_preset",
                    self._preset, path=DATA_SETTINGS,
                )
            self.setPreset("Quiet")
        else:
            # Sound is OFF → turn it ON: restore last non-quiet preset
            restore = settings_cache.get(
                "SoundFeatures", "last_non_quiet_preset",
                fallback="Normal", path=DATA_SETTINGS,
            ) or "Normal"
            # Back on: the preset as the user last had it, adjustments kept.
            self._set_preset(restore, reset_adjustments=False)

    # ── preset ────────────────────────────────────────────────────────────
    @pyqtProperty(str, notify=presetChanged)
    def preset(self):
        return self._preset

    @pyqtProperty(list, constant=True)
    def presets(self):
        return list(PRESETS)

    @pyqtSlot(str)
    def setPreset(self, name):
        """A preset picked by hand: its defaults replace any adjustments."""
        self._set_preset(name, reset_adjustments=True)

    def _set_preset(self, name, reset_adjustments: bool):
        if name not in PRESETS or name == self._preset:
            return
        old = self._preset
        self._preset = name
        settings_cache.set_value("SoundFeatures", "last_preset", name, path=DATA_SETTINGS)
        if name != "Quiet" and old != "Quiet":
            settings_cache.set_value("SoundFeatures", "last_non_quiet_preset", name, path=DATA_SETTINGS)
        elif name != "Quiet":
            settings_cache.set_value("SoundFeatures", "last_non_quiet_preset", name, path=DATA_SETTINGS)

        # Quiet is "off", not an adjustment — never let it overwrite the
        # saved volume/features, or every dock toggle would reset them.
        if name == "Quiet":
            self._apply_preset_flags()
        elif reset_adjustments:
            self._apply_preset_flags(persist=True)
        else:
            self._apply_preset_flags()
            self._restore_saved_flags()

        if name == "Quiet":
            self._stop_engine()
            self._stop_audio_feed()
        else:
            self._ensure_engine()
            self._sync_engine_features()
            self._start_audio_feed()
        self._sync_test_timer()

        self.presetChanged.emit()
        self.featuresChanged.emit()
        self._refresh_sound_availability()

    _FEATURE_NAMES = ("turbo", "flutter", "pops", "gears", "transmission")

    def _apply_preset_flags(self, persist: bool = False):
        flags = PRESET_FLAGS.get(self._preset, PRESET_FLAGS["Quiet"])
        self._turbo = flags["turbo"]
        self._flutter = flags["flutter"]
        self._pops = flags["pops"]
        self._gears = flags["gears"]
        self._transmission = flags["transmission"]
        self._master_volume = flags["volume"]
        if persist:
            # Choosing a preset resets the adjustable bits to its defaults —
            # write them, so the next boot restores this state and not the
            # slider position from before the preset was picked.
            cfg = settings_cache.load_copy(DATA_SETTINGS)
            if not cfg.has_section("SoundFeatures"):
                cfg.add_section("SoundFeatures")
            for name in self._FEATURE_NAMES:
                cfg.set("SoundFeatures", f"{name}_enabled", "true" if flags[name] else "false")
            cfg.set("SoundFeatures", "output_volume", str(flags["volume"]))
            settings_cache.save(cfg, DATA_SETTINGS)

    def _restore_saved_flags(self):
        """Overlay the user's saved adjustments on the preset defaults."""
        for name in self._FEATURE_NAMES:
            raw = settings_cache.get("SoundFeatures", f"{name}_enabled", fallback=None, path=DATA_SETTINGS)
            if raw is None or str(raw).strip() == "":
                continue
            setattr(self, f"_{name}", str(raw).strip().lower() in ("1", "true", "yes", "on"))
        raw = settings_cache.get("SoundFeatures", "output_volume", fallback=None, path=DATA_SETTINGS)
        try:
            if raw is not None and str(raw).strip() != "":
                self._master_volume = max(0, min(100, int(float(raw))))
        except (TypeError, ValueError):
            pass

    def _sync_engine_features(self):
        e = self._engine
        if e is None:
            return
        try:
            e.set_feature("turbo", self._turbo)
            e.set_feature("flutter", self._flutter)
            e.set_feature("pops", self._pops)
            e.set_feature("gears", self._gears)
            e.set_feature("transmission", self._transmission)
            e.set_volume(self._master_volume)
        except Exception:
            pass

    def _resolve_engine_sink(self) -> str:
        """The sink the engine stream should open on.

        An explicit choice wins. "Auto" (empty) used to mean the system
        default, which on the car becomes the R-Link the moment its A2DP link
        is up — and Bluetooth adds ~300 ms the driver hears as "the engine is
        a second behind". The USB card is wired to the speakers, so auto now
        prefers a wired ALSA output and never picks a bluez sink on its own.
        """
        if self._engine_output_channel:
            return self._engine_output_channel
        if len(self._engine_output_options) <= 1:
            self._refresh_engine_output_options()
        sinks = [o.get("value", "") for o in self._engine_output_options if o.get("value")]
        for pick in (
            lambda s: s.startswith("alsa_output.usb-"),
            lambda s: s.startswith("alsa_output.") and "hdmi" not in s,
            lambda s: s.startswith("alsa_output."),
        ):
            for sk in sinks:
                if pick(sk):
                    return sk
        return ""

    @pyqtProperty(str, notify=outputChanged)
    def engineOutputResolved(self):
        """What "Auto" currently resolves to, for the Settings hint."""
        if self._engine_output_channel:
            return self._engine_output_channel
        return self._resolve_engine_sink() or "system default"

    def _refresh_engine_output_options(self):
        options = [{"value": "", "label": "System Default"}]
        try:
            out = subprocess.check_output(
                ["bash", "-lc", "pactl list short sinks 2>/dev/null"],
                text=True,
                timeout=2.0,
            )
            for line in (out or "").splitlines():
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                sink_id = str(parts[1]).strip()
                if not sink_id:
                    continue
                options.append({"value": sink_id, "label": sink_id})
        except Exception:
            pass
        self._engine_output_options = options

    @pyqtProperty(str, notify=outputChanged)
    def engineOutputChannel(self):
        return self._engine_output_channel

    @pyqtProperty(list, notify=outputChanged)
    def engineOutputOptions(self):
        return list(self._engine_output_options)

    @pyqtSlot()
    def refreshEngineOutputOptions(self):
        self._refresh_engine_output_options()
        self.outputChanged.emit()

    @pyqtSlot(str)
    def setEngineOutputChannel(self, channel):
        clean = str(channel or "").strip()
        if clean == self._engine_output_channel:
            return
        self._engine_output_channel = clean
        settings_cache.set_value(
            "SoundFeatures", "engine_default_output", clean, path=DATA_SETTINGS
        )

        # Apply immediately if engine audio is active.
        if self._preset != "Quiet":
            was_active = self._telem_timer.isActive()
            self._stop_engine()
            self._ensure_engine()
            self._sync_engine_features()
            if was_active and not self._telem_timer.isActive():
                self._telem_timer.start()

        self.outputChanged.emit()

    # ── feature toggles ───────────────────────────────────────────────────
    @pyqtProperty(bool, notify=featuresChanged)
    def turboEnabled(self):
        return self._turbo

    @pyqtProperty(bool, notify=featuresChanged)
    def flutterEnabled(self):
        return self._flutter

    @pyqtProperty(bool, notify=featuresChanged)
    def popsEnabled(self):
        return self._pops


    @pyqtProperty(bool, notify=featuresChanged)
    def transmissionEnabled(self):
        return self._transmission

    @pyqtProperty(int, notify=featuresChanged)
    def masterVolume(self):
        return self._master_volume


    @pyqtSlot(int)
    def setMasterVolume(self, v):
        self._master_volume = max(0, min(100, v))
        settings_cache.set_value("SoundFeatures", "output_volume",
                                 str(self._master_volume), path=DATA_SETTINGS)
        if self._engine:
            try:
                self._engine.set_volume(self._master_volume)
            except Exception:
                pass
        self.featuresChanged.emit()

    # ── ducking (alert chimes) ──────────────────────────────────────────
    # Mirrors MusicBridge.duckForExternal/releaseExternalDuck in shape, but
    # simpler: there is nothing to "save" here, because self._master_volume
    # is untouched by a duck — only the engine's live gain moves, and it
    # always moves relative to whatever self._master_volume is *right now*
    # when the duck is released, so a preset/volume change mid-duck can't
    # leave the engine stuck at the ducked level.
    @pyqtSlot(int)
    def duckForAlert(self, percent):
        if self._engine is None:
            return
        pct = max(0, min(100, int(percent)))
        self._alert_ducked = True
        try:
            self._engine.set_volume(int(round(self._master_volume * (1.0 - pct / 100.0))))
        except Exception:
            pass

    @pyqtSlot()
    def releaseAlertDuck(self):
        self._alert_ducked = False
        if self._engine is None:
            return
        try:
            self._engine.set_volume(self._master_volume)
        except Exception:
            pass

    # ── hybrid mode ───────────────────────────────────────────────────────
    @pyqtProperty(bool, notify=hybridChanged)
    def hybridActive(self):
        return self._hybrid_active


    @pyqtSlot(bool)
    def setHybridActive(self, active):
        self._hybrid_active = active
        settings_cache.set_value("SoundFeatures", "hybrid_active",
                                 "true" if active else "false", path=DATA_SETTINGS)
        self.hybridChanged.emit()


    # ── telemetry display ─────────────────────────────────────────────────
    @pyqtProperty(float, notify=telemetryChanged)
    def rpm(self):
        return self._rpm

    @pyqtProperty(float, notify=telemetryChanged)
    def rpmLatencyMs(self):
        """Adapter → engine-sound RPM latency, smoothed (see _note_rpm_latency)."""
        return round(1000.0 * self._rpm_lat_ema, 0)

    @pyqtProperty(float, notify=telemetryChanged)
    def speed(self):
        return self._speed

    @pyqtProperty(int, notify=telemetryChanged)
    def gear(self):
        return self._gear

    @pyqtProperty(float, notify=telemetryChanged)
    def throttle(self):
        return self._throttle

    # ── telemetry feed control ────────────────────────────────────────────
    def _start_audio_feed(self):
        """Begin feeding telemetry to the audio engine — either via the
        coordinator subscription (default) or the legacy 50 ms timer."""
        if self._use_timer:
            if not self._telem_timer.isActive():
                self._telem_timer.start()
        else:
            # subscribe() de-dupes, so repeated calls are safe.
            TelemetryCoordinator.instance().subscribe(self._on_coordinator_snapshot)

    def _stop_audio_feed(self):
        if self._use_timer:
            self._telem_timer.stop()
        else:
            TelemetryCoordinator.instance().unsubscribe(self._on_coordinator_snapshot)

    # ── telemetry processing ──────────────────────────────────────────────
    def _process_telemetry(self):
        # Legacy timer path: pull the latest snapshot and feed it.
        self._feed_telemetry(TelemetryCoordinator.instance().snapshot())

    def _on_coordinator_snapshot(self, snap):
        # Event-driven path: runs on the coordinator thread the instant a fresh
        # sample lands. `snap` is shared and read-only here. process_sample()
        # takes the engine's state lock, so calling it off the GUI thread is
        # safe; telemetryChanged.emit() is auto-queued to the GUI thread for QML.
        self._feed_telemetry(snap)

    def _feed_telemetry(self, snap):
        def _val(key, default=0.0):
            entry = snap.get(key)
            if isinstance(entry, dict):
                v = entry.get("value", default)
            else:
                v = entry
            try:
                return float(v) if v is not None else float(default)
            except (TypeError, ValueError):
                return float(default)

        self._rpm = _val("RPM")
        self._speed = _val("Speed")
        self._gear = int(_val("Gear"))
        self._throttle = _val("Driver Demand", _val("Throttle Pos"))

        # The audio engine below is fed at the full coordinator rate — that is
        # the whole point of the event-driven feed. QML is not: every emit
        # crosses threads and re-evaluates `Modding.rpm/speed/gear/throttle`
        # bindings on all seven always-loaded pages, hidden ones included.
        # Coalesce to ~10 Hz, and only when a displayed value actually moved.
        now = time.monotonic()
        display = (round(self._rpm), round(self._speed), self._gear, round(self._throttle))
        if display != self._last_display and (now - self._last_emit_ts) >= 0.1:
            self._last_display = display
            self._last_emit_ts = now
            self.telemetryChanged.emit()

        e = self._engine
        if e is not None and self._preset != "Quiet" and not self._test_mode:
            try:
                throttle_norm = max(0.0, min(1.0, float(self._throttle) / 100.0))
                e.process_sample(
                    rpm=int(self._rpm),
                    gear=self._gear or None,
                    throttle=throttle_norm,
                    speed=self._speed or None,
                    clutch=None,
                )
            except Exception:
                pass
            self._note_rpm_latency(snap, now)
            self._maybe_sound_log(now)

    # ── Settings → Debugger: Sound Log ────────────────────────────────────
    @pyqtProperty(bool, notify=featuresChanged)
    def soundLogEnabled(self):
        return self._sound_log

    @pyqtSlot(bool)
    def setSoundLog(self, enabled):
        self._sound_log = bool(enabled)
        settings_cache.set_value("SoundFeatures", "sound_log_enabled",
                                 "true" if self._sound_log else "false", path=DATA_SETTINGS)
        print(f"[sound] log {'on' if self._sound_log else 'off'}", flush=True)
        self.featuresChanged.emit()

    def _maybe_sound_log(self, now: float) -> None:
        """One line of engine mix state every 500 ms while Sound Log is on."""
        if not self._sound_log or self._engine is None or now - self._sound_log_last < 0.5:
            return
        self._sound_log_last = now
        try:
            print(f"[sound] {self._engine.get_debug_state()}", flush=True)
        except Exception:
            pass

    # ── Settings → Debugger: Test Mode ────────────────────────────────────
    # A synthetic drive for hearing the engine sound without a car: idle, pull
    # through gears 1-5, lift and coast back to idle, on a 24 s loop. It only
    # runs while an engine-sound preset is active and test mode is on.
    _TEST_LOOP_S = 24.0

    @pyqtProperty(bool, notify=featuresChanged)
    def testModeEnabled(self):
        return self._test_mode

    @pyqtSlot(bool)
    def setTestMode(self, enabled):
        self._test_mode = bool(enabled)
        settings_cache.set_value("SoundFeatures", "test_mode_enabled",
                                 "true" if self._test_mode else "false", path=DATA_SETTINGS)
        print(f"[sound] test mode {'on' if self._test_mode else 'off'}", flush=True)
        self._sync_test_timer()
        self.featuresChanged.emit()

    def _sync_test_timer(self) -> None:
        run = self._test_mode and self._preset != "Quiet"
        if run and not self._test_timer.isActive():
            self._ensure_engine()
            self._test_t0 = time.monotonic()
            self._test_timer.start()
        elif not run and self._test_timer.isActive():
            self._test_timer.stop()

    @classmethod
    def _test_sample(cls, t: float) -> tuple:
        """(rpm, gear, throttle 0-1, speed km/h) at time t of the loop."""
        t = t % cls._TEST_LOOP_S
        if t < 3.0:                                   # idle
            return 850, 0, 0.0, 0.0
        if t < 18.0:                                  # 3 s per gear, 1..5
            g = int((t - 3.0) // 3.0) + 1
            f = ((t - 3.0) % 3.0) / 3.0               # 0→1 within the gear
            rpm = 1800 + f * 3200                     # 1800 → 5000
            speed = (g - 1) * 25 + f * 25             # 0 → 125 km/h over the run
            return int(rpm), g, 0.85, speed
        f = (t - 18.0) / (cls._TEST_LOOP_S - 18.0)    # lift off, coast to idle
        return int(3000 - f * 2150), 5 if f < 0.7 else 0, 0.0, max(0.0, 125 * (1 - f))

    def _test_tick(self) -> None:
        e = self._engine
        if e is None or self._preset == "Quiet" or not self._test_mode:
            self._test_timer.stop()
            return
        rpm, gear, throttle, speed = self._test_sample(time.monotonic() - self._test_t0)
        try:
            e.process_sample(rpm=rpm, gear=gear or None, throttle=throttle,
                             speed=speed or None, clutch=None)
        except Exception:
            pass
        self._maybe_sound_log(time.monotonic())

    # ── adapter → engine-sound latency (diagnostic) ──────────────────────
    # "The engine is a second behind" needs a number before it gets a fix:
    # TelemetryHub stamps each RPM sample with the monotonic time it left
    # the adapter; the gap to process_sample() here is the whole telemetry
    # chain (poll cadence, filters, coordinator hand-off). Everything after
    # this point is the audio path (block lerp, PortAudio buffer, the sink —
    # Bluetooth adds ~300 ms on its own). Logged with [Debug] telemetry_timing.
    _rpm_lat_ema: float = 0.0
    _rpm_lat_max: float = 0.0
    _rpm_lat_last_log: float = 0.0
    _rpm_lat_last_stamp: float = 0.0

    def _note_rpm_latency(self, snap, now: float) -> None:
        try:
            stamp = float((snap.get("RPM") or {}).get("sample_mono") or 0.0)
        except Exception:
            return
        if not stamp or stamp == self._rpm_lat_last_stamp:
            return  # same sample as last tick — not a new measurement
        self._rpm_lat_last_stamp = stamp
        lat = max(0.0, now - stamp)
        self._rpm_lat_ema += 0.1 * (lat - self._rpm_lat_ema)
        self._rpm_lat_max = max(self._rpm_lat_max, lat)
        if now - self._rpm_lat_last_log >= 10.0:
            self._rpm_lat_last_log = now
            try:
                from Modules.Runtime.trip_logger import log as _tl
                _tl("engine", "latency", rpm=round(self._rpm, 0), throttle=round(self._throttle, 0),
                    latency_ms=round(1000 * self._rpm_lat_ema, 0),
                    sink=self._engine_output_channel or "default")
            except Exception:
                pass
            if settings_cache.getboolean("Debug", "telemetry_timing", fallback=False, path=DATA_SETTINGS):
                print(
                    f"[engine] adapter→sound RPM latency avg {1000 * self._rpm_lat_ema:.0f} ms, "
                    f"max {1000 * self._rpm_lat_max:.0f} ms (10 s) | sink "
                    f"{self._engine_output_channel or 'default'}",
                    flush=True,
                )
            self._rpm_lat_max = 0.0

    # ── page lifecycle ────────────────────────────────────────────────────
    @pyqtSlot()
    def activate(self):
        self._refresh_sound_availability()
        if self._preset != "Quiet":
            self._ensure_engine()
            self._sync_engine_features()
            self._start_audio_feed()
            self._sync_test_timer()

    @pyqtSlot()
    def deactivate(self):
        self._test_timer.stop()
        self._stop_audio_feed()
        self._stop_engine()

    def cleanup(self):
        self._test_timer.stop()
        self._stop_audio_feed()
        self._stop_engine()

    # ── car asset resolution ────────────────────────────────────────────
    carChanged = pyqtSignal()

    @pyqtProperty(str, notify=carChanged)
    def carImagePath(self):
        """Resolve side.png for the currently selected car."""
        p = resolve_car_asset("side.png", require_theme_active=False)
        return p if p else os.path.join(icons_path, "default_car.png")

    @pyqtProperty(str, notify=carChanged)
    def carBackPath(self):
        """Resolve back.png for the currently selected car."""
        p = resolve_car_asset("back.png", require_theme_active=False)
        return p if p else os.path.join(icons_path, "back.png")

    @pyqtProperty(str, notify=carChanged)
    def carTopPath(self):
        """Resolve top.png — car-specific only when engine sound preset is active."""
        if self._preset != "Quiet":
            p = resolve_car_asset("top.png", require_theme_active=False)
            if p:
                return p
        return os.path.join(icons_path, "top.png")

    @pyqtProperty(str, notify=carChanged)
    def carLogoPath(self):
        """Resolve logo for the currently selected car's brand."""
        p = resolve_car_asset("logo.png", prefer_brand_for_logo=True, require_theme_active=False)
        return p if p else os.path.join(icons_path, "modding.png")

    @pyqtProperty(str, notify=carChanged)
    def carName(self):
        """Return the model name of the selected car."""
        _, model = get_selected_car()
        return model or _vp.get("short_name", "")

    @pyqtProperty(str, notify=carChanged)
    def carBrand(self):
        """Return the brand name of the selected car."""
        brand, _ = get_selected_car()
        return brand or ""

    @pyqtProperty("QVariantMap", notify=soundAvailabilityChanged)
    def soundAvailability(self):
        return dict(self._sound_availability)

    @pyqtProperty(list, notify=carChanged)
    def carBrands(self):
        """Return list of available car brand folders."""
        cars_dir = os.path.join(BASE_DIR, "Cars")
        if not os.path.isdir(cars_dir):
            return []
        return sorted(
            d for d in os.listdir(cars_dir)
            if os.path.isdir(os.path.join(cars_dir, d))
            and not d.startswith(".")
        )

    @pyqtSlot(str, result=list)
    def carModels(self, brand):
        """Return list of model folders within a brand."""
        brand_dir = os.path.join(BASE_DIR, "Cars", brand)
        if not os.path.isdir(brand_dir):
            return []
        return sorted(
            d for d in os.listdir(brand_dir)
            if os.path.isdir(os.path.join(brand_dir, d))
            and not d.startswith(".")
        )

    @pyqtSlot(str, result=str)
    def brandLogoPath(self, brand):
        """Return path to a brand's logo image."""
        brand_dir = os.path.join(BASE_DIR, "Cars", brand)
        for ext in ("png", "jpg", "jpeg"):
            p = os.path.join(brand_dir, f"logo.{ext}")
            if os.path.exists(p):
                return p
        return os.path.join(icons_path, "modding.png")

    @pyqtSlot(str, str, result=str)
    def modelPreviewPath(self, brand, model):
        """Return path to a model's preview/side image."""
        model_dir = os.path.join(BASE_DIR, "Cars", brand, model)
        for name in ("side.png", "side.jpg", "preview.png", "preview.jpg"):
            p = os.path.join(model_dir, name)
            if os.path.exists(p):
                return p
        return os.path.join(icons_path, "side.png")

    @pyqtSlot(str, str)
    def loadCar(self, brand, model):
        """Select a car brand/model and save to settings."""
        brand = (brand or "").strip()
        model = (model or "").strip()
        if not brand or not model:
            return

        settings_cache.set_value(
            "SoundFeatures", "last_car", f"{brand}/{model}", path=DATA_SETTINGS
        )

        # Hot-load newly selected car immediately while engine sound is active.
        car_folder = os.path.join(BASE_DIR, "Cars", brand, model)
        if self._preset != "Quiet":
            e = self._ensure_engine()
            if e is not None and os.path.isdir(car_folder):
                try:
                    e.load_car_folder(car_folder)
                except Exception:
                    pass

        self._refresh_sound_availability()
        self.carChanged.emit()
        self.presetChanged.emit()
