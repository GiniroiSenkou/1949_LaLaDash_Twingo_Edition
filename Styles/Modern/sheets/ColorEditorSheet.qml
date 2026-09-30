import QtQuick
import "../components" as UI

/**
 * A/B colour group editor.
 *
 * Opened by the Primary / Secondary / Third colour rows in Settings → Themes.
 * Picking calls the native dialog (`Settings.pickColor`) and then writes the
 * pair with `Settings.applyColorGroup`, which also derives the blended output
 * keys — so there is nothing to confirm.
 *
 * TopBar Color and Progress Bar are single colours and do NOT use this sheet;
 * they call pickColor + setStyleColor directly from the page.
 *
 *     colorSheet.open("Primary Color", "base_color_a", "base_color_b");
 */
UI.Sheet {
    id: root

    property string groupTitle: ""
    property string keyA: ""
    property string keyB: ""
    property color  currentA: "#2a2f3a"
    property color  currentB: "#2a2f3a"

    title: "Colour"
    subtitle: groupTitle

    function open(titleText, ka, kb) {
        root.groupTitle = titleText;
        root.keyA = ka;
        root.keyB = kb;
        if (Settings) {
            var a = Settings.getValue("Style", ka);
            var b = Settings.getValue("Style", kb);
            root.currentA = (a && a.length === 7 && a.charAt(0) === "#") ? a : "#2a2f3a";
            root.currentB = (b && b.length === 7 && b.charAt(0) === "#") ? b : "#2a2f3a";
        }
        root.opened = true;
    }

    function _apply() {
        if (Settings)
            Settings.applyColorGroup(root.keyA, root.keyB,
                                     root.currentA.toString(), root.currentB.toString());
    }

    function _pick(which) {
        if (!Settings)
            return;
        var start = which === "a" ? root.currentA.toString() : root.currentB.toString();
        var hex = Settings.pickColor(start);
        if (!hex)
            return;
        if (which === "a")
            root.currentA = hex;
        else
            root.currentB = hex;
        root._apply();
    }

    // ── body ───────────────────────────────────────────────────────────
    Column {
        anchors.fill: parent
        spacing: 0

        // Blend preview — A on the left, B on the right, the way the blend is
        // actually computed downstream.
        Rectangle {
            width: parent.width
            height: 96
            radius: Theme.radiusCard
            gradient: Gradient {
                orientation: Gradient.Horizontal
                GradientStop { position: 0.0; color: root.currentA }
                GradientStop { position: 1.0; color: root.currentB }
            }

            Text {
                anchors.centerIn: parent
                text: root.currentA.toString().toUpperCase() + "   →   " + root.currentB.toString().toUpperCase()
                color: Theme.ink
                font.family: Theme.fontFamily
                font.pixelSize: 15
                font.weight: Font.DemiBold
                style: Text.Outline
                styleColor: Theme.scrim
            }
        }

        UI.ListRow {
            label: "Color A"
            sub: root.keyA
            Row {
                spacing: 14

                Rectangle {
                    width: 44
                    height: 44
                    radius: Theme.radiusControl - 4
                    color: root.currentA
                    border.width: 1
                    border.color: Theme.outline
                }

                UI.PillButton {
                    text: "Pick A"
                    variant: "raised"
                    height: 44
                    implicitWidth: 120
                    onClicked: root._pick("a")
                }
            }
        }

        UI.ListRow {
            label: "Color B"
            sub: root.keyB
            divider: false
            Row {
                spacing: 14

                Rectangle {
                    width: 44
                    height: 44
                    radius: Theme.radiusControl - 4
                    color: root.currentB
                    border.width: 1
                    border.color: Theme.outline
                }

                UI.PillButton {
                    text: "Pick B"
                    variant: "raised"
                    height: 44
                    implicitWidth: 120
                    onClicked: root._pick("b")
                }
            }
        }
    }
}
