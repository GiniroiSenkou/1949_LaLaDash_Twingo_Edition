import QtQuick
import "../components" as UI
import "../sheets" as Sheets

/**
 * Music — the modern skin's player.
 *
 * Two columns (mockup MUSIC):
 *   left   player card — 216 px art + title/artist/SoundDrive symbol/tags,
 *          progress row, four round transports, a chip row, and the
 *          split-audio rail
 *   right  360 px "Up next" rail
 *
 * Every legacy control survives. The legacy transport strip carried eight
 * buttons; only ⏮ ⏸/▶ ⏭ map onto the mockup's round transports, so the other
 * five (☰ ⇉ ∿ ¶ ⌕) become the chip row with words instead of glyphs. The
 * three overlays the legacy page drew inline (library, search, lyrics) are
 * now UI.Sheets under ../sheets.
 *
 * The old "Stems Available" / "Using Stems" word badge is gone; the same state
 * is now a single ∿ symbol beside the track identity, in the idiom of the
 * lyrics indicator dot.
 *
 * Cost notes, because this is the heaviest page on the Pi:
 *  · The library never crosses into QML. Music.visibleTracks is 3400+ dicts
 *    and a `property var` assigned from it holds a live sequence wrapper, not
 *    a copy — indexing it re-reads the property, so a scan for the playing
 *    track cost one full re-marshal per row. Music.upNext(n) does the window
 *    in Python and hands over n rows, each carrying its own library index.
 *  · The window is skipped while the page is hidden or a sheet covers it,
 *    and replayed on the way back in (`_upNextDirty`).
 *  · activatePage()/deactivatePage() drive the bridge's poll rate — without
 *    them the progress poll never slows down.
 */
