import QtQuick

/**
 * Top status strip — 52 px, no chrome, sitting directly on the ground colour.
 * Replaces the legacy TeslaTopBar's tinted 60 px bar.
 *
 * The mockup's battery glyph and "410 km" range have no backing bridge
 * property, so the left slot shows the fuel level the app actually has.
 */
Item {
    id: strip
    height: Theme.statusH

    Row {
        anchors.left: parent.left
        anchors.leftMargin: 26
        anchors.verticalCenter: parent.verticalCenter
        spacing: 12

        Image {
            source: iconsPath + "/Dash/petrol.png"
            width: 22; height: 22
            sourceSize.width: 44; sourceSize.height: 44
            fillMode: Image.PreserveAspectFit
            asynchronous: true
            anchors.verticalCenter: parent.verticalCenter
            visible: status === Image.Ready
        }

        Text {
            text: Telemetry.fuelText
            color: Theme.ink
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSize("top_bar_text")
            font.weight: Font.DemiBold
            anchors.verticalCenter: parent.verticalCenter
        }

        // Car battery charge — an estimate from the resting voltage
        // (Telemetry.batterySocText: "78%", "78% ⚡" while the alternator
        // runs, "--%" until measured). Drawn glyph, not the red warning-lamp
        // battery icon, so it never reads as a fault.
        Item { width: 10; height: 1 }
        Item {
            id: battGlyph
            width: 30; height: 14
            anchors.verticalCenter: parent.verticalCenter
            visible: Telemetry.connected
            readonly property real pct: Telemetry.batterySocPercent
            Rectangle {
                x: 0; y: 0; width: 26; height: 14; radius: 3
                color: "transparent"
                border.width: 2
                border.color: Theme.inkDim
                Rectangle {
                    x: 3; y: 3
                    height: 8
                    width: battGlyph.pct >= 0 ? Math.max(1, Math.round(20 * battGlyph.pct / 100)) : 0
                    radius: 1
                    color: Telemetry.batterySocColor
                }
            }
            Rectangle { x: 26; y: 4; width: 3; height: 6; radius: 1; color: Theme.inkDim }
        }
        Text {
            text: Telemetry.batterySocText
            visible: Telemetry.connected
            color: Theme.ink
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSize("top_bar_text")
            font.weight: Font.DemiBold
            anchors.verticalCenter: parent.verticalCenter
        }
    }

    Text {
        id: clock
        anchors.centerIn: parent
        color: Theme.ink
        font.family: Theme.fontFamily
        font.pixelSize: 19
        font.weight: Font.DemiBold
    }

    Text {
        id: dateLabel
        anchors.right: parent.right
        anchors.rightMargin: 26
        anchors.verticalCenter: parent.verticalCenter
        color: Theme.inkDim
        font.family: Theme.fontFamily
        font.pixelSize: 15
        // Steps aside for the update chip so the two never overlap.
        visible: !(Settings && Settings.piUpdateRunning) && !(TripLog && TripLog.recording)
    }

    // Trip logging in progress, from any page (Settings → Debugger → Trip
    // logging). A quiet REC pill so a silent recording is never forgotten.
    Rectangle {
        id: recChip
        anchors.right: (Settings && Settings.piUpdateRunning) ? updateChip.left : parent.right
        anchors.rightMargin: (Settings && Settings.piUpdateRunning) ? 10 : 26
        anchors.verticalCenter: parent.verticalCenter
        visible: TripLog ? TripLog.recording : false
        width: recRow.implicitWidth + 22
        height: 30
        radius: 15
        color: Theme.dangerBg
        Row {
            id: recRow
            anchors.centerIn: parent
            spacing: 8
            Rectangle {
                width: 9; height: 9; radius: 4.5
                anchors.verticalCenter: parent.verticalCenter
                color: Theme.danger
                SequentialAnimation on opacity {
                    running: recChip.visible
                    loops: Animation.Infinite
                    NumberAnimation { to: 0.3; duration: 600; easing.type: Easing.InOutSine }
                    NumberAnimation { to: 1.0; duration: 600; easing.type: Easing.InOutSine }
                }
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "REC"
                color: Theme.dangerSoft
                font.family: Theme.fontFamily
                font.pixelSize: 13
                font.weight: Font.Bold
            }
        }
    }

    // Pi update in progress, from any page: a small pill with the percentage
    // and a thin bar. State lives in the bridge, so it shows on every page.
    Rectangle {
        id: updateChip
        anchors.right: parent.right
        anchors.rightMargin: 26
        anchors.verticalCenter: parent.verticalCenter
        visible: Settings ? Settings.piUpdateRunning : false
        width: updRow.implicitWidth + 24
        height: 30
        radius: 15
        color: Theme.surfaceRaised

        Row {
            id: updRow
            anchors.centerIn: parent
            spacing: 9

            Rectangle {   // spinner-free activity dot
                width: 9; height: 9; radius: 4.5
                anchors.verticalCenter: parent.verticalCenter
                color: Theme.ok
                SequentialAnimation on opacity {
                    running: updateChip.visible
                    loops: Animation.Infinite
                    NumberAnimation { to: 0.3; duration: 650; easing.type: Easing.InOutSine }
                    NumberAnimation { to: 1.0; duration: 650; easing.type: Easing.InOutSine }
                }
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "Updating " + (Settings ? Math.round(Settings.piUpdateProgress * 100) : 0) + " %"
                color: Theme.ink
                font.family: Theme.fontFamily
                font.pixelSize: 13
                font.weight: Font.DemiBold
            }
            Rectangle {
                anchors.verticalCenter: parent.verticalCenter
                width: 46; height: 5; radius: 2.5
                color: Theme.surface
                Rectangle {
                    width: parent.width * Math.max(0, Math.min(1, Settings ? Settings.piUpdateProgress : 0))
                    height: parent.height; radius: 2.5
                    color: Theme.ok
                    Behavior on width { NumberAnimation { duration: 220 } }
                }
            }
        }
    }

    Timer {
        interval: 1000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: {
            var d = new Date();
            clock.text = Qt.formatTime(d, "HH:mm");
            dateLabel.text = Qt.formatDate(d, "dddd d MMMM");
        }
    }
}
