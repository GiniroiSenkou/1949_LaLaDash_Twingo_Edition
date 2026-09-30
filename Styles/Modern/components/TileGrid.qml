import QtQuick

/**
 * Grid of badge tiles — music tags, car brands, car models, sound folders.
 *
 * `fitToPage` is the important mode: it solves for the column count that makes
 * every item fit in the available area at the largest possible tile size, so the
 * user picks by tapping instead of dragging. That is a touchscreen in a car —
 * scrolling a grid while parked is fine, scrolling one while driving is not.
 *
 * Items may be strings or objects with any of: label, count, icon, initial.
 * When no icon resolves the tile falls back to the label's first letter.
 */
Item {
    id: root

    property var    model: []
    property var    currentValue: undefined
    property string labelKey: "label"
    property string valueKey: "value"
    /** Solve columns/rows so everything fits; no scrolling. */
    property bool   fitToPage: true
    /** Used only when fitToPage is false. */
    property int    columns: 8
    property int    spacing: 8
    property bool   showCount: true
    property int    maxBadge: 150
    property int    minBadge: 44
    /** Icon inset as a fraction of the badge. Small by design — the source
        images already carry their own padding. */
    property real   iconInset: 0.02
    /** Deprecated: pre-fit-to-page callers set a fixed badge size. Treated as
        an upper bound so those call sites keep working. */
    property int    badgeSize: 0
    onBadgeSizeChanged: if (badgeSize > 0) maxBadge = badgeSize

    signal picked(var value)

    readonly property int count: model ? model.length : 0

    // ── solve the layout ───────────────────────────────────────────────
    // Try every column count and keep the one that yields the biggest badge.
    readonly property var _solved: {
        var n = count;
        if (n <= 0 || width <= 0 || height <= 0)
            return { cols: 1, badge: minBadge, tileW: 100, tileH: 100 };

        if (!fitToPage) {
            var c = Math.max(1, columns);
            var tw = (width - spacing * (c - 1)) / c;
            var b = Math.max(minBadge, Math.min(maxBadge, tw * 0.72));
            return { cols: c, badge: b, tileW: tw, tileH: b + _chromeH(b) };
        }

        var best = null;
        for (var cols = 1; cols <= n; cols++) {
            var rows = Math.ceil(n / cols);
            var tileW = (width - spacing * (cols - 1)) / cols;
            var tileH = (height - spacing * (rows - 1)) / rows;
            if (tileW < minBadge || tileH < minBadge)
                continue;
            // Badge is limited by width, and by height once labels are removed.
            var badge = Math.min(tileW * 0.82, maxBadge);
            for (var i = 0; i < 4; i++)      // settle badge vs its own chrome
                badge = Math.min(tileW * 0.82, maxBadge, tileH - _chromeH(badge));
            if (badge < minBadge)
                continue;
            if (best === null || badge > best.badge)
                best = { cols: cols, badge: badge, tileW: tileW, tileH: tileH };
        }
        if (best === null) {
            var fc = Math.max(1, Math.ceil(Math.sqrt(n * width / Math.max(1, height))));
            var ftw = (width - spacing * (fc - 1)) / fc;
            return { cols: fc, badge: minBadge, tileW: ftw, tileH: minBadge + _chromeH(minBadge) };
        }
        return best;
    }

    /** Vertical space a tile needs on top of its badge (label + count). */
    function _chromeH(badge) {
        var label = Math.max(13, Math.min(19, badge * 0.20));
        return label * 2.5 + (showCount ? label * 1.1 : 0) + 12;
    }

    function _f(item, key, fallback) {
        if (item && typeof item === "object" && item[key] !== undefined)
            return item[key];
        return fallback;
    }

    Grid {
        id: grid
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.verticalCenter: parent.verticalCenter
        columns: root._solved.cols
        spacing: root.spacing

        Repeater {
            model: root.model

            Item {
                id: tile
                readonly property var    itemValue: root._f(modelData, root.valueKey, modelData)
                readonly property string itemLabel: String(root._f(modelData, root.labelKey, modelData))
                readonly property string itemIcon: String(root._f(modelData, "icon", ""))
                readonly property var    itemCount: root._f(modelData, "count", "")
                readonly property bool   active: itemValue === root.currentValue
                readonly property real   badge: root._solved.badge
                readonly property real   labelSize: Math.max(13, Math.min(19, badge * 0.20))

                width: root._solved.tileW
                height: badge + root._chromeH(badge)

                Rectangle {
                    anchors.fill: parent
                    radius: 16
                    color: tile.active ? Theme.accentSoft
                         : (ma.pressed ? Theme.surfaceHover
                         : (ma.containsMouse ? Theme.surface : "transparent"))
                    Behavior on color { ColorAnimation { duration: 100 } }
                }

                Column {
                    anchors.top: parent.top
                    anchors.topMargin: 4
                    anchors.horizontalCenter: parent.horizontalCenter
                    width: parent.width - 6
                    spacing: 3

                    Rectangle {
                        width: tile.badge
                        height: tile.badge
                        radius: width / 2
                        anchors.horizontalCenter: parent.horizontalCenter
                        color: Theme.surfaceRaised
                        border.width: tile.active ? 2 : 1
                        border.color: tile.active ? Theme.accent : Theme.hairline
                        clip: true

                        Image {
                            id: badgeIcon
                            anchors.fill: parent
                            // Nearly edge-to-edge: these are logos and car
                            // renders with their own whitespace baked in, so a
                            // generous inset here reads as a mostly-empty tile.
                            anchors.margins: Math.max(1, tile.badge * root.iconInset)
                            source: tile.itemIcon
                            sourceSize.width: 192
                            sourceSize.height: 192
                            fillMode: Image.PreserveAspectFit
                            asynchronous: true
                            visible: tile.itemIcon !== "" && status === Image.Ready
                        }

                        Text {
                            anchors.centerIn: parent
                            visible: !badgeIcon.visible
                            text: tile.itemLabel.replace(/[^A-Za-z0-9]/g, "").slice(0, 1).toUpperCase()
                            color: tile.active ? Theme.accent : Theme.inkFaint
                            font.family: Theme.fontFamily
                            font.pixelSize: Math.max(16, tile.badge * 0.38)
                            font.weight: Font.Bold
                        }
                    }

                    Text {
                        width: parent.width
                        text: tile.itemLabel
                        color: tile.active ? Theme.accent : Theme.ink
                        font.family: Theme.fontFamily
                        font.pixelSize: tile.labelSize
                        font.weight: Font.DemiBold
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.WordWrap
                        maximumLineCount: 2
                        elide: Text.ElideRight
                        lineHeight: 1.1
                    }

                    Text {
                        width: parent.width
                        visible: root.showCount && String(tile.itemCount) !== ""
                        text: tile.itemCount
                        color: Theme.inkDim
                        font.family: Theme.fontFamily
                        font.pixelSize: Math.max(11, tile.labelSize * 0.8)
                        horizontalAlignment: Text.AlignHCenter
                    }
                }

                MouseArea {
                    id: ma
                    anchors.fill: parent
                    hoverEnabled: true
                    onClicked: root.picked(tile.itemValue)
                }
            }
        }
    }
}
