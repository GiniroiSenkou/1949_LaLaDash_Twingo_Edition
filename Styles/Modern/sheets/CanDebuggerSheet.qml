import QtQuick
import "../components" as UI

/**
 * CAN debugger — Live / Assign / Record, backed by the `Can` bridge.
 *
 * Opening it switches the STN monitor to "every ID" and starts a 2 Hz
 * refresh; closing it goes back to the mapped-IDs-only filter and stops the
 * timer (Can.open()/Can.close()). Nothing here polls while the sheet is
 * closed.
 *
 *  Live    – the bus: one row per ID, green = confirmed signal on it, amber =
 *            candidate, grey = unknown; bytes that changed in the last 2 s
 *            are highlighted. Tap a row for its bit grid and Assign.
 *  Assign  – the signal catalog with its bindings and live decoded values;
 *            Confirm / Clear per signal.
 *  Record  – baseline (hands off) then an action window; the bits that
 *            moved, ranked, merged across repeats; one-tap assign.
 */
UI.Sheet {
    id: sheet
    title: "CAN Debugger"
    subtitle: Can ? Can.statusText : ""
    fullScreen: true

    property string tab: "live"
    property int    pickByte: 0
    property int    pickBit: 0
    property string recAction: ""
    property real   recBaseline: 5
    property real   recWindow: 8

    onOpenedChanged: {
        if (!Can) return;
        if (opened) Can.open(); else Can.close();
    }

    function _statusColor(st) {
        if (st === "confirmed") return Theme.ok;
        if (st === "candidate") return Theme.warn;
        return Theme.inkGhost;
    }

    headerControl: Row {
        spacing: 8
        UI.PillButton {
            text: Can && Can.connected ? "Disconnect" : "Connect"
            height: 40
            onClicked: { if (!Can) return; if (Can.connected) Can.disconnectNow(); else Can.connectNow(); }
        }
        UI.Segmented {
            model: [ { label: "Live", value: "live" }, { label: "Assign", value: "assign" }, { label: "Record", value: "record" } ]
            currentValue: sheet.tab
            onSelected: function (v) { sheet.tab = v; }
        }
    }

    // ═══════════════════════════════ LIVE ═══════════════════════════════
    Item {
        anchors.fill: parent
        visible: sheet.tab === "live"

        // Connection preset card (+ a loud banner when the frames are a replay)
        Rectangle {
            id: presetCard
            width: parent.width
            height: Can && Can.isReplay ? 78 : 54
            radius: Theme.radiusControl
            color: Can && Can.isReplay ? Theme.dangerBg : Theme.surfaceRaised
            Column {
                anchors.fill: parent
                anchors.margins: 10
                spacing: 2
                Text {
                    visible: Can && Can.isReplay
                    width: parent.width
                    text: Can ? Can.replayText : ""
                    color: Theme.danger; font.family: Theme.fontFamily; font.pixelSize: 13; font.bold: true
                    wrapMode: Text.WordWrap
                }
                Text {
                    text: "Working connection (default)"
                    color: Theme.inkFaint; font.family: Theme.fontFamily; font.pixelSize: 12
                }
                Text {
                    width: parent.width
                    text: Can ? Can.presetText : ""
                    color: Theme.ink; font.family: Theme.fontFamily; font.pixelSize: 14; elide: Text.ElideRight
                }
            }
        }

        // ID table (left) + detail (right)
        Row {
            anchors.top: presetCard.bottom
            anchors.topMargin: 12
            anchors.bottom: parent.bottom
            width: parent.width
            spacing: 14

            Flickable {
                id: idList
                width: parent.width * 0.55
                height: parent.height
                contentHeight: idCol.height
                clip: true
                Column {
                    id: idCol
                    width: idList.width
                    spacing: 4
                    Row {
                        height: 24
                        spacing: 8
                        Text { width: 60;  text: "ID";    color: Theme.inkFaint; font.family: Theme.fontFamily; font.pixelSize: 12 }
                        Text { width: 60;  text: "Hz";    color: Theme.inkFaint; font.family: Theme.fontFamily; font.pixelSize: 12 }
                        Text { width: 220; text: "Data";  color: Theme.inkFaint; font.family: Theme.fontFamily; font.pixelSize: 12 }
                        Text { text: "Label";             color: Theme.inkFaint; font.family: Theme.fontFamily; font.pixelSize: 12 }
                    }
                    Repeater {
                        model: Can ? Can.liveIds : []
                        Rectangle {
                            id: idRow
                            width: idCol.width
                            height: 40
                            radius: 8
                            readonly property var changedFlags: modelData.changed
                            readonly property bool selected: Can && Can.selectedId === modelData.idHex
                            color: selected ? Theme.accentSoft : (rowArea.pressed ? Theme.surfacePressed : Theme.surfaceRaised)
                            Rectangle {   // status bar at the left edge
                                width: 5; height: parent.height - 12; radius: 3
                                anchors.left: parent.left; anchors.leftMargin: 6
                                anchors.verticalCenter: parent.verticalCenter
                                color: sheet._statusColor(modelData.status)
                            }
                            Row {
                                anchors.left: parent.left; anchors.leftMargin: 18
                                anchors.verticalCenter: parent.verticalCenter
                                spacing: 8
                                Text { width: 52; text: modelData.idHex; color: sheet._statusColor(modelData.status); font.family: "monospace"; font.pixelSize: 16; font.bold: true }
                                Text { width: 60; text: modelData.hz.toFixed(0); color: Theme.inkFaint; font.family: Theme.fontFamily; font.pixelSize: 14 }
                                Row {
                                    width: 220
                                    spacing: 3
                                    Repeater {
                                        model: modelData.bytes
                                        Text {
                                            text: modelData
                                            font.family: "monospace"; font.pixelSize: 14
                                            color: (idRow.changedFlags && idRow.changedFlags[index]) ? Theme.warn : Theme.ink
                                        }
                                    }
                                }
                                Text { text: modelData.label || ""; color: Theme.inkFaint; font.family: Theme.fontFamily; font.pixelSize: 13; elide: Text.ElideRight; width: 160 }
                            }
                            MouseArea { id: rowArea; anchors.fill: parent; onClicked: if (Can) Can.selectId(modelData.idHex) }
                        }
                    }
                    Text {
                        visible: !Can || Can.liveIds.length === 0
                        text: Can && Can.connected ? "Waiting for frames…" : "Not monitoring. Set Protocol to CAN-BUS in Settings → OBD and Connect."
                        color: Theme.inkFaint; font.family: Theme.fontFamily; font.pixelSize: 14
                        wrapMode: Text.WordWrap; width: idCol.width
                    }
                }
            }

            // Detail / bit grid
            Flickable {
                width: parent.width - idList.width - 14
                height: parent.height
                contentHeight: detailCol.height
                clip: true
                Column {
                    id: detailCol
                    width: parent.width
                    spacing: 8
                    readonly property var d: Can ? Can.detail : ({})
                    Text {
                        text: detailCol.d && detailCol.d.idHex
                              ? ("0x" + detailCol.d.idHex + "  ·  " + (detailCol.d.label || "unknown") + "  ·  " + detailCol.d.count + " frames")
                              : "Tap an ID"
                        color: Theme.ink; font.family: Theme.fontFamily; font.pixelSize: 16; font.bold: true
                    }
                    // bit grid: one row per byte, MSB→LSB
                    Repeater {
                        model: detailCol.d && detailCol.d.bytes ? detailCol.d.bytes : []
                        Row {
                            spacing: 4
                            readonly property var byteRow: modelData
                            Text { width: 56; text: "b" + byteRow.byte + " " + byteRow.hex; color: byteRow.changedAgoMs >= 0 && byteRow.changedAgoMs < 2000 ? Theme.warn : Theme.inkFaint; font.family: "monospace"; font.pixelSize: 14; anchors.verticalCenter: parent.verticalCenter }
                            Repeater {
                                model: 8
                                Rectangle {
                                    width: 34; height: 30; radius: 6
                                    readonly property int bitNo: 7 - index
                                    readonly property bool picked: sheet.pickByte === byteRow.byte && sheet.pickBit === bitNo
                                    color: picked ? Theme.accentSoft : (byteRow.bits[index] ? Theme.okBg : Theme.surfaceRaised)
                                    border.width: byteRow.flipped[index] ? 2 : 0
                                    border.color: Theme.warn
                                    Text { anchors.centerIn: parent; text: byteRow.bits[index]; color: Theme.ink; font.family: "monospace"; font.pixelSize: 14 }
                                    MouseArea { anchors.fill: parent; onClicked: { sheet.pickByte = byteRow.byte; sheet.pickBit = bitNo; } }
                                }
                            }
                        }
                    }
                    Text {
                        visible: !!(detailCol.d && detailCol.d.signals && detailCol.d.signals.length > 0)
                        text: "Bound here: " + (detailCol.d && detailCol.d.signals ? detailCol.d.signals.map(function (s) { return s.label + " (b" + s.byte + "." + s.bit + ", " + s.status + ")"; }).join(", ") : "")
                        width: parent.width; wrapMode: Text.WordWrap
                        color: Theme.inkFaint; font.family: Theme.fontFamily; font.pixelSize: 13
                    }
                    // Assign the picked bit to a catalog signal
                    Text {
                        visible: detailCol.d && detailCol.d.idHex ? true : false
                        text: "Assign bit b" + sheet.pickByte + "." + sheet.pickBit + " of 0x" + (detailCol.d ? detailCol.d.idHex : "") + " to:"
                        color: Theme.ink; font.family: Theme.fontFamily; font.pixelSize: 14
                    }
                    Flow {
                        visible: detailCol.d && detailCol.d.idHex ? true : false
                        width: parent.width
                        spacing: 6
                        Repeater {
                            model: Can ? Can.catalog.filter(function (r) { return r.kind === "bool"; }) : []
                            UI.PillButton {
                                text: modelData.label
                                height: 36
                                fontSize: 12
                                checked: modelData.status !== "unassigned" && modelData.id === ("0x" + (detailCol.d ? detailCol.d.idHex : "")) && modelData.byte === sheet.pickByte && modelData.bit === sheet.pickBit
                                onClicked: if (Can && detailCol.d) Can.assignBool(modelData.key, detailCol.d.idHex, sheet.pickByte, sheet.pickBit)
                            }
                        }
                    }
                }
            }
        }
    }

    // ══════════════════════════════ ASSIGN ══════════════════════════════
    Flickable {
        anchors.fill: parent
        visible: sheet.tab === "assign"
        contentHeight: assignCol.height
        clip: true
        Column {
            id: assignCol
            width: parent.width
            spacing: 4
            Text {
                text: "Green = confirmed and feeding the app · amber = candidate (shown here only) · grey = not mapped. Toggle the real thing and watch the Live column."
                width: parent.width; wrapMode: Text.WordWrap
                color: Theme.inkFaint; font.family: Theme.fontFamily; font.pixelSize: 13
            }
            Repeater {
                model: Can ? Can.catalog : []
                Rectangle {
                    width: assignCol.width
                    height: 46
                    radius: 8
                    color: Theme.surfaceRaised
                    Rectangle {
                        width: 5; height: parent.height - 12; radius: 3
                        anchors.left: parent.left; anchors.leftMargin: 6
                        anchors.verticalCenter: parent.verticalCenter
                        color: sheet._statusColor(modelData.status)
                    }
                    Row {
                        anchors.left: parent.left; anchors.leftMargin: 18
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 10
                        Text { width: 210; text: modelData.label; color: Theme.ink; font.family: Theme.fontFamily; font.pixelSize: 15; elide: Text.ElideRight }
                        Text { width: 110; text: modelData.group; color: Theme.inkGhost; font.family: Theme.fontFamily; font.pixelSize: 12 }
                        Text { width: 190; text: modelData.binding || "—"; color: sheet._statusColor(modelData.status); font.family: "monospace"; font.pixelSize: 14 }
                        Text { width: 90; text: modelData.live; color: modelData.live === "ON" ? Theme.ok : Theme.ink; font.family: "monospace"; font.pixelSize: 15; font.bold: modelData.live === "ON" }
                    }
                    Row {
                        anchors.right: parent.right; anchors.rightMargin: 8
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 6
                        UI.PillButton {
                            text: modelData.status === "confirmed" ? "Unconfirm" : "Confirm"
                            height: 34; fontSize: 12
                            enabled: modelData.status !== "unassigned"
                            onClicked: { if (!Can) return; if (modelData.status === "confirmed") Can.unconfirm(modelData.key); else Can.confirm(modelData.key); }
                        }
                        UI.PillButton {
                            text: "Clear"
                            height: 34; fontSize: 12
                            enabled: modelData.status !== "unassigned"
                            onClicked: if (Can) Can.clear(modelData.key)
                        }
                    }
                }
            }
        }
    }

    // ══════════════════════════════ RECORD ══════════════════════════════
    Item {
        anchors.fill: parent
        visible: sheet.tab === "record"

        Column {
            id: recLeft
            width: parent.width * 0.42
            height: parent.height
            spacing: 8
            Rectangle {
                width: parent.width
                height: hintText.height + 20
                radius: Theme.radiusControl
                color: Can && Can.recordPhase === "action" ? Theme.okBg
                     : Can && Can.recordPhase === "baseline" ? Theme.dangerBg
                     : Theme.surfaceRaised
                Text {
                    id: hintText
                    anchors.left: parent.left; anchors.right: parent.right; anchors.margins: 10
                    anchors.verticalCenter: parent.verticalCenter
                    text: Can ? Can.recordHint : ""
                    wrapMode: Text.WordWrap
                    color: Can && Can.recordPhase === "action" ? Theme.ok
                         : Can && Can.recordPhase === "baseline" ? Theme.danger
                         : Theme.inkFaint
                    font.family: Theme.fontFamily
                    font.pixelSize: Can && (Can.recordPhase === "action" || Can.recordPhase === "baseline") ? 17 : 13
                    font.bold: Can && (Can.recordPhase === "action" || Can.recordPhase === "baseline")
                }
            }
            Flickable {
                width: parent.width
                height: parent.height - 150
                contentHeight: actCol.height
                clip: true
                Column {
                    id: actCol
                    width: parent.width
                    spacing: 6
                    Repeater {
                        model: Can ? Can.actionGroups : []
                        Column {
                            width: actCol.width
                            spacing: 4
                            readonly property var grp: modelData
                            Text { text: grp.group; color: Theme.inkFaint; font.family: Theme.fontFamily; font.pixelSize: 12 }
                            Flow {
                                width: parent.width
                                spacing: 6
                                Repeater {
                                    model: grp.actions
                                    UI.PillButton {
                                        text: modelData
                                        height: 36; fontSize: 12
                                        checked: sheet.recAction === modelData
                                        onClicked: sheet.recAction = modelData
                                    }
                                }
                            }
                        }
                    }
                }
            }
            Row {
                spacing: 8
                UI.PillButton {
                    text: Can && Can.recordPhase === "baseline" ? ("Baseline… " + Can.recordSecondsLeft + " s")
                        : Can && Can.recordPhase === "action" ? ("DO IT NOW… " + Can.recordSecondsLeft + " s")
                        : "Record"
                    height: 44
                    enabled: Can && Can.connected && Can.monitoring && sheet.recAction !== "" && (Can.recordPhase === "idle" || Can.recordPhase === "done")
                    onClicked: if (Can) Can.startRecord(sheet.recAction, sheet.recBaseline, sheet.recWindow)
                }
                UI.PillButton {
                    text: "Cancel"; height: 44
                    enabled: Can && (Can.recordPhase === "baseline" || Can.recordPhase === "action")
                    onClicked: if (Can) Can.cancelRecord()
                }
                UI.PillButton {
                    text: "Reset runs"; height: 44
                    enabled: Can && Can.recordRuns > 0
                    onClicked: if (Can) Can.resetRecord()
                }
                UI.PillButton {
                    text: Can && Can.rawCapturing ? "Recording raw…" : "Raw 60 s to file"
                    height: 44
                    enabled: Can && Can.connected && Can.monitoring && !Can.rawCapturing
                    onClicked: if (Can) Can.startRawCapture(60)
                }
            }
        }

        Flickable {
            anchors.left: recLeft.right
            anchors.leftMargin: 14
            anchors.right: parent.right
            height: parent.height
            contentHeight: resCol.height
            clip: true
            Column {
                id: resCol
                width: parent.width
                spacing: 6
                Text {
                    text: Can && Can.recordAction
                          ? (Can.recordAction + " — " + Can.recordRuns + " run(s)" + (Can.recordPhase === "done" ? " · ranked bits:" : ""))
                          : "No recording yet"
                    color: Theme.ink; font.family: Theme.fontFamily; font.pixelSize: 15; font.bold: true
                }
                Text {
                    visible: Can && Can.rawCapturePath !== ""
                    text: Can ? ("Raw capture: " + Can.rawCapturePath) : ""
                    width: parent.width; wrapMode: Text.WrapAnywhere
                    color: Theme.inkFaint; font.family: Theme.fontFamily; font.pixelSize: 12
                }
                Repeater {
                    model: Can ? Can.recordResults : []
                    Rectangle {
                        width: resCol.width
                        height: 44
                        radius: 8
                        color: Theme.surfaceRaised
                        Row {
                            anchors.left: parent.left; anchors.leftMargin: 12
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: 10
                            Text { width: 120; text: "0x" + modelData.idHex + " b" + modelData.byte + "." + modelData.bit; color: Theme.ink; font.family: "monospace"; font.pixelSize: 15; font.bold: true }
                            Text { width: 70; text: modelData.kind; color: modelData.kind === "blink" ? Theme.warn : Theme.inkFaint; font.family: Theme.fontFamily; font.pixelSize: 13 }
                            Text { width: 120; text: modelData.runs + "/" + modelData.ofRuns + " runs · +" + modelData.firstAt + " s"; color: Theme.inkFaint; font.family: Theme.fontFamily; font.pixelSize: 13 }
                            Text { width: 60; text: "score " + modelData.score; color: Theme.inkGhost; font.family: Theme.fontFamily; font.pixelSize: 12 }
                        }
                        UI.PillButton {
                            anchors.right: parent.right; anchors.rightMargin: 8
                            anchors.verticalCenter: parent.verticalCenter
                            text: modelData.signal ? ("Assign as " + modelData.signal) : "Assign…"
                            height: 34; fontSize: 12
                            onClicked: {
                                if (!Can) return;
                                if (modelData.signal) { Can.assignBool(modelData.signal, modelData.idHex, modelData.byte, modelData.bit); }
                                else { Can.selectId(modelData.idHex); sheet.pickByte = modelData.byte; sheet.pickBit = modelData.bit; sheet.tab = "live"; }
                            }
                        }
                    }
                }
            }
        }
    }
}
