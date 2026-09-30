from __future__ import annotations

import os
import time

from PyQt6.QtCore import (
    QObject, QAbstractListModel, QModelIndex, Qt, QTimer,
    pyqtProperty, pyqtSignal, pyqtSlot,
)

from Modules.Runtime import settings_cache
from preferences import DATA_SETTINGS


def _triplog(channel, event, **fields):
    try:
        from Modules.Runtime.trip_logger import log as _tl
        _tl(channel, event, **fields)
    except Exception:
        pass

# Seconds an alert lives without being refreshed, by priority
_TTL_BY_PRIORITY: dict[str, float] = {
    "critical": 120.0,
    "warning":   60.0,
    "warn":      60.0,
    "info":      30.0,
}
_TTL_DEFAULT = 60.0

# Every alert key this app can raise, with a human label and a rough category
# for the Settings catalog below. This table only feeds the Tracked/Sound
# configuration UI — it does not decide when an alert fires; that logic lives
# in main.py's _EngineStatePoll.poll() (_DASH_LIGHT_ALERTS / _HIDDEN_METRIC_
# ALERTS there, plus the individually-coded conditions). Keep the *keys* in
# both files in sync by hand — a key with no catalog entry still works, it
# just won't show up here to be muted or given a sound.
ALERT_CATALOG: list[tuple[str, str, str]] = [
    # ── dash lights ──
    ("dl_oil",           "Oil pressure warning",         "Dash Light"),
    ("dl_airbag",        "Airbag fault",                 "Dash Light"),
    ("dl_stop",          "STOP — check vehicle",    "Dash Light"),
    ("dl_steering",      "Steering fault",                "Dash Light"),
    ("dl_efl",           "EFL fault indicator",           "Dash Light"),
    ("dl_abs",           "ABS fault",                     "Dash Light"),
    ("dl_engine",        "Engine warning",                "Dash Light"),
    ("dl_battery",       "Battery warning",               "Dash Light"),
    ("dl_coolant_temp",  "Coolant temperature warning",   "Dash Light"),
    ("dl_petrol",        "Low fuel warning",              "Dash Light"),
    ("dl_doors",         "Door open",                     "Dash Light"),
    ("dl_seatbelt",      "Seatbelt warning",              "Dash Light"),
    ("dl_tyres",         "Tyre warning",                  "Dash Light"),
    ("dl_high_beam",     "High beam on",                  "Dash Light"),
    ("dl_low_beam",      "Low beam on",                   "Dash Light"),
    ("dl_position",      "Position lights on",            "Dash Light"),
    ("dl_fog_front",     "Front fog lights on",           "Dash Light"),
    ("dl_fog_rear",      "Rear fog lights on",            "Dash Light"),
    ("dl_speed_limiter", "Speed limiter active",          "Dash Light"),
    # ── sensors with no dashboard tile of their own ──
    ("intake_air_hot", "Intake air temp high",          "Sensor"),
    ("boost_high",     "Boost pressure high",            "Sensor"),
    ("engine_load",    "Engine load high",               "Sensor"),
    ("equiv_ratio",    "Air/fuel mixture off",           "Sensor"),
    ("timing_adv",     "Timing advance abnormal",        "Sensor"),
    ("elm_voltage",    "OBD adapter voltage abnormal",   "Sensor"),
    ("fuel_pressure",  "Fuel pressure abnormal",         "Sensor"),
    # ── individually-coded ──
    ("fuel_low",       "Fuel level low",                  "Fuel"),
    ("coolant_high",   "Coolant overheating",             "Engine"),
    ("uphill_assist",  "Uphill assist active",            "Driving"),
    ("cold_engine",    "Engine cold — drive gently", "Engine"),
    ("engine_warm",    "Engine at operating temperature", "Engine"),
    ("turnoff_unsafe", "Not safe to turn off yet",        "Safety"),
    # ── added for configurable sounds ──
    ("stall_risk",            "Stall risk (throttle vs. RPM)",         "Driving"),
    ("redline_sustained",     "Engine held at redline",                "Engine"),
    ("battery_not_charging",  "Battery not charging while driving",    "Electrical"),
    ("excessive_idling",      "Excessive idling",                      "Driving"),
    # ── from the car itself (2026-09-15) ──
    # MIL via OBD mode-01 PID 01: the Pi in the car has no camera, so the
    # camera dash-light detector — and every dl_* alert — can never fire
    # there. This is the one check-engine signal the car can actually give.
    ("check_engine",          "Check engine light (OBD)",              "Engine"),
    # ── the Pi itself ──
    ("pi_undervoltage",       "Pi power supply low — throttling",      "Pi"),
    ("pi_temp_high",          "Pi running hot / thermal throttling",   "Pi"),
    ("pi_cpu_high",           "Pi CPU maxed out",                      "Pi"),
    ("pi_ram_low",            "Pi memory low",                         "Pi"),
    # ── links ──
    ("bt_link",               "Head unit Bluetooth connected / lost",  "Connectivity"),
    ("wifi_link",             "Wi-Fi joined / lost",                   "Connectivity"),
    # ── cameras (connect / disconnect) ──
    ("dashcam_link",          "70mai dashcam connected / lost",        "Connectivity"),
    ("gopro_link",            "GoPro connected / lost",                "Connectivity"),
    ("insta360_link",         "Insta360 connected / lost",             "Connectivity"),
    # ── Noob Mode coach (Settings → Driving Mode), Modules/Runtime/noob_coach.py ──
    ("noob_low_revs",         "Coach: low revs pulling away",          "Coach"),
    ("noob_stalled",          "Coach: engine stalled",                 "Coach"),
    ("noob_shift_up",         "Coach: shift up",                       "Coach"),
    ("noob_shift_down",       "Coach: shift down",                     "Coach"),
    ("noob_speeding",         "Coach: over your speed limit",          "Coach"),
    ("noob_uphill",           "Coach: uphill, keep the revs up",       "Coach"),
]

