import QtQuick
import QtQuick.Layouts
import "../components" as UI

/**
 * OBD dashboard — a pure readout, no interactive controls (same as legacy).
 *
 * Layout:
 *   top     two hero cards — RPM · Speed
 *   middle  Throttle·Load | Boost·Timing | ALERTS | car rear | LIGHTS | Gear·Intake | Lambda·Pedal
 *   bottom  four tiles across — Fuel · Oil temp · Coolant · Battery
 *
 * The middle band has two shapes. Full width, the eight readouts sit in four
 * short columns of two and the alerts/lights boxes are three tiles tall,
 * framing the car. With the side panel open (the driving state) the page
 * loses 40% of its width, and four columns left the two boxes at 118 px with
 * three-line alerts and a 2x2 light grid while half the band above and below
 * them was empty. So the readouts then stack four high in two columns and the
 * boxes take that full height, which hands the freed width to the boxes and
 * the car. Both boxes are always drawn, even empty: a dash-light card that
 * only appears when something is wrong made the whole band re-flow — and the
 * car jump sideways — every time a light cleared.
 *
 * Tapping the car toggles the driving screensaver: this is the one page with a
 * manual override, because it is the page you would rather be looking at. See
 * Nav.toggleDrivingScreensaver().
 *
 * Everything binds straight to TelemetryBridge; there is no timer or animation
 * on this page beyond the turn-off pill's 260 ms fade, so it costs nothing
 * while hidden.
 */
