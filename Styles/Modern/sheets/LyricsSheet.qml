import QtQuick
import "../components" as UI

/**
 * Lyrics — the legacy ¶ overlay, re-cut as a sheet.
 *
 * Four display modes, chosen in Settings (Music.lyricsDisplayMode); this sheet
 * only reads it. Two render paths cover them:
 *
 *   ListView (Layout A)
 *     smooth_wave    every line's font size is driven by its distance from the
 *                    centre of the view, base → 1.45× through a lens of radius
 *                    max(80, height * 0.32)
 *     single_focus   same list, but only the current line grows
 *
 *   Solo (Layout B)
 *     solo_spotlight one line, cross-fading 160 ms out / 220 ms in
 *     solo_preview   current line plus the next one lifting into place, 350 ms
 *
 * PERFORMANCE — smooth_wave re-evaluates every visible delegate's font size on
 * every frame the view scrolls, which is the single most expensive thing this
 * app can draw on a Pi 4B. It cannot be fixed without changing the effect, so
 * instead nothing survives the sheet being closed:
 *   · the ListView model empties, destroying every delegate and with it every
 *     proximity binding and Behavior;
 *   · every Behavior and every Connections block is gated on `opened`, so no
 *     animation can be started while the sheet is hidden.
 */
UI.Sheet {
    id: sheet

    title: "Lyrics"
    // Lyrics are the one thing the user reads at a glance, so the sheet takes
    // the whole panel — status strip and dock included. Close lives in the
    // Sheet's footer, at the bottom, where a thumb already is.
    fullScreen: true

    // ── mode / availability ────────────────────────────────────────────
    readonly property string _mode:
        Music ? (Music.lyricsDisplayMode || "smooth_wave") : "smooth_wave"

    // Cycling order, as asked for: wave → focus → spotlight → preview → wave.
    readonly property var _modes: [
        "smooth_wave", "single_focus", "solo_spotlight", "solo_preview"
    ]

    function _modeLabel(m) {
        return m === "single_focus"   ? "Single Focus"
             : m === "solo_spotlight" ? "Solo Spotlight"
             : m === "solo_preview"   ? "Solo Preview"
             :                          "Smooth Wave";
    }

    function _cycleMode() {
        if (!Music) return;
        var i = _modes.indexOf(sheet._mode);
        Music.setLyricsDisplayMode(_modes[(i < 0 ? 0 : i + 1) % _modes.length]);
    }

    readonly property bool _useListView:
        _mode === "smooth_wave" || _mode === "single_focus"
    readonly property bool _available: Music ? Music.lyricsAvailable : false

    // The mode cycler is the only control up here, so it can afford to be
    // a proper touch target rather than a chip.
    headerControl: UI.PillButton {
        text: sheet._modeLabel(sheet._mode)
        variant: "raised"
        implicitWidth: 240
        implicitHeight: 62
        fontSize: 20
        radius: 31
        onClicked: sheet._cycleMode()
    }

    onOpenedChanged: {
        if (!opened)
            return;
        lyricsList.userOffset = 0;
        lyricsList._scrollToCurrent(true);
        soloView._refreshTexts();
        soloView._resetSlots();
    }

    Item {
        anchors.fill: parent

        // ── header — tap to re-sync ────────────────────────────────────
        Item {
            id: header
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            height: headerCol.implicitHeight + 16

            Column {
                id: headerCol
                anchors.centerIn: parent
                width: parent.width
                spacing: 3

                Text {
                    id: headerTitle
                    width: parent.width
                    horizontalAlignment: Text.AlignHCenter
                    text: Music ? Music.title : ""
                    color: Theme.ink
                    font.family: Theme.fontFamily
                    font.pixelSize: 28
                    font.weight: Font.Bold
                    elide: Text.ElideRight
                    Behavior on opacity { NumberAnimation { duration: 110 } }
                }

                Text {
                    width: parent.width
                    horizontalAlignment: Text.AlignHCenter
                    visible: text !== ""
                    text: Music ? (Music.artist || "") : ""
                    color: Theme.inkDim
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSize("widgets_secondary_text")
                    elide: Text.ElideRight
                }

                // Sync flag — shown only when the lyrics won't fully follow the
                // song (no timing at all, or only some lines timed), so a file
                // that just doesn't scroll is explained rather than looking broken.
                Rectangle {
                    anchors.horizontalCenter: parent.horizontalCenter
                    visible: Music && Music.lyricsAvailable
                             && (!Music.lyricsSynced || Music.lyricsPartial)
                    width: flagRow.implicitWidth + 22
                    height: 26
                    radius: 13
                    color: Theme.surfaceRaised
                    Row {
                        id: flagRow
                        anchors.centerIn: parent
                        spacing: 7
                        Rectangle {
                            width: 8; height: 8; radius: 4
                            anchors.verticalCenter: parent.verticalCenter
                            color: Music ? Music.lyricsLabelColor : "transparent"
                        }
                        Text {
                            anchors.verticalCenter: parent.verticalCenter
                            text: Music ? Music.lyricsLabelText : ""
                            color: Theme.inkDim
                            font.family: Theme.fontFamily
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                        }
                    }
                }
            }

            MouseArea {
                anchors.fill: parent
                onPressed: headerTitle.opacity = 0.6
                onReleased: headerTitle.opacity = 1.0
                onCanceled: headerTitle.opacity = 1.0
                onClicked: {
                    lyricsList.userOffset = 0;
                    lyricsList._scrollToCurrent(false);
                }
            }
        }

        // ── body ───────────────────────────────────────────────────────
        Item {
            id: body
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: header.bottom
            anchors.bottom: parent.bottom
            anchors.topMargin: 10

            // ─── Layout A — ListView with the magnifier ────────────────
            ListView {
                id: lyricsList
                anchors.fill: parent
                anchors.leftMargin: 60
                anchors.rightMargin: 60
                visible: sheet._available && sheet._useListView
                clip: true
                spacing: 18
                boundsBehavior: Flickable.StopAtBounds

                // Emptying the model when the sheet closes is what actually
                // stops the magnifier costing anything.
                model: (sheet.opened && sheet._available && sheet._useListView && Music)
                       ? Music.lyricsLines : []

                // Fixed row pitch so the magnifier never triggers reflow.
                readonly property real rowHeight: 56
                readonly property real baseFontSize: Theme.fontSize("pages_primary_text")
                readonly property real peakFontSize: Theme.fontSize("pages_primary_text") * 1.45
                readonly property real lensRadius: Math.max(80, height * 0.32)
                readonly property string displayMode: sheet._mode

                /** Drag-captured lead/lag, preserved across timestamp advances. */
                property real userOffset: 0
                /** Suppresses the 750 ms glide for a one-shot jump. */
                property bool _snap: false

                readonly property int pastLinesVisible: 2

                function _rowPitch() { return rowHeight + spacing; }

                function _anchorYForCurrent(idx) {
                    if (idx < 0) return 0;
                    var normalY = (idx - pastLinesVisible) * _rowPitch();
                    if (idx >= pastLinesVisible) return normalY;
                    // Too few sung lines to fill the slots above — blend
                    // toward a fully centred anchor so line 1 opens centred.
                    var rowCenterY = idx * _rowPitch() + rowHeight / 2;
                    var centeredY = rowCenterY - height / 2;
                    var t = pastLinesVisible > 0 ? (idx / pastLinesVisible) : 1;
                    return centeredY * (1 - t) + normalY * t;
                }

                function _centeredYFor(idx) { return _anchorYForCurrent(idx); }

                function _scrollToCurrent(snap) {
                    if (!Music || !Music.lyricsSynced) return;
                    // Manual sync: while the user is dragging the text to line
                    // the file up by hand, the auto-scroll must not touch
                    // contentY — onDraggingChanged/onFlickEnded recapture the
                    // offset the moment they let go.
                    if (dragging || flicking || moving) return;
                    var idx = Music.lyricsCurrentLine;
                    if (idx < 0) return;
                    if (snap) _snap = true;
                    contentY = _centeredYFor(idx) + userOffset;
                    if (snap) _snap = false;
                }

                Behavior on contentY {
                    enabled: sheet.opened && !lyricsList._snap
                             && !lyricsList.dragging && !lyricsList.flicking
                    NumberAnimation { duration: 750; easing.type: Easing.InOutCubic }
                }

                onDraggingChanged: {
                    if (dragging) return;
                    if (!Music || !Music.lyricsSynced) return;
                    var idx = Music.lyricsCurrentLine;
                    if (idx < 0) return;
                    userOffset = contentY - _centeredYFor(idx);
                }
                onFlickEnded: {
                    if (!Music || !Music.lyricsSynced) return;
                    var idx = Music.lyricsCurrentLine;
                    if (idx < 0) return;
                    userOffset = contentY - _centeredYFor(idx);
                }

                delegate: Item {
                    id: lineRoot
                    width: lyricsList.width
                    height: lyricsList.rowHeight
                    clip: lyricsList.displayMode === "single_focus"

                    readonly property real _viewCenterY:
                        lyricsList.contentY + lyricsList.height / 2
                    readonly property real _itemCenterY: y + height / 2
                    readonly property real _proximity: {
                        var d = Math.abs(_itemCenterY - _viewCenterY);
                        var r = lyricsList.lensRadius;
                        if (d >= r) return 0;
                        var t = 1 - d / r;
                        return t * t;
                    }
                    readonly property bool _isPast:
                        Music && Music.lyricsSynced && Music.lyricsCurrentLine > index
                    readonly property bool _isCurrent:
                        Music && Music.lyricsSynced && Music.lyricsCurrentLine === index

                    Text {
                        anchors.centerIn: parent
                        width: parent.width
                        horizontalAlignment: Text.AlignHCenter
                        wrapMode: Text.WordWrap
                        text: (typeof modelData === "object" && modelData)
                              ? (modelData.text || "")
                              : (modelData || "")
                        font.family: Theme.fontFamily
                        // Lines whose timing was carried from the previous stamp
                        // (a partly-synced file) read in italic, so an
                        // approximately-placed line is visibly not exact.
                        font.italic: (typeof modelData === "object" && modelData)
                                     ? (modelData.estimated || false) : false

                        font.pixelSize: {
                            if (lineRoot._isPast)
                                return lyricsList.baseFontSize;
                            if (lyricsList.displayMode === "single_focus")
                                return lineRoot._isCurrent ? lyricsList.peakFontSize
                                                           : lyricsList.baseFontSize;
                            return lyricsList.baseFontSize
                                   + (lyricsList.peakFontSize - lyricsList.baseFontSize)
                                     * lineRoot._proximity;
                        }
                        font.bold: {
                            if (lyricsList.displayMode === "single_focus")
                                return lineRoot._isCurrent;
                            return lineRoot._proximity > 0.55;
                        }
                        opacity: {
                            if (lineRoot._isPast) return 0.30;
                            if (lyricsList.displayMode === "single_focus")
                                return lineRoot._isCurrent ? 1.0 : 0.45;
                            return 0.35 + 0.65 * lineRoot._proximity;
                        }
                        color: {
                            if (lineRoot._isPast) return Theme.inkGhost;
                            if (lyricsList.displayMode === "single_focus")
                                return lineRoot._isCurrent ? Theme.ink : Theme.inkDim;
                            return lineRoot._proximity > 0.4 ? Theme.ink : Theme.inkDim;
                        }

                        Behavior on opacity {
                            enabled: sheet.opened
                            NumberAnimation { duration: 120 }
                        }
                        Behavior on font.pixelSize {
                            enabled: sheet.opened
                            NumberAnimation { duration: 120 }
                        }
                    }
                }
            }

            // Auto-scroll to the current line; honours the drag-captured
            // offset so a manual lead/lag persists inside a track.
            Connections {
                target: Music
                enabled: sheet.opened && sheet._useListView
                ignoreUnknownSignals: true
                function onLyricsCurrentLineChanged() {
                    lyricsList._scrollToCurrent(false);
                }
                function onLyricsChanged() {
                    lyricsList.userOffset = 0;
                    lyricsList._scrollToCurrent(true);
                }
            }

            // ─── Layout B — solo view (one line ± preview) ─────────────
            Item {
                id: soloView
                anchors.fill: parent
                visible: sheet._available && !sheet._useListView

                readonly property bool isPreview: sheet._mode === "solo_preview"

                readonly property real mainFontSize: Theme.fontSize("pages_primary_text") * 2.0
                readonly property real previewFontSize: Theme.fontSize("pages_primary_text") * 1.1
                /** Gap under a (possibly wrapped) main lyric before the preview. */
                readonly property real previewGap: 56

                // Cached so the text swap can happen mid-fade, at peak
                // transparency, instead of jumping.
                property string _mainDisplay: ""
                property string _nextDisplay: ""

                function _lineAt(i) {
                    if (!Music) return "";
                    var lines = Music.lyricsLines;
                    if (!lines || i < 0 || i >= lines.length) return "";
                    var it = lines[i];
                    return (it && it.text) || "";
                }

                function _refreshTexts() {
                    if (!Music) { _mainDisplay = ""; _nextDisplay = ""; return; }
                    var idx = Math.max(0, Music.lyricsCurrentLine);
                    _mainDisplay = _lineAt(idx);
                    _nextDisplay = _lineAt(idx + 1);
                }

                function _resetSlots() {
                    soloMainText.opacity = 1.0;
                    soloPreviewText.opacity = 0.55;
                    soloPreviewText.font.pixelSize = previewFontSize;
                    soloPreviewText.y = Qt.binding(function () {
                        return soloMainText.y + soloMainText.height + soloView.previewGap;
                    });
                }

                Component.onCompleted: { _refreshTexts(); _resetSlots(); }
                onVisibleChanged: if (visible) { _refreshTexts(); _resetSlots(); }

                Text {
                    id: soloMainText
                    x: (parent.width - width) / 2
                    y: parent.height / 2 - height / 2
                    width: parent.width - 40
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.WordWrap
                    text: soloView._mainDisplay
                    color: Theme.ink
                    font.family: Theme.fontFamily
                    font.pixelSize: soloView.mainFontSize
                    font.bold: true
                    opacity: 1.0
                }

                Text {
                    id: soloPreviewText
                    visible: soloView.isPreview && text !== ""
                    x: (parent.width - width) / 2
                    y: soloMainText.y + soloMainText.height + soloView.previewGap
                    width: parent.width - 80
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.WordWrap
                    text: soloView._nextDisplay
                    color: Theme.inkDim
                    font.family: Theme.fontFamily
                    font.pixelSize: soloView.previewFontSize
                    opacity: 0.55
                }

                // solo_spotlight — quick cross-fade, text swapped at zero.
                SequentialAnimation {
                    id: spotlightFade
                    running: false
                    NumberAnimation {
                        target: soloMainText; property: "opacity"; to: 0
                        duration: 160; easing.type: Easing.OutCubic
                    }
                    ScriptAction { script: soloView._refreshTexts() }
                    NumberAnimation {
                        target: soloMainText; property: "opacity"; to: 1
                        duration: 220; easing.type: Easing.OutCubic
                    }
                }

                // solo_preview — the next line lifts to centre and grows
                // while the outgoing line fades. 350 ms parallel.
                SequentialAnimation {
                    id: previewLift
                    running: false
                    ParallelAnimation {
                        NumberAnimation {
                            target: soloMainText; property: "opacity"; to: 0
                            duration: 350; easing.type: Easing.OutCubic
                        }
                        NumberAnimation {
                            target: soloPreviewText; property: "opacity"; to: 1
                            duration: 350; easing.type: Easing.OutCubic
                        }
                        NumberAnimation {
                            target: soloPreviewText; property: "font.pixelSize"
                            to: soloView.mainFontSize
                            duration: 350; easing.type: Easing.OutCubic
                        }
                        NumberAnimation {
                            target: soloPreviewText; property: "y"
                            to: soloView.height / 2 - soloPreviewText.height / 2
                            duration: 350; easing.type: Easing.OutCubic
                        }
                    }
                    ScriptAction {
                        script: { soloView._refreshTexts(); soloView._resetSlots(); }
                    }
                }

                Connections {
                    target: Music
                    enabled: sheet.opened && !sheet._useListView
                    ignoreUnknownSignals: true
                    function onLyricsCurrentLineChanged() {
                        if (!Music || !Music.lyricsAvailable) return;
                        if (!soloView.visible) { soloView._refreshTexts(); return; }
                        if (soloView.isPreview) previewLift.start();
                        else spotlightFade.start();
                    }
                    function onLyricsChanged() {
                        soloView._refreshTexts();
                        soloView._resetSlots();
                    }
                    function onLyricsDisplayModeChanged() {
                        soloView._refreshTexts();
                        soloView._resetSlots();
                    }
                }
            }

            // ─── empty state ──────────────────────────────────────────
            Column {
                anchors.centerIn: parent
                width: Math.min(parent.width - 60, 620)
                spacing: 12
                visible: !sheet._available

                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "No lyrics available"
                    color: Theme.ink
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSize("pages_primary_text")
                    font.weight: Font.Bold
                }

                Text {
                    width: parent.width
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.WordWrap
                    text: "Drop a .lrc file in the Lyrics folder or next to the audio file, "
                          + "or add a USLT / LYRICS tag."
                    color: Theme.inkDim
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSize("widgets_secondary_text")
                }
            }
        }
    }
}
