import QtQuick
import QtQuick.Layouts
import "components" as UI

/**
 * Right-edge panel — the mockup has no equivalent, but it is the only way to
 * reach the sound favourites and the OBD log while driving, so it is kept and
 * restyled as a flat sheet.
 *
 * Four swipeable mini pages (Music · Navigate · Sound Favourites · OBD Log),
 * the car top-down render, and the alert list. Opens by dragging the right edge
 * or by Nav.toggleSidePanel(); state lives in NavigationBridge.
 */
Item {
    id: root

    anchors.right: parent.right
    width: parent.width * Nav.sidePanelRatio
    clip: true

    readonly property int miniPageCount: 4

    /** Alert severity -> colour, the same mapping the Home page uses. The two
        lists show the same `Alerts.model`, so a warning must not read as
        neutral here just because the panel is narrow. */
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
    property real panelX: Nav.sidePanelOpen ? 0 : width
    readonly property real visibleWidth: Math.max(0, width - panelX)

    // ── mini-page card geometry ─────────────────────────────────────────
    // The card is a FIXED height, derived from its tallest page, so nothing
    // can spill out of it and the car render below never shifts when you
    // swipe. Sound Favourites is the tallest: a section label over five rows.
    //
    // A layout only honours a child's `height` when that child's implicitHeight
    // is 0 — a PillButton's is 44, so the old `height: 28` was silently ignored
    // and five 44 px buttons (271 px total) overflowed a 157 px card. Every row
    // below therefore states Layout.preferredHeight, which layouts do honour.
    readonly property int miniLabelH:   22   // trimmed SectionLabel row
    readonly property int miniRowGap:    6
    readonly property int miniPadTop:   14
    readonly property int miniPadBot:   20   // reserved for the indicator dots
    /** Favourites are square-ish tiles on a 2-column grid: 3 rows, not 5. */
    readonly property int favTileH:     42
    readonly property int favRows:       3
    /** OBD log still needs five lines, so they are the compact row height. */
    readonly property int obdRowH:      22
    // The card is sized by whichever page is genuinely tallest — assuming it is
    // favourites is how the last overflow happened.
    readonly property int favContentH:  miniLabelH + favRows * favTileH + favRows * miniRowGap
    readonly property int obdContentH:  miniLabelH + 5 * obdRowH + 5 * miniRowGap
    readonly property int miniContentH: Math.max(favContentH, obdContentH)
    readonly property int miniCardH:    miniPadTop + miniContentH + miniPadBot

    Behavior on panelX {
        enabled: Nav.sidePanelAnimationEnabled()
        NumberAnimation {
            duration: Nav.sidePanelOpen ? 280 : 220
            easing.type: Nav.sidePanelOpen ? Easing.OutCubic : Easing.InCubic
        }
    }

    Rectangle {
        id: panel
        x: root.panelX
        width: root.width
        height: root.height
        color: Theme.surface

        // ── drag handle ─────────────────────────────────────────────────
        Rectangle {
            id: handle
            width: 5
            height: Math.max(120, panel.height * 0.35)
            radius: 3
            color: Theme.outline
            anchors.left: parent.left
            anchors.leftMargin: 7
            anchors.verticalCenter: parent.verticalCenter

            MouseArea {
                anchors.fill: parent
                anchors.margins: -10
                property real pressX: 0
                onPressed: function (mouse) { pressX = mouse.x; }
                onReleased: function (mouse) {
                    var dx = mouse.x - pressX;
                    if (dx > 6)
                        Nav.setSidePanelOpen(false);
                    else if (dx < -6 && Nav.engineOn)
                        Nav.setSidePanelOpen(true);
                    else if (Nav.sidePanelOpen || Nav.engineOn)
                        Nav.toggleSidePanel();
                }
            }
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.leftMargin: 24
            anchors.rightMargin: 14
            anchors.topMargin: 14
            anchors.bottomMargin: 14
            spacing: 10

            // ── mini page stack ─────────────────────────────────────────
            Rectangle {
                id: miniCard
                Layout.fillWidth: true
                Layout.preferredHeight: root.miniCardH
                radius: Theme.radiusCard
                color: Theme.surfaceRaised

                StackLayout {
                    id: miniStack
                    anchors.fill: parent
                    anchors.margins: root.miniPadTop
                    anchors.bottomMargin: root.miniPadBot
                    currentIndex: Nav.sidePanelMiniPage

                    // 0 — Music
                    ColumnLayout {
                        id: musicPage
                        spacing: root.miniRowGap

                        Text {
                            Layout.fillWidth: true
                            text: Music.title || "No track"
                            color: Theme.ink
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSize("top_bar_text")
                            font.weight: Font.Bold
                            elide: Text.ElideRight
                        }
                        Text {
                            Layout.fillWidth: true
                            visible: (Music.artist || "") !== ""
                            text: Music.artist || ""
                            color: Theme.inkDim
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSize("widgets_tertiary_text")
                            elide: Text.ElideRight
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 5
                            Layout.topMargin: 2
                            radius: 3
                            color: Theme.surfaceHover
                            Rectangle {
                                width: parent.width * Math.max(0, Math.min(1, Number(Music.progress) || 0))
                                height: parent.height
                                radius: parent.radius
                                color: Theme.accent
                            }
                        }

                        Item { Layout.fillHeight: true }

                        RowLayout {
                            id: musicControlsRow
                            Layout.alignment: Qt.AlignHCenter
                            spacing: 10

                            UI.IconButton {
                                Layout.preferredWidth: 74
                                Layout.preferredHeight: 56
                                radius: 16
                                glyph: "⏮"; glyphSize: 22
                                onClicked: Music.prev()
                            }
                            UI.IconButton {
                                Layout.preferredWidth: 74
                                Layout.preferredHeight: 56
                                radius: 16
                                checked: true
                                glyph: Music.playing ? "⏸" : "▶"; glyphSize: 22
                                onClicked: Music.togglePlayPause()
                            }
                            UI.IconButton {
                                Layout.preferredWidth: 74
                                Layout.preferredHeight: 56
                                radius: 16
                                glyph: "⏭"; glyphSize: 22
                                onClicked: Music.next()
                            }
                        }

                        Item { Layout.fillHeight: true }
                    }

                    // 1 — Navigate
                    ColumnLayout {
                        id: navPage
                        spacing: root.miniRowGap

                        UI.SectionLabel {
                            label: "Navigate"
                            Layout.preferredHeight: root.miniLabelH
                        }

                        Item { Layout.fillHeight: true }

                        // One pill per destination, sharing the width evenly.
                        // "outline" not "raised": a raised fill is the same
                        // colour as this card, which made them invisible.
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            Repeater {
                                model: [
                                    { label: "Home",      page: 0 },
                                    { label: "Dashboard", page: 1 },
                                    { label: "Music",     page: 4 }
                                ]
                                UI.PillButton {
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 48
                                    variant: "outline"
                                    text: modelData.label
                                    fontSize: Theme.fontSize("buttons_medium_text")
                                    onClicked: Nav.navigateTo(modelData.page)
                                }
                            }
                        }

                        Item { Layout.fillHeight: true }
                    }

                    // 2 — Sound favourites
                    // label 22 + 5 x 32 + 5 x 6 = 212 = the stack's height.
                    ColumnLayout {
                        id: favPage
                        spacing: root.miniRowGap

                        UI.SectionLabel {
                            label: "Sound Favorites"
                            Layout.preferredHeight: root.miniLabelH
                        }

                        // Five squares on a two-column grid — rows of 2, 2, and
                        // a centred last one. Three rows instead of five is
                        // what lets the whole card get shorter.
                        GridLayout {
                            Layout.fillWidth: true
                            columns: 2
                            columnSpacing: root.miniRowGap
                            rowSpacing: root.miniRowGap

                            Repeater {
                                model: 5
                                UI.PillButton {
                                    readonly property bool isLast: index === 4
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: root.favTileH
                                    // The odd one out spans both columns so it
                                    // sits centred rather than orphaned left.
                                    Layout.columnSpan: isLast ? 2 : 1
                                    radius: 12
                                    variant: "outline"
                                    fontSize: Theme.fontSize("widgets_tertiary_text")
                                    text: (Soundboard.favorites && Soundboard.favorites[index]
                                           && Soundboard.favorites[index].name)
                                          ? Soundboard.favorites[index].name
                                          : "Slot " + (index + 1)
                                    onClicked: Soundboard.playFavorite(index)
                                }
                            }
                        }
                    }

                    // 3 — OBD log
                    ColumnLayout {
                        id: obdPage
                        spacing: root.miniRowGap

                        UI.SectionLabel {
                            label: "OBD Log"
                            Layout.preferredHeight: root.miniLabelH
                        }

                        Repeater {
                            model: [
                                { label: "Status",  value: Telemetry.obdStatus },
                                { label: "RPM",     value: Telemetry.rpm.toFixed(0) },
                                { label: "Speed",   value: Telemetry.speed.toFixed(0) + " km/h" },
                                { label: "Coolant", value: Telemetry.coolantTemp.toFixed(0) + "°C" },
                                { label: "Voltage", value: Telemetry.voltage.toFixed(1) + "V" }
                            ]
                            RowLayout {
                                Layout.fillWidth: true
                                Layout.preferredHeight: root.obdRowH
                                spacing: 8
                                Text {
                                    text: modelData.label
                                    color: Theme.inkFaint
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSize("widgets_tertiary_text")
                                    Layout.preferredWidth: 68
                                    Layout.alignment: Qt.AlignVCenter
                                }
                                Text {
                                    text: modelData.value
                                    color: Theme.ink
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSize("widgets_tertiary_text")
                                    font.weight: Font.DemiBold
                                    Layout.fillWidth: true
                                    Layout.alignment: Qt.AlignVCenter
                                    elide: Text.ElideRight
                                }
                            }
                        }
                    }
                }

                // page swipe — passes taps through to the transport row
                MouseArea {
                    anchors.fill: parent
                    propagateComposedEvents: true
                    property real pressX: 0
                    z: -1
                    onPressed: function (mouse) { pressX = mouse.x; }
                    onReleased: function (mouse) {
                        var dx = mouse.x - pressX;
                        if (dx < -40)
                            Nav.setSidePanelMiniPage((Nav.sidePanelMiniPage + 1) % root.miniPageCount);
                        else if (dx > 40)
                            Nav.setSidePanelMiniPage((Nav.sidePanelMiniPage - 1 + root.miniPageCount) % root.miniPageCount);
                    }
                }

                Row {
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.bottom: parent.bottom
                    anchors.bottomMargin: 6
                    spacing: 6
                    Repeater {
                        model: root.miniPageCount
                        Rectangle {
                            width: 6; height: 6; radius: 3
                            color: Nav.sidePanelMiniPage === index ? Theme.accent : Theme.outline
                        }
                    }
                }
            }

            // ── car, top-down ───────────────────────────────────────────
            Image {
                Layout.fillWidth: true
                Layout.preferredHeight: 180
                Layout.minimumHeight: 150
                Layout.maximumHeight: 200
                fillMode: Image.PreserveAspectFit
                asynchronous: true
                sourceSize.width: 420
                sourceSize.height: 400
                source: (Modding.preset !== "Quiet" && Modding.carTopPath)
                        ? "file://" + Modding.carTopPath
                        : iconsPath + "/top.png"
                onStatusChanged: if (status === Image.Error) source = iconsPath + "/top.png"
            }

            // ── alerts ──────────────────────────────────────────────────
            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 6
                visible: Alerts.model.count > 0

                Repeater {
                    model: Alerts.model.count > 0 ? Alerts.model : null
                    Rectangle {
                        id: panelAlert
                        property color tone: root._priorityColor(model.priority)
                        Layout.fillWidth: true
                        Layout.preferredHeight: 46
                        visible: index < 3
                        radius: Theme.radiusControl
                        color: Theme.surfaceRaised

                        // Severity stripe, as on Home — narrower, because the
                        // panel is a third of the width.
                        Rectangle {
                            anchors.left: parent.left
                            anchors.leftMargin: 8
                            anchors.verticalCenter: parent.verticalCenter
                            width: 3
                            height: parent.height - 18
                            radius: 2
                            color: panelAlert.tone
                        }

                        Row {
                            anchors.left: parent.left
                            anchors.leftMargin: 19
                            anchors.right: parent.right
                            anchors.rightMargin: 10
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: 8

                            Image {
                                id: panelAlertIcon
                                width: 22
                                height: 22
                                sourceSize.width: 44
                                sourceSize.height: 44
                                fillMode: Image.PreserveAspectFit
                                asynchronous: true
                                anchors.verticalCenter: parent.verticalCenter
                                visible: status === Image.Ready
                                source: model.icon
                                        ? ("file://" + iconsPath + "/Dash/" + model.icon)
                                        : ""
                            }

                            Text {
                                width: parent.width - (panelAlertIcon.visible ? 30 : 0)
                                anchors.verticalCenter: parent.verticalCenter
                                text: model.text !== undefined ? model.text : ""
                                color: panelAlert.tone
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSize("widgets_tertiary_text")
                                font.weight: Font.DemiBold
                                elide: Text.ElideRight
                            }
                        }
                    }
                }
                Item { Layout.fillHeight: true }
            }

            // Soaks up the leftover height. Without it the column has nothing
            // that can grow when the alert list is empty (it is excluded from
            // the layout when hidden), so Qt spreads the slack between the rows
            // and the card and the car drift down the panel.
            Item { Layout.fillHeight: true }
        }

    }

    // ── edge grab ───────────────────────────────────────────────────────
    MouseArea {
        id: swipeArea
        anchors.right: parent.right
        width: 42
        height: parent.height
        z: 180
        preventStealing: true
        property real pressX: 0

        onPressed: function (mouse) { pressX = mouse.x; }
        onReleased: function (mouse) {
            var dx = mouse.x - pressX;
            if (dx < -10 && Nav.engineOn)
                Nav.setSidePanelOpen(true);
            else if (dx > 10)
                Nav.setSidePanelOpen(false);
            else if (Nav.engineOn)
                Nav.setSidePanelOpen(true);
        }

        Rectangle {
            visible: !Nav.sidePanelOpen && Nav.engineOn
            width: 4
            height: Math.max(100, parent.height * 0.28)
            radius: 2
            color: Theme.outline
            anchors.right: parent.right
            anchors.rightMargin: 6
            anchors.verticalCenter: parent.verticalCenter
        }
    }
}
