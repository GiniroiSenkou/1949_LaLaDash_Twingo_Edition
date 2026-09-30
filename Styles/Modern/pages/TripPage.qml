import QtQuick
import QtQuick.Layouts
import "../components" as UI

/**
 * Trip — the fuel price prompt after a refuel, the trip recap (distance,
 * litres, cost, eco scores), what is worth checking on the car, and the
 * week / month totals.
 *
 * Everything shown comes pre-formatted from the `Trip` bridge, which only
 * refreshes (1 s) while this page is visible. No timers or animations here.
 */
Item {
    id: root

    property bool priceEditorOpen: false
    property string priceDraft: ""

    onVisibleChanged: if (Trip) Trip.setPageVisible(visible)
    Component.onCompleted: if (Trip) Trip.setPageVisible(visible)

    readonly property var t: Trip ? Trip.trip : ({})
    readonly property var per: Trip ? Trip.periods : ({})
    readonly property bool hasTrip: Trip ? Trip.tripState !== "none" : false

    function stateText() {
        var s = Trip ? Trip.tripState : "none";
        if (s === "live") return "Trip in progress";
        if (s === "paused") return "Stopped — continues if you drive on within a few minutes";
        if (s === "last") return "Last trip · " + (t.started || "");
        return "No trip recorded yet";
    }

    function scoreColor(v) {
        return v >= 80 ? Theme.ok : (v >= 55 ? Theme.warn : Theme.danger);
    }

    component Stat: Column {
        id: st
        property string label: ""
        property string value: ""
        property int size: 30
        spacing: 2
        Text {
            text: st.label
            color: Theme.inkFaint
            font.family: Theme.fontFamily
            font.pixelSize: 13
            font.letterSpacing: 0.3
        }
        Text {
            text: st.value
            color: Theme.ink
            font.family: Theme.fontFamily
            font.pixelSize: st.size
            font.weight: Font.Bold
        }
    }

    component ScoreBar: Item {
        id: sb
        property string label: ""
        property string detail: ""
        property int score: 0
        implicitHeight: 44
        Text {
            id: sbLabel
            anchors.left: parent.left
            anchors.top: parent.top
            text: sb.label
            color: Theme.inkDim
            font.family: Theme.fontFamily
            font.pixelSize: 15
            font.weight: Font.DemiBold
        }
        Text {
            anchors.left: sbLabel.right
            anchors.leftMargin: 8
            anchors.baseline: sbLabel.baseline
            anchors.right: sbValue.left
            anchors.rightMargin: 8
            text: sb.detail
            color: Theme.inkFaint
            font.family: Theme.fontFamily
            font.pixelSize: 13
            elide: Text.ElideRight
        }
        Text {
            id: sbValue
            anchors.right: parent.right
            anchors.top: parent.top
            text: sb.score
            color: root.scoreColor(sb.score)
            font.family: Theme.fontFamily
            font.pixelSize: 15
            font.weight: Font.Bold
        }
        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            anchors.bottomMargin: 4
            height: 8
            radius: 4
            color: Theme.surfaceRaised
            Rectangle {
                width: parent.width * Math.max(0, Math.min(100, sb.score)) / 100
                height: parent.height
                radius: 4
                color: root.scoreColor(sb.score)
            }
        }
    }

    component PeriodCell: Rectangle {
        id: pc
        property string label: ""
        property var p: ({})
        radius: Theme.radiusControl
        color: Theme.surfaceRaised
        implicitHeight: 92
        Column {
            anchors.fill: parent
            anchors.margins: 12
            spacing: 2
            Text {
                text: pc.label
                color: Theme.inkFaint
                font.family: Theme.fontFamily
                font.pixelSize: 12
                font.weight: Font.Bold
                font.letterSpacing: 1.0
            }
            Text {
                text: (pc.p.cost || "—")
                color: Theme.ink
                font.family: Theme.fontFamily
                font.pixelSize: 22
                font.weight: Font.Bold
            }
            Text {
                text: (pc.p.km || "0") + " km · " + (pc.p.l100 || "—") + " L/100"
                color: Theme.inkDim
                font.family: Theme.fontFamily
                font.pixelSize: 13
                width: parent.width
                elide: Text.ElideRight
            }
        }
    }

    RowLayout {
        anchors.fill: parent
        anchors.topMargin: 6
        anchors.leftMargin: Theme.pad
        anchors.rightMargin: Theme.pad
        anchors.bottomMargin: Theme.pad
        spacing: Theme.gap

        // ── left: fuel / refuel + totals ────────────────────────────────
        ColumnLayout {
            Layout.preferredWidth: 380
            Layout.maximumWidth: 380
            Layout.fillHeight: true
            spacing: Theme.gap

            UI.Card {
                Layout.fillWidth: true
                Layout.preferredHeight: fuelCol.implicitHeight + 40
                color: (Trip && Trip.refuelPending) ? Theme.accentSoft : Theme.surface

                Column {
                    id: fuelCol
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: 20
                    spacing: 12

                    UI.SectionLabel {
                        label: (Trip && Trip.refuelPending) ? "Refuel detected" : "Fuel"
                    }

                    // refuel: price entry
                    Column {
                        width: parent.width
                        spacing: 12
                        visible: Trip ? Trip.refuelPending : false

                        Text {
                            width: parent.width
                            text: Trip ? Trip.refuelText : ""
                            color: Theme.inkDim
                            font.family: Theme.fontFamily
                            font.pixelSize: 15
                            wrapMode: Text.WordWrap
                        }
                        Text {
                            text: "Price per litre"
                            color: Theme.inkFaint
                            font.family: Theme.fontFamily
                            font.pixelSize: 13
                        }
                        Row {
                            spacing: 12
                            anchors.horizontalCenter: parent.horizontalCenter
                            UI.PillButton {
                                width: 64; height: 64
                                text: "−"
                                fontSize: 30
                                onClicked: if (Trip) Trip.stepPrice(-1)
                            }
                            Rectangle {
                                width: 200; height: 64
                                radius: Theme.radiusControl
                                color: Theme.surfaceRaised
                                Text {
                                    anchors.centerIn: parent
                                    text: (Trip ? Trip.draftPriceText : "") + " €/L"
                                    color: Theme.ink
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 30
                                    font.weight: Font.Bold
                                }
                                MouseArea {
                                    anchors.fill: parent
                                    onClicked: { root.priceDraft = ""; root.priceEditorOpen = true; }
                                }
                            }
                            UI.PillButton {
                                width: 64; height: 64
                                text: "+"
                                fontSize: 30
                                onClicked: if (Trip) Trip.stepPrice(1)
                            }
                        }
                        Row {
                            spacing: 10
                            anchors.horizontalCenter: parent.horizontalCenter
                            UI.PillButton {
                                width: 160; height: 52
                                variant: "accent"
                                text: "Confirm"
                                onClicked: if (Trip) Trip.confirmRefuel()
                            }
                            UI.PillButton {
                                width: 160; height: 52
                                variant: "ghost"
                                text: "Not a refuel"
                                onClicked: if (Trip) Trip.dismissRefuel()
                            }
                        }
                    }

                    // no refuel pending: what is in the tank
                    Column {
                        width: parent.width
                        spacing: 10
                        visible: !(Trip && Trip.refuelPending)

                        Row {
                            spacing: 28
                            Stat { label: "Tank"; value: Telemetry ? Telemetry.fuelText : "—" }
                            Stat { label: "Tank average"; value: Trip ? Trip.tankAvgPriceText : "—"; size: 22 }
                        }
                        Row {
                            spacing: 28
                            Stat { label: "Last price paid"; value: Trip ? Trip.lastPriceText : "—"; size: 18 }
                            Stat { label: "Estimate calibration"; value: "× " + (Trip ? Trip.calibrationText : "1.00"); size: 18 }
                        }
                        UI.PillButton {
                            height: 44
                            implicitWidth: 170
                            variant: "outline"
                            text: "I refuelled"
                            fontSize: 15
                            onClicked: if (Trip && Telemetry) Trip.manualRefuel(Telemetry.fuelLevel)
                        }
                    }
                }
            }

            UI.Card {
                Layout.fillWidth: true
                Layout.fillHeight: true

                UI.SectionLabel {
                    id: perHeading
                    label: "Totals"
                    anchors.left: parent.left
                    anchors.top: parent.top
                    anchors.margins: 20
                }
                GridLayout {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: perHeading.bottom
                    anchors.margins: 20
                    anchors.topMargin: 12
                    columns: 2
                    rowSpacing: 10
                    columnSpacing: 10
                    PeriodCell { Layout.fillWidth: true; label: "THIS WEEK"; p: root.per.week || ({}) }
                    PeriodCell { Layout.fillWidth: true; label: "LAST WEEK"; p: root.per.last_week || ({}) }
                    PeriodCell { Layout.fillWidth: true; label: "THIS MONTH"; p: root.per.month || ({}) }
                    PeriodCell { Layout.fillWidth: true; label: "LAST MONTH"; p: root.per.last_month || ({}) }
                }
            }
        }

        // ── middle: trip recap + eco ────────────────────────────────────
        UI.Card {
            Layout.fillWidth: true
            Layout.fillHeight: true

            Column {
                anchors.fill: parent
                anchors.margins: 22
                spacing: 16

                UI.SectionLabel {
                    label: root.stateText()
                    width: parent.width          // the paused message is longer than the card
                    wrapMode: Text.WordWrap
                }

                Row {
                    spacing: 36
                    Stat { label: "Distance"; value: (root.t.km || "0.0") + " km"; size: 40 }
                    Stat { label: "Cost"; value: root.t.cost || "—"; size: 40 }
                    Stat { label: "Time"; value: root.t.time || "—"; size: 40 }
                }
                Row {
                    spacing: 30
                    Stat { label: "Fuel"; value: (root.t.litres || "0.00") + " L"; size: 22 }
                    Stat { label: "Consumption"; value: (root.t.l100 || "—") + " L/100"; size: 22 }
                    Stat { label: "Per km"; value: root.t.perKm || "—"; size: 22 }
                    Stat { label: "Avg / max"; value: (root.t.avgSpeed || "0") + " / " + (root.t.maxSpeed || "0") + " km/h"; size: 22 }
                }

                Rectangle { width: parent.width; height: 1; color: Theme.hairline }

                Text {
                    width: parent.width
                    visible: !root.hasTrip
                    text: "Your first drive will show its eco score and fuel cost here."
                    color: Theme.inkFaint
                    font.family: Theme.fontFamily
                    font.pixelSize: 15
                    wrapMode: Text.WordWrap
                }

                Row {
                    visible: root.hasTrip
                    spacing: 16
                    Text {
                        text: root.t.overall !== undefined ? root.t.overall : "—"
                        color: root.scoreColor(root.t.overall || 0)
                        font.family: Theme.fontFamily
                        font.pixelSize: 56
                        font.weight: Font.Bold
                    }
                    Column {
                        anchors.verticalCenter: parent.verticalCenter
                        Text {
                            text: "Eco score"
                            color: Theme.ink
                            font.family: Theme.fontFamily
                            font.pixelSize: 18
                            font.weight: Font.DemiBold
                        }
                        Text {
                            text: "out of 100 — acceleration, gears, anticipation, idling"
                            color: Theme.inkFaint
                            font.family: Theme.fontFamily
                            font.pixelSize: 13
                        }
                    }
                }

                ScoreBar {
                    width: parent.width
                    visible: root.hasTrip
                    label: "Acceleration"
                    detail: (root.t.accEvents || 0) + ((root.t.accEvents === 1) ? " hard pull" : " hard pulls")
                    score: root.t.acc || 0
                }
                ScoreBar {
                    width: parent.width
                    visible: root.hasTrip
                    label: "Gear changes"
                    detail: (root.t.highUpshifts || 0) + " of " + (root.t.upshifts || 0) + " upshifts above 3 000 rpm"
                    score: root.t.gear || 0
                }
                ScoreBar {
                    width: parent.width
                    visible: root.hasTrip
                    label: "Anticipation"
                    detail: (root.t.brakeEvents || 0) + ((root.t.brakeEvents === 1) ? " hard brake" : " hard brakes")
                    score: root.t.antic || 0
                }
                ScoreBar {
                    width: parent.width
                    visible: root.hasTrip
                    label: "Idling"
                    detail: (root.t.idle || "0 min") + " idle"
                    score: root.t.idleScore || 0
                }

                Text {
                    width: parent.width
                    visible: text !== ""
                    text: root.t.tip || ""
                    color: Theme.accent
                    font.family: Theme.fontFamily
                    font.pixelSize: 15
                    font.weight: Font.DemiBold
                    wrapMode: Text.WordWrap
                }
            }
        }

        // ── right: worth checking ───────────────────────────────────────
        UI.Card {
            Layout.preferredWidth: 340
            Layout.maximumWidth: 340
            Layout.fillHeight: true

            Column {
                anchors.fill: parent
                anchors.margins: 20
                spacing: 12

                UI.SectionLabel { label: "Worth checking" }

                Text {
                    width: parent.width
                    visible: !(Trip && Trip.notes.length > 0)
                    text: "Nothing to flag on this trip."
                    color: Theme.inkFaint
                    font.family: Theme.fontFamily
                    font.pixelSize: 15
                    wrapMode: Text.WordWrap
                }

                Repeater {
                    model: Trip ? Trip.notes : []
                    delegate: Rectangle {
                        width: parent.width
                        height: noteCol.implicitHeight + 24
                        radius: Theme.radiusControl
                        color: modelData.recurring ? Theme.dangerBg : Theme.surfaceRaised
                        Column {
                            id: noteCol
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 12
                            spacing: 4
                            Text {
                                width: parent.width
                                text: modelData.title + (modelData.recurring ? "  ·  recurring" : "")
                                color: modelData.recurring ? Theme.danger : Theme.warn
                                font.family: Theme.fontFamily
                                font.pixelSize: 16
                                font.weight: Font.Bold
                                wrapMode: Text.WordWrap
                            }
                            Text {
                                width: parent.width
                                text: modelData.body
                                color: Theme.inkDim
                                font.family: Theme.fontFamily
                                font.pixelSize: 13
                                wrapMode: Text.WordWrap
                            }
                        }
                    }
                }

                Text {
                    width: parent.width
                    text: "Hints from engine data, not a diagnosis."
                    color: Theme.inkGhost
                    font.family: Theme.fontFamily
                    font.pixelSize: 12
                    wrapMode: Text.WordWrap
                }
            }
        }
    }

    // ── price keypad ────────────────────────────────────────────────────
    UI.Sheet {
        z: 60
        title: "Price per litre"
        subtitle: "As shown on the pump"
        opened: root.priceEditorOpen
        onClosed: root.priceEditorOpen = false

        Column {
            anchors.fill: parent
            spacing: 18

            Rectangle {
                width: parent.width
                height: 64
                radius: Theme.radiusControl
                color: Theme.surface
                Text {
                    anchors.centerIn: parent
                    text: (root.priceDraft || "0") + " €/L"
                    color: Theme.ink
                    font.family: Theme.fontFamily
                    font.pixelSize: 28
                    font.weight: Font.Bold
                }
            }

            UI.NumericKeypad {
                width: Math.min(parent.width, 420)
                anchors.horizontalCenter: parent.horizontalCenter
                enterLabel: "Use"
                enterEnabled: root.priceDraft !== "" && !isNaN(parseFloat(root.priceDraft))
                onDigitPressed: function (ch) {
                    if (ch === "." && root.priceDraft.indexOf(".") !== -1)
                        return;
                    if (root.priceDraft.length >= 6)
                        return;
                    root.priceDraft += ch;
                }
                onBackspace: root.priceDraft = root.priceDraft.slice(0, -1)
                onSubmitted: {
                    if (Trip) Trip.setDraftPrice(root.priceDraft);
                    root.priceEditorOpen = false;
                }
            }
        }
    }
}
