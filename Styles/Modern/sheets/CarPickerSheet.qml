import QtQuick
import "../components" as UI

/**
 * Car picker — two stage, brands then models, on top of UI.Sheet.
 *
 * Stage 1 lists Modding.carBrands; picking one drops into stage 2, which lists
 * Modding.carModels(brand). Picking a model calls Modding.loadCar() and closes.
 *
 * Full-screen and chromeless on purpose: there is no header bar and the dock
 * does not show through, so both stages hand the whole 1280x800 to the grid and
 * `fitToPage` solves a column count that makes every tile fit. Nothing here
 * scrolls — this is a touchscreen in a car, so every choice is one tap.
 *
 * The old car-picker overlay had neither a Back nor a
 * Close button — once open, the only way out was to pick a car. Both survive
 * the chromeless switch: Close is the Sheet's own, floated bottom centre, and
 * Back rides in the Sheet's footer slot beside it on stage 2 only.
 *
 * The tile model is built only while `opened` is true. Building it walks the
 * Cars/ tree through the bridge, which is filesystem work we do not want the
 * always-loaded Engine page paying for while the sheet is shut.
 */
UI.Sheet {
    id: root

    /** "" while browsing brands, the brand name while browsing its models. */
    property string selectedBrand: ""

    fullScreen: true
    chromeless: true
    showClose: false          // picking closes it; empty space closes it too
    contentMargin: 18

    // Kept for the non-chromeless case; the header does not render here.
    title: selectedBrand === "" ? "Cars" : selectedBrand
    subtitle: {
        var n = _tiles.length;
        if (selectedBrand === "")
            return n === 1 ? "1 brand" : n + " brands";
        return n === 1 ? "1 model" : n + " models";
    }

    /** Open at stage 1. */
    function show() {
        selectedBrand = "";
        opened = true;
    }

    onOpenedChanged: if (!opened) selectedBrand = "";

    function _file(path) {
        return path ? "file://" + path : iconsPath + "/side.png";
    }

    readonly property var _tiles: {
        var out = [];
        if (!opened || !Modding)
            return out;
        var i;
        if (selectedBrand === "") {
            var brands = Modding.carBrands || [];
            for (i = 0; i < brands.length; i++)
                out.push({ label: brands[i],
                           value: brands[i],
                           icon: root._file(Modding.brandLogoPath(brands[i])) });
        } else {
            var models = Modding.carModels(selectedBrand) || [];
            for (i = 0; i < models.length; i++)
                out.push({ label: models[i],
                           value: models[i],
                           icon: root._file(Modding.modelPreviewPath(selectedBrand, models[i])) });
        }
        return out;
    }

    // Highlight what is already loaded, on whichever stage shows it.
    readonly property var _current: {
        if (!Modding)
            return "";
        if (selectedBrand === "")
            return Modding.carBrand;
        return selectedBrand === Modding.carBrand ? Modding.carName : "";
    }

    footer: UI.PillButton {
        text: "Back"
        variant: "raised"
        fontSize: 18
        visible: root.selectedBrand !== ""
        // Match the floated Close button; collapse to nothing on stage 1 so the
        // footer Row keeps Close centred.
        width: visible ? 160 : 0
        height: visible ? 52 : 0
        onClicked: root.selectedBrand = ""
    }

    UI.TileGrid {
        anchors.fill: parent
        model: root._tiles
        currentValue: root._current
        labelKey: "label"
        valueKey: "value"
        // Solve the grid to the page: no dragging on either stage.
        fitToPage: true
        // Brands and models have no count to show — that row of chrome would
        // only steal height from the badge.
        showCount: false
        spacing: 10
        // Well above the 150 default: a brand with two or three models should
        // fill the page with big artwork instead of three small dots in a
        // corner. Wide lists stay width-limited and never reach this.
        maxBadge: 240

        onPicked: function (value) {
            if (root.selectedBrand === "") {
                root.selectedBrand = value;
                return;
            }
            Modding.loadCar(root.selectedBrand, value);
            root.close();
        }
    }
}
