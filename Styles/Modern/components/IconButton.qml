import QtQuick

/**
 * Square soft-cornered button holding an icon or a glyph.
 * Used by the dock (settings key, alerts, mute, engine) and media transports.
 */
Rectangle {
    id: btn

    property string iconSource: ""
    property string glyph: ""
    property int    iconSize: 24
    property bool   checked: false
    property bool   enabled: true
    property color  activeColor: Theme.accent
    property color  glyphColor: checked ? Theme.accentInk : Theme.ink
    property int    glyphSize: 20

    signal clicked()
    signal pressAndHold()

    implicitWidth: 60
    implicitHeight: 56
    radius: 16
    opacity: enabled ? 1.0 : 0.4

    color: checked
           ? (ma.pressed ? Qt.darker(activeColor, 1.15) : activeColor)
           : (ma.pressed ? Theme.surfacePressed : (ma.containsMouse ? Theme.surfaceHover : Theme.surface))
    Behavior on color { ColorAnimation { duration: 110 } }

    Image {
        anchors.centerIn: parent
        visible: btn.iconSource !== "" && status === Image.Ready
        source: btn.iconSource
        width: btn.iconSize
        height: btn.iconSize
        sourceSize.width: btn.iconSize * 2
        sourceSize.height: btn.iconSize * 2
        fillMode: Image.PreserveAspectFit
        asynchronous: true
    }

    Text {
        anchors.centerIn: parent
        visible: btn.glyph !== ""
        text: btn.glyph
        color: btn.glyphColor
        font.family: Theme.fontFamily
        font.pixelSize: btn.glyphSize
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
