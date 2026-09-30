"""
LaLaDash QML Bootstrap
─────────────────────
Replaces the old QWidget-based main.py with a QML scene-graph UI.
All backend modules remain unchanged; only the rendering layer moves to QML.
"""
import sys
import os
import signal
import time
import threading
from typing import Dict, Optional

# Allow Ctrl-C to kill the app
signal.signal(signal.SIGINT, signal.SIG_DFL)

# Suppress libpng iCCP profile warnings (harmless, but very spammy on RPi)
os.environ.setdefault("QT_LOGGING_RULES", "qt.gui.imageio=false")

from PyQt6.QtWidgets import QApplication
from PyQt6.QtQml import QQmlApplicationEngine, QJSEngine
from PyQt6.QtCore import QUrl, QTimer, QObject, QEvent, qInstallMessageHandler, QtMsgType


# ── Qt log filter ─────────────────────────────────────────────────────────
# Qt's font engine emits a `qWarning()` for every text run whose script
# isn't covered by the active font. With Liberation Sans (the Linux Arial
# alias) and lyrics that contain non-Latin glyphs, that's one line per
# repaint of every offending character — hundreds of duplicates per second.
# The warning isn't actionable here (the fallback font engine handles it
# transparently) so we drop those lines and forward everything else.
_NOISY_QT_PATTERNS = (
    "OpenType support missing",
    "qt.qpa.fonts: Populating font family aliases",
)

def _qt_message_filter(mode, ctx, msg):
    text = msg or ""
    for needle in _NOISY_QT_PATTERNS:
        if needle in text:
            return
    stream = sys.stderr if mode != QtMsgType.QtDebugMsg else sys.stdout
    try:
        stream.write(text + "\n")
        stream.flush()
    except Exception:
        pass

qInstallMessageHandler(_qt_message_filter)

# ── Backend services (unchanged) ──────────────────────────────────────────
from preferences import DATA_SETTINGS, BASE_DIR, MUSIC_FOLDER, COLOR_STATUS_ORANGE, COLOR_STATUS_RED
from Modules.Runtime.telemetry_coordinator import TelemetryCoordinator
from Modules.Runtime import settings_cache
from Modules.Music.audio_daemon_mgr import (
    ensure_started as audio_daemon_ensure_started,
    stop as audio_daemon_stop,
)
from Modules.Obd.obd_connection import OBDManager

# ── Bridge classes ────────────────────────────────────────────────────────
from bridges.settings_bridge import SettingsBridge
from bridges.alerts_bridge import DASH_LIGHT_ALERTS
from bridges.telemetry_bridge import TelemetryBridge
from bridges.navigation_bridge import NavigationBridge
from bridges.alerts_bridge import AlertsBridge
from bridges.music_bridge import MusicBridge
from bridges.modding_bridge import ModdingBridge
from bridges.soundboard_bridge import SoundboardBridge
from bridges.obd_bridge import OBDBridge
from bridges.can_bridge import CanBridge
from bridges.airphone_bridge import AirPhoneBridge
from bridges.dashcam_bridge import DashcamBridge
from bridges.actioncam_bridge import ActionCamBridge
from bridges.triplog_bridge import TripLogBridge
from bridges.trip_bridge import TripBridge
from bridges.theme_bridge import ThemeBridge


class _MusicGUIShim:
    """Minimal shim satisfying MusicPlayer.gui references without full QWidget GUI."""

    def __init__(self):
        self.cached_duration_ms = 0
        self._last_progress_value = -1
        self.music_player = None

    def update_current_song(self):
        """No-op — MusicBridge polling handles UI updates."""
        pass

    def update_progress(self):
        """No-op — MusicBridge polling handles progress updates."""
        pass


def _init_music_player(music_bridge: MusicBridge):
    """Instantiate the real MusicPlayer and wire it into MusicBridge."""
    try:
        from music import MusicPlayer
        shim = _MusicGUIShim()
        player = MusicPlayer(shim)
        shim.music_player = player
        music_bridge.set_player(shim)
    except Exception as e:
        print(f"WARNING: MusicPlayer init failed: {e}")


def _init_mpris(music_bridge: MusicBridge):
    """Start the MPRIS D-Bus bridge, sourcing all metadata from MusicBridge."""
    try:
        from Modules.Music.mpris_bridge import MPRISBridge
    except ImportError:
        return None

    try:
        controller = music_bridge.create_mpris_controller()
        bridge = MPRISBridge(controller=controller, identity="LaLaDash")
        bridge.start()
        return bridge
    except Exception as e:
        print(f"WARNING: MPRIS bridge init failed: {e}")
        return None


def _boot_obd():
    """Attempt OBD connection on startup (non-blocking best-effort).

    quick_connect() performs a blocking ELM327 handshake (protocol detect +
    PID probe) that can take several seconds — run it on a daemon thread so
    the Qt main thread / UI never freezes during startup."""
    try:
        from Modules.Runtime import settings_cache
        auto = settings_cache.getboolean("OBD", "auto_connect", fallback=True, path=DATA_SETTINGS)
        if not auto:
            return
    except Exception:
        return

    def _worker():
        try:
            OBDManager.instance().quick_connect()
        except Exception:
            pass

    threading.Thread(target=_worker, name="BootOBD", daemon=True).start()


# Dash light keys → alert table lives with the alert catalog
# (bridges/alerts_bridge.py), so Settings can preview each alert exactly as
# it is raised. Tuple: (hub_key, alert_key, display_text, icon, priority)
_DASH_LIGHT_ALERTS = DASH_LIGHT_ALERTS

# Metrics not shown as tiles on DashboardPage — alert when orange or red.
# Tuple: (snapshot_key, alert_key, display_label, icon_in_GUI/Icons/Dash/)
_HIDDEN_METRIC_ALERTS = [
    ("Intake Air",     "intake_air_hot",  "Intake air temp",   "engine.png"),
    ("Manifold Press", "boost_high",      "Boost pressure",    "engine.png"),
    ("Absolute Load",  "engine_load",     "Engine load",       "engine.png"),
    ("Equiv. Ratio",   "equiv_ratio",     "Air/fuel mixture",  ""),
    ("Timing Adv",     "timing_adv",      "Timing advance",    ""),
    ("ELM Voltage",    "elm_voltage",     "OBD adapter volts", "battery.png"),
    ("Fuel Pressure",  "fuel_pressure",   "Fuel pressure",     "petrol.png"),
]
# Hidden-metric alerts that are meaningless with the engine off.
_ENGINE_ONLY_HIDDEN_ALERTS = frozenset({"elm_voltage"})