# Dash light keys → alert table (used by main._EngineStatePoll to raise
# them). Tuple: (hub_key, alert_key, display_text, icon_in_GUI/Icons/Dash/, priority)
DASH_LIGHT_ALERTS = [
    ("oil",                 "dl_oil",           "Oil pressure warning",        "oil.png",                 "critical"),
    ("airbag",              "dl_airbag",        "Airbag fault",                "airbag.png",              "critical"),
    ("stop",                "dl_stop",          "STOP — check vehicle",        "stop.png",                "critical"),
    ("steering_fault",      "dl_steering",      "Steering fault",              "steering_fault.png",      "critical"),
    ("efl",                 "dl_efl",           "EFL fault indicator",         "efl.png",                 "critical"),
    ("abs",                 "dl_abs",           "ABS fault",                   "abs.png",                 "warning"),
    ("engine",              "dl_engine",        "Engine warning",              "engine.png",              "warning"),
    ("battery",             "dl_battery",       "Battery warning",             "battery.png",             "warning"),
    ("coolant_temperature", "dl_coolant_temp",  "Coolant temperature warning", "coolant_temperature.png", "warning"),
    ("petrol",              "dl_petrol",        "Low fuel warning",            "petrol.png",              "warning"),
    ("doors_open",          "dl_doors",         "Door open",                   "doors_open.png",          "warning"),
    ("seatbelt",            "dl_seatbelt",      "Seatbelt warning",            "seatbelt.png",            "warning"),
    ("tyres",               "dl_tyres",         "Tyre warning",                "tyres.png",               "warning"),
    ("high_beam",           "dl_high_beam",     "High beam on",                "high_beam.png",           "info"),
    ("low_beam",            "dl_low_beam",      "Low beam on",                 "low_beam.png",            "info"),
    ("position_lights",     "dl_position",      "Position lights on",          "position_lights.png",     "info"),
    ("fog_lights_front",    "dl_fog_front",     "Front fog lights on",         "fog_lights_front.png",    "info"),
    ("fog_lights_rear",     "dl_fog_rear",      "Rear fog lights on",          "fog_lights_rear.png",     "info"),
    ("speed_limiter",       "dl_speed_limiter", "Speed limiter active",        "speed_limiter.png",       "info"),
]

