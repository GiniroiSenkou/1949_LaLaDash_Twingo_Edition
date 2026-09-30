import QtQuick

/**
 * Track + accent fill + accent knob. Tap anywhere to jump, drag to scrub.
 *
 * `value` is 0..1. Emits `moved` continuously while dragging and `released`
 * once on commit — the same two-signal shape the legacy ClickableSlider had,
 * so callers that only want the commit can ignore `moved`.
 */
Item {
    id: root
    property real value: 0.0
    property bool enabled: true
    property int  trackHeight: 6
    property int  knobSize: 20
    property color fillColor: Theme.accent

    signal moved(real value)
    signal released(real value)
    signal pressed(real value)

    implicitWidth: 200
    implicitHeight: Math.max(knobSize, trackHeight) + 8
    opacity: enabled ? 1.0 : 0.4

    readonly property real _clamped: Math.max(0, Math.min(1, value))

    Rectangle {
        id: track
        anchors.verticalCenter: parent.verticalCenter
        width: parent.width
        height: root.trackHeight
        radius: height / 2
        color: Theme.surfaceHover

        Rectangle {
            width: parent.width * root._clamped
            height: parent.height
            radius: parent.radius
            color: root.fillColor
        }
    }

    Rectangle {
        id: knob
        width: root.knobSize
        height: root.knobSize
        radius: width / 2
        color: root.fillColor
        anchors.verticalCenter: parent.verticalCenter
        x: Math.max(0, Math.min(root.width - width, root.width * root._clamped - width / 2))
    }

    MouseArea {
        id: ma
        anchors.fill: parent
        enabled: root.enabled
        preventStealing: true

        function _at(mx) {
            return Math.max(0, Math.min(1, mx / Math.max(1, root.width)));
        }

        onPressed: function (mouse) {
            root.value = _at(mouse.x);
            root.pressed(root.value);
            root.moved(root.value);
        }
        onPositionChanged: function (mouse) {
            if (!pressed) return;
            root.value = _at(mouse.x);
            root.moved(root.value);
        }
        onReleased: root.released(root.value)
    }
}
