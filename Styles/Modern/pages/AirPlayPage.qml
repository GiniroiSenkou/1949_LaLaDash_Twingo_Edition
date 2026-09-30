import QtQuick
import QtQuick.Window
import "../components" as UI

/**
 * AirPlay page (Nav index 3) — iPhone screen-mirroring control.
 *
 * Nav index 3 was once called "Map" for historical reasons; it has never drawn
 * a map. Four bridge-driven states.
 *
 * The four states are mutually exclusive and come entirely from AirPhoneBridge;
 * there is no manual state switch. `rpiplay` paints its own video window over
 * this page while mirroring, so the active state is deliberately just a pill.
 *
 * Animation policy: the waiting pulse is gated on page visibility. The legacy
 * blinking "mirroring" dot ran with `running: true` — an ungated infinite loop
 * that kept QSGRenderThread awake forever — and is now a static dot.
 */
Item {
    id: root

    // ── state ───────────────────────────────────────────────────────────
    // A mirror counts as live as soon as its window is inside our window.
    //
    // `active` alone is not enough: it is inferred from a PulseAudio sink
    // input polled every 3 s, so a picture that is already on screen still
    // reads as "waiting" for up to a poll — and a muted mirror never flips it
    // at all. That gap is what made the "Waiting for iPhone…" card flash over
    // a running mirror each time the page was opened.
    readonly property bool mirroring:
        AirPhone ? (AirPhone.active || AirPhone.mirrorWindowReady) : false

    // Evaluated in the legacy order, cascaded so exactly one branch wins.
    readonly property string mode: {
        if (!AirPhone || !AirPhone.enabled)            return "disabled";
        if (!AirPhone.running && !root.mirroring)      return "stopped";
        if (AirPhone.running && !root.mirroring)       return "waiting";
        return "active";
    }

    readonly property string titleText:
        mode === "disabled" ? "AirPlay Disabled"
      : mode === "stopped"  ? "AirPlay Stopped"
      : "Waiting for iPhone…"

    readonly property string bodyText:
        mode === "disabled" ? "Enable in Settings › Core › AirPhone"
      : mode === "waiting"  ? "On your iPhone: Control Centre → Screen Mirroring → LaLaDash"
      : ""

    // stopped → the failure reason; waiting → what rpiplay last said.
    readonly property string detailText: {
        if (!AirPhone) return "";
        if (mode === "stopped") return AirPhone.lastError || AirPhone.lastLog || "";
        if (mode === "waiting") return AirPhone.lastLog || AirPhone.commandLine || "";
        return "";
    }

    readonly property string actionText:
        mode === "disabled" ? "Open Settings"
      : mode === "stopped"  ? "Start AirPlay"
      : "Stop"

    // Opening the page deliberately does *not* start the receiver. Legacy did,
    // which together with the boot autostart meant rpiplay was effectively
    // always running; starting it is now the "Start AirPlay" button's job.
    onVisibleChanged: {
        // Leaving the page must drop fullscreen with it, or the mirror keeps
        // the whole window — and the input grab that goes with it — over a
        // page that is no longer showing.
        if (!visible && AirPhone) AirPhone.setVideoFullscreen(false);
        _syncVideoSurface();
    }

    // ── where the mirror goes ───────────────────────────────────────────
    // rpiplay's GStreamer renderer owns a real X11 window of its own; the
    // bridge reparents it into this app's window, but it cannot know where
    // the page sits. `videoSurface` is that answer — an empty placeholder
    // whose geometry is pushed to the bridge whenever it changes. Nothing is
    // drawn into it from QML; it exists purely to be measured.
    //
    // Height of the control strip the mirror must not cover. That window is a
    // real X11 child stacked above the entire scene graph, so anything QML
    // draws inside the video rect is simply not visible — the strip has to be
    // carved out of the area handed over, never layered on top of it.
    readonly property int controlStripH: 44

    Item {
        id: videoSurface
        visible: false
        anchors.fill: parent
        anchors.topMargin: Theme.pad
                           + (root.mode === "active" ? root.controlStripH + Theme.pad : 0)
        anchors.bottomMargin: Theme.pad
        anchors.leftMargin: Theme.pad
        anchors.rightMargin: Theme.pad

        onWidthChanged:  root._syncVideoSurface()
        onHeightChanged: root._syncVideoSurface()
        onXChanged:      root._syncVideoSurface()
        onYChanged:      root._syncVideoSurface()
    }

    readonly property bool videoFullscreen:
        AirPhone ? AirPhone.videoFullscreen : false

    function _syncVideoSurface() {
        if (!AirPhone) return;
        // Fullscreen hands over the whole window, top bar and dock included.
        // The geometry is in window coordinates anyway, so the page is free to
        // offer an area larger than itself.
        if (root.videoFullscreen) {
            AirPhone.setVideoGeometry(0, 0, Window.width, Window.height);
            AirPhone.setVideoVisible(root.visible && AirPhone.running);
            return;
        }
        if (!videoSurface.width || !videoSurface.height) return;
        // Window coordinates: the bridge reparents into the app window, so
        // the child's position is relative to that, not to the screen.
        var p = videoSurface.mapToItem(null, 0, 0);
        AirPhone.setVideoGeometry(Math.round(p.x), Math.round(p.y),
                                  Math.round(videoSurface.width),
                                  Math.round(videoSurface.height));
        // Gated on `running`, not on `mode === "active"`. Active state is
        // inferred from a PulseAudio sink input, so a mirror whose audio does
        // not go through Pulse would never flip it — and the picture would
        // stay hidden for no reason. While rpiplay is merely running there is
        // no video window to show anyway, so this costs nothing.
        AirPhone.setVideoVisible(root.visible && AirPhone.running);
    }

    onModeChanged: _syncVideoSurface()
    onVideoFullscreenChanged: _syncVideoSurface()
    Component.onCompleted: _syncVideoSurface()

    // ── disabled / stopped / waiting: one centred card ───────────────────
    UI.Card {
        id: stateCard
        visible: root.mode !== "active"
        anchors.centerIn: parent
        width: Math.min(root.width - Theme.pad * 2, 620)
        height: col.implicitHeight + 72

        Column {
            id: col
            anchors.centerIn: parent
            width: parent.width - 56
            spacing: 20

            // airphone.png is a pure-black glyph on transparent, so it is
            // invisible straight onto a dark surface (it was in the legacy page
            // too). It gets an `ink` plate — the mockup's own solid-disc-with-
            // dark-glyph vocabulary — and the plate stays opaque so the
            // per-state icon opacity still reads as a dim/bright glyph.
            Item {
                id: iconHolder
                width: 112
                height: 112
                anchors.horizontalCenter: parent.horizontalCenter

                // Legacy pulse, 1.00 → 1.12 → 1.00 InOutSine. Gated on the
                // waiting state AND page visibility so it cannot spin while
                // the page is off-screen.
                SequentialAnimation on scale {
                    running: root.visible && root.mode === "waiting"
                    loops: Animation.Infinite
                    NumberAnimation { to: 1.12; duration: 800; easing.type: Easing.InOutSine }
                    NumberAnimation { to: 1.00; duration: 800; easing.type: Easing.InOutSine }
                    onRunningChanged: if (!running) iconHolder.scale = 1.0
                }

                Rectangle {
                    id: iconPlate
                    anchors.centerIn: parent
                    width: 104
                    height: 104
                    radius: width / 2
                    color: Theme.ink
                }

                Image {
                    id: icon
                    anchors.centerIn: parent
                    source: iconsPath + "/Settings/airphone.png"
                    width: 72
                    height: 72
                    sourceSize.width: 144
                    sourceSize.height: 144
                    fillMode: Image.PreserveAspectFit
                    smooth: true
                    asynchronous: true
                    opacity: root.mode === "disabled" ? 0.45
                           : root.mode === "stopped"  ? 0.6
                           : 1.0
                    Behavior on opacity { NumberAnimation { duration: 160 } }
                    onStatusChanged: if (status === Image.Error) source = iconsPath + "/side.png"
                }
            }

            Column {
                width: parent.width
                spacing: 4

                UI.SectionLabel {
                    label: "Screen mirroring"
                    width: parent.width
                    horizontalAlignment: Text.AlignHCenter
                }

                Text {
                    width: parent.width
                    text: root.titleText
                    color: Theme.ink
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSize("pages_primary_text")
                    font.weight: Font.Bold
                    horizontalAlignment: Text.AlignHCenter
                }
            }

            Text {
                width: Math.min(parent.width, 460)
                anchors.horizontalCenter: parent.horizontalCenter
                visible: root.bodyText !== ""
                text: root.bodyText
                color: Theme.inkDim
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSize("pages_secondary_text")
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.Wrap
            }

            // Diagnostics strip — a control step inside the card, never a border.
            Rectangle {
                id: detailStrip
                anchors.horizontalCenter: parent.horizontalCenter
                visible: root.detailText !== ""
                width: Math.min(parent.width, 560)
                height: detail.implicitHeight + 26
                radius: Theme.radiusControl
                color: Theme.surfaceRaised

                Text {
                    id: detail
                    anchors.centerIn: parent
                    width: parent.width - 32
                    text: root.detailText
                    color: (root.mode === "stopped" && AirPhone && AirPhone.lastError)
                           ? Theme.dangerSoft : Theme.inkFaint
                    font.family: Theme.fontFamily
                    font.pixelSize: root.mode === "waiting"
                                    ? Math.max(12, Theme.fontSize("pages_secondary_text") - 2)
                                    : Theme.fontSize("pages_secondary_text")
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.Wrap
                }
            }

            UI.PillButton {
                anchors.horizontalCenter: parent.horizontalCenter
                width: Math.max(implicitWidth, 220)
                height: 52
                text: root.actionText
                variant: root.mode === "waiting" ? "outline" : "accent"
                fontSize: Theme.fontSize("buttons_push_text")
                onClicked: {
                    if (root.mode === "disabled") {
                        if (Nav) Nav.navigateTo(6);      // int index; navigateToKey takes the string
                    } else if (root.mode === "stopped") {
                        if (AirPhone) AirPhone.start();
                    } else if (AirPhone) {
                        AirPhone.stop();
                    }
                }
            }
        }
    }

    // ── no usable video renderer ─────────────────────────────────────────
    // Without this the phone reports "mirroring" and the screen stays black,
    // which reads as a bug in this app rather than in the rpiplay build.
    // Either problem lands here: no usable renderer (black screen), or a
    // renderer that works but whose window could not be pulled into the page.
    readonly property string warningText: {
        if (!AirPhone) return "";
        if (!AirPhone.videoRendererAvailable) return AirPhone.rendererNote;
        return AirPhone.embedNote;
    }

    Rectangle {
        id: rendererWarning
        visible: root.warningText !== ""
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.margins: Theme.pad
        height: warnText.implicitHeight + 28
        radius: Theme.radiusControl
        color: Theme.dangerBg
        z: 10

        Text {
            id: warnText
            anchors.fill: parent
            anchors.margins: 14
            verticalAlignment: Text.AlignVCenter
            horizontalAlignment: Text.AlignHCenter
            text: root.warningText
            color: Theme.dangerSoft
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSize("pages_secondary_text")
            wrapMode: Text.WordWrap
        }
    }

    // ── active: top control strip, video window owns the rest ────────────
    Row {
        id: controlStrip
        // Covered by the mirror in fullscreen anyway; hidden so it is not
        // briefly drawn underneath on the way in and out.
        visible: root.mode === "active" && !root.videoFullscreen
        anchors.top: parent.top
        anchors.topMargin: Theme.pad
        anchors.horizontalCenter: parent.horizontalCenter
        height: root.controlStripH
        spacing: 12

        // Orientation. "Auto" follows the shape rpiplay's own window arrives
        // at, which tracks how the phone is being held; the two pins are for
        // when a source lies about itself or the driver wants it fixed.
        UI.Segmented {
            anchors.verticalCenter: parent.verticalCenter
            segmentHeight: 34
            fontSize: Theme.fontSize("widgets_tertiary_text")
            model: [
                { label: "Auto",      value: "auto" },
                { label: "Portrait",  value: "portrait" },
                { label: "Landscape", value: "landscape" }
            ]
            currentValue: AirPhone ? AirPhone.videoAspectMode : "auto"
            onSelected: function(value) {
                if (AirPhone) AirPhone.setVideoAspectMode(value);
            }
        }

        // Stream volume. rpiplay has no volume control of its own — the phone
        // sends a fixed level — so this rides the receiver's PulseAudio
        // sink-inputs. The value persists in [AirPlay] volume.
        Rectangle {
            id: volumePill
            anchors.verticalCenter: parent.verticalCenter
            width: volumeRow.implicitWidth + 28
            height: 44
            radius: height / 2
            color: Theme.surfaceRaised

            Row {
                id: volumeRow
                anchors.centerIn: parent
                spacing: 10

                UI.PillButton {
                    width: 34; height: 34; radius: 17
                    text: "−"
                    fontSize: 18
                    variant: "ghost"
                    anchors.verticalCenter: parent.verticalCenter
                    onClicked: if (AirPhone) AirPhone.setVolume((AirPhone.volume || 0) - 5)
                }

                Text {
                    width: 70
                    horizontalAlignment: Text.AlignHCenter
                    text: "Air " + (AirPhone ? AirPhone.volume : 0) + "%"
                    color: Theme.ink
                    font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSize("widgets_tertiary_text")
                    font.weight: Font.DemiBold
                    anchors.verticalCenter: parent.verticalCenter
                }

                UI.PillButton {
                    width: 34; height: 34; radius: 17
                    text: "+"
                    fontSize: 18
                    variant: "ghost"
                    anchors.verticalCenter: parent.verticalCenter
                    onClicked: if (AirPhone) AirPhone.setVolume((AirPhone.volume || 0) + 5)
                }
            }
        }

    Rectangle {
        id: activePill
        anchors.verticalCenter: parent.verticalCenter
        width: statusRow.implicitWidth + 36
        height: 44
        radius: height / 2
        color: Theme.okBg

        Row {
            id: statusRow
            anchors.centerIn: parent
            spacing: 12

            // Static, deliberately: the legacy 700 ms blink ran ungated and
            // never let the render thread idle.
            Rectangle {
                width: 10
                height: 10
                radius: 5
                color: Theme.ok
                anchors.verticalCenter: parent.verticalCenter
            }

            Text {
                text: {
                    var base = AirPhone && AirPhone.deviceName
                        ? "Mirroring — " + AirPhone.deviceName
                        : "Mirroring";
                    return AirPhone && AirPhone.musicDucked
                        ? base + " · music −" + AirPhone.duckPercent + "%"
                        : base;
                }
                color: Theme.okSoft
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSize("widgets_secondary_text")
                font.weight: Font.DemiBold
                anchors.verticalCenter: parent.verticalCenter
            }

            UI.PillButton {
                width: Math.max(implicitWidth, 100)
                height: 32
                radius: 16
                text: "Fullscreen"
                variant: "outline"
                fontSize: Theme.fontSize("widgets_tertiary_text")
                anchors.verticalCenter: parent.verticalCenter
                onClicked: if (AirPhone) AirPhone.setVideoFullscreen(true)
            }

            UI.PillButton {
                width: Math.max(implicitWidth, 80)
                height: 32
                radius: 16
                text: "Stop"
                variant: "outline"
                fontSize: Theme.fontSize("widgets_tertiary_text")
                anchors.verticalCenter: parent.verticalCenter
                onClicked: if (AirPhone) AirPhone.stop()
            }
        }
    }
    }
}
