import QtQuick

/** Switch. Accent track when on, neutral when off — as mocked. */
Item {
    id: root
    property bool checked: false
    property bool enabled: true
    signal toggled(bool value)

    implicitWidth: 56
    implicitHeight: 32
    opacity: enabled ? 1.0 : 0.4

    Rectangle {
        id: track
        anchors.fill: parent
        radius: height / 2
        color: root.checked ? Theme.accent : Theme.surfaceHover
        Behavior on color { ColorAnimation { duration: 120 } }

        Rectangle {
            id: knob
            width: parent.height - 6
            height: width
            radius: width / 2
            y: 3
            x: root.checked ? parent.width - width - 3 : 3
            // The knob stays light in both skins: on the light theme an
            // ink-coloured (near-black) knob read as a different control.
            color: root.checked ? Theme.accentInk : (Theme.lightTheme ? Theme.surface : Theme.ink)
            border.width: !root.checked && Theme.lightTheme ? 1 : 0
            border.color: Theme.outline
            Behavior on x { NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }
            Behavior on color { ColorAnimation { duration: 120 } }
        }
    }

    MouseArea {
        anchors.fill: parent
        enabled: root.enabled
        onClicked: {
            root.checked = !root.checked;
            root.toggled(root.checked);
        }
    }
}
