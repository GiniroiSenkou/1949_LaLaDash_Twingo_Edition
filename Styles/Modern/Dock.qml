import QtQuick
import "components" as UI

/**
 * Bottom dock — 88 px. Settings key on the left, a floating pill group of page
 * buttons in the centre, network rate (parked) or alerts (driving) / mute /
 * engine on the right.
 *
 * The pills are text, not the 1024×1024 PNGs the legacy nav bar loaded, which
 * is where most of this skin's texture-memory saving comes from.
 *
 * Page indices follow NavigationBridge.PAGE_KEYS:
 *   0 Home · 1 Dashboard · 2 Modding · 3 AirPlay · 4 Music · 5 Soundboard · 6 Settings
 *   7 70mai · 8 action cam · 9 Trip
 * Home and Dashboard swap on engine state, exactly as the legacy bar did.
 */
Item {
    id: dock
    height: Theme.dockH

    readonly property var pages: [
        { label: "Home",      page: 0 },
        { label: "Dashboard", page: 1 },
        { label: "Engine",    page: 2 },
        { label: "AirPlay",   page: 3 },
        { label: "Music",     page: 4 },
        { label: "Horn",      page: 5 },
        { label: "Trip",      page: 9 },
        { label: "70mai",     page: 7 },
        { label: "Cam",       page: 8 }   // label follows ActionCam.label
    ]

    function _pageVisible(index) {
        if (index === 0) return !Nav.engineOn;   // Home hidden while running
        if (index === 1) return Nav.engineOn;    // Dashboard only while running
        if (index === 3) return AirPhone ? AirPhone.serviceOn : true;      // AirPlay, unless the extra is off
        if (index === 7) return Dashcam ? Dashcam.enabled : false;       // the dashcam, when enabled
        if (index === 8) return ActionCam ? ActionCam.enabled : false;   // GoPro / Insta360, when enabled
        return true;
    }

    // ── settings key ───────────────────────────────────────────────────
    UI.IconButton {
        id: settingsKey
        anchors.left: parent.left
        anchors.leftMargin: 22
        anchors.verticalCenter: parent.verticalCenter
        anchors.verticalCenterOffset: -6
        width: 64
        height: 60
        checked: Nav.currentPage === 6
        iconSource: iconsPath + "/options.png"
        iconSize: 26
        onClicked: Nav.navigateTo(6)
    }

    // ── page pills ─────────────────────────────────────────────────────
    Rectangle {
        id: pillGroup
        anchors.horizontalCenter: parent.horizontalCenter
        // Centred on the dock while it fits; with the camera pills on it
        // would run under the right cluster, so it slides left just enough
        // (the settings key on the left has the room).
        anchors.horizontalCenterOffset: {
            var rightEdge = dock.width / 2 + width / 2;
            var limit = rightCluster.x - 14;
            return rightEdge > limit ? -(rightEdge - limit) : 0;
        }
        anchors.verticalCenter: parent.verticalCenter
        anchors.verticalCenterOffset: -6
        width: pillRow.implicitWidth + 12
        height: 64
        radius: 18
        color: Theme.surface

        Row {
            id: pillRow
            anchors.centerIn: parent
            spacing: 10

            Repeater {
                model: dock.pages

                UI.PillButton {
                    visible: dock._pageVisible(modelData.page)
                    width: visible ? implicitWidth : 0
                    height: 52
                    text: modelData.page === 8 && ActionCam ? ActionCam.label : modelData.label
                    variant: "ghost"
                    // A refuel waiting for its price lights the Trip pill.
                    checked: Nav.currentPage === modelData.page
                             || (modelData.page === 9 && Trip ? Trip.refuelPending : false)
                    fontSize: Theme.fontSize("buttons_medium_text")
                    radius: 14
                    onClicked: Nav.navigateTo(modelData.page)
                }
            }
        }
    }

    // ── right cluster ──────────────────────────────────────────────────
    Row {
        id: rightCluster
        anchors.right: parent.right
        anchors.rightMargin: 22
        anchors.verticalCenter: parent.verticalCenter
        anchors.verticalCenterOffset: -6
        spacing: 10

        // Parked: the network's download/upload rate (what you want to know
        // while a sync or an update runs with the car sitting still).
        // Driving: the alert summary, which is what matters then.
        Text {
            readonly property bool parked: !Nav.engineOn
            text: parked
                  ? (Settings && Settings.netRateText !== ""
                     ? Settings.netRateText : "No network")
                  : (Nav.alertText && Nav.alertText !== "" ? Nav.alertText : "No alerts")
            color: Theme.inkFaint
            font.family: Theme.fontFamily
            font.pixelSize: 14
            elide: Text.ElideRight
            width: Math.min(implicitWidth, 260)
            anchors.verticalCenter: parent.verticalCenter
            rightPadding: 6
        }

        UI.IconButton {
            id: muteBtn
            property bool muted: false
            width: 64; height: 60
            glyph: muted ? "🔇" : "🔊"
            glyphSize: 22
            checked: muted
            activeColor: Theme.danger
            onClicked: {
                muted = !muted;
                Music.setMute(muted);
            }
        }

        // Engine sound: state on the left, mode on the right — the mockup's
        // "Engine / Quiet" stack, made live.
        Rectangle {
            width: engineCol.implicitWidth + 32
            height: 60
            radius: 16
            color: engineMa.pressed ? Theme.surfacePressed
                 : (engineMa.containsMouse ? Theme.surfaceHover : Theme.surface)
            Behavior on color { ColorAnimation { duration: 110 } }

            Column {
                id: engineCol
                anchors.centerIn: parent
                spacing: 1

                Text {
                    text: "ENGINE"
                    color: Theme.inkFaint
                    font.family: Theme.fontFamily
                    font.pixelSize: 11
                    font.letterSpacing: 0.8
                    anchors.horizontalCenter: parent.horizontalCenter
                }
                Text {
                    text: Modding && Modding.engineSoundOn
                          ? (Modding.preset || "On")
                          : "Off"
                    color: (Modding && Modding.engineSoundOn) ? Theme.accent : Theme.inkDim
                    font.family: Theme.fontFamily
                    font.pixelSize: 15
                    font.weight: Font.Bold
                    anchors.horizontalCenter: parent.horizontalCenter
                }
            }

            MouseArea {
                id: engineMa
                anchors.fill: parent
                hoverEnabled: true
                onClicked: Modding.toggleEngineSound()
            }
        }
    }
}
