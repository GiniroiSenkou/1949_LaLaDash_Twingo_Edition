import QtQuick
import "../components" as UI

/**
 * Lights Simulator — force dash lights on or off to test everything that
 * draws them: the Dashboard's LIGHTS box, the driving screensaver's strip,
 * the light → alert mapping (and its chimes), the icons themselves.
 *
 * Backed by DashLightsHub's overrides through `Settings` (Auto / Force ON /
 * Force OFF per light). The car's own sources — the CAN signal map, the
 * camera detector — keep reporting underneath; Auto hands a light back to
 * them. Overrides outlive the sheet on purpose: "View on Dashboard" closes
 * it so what was forced can be seen where it is actually drawn.
 *
 * Nothing here runs while the sheet is closed: the lit set is read only
 * while open, and the two test timers (Cycle, Hazard blink) are gated on it.
 * Replaces the QWidget dialog that opened as a separate X window.
 */
UI.Sheet {
    id: sheet
    title: "Lights Simulator"
    subtitle: opened
              ? (sheet.lit.length + " lit · " + sheet.overrideCount + " overridden")
              : ""
    fullScreen: true

    readonly property var groups: ["Warnings", "Exterior lights", "Body & driver", "Systems"]
    readonly property var catalog: Settings ? Settings.dashLightCatalog : []
    /** Effective lit set — only marshalled while the sheet is open. */
    readonly property var lit: (opened && Telemetry) ? Telemetry.activeDashLightKeys : []
    readonly property int overrideCount: opened && Settings ? Settings.dashLightOverrideKeys.length : 0
    /** Bumped on every override change so the per-light state re-reads. */
    property int rev: 0
    property var detected: []

    // Test sequences
    property bool cycling: false
    property int  cycleIndex: -1
    property bool blinking: false
    property bool blinkOn: false

    function _forGroup(name) {
        var out = [];
        for (var i = 0; i < catalog.length; i++)
            if (catalog[i].group === name) out.push(catalog[i]);
        return out;
    }

    function _refreshDetected() {
        if (Settings && opened) detected = Settings.dashLightsDetected();
    }

    function stopTests() {
        if (cycling) { cycling = false; cycleIndex = -1; if (Settings) Settings.clearDashLightOverrides(); }
        if (blinking) { blinking = false; blinkOn = false;
                        if (Settings) Settings.setDashLightOverrides(["left_turn", "right_turn"], -1); }
    }

    onOpenedChanged: {
        if (opened) { rev++; _refreshDetected(); }
        else stopTests();
    }

    Connections {
        target: Settings
        function onDashLightOverridesChanged() { if (sheet.opened) sheet.rev++; }
    }
    Connections {
        target: Telemetry
        enabled: sheet.opened
        function onDashLightsChanged() { sheet._refreshDetected(); }
    }

    // Cycle: one light at a time through the whole catalog, so every icon
    // and every light → alert hop can be watched go by.
    Timer {
        interval: 1200
        repeat: true
        running: sheet.opened && sheet.cycling
        triggeredOnStart: true
        onTriggered: {
            if (!Settings || sheet.catalog.length === 0) return;
            sheet.cycleIndex = (sheet.cycleIndex + 1) % sheet.catalog.length;
            Settings.clearDashLightOverrides();
            Settings.setDashLightOverride(sheet.catalog[sheet.cycleIndex].key, 1);
        }
    }

    // Hazard: both indicators at the real ~1.5 Hz flash — the one light that
    // changes state faster than the poll that maps lights to alerts.
    Timer {
        interval: 330
        repeat: true
        running: sheet.opened && sheet.blinking
        triggeredOnStart: true
        onTriggered: {
            if (!Settings) return;
            sheet.blinkOn = !sheet.blinkOn;
            Settings.setDashLightOverrides(["left_turn", "right_turn"], sheet.blinkOn ? 1 : 0);
        }
    }

    headerControl: Row {
        spacing: 8
        UI.PillButton {
            text: "All on"; height: 40
            onClicked: { sheet.stopTests(); if (Settings) Settings.setAllDashLights(1); }
        }
        UI.PillButton {
            text: "All off"; height: 40
            onClicked: { sheet.stopTests(); if (Settings) Settings.setAllDashLights(0); }
        }
        UI.PillButton {
            text: "Auto (clear)"; height: 40
            variant: sheet.overrideCount > 0 ? "accent" : "raised"
            onClicked: { sheet.stopTests(); if (Settings) Settings.clearDashLightOverrides(); }
        }
    }

    Column {
        anchors.fill: parent
        spacing: 12

        // ── quick tests ─────────────────────────────────────────────────
        Rectangle {
            width: parent.width
            height: testsCol.implicitHeight + 20
            radius: Theme.radiusControl
            color: Theme.surfaceRaised

            Column {
                id: testsCol
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: 10
                spacing: 8

                Row {
                    spacing: 8
                    Text {
                        text: "PRESETS"
                        color: Theme.inkFaint; font.family: Theme.fontFamily
                        font.pixelSize: 11; font.weight: Font.Bold; font.letterSpacing: 1.2
                        width: 88
                        anchors.verticalCenter: parent.verticalCenter
                    }
                    UI.PillButton { variant: "outline"; text: "Night drive";  height: 40; onClicked: { sheet.stopTests(); if (Settings) Settings.applyDashLightPreset("night"); } }
                    UI.PillButton { variant: "outline"; text: "Warnings";     height: 40; onClicked: { sheet.stopTests(); if (Settings) Settings.applyDashLightPreset("warnings"); } }
                    UI.PillButton { variant: "outline"; text: "Critical";     height: 40; onClicked: { sheet.stopTests(); if (Settings) Settings.applyDashLightPreset("critical"); } }
                    UI.PillButton { variant: "outline"; text: "Body";         height: 40; onClicked: { sheet.stopTests(); if (Settings) Settings.applyDashLightPreset("body"); } }
                    UI.PillButton {
                        variant: "outline"
                        text: "Random fault"; height: 40
                        onClicked: {
                            sheet.stopTests();
                            if (!Settings) return;
                            var warn = sheet._forGroup("Warnings");
                            if (warn.length === 0) return;
                            var pick = warn[Math.floor(Math.random() * warn.length)];
                            Settings.setDashLightOverride(pick.key, 1);
                        }
                    }
                }

                Row {
                    spacing: 8
                    Text {
                        text: "SEQUENCES"
                        color: Theme.inkFaint; font.family: Theme.fontFamily
                        font.pixelSize: 11; font.weight: Font.Bold; font.letterSpacing: 1.2
                        width: 88
                        anchors.verticalCenter: parent.verticalCenter
                    }
                    UI.PillButton {
                        text: sheet.cycling
                              ? ("Cycling " + (sheet.cycleIndex + 1) + "/" + sheet.catalog.length + " — stop")
                              : "Cycle every light"
                        height: 40
                        variant: "outline"
                        checked: sheet.cycling
                        onClicked: {
                            if (sheet.cycling) { sheet.stopTests(); return; }
                            sheet.stopTests();
                            if (Settings) Settings.clearDashLightOverrides();
                            sheet.cycleIndex = -1;
                            sheet.cycling = true;
                        }
                    }
                    UI.PillButton {
                        text: sheet.blinking ? "Hazard blinking — stop" : "Hazard blink"
                        height: 40
                        variant: "outline"
                        checked: sheet.blinking
                        onClicked: {
                            if (sheet.blinking) { sheet.stopTests(); return; }
                            sheet.stopTests();
                            sheet.blinkOn = false;
                            sheet.blinking = true;
                        }
                    }
                    Item { width: 18; height: 1 }
                    Text {
                        text: "VIEW"
                        color: Theme.inkFaint; font.family: Theme.fontFamily
                        font.pixelSize: 11; font.weight: Font.Bold; font.letterSpacing: 1.2
                        anchors.verticalCenter: parent.verticalCenter
                    }
                    UI.PillButton {
                        text: "Dashboard"; height: 40; variant: "outline"
                        onClicked: { sheet.close(); if (Nav) Nav.navigateTo(1); }
                    }
                    UI.PillButton {
                        text: "Driving screensaver"; height: 40; variant: "outline"
                        onClicked: {
                            sheet.close();
                            if (Nav) { Nav.setScreensaverMode("driving"); Nav.showScreensaver(); }
                        }
                    }
                }

                // What the car itself reports, before any override — so a
                // forced light is never mistaken for a real one.
                Text {
                    width: parent.width
                    text: "Car reports: " + (sheet.detected.length > 0 ? sheet.detected.join(", ") : "nothing lit")
                          + "   ·   forced lights stay until Auto"
                    color: Theme.inkFaint
                    font.family: Theme.fontFamily
                    font.pixelSize: 12
                    elide: Text.ElideRight
                }
            }
        }

        // ── the lights, by group ────────────────────────────────────────
        Flickable {
            id: lightsFlick
            width: parent.width
            height: parent.height - y
            contentHeight: groupsCol.implicitHeight + 8
            clip: true
            boundsBehavior: Flickable.StopAtBounds

            readonly property int cardW: Math.floor((width - 5 * 10) / 6)

            Column {
                id: groupsCol
                width: lightsFlick.width
                spacing: 6

                Repeater {
                    model: sheet.groups

                    Column {
                        width: groupsCol.width
                        spacing: 6
                        readonly property string groupName: modelData

                        UI.SectionLabel { label: parent.groupName }

                        Flow {
                            width: parent.width
                            spacing: 10

                            Repeater {
                                model: sheet._forGroup(parent.parent.groupName)

                                Rectangle {
                                    id: card
                                    width: lightsFlick.cardW
                                    height: 96
                                    radius: Theme.radiusControl
                                    readonly property string key: modelData.key
                                    readonly property bool isLit: sheet.lit.indexOf(key) >= 0
                                    // -1 auto · 0 forced off · 1 forced on; re-read on rev
                                    readonly property int ovr: (sheet.rev, Settings) ? Settings.dashLightOverride(key) : -1
                                    color: isLit ? Theme.okBg : Theme.surfaceRaised
                                    border.width: isLit ? 1 : 0
                                    border.color: Theme.ok

                                    Row {
                                        anchors.top: parent.top
                                        anchors.left: parent.left
                                        anchors.right: parent.right
                                        anchors.margins: 10
                                        spacing: 8
                                        height: 40

                                        Image {
                                            width: 36; height: 36
                                            sourceSize.width: 72; sourceSize.height: 72
                                            fillMode: Image.PreserveAspectFit
                                            asynchronous: true
                                            cache: true
                                            opacity: card.isLit ? 1.0 : 0.45
                                            source: iconsPath + "/Dash/" + card.key + ".png"
                                            anchors.verticalCenter: parent.verticalCenter
                                        }
                                        Text {
                                            width: parent.width - 36 - 8 - 12
                                            text: modelData.label
                                            color: card.isLit ? Theme.ink : Theme.inkDim
                                            font.family: Theme.fontFamily
                                            font.pixelSize: 14
                                            font.weight: Font.DemiBold
                                            elide: Text.ElideRight
                                            anchors.verticalCenter: parent.verticalCenter
                                        }
                                        Rectangle {
                                            width: 8; height: 8; radius: 4
                                            color: card.isLit ? Theme.ok : Theme.inkGhost
                                            anchors.verticalCenter: parent.verticalCenter
                                        }
                                    }

                                    // Auto · ON · OFF
                                    Row {
                                        anchors.bottom: parent.bottom
                                        anchors.left: parent.left
                                        anchors.margins: 10
                                        spacing: 4

                                        Repeater {
                                            model: [ { label: "Auto", value: -1 },
                                                     { label: "ON",   value: 1 },
                                                     { label: "OFF",  value: 0 } ]
                                            Rectangle {
                                                readonly property bool active: card.ovr === modelData.value
                                                // Auto is the default state — it gets a neutral highlight, not
                                                // the accent, so an un-forced grid doesn't read as a wall of
                                                // blue. Only the meaningful overrides carry colour: ON green,
                                                // OFF red.
                                                readonly property bool forced: active && modelData.value !== -1
                                                width: Math.floor((card.width - 20 - 8) / 3)
                                                height: 30
                                                radius: Theme.radiusControl - 4
                                                color: active
                                                       ? (modelData.value === 1 ? Theme.ok
                                                          : modelData.value === 0 ? Theme.danger : Theme.surfaceHover)
                                                       : (segMa.pressed ? Theme.surfacePressed : Theme.surface)
                                                Text {
                                                    anchors.centerIn: parent
                                                    text: modelData.label
                                                    color: parent.forced ? Theme.accentInk
                                                           : parent.active ? Theme.ink : Theme.inkDim
                                                    font.family: Theme.fontFamily
                                                    font.pixelSize: 12
                                                    font.weight: parent.active ? Font.DemiBold : Font.Medium
                                                }
                                                MouseArea {
                                                    id: segMa
                                                    anchors.fill: parent
                                                    onClicked: {
                                                        if (sheet.cycling || sheet.blinking) sheet.stopTests();
                                                        if (Settings) Settings.setDashLightOverride(card.key, modelData.value);
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
