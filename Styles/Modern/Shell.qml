import QtQuick
import QtQuick.Layouts
import "pages" as Pages
import "components" as UI

/**
 * Modern shell — status strip, page stack, side panel, dock, screensavers.
 *
 * This is the whole of the modern skin's chrome. main.qml loads exactly one
 * shell, so nothing here exists when another style is active.
 *
 * Page stack order matches NavigationBridge.PAGE_KEYS:
 *   0 Home · 1 Dashboard · 2 Modding · 3 AirPlay · 4 Music · 5 Soundboard · 6 Settings · 7 70mai · 8 Action cam
 */
Item {
    id: shell
    anchors.fill: parent

    // ── settings helpers (lifted from the legacy shell) ─────────────────
    function _boolSetting(section, key, fallbackValue) {
        if (!Settings)
            return fallbackValue;
        var raw = String(Settings.getValue(section, key) || "").trim().toLowerCase();
        if (raw === "")
            return fallbackValue;
        return raw === "1" || raw === "true" || raw === "yes" || raw === "on";
    }

    function _intSetting(section, key, fallbackValue) {
        if (!Settings)
            return fallbackValue;
        var parsed = parseInt(Settings.getValue(section, key));
        if (!isFinite(parsed) || parsed <= 0)
            parsed = fallbackValue;
        return parsed;
    }

    // Parked means *parked*: engine off, not turning, not rolling. `!engineOn`
    // alone is a state-machine flag with hysteresis in it, so on its own it
    // let the parked screensaver arrive over a car that was still moving.
    function _parkingScreensaverAllowed() {
        return _boolSetting("UI", "parking_screensaver_enabled", true)
               && !Nav.engineOn
               && Telemetry.rpm < 1.0
               && Telemetry.speed < 1.0;
    }

    function _drivingScreensaverAllowed() {
        return _boolSetting("UI", "driving_screensaver_enabled", true)
               && Nav.engineOn && Telemetry.speed > 8.0;
    }

    // The Dashboard is the one page with a manual override: tapping its centre
    // toggles the driving screensaver, and having toggled it *off* there pins
    // it off for that page only. Every other page still gets it on the timer.
    function _drivingTimerAllowed() {
        return _drivingScreensaverAllowed()
               && !(Nav.currentPage === 1 && Nav.drivingSuppressed);
    }

    function _syncScreensaverIntervals() {
        parkedScreensaverTimer.interval = _intSetting("UI", "parking_screensaver_timeout_sec", 20) * 1000;
        drivingScreensaverTimer.interval = _intSetting("UI", "driving_screensaver_timeout_sec", 20) * 1000;
    }

    // Nav.notifyActivity() has already dealt with dismissing an active
    // screensaver (and with ignoring the press that raised one). All this has
    // to do is put the idle countdown back to the start.
    function _registerUserActivity() {
        _syncScreensaverIntervals();
        if (drivingScreensaverTimer.running)
            drivingScreensaverTimer.restart();
        if (parkedScreensaverTimer.running)
            parkedScreensaverTimer.restart();
    }

    Rectangle {
        anchors.fill: parent
        color: Theme.ground
    }

    StatusStrip {
        id: statusStrip
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
    }

    StackLayout {
        id: pageStack
        anchors.top: statusStrip.bottom
        anchors.bottom: dock.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.rightMargin: sidePanel.visibleWidth
        currentIndex: Nav.currentPage
        clip: false

        Pages.HomePage {}
        Pages.DashboardPage {}
        Pages.EnginePage {}
        Pages.AirPlayPage {}
        Pages.MusicPage {}
        Pages.SoundboardPage {}
        Pages.SettingsPage {}
        Pages.DashcamPage {}     // 7 — dock pill only while [Dashcam] enabled
        Pages.ActionCamPage {}   // 8 — GoPro / Insta360, pill only while [ActionCam] enabled
        Pages.TripPage {}        // 9 — trip recap, refuel price, fuel cost
    }

    SidePanel {
        id: sidePanel
        anchors.top: statusStrip.bottom
        anchors.bottom: dock.top
        z: 120
    }

    Dock {
        id: dock
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.right: parent.right
    }

    // ── reverse view ────────────────────────────────────────────────────
    // The action cam's picture over whatever page is showing while the CAN
    // "reverse" signal is on (Settings › Action cam › Show when reversing).
    // A Loader, so it costs nothing until reverse is engaged; a tap puts it
    // away until the next time reverse is engaged.
    Loader {
        id: reverseView
        anchors.top: statusStrip.bottom
        anchors.bottom: dock.top
        anchors.left: parent.left
        anchors.right: parent.right
        z: 250   // above the screensavers: a rear view is never hidden by a clock
        property bool dismissed: false
        readonly property bool wanted: ActionCam && ActionCam.enabled && ActionCam.reverseView
                                       && ActionCam.reverseActive
        onWantedChanged: if (!wanted) dismissed = false
        active: wanted && !dismissed
        sourceComponent: Rectangle {
            color: Theme.ground
            readonly property bool picture: ActionCam && ActionCam.streamActive
                                            && ActionCam.streamState === "playing" && ActionCam.frameRev > 0
            UI.LiveFrame {
                anchors.fill: parent
                visible: parent.picture
                provider: "actioncam"
                rev: ActionCam ? ActionCam.frameRev : 0
                active: ActionCam ? ActionCam.streamActive : false
            }
            Text {
                anchors.centerIn: parent
                visible: !parent.picture
                text: !ActionCam ? "" : ActionCam.state === "online"
                      ? (ActionCam.streamState === "error" ? "Rear camera: " + ActionCam.streamError : "Rear camera · connecting…")
                      : "Rear camera · " + (ActionCam.statusText || "not connected")
                color: Theme.inkDim
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSize("pages_primary_text")
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.Wrap
                width: parent.width - 80
            }
            Rectangle {
                anchors.top: parent.top
                anchors.left: parent.left
                anchors.margins: 16
                width: revRow.implicitWidth + 24
                height: 34
                radius: 17
                color: Theme.scrim
                Row {
                    id: revRow
                    anchors.centerIn: parent
                    spacing: 8
                    Rectangle { width: 10; height: 10; radius: 5; color: Theme.warn
                                anchors.verticalCenter: parent.verticalCenter }
                    Text { text: "REVERSE  ·  " + (ActionCam ? ActionCam.label : "camera"); color: Theme.ink
                           font.family: Theme.fontFamily; font.pixelSize: 14; font.weight: Font.Bold
                           anchors.verticalCenter: parent.verticalCenter }
                }
            }
            MouseArea { anchors.fill: parent; onClicked: reverseView.dismissed = true }
        }
    }

    Screensavers {
        id: screensavers
        anchors.fill: parent
        z: 200
    }

    Timer {
        id: parkedScreensaverTimer
        interval: 20000
        running: !Nav.screensaverActive && shell._parkingScreensaverAllowed()
                 && !shell._drivingTimerAllowed()
        repeat: false
        onRunningChanged: if (running) shell._syncScreensaverIntervals()
        onTriggered: {
            Nav.setScreensaverMode("parked");
            Nav.showScreensaver();
        }
    }

    Timer {
        id: drivingScreensaverTimer
        interval: 20000
        running: !Nav.screensaverActive && shell._drivingTimerAllowed()
        repeat: false
        onRunningChanged: if (running) shell._syncScreensaverIntervals()
        onTriggered: {
            Nav.setScreensaverMode("driving");
            Nav.showScreensaver();
        }
    }

    // Debounced auto-hide. _drivingScreensaverAllowed() (speed > 8 km/h) has
    // no hysteresis band of its own, so speed hovering right at that line in
    // stop-start traffic could flip it on every telemetry tick — hiding and
    // immediately re-showing the driving screensaver (replaying its whole
    // intro fade) several times a second. A Timer bound to the condition only
    // fires once it has held continuously for the full interval: as long as
    // `running`'s computed value stays true across ticks the binding never
    // reassigns it, so the countdown is never restarted mid-way — the moment
    // speed pops back above 8, the binding flips false and QML stops the
    // timer outright. Same `running:`-driven pattern the two screensaver
    // timers below already use.
    Timer {
        id: drivingAutoHideTimer
        interval: 1500
        repeat: false
        running: Nav.screensaverActive && Nav.screensaverMode === "driving"
                 && !Nav.screensaverManual && !shell._drivingScreensaverAllowed()
        onTriggered: Nav.hideScreensaver()
    }

    // Activity comes from the application-wide event filter, not from a
    // handler in the scene: a TapHandler here never sees a press that a child
    // MouseArea grabs, so buttons, list rows and the dock did not count as
    // use. Changing page counts too — it is the clearest possible signal that
    // someone is at the wheel, and the screensaver landing on the page you
    // just navigated to was the most visible form of the bug.
    Connections {
        target: Nav
        function onUserActivity() { shell._registerUserActivity(); }
        function onCurrentPageChanged() { shell._registerUserActivity(); }
    }

    Component.onCompleted: {
        shell._syncScreensaverIntervals();
        if (Settings)
            Settings.setFullscreenEnabled(Settings.fullscreenEnabled);
    }
}