class _ActivityFilter(QObject):
    """Application-wide input watcher feeding NavigationBridge.notifyActivity().

    The shell used to reset its screensaver timers from a `TapHandler` at the
    root of the QML tree. A pointer handler there never sees a press that a
    child `MouseArea` takes an exclusive grab on — which is nearly every button,
    list row and dock item — so pressing things did not count as activity and
    the screensaver arrived mid-use, most visibly right after changing page.

    An event filter on the QApplication sees the press before anyone grabs it,
    so nothing can swallow it. The handler is a bool test and, at most, one
    signal every 200 ms; events are never consumed.
    """

    _WATCHED = (
        QEvent.Type.MouseButtonPress,
        QEvent.Type.TouchBegin,
        QEvent.Type.Wheel,
        QEvent.Type.KeyPress,
    )

    def __init__(self, nav, parent=None):
        super().__init__(parent)
        self._nav = nav

    def eventFilter(self, obj, event):
        try:
            if event.type() in self._WATCHED:
                self._nav.notifyActivity()
        except Exception:
            pass
        return False


class _EngineStatePoll:
    """
    Periodic state check mirroring MainApplication._poll_engine_state().
    Runs on a 350 ms QTimer in the main thread.
    Tracks: engine on/off, fuel bands, coolant alerts, speed, dash lights,
    and triggers page navigation on engine state changes.
    """

    def __init__(self, nav: NavigationBridge, telem: TelemetryBridge, alerts: AlertsBridge, settings=None, music=None):
        self.nav = nav
        self.telem = telem
        self.alerts = alerts
        self.settings = settings   # SettingsBridge — Pi health numbers, Wi-Fi SSID
        self.music = music         # MusicBridge — head-unit Bluetooth link state
        self._fuel_alert_band = "unknown"
        self._last_speed_kph = 0.0
        self._engine_on = False
        self._uphill_active = False
        self._freeze_active = False
        self._hidden_alert_colors: Dict[str, str] = {}
        self._hidden_alert_expiry: Dict[str, float] = {}  # alert_key → auto-clear timestamp
        self._obd_was_ok: bool = False                    # OBD disconnect edge detection
        self._engine_was_cold: bool = True                # cold→warm transition tracking
        # Debounce state for the newer, condition-sustained-for-N-seconds
        # alerts below — see _sustained(). None = condition not currently true.
        self._stall_risk_since: Optional[float] = None
        self._redline_since: Optional[float] = None
        self._battery_bad_since: Optional[float] = None
        self._idle_since: Optional[float] = None
        self._undervolt_since: Optional[float] = None
        self._undervolt_last_seen: float = 0.0
        self._mil_since: Optional[float] = None
        self._mil_last: bool = False
        # Trip page: recorder (fed below) and its bridge; set by main().
        self.trip_rec = None
        self.trip_bridge = None
        # Pi health: latched states (on above the warn line for hold_s, off
        # once below the release line) so a value hovering on a threshold
        # does not flap the alert list.
        self._pi_latched: Dict[str, bool] = {}
        self._pi_since: Dict[str, Optional[float]] = {}
        # Link notices: last seen state (None = not yet observed) and when
        # the current notice was raised (they clear themselves).
        self._bt_last: Optional[bool] = None
        self._bt_notice_until: float = 0.0
        self._wifi_last: Optional[str] = None
        self._wifi_notice_until: float = 0.0
        # Noob Mode coach (created on first use) and which of its alerts are up.
        self._noob = None
        self._noob_shown: Dict[str, bool] = {}

    @staticmethod
    def _val(snap, key, default=0.0):
        """Extract a numeric value from the nested telemetry snapshot."""
        try:
            entry = snap.get(key)
            if isinstance(entry, dict):
                v = entry.get("value", default)
                return float(v) if v is not None else float(default)
            return float(entry) if entry is not None else float(default)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _entry(snap, key):
        entry = snap.get(key)
        return entry if isinstance(entry, dict) else {}

    @staticmethod
    def _badge(entry, badge_id):
        badges = entry.get("badges", []) if isinstance(entry, dict) else []
        if not isinstance(badges, list):
            return None
        for badge in badges:
            if isinstance(badge, dict) and badge.get("id") == badge_id:
                return badge
        return None

    @staticmethod
    def _icon_name(icon):
        if not isinstance(icon, str):
            return ""
        icon = icon.strip()
        if not icon:
            return ""
        base = os.path.basename(icon)
        dash_icon = os.path.join(BASE_DIR, "GUI", "Icons", "Dash", base)
        return base if os.path.exists(dash_icon) else ""

    @staticmethod
    def _alerts_enabled(key, default=True):
        return settings_cache.getboolean("Alerts", key, fallback=default, path=DATA_SETTINGS)

    def _sustained(self, state_attr: str, condition: bool, hold_s: float, now: float) -> bool:
        """True once `condition` has held continuously for `hold_s` seconds.

        `state_attr` names an instance attribute holding the timestamp the
        condition first became true (None while it's false). Same hysteresis
        idea as everywhere else in this class (RPM/fuel/coolant bands) applied
        to a plain time window instead of a value band — a telemetry reading
        that is only momentarily out of range for one or two ticks should not
        raise an alert, only one that actually persists.
        """
        since = getattr(self, state_attr)
        if not condition:
            if since is not None:
                setattr(self, state_attr, None)
            return False
        if since is None:
            setattr(self, state_attr, now)
            return False
        return (now - since) >= hold_s

    def _refuel_pending(self) -> bool:
        try:
            from Modules.Settings import fuel_ledger
            return fuel_ledger.pending_refuel() is not None
        except Exception:
            return False

    def _trip_worth_recap(self) -> bool:
        t = self.trip_rec.trip if self.trip_rec is not None else None
        return bool(t) and float(t.get("km") or 0) >= 0.5

    # ── Noob Mode (Settings → Driving Mode) ──
    # Settings are read through settings_cache (cached, no disk I/O per tick);
    # with Noob Mode off this is one bool lookup and every coach alert stays
    # cleared.
    def _noob_tick(self, now, rpm, engine_on, uphill, snap):
        from Modules.Runtime.noob_coach import ALERTS, NoobCoach
        get = lambda k, d=False: settings_cache.getboolean("SoundFeatures", k, fallback=d, path=DATA_SETTINGS)
        cfg = {
            "enabled": get("noob_mode_enabled") and engine_on,
            "starting": get("noob_starting"),
            "rpm": get("noob_rpm"),
            "speeding": get("noob_speeding"),
            "uphill": get("noob_uphill"),
            "limit": settings_cache.getfloat("SoundFeatures", "noob_speed_limit", fallback=90.0, path=DATA_SETTINGS),
        }
        if not cfg["enabled"] and not any(self._noob_shown.values()):
            return
        if self._noob is None:
            try:
                from Modules.Obd.telemetry import GEAR_BANDS
            except Exception:
                GEAR_BANDS = {}
            self._noob = NoobCoach(GEAR_BANDS)
            self._noob_shown = {}
        states = self._noob.update(
            now, rpm=rpm, speed=self._val(snap, "Speed"), gear=int(self._val(snap, "Gear")),
            uphill=uphill, cfg=cfg,
        )
        for key, active in states.items():
            if active == self._noob_shown.get(key, False):
                continue
            self._noob_shown[key] = active
            text, icon, prio = ALERTS[key]
            if active:
                self.alerts.setAlert(key, text, icon, prio, True)
            else:
                self.alerts.setAlert(key, "", "", "info", False)

    def poll(self):
        coord = TelemetryCoordinator.instance()
        snap = coord.snapshot()
        obd_ok = coord.connected()
        sim_on = coord.sim_enabled()
        now = time.time()

        # ── OBD disconnect → navigate home ──
        # Not off the Trip page: switching the ignition off drops the link
        # right after the engine-off recap was put on screen.
        if self._obd_was_ok and not obd_ok and not sim_on and self.nav.currentPage != 9:
            self.nav.navigateTo(0)
        self._obd_was_ok = obd_ok or sim_on

        # ── RPM / engine state ──
        rpm = self._val(snap, "RPM")
        engine_on = rpm >= 5.0 if self._engine_on else rpm >= 50.0
        self._engine_on = engine_on
        if engine_on != self.nav.engineOn:
            self.nav.engineOn = engine_on
            # Auto-navigate: engine on → dashboard (page 1), engine off → home (page 0).
            # The Trip page (9) takes precedence when there is something to act
            # on: a refuel waiting for its price at start, a recap at stop.
            if engine_on:
                if self._refuel_pending():
                    self.nav.navigateTo(9)
                elif self.nav.currentPage == 0:
                    self.nav.navigateTo(1)
            else:
                if self._trip_worth_recap():
                    self.nav.navigateTo(9)
                elif self.nav.currentPage != 0:
                    self.nav.navigateTo(0)
            if self.trip_bridge is not None:
                self.trip_bridge.noteEngine(engine_on)

        # ── Speed (for screensaver mode) ──
        self._last_speed_kph = self._val(snap, "Speed")

        # ── Trips accumulation ──
        try:
            from Modules.Settings.trips_store import update_trips_from_telemetry_snapshot

            update_trips_from_telemetry_snapshot(
                snap if snap else None,
                allow_update=(obd_ok and (not sim_on)),
            )
        except Exception:
            pass

        # ── Trip recorder (Trip page): distance, fuel, cost, eco, refuel ──
        if self.trip_rec is not None:
            try:
                self.trip_rec.feed(snap, engine_on, obd_ok and not sim_on, now, mil=self._mil_last)
            except Exception as exc:
                print(f"[trip] recorder error: {exc}", flush=True)

        # ── Fuel ──
        fuel_pct = self._val(snap, "Fuel Level", -1)
        fuel_band = "unknown"
        if fuel_pct >= 0:
            if fuel_pct <= 15.0:
                fuel_band = "low"
            elif fuel_pct <= 35.0:
                fuel_band = "intermediate"
            else:
                fuel_band = "ok"
        if fuel_band != self._fuel_alert_band:
            self._fuel_alert_band = fuel_band
            if fuel_band == "low":
                self.alerts.setAlert("fuel_low", "Fuel level LOW", "petrol.png", "critical", True)
            elif fuel_band == "intermediate":
                self.alerts.setAlert("fuel_low", "Fuel level intermediate", "petrol.png", "warning", True)
            elif fuel_band == "ok":
                self.alerts.setAlert("fuel_low", "", "", "info", False)

        # ── Coolant alert ──
        alert_parts = []
        try:
            coolant = self._val(snap, "Coolant")
            if coolant > 105:
                alert_parts.append(f"Coolant {int(coolant)}°C")
                self.alerts.setAlert(
                    "coolant_high",
                    f"Coolant temp high: {int(coolant)}°C",
                    "coolant_temperature.png",
                    "critical",
                    True,
                )
            else:
                self.alerts.setAlert("coolant_high", "", "", "info", False)
        except (TypeError, ValueError):
            pass

        # ── Uphill assist + freeze/cold alerts from telemetry badges ──
        rpm_entry = self._entry(snap, "RPM")
        uphill_badge = self._badge(rpm_entry, "uphill_assist")
        uphill_pref = self._alerts_enabled("uphill_enabled", default=True)
        if isinstance(uphill_badge, dict) and "active" in uphill_badge:
            uphill_active = bool(uphill_badge.get("active"))
        else:
            uphill_tip = str((uphill_badge or {}).get("tooltip", "") if isinstance(uphill_badge, dict) else "")
            uphill_active = "(applying +" in uphill_tip.lower()
        uphill_detected = bool(uphill_active)
        uphill_active = bool(uphill_pref and uphill_active)
        self._noob_tick(now, rpm, engine_on, uphill_detected, snap)
        if uphill_active:
            icon = self._icon_name((uphill_badge or {}).get("icon", ""))
            text = str((uphill_badge or {}).get("tooltip", "") if isinstance(uphill_badge, dict) else "").strip()
            self.alerts.setAlert(
                "uphill_assist",
                text or "Uphill assist",
                icon,
                "info",
                True,
            )
        else:
            self.alerts.setAlert("uphill_assist", "", "", "info", False)
        if uphill_active != self._uphill_active:
            self._uphill_active = uphill_active

        freeze_pref = self._alerts_enabled("freeze_enabled", default=True)
        cold_badge = None
        for metric_name in ("RPM", "Coolant", "Oil Temp"):
            cold_badge = self._badge(self._entry(snap, metric_name), "engine_cold")
            if cold_badge is not None:
                break
        coolant_now = self._val(snap, "Coolant", -1.0)
        oil_now = self._val(snap, "Oil Temp", -1.0)
        freeze_by_temp = bool(
            engine_on
            and (
                (coolant_now > 0.0 and coolant_now < 75.0)
                or (oil_now > 0.0 and oil_now < 65.0)
            )
        )
        freeze_active = bool(freeze_pref and (cold_badge or freeze_by_temp))
        if freeze_active:
            icon = self._icon_name((cold_badge or {}).get("icon", ""))
            text = str((cold_badge or {}).get("tooltip", "") if isinstance(cold_badge, dict) else "").strip()
            self.alerts.setAlert(
                "cold_engine",
                text or "Engine cold - drive gently",
                icon or "cold.png",
                "warning",
                True,
            )
        else:
            self.alerts.setAlert("cold_engine", "", "", "info", False)
        if freeze_active != self._freeze_active:
            self._freeze_active = freeze_active

        # ── Engine warm transition alert ──
        # Fires once when cold overlay clears while engine is running.
        # Auto-clears after 30 s via _hidden_alert_expiry.
        warm_key = "engine_warm"
        if warm_key in self._hidden_alert_expiry and now >= self._hidden_alert_expiry[warm_key]:
            del self._hidden_alert_expiry[warm_key]
            self.alerts.setAlert(warm_key, "", "", "info", False)
        if engine_on and not freeze_active and self._engine_was_cold:
            self.alerts.setAlert(warm_key, "Engine at operating temperature", "engine.png", "info", True)
            self._hidden_alert_expiry[warm_key] = now + 30.0
        if not engine_on:
            self._engine_was_cold = True
            self.alerts.setAlert(warm_key, "", "", "info", False)
            self._hidden_alert_expiry.pop(warm_key, None)
        elif freeze_active:
            self._engine_was_cold = True
        else:
            self._engine_was_cold = False

        # ── Safe turn-off status + popup ──
        turnoff = self._entry(snap, "TurnOff")
        turnoff_safe = bool(turnoff.get("safe", False))
        turnoff_reason = str(turnoff.get("reason", "") or "").strip()
        turnoff_icon = self._icon_name(turnoff.get("icon", "")) or "turn_off.png"

        unsafe_text = "Not safe to turn off engine"
        if turnoff_reason:
            unsafe_text += f" - {turnoff_reason}"
        unsafe_active = (obd_ok or sim_on) and bool(turnoff) and not turnoff_safe
        self.alerts.setAlert(
            "turnoff_unsafe",
            unsafe_text if unsafe_active else "",
            turnoff_icon if unsafe_active else "",
            "warning",
            unsafe_active,
        )
        self.alerts.setAlert("turnoff_safe", "", "", "info", False)

        # ── Dash lights sync ──
        try:
            from Modules.Layout.dash_lights_hub import DashLightsHub
            dash_lights_pref = self._alerts_enabled("dash_lights_enabled", default=True)
            if dash_lights_pref:
                active_lights = set(DashLightsHub.instance().get_active())
                for hub_key, alert_key, text, icon, priority in _DASH_LIGHT_ALERTS:
                    if hub_key in active_lights:
                        self.alerts.setAlert(alert_key, text, icon, priority, True)
                    else:
                        self.alerts.setAlert(alert_key, "", "", "info", False)
            else:
                for hub_key, alert_key, text, icon, priority in _DASH_LIGHT_ALERTS:
                    self.alerts.setAlert(alert_key, "", "", "info", False)
        except Exception:
            pass

        # ── Hidden metric alerts (metrics with no dashboard tile) ──
        for snap_key, alert_key, label, icon in _HIDDEN_METRIC_ALERTS:
            # Auto-clear after 30 s if condition hasn't changed severity
            if alert_key in self._hidden_alert_expiry and now >= self._hidden_alert_expiry[alert_key]:
                del self._hidden_alert_expiry[alert_key]
                self.alerts.setAlert(alert_key, "", "", "info", False)
                # Keep color tracker intact so re-fire only happens on severity change

            entry   = self._entry(snap, snap_key)
            color   = entry.get("color", "")
            val_txt = str(entry.get("text", "")).strip()
            unit    = str(entry.get("unit", "")).strip()

            # Supply voltage only means something with the alternator
            # turning; parked with the ignition on, the rail hovers around
            # the band edge and every 12.2↔11.9 V wobble re-raised this.
            # The Home battery tile covers the engine-off case.
            if alert_key in _ENGINE_ONLY_HIDDEN_ALERTS and not engine_on:
                color = ""

            if color == self._hidden_alert_colors.get(alert_key):
                continue  # no change — skip redundant setAlert calls

            self._hidden_alert_colors[alert_key] = color

            if color == COLOR_STATUS_RED:
                text = f"{label} critical: {val_txt} {unit}".strip()
                self.alerts.setAlert(alert_key, text, icon, "critical", True)
                self._hidden_alert_expiry[alert_key] = now + 30.0
            elif color == COLOR_STATUS_ORANGE:
                text = f"{label} high: {val_txt} {unit}".strip()
                self.alerts.setAlert(alert_key, text, icon, "warning", True)
                self._hidden_alert_expiry[alert_key] = now + 30.0
            else:
                self.alerts.setAlert(alert_key, "", "", "info", False)
                self._hidden_alert_expiry.pop(alert_key, None)

        # ── Stall risk / engine lugging ──
        # Deliberately gear-independent: gear is only ever estimated from
        # speed/RPM ratio (see GearEstimator in telemetry.py) and isn't
        # trustworthy enough to gate a safety alert on. Heavy throttle with
        # RPM way too low for it — regardless of which gear that technically
        # is — is exactly the state that stalls the engine.
        throttle_val = self._val(snap, "Throttle Pos", self._val(snap, "Accel Pedal D", -1.0))
        stall_condition = bool(
            engine_on and self._last_speed_kph > 3.0
            and throttle_val >= 45.0 and 0 < rpm < 1300.0
        )
        if self._sustained("_stall_risk_since", stall_condition, 0.8, now):
            self.alerts.setAlert(
                "stall_risk", "Stall risk — throttle high, RPM too low for the gear",
                "engine.png", "warning", True,
            )
        else:
            self.alerts.setAlert("stall_risk", "", "", "info", False)

        # ── Sustained redline ──
        # Static text, deliberately: an early version embedded the live RPM
        # value, which changes every tick and defeats set_alert's no-op-refresh
        # guard — the exact "value-embedded text churns the model" trap
        # coolant_high already fell into once (see POST_GUI_CHECKS.md).
        redline_rpm = 5800.0
        try:
            from Modules.Obd.telemetry import GAUGE_BANDS
            redline_rpm = float(GAUGE_BANDS.get("RPM", (0, 0, 5800, 0))[2])
        except Exception:
            pass
        redline_condition = bool(engine_on and rpm >= redline_rpm)
        if self._sustained("_redline_since", redline_condition, 2.0, now):
            self.alerts.setAlert(
                "redline_sustained", "Engine held at redline",
                "engine.png", "critical", True,
            )
        else:
            self.alerts.setAlert("redline_sustained", "", "", "info", False)

        # ── Battery not charging while driving ──
        # Reuses TelemetryBridge's own charging/resting classification (built
        # for the Home page battery tile) rather than re-deriving the voltage
        # thresholds a second time.
        battery_condition = False
        try:
            battery_condition = bool(engine_on and self.telem.batteryState == "draining")
        except Exception:
            pass
        if self._sustained("_battery_bad_since", battery_condition, 4.0, now):
            self.alerts.setAlert(
                "battery_not_charging", "Battery not charging — check the alternator",
                "battery.png", "warning", True,
            )
        else:
            self.alerts.setAlert("battery_not_charging", "", "", "info", False)

        # ── Excessive idling ──
        idling_condition = bool(engine_on and self._last_speed_kph < 1.0)
        if self._sustained("_idle_since", idling_condition, 300.0, now):
            self.alerts.setAlert(
                "excessive_idling", "Engine idling a long time — consider turning it off",
                "turn_off.png", "info", True,
            )
        else:
            self.alerts.setAlert("excessive_idling", "", "", "info", False)

        # ── Check engine (MIL) from OBD ──
        # TelemetryHub polls mode-01 PID 01 every 30 s; "MIL" is 1.0/0.0 while
        # the ECU answers and absent otherwise. Static text: the code count
        # lives on the Home "Codes" tile, not in here (no-op-refresh guard).
        mil_on = False
        try:
            mil_on = self._val(snap, "MIL", 0.0) >= 1.0
        except Exception:
            pass
        self._mil_last = mil_on
        if self._sustained("_mil_since", mil_on, 1.0, now):
            self.alerts.setAlert(
                "check_engine", "Check engine light on — codes stored",
                "engine.png", "warning", True,
            )
        else:
            self.alerts.setAlert("check_engine", "", "", "info", False)

        # ── Pi under-voltage / throttling ──
        # get_throttled bit 0 = under-voltage now, bits 1-2 = clock capped /
        # throttled now. The 2 s sysinfo worker reads the sysfs node; this is
        # a dict lookup. Sustained 2 s so a single sample dip does not flap.
        flags = -1
        try:
            if self.settings is not None:
                flags = int(self.settings.pi_power_flags())
        except Exception:
            flags = -1
        undervolt_now = flags > 0 and bool(flags & 0x7)
        # The car's dips come and go every few seconds; without a release
        # hold the alert would flap in the list at the same rate.
        if undervolt_now:
            self._undervolt_last_seen = now
        undervolt = undervolt_now or (now - self._undervolt_last_seen) < 30.0
        if self._sustained("_undervolt_since", undervolt, 2.0, now):
            self.alerts.setAlert(
                "pi_undervoltage", "Pi power supply low — performance reduced",
                "rpi.png", "warning", True,
            )
        else:
            self.alerts.setAlert("pi_undervoltage", "", "", "info", False)

        self._poll_pi_health(flags, now)
        self._poll_links(now)

        if not alert_parts and freeze_active:
            alert_parts.append("Engine cold")
        if not alert_parts and uphill_active:
            alert_parts.append("Uphill assist")
        if not alert_parts and unsafe_active:
            alert_parts.append(unsafe_text)

        self.nav.alertText = " | ".join(alert_parts) if alert_parts else "No alerts"


    # ── Pi health ────────────────────────────────────────────────────────
    def _latched(self, key: str, on: bool, off: bool, hold_s: float, now: float) -> bool:
        """Set once `on` has held hold_s; cleared as soon as `off` is true."""
        state = self._pi_latched.get(key, False)
        if state:
            if off:
                self._pi_latched[key] = False
                self._pi_since[key] = None
                return False
            return True
        if not on:
            self._pi_since[key] = None
            return False
        since = self._pi_since.get(key)
        if since is None:
            self._pi_since[key] = now
            return False
        if now - since >= hold_s:
            self._pi_latched[key] = True
            return True
        return False

    def _poll_pi_health(self, flags: int, now: float) -> None:
        """Temperature, CPU, GPU pressure, RAM — all from the 2 s system-info
        worker's cache (dict lookups here), thresholds in [Alerts]."""
        if self.settings is None:
            return
        try:
            h = self.settings.pi_health()
        except Exception:
            return
        cfg = lambda k, d: settings_cache.getfloat("Alerts", k, fallback=d, path=DATA_SETTINGS)

        # Temperature: the SoC is one die, so this is the GPU's too. Pi 4
        # soft-throttles at 80 °C and hard-throttles at 85 °C; the firmware's
        # soft-limit bit is the authoritative "throttling now".
        temp = h.get("temp_c", -1.0)
        warn_c = cfg("pi_temp_warn_c", 75.0)
        crit_c = cfg("pi_temp_crit_c", 82.0)
        soft_limit = flags > 0 and bool(flags & 0x8)
        if temp >= 0:
            hot = self._latched("temp", temp >= warn_c, temp < warn_c - 3.0, 5.0, now)
            if soft_limit or temp >= crit_c:
                self.alerts.setAlert("pi_temp_high", "Pi overheating — thermal throttling", "rpi.png", "critical", True)
            elif hot:
                self.alerts.setAlert("pi_temp_high", "Pi running hot — check airflow", "rpi.png", "warning", True)
            else:
                self.alerts.setAlert("pi_temp_high", "", "", "info", False)

        # CPU: system-wide, all cores. Sustained saturation is what starves
        # the audio callback and the render thread.
        cpu = h.get("sys_cpu_pct", -1.0)
        if cpu >= 0:
            busy = self._latched("cpu", cpu >= cfg("pi_cpu_warn_pct", 85.0), cpu < cfg("pi_cpu_warn_pct", 85.0) - 15.0, 10.0, now)
            if busy:
                self.alerts.setAlert("pi_cpu_high", "Pi CPU maxed out — UI may stutter", "rpi.png", "warning", True)
            else:
                self.alerts.setAlert("pi_cpu_high", "", "", "info", False)

        # No GPU alert: the Pi has no GPU busy counter, and the v3d clock the
        # firmware drives to max whenever anything renders made it fire constantly.

        # RAM: MemAvailable, not "free" — the kernel's own reclaimable figure.
        ram = h.get("ram_avail_mb", -1.0)
        if ram >= 0:
            low = self._latched("ram", ram < cfg("pi_ram_warn_mb", 300.0), ram >= cfg("pi_ram_warn_mb", 300.0) * 1.5, 5.0, now)
            if low:
                self.alerts.setAlert("pi_ram_low", "Pi memory low", "rpi.png", "warning", True)
            else:
                self.alerts.setAlert("pi_ram_low", "", "", "info", False)

    # ── link notices ─────────────────────────────────────────────────────
    _LINK_NOTICE_S = 10.0

    def _poll_links(self, now: float) -> None:
        """Head-unit Bluetooth and Wi-Fi: a notice on each transition that
        clears itself after _LINK_NOTICE_S. The first observation at boot
        only announces a *connected* link — a "disconnected" alarm while the
        head unit is still booting would be noise."""
        # Bluetooth (via MusicBridge's auto-connect supervisor)
        bt = None
        bt_name = ""
        try:
            if self.music is not None and self.music._bt_auto is not None:
                bt = self.music._bt_auto.status == "connected"
                bt_name = self.music._bt_auto.connected_name or "head unit"
        except Exception:
            bt = None
        if bt is not None and bt != self._bt_last:
            if bt:
                self.alerts.setAlert("bt_link", f"Bluetooth connected — {bt_name}", "bt.png", "info", True)
                self._bt_notice_until = now + self._LINK_NOTICE_S
            elif self._bt_last is not None:
                self.alerts.setAlert("bt_link", f"Bluetooth lost — {bt_name}", "bt.png", "warning", True)
                self._bt_notice_until = now + self._LINK_NOTICE_S * 1.5
            self._bt_last = bt
        if self._bt_notice_until and now >= self._bt_notice_until:
            self._bt_notice_until = 0.0
            self.alerts.setAlert("bt_link", "", "", "info", False)

        # Wi-Fi (SSID from the system-info worker; either adapter)
        ssid = None
        try:
            if self.settings is not None:
                ssid = self.settings.wifi_ssid_cached()
        except Exception:
            ssid = None
        if ssid is not None and ssid != self._wifi_last:
            if ssid:
                self.alerts.setAlert("wifi_link", f"Wi-Fi connected — {ssid}", "wifi.png", "info", True)
                self._wifi_notice_until = now + self._LINK_NOTICE_S
            elif self._wifi_last:
                self.alerts.setAlert("wifi_link", f"Wi-Fi lost — {self._wifi_last}", "wifi.png", "info", True)
                self._wifi_notice_until = now + self._LINK_NOTICE_S
            self._wifi_last = ssid
        if self._wifi_notice_until and now >= self._wifi_notice_until:
            self._wifi_notice_until = 0.0
            self.alerts.setAlert("wifi_link", "", "", "info", False)


