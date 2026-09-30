import QtQuick

/**
 * On-screen keyboard for a car touchscreen.
 *
 * The Pi runs labwc with no desktop shell, so there is no system keyboard to
 * fall back on — this is the only way to type a Wi-Fi password on the device.
 *
 * Emits characters rather than editing anything itself, so the caller keeps
 * ownership of the text it is building:
 *
 *     UI.TouchKeyboard {
 *         onKeyPressed: function (ch) { field.text += ch; }
 *         onBackspace:  field.text = field.text.slice(0, -1)
 *         onSubmitted:  page.connect()
 *     }
 *
 * Keys are 52 px tall with 6 px gaps, which is comfortable at arm's length in
 * a moving car and still leaves room for a field and a network list at 800 px.
 */
Item {
    id: root

    /** Emitted with the character to append. */
    signal keyPressed(string text)
    /** Emitted on the ⌫ key. */
    signal backspace()
    /** Emitted on the Enter key. */
    signal submitted()
    property string enterLabel: "Enter"
    property bool enterEnabled: true

    /** "letters" | "symbols" — not `layer`: Item.layer is FINAL. */
    property string keyLayer: "letters"
    property bool shifted: false
    /** Latched by holding Shift; survives key presses until tapped off. */
    property bool capsLock: false

    readonly property int keyH: 52
    readonly property int keyGap: 6

    readonly property var letterRows: [
        ["q", "w", "e", "r", "t", "y", "u", "i", "o", "p"],
        ["a", "s", "d", "f", "g", "h", "j", "k", "l"],
        ["z", "x", "c", "v", "b", "n", "m"]
    ]

    readonly property var symbolRows: [
        ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"],
        ["-", "/", ":", ";", "(", ")", "€", "&", "@"],
        [".", ",", "?", "!", "'", "\"", "#", "%", "*", "+", "="]
    ]

    readonly property var activeRows: keyLayer === "symbols" ? symbolRows : letterRows

    implicitHeight: keyColumn.implicitHeight

    function _emit(ch) {
        var out = ch;
        if (root.keyLayer === "letters" && (root.shifted || root.capsLock))
            out = ch.toUpperCase();
        root.keyPressed(out);
        // Shift is one-shot unless it was latched.
        if (root.shifted && !root.capsLock)
            root.shifted = false;
    }

    // ── one key ──────────────────────────────────────────────────────────
    component Key: Rectangle {
        id: key
        property string label: ""
        property bool accent: false
        property bool active: false
        property bool keyEnabled: true
        property int fontSize: 20
        signal activated()
        signal held()

        height: root.keyH
        radius: Theme.radiusControl
        color: !key.keyEnabled ? Theme.surface
             : key.active ? Theme.accent
             : keyArea.pressed ? Theme.surfacePressed
             : key.accent ? Theme.accentSoft
             : Theme.surfaceRaised
        opacity: key.keyEnabled ? 1.0 : 0.4

        Text {
            anchors.centerIn: parent
            text: key.label
            color: key.active ? Theme.accentInk : Theme.ink
            font.family: Theme.fontFamily
            font.pixelSize: key.fontSize
            font.weight: Font.DemiBold
        }

        MouseArea {
            id: keyArea
            anchors.fill: parent
            enabled: key.keyEnabled
            onClicked: key.activated()
            onPressAndHold: key.held()
        }
    }

    Column {
        id: keyColumn
        anchors.left: parent.left
        anchors.right: parent.right
        spacing: root.keyGap

        // Letter/symbol rows. Each row is centred and sized from the widest
        // row so the keys line up into a grid instead of drifting per row.
        Repeater {
            model: root.activeRows

            Row {
                property var rowKeys: modelData
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: root.keyGap

                Repeater {
                    model: parent.rowKeys
                    Key {
                        // 11 columns is the widest row in either layer.
                        width: Math.floor((keyColumn.width - 10 * root.keyGap) / 11)
                        label: root.keyLayer === "letters" && (root.shifted || root.capsLock)
                               ? modelData.toUpperCase() : modelData
                        onActivated: root._emit(modelData)
                    }
                }
            }
        }

        // ── modifier row ──────────────────────────────────────────────────
        Row {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: root.keyGap

            Key {
                width: Math.floor(keyColumn.width * 0.14)
                label: root.capsLock ? "⇪" : "⇧"
                fontSize: 22
                visible: root.keyLayer === "letters"
                active: root.shifted || root.capsLock
                // Tap = one capital, hold = caps lock. A double-tap latch is
                // unreliable on a touch panel in a moving car.
                onActivated: {
                    if (root.capsLock) {
                        root.capsLock = false;
                        root.shifted = false;
                    } else {
                        root.shifted = !root.shifted;
                    }
                }
                onHeld: {
                    root.capsLock = true;
                    root.shifted = false;
                }
            }

            Key {
                width: Math.floor(keyColumn.width * 0.14)
                label: root.keyLayer === "symbols" ? "abc" : "?123"
                fontSize: 17
                onActivated: {
                    root.keyLayer = root.keyLayer === "symbols" ? "letters" : "symbols";
                    root.shifted = false;
                }
            }

            Key {
                width: Math.floor(keyColumn.width * 0.30)
                label: "space"
                fontSize: 16
                onActivated: root.keyPressed(" ")
            }

            Key {
                width: Math.floor(keyColumn.width * 0.14)
                label: "⌫"
                fontSize: 22
                onActivated: root.backspace()
            }

            Key {
                width: Math.floor(keyColumn.width * 0.22)
                label: root.enterLabel
                fontSize: 17
                accent: true
                keyEnabled: root.enterEnabled
                onActivated: root.submitted()
            }
        }
    }
}
