import QtQuick
import QtQuick.Window
import "../components" as UI
import "../sheets" as Sheets

/**
 * Action cam page (Nav index 8) — a GoPro or an Insta360 over USB or Wi-Fi.
 *
 * Same shape as the 70mai page: the picture on the left (frames through the
 * `actioncam` image provider), the camera's own numbers on the right, and
 * no timers of its own — `ActionCam.setPageVisible` drives the bridge's poll
 * cadence and the stream (bridges/actioncam_bridge.py). The dock pill after
 * "70mai" reads "GoPro" / "Insta360" and exists only while enabled.
 *
 * An Insta360 in Webcam mode is picture only (`ActionCam.controllable` false):
 * the Record / Photo buttons stay hidden and the card says so.
 *
 * Tapping the picture goes full screen (a window-level overlay, dock and
 * status strip covered); a tap there, leaving the page or a screensaver
 * brings it back. Only one Image asks the provider at a time: the in-page
 * one goes blank while the full-screen one shows.
 */
Item {
    id: root
    objectName: "actionCamPage"

    readonly property bool   on:        ActionCam ? ActionCam.enabled : false
    readonly property string kind:      ActionCam ? ActionCam.kind : "gopro"
    readonly property string label:     ActionCam ? ActionCam.label : "Camera"
    readonly property string state:     ActionCam ? ActionCam.state : "off"
    readonly property bool   online:    state === "online"
    readonly property bool   control:   online && ActionCam && ActionCam.controllable
    readonly property bool   streaming: ActionCam ? ActionCam.streamActive : false
    readonly property string streamState: ActionCam ? ActionCam.streamState : ""
    readonly property bool   picture:   streaming && streamState === "playing"
                                        && ActionCam && ActionCam.frameRev > 0
    readonly property bool   wifiGoPro: ActionCam && ActionCam.link === "wifi" && kind === "gopro"

    property bool fullScreen: false

    onVisibleChanged: {
        if (!visible) fullScreen = false;
        if (ActionCam) ActionCam.setPageVisible(visible);
    }
    Component.onCompleted: if (visible && ActionCam) ActionCam.setPageVisible(true)
    Connections {
        target: Nav
        function onScreensaverChanged() { if (Nav.screensaverActive) root.fullScreen = false; }
    }

    readonly property string titleText: {
        if (!on)                       return label + " is off";
        if (state === "off")           return "Camera not connected";
        if (state === "scanning")      return ActionCam.link === "usb" ? "Looking for the camera on USB…"
                                                                       : "Looking for the camera's Wi-Fi…";
        if (state === "joining")       return "Joining the camera's Wi-Fi…";
        if (state === "error")         return ActionCam.statusText || "Camera error";
        if (!streaming)                return "Live view off";
        if (streamState === "error")   return "No live picture";
        return "Connecting to the stream…";
    }
    readonly property string bodyText: {
        if (!on)                       return "Enable it in Settings › Core › Action cam";
        if (state === "off" || state === "scanning") {
            if (ActionCam.link === "usb")
                return kind === "insta360" ? "Set the camera to Webcam mode and plug it in"
                                           : "USB mode on the camera: GoPro Connect";
            return wifiGoPro ? "The GoPro's Wi-Fi is off until asked — Wi-Fi on sends the Bluetooth nudge"
                             : "Connect joins the camera's hotspot";
        }
        if (state === "error")         return ActionCam.lastError || "";
        if (online && streaming && streamState === "error")
                                       return ActionCam.streamError || "";
        if (online && !streaming)      return ActionCam.recording === 1 && ActionCam.stream === "preview"
                                            ? "No preview while recording on this camera"
                                            : "Press Live view to start it";
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
                provider: "actioncam"
                rev: ActionCam ? ActionCam.frameRev : 0
                active: root.streaming && !root.fullScreen
            }

            // tap the picture → full screen
            MouseArea {
                anchors.fill: parent
                enabled: root.picture
                onClicked: root.fullScreen = true
            }

            Rectangle {
                visible: root.picture && ActionCam.recording === 1
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
                    Text { text: ActionCam ? (ActionCam.recordingText || "REC") : "REC"; color: Theme.dangerSoft
                           font.family: Theme.fontFamily; font.pixelSize: 13; font.weight: Font.Bold
                           anchors.verticalCenter: parent.verticalCenter }
                }
            }

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
                        source: iconsPath + "/Dash/" + (root.kind === "insta360" ? "insta360.png" : "gopro.png")
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

        Text {
            id: statusLine
            anchors.bottom: controls.top
            anchors.bottomMargin: 12
            anchors.left: parent.left
            anchors.right: parent.right
            text: {
                if (!ActionCam) return "";
                var s = ActionCam.statusText || "";
                if (ActionCam.camIp) s += (s ? "  ·  " : "") + ActionCam.camIp;
                if (ActionCam.bleNote) s += (s ? "  ·  " : "") + ActionCam.bleNote;
                if (ActionCam.busy) s += "  ·  …";
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
                onClicked: root.streaming ? ActionCam.stopView() : ActionCam.startView()
            }
            UI.PillButton {
                visible: root.control
                text: ActionCam && ActionCam.recording === 1 ? "Stop" : "Record"
                variant: ActionCam && ActionCam.recording === 1 ? "danger" : "raised"
                enabled: root.control && !ActionCam.busy
                height: 50
                onClicked: ActionCam.record()
            }
            UI.PillButton {
                visible: root.control
                text: "Photo"
                enabled: root.control && !ActionCam.busy && ActionCam.recording !== 1
                height: 50
                onClicked: ActionCam.photo()
            }
            UI.PillButton {
                visible: root.on && ActionCam && ActionCam.kind === "gopro"
                text: "Settings"
                enabled: root.control
                height: 50
                onClicked: camSettings.opened = true
            }
            UI.PillButton {
                text: "Refresh"
                enabled: root.on && !ActionCam.busy
                height: 50
                onClicked: ActionCam.refresh()
            }
            UI.PillButton {
                visible: root.picture
                text: "Full screen"
                variant: "outline"
                height: 50
                onClicked: root.fullScreen = true
            }
            UI.PillButton {
                visible: root.on && !root.online && root.state !== "joining"
                text: "Connect"
                variant: "accent"
                enabled: !ActionCam.busy
                height: 50
                onClicked: ActionCam.connectNow()
            }
            UI.PillButton {
                visible: root.on && root.wifiGoPro && !root.online
                text: "Wi-Fi on"
                variant: "outline"
                enabled: ActionCam && ActionCam.bleMac !== "" && !ActionCam.busy
                height: 50
                onClicked: ActionCam.wakeWifi()
            }
            UI.PillButton {
                visible: root.on && (root.online || root.state === "joining")
                text: "Disconnect"
                variant: "outline"
                height: 50
                onClicked: ActionCam.disconnectNow()
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

                InfoRow { key: "Camera";  value: root.label + (ActionCam && ActionCam.uvcName ? " · " + ActionCam.uvcName : "") }
                InfoRow {
                    key: "Link"
                    value: !ActionCam ? "" : (ActionCam.linkText + (root.online && ActionCam.camIp ? " · " + ActionCam.camIp : ""))
                    valueColor: root.online ? Theme.ok : Theme.ink
                }
                InfoRow {
                    key: "Control"
                    value: !ActionCam || !root.online ? "" : (ActionCam.controllable ? "yes" : "picture only")
                }
                InfoRow { key: "Mode";    value: ActionCam ? ActionCam.mode : "" }
                InfoRow {
                    key: "Recording"
                    value: !ActionCam || !root.online ? ""
                         : ActionCam.recording === 1 ? (ActionCam.recordingText || "yes")
                         : ActionCam.recording === 0 ? "no" : "—"
                    valueColor: ActionCam && ActionCam.recording === 1 ? Theme.danger : Theme.ink
                }

                // battery bar
                Column {
                    width: parent.width
                    spacing: 6
                    InfoRow {
                        key: "Battery"
                        value: ActionCam && ActionCam.battery >= 0 ? ActionCam.battery + " %" : ""
                        valueColor: ActionCam && ActionCam.battery >= 0 && ActionCam.battery <= 15 ? Theme.dangerSoft : Theme.ink
                    }
                    Rectangle {
                        x: 102
                        width: parent.width - 102
                        height: 8
                        radius: 4
                        color: Theme.surfaceRaised
                        visible: ActionCam && ActionCam.battery >= 0
                        Rectangle {
                            width: parent.width * Math.max(0, Math.min(100, ActionCam ? ActionCam.battery : 0)) / 100
                            height: parent.height
                            radius: 4
                            color: ActionCam && ActionCam.battery <= 15 ? Theme.danger
                                 : ActionCam && ActionCam.battery <= 30 ? Theme.warn : Theme.ok
                        }
                    }
                }

                InfoRow {
                    key: "SD card"
                    value: ActionCam ? ActionCam.sdText : ""
                    valueColor: ActionCam && !ActionCam.sdOk ? Theme.dangerSoft : Theme.ink
                }
            }
        }

        UI.Card {
            width: parent.width
            height: Math.max(clipsCol.implicitHeight + padding * 2, 120)

            Column {
                id: clipsCol
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: parent.padding
                spacing: 6

                UI.SectionLabel { label: "Latest on the card"; width: parent.width }

                Text {
                    visible: !ActionCam || ActionCam.clips.length === 0
                    width: parent.width
                    text: root.control ? "Nothing listed yet" : (root.online ? "No file list in this mode" : "Connect to list the camera's files")
                    color: Theme.inkFaint
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSize("widgets_tertiary_text")
                    wrapMode: Text.Wrap
                }

                Repeater {
                    model: ActionCam ? ActionCam.clips : []
                    Rectangle {
                        width: clipsCol.width
                        height: 40
                        radius: Theme.radiusControl
                        color: Theme.surfaceRaised
                        Row {
                            anchors.fill: parent
                            anchors.leftMargin: 12
                            anchors.rightMargin: 12
                            spacing: 10
                            Rectangle { width: 3; height: 20; radius: 1.5; color: Theme.accent
                                        anchors.verticalCenter: parent.verticalCenter }
                            Text {
                                anchors.verticalCenter: parent.verticalCenter
                                text: modelData.name
                                color: Theme.ink
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSize("widgets_secondary_text")
                                font.weight: Font.DemiBold
                            }
                            Text {
                                anchors.verticalCenter: parent.verticalCenter
                                text: (modelData.time ? modelData.time + "  " : "") + modelData.size
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
            provider: "actioncam"
            rev: ActionCam ? ActionCam.frameRev : 0
            active: root.fullScreen && root.streaming
        }
        Rectangle {
            visible: ActionCam && ActionCam.recording === 1
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
                Text { text: ActionCam ? (ActionCam.recordingText || "REC") : "REC"; color: Theme.dangerSoft
                       font.family: Theme.fontFamily; font.pixelSize: 14; font.weight: Font.Bold
                       anchors.verticalCenter: parent.verticalCenter }
            }
        }
        Text {
            anchors.top: parent.top
            anchors.right: parent.right
            anchors.margins: 18
            text: root.label + "  ·  tap to leave"
            color: Theme.inkFaint
            font.family: Theme.fontFamily
            font.pixelSize: 14
        }
        MouseArea { anchors.fill: parent; onClicked: root.fullScreen = false }
    }

    Sheets.ActionCamSettingsSheet {
        id: camSettings
        objectName: "camSettingsSheet"   // `eval @camSettingsSheet opened = true`
        z: 60
        onClosed: opened = false
    }
}
