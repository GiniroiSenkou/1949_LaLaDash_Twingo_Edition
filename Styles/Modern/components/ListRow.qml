import QtQuick

/**
 * One settings row: label + sub-label on the left, any control on the right.
 *
 * The control is the default child, so callers write whatever they need:
 *
 *     ListRow {
 *         label: "Wi-Fi"; sub: "Wireless radio and network scan"
 *         Toggle { checked: ...; onToggled: ... }
 *     }
 *
 * Set `clickable` for value rows that open something; the whole row then
 * highlights and emits `clicked()`.
 */
Item {
    id: root

    property string label: ""
    property string sub: ""
    property color  labelColor: Theme.ink
    property bool   clickable: false
    property bool   enabled: true
    property bool   divider: true
    property int    minHeight: 64

    default property alias control: slot.data

    signal clicked()

    implicitHeight: Math.max(minHeight, Math.max(texts.implicitHeight, slot.childrenRect.height) + 28)
    implicitWidth: parent ? parent.width : 400
    opacity: enabled ? 1.0 : 0.4

    Rectangle {
        anchors.fill: parent
        anchors.leftMargin: -8
        anchors.rightMargin: -8
        radius: Theme.radiusControl
        color: ma.pressed ? Theme.surfaceRaised : "transparent"
        visible: root.clickable
        Behavior on color { ColorAnimation { duration: 110 } }
    }

    Column {
        id: texts
        anchors.left: parent.left
        anchors.right: slot.left
        anchors.rightMargin: 20
        anchors.verticalCenter: parent.verticalCenter
        spacing: 3

        Text {
            width: parent.width
            text: root.label
            color: root.labelColor
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSize("settings_primary_text")
            font.weight: Font.DemiBold
            elide: Text.ElideRight
        }
        Text {
            width: parent.width
            visible: root.sub !== ""
            text: root.sub
            color: Theme.inkFaint
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSize("settings_secondary_text")
            wrapMode: Text.WordWrap
            maximumLineCount: 2
            elide: Text.ElideRight
        }
    }

    Item {
        id: slot
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        width: childrenRect.width
        height: childrenRect.height
    }

    Rectangle {
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        height: 1
        color: Theme.hairline
        visible: root.divider
    }

    MouseArea {
        id: ma
        anchors.fill: parent
        enabled: root.clickable && root.enabled
        onClicked: root.clicked()
        z: -1
    }
}
