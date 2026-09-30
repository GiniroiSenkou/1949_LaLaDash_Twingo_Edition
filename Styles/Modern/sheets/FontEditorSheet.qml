import QtQuick
import "../components" as UI

/**
 * Font group editor.
 *
 * Opened by any of the fifteen Font rows in Settings. Every change writes
 * straight through `Settings.setFontValue(groupKey, field, value)`; there is no
 * OK/Cancel, exactly as in the legacy overlay.
 *
 *     fontSheet.open("settings_primary_text", "Settings Label");
 */
UI.Sheet {
    id: root

    property string groupKey: ""
    property string groupTitle: ""

    // Editor state, loaded from Settings.fontGroupRaw().
    property int    currentSize: 14
    property real   currentSpacing: 0.0
    property string currentStyle: "regular"
    property bool   currentDebug: false

    title: "Font"
    subtitle: groupTitle

    readonly property var styleOptions: [
        { value: "regular",      label: "Regular" },
        { value: "bold",         label: "Bold" },
        { value: "italic",       label: "Italic" },
        { value: "bold_italic",  label: "Bold Italic" },
        { value: "light",        label: "Light" },
        { value: "light_italic", label: "Light Italic" },
        { value: "black",        label: "Black" }
    ]

    function open(key, titleText) {
        root.groupKey = key;
        root.groupTitle = titleText;
        root._load();
        root.opened = true;
    }

    function _load() {
        if (!Settings)
            return;
        var raw = Settings.fontGroupRaw(root.groupKey);
        if (!raw)
            return;
        root.currentSize = raw.size || 14;
        root.currentSpacing = raw.spacing || 0.0;
        root.currentStyle = raw.style || "regular";
        root.currentDebug = raw.debug || false;
    }

    function _save(field, value) {
        if (Settings)
            Settings.setFontValue(root.groupKey, field, value);
    }

    function _bumpSize(delta) {
        var next = Math.max(8, Math.min(64, root.currentSize + delta));
        if (next === root.currentSize)
            return;
        root.currentSize = next;
        root._save("size", next.toString());
    }

    function _bumpSpacing(delta) {
        var next = Math.round((root.currentSpacing + delta) * 10) / 10;
        next = Math.max(-2.0, Math.min(12.0, next));
        if (next === root.currentSpacing)
            return;
        root.currentSpacing = next;
        root._save("spacing", next.toFixed(1));
    }

    // ── body ───────────────────────────────────────────────────────────
    Flickable {
        anchors.fill: parent
        clip: true
        contentWidth: width
        contentHeight: editorCol.implicitHeight
        boundsBehavior: Flickable.StopAtBounds

        Column {
            id: editorCol
            width: parent.width
            spacing: 0

            // ── Size ───────────────────────────────────────────────────
            UI.ListRow {
                label: "Size"
                sub: "Glyph height in pixels · 8 – 64"
                Stepper {
                    readout: root.currentSize + "px"
                    onDown: root._bumpSize(-1)
                    onUp: root._bumpSize(1)
                }
            }

            // ── Spacing ────────────────────────────────────────────────
            UI.ListRow {
                label: "Spacing"
                sub: "Letter spacing · −2.0 – 12.0, steps of 0.2"
                Stepper {
                    readout: root.currentSpacing.toFixed(1)
                    onDown: root._bumpSpacing(-0.2)
                    onUp: root._bumpSpacing(0.2)
                }
            }

            // ── Style ──────────────────────────────────────────────────
            Item {
                width: parent.width
                implicitHeight: styleCol.implicitHeight + 26

                Column {
                    id: styleCol
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 10

                    UI.SectionLabel { label: "Style" }

                    Flow {
                        width: parent.width
                        spacing: 8

                        Repeater {
                            model: root.styleOptions

                            UI.PillButton {
                                text: modelData.label
                                variant: "raised"
                                radius: Theme.radiusControl
                                height: 42
                                checked: root.currentStyle === modelData.value
                                onClicked: {
                                    root.currentStyle = modelData.value;
                                    root._save("style", modelData.value);
                                }
                            }
                        }
                    }
                }

                Rectangle {
                    anchors.bottom: parent.bottom
                    anchors.left: parent.left
                    anchors.right: parent.right
                    height: 1
                    color: Theme.hairline
                }
            }

            // ── Debug highlight ────────────────────────────────────────
            UI.ListRow {
                label: "Debug Highlight"
                sub: "Highlight this font group in purple"
                divider: false
                UI.Toggle {
                    checked: root.currentDebug
                    onToggled: function (v) {
                        root.currentDebug = v;
                        root._save("debug", v ? "1" : "0");
                    }
                }
            }
        }
    }

    // ── −/value/+ trio ─────────────────────────────────────────────────
    component Stepper: Row {
        id: stepper
        property string readout: ""
        signal down()
        signal up()

        spacing: 10

        UI.PillButton {
            text: "−"
            variant: "raised"
            radius: Theme.radiusControl
            implicitWidth: 52
            height: 44
            fontSize: 20
            onClicked: stepper.down()
        }

        Rectangle {
            width: 78
            height: 44
            radius: Theme.radiusControl
            color: Theme.surfaceRaised

            Text {
                anchors.centerIn: parent
                text: stepper.readout
                color: Theme.ink
                font.family: Theme.fontFamily
                font.pixelSize: 16
                font.weight: Font.Bold
            }
        }

        UI.PillButton {
            text: "+"
            variant: "raised"
            radius: Theme.radiusControl
            implicitWidth: 52
            height: 44
            fontSize: 20
            onClicked: stepper.up()
        }
    }
}
