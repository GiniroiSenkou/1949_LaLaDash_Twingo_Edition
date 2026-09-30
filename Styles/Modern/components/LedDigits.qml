import QtQuick

/**
 * Digits on an 80s LED panel: each glyph sits in its own fixed-width cell,
 * with a soft shadow of itself behind it.
 *
 * The per-cell layout is not decoration. Manrope has no tabular figures and
 * Qt 6.4 has no `font.features`, so a plain Text jitters horizontally as the
 * digits change. One cell per character fixes that, and is what makes the
 * readout hold still at speed.
 *
 * The shadow is a copy of the *same* character, slightly larger and faint — a
 * halo, not a second reading. An earlier version drew an unlit "8" behind every
 * cell and padded short values with more of them to hold a fixed width, which
 * turned 800 rpm into something that read as 8800.
 *
 * Cost: one Repeater over the character count (never more than a handful), no
 * timers, no animation. Rebinds only when the value or colour changes.
 */
Row {
    id: root

    /** The characters to show. Non-digits (":", ".", "-") are laid out too. */
    property string text: ""
    property int    pixelSize: 64
    property color  color: Theme.ink
    /** Strength of the glyph's own shadow. Set against the ground colour
        rather than by eye: much below this it is invisible on the panel, and
        much above it stops reading as a shadow and starts reading as a second
        digit behind the first. */
    property real   shadowOpacity: 0.28
    /** How much larger the shadow is than the glyph — this is what makes it
        show as a halo around the edges instead of hiding exactly behind. */
    property real   shadowScale: 1.12
    property bool   showShadow: true

    spacing: Math.round(pixelSize * 0.04)

    Repeater {
        model: root.text.length

        Item {
            readonly property string ch: root.text.charAt(index)
            // ":" and "." are narrow on a real panel; digits get a full cell.
            readonly property bool narrow: ch === ":" || ch === "." || ch === " "
            width: narrow ? Math.round(root.pixelSize * 0.28)
                          : Math.round(root.pixelSize * 0.62)
            height: Math.round(root.pixelSize * 1.18)

            Text {
                anchors.centerIn: parent
                visible: root.showShadow && parent.ch !== " "
                text: parent.ch
                color: root.color
                opacity: root.shadowOpacity
                scale: root.shadowScale
                font.family: Theme.fontFamily
                font.pixelSize: root.pixelSize
                font.weight: Font.Bold
            }

            Text {
                anchors.centerIn: parent
                text: parent.ch
                color: root.color
                font.family: Theme.fontFamily
                font.pixelSize: root.pixelSize
                font.weight: Font.Bold
            }
        }
    }
}