# What each alert looks like when it fires — icon file (GUI/Icons/Dash/) and
# severity — so Settings → Alerts can show the real thing instead of a
# label. Dash lights come from DASH_LIGHT_ALERTS; the rest mirror the
# setAlert() calls in main.py (a hidden-metric alert previews as a warning,
# its live severity follows the reading).
ALERT_PREVIEW: dict[str, tuple[str, str]] = {
    alert_key: (icon, priority) for _hub, alert_key, _text, icon, priority in DASH_LIGHT_ALERTS
}
ALERT_PREVIEW.update({
    "intake_air_hot":       ("engine.png",              "warning"),
    "boost_high":           ("engine.png",              "warning"),
    "engine_load":          ("engine.png",              "warning"),
    "equiv_ratio":          ("",                        "warning"),
    "timing_adv":           ("",                        "warning"),
    "elm_voltage":          ("battery.png",             "warning"),
    "fuel_pressure":        ("petrol.png",              "warning"),
    "fuel_low":             ("petrol.png",              "warning"),
    "coolant_high":         ("coolant_temperature.png", "critical"),
    "uphill_assist":        ("",                        "info"),
    "cold_engine":          ("cold.png",                "warning"),
    "engine_warm":          ("engine.png",              "info"),
    "turnoff_unsafe":       ("turn_off.png",            "warning"),
    "stall_risk":           ("engine.png",              "warning"),
    "redline_sustained":    ("engine.png",              "critical"),
    "battery_not_charging": ("battery.png",             "warning"),
    "excessive_idling":     ("turn_off.png",            "info"),
    "check_engine":         ("engine.png",              "warning"),
    "pi_undervoltage":      ("rpi.png",                 "warning"),
    "pi_temp_high":         ("rpi.png",                 "warning"),
    "pi_cpu_high":          ("rpi.png",                 "warning"),
    "pi_ram_low":           ("rpi.png",                 "warning"),
    "bt_link":              ("bt.png",                  "info"),
    "wifi_link":            ("wifi.png",                "info"),
    "dashcam_link":         ("70mai.png",               "info"),
    "gopro_link":           ("gopro.png",               "info"),
    "insta360_link":        ("insta360.png",            "info"),
})
# The coach's alerts preview exactly as they are raised.
from Modules.Runtime.noob_coach import ALERTS as _NOOB_ALERTS
ALERT_PREVIEW.update({k: (icon, prio) for k, (_t, icon, prio) in _NOOB_ALERTS.items()})

_ALERT_SOUNDS_SECTION = "AlertSounds"


def _alert_config(key: str) -> dict:
    """Per-key {tracked, sound_enabled, sound_name, sound_path}.

    tracked defaults True (no behavior change for anyone who hasn't touched
    this) and sound_enabled defaults False (no surprise alarms on upgrade) —
    the Steering-Keys bindings pattern (bridges/music_bridge.py) for a
    settings_cache-backed per-item override, applied here per alert key.
    """
    return {
        "tracked": settings_cache.getboolean(
            _ALERT_SOUNDS_SECTION, f"{key}_tracked", fallback=True, path=DATA_SETTINGS
        ),
        "sound_enabled": settings_cache.getboolean(
            _ALERT_SOUNDS_SECTION, f"{key}_sound_enabled", fallback=False, path=DATA_SETTINGS
        ),
        "sound_name": settings_cache.get(
            _ALERT_SOUNDS_SECTION, f"{key}_sound_name", fallback="", path=DATA_SETTINGS
        ) or "",
        "sound_path": settings_cache.get(
            _ALERT_SOUNDS_SECTION, f"{key}_sound_path", fallback="", path=DATA_SETTINGS
        ) or "",
    }


class AlertItem:
    __slots__ = ("key", "text", "icon", "priority", "until")

    def __init__(self, key="", text="", icon="", priority="info", until=0.0):
        self.key = key
        self.text = text
        self.icon = icon
        self.priority = priority  # "info", "warning", "critical"
        self.until = until        # epoch seconds; 0.0 = never expires


