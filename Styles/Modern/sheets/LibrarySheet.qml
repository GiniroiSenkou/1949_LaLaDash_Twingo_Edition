import QtQuick
import "../components" as UI

/**
 * Playlists — the tag/collection picker the legacy ☰ button opened.
 *
 * The legacy overlay was a hand-rolled 8-column Grid of icon + label tiles;
 * that is exactly UI.TileGrid, so the only work left here is reshaping the
 * bridge model:
 *   · Music.tagList carries a bare icon *filename* ("rock.png"); TileGrid
 *     wants a resolvable path.
 *   · count is an int; the mockup rendered "412 tracks".
 *
 * The map runs once per open (and on a library reload while open) rather than
 * on every binding evaluation.
 *
 * SHAPE — this is a picker used from the driver's seat, so it takes the whole
 * panel (`fullScreen`) with no header (`chromeless`) and hands every pixel to
 * the grid. TileGrid's `fitToPage` then solves the column count that makes all
 * of the tags fit at the largest possible badge: one tap, never a drag. The
 * per-tag track count is dropped for the same reason — it is chrome, and every
 * line of it comes straight off the icon size.
 */
UI.Sheet {
    id: sheet

    fullScreen: true
    chromeless: true
    showClose: false          // picking closes it; empty space closes it too
    contentMargin: 12

    title: "Playlists"

    property var tiles: []

    function _rebuild() {
        if (!opened || !Music) {
            tiles = [];
            return;
        }
        var src = Music.tagList || [];
        var out = [];
        for (var i = 0; i < src.length; ++i) {
            var t = src[i];
            out.push({
                "name":  t.name,
                "label": t.label,
                "icon":  iconsPath + "/Tags/" + t.icon
            });
        }
        tiles = out;
    }

    onOpenedChanged: _rebuild()

    Connections {
        target: Music
        enabled: sheet.opened
        ignoreUnknownSignals: true
        function onPlaylistChanged() { sheet._rebuild(); }
    }

    UI.TileGrid {
        anchors.fill: parent
        fitToPage: true
        showCount: false
        spacing: 6
        maxBadge: 300
        model: sheet.tiles
        currentValue: Music ? Music.currentTag : ""
        labelKey: "label"
        valueKey: "name"
        onPicked: function (v) {
            if (Music) Music.filterByTag(v);
            sheet.close();
        }
    }
}
