import QtQuick

/**
 * A live picture from an image provider, without the flicker.
 *
 * `Image { source: "image://cam/" + rev; cache: false; asynchronous: true }`
 * drops its texture the moment `source` changes and paints nothing until the
 * new one has loaded — on the dev PC that gap is invisible, on the Pi it is a
 * blank frame every frame: the flicker seen on both camera pages over USB.
 *
 * Two Images take turns instead: the front one keeps showing while the back
 * one loads the next rev, and they swap only once the back is Ready. A rev
 * that arrives while the back is still loading is simply skipped, which is
 * also the rate limiter a slow device needs — the picture never queues up.
 *
 * Usage:
 *   UI.LiveFrame { anchors.fill: parent; provider: "actioncam"; rev: ActionCam.frameRev
 *                  active: ActionCam.streamActive }
 */
Item {
    id: root

    property string provider: ""
    property int    rev: 0
    property bool   active: true
    property int    fillMode: Image.PreserveAspectFit
    /** True once a frame is on screen. */
    readonly property bool showing: _front === 0 ? imgA.status === Image.Ready : imgB.status === Image.Ready

    property int _front: 0

    Image {
        id: imgA
        anchors.fill: parent
        cache: false
        asynchronous: true
        fillMode: root.fillMode
        smooth: true
        visible: root._front === 0 && status === Image.Ready
        onStatusChanged: if (status === Image.Ready && root._front !== 0) root._front = 0
    }

    Image {
        id: imgB
        anchors.fill: parent
        cache: false
        asynchronous: true
        fillMode: root.fillMode
        smooth: true
        visible: root._front === 1 && status === Image.Ready
        onStatusChanged: if (status === Image.Ready && root._front !== 1) root._front = 1
    }

    onRevChanged: _next()
    onActiveChanged: {
        if (!active) { imgA.source = ""; imgB.source = ""; _front = 0; }
        else _next();
    }

    function _next() {
        if (!active || rev <= 0 || provider === "") return;
        var back = (_front === 0) ? imgB : imgA;
        if (back.status === Image.Loading) return;      // still busy: drop this rev
        back.source = "image://" + provider + "/" + rev;
    }
}