Item {
    id: root

    // ── sheet state ────────────────────────────────────────────────────
    property bool libraryOpen: false
    property bool searchOpen:  false
    property bool lyricsOpen:  false

    // Library and Search are mutually exclusive, exactly as legacy.
    onLibraryOpenChanged: {
        if (libraryOpen) searchOpen = false;
        else _settle();
    }
    onSearchOpenChanged: {
        if (searchOpen) libraryOpen = false;
        else _settle();
    }

    readonly property int pagePad: 22
    readonly property int railW:   360
    readonly property int colGap:  24

    // ── page lifecycle — drives the bridge poll rate. Do not remove. ────
    onVisibleChanged: {
        if (!Music) return;
        if (visible) {
            Music.activatePage();
            _settle();
        } else {
            Music.deactivatePage();
        }
    }

    Component.onCompleted: {
        if (Music && visible) {
            Music.activatePage();
            _rebuildUpNext();
        }
        // Otherwise stay dirty — walking the library for a page nobody has
        // opened yet is exactly the boot cost this skin avoids.
    }

    // ── SoundDrive mix mode ────────────────────────────────────────────
    // The Damuci mix modes, persisted by the bridge into [Music] mix_mode.
    readonly property var mixModes: ["Mode A", "Mode B", "Mode C", "Mode D", "Mode E"]

    readonly property string _mixLetter: {
        var m = Music ? (Music.mixMode || "Mode A") : "Mode A";
        return String(m).slice(-1).toUpperCase();
    }

    function _cycleMixMode() {
        if (!Music) return;
        var i = mixModes.indexOf(Music.mixMode || "Mode A");
        Music.setMixMode(mixModes[(i < 0 ? 0 : i + 1) % mixModes.length]);
    }

    // ── "Up next" window ───────────────────────────────────────────────
    property bool _upNextDirty: true

    /** Rows the rail shows. The library is 3400 tracks; this is "up next",
        not "the library from A". */
    readonly property int upNextCount: 15
    /** The window itself. Each row carries `index`, its position in the
        bridge's visible-track list, because selectFile() takes that index —
        get it wrong and the rail plays the wrong song. */
    property var upNext: []

    readonly property string _curTitle:  Music ? Music.title  : ""
    readonly property string _curArtist: Music ? Music.artist : ""

    function _queueInvalidated() {
        // Re-windowing behind a sheet or a hidden page is pure waste;
        // remember that it is stale and do it when it is next looked at.
        if (_looking()) _rebuildUpNext();
        else _upNextDirty = true;
    }

    function _looking() { return visible && !searchOpen && !libraryOpen; }

    /** Whatever went stale while nobody was looking, catch up now. */
    function _settle() {
        if (_looking() && _upNextDirty) _rebuildUpNext();
    }

    /** Ask the bridge for the rows that follow the playing track. "Up next"
        means what follows, so the playing track is not row 0 — it already
        occupies the whole left half of the page. */
    function _rebuildUpNext() {
        upNext = Music ? Music.upNext(upNextCount) : [];
        _upNextDirty = false;
        queueView.positionViewAtBeginning();
    }

    Connections {
        target: Music
        ignoreUnknownSignals: true
        function onPlaylistChanged() { root._queueInvalidated(); }
        function onTrackChanged() {
            if (root._looking()) root._rebuildUpNext();
            else root._upNextDirty = true;
        }
    }

    // ═══════════════════════════════════════════════════════════════════
    Item {
        id: content
        anchors.fill: parent
        anchors.leftMargin: root.pagePad
        anchors.rightMargin: root.pagePad
        anchors.topMargin: 6
        anchors.bottomMargin: 6

        // ── LEFT: the player card ──────────────────────────────────────
        UI.Card {
            id: playerCard
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            anchors.right: rail.visible ? rail.left : parent.right
            anchors.rightMargin: rail.visible ? root.colGap : 0
            radius: 22

            Column {
                id: playerCol
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.leftMargin: 28
                anchors.rightMargin: 28
                anchors.verticalCenter: parent.verticalCenter
                spacing: 34

                // ── art + track identity ───────────────────────────
                Item {
                    width: parent.width
                    height: 216

                    Rectangle {
                        id: artFrame
                        width: 216
                        height: 216
                        radius: 16
                        color: Theme.surfaceRaised
                        border.width: 1
                        border.color: Theme.hairline

                        Image {
                            id: albumArt
                            anchors.fill: parent
                            anchors.margins: 3
                            source: Music ? Music.albumArtUrl : ""
                            fillMode: Image.PreserveAspectCrop
                            sourceSize.width: 432
                            sourceSize.height: 432
                            asynchronous: true
                            cache: true
                            smooth: true
                            visible: status === Image.Ready
                        }

                        Text {
                            anchors.centerIn: parent
                            // Covers empty source, load error and the
                            // brief decode window alike — comparing
                            // `source` to "" does not, because a url
                            // property is not a JS string here.
                            visible: albumArt.status !== Image.Ready
                            text: "♫"
                            color: Theme.inkGhost
                            font.family: Theme.fontFamily
                            font.pixelSize: 54
                        }
                    }

                    Column {
                        anchors.left: artFrame.right
                        anchors.leftMargin: 24
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 6

                        Text {
                            width: parent.width
                            text: (Music ? Music.title : "") || "No track"
                            color: Theme.ink
                            font.family: Theme.fontFamily
                            font.pixelSize: 38
                            font.weight: Font.Bold
                            elide: Text.ElideRight
                            maximumLineCount: 2
                            wrapMode: Text.WordWrap
                            lineHeight: 1.15
                        }

                        Text {
                            width: parent.width
                            visible: Music ? Music.artist !== "" : false
                            text: Music ? (Music.artist || "") : ""
                            color: Theme.inkDim
                            font.family: Theme.fontFamily
                            font.pixelSize: 21
                            elide: Text.ElideRight
                        }

                        // Stems symbol + current-track tags — the mockup's
                        // third identity line, made live.
                        Row {
                            id: metaRow
                            spacing: 14
                            topPadding: 6

                            // Both flags are read straight off the bridge, never
                            // off a child's `visible`: QQuickItem reports
                            // EFFECTIVE visibility, so a parent whose visibility
                            // depends on a child's can never turn back on once
                            // it is off.
                            readonly property bool hasTags:
                                Music ? Music.currentTrackTags.length > 0 : false

                            visible: hasTags

                            // The stems state is shown as a dot on the
                            // SoundDrive chip, in the idiom of the lyrics
                            // indicator — no badge and no symbol here.

                            Row {
                                id: tagRow
                                spacing: 12
                                anchors.verticalCenter: parent.verticalCenter
                                visible: metaRow.hasTags

                                Repeater {
                                    model: Music ? Music.currentTrackTags : []

                                    Image {
                                        // Legacy rendered these at 80; the first
                                        // modern pass shrank them to 31. Doubling
                                        // that still landed under legacy, so they
                                        // go above it — they read as the track's
                                        // identity next to the enlarged art.
                                        width: 84
                                        height: 84
                                        sourceSize.width: 168
                                        sourceSize.height: 168
                                        source: iconsPath + "/Tags/" + modelData
                                        fillMode: Image.PreserveAspectFit
                                        asynchronous: true
                                        smooth: true
                                        onStatusChanged: {
                                            if (status === Image.Error)
                                                source = iconsPath + "/Tags/untagged.png";
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                // Progress, transports and chips travel together as
                // one block, exactly as the mockup groups them.
                Column {
                    width: parent.width
                    spacing: 26

                    // ── progress ───────────────────────────────────────
                    Item {
                        width: parent.width
                        height: 26

                        Text {
                            id: elapsed
                            anchors.left: parent.left
                            anchors.verticalCenter: parent.verticalCenter
                            width: 46
                            text: Music ? Music.positionText : "0:00"
                            color: Theme.inkDim
                            font.family: Theme.fontFamily
                            font.pixelSize: 14
                        }

                        UI.Slider {
                            id: progressSlider
                            anchors.left: elapsed.right
                            anchors.right: remaining.left
                            anchors.leftMargin: 14
                            anchors.rightMargin: 14
                            anchors.verticalCenter: parent.verticalCenter
                            trackHeight: 6
                            knobSize: 18
                            value: Music ? Music.progress : 0

                            // Legacy seeked from BOTH signals — keep that.
                            onMoved: function (v) { if (Music) Music.seekFraction(v); }
                            onReleased: function (v) {
                                if (Music) Music.seekFraction(v);
                                // UI.Slider assigns `value` itself while
                                // dragging, which severs the declarative
                                // binding for good. Re-arm it on commit or
                                // the fill stops following playback.
                                value = Qt.binding(function () {
                                    return Music ? Music.progress : 0;
                                });
                            }
                        }

                        Text {
                            id: remaining
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            width: 46
                            horizontalAlignment: Text.AlignRight
                            text: Music ? Music.remainingText : "-0:00"
                            color: Theme.inkDim
                            font.family: Theme.fontFamily
                            font.pixelSize: 14
                        }
                    }

                    // ── transports ─────────────────────────────────────
                    Item {
                        width: parent.width
                        height: 96

                        // SoundDrive mix mode — cycles A→B→C→D→E→A. Only
                        // meaningful when this track actually has stems for
                        // SoundDrive to mix, so it is gated on the same
                        // condition as the SoundDrive dot rather than on the
                        // chip's visibility. Sits out on the left, clear of
                        // the centred transport group.
                        UI.PillButton {
                            id: mixModeBtn
                            visible: Music ? (Music.stemsAvailable && Music.soundDriveEnabled) : false
                            anchors.left: parent.left
                            anchors.verticalCenter: parent.verticalCenter
                            width: 70; height: 70; radius: 35
                            text: root._mixLetter
                            fontSize: 28
                            onClicked: root._cycleMixMode()
                        }

                    Row {
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 26

                        UI.PillButton {
                            width: 70; height: 70; radius: 35
                            text: "⏮"
                            fontSize: 26
                            onClicked: if (Music) Music.prev()
                        }

                        UI.PillButton {
                            width: 96; height: 96; radius: 48
                            variant: "accent"
                            text: (Music && Music.playing) ? "⏸" : "▶"
                            fontSize: 34
                            onClicked: if (Music) Music.togglePlayPause()
                        }

                        UI.PillButton {
                            width: 70; height: 70; radius: 35
                            text: "⏭"
                            fontSize: 26
                            onClicked: if (Music) Music.next()
                        }
                    }
                    }

                    // ── chips — the five non-transport legacy buttons ──
                    Row {
                        anchors.horizontalCenter: parent.horizontalCenter
                        spacing: 10

                        // ☰ — library sheet
                        UI.PillButton {
                            height: 44; radius: 22
                            text: "Playlists"
                            checked: root.libraryOpen
                            onClicked: root.libraryOpen = !root.libraryOpen
                        }

                        // ⇉ — split audio
                        UI.PillButton {
                            id: splitChip
                            visible: Music ? Music.splitAudioConfigured : false
                            height: 44; radius: 22
                            text: "Split"
                            checked: Music ? Music.splitAudioEnabled : false
                            onClicked: if (Music) Music.toggleSplitAudio(!Music.splitAudioEnabled)

                            // Legacy highlighted the border while the mirror
                            // bus was actually running; a live dot is the
                            // modern equivalent.
                            Rectangle {
                                visible: Music ? Music.splitAudioActive : false
                                width: 8; height: 8; radius: 4
                                color: Theme.ok
                                anchors.right: parent.right
                                anchors.rightMargin: 10
                                anchors.top: parent.top
                                anchors.topMargin: 9
                            }
                        }

                        // ∿ — SoundDrive
                        UI.PillButton {
                            id: soundDriveChip
                            visible: Music ? Music.soundDriveToggleVisible : false
                            height: 44; radius: 22
                            text: "SoundDrive"
                            checked: Music ? Music.soundDriveActive : false
                            onClicked: if (Music) Music.setPlayerSoundDrive(!Music.soundDriveActive)

                            // Same dot the Lyrics chip uses: this track has
                            // stems and SoundDrive is enabled, so there is
                            // something for SoundDrive to work with.
                            Rectangle {
                                visible: Music ? (Music.stemsAvailable && Music.soundDriveEnabled) : false
                                width: 8; height: 8; radius: 4
                                color: soundDriveChip.checked ? Theme.accentInk : Theme.accent
                                anchors.right: parent.right
                                anchors.rightMargin: 10
                                anchors.top: parent.top
                                anchors.topMargin: 9
                            }
                        }

                        // ¶ — lyrics sheet
                        UI.PillButton {
                            id: lyricsChip
                            height: 44; radius: 22
                            text: "Lyrics"
                            checked: root.lyricsOpen
                            onClicked: root.lyricsOpen = !root.lyricsOpen

                            Rectangle {
                                visible: Music ? Music.lyricsAvailable : false
                                width: 8; height: 8; radius: 4
                                color: lyricsChip.checked ? Theme.accentInk : Theme.accent
                                anchors.right: parent.right
                                anchors.rightMargin: 10
                                anchors.top: parent.top
                                anchors.topMargin: 9
                            }
                        }

                        // ⌕ — search sheet
                        UI.PillButton {
                            height: 44; radius: 22
                            text: "Search"
                            checked: root.searchOpen
                            onClicked: root.searchOpen = !root.searchOpen
                        }
                    }

                    // ── split-audio rail ───────────────────────────────
                    // Legacy hung this vertically off the left edge of the
                    // screen; there is no left edge to spare in a two-column
                    // layout, so it becomes a second chip row that only
                    // exists while split audio is on.
                    Row {
                        anchors.horizontalCenter: parent.horizontalCenter
                        spacing: 10
                        visible: Music ? (Music.splitAudioEnabled && Music.splitAudioConfigured) : false

                        UI.PillButton {
                            width: 78; height: 44; radius: 22
                            text: "F ‖ B"
                            checked: Music ? Music.fbSplitActive : false
                            onClicked: if (Music) Music.toggleBackChannel()
                        }

                        // Front/rear level fader. The channel split above sends
                        // L forward and R back, which on ordinary stereo music
                        // is nearly the same signal at both ends of the car —
                        // level is what makes the difference audible. Tap steps
                        // toward the rear and wraps; hold re-centres.
                        UI.PillButton {
                            width: 96; height: 44; radius: 22
                            visible: Music ? Music.splitBalanceAvailable : false
                            checked: Music ? Music.splitBalance !== 0 : false
                            fontSize: 15
                            text: {
                                if (!Music || Music.splitBalance === 0)
                                    return "F = R";
                                return Music.splitBalance < 0
                                    ? "F +" + Math.abs(Music.splitBalance)
                                    : "R +" + Music.splitBalance;
                            }
                            onClicked: {
                                if (!Music) return;
                                var next = (Music.splitBalance || 0) + 25;
                                if (next > 100) next = -100;
                                Music.setSplitBalance(next);
                            }
                            onPressAndHold: if (Music) Music.setSplitBalance(0)
                        }

                        UI.PillButton {
                            width: 44; height: 44; radius: 22
                            text: "−"
                            fontSize: 20
                            onClicked: {
                                if (Music)
                                    Music.setSplitAudioVolume((Music.splitAudioVolume || 0) - 5);
                            }
                        }

                        Rectangle {
                            width: 108; height: 44; radius: 22
                            color: Theme.surfaceRaised

                            Text {
                                anchors.centerIn: parent
                                text: "Split " + (Music ? Music.splitAudioVolume : 0) + "%"
                                color: Theme.ink
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSize("buttons_medium_text")
                                font.weight: Font.DemiBold
                            }
                        }

                        UI.PillButton {
                            width: 44; height: 44; radius: 22
                            text: "+"
                            fontSize: 20
                            onClicked: {
                                if (Music)
                                    Music.setSplitAudioVolume((Music.splitAudioVolume || 0) + 5);
                            }
                        }
                    }
                }
            }
        }

        // ── RIGHT: "Up next" rail ──────────────────────────────────────
        UI.Card {
            id: rail
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            // The side panel already takes 40% of the window; keeping the rail
            // as well squeezed the player off its grid. It stands down while
            // the panel is open and the player takes the whole card.
            visible: !(Nav && Nav.sidePanelOpen)
            width: visible ? root.railW : 0
            radius: 22

            Text {
                id: railTitle
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: 20
                text: "Up next"
                color: Theme.ink
                font.family: Theme.fontFamily
                font.pixelSize: 15
                font.weight: Font.Bold
                font.letterSpacing: 0.3
            }

            ListView {
                id: queueView
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: railTitle.bottom
                anchors.bottom: parent.bottom
                anchors.leftMargin: 14
                anchors.rightMargin: 14
                anchors.topMargin: 14
                anchors.bottomMargin: 16
                clip: true
                spacing: 6
                boundsBehavior: Flickable.StopAtBounds
                cacheBuffer: 120

                // The 15-row window from Music.upNext(), never the library.
                model: root.upNext

                delegate: Rectangle {
                    id: queueRow
                    /** Index in the bridge's visible-track list. */
                    readonly property int trackIndex: modelData.index
                    readonly property bool isCurrent:
                        modelData.title === root._curTitle
                        && modelData.artist === root._curArtist

                    width: queueView.width
                    height: 62
                    radius: 12
                    color: isCurrent ? Theme.accentSoft
                                     : (rowMa.pressed ? Theme.surfacePressed
                                        : (rowMa.containsMouse ? Theme.surfaceHover : "transparent"))

                    Rectangle {
                        id: rowThumb
                        anchors.left: parent.left
                        anchors.leftMargin: 14
                        anchors.verticalCenter: parent.verticalCenter
                        width: 44
                        height: 44
                        radius: 9
                        color: Theme.surfaceRaised
                        clip: true

                        // Music.artForIndex() resolves one row's cover and
                        // memoises it by track key, so scrolling the rail does
                        // not re-read files. `cache: true` keeps the decoded
                        // texture too — these are the same few covers over and
                        // over, and re-decoding them was the "always loading".
                        Image {
                            id: rowArt
                            anchors.fill: parent
                            // artRevision is folded in so this binding re-runs
                            // when a background lookup lands — a slot call
                            // alone gives QML nothing to depend on.
                            source: (Music && Music.artRevision >= 0)
                                    ? Music.artForIndex(queueRow.trackIndex) : ""
                            sourceSize.width: 88
                            sourceSize.height: 88
                            fillMode: Image.PreserveAspectCrop
                            asynchronous: true
                            cache: true
                            smooth: true
                            visible: status === Image.Ready
                        }

                        Text {
                            anchors.centerIn: parent
                            visible: !rowArt.visible
                            text: "♫"
                            color: queueRow.isCurrent ? Theme.accent : Theme.inkGhost
                            font.family: Theme.fontFamily
                            font.pixelSize: 18
                        }
                    }

                    Column {
                        anchors.left: rowThumb.right
                        anchors.leftMargin: 12
                        anchors.right: parent.right
                        anchors.rightMargin: 14
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 1

                        Text {
                            width: parent.width
                            text: modelData.title
                            color: queueRow.isCurrent ? Theme.accent : Theme.ink
                            font.family: Theme.fontFamily
                            font.pixelSize: 15
                            font.weight: Font.DemiBold
                            elide: Text.ElideRight
                        }

                        Text {
                            width: parent.width
                            visible: modelData.artist !== ""
                            text: modelData.artist
                            color: Theme.inkFaint
                            font.family: Theme.fontFamily
                            font.pixelSize: 13
                            elide: Text.ElideRight
                        }
                    }

                    MouseArea {
                        id: rowMa
                        anchors.fill: parent
                        hoverEnabled: true
                        onClicked: if (Music) Music.selectFile(queueRow.trackIndex)
                    }
                }

                Text {
                    anchors.centerIn: parent
                    visible: queueView.count === 0
                    text: "Nothing queued"
                    color: Theme.inkFaint
                    font.family: Theme.fontFamily
                    font.pixelSize: 15
                }
            }
        }
    }


    // ── sheets ─────────────────────────────────────────────────────────
    // UI.Sheet.close() assigns `opened` from JS, which severs the declarative
    // binding for good; every close handler re-arms it, or the sheet can only
    // ever be opened once.
    Sheets.LibrarySheet {
        z: 100
        opened: root.libraryOpen
        onClosed: {
            root.libraryOpen = false;
            opened = Qt.binding(function () { return root.libraryOpen; });
        }
    }

    Sheets.SearchSheet {
        z: 100
        opened: root.searchOpen
        onClosed: {
            root.searchOpen = false;
            opened = Qt.binding(function () { return root.searchOpen; });
        }
    }

    Sheets.LyricsSheet {
        z: 100
        opened: root.lyricsOpen
        onClosed: {
            root.lyricsOpen = false;
            opened = Qt.binding(function () { return root.lyricsOpen; });
        }
    }
}
