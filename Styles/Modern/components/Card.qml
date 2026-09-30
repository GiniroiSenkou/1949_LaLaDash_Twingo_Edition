import QtQuick

/**
 * Flat surface panel — the modern skin's only container chrome.
 * No gradient, no border, no shadow: depth comes from the surface step alone.
 */
Rectangle {
    id: card
    property bool raised: false
    property int  padding: Theme.pad

    color: raised ? Theme.surfaceRaised : Theme.surface
    radius: Theme.radiusCard
}
