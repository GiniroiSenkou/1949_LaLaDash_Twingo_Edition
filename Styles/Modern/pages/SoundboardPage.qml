import QtQuick
import QtQuick.Layouts
import "../components" as UI
import "../sheets" as Sheets

/**
 * Soundboard — three flat cards on `ground`: five favourite slots, the three
 * big playback actions, and the output volume.
 *
 * The mockup has no soundboard screen, so this is built from its vocabulary:
 * `surface` cards at `radiusCard`, `surfaceRaised` controls inside them, one
 * accent, and a lot of empty space. No timers and no animations — the legacy
 * page had none either, and the page must cost nothing while it is hidden.
 */
Item {
    id: root

    /** Favourite at `i`, or null when the slot is empty. */
    function favorite(i) {
        if (!Soundboard)
            return null;
        var favs = Soundboard.favorites;
        if (favs && favs.length > i && favs[i] && favs[i].name)
            return favs[i];
        return null;
    }

    // Volume commits on release only (hammering PulseAudio on every drag frame
    // is what the legacy page was avoiding); the readout still follows the drag.
    property bool  dragging: false
    property real  dragValue: 0.0
    readonly property real shownVolume: dragging ? dragValue
                                                 : (Soundboard ? Soundboard.volume : 0.5)

    ColumnLayout {
        anchors.fill: parent
        anchors.topMargin: 6
        anchors.leftMargin: Theme.pad
        anchors.rightMargin: Theme.pad
        anchors.bottomMargin: Theme.pad
        spacing: Theme.gap

        // ── favourites ──────────────────────────────────────────────────
        UI.Card {
            Layout.fillWidth: true
            Layout.preferredHeight: 162

            UI.SectionLabel {
                id: favHeading
                label: "Favourites"
                anchors.left: parent.left
                anchors.leftMargin: 20
                anchors.top: parent.top
                anchors.topMargin: 12
            }

            Row {
                id: favRow
                anchors.left: parent.left
                anchors.leftMargin: 20
                anchors.right: parent.right
                anchors.rightMargin: 20
                anchors.top: favHeading.bottom
                anchors.topMargin: 8
                anchors.bottom: parent.bottom
                anchors.bottomMargin: 20
                spacing: 12

                Repeater {
                    model: 5

                    UI.PillButton {
                        id: slotBtn

                        readonly property var fav: root.favorite(index)
                        readonly property bool assigned: fav !== null

                        width: (favRow.width - favRow.spacing * 4) / 5
                        height: favRow.height
                        radius: Theme.radiusControl
                        variant: assigned ? "raised" : "outline"
                        // Content is drawn below so a long sound name can elide.
                        text: ""

                        onClicked: {
                            if (assigned)
                                Soundboard.playFavorite(index);
                            else
                                picker.openFor("favorite", index);
                        }
                        onPressAndHold: {
                            if (assigned)
                                picker.openFor("favorite", index);
                        }

                        Column {
                            anchors.centerIn: parent
                            width: parent.width - 24
                            spacing: 5

                            Text {
                                width: parent.width
                                visible: slotBtn.assigned
                                text: "SLOT " + (index + 1)
                                color: Theme.accent
                                font.family: Theme.fontFamily
                                font.pixelSize: 11
                                font.weight: Font.Bold
                                font.letterSpacing: 1.1
                                horizontalAlignment: Text.AlignHCenter
                            }

                            Text {
                                width: parent.width
                                text: slotBtn.assigned ? slotBtn.fav.name : "Slot " + (index + 1)
                                color: slotBtn.assigned ? Theme.ink : Theme.inkGhost
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSize("settings_primary_text")
                                font.weight: slotBtn.assigned ? Font.DemiBold : Font.Medium
                                horizontalAlignment: Text.AlignHCenter
                                wrapMode: Text.WordWrap
                                maximumLineCount: 2
                                elide: Text.ElideRight
                                lineHeight: 1.15
                            }
                        }
                    }
                }
            }
        }

        // ── the three actions ───────────────────────────────────────────
        UI.Card {
            Layout.fillWidth: true
            Layout.fillHeight: true

            UI.SectionLabel {
                id: playbackHeading
                label: "Playback"
                anchors.left: parent.left
                anchors.leftMargin: 20
                anchors.top: parent.top
                anchors.topMargin: 12
            }

            Row {
                id: actionRow
                anchors.left: parent.left
                anchors.leftMargin: 20
                anchors.right: parent.right
                anchors.rightMargin: 20
                anchors.top: playbackHeading.bottom
                anchors.topMargin: 10
                anchors.bottom: parent.bottom
                anchors.bottomMargin: 20
                spacing: Theme.gap

                Repeater {
                    model: [
                        { "glyph": "▶", "label": "Play Sound",      "action": "play"   },
                        { "glyph": "■", "label": "Stop All Sounds", "action": "stop"   },
                        { "glyph": "∿", "label": "Random Sound",    "action": "random" }
                    ]

                    UI.PillButton {
                        id: actionBtn

                        readonly property bool primary: modelData.action === "play"

                        width: (actionRow.width - actionRow.spacing * 2) / 3
                        height: actionRow.height
                        radius: Theme.radiusCard
                        variant: "raised"
                        text: ""

                        onClicked: {
                            if (modelData.action === "play") {
                                picker.openFor("play", -1);
                            } else if (modelData.action === "stop") {
                                if (Soundboard) Soundboard.stop();
                            } else if (modelData.action === "random") {
                                if (Soundboard) Soundboard.playRandom();
                            }
                        }

                        Column {
                            anchors.centerIn: parent
                            width: parent.width - 40
                            spacing: 18

                            Rectangle {
                                width: 88
                                height: 88
                                radius: width / 2
                                color: actionBtn.primary ? Theme.accent : Theme.surface
                                anchors.horizontalCenter: parent.horizontalCenter

                                Text {
                                    anchors.centerIn: parent
                                    text: modelData.glyph
                                    color: actionBtn.primary ? Theme.accentInk : Theme.ink
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 34
                                }
                            }

                            Text {
                                width: parent.width
                                text: modelData.label
                                color: Theme.ink
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSize("widgets_primary_text")
                                font.weight: Font.DemiBold
                                horizontalAlignment: Text.AlignHCenter
                                wrapMode: Text.WordWrap
                                maximumLineCount: 2
                                elide: Text.ElideRight
                            }
                        }
                    }
                }
            }
        }

        // ── volume ──────────────────────────────────────────────────────
        UI.Card {
            Layout.fillWidth: true
            Layout.preferredHeight: volumeRow.implicitHeight + 32

            UI.ListRow {
                id: volumeRow
                anchors.left: parent.left
                anchors.leftMargin: 20
                anchors.right: parent.right
                anchors.rightMargin: 20
                anchors.verticalCenter: parent.verticalCenter
                label: "Volume"
                sub: "Soundboard output level"
                divider: false

                Row {
                    spacing: 16

                    UI.Slider {
                        id: volumeSlider
                        width: 420
                        anchors.verticalCenter: parent.verticalCenter
                        value: Soundboard ? Soundboard.volume : 0.5
                        onMoved: function (v) {
                            root.dragValue = v;
                            root.dragging = true;
                        }
                        onReleased: function (v) {
                            root.dragValue = v;
                            root.dragging = false;
                            if (Soundboard) Soundboard.setVolume(v);
                        }
                    }

                    Text {
                        width: 52
                        anchors.verticalCenter: parent.verticalCenter
                        text: Math.round(root.shownVolume * 100) + "%"
                        color: Theme.inkDim
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSize("widgets_secondary_text")
                        horizontalAlignment: Text.AlignRight
                    }
                }
            }
        }
    }

    // ── picker ──────────────────────────────────────────────────────────
    Sheets.SoundPickerSheet {
        id: picker
        z: 50
    }
}