def main():
    os.chdir(BASE_DIR)

    app = QApplication(sys.argv)
    app.setApplicationName("LaLaDash")
    app.setOrganizationName("LaLaDash")

    # Start backend services
    audio_daemon_ensure_started()
    TelemetryCoordinator.instance().start()

    # Create bridge instances
    settings_bridge = SettingsBridge(app)
    theme_bridge = ThemeBridge(settings_bridge, app)
    telemetry_bridge = TelemetryBridge(app)
    navigation_bridge = NavigationBridge(app)
    alerts_bridge = AlertsBridge(app)
    music_bridge = MusicBridge(app)
    modding_bridge = ModdingBridge(app)
    soundboard_bridge = SoundboardBridge(app)
    obd_bridge = OBDBridge(app)
    can_bridge = CanBridge(app)
    can_bridge.set_obd_bridge(obd_bridge)
    airphone_bridge = AirPhoneBridge(app)
    # The 70mai over its Wi-Fi hotspot. Inert until [Dashcam] enabled; see the
    # bridge's docstring for what runs when.
    dashcam_bridge = DashcamBridge(app)
    dashcam_bridge.set_navigation_bridge(navigation_bridge)
    dashcam_bridge.set_alerts_bridge(alerts_bridge)   # 70mai connect/disconnect alerts
    # GoPro / Insta360 over USB or Wi-Fi — same rules, [ActionCam].
    actioncam_bridge = ActionCamBridge(app)
    actioncam_bridge.set_navigation_bridge(navigation_bridge)
    actioncam_bridge.set_alerts_bridge(alerts_bridge)   # GoPro/Insta360 connect/disconnect alerts
    # Trip logging (Settings → Debugger). Inert until Start is pressed.
    triplog_bridge = TripLogBridge(app)
    # Trip page: fuel cost, refuel price prompt, eco recap (fed by the engine poll).
    from Modules.Runtime.trip_recorder import TripRecorder
    trip_recorder = TripRecorder()
    trip_bridge = TripBridge(trip_recorder, app)

    # Sees every press before a QML MouseArea can grab it; see _ActivityFilter.
    app._activity_filter = _ActivityFilter(navigation_bridge, app)
    app.installEventFilter(app._activity_filter)

    music_bridge.setModdingBridge(modding_bridge)
    # Lets an incoming AirPlay stream duck music by the [AirPlay] duck_percent
    # amount, and hand the level back when the phone disconnects.
    airphone_bridge.set_music_bridge(music_bridge)
    # An alert with its "Sound" toggle on ducks both of these the same way,
    # for as long as its chime is actually playing.
    alerts_bridge.set_music_bridge(music_bridge)
    alerts_bridge.set_modding_bridge(modding_bridge)
    # Steering-wheel Mode/OK and the two call buttons arrive as Bluetooth input
    # events, not over MPRIS, so they need navigation to act on.
    music_bridge.setNavigationBridge(navigation_bridge)

    # Start the MPRIS bridge and register it BEFORE _init_music_player (which
    # launches the mpris-proxy supervisor): the supervisor waits for the
    # bridge to own the bus name before starting the proxy, so the car head
    # unit never attaches to an unpublished player.
    _mpris_bridge = _init_mpris(music_bridge)
    music_bridge.attach_mpris_bridge(_mpris_bridge)

    # Connect MusicBridge to actual MusicPlayer and load music library
    _init_music_player(music_bridge)
    if os.path.isdir(MUSIC_FOLDER):
        music_bridge.browseFolder(MUSIC_FOLDER)

    # QML engine
    engine = QQmlApplicationEngine()
    app._mpris = _mpris_bridge   # keep alive; stop on exit
    app._bridges = {
        "settings": settings_bridge,
        "theme": theme_bridge,
        "telemetry": telemetry_bridge,
        "navigation": navigation_bridge,
        "alerts": alerts_bridge,
        "music": music_bridge,
        "modding": modding_bridge,
        "soundboard": soundboard_bridge,
        "obd": obd_bridge,
        "can": can_bridge,
        "airphone": airphone_bridge,
        "dashcam": dashcam_bridge,
        "actioncam": actioncam_bridge,
        "triplog": triplog_bridge,
        "trip": trip_bridge,
    }
    engine._bridge_refs = app._bridges
    # Live frames for the camera pages: Image { source: "image://dashcam/" + rev }.
    app._dashcam_provider = dashcam_bridge.image_provider()
    engine.addImageProvider("dashcam", app._dashcam_provider)
    app._actioncam_provider = actioncam_bridge.image_provider()
    engine.addImageProvider("actioncam", app._actioncam_provider)

    for bridge in app._bridges.values():
        QJSEngine.setObjectOwnership(bridge, QJSEngine.ObjectOwnership.CppOwnership)

    # Expose bridges as context properties (available as globals in QML)
    ctx = engine.rootContext()
    ctx.setContextProperty("Theme", theme_bridge)
    ctx.setContextProperty("Settings", settings_bridge)
    ctx.setContextProperty("Telemetry", telemetry_bridge)
    ctx.setContextProperty("Nav", navigation_bridge)
    ctx.setContextProperty("Alerts", alerts_bridge)
    ctx.setContextProperty("Music", music_bridge)
    ctx.setContextProperty("Modding", modding_bridge)
    ctx.setContextProperty("Soundboard", soundboard_bridge)
    ctx.setContextProperty("OBD", obd_bridge)
    ctx.setContextProperty("Can", can_bridge)
    ctx.setContextProperty("AirPhone", airphone_bridge)
    ctx.setContextProperty("Dashcam", dashcam_bridge)
    ctx.setContextProperty("ActionCam", actioncam_bridge)
    ctx.setContextProperty("TripLog", triplog_bridge)
    ctx.setContextProperty("Trip", trip_bridge)

    # Expose paths
    ctx.setContextProperty("basePath", BASE_DIR)
    ctx.setContextProperty("iconsPath", os.path.join(BASE_DIR, "GUI", "Icons"))
    ctx.setContextProperty("fontsPath", os.path.join(BASE_DIR, "Fonts"))

    # Active visual style — main.qml loads exactly this one shell, so nothing
    # belonging to any other style is ever parsed or instantiated.
    from Modules.Runtime import styles as _styles
    _style_name, _ = _styles.resolve(settings_bridge.styleName)
    ctx.setContextProperty(
        "stylePath",
        QUrl.fromLocalFile(os.path.join(BASE_DIR, "Styles", _style_name) + os.sep).toString(),
    )
    ctx.setContextProperty("styleName", _style_name)

    # Add QML import paths (root dir contains Theme singleton + qmldir)
    engine.addImportPath(BASE_DIR)

    # Load root QML
    qml_path = os.path.join(BASE_DIR, "main.qml")
    engine.load(QUrl.fromLocalFile(qml_path))

    # Apply resolution override after window is created (short delay so window exists)
    QTimer.singleShot(200, settings_bridge.applyStartupResolution)

    # rpiplay stays off until asked for. `enabled` only means the feature is
    # available; it used to also mean "run the receiver from boot", which left
    # the process up permanently for something wanted occasionally. Opt in with
    # [AirPlay] autostart.
    if airphone_bridge.enabled and airphone_bridge.autostart:
        QTimer.singleShot(500, airphone_bridge.start)

    # The camera powers with ignition: engine on / OBD connected is when it
    # can be there, and the only time the bridge watches for its Wi-Fi.
    def _dashcam_car_state():
        awake = (navigation_bridge.engineOn, obd_bridge.connected)
        dashcam_bridge.noteCarState(*awake)
        actioncam_bridge.noteCarState(*awake)
    navigation_bridge.engineStateChanged.connect(_dashcam_car_state)
    obd_bridge.connectionChanged.connect(_dashcam_car_state)
    QTimer.singleShot(800, dashcam_bridge.boot)
    QTimer.singleShot(900, actioncam_bridge.boot)
    # Settings → Services master switches: a flip stops the process behind
    # the extra (rpiplay, the steering-key listener, a camera link/stream)
    # and pulls the user off its page; on lets each module's own flag act.
    def _services_flipped():
        for _cam in (dashcam_bridge, actioncam_bridge):
            _cam.refreshService()
        if not airphone_bridge.enabled:
            try:
                airphone_bridge.stop()
            except Exception:
                pass
            if navigation_bridge.currentPage == 3:
                navigation_bridge.navigateTo(0)
        airphone_bridge.changed.emit()
        music_bridge.refreshMediaKeysService()
    settings_bridge.servicesChanged.connect(_services_flipped)

    # Reverse gear (CAN) → the action cam's rear-view overlay.
    telemetry_bridge.reverseGearChanged.connect(
        lambda: actioncam_bridge.noteReverse(telemetry_bridge.reverseGear))

    if not engine.rootObjects():
        print("ERROR: Failed to load QML. Check main.qml")
        try:
            modding_bridge.cleanup()
        except Exception:
            pass
        try:
            music_bridge.stop_polling()
        except Exception:
            pass
        try:
            TelemetryCoordinator.instance().stop()
        except Exception:
            pass
        try:
            audio_daemon_stop()
        except Exception:
            pass
        sys.exit(1)

    # rpiplay draws its mirror into its own X11 window; the AirPlay page
    # reparents that window into this one so it appears inside the page
    # instead of floating over the dashboard. Harmless when rpiplay is not
    # installed or the platform is not X11.
    try:
        airphone_bridge.set_host_window(engine.rootObjects()[0])
    except Exception as exc:
        print(f"[AirPlay] could not adopt host window: {exc}", flush=True)

    # Dev-only introspection channel (screenshots, page nav, frame stats).
    # Never imported unless explicitly enabled, so a normal run costs nothing.
    app._debug_probe = None
    if os.environ.get("LALADASH_DEBUG") == "1":
        try:
            from Modules.Runtime.debug_probe import DebugProbe, install_signal_dump
            install_signal_dump()
            app._debug_probe = DebugProbe(engine.rootObjects()[0], app._bridges)
        except Exception as exc:
            print(f"[debug_probe] failed to start: {exc}", flush=True)

    # Boot OBD (deferred)
    QTimer.singleShot(0, _boot_obd)

    # Engine state polling (mirrors old 350ms timer)
    engine_poll = _EngineStatePoll(navigation_bridge, telemetry_bridge, alerts_bridge, settings_bridge, music_bridge)
    engine_poll.trip_rec = trip_recorder
    engine_poll.trip_bridge = trip_bridge

    def _fuel_watch(on):
        try:
            from Modules.Obd.telemetry import TelemetryHub
            TelemetryHub.instance().set_fuel_watch(on)
        except Exception:
            pass

    def _on_refuel(p):
        try:
            from Modules.Obd.telemetry import TelemetryHub
            TelemetryHub.instance().snap_fuel(float(p.get("pct_after")))
        except Exception:
            pass
        trip_bridge.noteRefuel()
        # Detected while already rolling: the dock's Trip pill lights instead.
        if engine_poll._last_speed_kph < 5.0:
            navigation_bridge.navigateTo(9)

    trip_recorder.fuel_watch_changed = _fuel_watch
    trip_recorder.on_refuel = _on_refuel

    poll_timer = QTimer()
    poll_timer.setInterval(350)
    poll_timer.timeout.connect(engine_poll.poll)
    poll_timer.start()

    # The Dashboard's own tiles (timing, lambda, pedal, intake…) poll faster
    # while that page is showing. DashboardPage.qml has no lifecycle hook, so
    # gate it here from the page index (1 = Dashboard).
    def _on_page_for_telemetry():
        try:
            from Modules.Obd.telemetry import TelemetryHub
            TelemetryHub.instance().set_dashboard_visible(int(navigation_bridge.currentPage) == 1)
        except Exception:
            pass
    navigation_bridge.currentPageChanged.connect(_on_page_for_telemetry)
    _on_page_for_telemetry()

    # Music progress polling (start when music page is active)
    music_bridge.start_polling()
    # Buttons the head unit sends outside MPRIS. Deferred so a slow /dev/input
    # scan cannot sit in front of the first painted frame.
    QTimer.singleShot(1500, music_bridge.start_media_keys)
    # Keep the head unit's A2DP link up: the Pi and the R-Link boot together
    # on ignition and whichever side connects first finds the other not up
    # yet, with no retry on either side (Modules/Runtime/bt_autoconnect.py).
    QTimer.singleShot(2500, music_bridge.start_bt_autoconnect)

    # Cleanup on exit
    def _cleanup():
        if getattr(app, "_debug_probe", None) is not None:
            try:
                app._debug_probe.stop()
            except Exception:
                pass
        poll_timer.stop()
        try:
            trip_recorder.flush()
        except Exception:
            pass
        for _cam in (dashcam_bridge, actioncam_bridge):
            try:
                _cam.shutdown()
            except Exception:
                pass
        try:
            triplog_bridge.shutdown()
        except Exception:
            pass
        modding_bridge.cleanup()
        music_bridge.stop_polling()
        try:
            if app._mpris is not None:
                app._mpris.stop()
        except Exception:
            pass
        try:
            music_bridge.stop_mpris_proxy()
        except Exception:
            pass
        try:
            if music_bridge._player:
                music_bridge._player.stop()
        except Exception:
            pass
        try:
            settings_bridge._sysinfo.stop()
        except Exception:
            pass
        try:
            settings_bridge._wifi_watcher.stop()
        except Exception:
            pass
        TelemetryCoordinator.instance().stop()
        # Hand the adapter back at 115200: the STN session has it at 2 Mbaud
        # and, powered from OBD pin 16, it keeps that across our restarts.
        try:
            OBDManager.instance().disconnect()
        except Exception:
            pass
        audio_daemon_stop()

    app.aboutToQuit.connect(_cleanup)

    # `systemctl restart` (and a shutdown) deliver SIGTERM. Python's default
    # is to die on the spot, skipping _cleanup — which is how the OBD adapter
    # got left at the STN session's 2 Mbaud with its monitor streaming,
    # unreachable at 115200 until something reset it. Turn the signal into
    # an orderly quit instead; the handler runs at the next Python wake-up
    # (the 350 ms engine poll at the latest).
    signal.signal(signal.SIGTERM, lambda *_: QTimer.singleShot(0, app.quit))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
