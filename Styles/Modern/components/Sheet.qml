import QtQuick
import QtQuick.Window

/**
 * Full-bleed overlay: header, free body, optional footer.
 *
 * `fullScreen` reparents the sheet to the window's contentItem so it covers the
 * status strip and the dock too. Pickers use it with `chromeless`, which drops
 * the header entirely and floats a single Close button at the bottom — the
 * whole area then belongs to the content, so a grid can size itself to fit and
 * the user selects by tapping instead of scrolling.
 *
 * Body content is the default child. `headerControl` is a slot beside the title
 * for a page-level action (the lyrics mode cycler, for example).
 */
Item {
    id: root

    property string title: ""
    property string subtitle: ""
    property bool   opened: false
    property bool   showClose: true
    property bool   fullScreen: false
    /** No header; Close floats over the bottom of the content. */
    property bool   chromeless: false
    property int    contentMargin: 26
    property alias  footer: footerSlot.data
    property alias  headerControl: headerSlot.data

    default property alias body: bodySlot.data

    signal closed()

    // A full-screen sheet reparents to the window so it covers the status strip
    // and the dock. Done once on completion — `parent` cannot be bound to
    // "leave as-is", and fullScreen is fixed per call site.
    Component.onCompleted: {
        if (fullScreen && Window.window)
            parent = Window.window.contentItem;
    }

    anchors.fill: parent
    z: fullScreen ? 9000 : 50

    visible: opacity > 0.01
    opacity: opened ? 1.0 : 0.0
    enabled: opened
    Behavior on opacity { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }

    function close() {
        opened = false;
        closed();
    }

    Rectangle {
        anchors.fill: parent
        color: Theme.sheet
        MouseArea {
            anchors.fill: parent
            onWheel: function (w) { w.accepted = true; }
            // A chromeless sheet with no Close button would otherwise trap the
            // user, so empty space dismisses it. Content sits above this, so
            // only genuine misses reach here.
            onClicked: if (root.chromeless && !root.showClose) root.close()
        }
    }

    // ── header ─────────────────────────────────────────────────────────
    Item {
        id: header
        visible: !root.chromeless
        height: visible ? 46 : 0
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.topMargin: 14
        anchors.leftMargin: root.contentMargin
        anchors.rightMargin: root.contentMargin

        Row {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            spacing: 14

            Text {
                text: root.title
                color: Theme.ink
                font.family: Theme.fontFamily
                font.pixelSize: 24
                font.weight: Font.Bold
                anchors.verticalCenter: parent.verticalCenter
            }
            Text {
                text: root.subtitle
                color: Theme.inkFaint
                font.family: Theme.fontFamily
                font.pixelSize: 14
                anchors.verticalCenter: parent.verticalCenter
            }
        }

        Item {
            id: headerSlot
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            width: childrenRect.width
            height: childrenRect.height
        }
    }

    // ── body ───────────────────────────────────────────────────────────
    Item {
        id: bodySlot
        anchors.top: root.chromeless ? parent.top : header.bottom
        anchors.topMargin: root.chromeless ? root.contentMargin : 10
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: footerRow.top
        anchors.leftMargin: root.contentMargin
        anchors.rightMargin: root.contentMargin
        anchors.bottomMargin: 10
    }

    // ── footer ─────────────────────────────────────────────────────────
    Row {
        id: footerRow
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 16
        anchors.horizontalCenter: root.chromeless ? parent.horizontalCenter : undefined
        anchors.left: root.chromeless ? undefined : parent.left
        anchors.leftMargin: root.chromeless ? 0 : root.contentMargin
        spacing: 10
        height: Math.max(footerSlot.childrenRect.height, closeBtn.visible ? closeBtn.height : 0)

        Item {
            id: footerSlot
            width: childrenRect.width
            height: childrenRect.height
            anchors.verticalCenter: parent.verticalCenter
        }

        PillButton {
            id: closeBtn
            visible: root.showClose
            text: "Close"
            variant: root.chromeless ? "accent" : "raised"
            implicitWidth: root.chromeless ? 160 : 110
            implicitHeight: root.chromeless ? 52 : 44
            anchors.verticalCenter: parent.verticalCenter
            onClicked: root.close()
        }
    }

    Keys.onEscapePressed: root.close()
}
