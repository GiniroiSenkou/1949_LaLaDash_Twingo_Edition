import QtQuick
import "../components" as UI

/**
 * Search — the legacy ⌕ drop-down, re-cut as a sheet.
 *
 * Typing pushes straight into the bridge (Music.setSearchQuery), which
 * re-filters and emits playlistChanged; the result list is a snapshot taken
 * on that signal rather than a binding on Music.visibleTracks, which
 * materialises the whole library every time it is read.
 *
 * Opening seeds the query from whatever is in the field and takes focus;
 * closing clears the field and the bridge filter, exactly as legacy did.
 */
UI.Sheet {
    id: sheet

    title: "Search"
    subtitle: opened ? (results.length + (results.length === 1 ? " track" : " tracks")) : ""

    property var results: []

    function _refresh() {
        results = (opened && Music) ? Music.visibleTracks : [];
    }

    onOpenedChanged: {
        if (opened) {
            if (Music) Music.setSearchQuery(input.text);
            _refresh();
            input.forceActiveFocus();
        } else {
            input.text = "";
            if (Music) Music.clearSearch();
            results = [];
        }
    }

    Connections {
        target: Music
        enabled: sheet.opened
        ignoreUnknownSignals: true
        function onPlaylistChanged() { sheet._refresh(); }
    }

    Item {
        anchors.fill: parent

        Rectangle {
            id: field
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            height: 52
            radius: Theme.radiusControl
            color: Theme.surface

            Text {
                id: searchGlyph
                anchors.left: parent.left
                anchors.leftMargin: 16
                anchors.verticalCenter: parent.verticalCenter
                text: "⌕"
                color: Theme.inkFaint
                font.family: Theme.fontFamily
                font.pixelSize: 24
            }

            TextInput {
                id: input
                anchors.left: searchGlyph.right
                anchors.leftMargin: 12
                anchors.right: parent.right
                anchors.rightMargin: 16
                anchors.verticalCenter: parent.verticalCenter
                height: parent.height - 14
                color: Theme.ink
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSize("widgets_secondary_text")
                verticalAlignment: TextInput.AlignVCenter
                selectByMouse: true
                clip: true
                onTextChanged: if (Music) Music.setSearchQuery(text)

                Text {
                    anchors.fill: parent
                    visible: parent.text === ""
                    text: "Search songs"
                    color: Theme.inkGhost
                    font: parent.font
                    verticalAlignment: Text.AlignVCenter
                }
            }
        }

        ListView {
            id: resultsView
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: field.bottom
            anchors.bottom: parent.bottom
            anchors.topMargin: 12
            clip: true
            spacing: 2
            boundsBehavior: Flickable.StopAtBounds
            cacheBuffer: 120
            model: sheet.results

            delegate: Rectangle {
                width: resultsView.width
                height: 48
                radius: Theme.radiusControl
                color: rowMa.pressed ? Theme.surfacePressed
                                     : (rowMa.containsMouse ? Theme.surfaceHover : "transparent")

                Column {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.leftMargin: 14
                    anchors.rightMargin: 14
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 1

                    Text {
                        width: parent.width
                        text: modelData.title
                        color: Theme.ink
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSize("widgets_secondary_text")
                        elide: Text.ElideRight
                    }

                    Text {
                        width: parent.width
                        visible: modelData.artist !== ""
                        text: modelData.artist
                        color: Theme.inkFaint
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSize("widgets_tertiary_text")
                        elide: Text.ElideRight
                    }
                }

                MouseArea {
                    id: rowMa
                    anchors.fill: parent
                    hoverEnabled: true
                    onClicked: {
                        if (Music) Music.selectFile(index);
                        sheet.close();
                    }
                }
            }

            Text {
                anchors.centerIn: parent
                visible: resultsView.count === 0
                text: input.text === "" ? "Type to search the library" : "No matches"
                color: Theme.inkFaint
                font.family: Theme.fontFamily
                font.pixelSize: 15
            }
        }
    }
}
