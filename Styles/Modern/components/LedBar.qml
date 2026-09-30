import QtQuick

/**
 * Segmented LED bargraph — the 80s dash tachometer.
 *
 * Segments light left to right up to `value`, and each segment carries the
 * colour of the zone it sits in, so the orange and red shoulders are visible
 * *before* the needle gets there. That is the whole point of the shape: a
 * driver reads how much headroom is left, not just the current number.
 *
 * Zone thresholds are given in the value's own units (`warnAt`, `dangerAt`,
 * `redlineAt`) so they can be fed straight from the telemetry bands rather
 * than re-derived here — see Modules/Obd/telemetry.py, where RPM is
 * band_higher_4(v, 5000, 5500, 5800).
 *
 * Pi cost: every segment binds to `_lit`, a single int, so a telemetry tick
 * re-evaluates one expression per segment and nothing re-lays out. No timers
 * and no animations — the value already arrives rate-limited from the bridge.
 */
Item {
    id: root

    property real value: 0
    property real maxValue: 100
    /** Value at which segments turn yellow / orange / red. */
    property real warnAt:    maxValue * 0.70
    property real dangerAt:  maxValue * 0.85
    property real redlineAt: maxValue * 0.95
    property int  segments: 32
    property real segmentGap: 3
    /** Unlit segments still show faintly, as an unlit LED does. */
    property real dimOpacity: 0.16

    /** Rev-ladder shape. 0 = a flat bar, every segment the same height.
        1 = segments climb from `rampFloor` of the height on the left to full
        height at the redline, the way a racing dash draws its shift ladder —
        the bar then reads as rising even before the colours change. */
    property real ramp: 0
    property real rampFloor: 0.45

    implicitHeight: 26

    readonly property int _lit: {
        if (maxValue <= 0) return 0;
        var f = Math.max(0, Math.min(1, value / maxValue));
        // Ceil, not round: a segment lights as soon as the value reaches any
        // part of it. Together with upper-edge zoning below this guarantees
        // the last lit segment is never a milder colour than the digits —
        // 5950 rpm showing red digits above an orange-topped bar reads as a
        // bug in the gauge.
        return Math.ceil(f * segments);
    }

    function _zoneColor(i) {
        // Upper edge of the segment: a segment counts as being in a zone the
        // moment any of it is, which is the safe direction for a tachometer.
        var v = ((i + 1) / segments) * maxValue;
        if (v >= redlineAt) return Theme.danger;
        if (v >= dangerAt)  return Theme.statusOrange;
        if (v >= warnAt)    return Theme.warn;
        return Theme.ok;
    }

    Row {
        anchors.fill: parent
        spacing: root.segmentGap

        Repeater {
            model: root.segments

            Rectangle {
                width: Math.max(2, (root.width - root.segmentGap * (root.segments - 1))
                                   / root.segments)
                height: {
                    if (root.ramp <= 0 || root.segments < 2) return root.height;
                    var t = index / (root.segments - 1);
                    var f = root.rampFloor + (1 - root.rampFloor) * t;
                    return Math.max(2, root.height * (1 - root.ramp + root.ramp * f));
                }
                // Segments sit on a common baseline so the ladder climbs.
                // Set explicitly rather than anchored: vertical anchors on a
                // Row's children fight the positioner.
                y: root.height - height
                radius: 2
                color: root._zoneColor(index)
                opacity: index < root._lit ? 1.0 : root.dimOpacity
            }
        }
    }
}
