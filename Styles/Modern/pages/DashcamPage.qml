import QtQuick
import QtQuick.Window
import "../components" as UI

/**
 * 70mai page (Nav index 7) — the dashcam over its own Wi-Fi hotspot.
 *
 * Reachable from the dock only while [Dashcam] enabled (the pill after Horn).
 * Everything on it is driven by the Dashcam bridge; the page owns no timers:
 * `setPageVisible` tells the bridge to poll faster and to run the live
 * stream, and leaving the page stops both (bridges/dashcam_bridge.py).
 *
 * Left: the live picture (frames arrive through the `dashcam` image
 * provider — `image://dashcam/<rev>`, a new rev per frame) with a state
 * placeholder over it, and the controls. Right: what the camera reports.
 */
Item {
    id: root
    objectName: "dashcamPage"

    readonly property bool   on:       Dashcam ? Dashcam.enabled : false
    readonly property string state:    Dashcam ? Dashcam.state : "off"
    readonly property bool   online:   state === "online"
    readonly property bool   linked:   Dashcam ? Dashcam.linked : false
    readonly property bool   streaming: Dashcam ? Dashcam.streamActive : false
    readonly property string streamState: Dashcam ? Dashcam.streamState : ""
    readonly property bool   picture:  streaming && streamState === "playing"
                                       && Dashcam && Dashcam.frameRev > 0

    // Tap the picture → full screen (window-level overlay); tap, leaving the
    // page or a screensaver brings it back. One Image at a time asks the
    // provider: the in-page one goes blank while the full-screen one shows.
    property bool fullScreen: false

    onVisibleChanged: {
        if (!visible) fullScreen = false;
        if (Dashcam) Dashcam.setPageVisible(visible);
    }
    Component.onCompleted: if (visible && Dashcam) Dashcam.setPageVisible(true)
    Connections {
        target: Nav
        function onScreensaverChanged() { if (Nav.screensaverActive) root.fullScreen = false; }
    }

    // ── what the placeholder says ───────────────────────────────────────
    readonly property string titleText: {
        if (!on)                          return "70mai is off";
        if (state === "off")              return "Camera not connected";
        if (state === "scanning")         return "Looking for the camera…";
        if (state === "joining")          return "Joining the camera's Wi-Fi…";
        if (state === "needs_pairing")    return "Pair the camera";
        if (state === "pairing")          return "Press the camera's power button";
        if (state === "error")            return Dashcam.statusText || "Camera error";
        if (!streaming)                   return "Live view off";
        if (streamState === "error")      return "No live picture";
        return "Connecting to the stream…";
    }
    readonly property string bodyText: {
        if (!on)                          return "Enable it in Settings › Core › 70mai";
        if (state === "off")              return "Connect joins the camera's Wi-Fi hotspot";
        if (state === "needs_pairing")    return "Press Pair, then the power button on the camera when asked";
        if (state === "pairing")          return "Waiting up to a minute for the press…";
        if (state === "error")            return Dashcam.lastError || "";
        if (online && streaming && streamState === "error")
                                          return Dashcam.streamError || "";
        if (online && !streaming)         return Dashcam.liveOnOpen
                                            ? "Press Live view to start it"
                                            : "Live view starts from the button below";
        return "";
    }

    // ── left: picture + controls ────────────────────────────────────────
    Item {
        id: left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.margins: Theme.pad
        width: Math.round(parent.width * 0.60) - Theme.pad

        UI.Card {
            id: frame
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: parent.right
            // All the height the controls leave; the picture keeps its own
            // aspect inside (PreserveAspectFit), so 4:3 and 16:9 cams both fit.
            height: Math.max(200, left.height - controls.height - statusLine.implicitHeight - 12 - Theme.gap - 10)
            padding: 0
            clip: true

            // Double-buffered (UI.LiveFrame): a plain Image blanks between
            // frames on the Pi, which reads as flicker.
            UI.LiveFrame {
                id: live
                anchors.fill: parent
                anchors.margins: 2
                visible: root.picture
                provider: "dashcam"
                rev: Dashcam ? Dashcam.frameRev : 0
                active: root.streaming && !root.fullScreen
            }

            MouseArea {
                anchors.fill: parent
                enabled: root.picture
                onClicked: root.fullScreen = true
            }

            // REC badge over the picture
            Rectangle {
                visible: root.picture && Dashcam.recording === 1
                anchors.top: parent.top
                anchors.left: parent.left
                anchors.margins: 14
                width: recRow.implicitWidth + 22
                height: 30
                radius: 15
                color: Theme.dangerBg
                Row {
                    id: recRow
                    anchors.centerIn: parent
                    spacing: 8
                    Rectangle { width: 10; height: 10; radius: 5; color: Theme.danger
                                anchors.verticalCenter: parent.verticalCenter }
                    Text { text: "REC"; color: Theme.dangerSoft
                           font.family: Theme.fontFamily; font.pixelSize: 13; font.weight: Font.Bold
                           anchors.verticalCenter: parent.verticalCenter }
                }
            }

            // state placeholder
            Column {
                visible: !root.picture
                anchors.centerIn: parent
                width: parent.width - 80
                spacing: 12

                Item {
                    id: iconHolder
                    width: 96
                    height: 96
                    anchors.horizontalCenter: parent.horizontalCenter
                    SequentialAnimation on opacity {
                        running: root.visible && !root.picture
                                 && (root.state === "scanning" || root.state === "joining"
                                     || root.state === "pairing"
                                     || (root.streaming && root.streamState !== "error"))
                        loops: Animation.Infinite
                        NumberAnimation { to: 0.45; duration: 700; easing.type: Easing.InOutSine }
                        NumberAnimation { to: 1.0;  duration: 700; easing.type: Easing.InOutSine }
                        onRunningChanged: if (!running) iconHolder.opacity = 1.0
                    }
                    Rectangle {
                        anchors.centerIn: parent
                        width: 88; height: 88; radius: 44
                        color: Theme.surfaceRaised
                    }
                    Image {
                        anchors.centerIn: parent
                        source: iconsPath + "/Dash/70mai.png"
                        width: 52; height: 52
                        sourceSize.width: 104
                        sourceSize.height: 104
                        fillMode: Image.PreserveAspectFit
                        smooth: true
                        asynchronous: true
                        opacity: root.on ? 0.9 : 0.45
                        onStatusChanged: if (status === Image.Error) source = iconsPath + "/side.png"
                    }
                }

                Text {
                    width: parent.width
                    text: root.titleText
                    color: root.state === "error" ? Theme.dangerSoft : Theme.ink
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSize("pages_primary_text")
                    font.weight: Font.Bold
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.Wrap
                }
                Text {
                    width: parent.width
                    visible: text !== ""
                    text: root.bodyText
                    color: Theme.inkDim
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSize("pages_secondary_text")
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.Wrap
                }
            }
        }

        // status line under the picture
        Text {
            id: statusLine
            anchors.bottom: controls.top
            anchors.bottomMargin: 12
            anchors.left: parent.left
            anchors.right: parent.right
            text: {
                if (!Dashcam) return "";
                var s = Dashcam.statusText || "";
                if (Dashcam.camIp) s += (s ? "  ·  " : "") + Dashcam.camIp;
                if (Dashcam.busy) s += "  ·  …";
                return s;
            }
            color: root.state === "error" ? Theme.dangerSoft : Theme.inkFaint
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSize("widgets_tertiary_text")
            elide: Text.ElideRight
        }

        Flow {
            id: controls
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            spacing: 10

            UI.PillButton {
                text: root.streaming ? "Live view · on" : "Live view"
                variant: root.streaming ? "accent" : "raised"
                checked: root.streaming
                enabled: root.online
                height: 50
                onClicked: root.streaming ? Dashcam.stopView() : Dashcam.startView()
            }
            UI.PillButton {
                text: "Snapshot"
                enabled: root.online && !Dashcam.busy
                height: 50
                onClicked: Dashcam.snapshot()
            }
            UI.PillButton {
                text: "Refresh"
                enabled: root.on && !Dashcam.busy
                height: 50
                onClicked: Dashcam.refresh()
            }
            UI.PillButton {
                visible: root.picture
                text: "Full screen"
                variant: "outline"
                height: 50
                onClicked: root.fullScreen = true
            }
            UI.PillButton {
                visible: root.on && !root.linked && root.state !== "joining"
                text: root.state === "scanning" ? "Connect now" : "Connect"
                variant: "accent"
                enabled: !Dashcam.busy
                height: 50
                onClicked: Dashcam.connectNow()
            }
            UI.PillButton {
                visible: root.on && (root.linked || root.state === "joining")
                text: "Disconnect"
                variant: "outline"
                height: 50
                onClicked: Dashcam.disconnectNow()
            }
            UI.PillButton {
                visible: root.on && root.linked && !root.online
                text: root.state === "pairing" ? "Cancel" : "Pair"
                variant: root.state === "pairing" ? "outline" : "accent"
                height: 50
                onClicked: root.state === "pairing" ? Dashcam.cancelPair() : Dashcam.pair()
            }
            UI.PillButton {
                visible: !root.on
                text: "Open Settings"
                variant: "accent"
                height: 50
                onClicked: if (Nav) Nav.navigateTo(6)
            }
        }
    }

    // ── right: what the camera reports ──────────────────────────────────
    Column {
        id: right
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.right: parent.right
        anchors.left: left.right
        anchors.margins: Theme.pad
        anchors.leftMargin: 0
        spacing: Theme.gap

        UI.Card {
            width: parent.width
            height: infoCol.implicitHeight + padding * 2

            Column {
                id: infoCol
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: parent.padding
                spacing: 10

                UI.SectionLabel { label: "Camera"; width: parent.width }

                component InfoRow: Row {
                    property string key: ""
                    property string value: ""
                    property color valueColor: Theme.ink
                    width: parent.width
                    spacing: 10
                    Text {
                        width: 92
                        text: key
                        color: Theme.inkFaint
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSize("widgets_secondary_text")
                    }
                    Text {
                        width: parent.width - 102
                        text: value !== "" ? value : "—"
                        color: valueColor
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSize("widgets_secondary_text")
                        font.weight: Font.DemiBold
                        elide: Text.ElideRight
                    }
                }

                InfoRow { key: "Model";    value: Dashcam ? Dashcam.model : "" }
                InfoRow { key: "Firmware"; value: Dashcam ? Dashcam.firmware : "" }
                InfoRow { key: "Network";  value: Dashcam ? (Dashcam.ssid || "auto (70mai_*)") : "" }
                InfoRow {
                    key: "Link"
                    value: !Dashcam ? "" : root.linked ? (Dashcam.camIp || "connected")
                         : root.state === "joining" ? "joining…" : root.state === "scanning" ? "looking…" : "off"
                    valueColor: root.linked ? Theme.ok : Theme.ink
                }
                InfoRow {
                    key: "Paired"
                    value: !Dashcam ? "" : Dashcam.paired ? "yes" : "no"
                    valueColor: Dashcam && Dashcam.paired ? Theme.ok : Theme.warn
                }
                InfoRow {
                    key: "Recording"
                    value: !Dashcam || !root.online ? ""
                         : Dashcam.recording === 1 ? "yes" : Dashcam.recording === 0 ? "no" : "checking…"
                    valueColor: Dashcam && Dashcam.recording === 1 ? Theme.ok
                              : Dashcam && Dashcam.recording === 0 ? Theme.dangerSoft : Theme.ink
                }

                // SD card: text + fill bar
                Column {
                    width: parent.width
                    spacing: 6
                    InfoRow { key: "SD card"; value: Dashcam ? Dashcam.sdText : "" }
                    Rectangle {
                        x: 102
                        width: parent.width - 102
                        height: 8
                        radius: 4
                        color: Theme.surfaceRaised
                        visible: Dashcam && Dashcam.sdPercent >= 0
                        Rectangle {
                            width: parent.width * Math.max(0, Math.min(100, Dashcam ? Dashcam.sdPercent : 0)) / 100
                            height: parent.height
                            radius: 4
                            color: Dashcam && Dashcam.sdPercent >= 95 ? Theme.danger
                                 : Dashcam && Dashcam.sdPercent >= 85 ? Theme.warn : Theme.accent
                        }
                    }
                }

                InfoRow { key: "GPS"; value: Dashcam ? Dashcam.gpsText : "" }
            }
        }

        UI.Card {
            width: parent.width
            height: Math.max(eventsCol.implicitHeight + padding * 2, 120)

            Column {
                id: eventsCol
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: parent.padding
                spacing: 6

                UI.SectionLabel {
                    label: "Emergency clips" + (Dashcam && Dashcam.eventCount > 0 ? " · " + Dashcam.eventCount : "")
                    width: parent.width
                }

                Text {
                    visible: !Dashcam || Dashcam.events.length === 0
                    width: parent.width
                    text: root.online ? "None on the card" : "Connect to see the camera's locked clips"
                    color: Theme.inkFaint
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSize("widgets_tertiary_text")
                    wrapMode: Text.Wrap
                }

                Repeater {
                    model: Dashcam ? Dashcam.events : []
                    Rectangle {
                        width: eventsCol.width
                        height: 40
                        radius: Theme.radiusControl
                        color: Theme.surfaceRaised
                        Row {
                            anchors.fill: parent
                            anchors.leftMargin: 12
                            anchors.rightMargin: 12
                            spacing: 10
                            Rectangle { width: 3; height: 20; radius: 1.5; color: Theme.warn
                                        anchors.verticalCenter: parent.verticalCenter }
                            Text {
                                anchors.verticalCenter: parent.verticalCenter
                                text: modelData.time
                                color: Theme.ink
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSize("widgets_secondary_text")
                                font.weight: Font.DemiBold
                            }
                            Text {
                                anchors.verticalCenter: parent.verticalCenter
                                text: modelData.size
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSize("widgets_tertiary_text")
                            }
                        }
                    }
                }
            }
        }
    }

    // ── full screen ─────────────────────────────────────────────────────
    Rectangle {
        id: fullView
        parent: Window.window ? Window.window.contentItem : root
        anchors.fill: parent
        z: 9500
        visible: root.fullScreen && root.visible
        color: Theme.ground

        UI.LiveFrame {
            anchors.fill: parent
            provider: "dashcam"
            rev: Dashcam ? Dashcam.frameRev : 0
            active: root.fullScreen && root.streaming
        }
        Rectangle {
            visible: Dashcam && Dashcam.recording === 1
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.margins: 18
            width: fsRec.implicitWidth + 24
            height: 34
            radius: 17
            color: Theme.dangerBg
            Row {
                id: fsRec
                anchors.centerIn: parent
                spacing: 8
                Rectangle { width: 10; height: 10; radius: 5; color: Theme.danger
                            anchors.verticalCenter: parent.verticalCenter }
                Text { text: "REC"; color: Theme.dangerSoft
                       font.family: Theme.fontFamily; font.pixelSize: 14; font.weight: Font.Bold
                       anchors.verticalCenter: parent.verticalCenter }
            }
        }
        Text {
            anchors.top: parent.top
            anchors.right: parent.right
            anchors.margins: 18
            text: "70mai  ·  tap to leave"
            color: Theme.inkFaint
            font.family: Theme.fontFamily
            font.pixelSize: 14
        }
        MouseArea { anchors.fill: parent; onClicked: root.fullScreen = false }
    }
}
