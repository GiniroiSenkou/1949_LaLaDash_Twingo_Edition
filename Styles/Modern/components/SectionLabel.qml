import QtQuick

/** Uppercase group heading — "ALERTS", "CORE", "SOUND LAYERS". */
Text {
    property string label: ""
    text: label.toUpperCase()
    color: Theme.inkDim
    font.family: Theme.fontFamily
    font.pixelSize: 13
    font.weight: Font.Bold
    font.letterSpacing: 1.3
    topPadding: 6
    bottomPadding: 2
}
