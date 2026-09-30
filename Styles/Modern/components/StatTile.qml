import QtQuick

/**
 * Label above a large value with an optional unit — the readout unit of the
 * dashboard and home stat grids.
 *
 * `hero: true` switches to the mockup's inline form (small caps label left,
 * oversized number pushed right) used for the RPM / Speed pair.
 */
Rectangle {
    id: tile

    property string label: ""
    property string value: "--"
    property string unit: ""
    property color  valueColor: Theme.ink
    property bool   hero: false
    property int    valueSize: hero ? 74 : 40
    property string iconSource: ""

    /** Inner padding of the stacked form: 16 px on a full-width tile, down to
        10 px on the Dashboard's narrow columns. */
    readonly property int _inset: Math.max(10, Math.min(16, Math.round(width * 0.11)))

    color: Theme.surface
    radius: Theme.radiusCard
    implicitHeight: hero ? 92 : 92

    // ── inline hero form ───────────────────────────────────────────────
    Item {
        anchors.fill: parent
        anchors.leftMargin: 24
        anchors.rightMargin: 24
        visible: tile.hero

        Text {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: tile.label.toUpperCase()
            color: Theme.inkDim
            font.family: Theme.fontFamily
            font.pixelSize: 13
            font.weight: Font.DemiBold
            font.letterSpacing: 1.3
        }

        Row {
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: 8

            Text {
                text: tile.value
                color: tile.valueColor
                font.family: Theme.fontFamily
                font.pixelSize: tile.valueSize
                font.weight: Font.Bold
                anchors.bottom: parent.bottom
            }
            Text {
                visible: tile.unit !== ""
                text: tile.unit
                color: Theme.inkFaint
                font.family: Theme.fontFamily
                font.pixelSize: 22
                font.weight: Font.DemiBold
                anchors.bottom: parent.bottom
                anchors.bottomMargin: tile.valueSize * 0.12
            }
        }
    }

    // ── stacked form ───────────────────────────────────────────────────
    // Sized to fit rather than to overflow: the Dashboard's readout columns
    // narrow to ~100 px with the side panel open, and at that width a fixed
    // 16 px inset plus a 38 px "N/A kPa" ran past the tile's right edge while
    // the label was clipped to "THROTT". Label and value both shrink to the
    // width they are given (HorizontalFit) before anything gets elided, the
    // inset scales with the tile, and the block is centred vertically so the
    // value no longer sits on the bottom edge of a short tile.
    Column {
        id: stack
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        anchors.leftMargin: tile._inset
        anchors.rightMargin: tile._inset
        visible: !tile.hero
        spacing: 2

        Row {
            id: labelRow
            width: parent.width
            spacing: 6
            Image {
                id: labelIcon
                visible: tile.iconSource !== "" && status === Image.Ready
                source: tile.iconSource
                width: 16; height: 16
                sourceSize.width: 32; sourceSize.height: 32
                fillMode: Image.PreserveAspectFit
                asynchronous: true
                anchors.verticalCenter: parent.verticalCenter
            }
            Text {
                width: parent.width - (labelIcon.visible ? labelIcon.width + parent.spacing : 0)
                text: tile.label.toUpperCase()
                color: Theme.inkDim
                font.family: Theme.fontFamily
                font.pixelSize: 12
                font.weight: Font.DemiBold
                font.letterSpacing: 1.0
                fontSizeMode: Text.HorizontalFit
                minimumPixelSize: 9
                elide: Text.ElideRight
                anchors.verticalCenter: parent.verticalCenter
            }
        }

        Row {
            width: parent.width
            spacing: 6
            Text {
                id: valueText
                // Natural width while it fits, so the unit sits right after
                // the number; capped at what is left beside the unit, where
                // HorizontalFit shrinks the digits instead of overflowing.
                readonly property int avail: parent.width - (unitText.visible ? unitText.implicitWidth + parent.spacing : 0)
                width: Math.max(0, Math.min(implicitWidth, avail))
                text: tile.value
                color: tile.valueColor
                font.family: Theme.fontFamily
                font.pixelSize: tile.valueSize
                font.weight: Font.Bold
                fontSizeMode: Text.HorizontalFit
                minimumPixelSize: 14
            }
            Text {
                id: unitText
                visible: tile.unit !== ""
                text: tile.unit
                color: Theme.inkFaint
                font.family: Theme.fontFamily
                font.pixelSize: 16
                anchors.bottom: parent.bottom
                anchors.bottomMargin: Math.round(valueText.height * 0.16)
            }
        }
    }
}
