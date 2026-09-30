import QtQuick

/**
 * Instrument-cluster dial — a radial tick ladder.
 *
 * The lit sweep is made of the tick marks themselves rather than a drawn arc,
 * widened until it reads as a filled band (`filled`) and ramped in brightness
 * along the sweep (`litBoost`) rather than filled flat, with a single rotated
 * marker at the current value (`showHead`) standing in for a needle.
 *
 * That shape is the only one that is free on a Pi 4B: the ticks are created
 * once and never move, so a telemetry tick changes `width`, `color` and
 * `opacity` on N Rectangles and rotates one marker — nothing re-lays out.
 * A `Canvas` arc would repaint the whole gauge at the telemetry rate — the
 * exact "render thread never sleeps" bug this skin exists to avoid.
 *
 * Zone thresholds are in the value's own units so they can be fed straight
 * from `Telemetry.gaugeBand(<metric>)`; see LedBar for the same contract.
 * `_zoneColor` uses each tick's upper edge, so the last lit tick is never a
 * milder colour than the digits reading the same value.
 *
 * Angles are clock-style degrees: 0 is straight up, growing clockwise. The
 * default sweep runs from lower-left round to lower-right, leaving the bottom
 * open for a caption.
 */
Item {
    id: root

    property real value: 0
    property real maxValue: 100
    property real warnAt:    maxValue * 0.70
    property real dangerAt:  maxValue * 0.85
    property real redlineAt: maxValue * 0.95

    /** Sweep, in clock degrees. */
    property real startAngle: -140
    property real endAngle:    140

    property int  tickCount: 61
    /** Every Nth tick is long, and carries a number when `labels` has one. */
    property int  majorEvery: 10
    /** Numbers drawn just inside the ring, in dial order. Empty = none. */
    property var  labels: []

    /** Colour of the lit sweep below the warn threshold. Zones override it
        above that, so a redline still reads red however this is set. */
    property color litColor: Theme.accent
    property real  dimOpacity: 0.20

    /** Lit ticks widen and lengthen until the swept part reads as a solid
        band rather than a ladder of hairlines. A thin blue ladder is legible
        as a scale but says very little about *where* the needle is, which is
        the one thing a dial is for. */
    property bool  filled: true
    /** How much brighter the lit sweep is than its own zone colour. Kept
        modest: past about 1.2 the accent blue washes out to near-white and the
        band stops reading as a colour at all. */
    property real  litBoost: 1.18
    /** A brighter, longer marker at the current value. This is the "needle":
        one rotated Item, so a telemetry update moves one item rather than
        repainting anything. */
    property bool  showHead: true

    readonly property real _fillDepth: filled ? Math.round(radius * 0.11) : 0
    /** Arc length one tick occupies, so a filled sweep has no gaps in it. */
    readonly property real _bandWidth: {
        if (!filled) return minorWidth;
        var sweep = Math.abs(endAngle - startAngle) / 360;
        var arc = 2 * Math.PI * Math.max(1, radius - minorLength) * sweep;
        return Math.max(minorWidth, arc / Math.max(1, tickCount - 1) + 0.6);
    }

    property int minorLength: Math.round(radius * 0.07)
    property int majorLength: Math.round(radius * 0.15)
    property int minorWidth: 2
    property int majorWidth: 4

    readonly property real radius: Math.min(width, height) / 2
    readonly property int  _lit: {
        if (maxValue <= 0 || tickCount < 2) return 0;
        var f = Math.max(0, Math.min(1, value / maxValue));
        return Math.ceil(f * (tickCount - 1));
    }

    function _zoneColor(i) {
        var v = (i / Math.max(1, tickCount - 1)) * maxValue;
        if (v >= redlineAt) return Theme.danger;
        if (v >= dangerAt)  return Theme.statusOrange;
        if (v >= warnAt)    return Theme.warn;
        return root.litColor;
    }

    /** Colour of tick `i` while lit: its zone colour, brightened along the
        sweep so the band is a ramp rather than one flat fill — dim where the
        dial starts, hot where it ends.

        Deliberately a function of `i` alone and not of the current value: a
        gradient that tracked the head would put all 61 colour bindings on the
        telemetry signal, which is ~1200 colour computations a second across
        the two dials on a Pi 4B. This way each tick computes its colour once
        and only re-runs when it crosses the lit threshold. */
    function _litColorAt(i) {
        var f = i / Math.max(1, tickCount - 1);
        return Qt.lighter(_zoneColor(i), 1 + (litBoost - 1) * (0.30 + 0.70 * f));
    }

    /** Same idea for weight: the sweep fades in from its start. */
    function _litOpacityAt(i) {
        var f = i / Math.max(1, tickCount - 1);
        return 0.62 + 0.38 * f;
    }

    function _angleAt(i) {
        return startAngle + (endAngle - startAngle) * (i / Math.max(1, tickCount - 1));
    }

    // ── ticks ───────────────────────────────────────────────────────────
    Repeater {
        model: root.tickCount

        Item {
            // A full-size item rotated about the centre: the tick is placed at
            // the top of it, so the rotation alone puts it on the ring. No
            // per-tick trigonometry, and nothing to recompute when a value
            // changes.
            width: root.width
            height: root.height
            transformOrigin: Item.Center
            rotation: root._angleAt(index)

            readonly property bool major: (index % root.majorEvery) === 0
            // One boolean per tick — the same rebind cost the opacity binding
            // already had, and every size and colour below keys off it.
            readonly property bool lit: index <= root._lit

            Rectangle {
                width: parent.major ? root.majorWidth
                     : (parent.lit ? root._bandWidth : root.minorWidth)
                height: parent.major ? root.majorLength
                      : (parent.lit ? root.minorLength + root._fillDepth : root.minorLength)
                radius: parent.lit && !parent.major ? 0 : width / 2
                x: (root.width - width) / 2
                y: root.height / 2 - root.radius
                color: parent.lit ? root._litColorAt(index) : root._zoneColor(index)
                opacity: parent.lit ? root._litOpacityAt(index) : root.dimOpacity
            }
        }
    }

    // ── numbers ─────────────────────────────────────────────────────────
    Repeater {
        model: root.labels

        Text {
            readonly property real ang: root._angleAt(index * root.majorEvery)
            readonly property real rad: (ang - 90) * Math.PI / 180
            readonly property real ring: root.radius - root.majorLength - height * 0.9

            text: modelData
            color: Theme.inkDim
            font.family: Theme.fontFamily
            font.pixelSize: Math.max(11, Math.round(root.radius * 0.13))
            font.weight: Font.DemiBold
            x: root.width  / 2 + ring * Math.cos(rad) - width  / 2
            y: root.height / 2 + ring * Math.sin(rad) - height / 2
        }
    }

    // ── head marker ─────────────────────────────────────────────────────
    // Drawn after the ticks so it sits over them. Its rotation is the only
    // thing that moves when the value changes.
    Item {
        visible: root.showHead && root.value > 0
        width: root.width
        height: root.height
        transformOrigin: Item.Center
        rotation: root._angleAt(root._lit)
        z: 1

        // Halo first, marker over it: two plain Rectangles, no effects.
        Rectangle {
            width: root.majorWidth * 3.2
            height: root.majorLength + root._fillDepth + root.radius * 0.05
            radius: width / 2
            x: (root.width - width) / 2
            y: root.height / 2 - root.radius - root.radius * 0.02
            color: root._litColorAt(root._lit)
            opacity: 0.26
        }

        Rectangle {
            width: root.majorWidth * 1.4
            height: root.majorLength + root._fillDepth
            radius: width / 2
            x: (root.width - width) / 2
            y: root.height / 2 - root.radius
            color: Qt.lighter(root._litColorAt(root._lit), 1.25)
        }
    }

    /** Centre content goes here — the numeral, a unit, whatever the dial is
        wrapped around. */
    default property alias content: centre.data

    Item {
        id: centre
        anchors.centerIn: parent
        width:  root.radius * 1.25
        height: root.radius * 1.25
    }
}
