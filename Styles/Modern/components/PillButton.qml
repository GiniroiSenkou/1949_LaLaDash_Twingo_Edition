import QtQuick

/**
 * The modern skin's one button.
 *
 * variant:  "raised"  surfaceRaised fill, ink text        (default)
 *           "accent"  accent fill, accentInk text
 *           "ghost"   transparent, ink text
 *           "outline" transparent + hairline border
 *           "danger"  danger-tinted fill, danger text
 *
 * `checked` promotes any variant to the accent fill, which is how the mockup
 * shows selection everywhere (dock pills, presets, segmented choices).
 *
 * Hover and pressed are explicit palette steps rather than computed tints, so
 * they stay selectable from settings.
 */
Rectangle {
    id: btn

    property string text: ""
    property string iconSource: ""
    property int    iconSize: 22
    property string variant: "raised"
    property bool   checked: false
    property bool   enabled: true
    property int    fontSize: Theme.fontSize("buttons_medium_text")
    property int    fontWeight: Font.DemiBold
    property color  textColor: _ink
    property bool   busy: false

    signal clicked()
    signal pressAndHold()

    implicitWidth: Math.max(row.implicitWidth + 40, 44)
    implicitHeight: 44
    radius: Theme.radiusPill
    opacity: enabled ? 1.0 : 0.4

    readonly property bool _accent: checked || variant === "accent"
    readonly property color _ink: {
        if (_accent) return Theme.accentInk;
        if (variant === "danger") return Theme.dangerSoft;
        return Theme.ink;
    }

    color: {
        if (!enabled) return variant === "ghost" ? "transparent" : Theme.surfaceRaised;
        if (_accent)
            return ma.pressed ? Theme.accentPressed : (ma.containsMouse ? Theme.accentHover : Theme.accent);
        if (variant === "danger")
            return ma.pressed ? Qt.darker(Theme.dangerBg, 1.2) : Theme.dangerBg;
        if (variant === "ghost" || variant === "outline")
            return ma.pressed ? Theme.surfaceRaised : (ma.containsMouse ? Theme.surface : "transparent");
        return ma.pressed ? Theme.surfacePressed : (ma.containsMouse ? Theme.surfaceHover : Theme.surfaceRaised);
    }

    border.width: variant === "outline" && !_accent ? 1 : 0
    border.color: Theme.outline

    Behavior on color { ColorAnimation { duration: 110 } }

    Row {
        id: row
        anchors.centerIn: parent
        spacing: btn.text !== "" && btn.iconSource !== "" ? 10 : 0

        Image {
            visible: btn.iconSource !== ""
            source: btn.iconSource
            width: btn.iconSize
            height: btn.iconSize
            sourceSize.width: btn.iconSize * 2
            sourceSize.height: btn.iconSize * 2
            fillMode: Image.PreserveAspectFit
            asynchronous: true
            anchors.verticalCenter: parent.verticalCenter
            onStatusChanged: if (status === Image.Error) visible = false
        }

        Text {
            visible: btn.text !== ""
            text: btn.text
            color: btn.textColor
            font.family: Theme.fontFamily
            font.pixelSize: btn.fontSize
            font.weight: btn.fontWeight
            anchors.verticalCenter: parent.verticalCenter
            horizontalAlignment: Text.AlignHCenter
        }
    }

    MouseArea {
        id: ma
        anchors.fill: parent
        hoverEnabled: true
        enabled: btn.enabled
        onClicked: btn.clicked()
        onPressAndHold: btn.pressAndHold()
    }
}
