import QtQuick

/**
 * Segmented choice — a raised trough holding one pill per option, the selected
 * one filled with the accent. Used for presets, resolution, CAN bitrate, and
 * every "choice" row in Settings.
 *
 * `model` may be plain strings or objects; `labelKey`/`valueKey` name the fields
 * when they are objects.
 */
Rectangle {
    id: root

    property var    model: []
    property string labelKey: "label"
    property string valueKey: "value"
    property var    currentValue: undefined
    property int    segmentHeight: 40
    property int    fontSize: Theme.fontSize("buttons_multiselect_text")
    /** Allow clicking the selected segment again to clear the selection. */
    property bool   clearable: false

    signal selected(var value)

    function _label(item) {
        return (item && typeof item === "object") ? item[labelKey] : item;
    }
    function _value(item) {
        return (item && typeof item === "object") ? item[valueKey] : item;
    }

    implicitWidth: row.implicitWidth + 8
    implicitHeight: segmentHeight + 8
    radius: Theme.radiusControl
    color: Theme.surfaceRaised

    Row {
        id: row
        anchors.centerIn: parent
        spacing: 6

        Repeater {
            model: root.model

            Rectangle {
                readonly property bool active: root._value(modelData) === root.currentValue
                height: root.segmentHeight
                width: Math.max(72, label.implicitWidth + 36)
                radius: Theme.radiusControl - 4
                color: active
                       ? (ma.pressed ? Theme.accentPressed : (ma.containsMouse ? Theme.accentHover : Theme.accent))
                       : (ma.pressed ? Theme.surfacePressed : (ma.containsMouse ? Theme.surfaceHover : "transparent"))
                Behavior on color { ColorAnimation { duration: 110 } }

                Text {
                    id: label
                    anchors.centerIn: parent
                    text: root._label(modelData)
                    color: parent.active ? Theme.accentInk : Theme.inkDim
                    font.family: Theme.fontFamily
                    font.pixelSize: root.fontSize
                    font.weight: Font.DemiBold
                }

                MouseArea {
                    id: ma
                    anchors.fill: parent
                    hoverEnabled: true
                    onClicked: {
                        if (parent.active && root.clearable)
                            root.selected("");
                        else
                            root.selected(root._value(modelData));
                    }
                }
            }
        }
    }
}
