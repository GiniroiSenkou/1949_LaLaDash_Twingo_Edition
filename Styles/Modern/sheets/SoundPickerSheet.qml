import QtQuick
import "../components" as UI

/**
 * Sound picker — the modern skin's replacement for the legacy fullscreen
 * SoundPickerOverlay.
 *
 * Three axes of state, exactly as the legacy overlay had them (plus one more):
 *
 *   mode  "play"      pick a sound and play it now
 *         "favorite"  pick a sound and write it into favourite `slot`
 *         "alert"     pick a sound and assign it to alert `alertKey`
 *   page  0           folder grid   (Soundboard.folders)
 *         1           file list     (Soundboard.files)
 *
 * Open it through `openFor(mode, slot, alertKey)` rather than by assigning
 * `opened` — `UI.Sheet.close()` writes to `opened`, so a binding on it would
 * be broken the first time the sheet closes and it would never reopen.
 * `openFor` also resets mode/page/slot/alertKey, which the legacy overlay
 * never did: it only ever set them on the way in, so a cancelled favourite
 * assignment leaked into the next "Play Sound" tap.
 */
UI.Sheet {
    id: sheet

    /** "play", "favorite", or "alert". */
    property string mode: "play"
    /** Favourite slot being assigned in "favorite" mode; -1 otherwise. */
    property int    slot: -1
    /** Alert key being assigned in "alert" mode; "" otherwise. */
    property string alertKey: ""
    /** 0 = folder grid, 1 = file list. */
    property int    page: 0

    /** Open the picker, resetting every axis of state first. */
    function openFor(pickMode, pickSlot, pickAlertKey) {
        sheet.mode = (pickMode === "favorite" || pickMode === "alert") ? pickMode : "play";
        sheet.slot = (pickSlot === undefined || pickSlot === null) ? -1 : pickSlot;
        sheet.alertKey = (pickAlertKey === undefined || pickAlertKey === null) ? "" : pickAlertKey;
        sheet.page = 0;
        sheet.opened = true;
    }

    // ── derived state ───────────────────────────────────────────────────
    readonly property bool slotAssigned: {
        if (!Soundboard || sheet.slot < 0)
            return false;
        var favs = Soundboard.favorites;
        return !!(favs && favs.length > sheet.slot && favs[sheet.slot] && favs[sheet.slot].name);
    }

    readonly property bool alertSoundAssigned: {
        if (!Alerts || sheet.alertKey === "")
            return false;
        var rows = Alerts.alertCatalog;
        for (var i = 0; i < rows.length; ++i)
            if (rows[i].key === sheet.alertKey) return !!rows[i].soundName;
        return false;
    }

    // TileGrid wants objects so each tile can carry its icon.
    readonly property var folderModel: {
        var out = [];
        var names = Soundboard ? Soundboard.folders : [];
        for (var i = 0; i < names.length; ++i) {
            out.push({
                "label": String(names[i]),
                "value": String(names[i]),
                "icon": iconsPath + "/soundboard.png"
            });
        }
        return out;
    }

    readonly property int fileCount: (Soundboard && Soundboard.files) ? Soundboard.files.length : 0

    // The footer carries its own Close so it stays leftmost and never shifts.
    showClose: false

    title: (mode === "favorite" && slot >= 0) ? "Choose Favourite Sound " + (slot + 1)
         : (mode === "alert") ? "Choose Alert Sound"
         : "Soundboard"

    subtitle: {
        if (page === 1 && Soundboard && Soundboard.currentFolder) {
            if (mode === "favorite")
                return Soundboard.currentFolder + " · selecting file saves the slot immediately";
            if (mode === "alert")
                return Soundboard.currentFolder + " · selecting file assigns it to the alert immediately";
            return Soundboard.currentFolder + " · choose a sound to play now";
        }
        if (mode === "favorite")
            return "Choose a folder, then a file to assign the slot. Playback stays disabled.";
        if (mode === "alert")
            return "Choose a folder, then a file to play when this alert fires. Playback stays disabled.";
        return "Choose a folder, then a sound to play.";
    }

    // ── body ────────────────────────────────────────────────────────────

    // Page 0 — folders
    Item {
        anchors.fill: parent
        visible: sheet.page === 0

        UI.TileGrid {
            anchors.fill: parent
            fitToPage: true
            showCount: false
            maxBadge: 190
            model: sheet.folderModel
            labelKey: "label"
            valueKey: "value"
            currentValue: Soundboard ? Soundboard.currentFolder : ""
            onPicked: function (value) {
                if (Soundboard)
                    Soundboard.selectFolder(value);
                sheet.page = 1;
            }
        }

        Text {
            anchors.centerIn: parent
            visible: sheet.folderModel.length === 0
            text: "No sound folders found in Sounds/."
            color: Theme.inkFaint
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSize("pages_secondary_text")
        }
    }

    // Page 1 — files in the selected folder
    Item {
        anchors.fill: parent
        visible: sheet.page === 1

        ListView {
            id: fileList
            anchors.fill: parent
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            model: Soundboard ? Soundboard.files : []

            delegate: UI.ListRow {
                width: fileList.width
                label: modelData
                clickable: true
                minHeight: 66
                divider: index < fileList.count - 1
                onClicked: {
                    if (!Soundboard)
                        return;
                    if (sheet.mode === "favorite" && sheet.slot >= 0) {
                        var path = Soundboard.pathForIndex(index);
                        Soundboard.setFavorite(sheet.slot, path, modelData);
                    } else if (sheet.mode === "alert" && sheet.alertKey !== "") {
                        if (Alerts) Alerts.setAlertSound(sheet.alertKey, Soundboard.pathForIndex(index), modelData);
                    } else {
                        Soundboard.playFile(index);
                    }
                    sheet.close();
                }
            }
        }

        Text {
            anchors.centerIn: parent
            visible: sheet.fileCount === 0
            text: "No sounds in this folder."
            color: Theme.inkFaint
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSize("pages_secondary_text")
        }
    }

    // ── footer ──────────────────────────────────────────────────────────
    footer: Row {
        spacing: 10

        UI.PillButton {
            width: 120
            text: "Close"
            variant: "raised"
            onClicked: sheet.close()
        }

        UI.PillButton {
            visible: sheet.page === 1
            width: 120
            text: "Back"
            variant: "outline"
            onClicked: sheet.page = 0
        }

        UI.PillButton {
            visible: sheet.mode === "play"
            width: 150
            text: "Stop Sounds"
            variant: "outline"
            onClicked: if (Soundboard) Soundboard.stop()
        }

        // Not in the legacy overlay: clearFavorite() existed on the bridge but
        // nothing in the UI could ever reach it.
        UI.PillButton {
            visible: sheet.mode === "favorite" && sheet.slotAssigned
            width: 150
            text: "Clear slot"
            variant: "danger"
            onClicked: {
                if (Soundboard)
                    Soundboard.clearFavorite(sheet.slot);
                sheet.close();
            }
        }

        UI.PillButton {
            visible: sheet.mode === "alert" && sheet.alertSoundAssigned
            width: 150
            text: "Clear sound"
            variant: "danger"
            onClicked: {
                if (Alerts) Alerts.setAlertSound(sheet.alertKey, "", "");
                sheet.close();
            }
        }
    }
}
