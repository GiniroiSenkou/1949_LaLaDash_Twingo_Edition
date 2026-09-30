from __future__ import annotations

import time

from PyQt6.QtCore import QObject, QTimer, pyqtProperty, pyqtSignal, pyqtSlot

from Modules.Runtime.telemetry_coordinator import TelemetryCoordinator
from Modules.Runtime import vehicle_profile as _vp
from preferences import (
    COLOR_STATUS_GREEN,
    COLOR_STATUS_ORANGE,
    COLOR_STATUS_RED,
    COLOR_STATUS_NA,
)


class TelemetryBridge(QObject):
    """Exposes telemetry snapshot to QML via typed properties.

    The TelemetryHub snapshot uses a nested dict format:
        {"RPM": {"value": 1234.0, "text": "1234", "unit": "rpm", ...}, ...}
    All accessors handle this nested structure.
    """

    changed = pyqtSignal()
    # Fires only when the set of active dash-light keys actually changes.
    # Bound to activeDashLights / activeDashLightKeys so the Dashboard
    # Repeater doesn't rebuild its Image children on every 80 ms tick.
    dashLightsChanged = pyqtSignal()
    # CAN "reverse" state (Data/can_signal_map.json), transitions only.
    reverseGearChanged = pyqtSignal()

    # Coordinator emits every 50 ms. Coalesce bridge re-emits so QML doesn't
    # re-evaluate the ~30 bound pyqtProperty notifiers 20×/sec on every page.
    # 80 ms ≈ 12 Hz — perceptually identical for a speedometer needle but
    # ~40 % less QML render work on the Pi 4B.
    _EMIT_MIN_INTERVAL_S = 0.080

    def __init__(self, parent=None):
        super().__init__(parent)
        self._snap: dict = {}
        self._connected = False
        self._sim_enabled = False
        self._last_emit_ts = 0.0
        self._emit_timer = QTimer(self)
        self._emit_timer.setSingleShot(True)
        self._emit_timer.timeout.connect(self._coalesced_emit)
        self._cached_dash_lights: tuple = ()
        self._reverse = False
        self._reverse_override = None   # debug probe / simulator
        # See _maybe_update_trips(): formatted once per coordinator tick and
        # cached here so the totalKm/todayKm property getters — read directly
        # by QML bindings at ~12.5 Hz — are plain string returns, never I/O.
        self._display_total_km = "0.0 km"
        self._display_today_km = "0.0 km"
        self._display_today_date = ""

        coord = TelemetryCoordinator.instance()
        coord.changed.connect(self._on_changed)

    # ── snapshot update ───────────────────────────────────────────────────
    @pyqtSlot(dict)
    def _on_changed(self, snap: dict):
        coord = TelemetryCoordinator.instance()
        self._snap = snap
        self._connected = coord.connected()
        self._sim_enabled = coord.sim_enabled()
        self._maybe_emit_dash_lights()
        self._maybe_update_reverse()
        self._maybe_update_trips()
        self._update_battery_soc()
        now = time.monotonic()
        elapsed = now - self._last_emit_ts
        if elapsed >= self._EMIT_MIN_INTERVAL_S:
            self._last_emit_ts = now
            self.changed.emit()
        elif not self._emit_timer.isActive():
            remaining_ms = int((self._EMIT_MIN_INTERVAL_S - elapsed) * 1000) + 1
            self._emit_timer.start(max(1, remaining_ms))

    def _maybe_update_reverse(self):
        """`CAN reverse` from the signal map → reverseGear, emitted on change
        only (the action cam's reverse overlay hangs off it)."""
        if self._reverse_override is not None:
            rev = bool(self._reverse_override)
        else:
            e = self._snap.get("CAN reverse")
            v = e.get("value") if isinstance(e, dict) else e
            try:
                rev = bool(v) and float(v) >= 0.5
            except (TypeError, ValueError):
                rev = False
        if rev != self._reverse:
            self._reverse = rev
            self.reverseGearChanged.emit()

    @pyqtProperty(bool, notify=reverseGearChanged)
    def reverseGear(self):
        return self._reverse


    def _coalesced_emit(self):
        self._last_emit_ts = time.monotonic()
        self.changed.emit()

    def _maybe_emit_dash_lights(self):
        """Emit dashLightsChanged only when the active set actually differs.
        Cached as a tuple so == comparison is cheap; QML readers get a fresh
        list each time so they can mutate it freely."""
        try:
            from Modules.Layout.dash_lights_hub import DashLightsHub
            current = tuple(DashLightsHub.instance().get_active())
        except Exception:
            current = ()
        if current != self._cached_dash_lights:
            self._cached_dash_lights = current
            self.dashLightsChanged.emit()

    def _maybe_update_trips(self):
        """Refresh the cached totalKm/todayKm display strings.

        Runs on the coordinator's own tick (up to ~20 Hz in sim), not the
        throttled QML re-emit — but `peek_trips_snapshot()` is a read-only
        formatter (see trips_store.py), so this is a cheap dict/string
        operation, never the disk write that used to live behind these two
        properties. Actual accumulation + persistence stays solely on
        `_EngineStatePoll`'s 350 ms poll in main.py.
        """
        try:
            from Modules.Settings.trips_store import peek_trips_snapshot
            trips = peek_trips_snapshot()
            self._display_total_km = str(trips.get("display_total_km", "0.0 km"))
            self._display_today_km = str(trips.get("display_today_km", "0.0 km"))
            self._display_today_date = str(trips.get("today_key", ""))
        except Exception:
            pass

    # ── helpers (nested snapshot access) ──────────────────────────────────
    def _entry(self, key) -> dict:
        """Get a snapshot entry dict, or empty dict if missing."""
        e = self._snap.get(key)
        return e if isinstance(e, dict) else {}

    def _f(self, key, default=0.0):
        """Get float value from nested snapshot entry."""
        try:
            v = self._entry(key).get("value", default)
            return float(v) if v is not None else float(default)
        except (TypeError, ValueError):
            return float(default)

    def _s(self, key, default=""):
        """Get text string from nested snapshot entry."""
        v = self._entry(key).get("text", default)
        return str(v) if v is not None else str(default)

    def _i(self, key, default=0):
        """Get integer value from nested snapshot entry."""
        try:
            v = self._entry(key).get("value", default)
            return int(float(v)) if v is not None else int(default)
        except (TypeError, ValueError):
            return int(default)

    def _color(self, key, default="#888888"):
        """Get the status color hex string from a snapshot entry."""
        c = self._entry(key).get("color", default)
        return str(c) if c else str(default)

    def _value_text_color(self, key):
        """Telemetry's value-text colour override, or "" for none.

        "" lets QML fall back to Theme.ink, so a plain reading follows the
        active palette (white on the dark skin, near-black on the light one)."""
        c = self._entry(key).get("value_text_color")
        return str(c) if c else ""

    @pyqtSlot(str, result=list)
    def gaugeBand(self, key):
        """(warn, danger, redline, full-scale) for a metric, [] if it has none.

        Read once into a `readonly property var` on the QML side — these are
        constants, not live values. Sourced from GAUGE_BANDS in
        Modules/Obd/telemetry.py, which is the same tuple the colour lambda
        uses, so a bargraph's zones cannot disagree with the value's colour.
        """
        try:
            from Modules.Obd.telemetry import GAUGE_BANDS
            return [float(v) for v in GAUGE_BANDS[str(key)]]
        except Exception:
            return []


    # ── connection state ──────────────────────────────────────────────────
    @pyqtProperty(bool, notify=changed)
    def connected(self):
        return self._connected or self._sim_enabled


    # ── core telemetry ────────────────────────────────────────────────────
    @pyqtProperty(float, notify=changed)
    def rpm(self):
        return self._f("RPM")

    @pyqtProperty(float, notify=changed)
    def speed(self):
        return self._f("Speed")

    @pyqtProperty(float, notify=changed)
    def fuelLevel(self):
        return self._f("Fuel Level")

    @pyqtProperty(float, notify=changed)
    def fuelRawLevel(self):
        try:
            v = self._entry("Fuel Level").get("raw_value")
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    # ── fuel gauge calibration from the car's own gauge ──────────────────
    # The reference car's sender is far from linear: raw 28.6 % showed as 4 of the
    # dash's 6 bars (2026-09-15), where the affine offset/scale said 40 %.
    # Rather than guess a curve, let the driver teach it: "the dash shows N
    # bars right now" pairs the current raw reading with that bar's
    # mid-point and grows [Calibration] fuel_lut one point at a time.
    _GAUGE_BARS = int(_vp.get("fuel_gauge_bars", 6))
    _LUT_MERGE_RAW_PCT = 3.0   # a new sample replaces an old one this close

    @pyqtSlot(result=bool)
    def clearFuelCalibration(self):
        """Forget the taught curve and fall back to offset/scale.

        Without this a bad sample was permanent: nothing in the UI could undo
        a teach, so one wrong tap left the gauge reading wrong forever.
        """
        try:
            from Modules.Runtime import settings_cache
            from preferences import DATA_SETTINGS
            cfg = settings_cache.load_copy(DATA_SETTINGS)
            if not cfg.has_section("Calibration"):
                cfg.add_section("Calibration")
            cfg.set("Calibration", "fuel_lut", "")
            cfg.set("Calibration", "fuel_use_lut", "false")
            if not settings_cache.save(cfg, DATA_SETTINGS):
                return False
            try:
                from Modules.Obd.telemetry import TelemetryHub
                TelemetryHub.instance().reload_fuel_calibration()
            except Exception:
                pass
            print("[fuel] calibration cleared — back to offset/scale", flush=True)
            self.changed.emit()
            return True
        except Exception as exc:
            print(f"[fuel] clearFuelCalibration failed: {exc}", flush=True)
            return False

    @pyqtProperty(str, notify=changed)
    def fuelCalibrationSummary(self):
        try:
            from Modules.Runtime import settings_cache
            from preferences import DATA_SETTINGS
            use = settings_cache.getboolean("Calibration", "fuel_use_lut", fallback=False, path=DATA_SETTINGS)
            lut = settings_cache.get("Calibration", "fuel_lut", fallback="", path=DATA_SETTINGS) or ""
            n = len([p for p in lut.split(",") if ":" in p])
            if use and n:
                return f"Curve from {n} gauge samples"
            return "Offset/scale only — tap the bars the dash shows to teach the curve"
        except Exception:
            return ""

    @pyqtSlot(int, result=bool)
    def recordFuelGaugeBars(self, bars):
        """Pair the current raw sender % with what the dash gauge shows."""
        try:
            from Modules.Runtime import settings_cache
            from preferences import DATA_SETTINGS
            bars = max(0, min(self._GAUGE_BARS, int(bars)))
            raw = self.fuelRawLevel
            if raw <= 0.0:
                return False
            # Bar N lit means the level sits between (N-1)/6 and N/6: use the
            # middle of that band. 0 bars = reserve, call it 3 %.
            target = 3.0 if bars == 0 else (bars - 0.5) * 100.0 / self._GAUGE_BARS
            pts: list[tuple[float, float]] = []
            lut = settings_cache.get("Calibration", "fuel_lut", fallback="", path=DATA_SETTINGS) or ""
            for pair in lut.split(","):
                if ":" not in pair:
                    continue
                a, b = pair.split(":", 1)
                try:
                    pts.append((float(a), float(b)))
                except ValueError:
                    continue
            # Replace anything measured at (about) this raw level, keep the
            # fixed end points, and keep the curve monotonic.
            #
            # Three things get dropped, and the last two are what stopped the
            # gauge moving. (1) a sample at about this raw level; (2) a sample
            # for this same bar — tapping "6 bars" at a different raw reading
            # must MOVE that point, not add a second one with the same y, and
            # two points sharing a y pin the gauge flat between them; (3) a
            # sample this one contradicts, since a lower raw reading cannot
            # mean a fuller tank. The old code kept contradictions and clamped
            # y monotonic instead, which manufactured exactly those flat runs.
            pts = [p for p in pts
                   if abs(p[0] - raw) > self._LUT_MERGE_RAW_PCT
                   and abs(p[1] - target) > 0.5
                   and not (p[0] < raw and p[1] >= target)
                   and not (p[0] > raw and p[1] <= target)
                   and 0.0 < p[0] < 100.0]
            pts.append((round(raw, 1), round(target, 1)))
            pts.sort()
            mono: list[tuple[float, float]] = []
            last_y = 0.0
            for x, y in pts:
                y = max(y, last_y)
                mono.append((x, y))
                last_y = y
            mono = [(0.0, 0.0)] + mono + [(100.0, 100.0)]
            text = ", ".join(f"{x:g}:{y:g}" for x, y in mono)
            cfg = settings_cache.load_copy(DATA_SETTINGS)
            if not cfg.has_section("Calibration"):
                cfg.add_section("Calibration")
            cfg.set("Calibration", "fuel_lut", text)
            cfg.set("Calibration", "fuel_use_lut", "true")
            if not settings_cache.save(cfg, DATA_SETTINGS):
                return False
            try:
                from Modules.Obd.telemetry import TelemetryHub
                TelemetryHub.instance().reload_fuel_calibration()
            except Exception:
                pass
            print(f"[fuel] gauge {bars}/{self._GAUGE_BARS} bars at raw {raw:.1f}% → lut {text}", flush=True)
            self.changed.emit()
            return True
        except Exception as exc:
            print(f"[fuel] recordFuelGaugeBars failed: {exc}", flush=True)
            return False

    @pyqtProperty(str, notify=changed)
    def fuelText(self):
        v = self._entry("Fuel Level").get("value")
        if v is None:
            return "—%"
        try:
            return f"{int(float(v))}%"
        except (TypeError, ValueError):
            return "—%"

    @pyqtProperty(float, notify=changed)
    def coolantTemp(self):
        return self._f("Coolant")

    @pyqtProperty(float, notify=changed)
    def oilTemp(self):
        return self._f("Oil Temp")

    @pyqtProperty(str, notify=changed)
    def oilTempStatus(self):
        e = self._entry("Oil Temp")
        if e.get("stale"):
            return str(e.get("status") or "Last valid oil temperature")
        return "Live" if e.get("value") is not None else "N/A"

    @pyqtProperty(float, notify=changed)
    def intakeTemp(self):
        return self._f("Intake Air")

    @pyqtProperty(float, notify=changed)
    def throttle(self):
        return self._f("Throttle Pos")

    @pyqtProperty(str, notify=changed)
    def throttleSource(self):
        return str(self._entry("Throttle Pos").get("source") or "Throttle Pos")

    @pyqtProperty(int, notify=changed)
    def gear(self):
        return self._i("Gear")

    @pyqtProperty(float, notify=changed)
    def voltage(self):
        return self._f("ECU Voltage")


    @pyqtProperty(float, notify=changed)
    def engineLoad(self):
        return self._f("Absolute Load")


    # ── the four extra dashboard readouts ────────────────────────────────
    # Chosen as the most informative PIDs the Dashboard did not already show,
    # and deliberately not restatements of one another:
    #   Boost  — the engine is turbocharged; this is the one number that says
    #            whether the turbo is actually making pressure.
    #   Timing — ignition advance being pulled is the earliest sign of knock or
    #            bad fuel, and nothing else on the page hints at it.
    #   Lambda — commanded mixture: open vs closed loop, and enrichment on load.
    #   Pedal  — pedal against Throttle Pos is the drive-by-wire cross-check;
    #            the two disagreeing is a fault you cannot see any other way.
    # Text readouts, so a PID the ECU does not answer reads "N/A" rather than
    # a confident 0.
    @pyqtProperty(str, notify=changed)
    def manifoldPressureText(self):
        return self._metric_text("Manifold Press", 0)

    @pyqtProperty(str, notify=changed)
    def manifoldPressureColor(self):
        return self._color("Manifold Press")


    @pyqtProperty(str, notify=changed)
    def timingAdvanceText(self):
        return self._metric_text("Timing Adv", 1)

    @pyqtProperty(str, notify=changed)
    def timingAdvanceColor(self):
        return self._color("Timing Adv")


    @pyqtProperty(str, notify=changed)
    def equivRatioText(self):
        return self._metric_text("Equiv. Ratio", 2)

    @pyqtProperty(str, notify=changed)
    def equivRatioColor(self):
        return self._color("Equiv. Ratio")


    @pyqtProperty(str, notify=changed)
    def accelPedalText(self):
        return self._metric_text("Accel Pedal D", 0)

    @pyqtProperty(str, notify=changed)
    def accelPedalColor(self):
        return self._color("Accel Pedal D")

    def _metric_text(self, key, decimals):
        """Formatted value, or "N/A" when the ECU never answered the PID.

        A missing PID is common — half of these are optional in the standard —
        and showing 0 for one is worse than showing nothing, because 0 boost or
        0 degrees of advance is a reading a driver would act on.
        """
        entry = self._entry(key)
        value = entry.get("value")
        if value is None:
            return "N/A"
        try:
            return f"{float(value):.{decimals}f}"
        except (TypeError, ValueError):
            return "N/A"

    # ── metric status colors (from telemetry band functions) ─────────────
    @pyqtProperty(str, notify=changed)
    def rpmColor(self):
        return self._color("RPM")

    @pyqtProperty(str, notify=changed)
    def speedColor(self):
        return self._color("Speed")

    @pyqtProperty(str, notify=changed)
    def gearColor(self):
        return self._color("Gear")


    @pyqtProperty(str, notify=changed)
    def throttleColor(self):
        return self._color("Throttle Pos")

    @pyqtProperty(str, notify=changed)
    def voltageColor(self):
        return self._color("ECU Voltage")

    @pyqtProperty(str, notify=changed)
    def rpmValueTextColor(self):
        return self._value_text_color("RPM")

    @pyqtProperty(str, notify=changed)
    def coolantValueTextColor(self):
        return self._value_text_color("Coolant")

    @pyqtProperty(str, notify=changed)
    def oilTempValueTextColor(self):
        return self._value_text_color("Oil Temp")

    @pyqtProperty(str, notify=changed)
    def fuelValueTextColor(self):
        return self._value_text_color("Fuel Level")

    @pyqtProperty(float, notify=changed)
    def maf(self):
        return self._f("MAF")

    # ── OBD status ────────────────────────────────────────────────────────
    @pyqtProperty(str, notify=changed)
    def obdStatus(self):
        if self._sim_enabled:
            rpm = self._f("RPM")
            if rpm < 10.0:
                return "Simulator Active - Engine Off"
            return "Simulator Active - Engine On"
        if not self._connected:
            return "OBD Disconnected"
        rpm = self._f("RPM")
        if rpm < 10.0:
            return "OBD Connected - Engine Off"
        return "OBD Connected - Engine On"

    # ── formatted telemetry strings ───────────────────────────────────────
    @pyqtProperty(str, notify=changed)
    def runTime(self):
        txt = self._s("Run Time", "N/A")
        if txt and txt != "N/A":
            return f"{txt} s"
        return "N/A"

    @pyqtProperty(str, notify=changed)
    def runTimeText(self):
        """Engine-on time as H:MM:SS / M:SS, or an em dash when unknown.

        `runTime` hands QML the raw "NNN s" the OBD PID reports, which is
        unreadable at a glance on a screensaver. Formatted here rather than in
        QML so every caller gets the same rendering, and because the em-dash
        fallback matters: "N/A s" is worse than nothing.
        """
        try:
            secs = int(self._f("Run Time", -1))
        except Exception:
            secs = -1
        if secs < 0:
            return "\u2014"
        hours, rem = divmod(secs, 3600)
        mins, sec = divmod(rem, 60)
        if hours:
            return f"{hours}:{mins:02d}:{sec:02d}"
        return f"{mins}:{sec:02d}"

    @pyqtProperty(str, notify=changed)
    def distSinceClear(self):
        txt = self._s("Dist. since clear", "N/A")
        if txt and txt != "N/A":
            return f"{txt} km"
        return "N/A"

    # ── battery / charging ────────────────────────────────────────────────
    # A voltage alone does not answer the question the Home tile is there to
    # answer — "is the car putting charge back in, or am I running the battery
    # down?" That depends on whether the engine is turning: ~12.6 V is a healthy
    # resting battery but a dead alternator once the engine is running.
    _CHARGING_MIN_V = 13.2      # alternator regulating
    # "At rest" here means ignition on, engine off — the head unit, screen
    # and Pi are all on the rail, so this is a loaded reading, not an
    # open-circuit one. The reference car measures 12.1-12.2 V in that state and
    # the old 12.4 V line called that "Low" every time the car was parked.
    _RESTING_OK_V   = 12.0
    _RESTING_LOW_V  = 11.6
    # Keep the last good reading across a missed poll so the tile does not
    # blink to N/A between samples (~0.6 s cadence, 2 s stale rule).
    _BATTERY_HOLD_S = 6.0
    _last_batt_v: float = 0.0
    _last_batt_ts: float = 0.0

    def _battery_volts(self) -> float:
        """ECU supply voltage, falling back to the adapter's own reading.

        Many ELM327 clones answer ELM_VOLTAGE when the ECU refuses
        CONTROL_MODULE_VOLTAGE, and for this purpose they measure the same rail.
        """
        v = self._f("ECU Voltage")
        if v <= 0.5:
            v = self._f("ELM Voltage")
        now = time.monotonic()
        if v > 0.5:
            self._last_batt_v = v
            self._last_batt_ts = now
        elif now - self._last_batt_ts <= self._BATTERY_HOLD_S:
            v = self._last_batt_v
        return v

    @pyqtProperty(str, notify=changed)
    def batteryText(self):
        v = self._battery_volts()
        if v <= 0.5:
            return "N/A"
        return f"{v:.1f} V"

    @pyqtProperty(str, notify=changed)
    def batteryState(self):
        """One of: charging / draining / resting / low / unknown."""
        v = self._battery_volts()
        if v <= 0.5:
            return "unknown"
        running = self._f("RPM") >= 300.0
        if running:
            return "charging" if v >= self._CHARGING_MIN_V else "draining"
        if v >= self._RESTING_OK_V:
            return "resting"
        return "low" if v >= self._RESTING_LOW_V else "draining"

    @pyqtProperty(str, notify=changed)
    def batteryStatusText(self):
        return {
            "charging": "Charging",
            "draining": "Not charging",
            "resting":  "At rest",
            "low":      "Low",
        }.get(self.batteryState, "No reading")

    # ── battery state of charge (estimate) ───────────────────────────────
    # A lead-acid 12 V battery's *resting* voltage tracks its charge closely
    # enough for a gauge: ~12.65 V full, 12.24 V half, 11.9 V empty. That
    # only holds while nothing charges it — with the engine running the
    # alternator pins the rail near 14 V and the voltage says nothing about
    # charge. So: measured while the engine is off (ignition on, the rail
    # carrying the R-Link + screen + Pi, hence a small load offset added
    # back), held while driving with a charging marker. If the body computer
    # turns out to broadcast a real SoC on CAN, a confirmed `battery_soc`
    # signal replaces this estimate ("Battery SoC" snapshot key).
    _OCV_TABLE = ((11.85, 0.0), (12.06, 25.0), (12.24, 50.0), (12.45, 75.0), (12.65, 100.0))
    _SOC_STABLE_V = 0.06        # spread over the sample window that counts as "settled"
    _SOC_WINDOW = 5             # samples (~3 s at the 0.6 s ECU-voltage cadence)
    _soc_percent: float = -1.0  # -1 = never measured
    _soc_charging: bool = False
    _soc_samples: list = []
    _soc_last_ts: float = 0.0
    # Learning the full point (see _maybe_learn_full): alternator time this
    # drive, persisted once a minute so a key-off that cuts the Pi's power
    # loses at most that much.
    _chg_accum_s: float = 0.0
    _chg_last_mono: float = 0.0
    _chg_last_persist: float = 0.0
    _learned_this_boot: bool = False
    _CHG_MIN_S = 45 * 60          # a drive long enough to fill the battery
    _REST_MIN_S = 4 * 3600        # surface charge gone; true resting voltage
    _REST_MAX_S = 7 * 86400       # older than this, self-discharge muddies it

    def _ocv_table(self):
        """The resting-voltage table, stretched to this battery's full/empty
        points. Lead-acid chemistry fixes them — a flooded battery rests at
        ~12.65 V full, an AGM/EFB nearer 12.85 V — so they are a setting, not
        something learned from the highest reading seen: the highest reading
        with the engine running is the alternator (13.5–14.5 V), not the
        battery, and the estimate is only ever taken at rest."""
        from Modules.Runtime import settings_cache
        from preferences import DATA_SETTINGS
        full = settings_cache.getfloat("Calibration", "battery_full_v", fallback=12.65, path=DATA_SETTINGS)
        empty = settings_cache.getfloat("Calibration", "battery_empty_v", fallback=11.85, path=DATA_SETTINGS)
        if not (10.5 <= empty < full <= 13.5):
            full, empty = 12.65, 11.85
        scale = (full - empty) / (12.65 - 11.85)
        return tuple((empty + (v0 - 11.85) * scale, s0) for v0, s0 in self._OCV_TABLE)

    def _ocv_to_soc(self, v: float) -> float:
        t = self._ocv_table()
        if v <= t[0][0]:
            return 0.0
        if v >= t[-1][0]:
            return 100.0
        for (v0, s0), (v1, s1) in zip(t, t[1:]):
            if v0 <= v <= v1:
                return s0 + (v - v0) / (v1 - v0) * (s1 - s0)
        return 0.0

    @pyqtProperty(float, notify=changed)
    def batteryFullVolts(self):
        from Modules.Runtime import settings_cache
        from preferences import DATA_SETTINGS
        return settings_cache.getfloat("Calibration", "battery_full_v", fallback=12.65, path=DATA_SETTINGS)

    @pyqtProperty(str, notify=changed)
    def batteryFullMode(self):
        """"auto" (learn the full point from a long drive + long rest) or
        "fixed" (a battery type picked by hand)."""
        from Modules.Runtime import settings_cache
        from preferences import DATA_SETTINGS
        return (settings_cache.get("Calibration", "battery_full_mode", fallback="auto", path=DATA_SETTINGS) or "auto").lower()

    @pyqtProperty(str, notify=changed)
    def batteryLearnedText(self):
        from Modules.Runtime import settings_cache
        from preferences import DATA_SETTINGS
        when = settings_cache.getfloat("Calibration", "battery_full_learned", fallback=0.0, path=DATA_SETTINGS)
        charge = settings_cache.getfloat("Calibration", "battery_charge_s", fallback=0.0, path=DATA_SETTINGS)
        if when > 0:
            return f"Learned {self.batteryFullVolts:.2f} V on {time.strftime('%d %b', time.localtime(when))}"
        if charge >= self._CHG_MIN_S:
            return f"{charge / 60:.0f} min charged — will learn at the next start after ≥ 4 h rest"
        return f"Not learned yet — needs a ≥ 45 min drive, then ≥ 4 h rest ({charge / 60:.0f} min so far)"

    @pyqtSlot(str)
    def setBatteryFullMode(self, mode):
        from Modules.Runtime import settings_cache
        from preferences import DATA_SETTINGS
        settings_cache.set_value("Calibration", "battery_full_mode",
                                 "auto" if str(mode).lower() == "auto" else "fixed", path=DATA_SETTINGS)
        self._learned_this_boot = False
        self.changed.emit()

    @pyqtSlot(float)
    def setBatteryFullVolts(self, v):
        """A battery type picked by hand: 12.65 V flooded, 12.85 V AGM/EFB.
        The empty point follows (0.8 V below, the table's span). Turns the
        learning off — a hand-picked value is a decision, not a guess."""
        from Modules.Runtime import settings_cache
        from preferences import DATA_SETTINGS
        v = max(12.3, min(13.2, float(v)))
        cfg = settings_cache.load_copy(DATA_SETTINGS)
        if not cfg.has_section("Calibration"):
            cfg.add_section("Calibration")
        cfg.set("Calibration", "battery_full_v", f"{v:.2f}")
        cfg.set("Calibration", "battery_empty_v", f"{v - 0.8:.2f}")
        cfg.set("Calibration", "battery_full_mode", "fixed")
        settings_cache.save(cfg, DATA_SETTINGS)
        self._soc_percent = -1.0   # re-estimate from the next settled window
        self._soc_samples = []
        self.changed.emit()

    @pyqtProperty(str, notify=changed)
    def batterySocDetail(self):
        """For Settings: what the estimate is built from."""
        if self._f("RPM") >= 300.0:
            return "Charging — estimate held until the engine is off"
        if self._soc_samples:
            v = sum(self._soc_samples) / len(self._soc_samples)
            return f"Resting {v:.2f} V (+ key-on offset) → 100 % at {self.batteryFullVolts:.2f} V"
        return f"No resting reading yet · 100 % at {self.batteryFullVolts:.2f} V"

    def _update_battery_soc(self) -> None:
        # A real CAN SoC beats any estimate.
        can_soc = self._entry("Battery SoC").get("value")
        if can_soc is not None:
            try:
                self._soc_percent = max(0.0, min(100.0, float(can_soc)))
                self._soc_charging = self._f("RPM") >= 300.0
                return
            except (TypeError, ValueError):
                pass
        v = self._battery_volts()
        running = self._f("RPM") >= 300.0
        self._soc_charging = running
        self._track_charge(running, v)
        if running or v <= 0.5:
            self._soc_samples = []
            return
        now = time.monotonic()
        if now - self._soc_last_ts < 0.5:
            return   # ECU voltage refreshes at ~0.6 s; don't count one reading five times
        self._soc_last_ts = now
        self._soc_samples.append(v)
        del self._soc_samples[:-self._SOC_WINDOW]
        if len(self._soc_samples) < self._SOC_WINDOW:
            return
        if max(self._soc_samples) - min(self._soc_samples) > self._SOC_STABLE_V:
            return   # a fan or the screen just changed the load; wait for it to settle
        from Modules.Runtime import settings_cache
        from preferences import DATA_SETTINGS
        offset = settings_cache.getfloat("Calibration", "battery_load_offset_v", fallback=0.15, path=DATA_SETTINGS)
        resting = sum(self._soc_samples) / len(self._soc_samples) + offset
        self._maybe_learn_full(resting)
        est = self._ocv_to_soc(resting)
        if self._soc_percent < 0:
            self._soc_percent = est
        else:
            self._soc_percent += 0.3 * (est - self._soc_percent)

    # ── learning this battery's full point ───────────────────────────────
    # Voltage alone cannot say how many amp-hours the battery holds — that
    # needs current integration (a shunt, or the car's own battery sensor on
    # CAN, which is what the `battery_soc` catalog slot is for). What it can
    # do is find where *this* battery rests when it is genuinely full: after
    # a drive long enough to charge it (≥ 45 min on the alternator) and a
    # rest long enough for the surface charge to bleed off (≥ 4 h — right
    # after key-off it reads 12.9–13.2 V and means nothing), the first
    # settled key-on reading is the 100 % point. That is the only number
    # worth learning; the highest reading ever seen is the alternator.
    def _track_charge(self, running: bool, v: float) -> None:
        now = time.monotonic()
        if running:
            if self._chg_last_mono and v >= 13.6:
                self._chg_accum_s += now - self._chg_last_mono
            self._chg_last_mono = now
            if now - self._chg_last_persist >= 60.0 and self._chg_accum_s > 0:
                self._chg_last_persist = now
                self._persist_charge()
        else:
            self._chg_last_mono = 0.0

    def _persist_charge(self) -> None:
        from Modules.Runtime import settings_cache
        from preferences import DATA_SETTINGS
        try:
            cfg = settings_cache.load_copy(DATA_SETTINGS)
            if not cfg.has_section("Calibration"):
                cfg.add_section("Calibration")
            cfg.set("Calibration", "battery_charge_s", f"{self._chg_accum_s:.0f}")
            cfg.set("Calibration", "battery_charge_last_seen", f"{time.time():.0f}")
            settings_cache.save(cfg, DATA_SETTINGS)
        except Exception:
            pass

    def _maybe_learn_full(self, resting_v: float) -> None:
        if self._learned_this_boot:
            return
        from Modules.Runtime import settings_cache
        from preferences import DATA_SETTINGS
        g = lambda k, d: settings_cache.getfloat("Calibration", k, fallback=d, path=DATA_SETTINGS)
        mode = (settings_cache.get("Calibration", "battery_full_mode", fallback="auto", path=DATA_SETTINGS) or "auto").lower()
        if mode != "auto":
            return
        charge_s = g("battery_charge_s", 0.0)
        last_seen = g("battery_charge_last_seen", 0.0)
        rest = time.time() - last_seen if last_seen > 0 else -1.0
        if charge_s < self._CHG_MIN_S or not (self._REST_MIN_S <= rest <= self._REST_MAX_S):
            return
        self._learned_this_boot = True
        v_full = max(12.5, min(13.0, resting_v))
        old = g("battery_full_v", 0.0)
        learned_before = g("battery_full_learned", 0.0) > 0
        new = old + 0.5 * (v_full - old) if (learned_before and old > 0) else v_full
        try:
            cfg = settings_cache.load_copy(DATA_SETTINGS)
            if not cfg.has_section("Calibration"):
                cfg.add_section("Calibration")
            cfg.set("Calibration", "battery_full_v", f"{new:.2f}")
            cfg.set("Calibration", "battery_empty_v", f"{new - 0.8:.2f}")
            cfg.set("Calibration", "battery_full_learned", f"{time.time():.0f}")
            cfg.set("Calibration", "battery_charge_s", "0")   # this drive has been used
            settings_cache.save(cfg, DATA_SETTINGS)
            print(f"[battery] learned full point {new:.2f} V from a settled {resting_v:.2f} V "
                  f"after {charge_s / 60:.0f} min on the alternator and {rest / 3600:.1f} h of rest",
                  flush=True)
        except Exception:
            pass
        self._soc_percent = -1.0   # re-estimate against the new table

    @pyqtProperty(float, notify=changed)
    def batterySocPercent(self):
        return round(self._soc_percent) if self._soc_percent >= 0 else -1.0


    @pyqtProperty(str, notify=changed)
    def batterySocText(self):
        """"78%" resting, "78% ⚡" while the alternator runs, "--%" unknown."""
        if self._soc_percent < 0:
            return "--%"
        txt = f"{int(round(self._soc_percent))}%"
        return txt + " ⚡" if self._soc_charging else txt

    @pyqtProperty(str, notify=changed)
    def batterySocColor(self):
        if self._soc_percent < 0:
            return COLOR_STATUS_NA
        if self._soc_percent >= 60:
            return COLOR_STATUS_GREEN
        if self._soc_percent >= 35:
            return COLOR_STATUS_ORANGE
        return COLOR_STATUS_RED

    @pyqtProperty(str, notify=changed)
    def batteryColor(self):
        state = self.batteryState
        if state == "charging":
            return COLOR_STATUS_GREEN
        if state == "resting":
            return COLOR_STATUS_GREEN
        if state == "low":
            return COLOR_STATUS_ORANGE
        if state == "draining":
            return COLOR_STATUS_RED
        return COLOR_STATUS_NA

    @pyqtProperty(str, notify=changed)
    def warmups(self):
        return self._s("Warmups since clear", "N/A")

    @pyqtProperty(str, notify=changed)
    def dtcCount(self):
        return self._s("DTC Count", "0")

    # ── trip data (from trips_store) ──────────────────────────────────────
    # Cached by _maybe_update_trips() on every coordinator tick — these two
    # used to call into trips_store on every read, which could hit a
    # synchronous disk write (see peek_trips_snapshot()'s docstring); that
    # was the driving screensaver's trip-readout flicker.
    @pyqtProperty(str, notify=changed)
    def totalKm(self):
        return self._display_total_km

    @pyqtProperty(str, notify=changed)
    def todayKm(self):
        return self._display_today_km

    @pyqtProperty(str, notify=changed)
    def todayDate(self):
        """ISO date trips_store rolled 'today' over at — was previously read
        (always empty) from a Data/settings.ini [Trips] section that nothing
        ever wrote; the real value has only ever lived in trips_store."""
        return self._display_today_date

    # ── dash lights ─────────────────────────────────────────────────────
    # Notify on dashLightsChanged (set-diff in _on_changed), NOT on the
    # main `changed` signal — otherwise the DashboardPage Repeater rebuilds
    # its Image children on every 80 ms tick, re-decoding textures.
    @pyqtProperty(int, notify=dashLightsChanged)
    def activeDashLights(self):
        return len(self._cached_dash_lights)

    @pyqtProperty(list, notify=dashLightsChanged)
    def activeDashLightKeys(self):
        return list(self._cached_dash_lights)

    # ── turn-off safety (TurnOff snapshot entry) ─────────────────────────
    @pyqtProperty(bool, notify=changed)
    def turnoffSafe(self):
        return bool(self._entry("TurnOff").get("safe", False))

    @pyqtProperty(str, notify=changed)
    def turnoffReason(self):
        return str(self._entry("TurnOff").get("reason", "") or "")


    # ── raw snapshot access ───────────────────────────────────────────────
    @pyqtSlot(str, result=str)
    def value(self, key):
        """Get any telemetry value by key name (nested format aware)."""
        entry = self._snap.get(key)
        if isinstance(entry, dict):
            v = entry.get("value")
            return str(v) if v is not None else ""
        return str(entry) if entry is not None else ""

    @pyqtSlot(str, result=str)
    def text(self, key):
        """Get formatted text for any telemetry key."""
        entry = self._snap.get(key)
        if isinstance(entry, dict):
            return str(entry.get("text", ""))
        return str(entry) if entry is not None else ""

    @pyqtSlot(str, result=str)
    def unit(self, key):
        """Get the unit string for any telemetry key."""
        entry = self._snap.get(key)
        if isinstance(entry, dict):
            return str(entry.get("unit", ""))
        return ""