class AlertsModel(QAbstractListModel):
    """QML-compatible list model for dashboard alerts."""

    KeyRole = Qt.ItemDataRole.UserRole + 1
    TextRole = Qt.ItemDataRole.UserRole + 2
    IconRole = Qt.ItemDataRole.UserRole + 3
    PriorityRole = Qt.ItemDataRole.UserRole + 4

    countChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[AlertItem] = []

    def roleNames(self):
        return {
            self.KeyRole: b"key",
            self.TextRole: b"text",
            self.IconRole: b"icon",
            self.PriorityRole: b"priority",
        }

    def rowCount(self, parent=QModelIndex()):
        return len(self._items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._items):
            return None
        item = self._items[index.row()]
        if role == self.KeyRole:
            return item.key
        if role == self.TextRole:
            return item.text
        if role == self.IconRole:
            return item.icon
        if role == self.PriorityRole:
            return item.priority
        return None

    @pyqtProperty(int, notify=countChanged)
    def count(self):
        return len(self._items)

    def is_active(self, key: str) -> bool:
        """Whether `key` is currently a live item — the edge AlertsBridge
        uses to fire a chime only on the transition into being active, never
        on every re-assertion of an already-active alert."""
        return any(item.key == key for item in self._items)

    def cleanup_expired(self):
        """Remove alerts whose TTL has elapsed. Called by the bridge timer."""
        now = time.time()
        expired = [i for i, item in enumerate(self._items)
                   if item.until > 0 and item.until < now]
        if not expired:
            return
        prev = len(self._items)
        self.beginResetModel()
        for i in reversed(expired):
            self._items.pop(i)
        self.endResetModel()
        if len(self._items) != prev:
            self.countChanged.emit()

    def set_alert(self, key, text, icon="", priority="info", active=True):
        ttl = _TTL_BY_PRIORITY.get(str(priority), _TTL_DEFAULT)
        until = time.time() + ttl

        # Trip log: only the raised / cleared edges, never the per-tick refresh.
        _was_active = any(it.key == key for it in self._items)
        if active and text and not _was_active:
            _triplog("alerts", "raised", key=key, priority=str(priority), text=str(text)[:60])
        elif not active and _was_active:
            _triplog("alerts", "cleared", key=key)

        previous_count = len(self._items)
        for i, item in enumerate(self._items):
            if item.key == key:
                if not active:
                    self.beginResetModel()
                    self._items.pop(i)
                    self.endResetModel()
                    if len(self._items) != previous_count:
                        self.countChanged.emit()
                    return
                # No-op refresh: same (text,icon,priority). Just bump TTL —
                # no model reset, no reordering. Without this guard, callers
                # that re-assert alerts every poll tick force a full Repeater
                # rebuild ~3×/s for every active alert.
                if item.text == text and item.icon == icon and item.priority == priority:
                    item.until = until
                    return
                self.beginResetModel()
                item = self._items.pop(i)
                item.text = text
                item.icon = icon
                item.priority = priority
                item.until = until  # refresh TTL
                self._items.insert(0, item)
                self._items = self._items[:5]
                self.endResetModel()
                return
        if active and text:
            self.beginResetModel()
            self._items.insert(0, AlertItem(key=key, text=text, icon=icon,
                                            priority=priority, until=until))
            self._items = self._items[:5]
            self.endResetModel()
            if len(self._items) != previous_count:
                self.countChanged.emit()


