from __future__ import annotations

import time

from PyQt6.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot

from Modules.Runtime.telemetry_coordinator import TelemetryCoordinator
from Modules.Runtime import settings_cache
from preferences import DATA_SETTINGS


class NavigationBridge(QObject):
    """Page navigation state machine, side panel, engine toggle, screensaver."""

    currentPageChanged = pyqtSignal()
    engineStateChanged = pyqtSignal()
    sidePanelChanged = pyqtSignal()
    alertTextChanged = pyqtSignal()
    screensaverChanged = pyqtSignal()
    # Any real input anywhere in the app. The shell restarts its idle timers on
    # this rather than on a TapHandler: a TapHandler at the shell root never
    # sees a press a child MouseArea grabs, which is most of them, so the
    # screensaver used to arrive while the user was actively pressing buttons.
    userActivity = pyqtSignal()

    # 7 = the 70mai dashcam page, 8 = the action cam (GoPro / Insta360);
    # their dock pills only show while [Dashcam] / [ActionCam] enabled
    # (Styles/Modern/Dock.qml). 9 = Trip (recap, refuel price, fuel cost).
    PAGE_KEYS = ["home", "dashboard", "modding", "map", "music", "soundboard", "settings",
                 "dashcam", "actioncam", "trip"]
    PAGE_LABELS = ["Home", "Dashboard", "Modding", "Map", "Music", "Soundboard", "Settings",
                   "70mai", "Action cam", "Trip"]
    PAGE_ICONS = ["home.png", "dashboard.png", "modding.png", "map.png",
                  "music.png", "soundboard.png", "options.png", "dashcam.png", "actioncam.png",
                  "side.png"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_page = 0
        self._engine_on = False
        self._has_engine_ever_on = False
        self._telemetry_available = False
        self._side_panel_open = False
        self._manual_side_panel_open = False
        self._auto_panel_open_done = False
        self._side_panel_mini_page = 0
        self._alert_text = "No alerts"
        self._screensaver_active = False
        self._screensaver_mode = "parked"  # "parked" or "driving"
        # When the screensaver was last raised, so the tap that raised it is
        # not immediately read as the activity that dismisses it.
        self._screensaver_shown_at = 0.0
        # Set when the driving screensaver is dismissed by hand from the
        # Dashboard. While set, the idle timer will not force it back on *that*
        # page — every other page still gets it.
        self._driving_suppressed = False
        self._last_activity_ts = 0.0
        self._screensaver_manual = False
        # When the screensaver was last put away, so the rest of the gesture
        # that dismissed it cannot bring it straight back. See
        # toggleDrivingScreensaver().
        self._screensaver_hidden_at = 0.0
        coord = TelemetryCoordinator.instance()
        self._telemetry_available = bool(coord.connected() or coord.sim_enabled())
        coord.changed.connect(self._on_telemetry_changed)

    # ── page navigation ───────────────────────────────────────────────────
    @pyqtProperty(int, notify=currentPageChanged)
    def currentPage(self):
        return self._current_page

    @currentPage.setter
    def currentPage(self, value):
        self.navigateTo(value)

    @pyqtProperty(str, notify=currentPageChanged)
    def currentPageKey(self):
        if 0 <= self._current_page < len(self.PAGE_KEYS):
            return self.PAGE_KEYS[self._current_page]
        return "home"


    @pyqtProperty(list, constant=True)
    def pageKeys(self):
        return list(self.PAGE_KEYS)


    @pyqtSlot(int)
    def navigateTo(self, index):
        if not (0 <= index < len(self.PAGE_KEYS)):
            return
        # Dashboard guard: can't stay on dashboard (1) when engine is off
        if index == 1 and not self._engine_on:
            index = 0
        if index != self._current_page:
            self._current_page = index
            self.currentPageChanged.emit()
            self._update_side_panel_visibility()

    @pyqtSlot(str)
    def navigateToKey(self, key):
        if key in self.PAGE_KEYS:
            self.navigateTo(self.PAGE_KEYS.index(key))


    # ── engine state ──────────────────────────────────────────────────────
    @pyqtProperty(bool, notify=engineStateChanged)
    def engineOn(self):
        return self._engine_on

    @engineOn.setter
    def engineOn(self, value):
        if value != self._engine_on:
            self._engine_on = bool(value)
            if self._engine_on:
                self._has_engine_ever_on = True
                if not self._auto_panel_open_done:
                    self._auto_panel_open_done = True
                    self._manual_side_panel_open = True
                    self._update_side_panel_visibility()
            else:
                # Engine off — always close side panel
                self._manual_side_panel_open = False
                self._update_side_panel_visibility()
            self.engineStateChanged.emit()

    @pyqtProperty(bool, notify=engineStateChanged)
    def hasEngineEverOn(self):
        return self._has_engine_ever_on


    # ── side panel ────────────────────────────────────────────────────────
    @pyqtProperty(bool, notify=sidePanelChanged)
    def sidePanelOpen(self):
        return self._side_panel_open

    @pyqtProperty(int, notify=sidePanelChanged)
    def sidePanelMiniPage(self):
        return self._side_panel_mini_page

    @pyqtProperty(float, constant=True)
    def sidePanelRatio(self):
        return 0.40

    @pyqtSlot()
    def toggleSidePanel(self):
        self._manual_side_panel_open = not self._manual_side_panel_open
        self._update_side_panel_visibility()

    @pyqtSlot(bool)
    def setSidePanelOpen(self, opened):
        self._manual_side_panel_open = opened
        self._update_side_panel_visibility()

    @pyqtSlot(int)
    def setSidePanelMiniPage(self, index):
        clean = max(0, min(3, int(index)))
        if clean != self._side_panel_mini_page:
            self._side_panel_mini_page = clean
            self.sidePanelChanged.emit()

    def _on_telemetry_changed(self, _snap):
        coord = TelemetryCoordinator.instance()
        available = bool(coord.connected() or coord.sim_enabled())
        if available == self._telemetry_available:
            return
        self._telemetry_available = available
        if not available and self._current_page != 0:
            self._current_page = 0
            self.currentPageChanged.emit()
        self._update_side_panel_visibility()

    def _update_side_panel_visibility(self):
        should = self._manual_side_panel_open
        if should != self._side_panel_open:
            self._side_panel_open = should
            self.sidePanelChanged.emit()

    # ── alerts ────────────────────────────────────────────────────────────
    @pyqtProperty(str, notify=alertTextChanged)
    def alertText(self):
        return self._alert_text

    @alertText.setter
    def alertText(self, text):
        if text != self._alert_text:
            self._alert_text = str(text or "No alerts")
            self.alertTextChanged.emit()

    # ── screensaver ───────────────────────────────────────────────────────
    @pyqtProperty(bool, notify=screensaverChanged)
    def screensaverActive(self):
        return self._screensaver_active

    @pyqtProperty(str, notify=screensaverChanged)
    def screensaverMode(self):
        return self._screensaver_mode

    @pyqtProperty(bool, notify=screensaverChanged)
    def screensaverManual(self):
        """True when the screensaver was raised by hand rather than by the idle
        timer. The shell's "speed dropped, put it away" rule skips these:
        asking for it at a red light and having it vanish is not useful."""
        return self._screensaver_manual

    @pyqtSlot()
    def showScreensaver(self):
        if not self._screensaver_active:
            self._screensaver_manual = False
            self._screensaver_active = True
            self._screensaver_shown_at = time.monotonic()
            self.screensaverChanged.emit()

    @pyqtSlot()
    def hideScreensaver(self):
        if self._screensaver_active:
            self._screensaver_active = False
            self._screensaver_hidden_at = time.monotonic()
            self.screensaverChanged.emit()

    # ── activity ─────────────────────────────────────────────────────────
    # Called from the application-wide event filter (see main.py) on any press,
    # touch or key. Cheap by design: a bool check and, at most, one signal.
    _ACTIVITY_MIN_INTERVAL_S = 0.20
    # A press that lands within this of the screensaver being raised is the
    # press that raised it, not a dismissal.
    _SHOW_GRACE_S = 0.40

    @pyqtSlot()
    def notifyActivity(self):
        now = time.monotonic()
        if self._screensaver_active:
            if (now - self._screensaver_shown_at) >= self._SHOW_GRACE_S:
                # Dismissing the driving screensaver by hand while the Dashboard
                # is the page underneath means "I want the Dashboard", so it
                # pins off there — the same thing the centre tap does.
                if self._screensaver_mode == "driving" and self._current_page == 1:
                    self._driving_suppressed = True
                self.hideScreensaver()
            return
        if (now - self._last_activity_ts) < self._ACTIVITY_MIN_INTERVAL_S:
            return
        self._last_activity_ts = now
        self.userActivity.emit()

    # ── driving screensaver: manual override on the Dashboard ────────────
    @pyqtProperty(bool, notify=screensaverChanged)
    def drivingSuppressed(self):
        """True once the driving screensaver has been dismissed by hand from
        the Dashboard. Only the Dashboard honours it."""
        return self._driving_suppressed

    # A tap that dismisses the screensaver must not also be read as the tap
    # that asks for it back. The activity filter runs on MouseButtonPress,
    # *before* Qt Quick delivers that press to any item — so by delivery time
    # the screensaver is already hidden, the press lands on whatever is beneath
    # it, and on the Dashboard that is this very toggle. The release then
    # completed a click and put the screensaver straight back up, which is the
    # "it keeps coming back instead of showing the Dashboard" the user hit.
    _DISMISS_COOLDOWN_S = 0.9

    @pyqtSlot()
    def toggleDrivingScreensaver(self):
        """Dashboard centre tap. On -> off pins it off for this page; off -> on
        clears the pin, so the idle timer works again."""
        if self._screensaver_active:
            self._driving_suppressed = True
            self._screensaver_active = False
            self._screensaver_hidden_at = time.monotonic()
            self.screensaverChanged.emit()
            return
        if (time.monotonic() - self._screensaver_hidden_at) < self._DISMISS_COOLDOWN_S:
            # Tail of the dismissal, not a new request. Leave the pin alone.
            return
        self._driving_suppressed = False
        self._screensaver_mode = "driving"
        self._screensaver_manual = True
        self._screensaver_active = True
        self._screensaver_shown_at = time.monotonic()
        self.screensaverChanged.emit()

    @pyqtSlot(str)
    def setScreensaverMode(self, mode):
        if mode in ("parked", "driving") and mode != self._screensaver_mode:
            self._screensaver_mode = mode
            self.screensaverChanged.emit()

    # ── settings queries ──────────────────────────────────────────────────
    @pyqtSlot(result=bool)
    def animationEnabled(self):
        return settings_cache.getboolean(
            "UI", "animation_master_enabled", fallback=True, path=DATA_SETTINGS
        )

    @pyqtSlot(result=bool)
    def sidePanelAnimationEnabled(self):
        return settings_cache.getboolean(
            "UI", "side_panel_animation_enabled", fallback=True, path=DATA_SETTINGS
        )

    @pyqtSlot(result=bool)
    def introAnimationEnabled(self):
        """Home cards slide up on page open (Settings → Animation → Intro)."""
        return self.animationEnabled() and settings_cache.getboolean(
            "UI", "home_intro_enabled", fallback=False, path=DATA_SETTINGS
        )

    @pyqtSlot(result=bool)
    def settingsAnimationEnabled(self):
        """Settings slides in from the right on open (Animation → Settings Animation)."""
        return self.animationEnabled() and settings_cache.getboolean(
            "UI", "settings_page_animation_enabled", fallback=False, path=DATA_SETTINGS
        )