Item {
    id: page

    // Legacy shrank its text by 0.88 with the side panel open. The shell also
    // narrows the page stack, so the columns get a narrower track as well.
    readonly property real textScale: (Nav && Nav.sidePanelOpen) ? 0.88 : 1.0
    readonly property int  heroSize:  Math.round(74 * textScale)
    readonly property int  midSize:   Math.round(38 * textScale)
    readonly property int  tileSize:  Math.round(40 * textScale)
    readonly property int  heroRowH:  Math.round(106 * textScale)
    readonly property int  rowH:      Math.round(92 * textScale)
    readonly property int  tileGap:   10

    // The band runs left to right as tiles | alerts | car | lights | tiles.
    //
    // Heights are computed from the *page*, deliberately not from
    // centreBand.height. The band is a RowLayout whose implicit height comes
    // from its children's Layout.preferredHeight, so sizing children from it is
    // a binding loop; Qt breaks it silently and leaves a stale value, which is
    // what pinned the two boxes to roughly one cell instead of two.
    readonly property int  bandH:     Math.max(200, page.height - heroRowH - rowH - 40)
    readonly property int  bandW:     Math.max(560, page.width - 2 * Theme.pad)
    readonly property int  bandGap:   bandW > 1000 ? 12 : 8
    // Narrow band (side panel open): readouts stack four high in two columns.
    readonly property bool stacked:   bandW < 900
    // Tiles keep their own height rather than stretching to fill the band; the
    // slack above and below is what stops the centre of the page reading as
    // one solid wall of boxes.
    readonly property int  cellH:     rowH
    readonly property int  tilesPerCol: stacked ? 4 : 2
    // Readout column width. Proportional so the car keeps a usable share at
    // every width; stacked columns are a little wider since there are only two.
    readonly property int  tileColW:  stacked
                                      ? Math.max(96, Math.min(118, Math.round(bandW * 0.145)))
                                      : Math.max(84, Math.min(126, Math.round(bandW * 0.098)))
    // The alerts and lights boxes. Wider than a readout column — they hold
    // wrapped alert lines and a grid of light icons — and taller than the
    // readout stack when there are only two tiles of it: three cells, so the
    // wide layout shows five alerts / nine lights instead of three / six.
    // Stacked, they run the full four-tile height.
    readonly property int  boxW:      stacked
                                      ? Math.max(140, Math.min(200, Math.round(bandW * 0.21)))
                                      : Math.max(118, Math.min(196, Math.round(bandW * 0.158)))
    readonly property int  boxCells:  Math.min(stacked ? 4 : 3,
                                               Math.max(2, Math.floor((bandH - 16 + tileGap) / (cellH + tileGap))))
    readonly property int  boxH:      cellH * boxCells + tileGap * (boxCells - 1)

    /** Alert severity -> colour. Same mapping HomePage, SidePanel and the
        screensavers use; they all render the same Alerts.model, so a warning
        must not read as neutral here. */
    function _priorityColor(p) {
        switch (String(p || "info")) {
        case "critical":
        case "danger":  return Theme.danger;
        case "warning":
        case "warn":    return Theme.warn;
        case "success": return Theme.ok;
        default:        return Theme.accent;
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.leftMargin: Theme.pad
        anchors.rightMargin: Theme.pad
        anchors.topMargin: 6
        anchors.bottomMargin: 0
        spacing: 14

        // ── hero row: RPM · Speed ───────────────────────────────────────
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: false
            Layout.preferredHeight: page.heroRowH
            Layout.minimumHeight: page.heroRowH
            Layout.maximumHeight: page.heroRowH
            spacing: 14

            UI.StatTile {
                Layout.fillWidth: true
                Layout.fillHeight: true
                hero: true
                label: "RPM"
                value: Telemetry ? Telemetry.rpm.toFixed(0) : "0"
                unit: "rpm"
                valueSize: page.heroSize
                valueColor: Telemetry && Telemetry.rpmValueTextColor ? Telemetry.rpmValueTextColor : Theme.ink
            }

            UI.StatTile {
                Layout.fillWidth: true
                Layout.fillHeight: true
                hero: true
                label: "Speed"
                value: Telemetry ? Telemetry.speed.toFixed(0) : "0"
                unit: "km/h"
                valueSize: page.heroSize
                valueColor: Telemetry ? Telemetry.speedColor : Theme.ink
            }
        }

        // ── centre band ─────────────────────────────────────────────────
        // Five columns: readouts · alerts · car · lights · readouts. The two
        // inner boxes are taller than a tile (boxCells of them) so they read
        // as one pair framing the car rather than as two more tiles, and they
        // are always present — an alerts box that vanishes when all is well
        // makes the whole band jump every time a warning clears.
        RowLayout {
            id: centreBand
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: page.bandGap

            // left readouts — Throttle · Load · Boost · Timing
            // One Grid for both shapes: filled top-to-bottom, so with two rows
            // it is the two short columns (first pair left, second pair right)
            // and with four rows a single stack.
            Item {
                Layout.preferredWidth: page.stacked ? page.tileColW : page.tileColW * 2 + page.bandGap
                Layout.minimumWidth: Layout.preferredWidth
                Layout.fillHeight: true

                Grid {
                    id: leftTiles
                    anchors.left: parent.left
                    anchors.verticalCenter: parent.verticalCenter
                    flow: Grid.TopToBottom
                    rows: page.tilesPerCol
                    rowSpacing: page.tileGap
                    columnSpacing: page.bandGap

                    UI.StatTile {
                        width: page.tileColW
                        height: page.cellH
                        label: "Throttle"
                        value: Telemetry ? Telemetry.throttle.toFixed(0) : "0"
                        unit: "%"
                        valueSize: page.midSize
                        iconSource: iconsPath + "/Telemetry/throttle.png"
                        valueColor: Telemetry ? Telemetry.throttleColor : Theme.ink
                    }

                    UI.StatTile {
                        width: page.tileColW
                        height: page.cellH
                        label: "Load"
                        value: Telemetry ? Telemetry.engineLoad.toFixed(0) : "0"
                        unit: "%"
                        valueSize: page.midSize
                        iconSource: iconsPath + "/Telemetry/load.png"
                        valueColor: Theme.ink
                    }

                    UI.StatTile {
                        width: page.tileColW
                        height: page.cellH
                        label: "Boost"
                        value: Telemetry ? Telemetry.manifoldPressureText : "N/A"
                        unit: "kPa"
                        valueSize: page.midSize
                        iconSource: iconsPath + "/Telemetry/map.png"
                        valueColor: Telemetry ? Telemetry.manifoldPressureColor : Theme.ink
                    }

                    UI.StatTile {
                        width: page.tileColW
                        height: page.cellH
                        label: "Timing"
                        value: Telemetry ? Telemetry.timingAdvanceText : "N/A"
                        unit: "\u00b0"
                        valueSize: page.midSize
                        iconSource: iconsPath + "/Telemetry/timing.png"
                        valueColor: Telemetry ? Telemetry.timingAdvanceColor : Theme.ink
                    }
                }
            }

            // ── alerts box — left of the car ────────────────────────────
            UI.Card {
                id: alertsBox
                Layout.preferredWidth: page.boxW
                Layout.minimumWidth: page.boxW
                // boxH is derived from rowH and the page height, never from
                // the layout — no path back to it, so this preferred height is
                // safe. (Deriving it from centreBand.height was the binding
                // loop that pinned both boxes to a stale value.)
                Layout.preferredHeight: page.boxH
                Layout.alignment: Qt.AlignVCenter

                Text {
                    id: alertsBoxLabel
                    anchors.top: parent.top
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.margins: 12
                    text: "ALERTS"
                    color: Theme.inkFaint
                    font.family: Theme.fontFamily
                    font.pixelSize: 12
                    font.weight: Font.Bold
                    font.letterSpacing: 1.2
                }

                Text {
                    anchors.centerIn: parent
                    visible: !(Alerts && Alerts.model && Alerts.model.count > 0)
                    text: "All clear"
                    color: Theme.inkGhost
                    font.family: Theme.fontFamily
                    font.pixelSize: 14
                }

                ListView {
                    anchors.top: alertsBoxLabel.bottom
                    anchors.topMargin: 8
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    anchors.leftMargin: 12
                    anchors.rightMargin: 12
                    anchors.bottomMargin: 12
                    clip: true
                    spacing: 6
                    model: Alerts ? Alerts.model : null

                    // Severity stripe, the alert's own Dash icon, then the
                    // text — the same three parts HomePage and the screensaver
                    // show, so one warning does not look like three different
                    // things depending on where you read it.
                    delegate: Row {
                        width: ListView.view.width
                        spacing: 6

                        readonly property color tone: page._priorityColor(model.priority)

                        Rectangle {
                            width: 3
                            height: Math.max(alertLine.implicitHeight + 4, 18)
                            radius: 1.5
                            color: parent.tone
                        }

                        Image {
                            id: alertRowIcon
                            width: 18
                            height: 18
                            sourceSize.width: 36
                            sourceSize.height: 36
                            fillMode: Image.PreserveAspectFit
                            asynchronous: true
                            cache: true
                            anchors.verticalCenter: parent.verticalCenter
                            visible: status === Image.Ready
                            source: model.icon
                                    ? ("file://" + iconsPath + "/Dash/" + model.icon)
                                    : ""
                        }

                        Text {
                            id: alertLine
                            width: parent.width - 9 - (alertRowIcon.visible ? 24 : 0)
                            text: model.text !== undefined ? model.text : ""
                            color: parent.tone
                            font.family: Theme.fontFamily
                            font.pixelSize: 14
                            font.weight: Font.DemiBold
                            wrapMode: Text.WordWrap
                            maximumLineCount: 2
                            elide: Text.ElideRight
                        }
                    }
                }
            }

            // centre — the car, and the turn-off advice under it
            Item {
                id: centre
                Layout.fillWidth: true
                Layout.fillHeight: true


                Column {
                    anchors.centerIn: parent
                    spacing: 12

                    Image {
                        id: carImage
                        anchors.horizontalCenter: parent.horizontalCenter
                        // back.png is a rear three-quarter view — taller than it
                        // is wide — so PreserveAspectFit sizes it off the height,
                        // and the old 0.68-of-the-band cap was what kept it
                        // small however much width the band gave it. It now
                        // takes the band's full height less the turn-off pill.
                        width: Math.min(centre.width - 8, 560)
                        height: Math.max(120, centre.height - (turnoff.visible ? 108 : 16))
                        fillMode: Image.PreserveAspectFit
                        smooth: true
                        asynchronous: true
                        cache: true
                        sourceSize.width: 620
                        sourceSize.height: 460
                        source: {
                            if (!Modding || Modding.preset === "Quiet")
                                return "file://" + iconsPath + "/back.png";
                            var p = Modding.carBackPath || (iconsPath + "/back.png");
                            return String(p).startsWith("file://") ? p : "file://" + p;
                        }
                        onStatusChanged: {
                            if (status === Image.Error)
                                source = "file://" + iconsPath + "/back.png";
                        }

                        // The driving screensaver's manual switch, and the only
                        // page that has one: tapping the car raises it, tapping
                        // it again puts it away and pins it off here
                        // (Nav.drivingSuppressed). Every other page still
                        // follows the idle timer.
                        //
                        // Scoped to the car, not to the whole centre column —
                        // the column also holds the turn-off pill and a lot of
                        // empty space, and a tap anywhere in it was toggling
                        // the screensaver by accident.
                        MouseArea {
                            anchors.fill: parent
                            enabled: Telemetry && Telemetry.connected
                            onClicked: if (Nav) Nav.toggleDrivingScreensaver()
                        }
                    }

                    // turn-off advice — shown while stopped, in neutral
                    Column {
                        id: turnoff
                        anchors.horizontalCenter: parent.horizontalCenter
                        spacing: 6

                        readonly property bool shown: Nav && Nav.engineOn
                                                      && Telemetry.speed < 5
                                                      && Telemetry.gear === 0
                        readonly property bool safe: Telemetry ? Telemetry.turnoffSafe : false
                        readonly property color tone: safe ? Theme.ok : Theme.warn

                        opacity: shown ? 1.0 : 0.0
                        visible: opacity > 0
                        Behavior on opacity { NumberAnimation { duration: 260; easing.type: Easing.OutCubic } }

                        Rectangle {
                            anchors.horizontalCenter: parent.horizontalCenter
                            width: pillRow.implicitWidth + 52
                            height: 48
                            radius: 24
                            color: turnoff.safe ? Theme.okBg : Theme.surface

                            Row {
                                id: pillRow
                                anchors.centerIn: parent
                                spacing: 10

                                Rectangle {
                                    width: 8; height: 8; radius: 4
                                    color: turnoff.tone
                                    anchors.verticalCenter: parent.verticalCenter
                                }

                                Text {
                                    text: turnoff.safe ? "Safe to turn off" : "Wait before turning off"
                                    color: turnoff.tone
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 16
                                    font.weight: Font.DemiBold
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                            }
                        }

                        Text {
                            anchors.horizontalCenter: parent.horizontalCenter
                            width: Math.min(implicitWidth, centre.width - 16)
                            visible: text !== ""
                            text: Telemetry ? Telemetry.turnoffReason : ""
                            color: Theme.inkFaint
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSize("widgets_secondary_text")
                            horizontalAlignment: Text.AlignHCenter
                            elide: Text.ElideRight
                        }
                    }
                }
            }

            // ── lights box — right of the car ───────────────────────────
            UI.Card {
                id: lightsBox
                Layout.preferredWidth: page.boxW
                Layout.minimumWidth: page.boxW
                // Same height rule as the alerts box, see there.
                Layout.preferredHeight: page.boxH
                Layout.alignment: Qt.AlignVCenter

                readonly property int lightCount: Telemetry ? Telemetry.activeDashLightKeys.length : 0
                // Two columns of big icons when the box is tall (stacked
                // layout), three when it is wide and short: the icons are
                // what gets read while driving, so trade columns for size
                // whenever the height allows.
                readonly property int cols: page.stacked ? 2 : (width >= 140 ? 3 : 2)
                readonly property int iconSize: Math.max(22, Math.min(56,
                    Math.floor((width - 24 - (cols - 1) * 8) / cols)))

                Text {
                    id: lightsBoxLabel
                    anchors.top: parent.top
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.margins: 12
                    text: "LIGHTS"
                    color: Theme.inkFaint
                    font.family: Theme.fontFamily
                    font.pixelSize: 12
                    font.weight: Font.Bold
                    font.letterSpacing: 1.2
                }

                Text {
                    anchors.centerIn: parent
                    visible: lightsBox.lightCount === 0
                    text: "None"
                    color: Theme.inkGhost
                    font.family: Theme.fontFamily
                    font.pixelSize: 14
                }

                Flickable {
                    anchors.top: lightsBoxLabel.bottom
                    anchors.topMargin: 8
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    anchors.leftMargin: 12
                    anchors.rightMargin: 12
                    anchors.bottomMargin: 12
                    clip: true
                    contentHeight: lightsGrid.implicitHeight
                    interactive: contentHeight > height

                    Grid {
                        id: lightsGrid
                        width: parent.width
                        columns: lightsBox.cols
                        spacing: 8

                        Repeater {
                            model: Telemetry ? Telemetry.activeDashLightKeys : []

                            Image {
                                width: lightsBox.iconSize
                                height: lightsBox.iconSize
                                sourceSize.width: lightsBox.iconSize * 2
                                sourceSize.height: lightsBox.iconSize * 2
                                fillMode: Image.PreserveAspectFit
                                asynchronous: true
                                cache: true
                                source: iconsPath + "/Dash/" + modelData + ".png"
                                onStatusChanged: if (status === Image.Error) visible = false
                            }
                        }
                    }
                }
            }

            // right readouts — Gear · Intake · Lambda · Pedal
            // One Grid for both shapes: filled top-to-bottom, so with two rows
            // it is the two short columns (first pair left, second pair right)
            // and with four rows a single stack.
            Item {
                Layout.preferredWidth: page.stacked ? page.tileColW : page.tileColW * 2 + page.bandGap
                Layout.minimumWidth: Layout.preferredWidth
                Layout.fillHeight: true

                Grid {
                    id: rightTiles
                    anchors.left: parent.left
                    anchors.verticalCenter: parent.verticalCenter
                    flow: Grid.TopToBottom
                    rows: page.tilesPerCol
                    rowSpacing: page.tileGap
                    columnSpacing: page.bandGap

                    UI.StatTile {
                        width: page.tileColW
                        height: page.cellH
                        label: "Gear"
                        value: (Telemetry && Telemetry.gear > 0) ? Telemetry.gear.toString() : "N"
                        valueSize: Math.round(42 * page.textScale)
                        iconSource: iconsPath + "/Telemetry/gear.png"
                        valueColor: Telemetry ? Telemetry.gearColor : Theme.accent
                    }

                    UI.StatTile {
                        width: page.tileColW
                        height: page.cellH
                        label: "Intake"
                        value: Telemetry ? Telemetry.intakeTemp.toFixed(0) : "0"
                        unit: "\u00b0C"
                        valueSize: page.midSize
                        iconSource: iconsPath + "/Telemetry/iat.png"
                        valueColor: Theme.ink
                    }

                    UI.StatTile {
                        width: page.tileColW
                        height: page.cellH
                        label: "Lambda"
                        value: Telemetry ? Telemetry.equivRatioText : "N/A"
                        unit: "\u03bb"
                        valueSize: page.midSize
                        iconSource: iconsPath + "/Telemetry/lambda.png"
                        valueColor: Telemetry ? Telemetry.equivRatioColor : Theme.ink
                    }

                    UI.StatTile {
                        width: page.tileColW
                        height: page.cellH
                        label: "Pedal"
                        value: Telemetry ? Telemetry.accelPedalText : "N/A"
                        unit: "%"
                        valueSize: page.midSize
                        iconSource: iconsPath + "/Telemetry/pedal.png"
                        valueColor: Telemetry ? Telemetry.accelPedalColor : Theme.ink
                    }
                }
            }
        }

        // ── bottom row: Fuel · Oil temp · Coolant · Battery ─────────────
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: false
            Layout.preferredHeight: page.rowH
            Layout.minimumHeight: page.rowH
            Layout.maximumHeight: page.rowH
            Layout.bottomMargin: 6
            spacing: 14

            UI.StatTile {
                Layout.fillWidth: true
                Layout.fillHeight: true
                label: "Fuel"
                value: Telemetry ? Telemetry.fuelText : "--"
                valueSize: page.tileSize
                iconSource: iconsPath + "/Telemetry/fuel.png"
                valueColor: Telemetry && Telemetry.fuelValueTextColor ? Telemetry.fuelValueTextColor : Theme.ink
            }

            UI.StatTile {
                Layout.fillWidth: true
                Layout.fillHeight: true
                label: "Oil Temp"
                value: (Telemetry && Telemetry.oilTemp > 0) ? Telemetry.oilTemp.toFixed(0) : "N/A"
                unit: "°C"
                valueSize: page.tileSize
                iconSource: iconsPath + "/Telemetry/oil_temp.png"
                valueColor: Telemetry && Telemetry.oilTempValueTextColor ? Telemetry.oilTempValueTextColor : Theme.ink
            }

            UI.StatTile {
                Layout.fillWidth: true
                Layout.fillHeight: true
                label: "Coolant"
                value: Telemetry ? Telemetry.coolantTemp.toFixed(0) : "0"
                unit: "°C"
                valueSize: page.tileSize
                iconSource: iconsPath + "/Telemetry/coolant.png"
                valueColor: Telemetry && Telemetry.coolantValueTextColor ? Telemetry.coolantValueTextColor : Theme.ink
            }

            UI.StatTile {
                Layout.fillWidth: true
                Layout.fillHeight: true
                label: "Battery"
                value: Telemetry ? Telemetry.voltage.toFixed(1) : "0.0"
                unit: "V"
                valueSize: page.tileSize
                iconSource: iconsPath + "/Telemetry/voltage.png"
                valueColor: Telemetry ? Telemetry.voltageColor : Theme.ink
            }
        }
    }
}
