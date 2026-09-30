import QtQuick
import "../components" as UI

/**
 * Centred yes/no confirmation.
 *
 * Used by all seven destructive tiles — Restart Audio, Restart MPRIS, Delete
 * Cache, Close App, Restart App, Reboot, Shutdown.
 *
 *     confirmSheet.open("Reboot: Restart the device?",
 *                       function () { Settings.rebootSystem(); });
 *
 * Tapping the backdrop dismisses without running the callback, which is what
 * the legacy overlay did too.
 */
Item {
    id: root

    property string title: ""
    property string message: ""
    property string confirmText: "Yes"
    property string cancelText: "No"
    /** Invoked only when the user picks the confirm button. */
    property var    callback: null
    property bool   opened: false

    signal closed()

    anchors.fill: parent
    visible: opacity > 0.01
    opacity: opened ? 1.0 : 0.0
    enabled: opened
    Behavior on opacity { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }

    function open(titleText, cb) {
        root.title = titleText;
        root.callback = cb;
        root.opened = true;
    }

    function dismiss() {
        root.opened = false;
        root.callback = null;
        root.closed();
    }

    function accept() {
        var cb = root.callback;
        root.opened = false;
        root.callback = null;
        if (cb)
            cb();
        root.closed();
    }

    // Backdrop — dismisses, never runs the callback.
    Rectangle {
        anchors.fill: parent
        color: Theme.scrim
        MouseArea {
            anchors.fill: parent
            onClicked: root.dismiss()
            onWheel: function (wheel) { wheel.accepted = true; }
        }
    }

    UI.Card {
        anchors.centerIn: parent
        width: Math.min(root.width - 80, 460)
        height: dialogCol.implicitHeight + 52
        raised: true

        // Swallow taps on the card so they do not reach the backdrop.
        MouseArea { anchors.fill: parent }

        Column {
            id: dialogCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: 26
            anchors.rightMargin: 26
            anchors.verticalCenter: parent.verticalCenter
            spacing: 20

            Text {
                width: parent.width
                text: root.title
                color: Theme.ink
                font.family: Theme.fontFamily
                font.pixelSize: 20
                font.weight: Font.Bold
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
            }

            Text {
                width: parent.width
                visible: root.message !== ""
                text: root.message
                color: Theme.inkFaint
                font.family: Theme.fontFamily
                font.pixelSize: 14
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
            }

            Row {
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: 12

                UI.PillButton {
                    text: root.confirmText
                    variant: "danger"
                    implicitWidth: 150
                    height: 50
                    onClicked: root.accept()
                }

                UI.PillButton {
                    text: root.cancelText
                    variant: "raised"
                    implicitWidth: 150
                    height: 50
                    onClicked: root.dismiss()
                }
            }
        }
    }
}
