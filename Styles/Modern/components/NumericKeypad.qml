import QtQuick

/**
 * On-screen numeric pad — 0-9, a decimal point, backspace, Enter.
 *
 * `TouchKeyboard` (the QWERTY pad used for Wi-Fi passwords) buries its digits
 * inside a punctuation-heavy "symbols" layer, which is the wrong shape for
 * entering a single number like the car's odometer reading. This follows the
 * same stateless-emitter contract so call sites read the same way:
 *
 *     UI.NumericKeypad {
 *         onDigitPressed: function (ch) { field.text += ch; }
 *         onBackspace:    field.text = field.text.slice(0, -1)
 *         onSubmitted:    page.save()
 *     }
 *
 * Same 52 px key height as TouchKeyboard, for the same reason: comfortable at
 * arm's length in a moving car.
 */
Item {
    id: root

    /** Emitted with "0".."9" or ".". */
    signal digitPressed(string text)
    signal backspace()
    signal submitted()

    property string enterLabel: "Save"
    property bool enterEnabled: true
    /** Hide the "." key for integer-only fields (e.g. nothing here needs it
        today, but a future caller might). */
    property bool allowDecimal: true

    readonly property int keyH: 52
    readonly property int keyGap: 8

    implicitHeight: grid.implicitHeight + keyGap + keyH

    component Key: Rectangle {
        id: key
        property string label: ""
        property bool accent: false
        property bool keyEnabled: true
        property int fontSize: 22
        signal activated()

        radius: Theme.radiusControl
        color: !key.keyEnabled ? Theme.surface
             : keyArea.pressed ? Theme.surfacePressed
             : key.accent ? Theme.accentSoft
             : Theme.surfaceRaised
        opacity: key.keyEnabled ? 1.0 : 0.4

        Text {
            anchors.centerIn: parent
            text: key.label
            color: Theme.ink
            font.family: Theme.fontFamily
            font.pixelSize: key.fontSize
            font.weight: Font.DemiBold
        }

        MouseArea {
            id: keyArea
            anchors.fill: parent
            enabled: key.keyEnabled
            onClicked: key.activated()
        }
    }

    Grid {
        id: grid
        anchors.left: parent.left
        anchors.right: parent.right
        columns: 3
        columnSpacing: root.keyGap
        rowSpacing: root.keyGap

        readonly property int cellW: Math.floor((width - 2 * columnSpacing) / 3)

        Repeater {
            // 1-9, then "." (or blank), 0, backspace — the familiar phone-pad
            // layout rather than a straight 0-9 run, so it's recognisable at
            // a glance.
            model: ["1","2","3","4","5","6","7","8","9",
                    root.allowDecimal ? "." : "", "0", "⌫"]

            Key {
                width: grid.cellW
                height: root.keyH
                label: modelData
                keyEnabled: modelData !== ""
                onActivated: {
                    if (modelData === "⌫") root.backspace();
                    else if (modelData !== "") root.digitPressed(modelData);
                }
            }
        }
    }

    Key {
        anchors.top: grid.bottom
        anchors.topMargin: root.keyGap
        anchors.left: parent.left
        anchors.right: parent.right
        height: root.keyH
        label: root.enterLabel
        fontSize: 18
        accent: true
        keyEnabled: root.enterEnabled
        onActivated: root.submitted()
    }
}