class AlertsBridge(QObject):
    """Wraps AlertsModel and provides a QML-friendly interface."""

    alertTextChanged = pyqtSignal()
    # Catalog config (tracked/sound/volume/duck) — separate from the model's
    # own countChanged, since this is Settings-page state, not live alerts.
    alertCatalogChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = AlertsModel(self)
        self._alert_text = "No alerts"

        # Periodically evict expired alerts so the UI updates automatically.
        # Only run the timer when at least one alert is present — otherwise
        # 5 s wakeups burn CPU on the Pi for nothing.
        self._cleanup_timer = QTimer(self)
        self._cleanup_timer.setInterval(5000)  # every 5 s
        self._cleanup_timer.timeout.connect(self._on_cleanup_tick)
        self._model.countChanged.connect(self._sync_cleanup_timer)

        # Wired from main.py once both exist — see set_music_bridge/
        # set_modding_bridge. Either can stay None (e.g. during startup, or
        # if a bridge fails to construct); ducking just no-ops for that half.
        self._music_bridge = None
        self._modding_bridge = None
        self._alert_ducked = False

        # One-shot alert chime. A dedicated pygame Channel/Sound, never the
        # shared pygame.mixer.music stream Soundboard plays through — an
        # alert firing must not interrupt (or be interrupted by) a Soundboard
        # sound. Polled rather than event-driven because pygame has no Qt-
        # native "playback finished" signal; only runs while a chime is
        # actually in flight.
        self._chime_channels: list = []
        self._chime_timer = QTimer(self)
        self._chime_timer.setInterval(200)
        self._chime_timer.timeout.connect(self._on_chime_poll)
        # A duck must never outlive its chime. If the poll stops for any
        # reason (mixer error, a channel that lies about get_busy) this is
        # the backstop that puts the volume back.
        self._duck_watchdog = QTimer(self)
        self._duck_watchdog.setSingleShot(True)
        self._duck_watchdog.setInterval(8000)
        self._duck_watchdog.timeout.connect(self._on_duck_watchdog)

    def _sync_cleanup_timer(self):
        if self._model.count > 0:
            if not self._cleanup_timer.isActive():
                self._cleanup_timer.start()
        else:
            if self._cleanup_timer.isActive():
                self._cleanup_timer.stop()

    def _on_cleanup_tick(self):
        self._model.cleanup_expired()
        # cleanup_expired emits countChanged on transitions; the sync slot
        # handles stop when the list becomes empty.

    @pyqtProperty(QObject, constant=True)
    def model(self):
        return self._model

    @pyqtProperty(str, notify=alertTextChanged)
    def alertText(self):
        return self._alert_text

    @alertText.setter
    def alertText(self, text):
        if text != self._alert_text:
            self._alert_text = str(text or "No alerts")
            self.alertTextChanged.emit()

    @pyqtSlot(str, str, str, str, bool)
    def setAlert(self, key, text, icon, priority, active):
        cfg = _alert_config(key)
        if not cfg["tracked"]:
            # Untracked: never push a fresh one, but do clear a lingering
            # instance from before tracking was switched off.
            if not active:
                return
            if self._model.is_active(key):
                self._model.set_alert(key, "", active=False)
            return
        became_active = bool(active) and not self._model.is_active(key)
        self._model.set_alert(key, text, icon, priority, active)
        if became_active and cfg["sound_enabled"] and cfg["sound_path"]:
            self._play_chime(cfg["sound_path"])


    # ── alert catalog: tracked / sound / volume / ducking ─────────────────
    def set_music_bridge(self, bridge):
        self._music_bridge = bridge

    def set_modding_bridge(self, bridge):
        self._modding_bridge = bridge

    @pyqtProperty(list, notify=alertCatalogChanged)
    def alertCatalog(self):
        """Every configurable alert, catalog labels merged with live config —
        same shape as MusicBridge.mediaKeyBindings: QML re-reads this whole
        list on alertCatalogChanged, so a row's toggle reflects the write
        that was just made to it."""
        rows = []
        for key, label, category in ALERT_CATALOG:
            cfg = _alert_config(key)
            icon, priority = ALERT_PREVIEW.get(key, ("", "info"))
            rows.append({
                "key": key,
                "label": label,
                "category": category,
                "icon": icon,
                "priority": priority,
                "tracked": cfg["tracked"],
                "soundEnabled": cfg["sound_enabled"],
                "soundName": cfg["sound_name"],
            })
        return rows

    @pyqtSlot(str, bool)
    def setAlertTracked(self, key, tracked):
        settings_cache.set_value(
            _ALERT_SOUNDS_SECTION, f"{key}_tracked",
            "1" if tracked else "0", path=DATA_SETTINGS,
        )
        self.alertCatalogChanged.emit()

    @pyqtSlot(str, bool)
    def setAlertSoundEnabled(self, key, enabled):
        settings_cache.set_value(
            _ALERT_SOUNDS_SECTION, f"{key}_sound_enabled",
            "1" if enabled else "0", path=DATA_SETTINGS,
        )
        self.alertCatalogChanged.emit()

    @pyqtSlot(str, str, str)
    def setAlertSound(self, key, path, name):
        settings_cache.set_value(
            _ALERT_SOUNDS_SECTION, f"{key}_sound_path", str(path or ""), path=DATA_SETTINGS
        )
        settings_cache.set_value(
            _ALERT_SOUNDS_SECTION, f"{key}_sound_name", str(name or ""), path=DATA_SETTINGS
        )
        self.alertCatalogChanged.emit()

    @pyqtProperty(int, notify=alertCatalogChanged)
    def alertVolumePercent(self):
        """Fixed chime volume — deliberately never 100%; see setAlertVolumePercent."""
        return max(0, min(100, settings_cache.getint(
            _ALERT_SOUNDS_SECTION, "volume_percent", fallback=65, path=DATA_SETTINGS
        )))

    @pyqtSlot(int)
    def setAlertVolumePercent(self, value):
        settings_cache.set_value(
            _ALERT_SOUNDS_SECTION, "volume_percent",
            str(max(0, min(100, int(value)))), path=DATA_SETTINGS,
        )
        self.alertCatalogChanged.emit()

    @pyqtProperty(int, notify=alertCatalogChanged)
    def alertDuckPercent(self):
        return max(0, min(100, settings_cache.getint(
            _ALERT_SOUNDS_SECTION, "duck_percent", fallback=50, path=DATA_SETTINGS
        )))

    @pyqtSlot(int)
    def setAlertDuckPercent(self, value):
        settings_cache.set_value(
            _ALERT_SOUNDS_SECTION, "duck_percent",
            str(max(0, min(100, int(value)))), path=DATA_SETTINGS,
        )
        self.alertCatalogChanged.emit()

    # ── one-shot chime + ducking ───────────────────────────────────────────
    def _play_chime(self, path):
        if not path or not os.path.isfile(path):
            return
        try:
            import pygame
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            sound = pygame.mixer.Sound(path)
        except Exception:
            return
        try:
            sound.set_volume(self.alertVolumePercent / 100.0)
        except Exception:
            pass
        # Duck only once the chime is actually playing. Ducking first and
        # letting play() raise — a USB audio device that re-enumerated, or no
        # free mixer channel — left the duck applied with no timer running to
        # release it, so the engine and music stayed at duck_percent (50 % by
        # default) until the app was restarted.
        try:
            channel = sound.play()
        except Exception as exc:
            print(f"[alerts] chime play failed: {exc}", flush=True)
            self._release_alert_duck()
            return
        self._duck_for_alert()
        if channel is not None:
            self._chime_channels.append(channel)
        if not self._chime_timer.isActive():
            self._chime_timer.start()
        self._duck_watchdog.start()

    def _on_chime_poll(self):
        self._chime_channels = [c for c in self._chime_channels if c.get_busy()]
        if not self._chime_channels:
            self._chime_timer.stop()
            self._release_alert_duck()

    def _duck_for_alert(self):
        if self._alert_ducked:
            return
        self._alert_ducked = True
        pct = self.alertDuckPercent
        if self._music_bridge is not None:
            try:
                self._music_bridge.duckForExternal(pct)
            except Exception:
                pass
        if self._modding_bridge is not None:
            try:
                self._modding_bridge.duckForAlert(pct)
            except Exception:
                pass

    def _on_duck_watchdog(self):
        """Last resort: the chime poll never finished the job."""
        if self._alert_ducked:
            print("[alerts] duck watchdog fired — releasing a stuck alert duck", flush=True)
        self._chime_channels.clear()
        self._chime_timer.stop()
        self._release_alert_duck()

    def _release_alert_duck(self):
        try:
            self._duck_watchdog.stop()
        except Exception:
            pass
        if not self._alert_ducked:
            return
        self._alert_ducked = False
        if self._music_bridge is not None:
            try:
                self._music_bridge.releaseExternalDuck()
            except Exception:
                pass
        if self._modding_bridge is not None:
            try:
                self._modding_bridge.releaseAlertDuck()
            except Exception:
                pass
