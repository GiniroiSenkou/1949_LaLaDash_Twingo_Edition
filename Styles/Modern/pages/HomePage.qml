import QtQuick
import QtQuick.Layouts
import QtQuick.Window
import "../components" as UI

/**
 * Home — the modern skin's landing page.
 *
 * Two rows:
 *   top     alerts column (left, bottom-aligned)  ·  car render + ground shadow (right)
 *   bottom  238 px pair — now-playing card (left) · OBD card (right)
 *
 * The bottom-left slot is shared. Legacy kept the turn-off prompt and the
 * session-restore prompt as a floating overlay; here they take over the
 * now-playing card's slot instead, one at a time:
 *   session restore  >  turn off?  >  now playing
 * Both prompts stay gated on Settings.homeWidgetsEnabled exactly as the legacy
 * overlay was; the OBD card is part of the page proper and is always shown.
 */
Item {
    id: root

    readonly property int pagePad: 22
    readonly property int gap:     16
    readonly property int bottomH: 238

    // ── slot arbitration ───────────────────────────────────────────────
    readonly property bool _widgetsOn: Settings ? Settings.homeWidgetsEnabled : true
    readonly property bool _sessionVisible: _widgetsOn && sessionCard._open && !sessionCard._dismissed
    readonly property bool _turnoffVisible: _widgetsOn && !_sessionVisible
                                            && Nav.hasEngineEverOn && !Nav.engineOn

    /** Session-card entry point. */
    function showSession() { sessionCard.show(); }

    // ── helpers ────────────────────────────────────────────────────────
    function _stat(v) {
        var s = (v === undefined || v === null) ? "" : String(v);
        return s.length > 0 ? s : "--";
    }

    function _priorityColor(p) {
        switch (String(p || "info")) {
        case "critical":
        case "danger":  return Theme.danger;
        case "warning":
        case "warn":    return Theme.warn;
        case "success": return Theme.ok;
        default:        return Theme.accent;
        }
    }

    function _rgbaOf(c, a) {
        return "rgba(" + Math.round(c.r * 255) + ","
                       + Math.round(c.g * 255) + ","
                       + Math.round(c.b * 255) + "," + a + ")";
    }

    // ── one OBD readout: small caps label over a big value ─────────────
    component StatCell: Item {
        id: cell
        property string label: ""
        property string value: ""
        /** Empty = ordinary ink. Set to colour a value by its status. */
        property string valueColor: ""
        /** Optional caption under the value (battery uses it for charge state). */
        property string caption: ""

        implicitHeight: cellLabel.implicitHeight + 3 + cellValue.implicitHeight
                        + (cellCaption.visible ? cellCaption.implicitHeight + 1 : 0)

        Text {
            id: cellLabel
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            text: cell.label
            color: Theme.inkFaint
            font.family: Theme.fontFamily
            font.pixelSize: 13
            font.letterSpacing: 0.3
            elide: Text.ElideRight
        }

        Text {
            id: cellValue
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: cellLabel.bottom
            anchors.topMargin: 3
            text: root._stat(cell.value)
            color: cell.valueColor !== "" ? cell.valueColor : Theme.ink
            font.family: Theme.fontFamily
            font.pixelSize: 24
            font.weight: Font.Bold
            elide: Text.ElideRight
        }

        Text {
            id: cellCaption
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: cellValue.bottom
            anchors.topMargin: 1
            visible: cell.caption !== ""
            text: cell.caption
            color: Theme.inkFaint
            font.family: Theme.fontFamily
            font.pixelSize: 12
            elide: Text.ElideRight
        }
    }

    // ═══════════════════════════════════════════════════════════════════
    ColumnLayout {
        anchors.fill: parent
        anchors.leftMargin: root.pagePad
        anchors.rightMargin: root.pagePad
        anchors.topMargin: 0
        anchors.bottomMargin: 6
        spacing: root.gap

        // ── TOP: alerts · car ──────────────────────────────────────────
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 0
            spacing: root.gap

            // ── alerts column ──────────────────────────────────────────
            Item {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.preferredWidth: 1
                clip: true

                Column {
                    id: alertsCol
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    anchors.bottomMargin: 4
                    spacing: 10

                    UI.SectionLabel { label: "Alerts" }

                    // empty state — dashed outline pill
                    Item {
                        width: alertsCol.width
                        height: 96
                        visible: Alerts.model.count === 0

                        Canvas {
                            id: emptyPill
                            anchors.fill: parent
                            property color strokeColor: Theme.outline
                            onStrokeColorChanged: requestPaint()
                            onPaint: {
                                var ctx = getContext("2d");
                                ctx.reset();
                                var r = 20;
                                var x = 0.5, y = 0.5;
                                var w = width - 1, h = height - 1;
                                ctx.strokeStyle = strokeColor;
                                ctx.lineWidth = 1;
                                if (ctx.setLineDash)
                                    ctx.setLineDash([7, 6]);
                                ctx.beginPath();
                                ctx.moveTo(x + r, y);
                                ctx.lineTo(x + w - r, y);
                                ctx.arcTo(x + w, y, x + w, y + r, r);
                                ctx.lineTo(x + w, y + h - r);
                                ctx.arcTo(x + w, y + h, x + w - r, y + h, r);
                                ctx.lineTo(x + r, y + h);
                                ctx.arcTo(x, y + h, x, y + h - r, r);
                                ctx.lineTo(x, y + r);
                                ctx.arcTo(x, y, x + r, y, r);
                                ctx.stroke();
                            }
                        }

                        Text {
                            anchors.centerIn: parent
                            text: "Nothing to report"
                            color: Theme.inkFaint
                            font.family: Theme.fontFamily
                            font.pixelSize: 15
                        }
                    }

                    // live alerts
                    Column {
                        width: alertsCol.width
                        spacing: 10
                        visible: Alerts.model.count > 0

                        Repeater {
                            model: Alerts.model

                            Rectangle {
                                id: alertCard
                                property color tone: root._priorityColor(model.priority)
                                visible: index < 4
                                width: alertsCol.width
                                height: visible ? 66 : 0
                                radius: Theme.radiusCard
                                color: Theme.surface

                                Rectangle {
                                    anchors.left: parent.left
                                    anchors.leftMargin: 14
                                    anchors.verticalCenter: parent.verticalCenter
                                    width: 4
                                    height: parent.height - 26
                                    radius: 2
                                    color: alertCard.tone
                                }

                                Row {
                                    anchors.left: parent.left
                                    anchors.leftMargin: 30
                                    anchors.right: parent.right
                                    anchors.rightMargin: 18
                                    anchors.verticalCenter: parent.verticalCenter
                                    spacing: 12

                                    Image {
                                        id: alertIcon
                                        width: 30
                                        height: 30
                                        sourceSize.width: 60
                                        sourceSize.height: 60
                                        fillMode: Image.PreserveAspectFit
                                        asynchronous: true
                                        anchors.verticalCenter: parent.verticalCenter
                                        visible: status === Image.Ready
                                        source: model.icon
                                                ? ("file://" + iconsPath + "/Dash/" + model.icon)
                                                : ""
                                    }

                                    Text {
                                        width: parent.width - (alertIcon.visible ? 42 : 0)
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: model.text
                                        color: alertCard.tone
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 17
                                        font.weight: Font.DemiBold
                                        elide: Text.ElideRight
                                        maximumLineCount: 2
                                        wrapMode: Text.WordWrap
                                    }
                                }
                            }
                        }
                    }
                }
            }

            // ── car render ─────────────────────────────────────────────
            Item {
                id: carCell
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.preferredWidth: 1

                Canvas {
                    id: groundShadow
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.bottom: parent.bottom
                    anchors.bottomMargin: 14
                    width: Math.max(80, parent.width * 0.76)
                    height: 26
                    property color tone: Theme.scrim
                    onToneChanged: requestPaint()
                    onPaint: {
                        var ctx = getContext("2d");
                        ctx.reset();
                        if (width <= 0 || height <= 0)
                            return;
                        ctx.save();
                        ctx.translate(width / 2, height / 2);
                        ctx.scale(width / 2, height / 2);
                        var g = ctx.createRadialGradient(0, 0, 0, 0, 0, 1);
                        g.addColorStop(0.0, root._rgbaOf(tone, 0.75));
                        g.addColorStop(0.72, root._rgbaOf(tone, 0.0));
                        g.addColorStop(1.0, root._rgbaOf(tone, 0.0));
                        ctx.fillStyle = g;
                        ctx.beginPath();
                        ctx.arc(0, 0, 1, 0, Math.PI * 2);
                        ctx.fill();
                        ctx.restore();
                    }
                }

                Image {
                    id: carImage
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.verticalCenterOffset: -8
                    width: parent.width
                    height: parent.height * 0.88
                    fillMode: Image.PreserveAspectFit
                    smooth: true
                    mipmap: true
                    // Decoded off the GUI thread — a preset swap must not stall
                    // the scene graph while V3D uploads the texture.
                    asynchronous: true
                    cache: true
                    sourceSize.width: 640
                    sourceSize.height: 360

                    source: (!Modding || !Modding.engineSoundOn)
                            ? iconsPath + "/side.png"
                            : "file://" + Modding.carImagePath

                    onStatusChanged: {
                        if (status === Image.Error && String(source).indexOf("side.png") < 0)
                            source = iconsPath + "/side.png";
                    }

                    // ── arrival ─────────────────────────────────────────
                    // Picks up where the boot splash leaves off: the car
                    // appears where the splash's spinner was, in the middle of
                    // the screen, then drives out to its place on the page.
                    //
                    // Runs once per app start, never on later visits to Home —
                    // an animation that replayed every time the dock came back
                    // here would be a tic, not an entrance. Offsets are applied
                    // through a transform so none of the anchors move and the
                    // resting layout is untouched.
                    property real introDX: 0
                    property real introDY: 0
                    property real introScale: 1.0
                    property bool introDone: false
                    property int  _introTries: 0

                    transform: [
                        Scale {
                            origin.x: carImage.width / 2
                            origin.y: carImage.height / 2
                            xScale: carImage.introScale
                            yScale: carImage.introScale
                        },
                        Translate { x: carImage.introDX; y: carImage.introDY }
                    ]

                    function startIntro() {
                        if (introDone) return;
                        introDone = true;
                        // Hidden while the layout settles, so the car does not
                        // show at its resting place for a frame and then jump
                        // back to the middle to fly out again.
                        opacity = 0;
                        introDelay.start();
                    }

                    // The offset can only be measured once the layouts have
                    // placed this image. Measured too early, mapToItem still
                    // reports the pre-layout origin, the offset comes out with
                    // the wrong sign, and the car flies in from off the right
                    // edge instead of from the middle.
                    Timer {
                        id: introDelay
                        interval: 350
                        repeat: false
                        onTriggered: carImage._beginIntro()
                    }

                    function _beginIntro() {
                        var c = mapToItem(null, width / 2, height / 2);
                        var settled = width > 0 && height > 0 && Window.width > 0
                                      && c.x > 0 && c.x < Window.width;
                        if (!settled) {
                            // Bounded: a page that never lays out gives up and
                            // simply shows the car where it belongs.
                            if (++_introTries < 12) { introDelay.start(); return; }
                            opacity = 1;
                            return;
                        }
                        introDX = Window.width / 2 - c.x;
                        introDY = Window.height / 2 - c.y;
                        introScale = 1.16;
                        introAnim.start();
                    }

                    SequentialAnimation {
                        id: introAnim
                        NumberAnimation {
                            target: carImage; property: "opacity"
                            to: 1; duration: 260; easing.type: Easing.OutCubic
                        }
                        ParallelAnimation {
                            NumberAnimation {
                                target: carImage; property: "introDX"
                                to: 0; duration: 820; easing.type: Easing.InOutCubic
                            }
                            NumberAnimation {
                                target: carImage; property: "introDY"
                                to: 0; duration: 820; easing.type: Easing.InOutCubic
                            }
                            NumberAnimation {
                                target: carImage; property: "introScale"
                                to: 1.0; duration: 820; easing.type: Easing.InOutCubic
                            }
                        }
                    }
                }
            }
        }

        // ── BOTTOM: now-playing / prompt · OBD ─────────────────────────
        // fillHeight defaults to true for a nested layout — pin it off so the
        // 238 px band stays fixed and the car row takes the slack.
        RowLayout {
            id: bottomRow
            Layout.fillWidth: true
            Layout.fillHeight: false
            transform: Translate { id: bottomSlide }
            Layout.preferredHeight: root.bottomH
            Layout.minimumHeight: root.bottomH
            Layout.maximumHeight: root.bottomH
            spacing: root.gap

            // ═══ shared left slot ═══════════════════════════════════════
            Item {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.preferredWidth: 1

                // ── now playing ────────────────────────────────────────
                UI.Card {
                    id: nowPlaying
                    anchors.fill: parent
                    visible: !root._sessionVisible && !root._turnoffVisible

                    MouseArea {
                        anchors.fill: parent
                        onClicked: Nav.navigateTo(4)
                    }

                    // The card is 238 px tall; the art is sized off it rather
                    // than fixed, so the artwork, the type and the transport
                    // all scale with the slot instead of clustering top-left.
                    Item {
                        anchors.fill: parent
                        anchors.margins: 22

                        // progress — full width along the foot of the card
                        Rectangle {
                            id: progressTrack
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.bottom: parent.bottom
                            height: 6
                            radius: 3
                            color: Theme.outline

                            Rectangle {
                                width: parent.width * (Music ? Music.progress : 0)
                                height: parent.height
                                radius: parent.radius
                                color: Theme.accent
                            }
                        }

                        // album art — square, as tall as the row allows (170 px)
                        Rectangle {
                            id: artThumb
                            anchors.left: parent.left
                            anchors.top: parent.top
                            anchors.bottom: progressTrack.top
                            anchors.bottomMargin: 18
                            width: height
                            radius: 18
                            color: Theme.surfaceRaised
                            clip: true

                            Text {
                                anchors.centerIn: parent
                                text: "♫"
                                color: Theme.inkGhost
                                font.family: Theme.fontFamily
                                font.pixelSize: 64
                                visible: art.status !== Image.Ready
                            }

                            Image {
                                id: art
                                anchors.fill: parent
                                sourceSize.width: 340
                                sourceSize.height: 340
                                fillMode: Image.PreserveAspectCrop
                                asynchronous: true
                                cache: true
                                smooth: true
                                visible: status === Image.Ready
                                source: Music ? Music.albumArtUrl : ""
                            }
                        }

                        // play / pause
                        UI.IconButton {
                            id: playBtn
                            anchors.right: parent.right
                            anchors.verticalCenter: artThumb.verticalCenter
                            width: 78
                            height: 78
                            radius: 39
                            checked: true
                            activeColor: Theme.accent
                            glyph: (Music && Music.playing) ? "⏸" : "▶"
                            glyphSize: 34
                            onClicked: if (Music) Music.togglePlayPause()
                        }

                        // title / artist
                        Column {
                            anchors.left: artThumb.right
                            anchors.leftMargin: 20
                            anchors.right: playBtn.left
                            anchors.rightMargin: 20
                            anchors.verticalCenter: artThumb.verticalCenter
                            spacing: 6

                            Text {
                                width: parent.width
                                text: (Music && Music.title) ? Music.title : "Nothing playing"
                                color: Theme.ink
                                font.family: Theme.fontFamily
                                font.pixelSize: 28
                                font.weight: Font.Bold
                                elide: Text.ElideRight
                            }

                            Text {
                                width: parent.width
                                text: (Music && Music.artist) ? Music.artist : "—"
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: 19
                                elide: Text.ElideRight
                            }
                        }
                    }
                }

                // ── turn off? ──────────────────────────────────────────
                UI.Card {
                    id: turnoffCard
                    anchors.fill: parent
                    visible: root._turnoffVisible

                    // Same slot, same treatment as the session prompt: the copy
                    // sits centred above the button instead of hugging the top.
                    Column {
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.leftMargin: 24
                        anchors.rightMargin: 24
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.verticalCenterOffset: -(shutdownBtn.height + 24) / 2
                        spacing: 10

                        Text {
                            width: parent.width
                            text: "Turn off?"
                            color: Theme.ink
                            font.family: Theme.fontFamily
                            font.pixelSize: 28
                            font.weight: Font.Bold
                            elide: Text.ElideRight
                        }

                        Text {
                            width: parent.width
                            text: "Engine is off. System is ready to power down."
                            color: Theme.inkFaint
                            font.family: Theme.fontFamily
                            font.pixelSize: 17
                            wrapMode: Text.WordWrap
                            maximumLineCount: 2
                        }
                    }

                    UI.PillButton {
                        id: shutdownBtn
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.bottom: parent.bottom
                        anchors.margins: 24
                        height: 56
                        variant: "danger"
                        text: "⏻  Shut Down"
                        fontSize: Theme.fontSize("pages_primary_text")
                        onClicked: if (Settings) Settings.shutdownSystem()
                    }
                }

                // ── session restore ────────────────────────────────────
                UI.Card {
                    id: sessionCard
                    anchors.fill: parent

                    property bool _open: false
                    property bool _dismissed: false

                    visible: root._sessionVisible

                    // One-shot per app run: never re-opens once dismissed.
                    function show() {
                        if (_open || _dismissed || (Music && Music.playing))
                            return;
                        Nav.navigateTo(0);
                        _open = true;
                    }
                    function dismiss() {
                        _open = false;
                        _dismissed = true;
                    }

                    Connections {
                        target: Music
                        function onTrackChanged() {
                            if (Music && Music.playing && sessionCard._open)
                                sessionCard.dismiss();
                        }
                    }

                    // Centred in the space above the button rather than pinned
                    // to the top — the slot is 238 px and two short lines were
                    // leaving a hole in the middle of the card.
                    Column {
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.leftMargin: 24
                        anchors.rightMargin: 24
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.verticalCenterOffset: -(resumeBtn.height + 24) / 2
                        spacing: 14

                        Row {
                            width: parent.width
                            spacing: 12

                            Image {
                                id: tagIcon
                                width: 30
                                height: 30
                                sourceSize.width: 60
                                sourceSize.height: 60
                                fillMode: Image.PreserveAspectFit
                                asynchronous: true
                                smooth: true
                                anchors.verticalCenter: parent.verticalCenter
                                source: iconsPath + "/Tags/"
                                        + ((Music && Music.sessionTagIcon) ? Music.sessionTagIcon
                                                                           : "default.png")
                                onStatusChanged: {
                                    if (status === Image.Error
                                            && String(source).indexOf("default.png") < 0)
                                        source = iconsPath + "/Tags/default.png";
                                }
                            }

                            Text {
                                width: parent.width - 42
                                anchors.verticalCenter: parent.verticalCenter
                                text: (Music && Music.sessionTag && Music.sessionTag !== "All")
                                      ? Music.sessionTag : "All tracks"
                                color: Theme.inkDim
                                font.family: Theme.fontFamily
                                font.pixelSize: 15
                                font.weight: Font.Bold
                                font.letterSpacing: 1.3
                                elide: Text.ElideRight
                            }
                        }

                        Text {
                            width: parent.width
                            text: (Music && Music.sessionArtist)
                                  ? Music.sessionArtist
                                    + (Music.sessionTitle ? " - " + Music.sessionTitle : "")
                                  : ((Music && Music.sessionTitle) ? Music.sessionTitle
                                                                   : "Continue where you left off")
                            color: Theme.ink
                            font.family: Theme.fontFamily
                            font.pixelSize: 27
                            font.weight: Font.Bold
                            wrapMode: Text.WordWrap
                            maximumLineCount: 2
                            elide: Text.ElideRight
                        }
                    }

                    UI.PillButton {
                        id: resumeBtn
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.bottom: parent.bottom
                        anchors.margins: 24
                        height: 56
                        variant: "accent"
                        text: "▶  Resume"
                        fontSize: Theme.fontSize("pages_primary_text")
                        onClicked: {
                            sessionCard.dismiss();
                            if (Music)
                                Music.restoreSession();
                        }
                    }
                }
            }

            // ═══ OBD card ═══════════════════════════════════════════════
            UI.Card {
                id: obdCard
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.preferredWidth: 1

                readonly property bool _conn: Telemetry ? Telemetry.connected : false
                readonly property color _tone: _conn ? Theme.ok : Theme.danger

                Item {
                    anchors.fill: parent
                    anchors.margins: 20

                    // header
                    Item {
                        id: obdHeader
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        height: 32

                        Rectangle {
                            id: statusDot
                            anchors.left: parent.left
                            anchors.verticalCenter: parent.verticalCenter
                            width: 8
                            height: 8
                            radius: 4
                            color: obdCard._tone
                        }

                        Text {
                            anchors.left: statusDot.right
                            anchors.leftMargin: 9
                            anchors.right: retryChip.left
                            anchors.rightMargin: 12
                            anchors.verticalCenter: parent.verticalCenter
                            // "OBD Connected · CAN-BUS (STN 2 Mbaud) · Engine Off":
                            // the protocol in use is part of the status, so a
                            // link that came up on PIDs only is never mistaken
                            // for the CAN one. While a Retry runs it says so,
                            // and a failed one says why (ignition off, adapter
                            // silent) instead of just "Disconnected".
                            text: {
                                var base = (Telemetry && Telemetry.obdStatus) ? Telemetry.obdStatus : "Disconnected";
                                if (OBD && OBD.connecting) return "OBD Connecting…";
                                if (!Telemetry || !Telemetry.connected) {
                                    var err = OBD ? OBD.connectionErrorText : "";
                                    return err ? (base + " — " + err) : base;
                                }
                                var proto = OBD && OBD.protocol === "can"
                                            ? (Can && Can.connected ? "CAN-BUS" : "CAN-BUS (PIDs only)")
                                            : "OBD2";
                                return base.replace(" - ", " · " + proto + " · ");
                            }
                            color: obdCard._tone
                            font.family: Theme.fontFamily
                            font.pixelSize: 15
                            font.weight: Font.DemiBold
                            elide: Text.ElideRight
                        }

                        UI.PillButton {
                            id: retryChip
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            implicitWidth: 78
                            height: 32
                            variant: "outline"
                            text: "Retry"
                            fontSize: 13
                            enabled: !(OBD && OBD.connecting)
                            onClicked: if (OBD) OBD.connect()
                        }
                    }

                    // stat grid — 3 columns, matches the mockup.
                    // Written out rather than driven by an array model: an
                    // inline model literal is rebuilt on every Telemetry tick
                    // (~12 Hz), which would re-create all six delegates.
                    GridLayout {
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: obdHeader.bottom
                        anchors.topMargin: 16
                        columns: 3
                        rowSpacing: 14
                        columnSpacing: 14

                        StatCell {
                            Layout.fillWidth: true; Layout.preferredWidth: 1
                            label: "Today"
                            value: Telemetry ? Telemetry.todayKm : ""
                            caption: (Trip && Trip.todayCostText !== "—") ? Trip.todayCostText + " fuel" : ""
                        }
                        StatCell {
                            Layout.fillWidth: true; Layout.preferredWidth: 1
                            label: "Total"
                            value: Telemetry ? Telemetry.totalKm : ""
                        }
                        StatCell {
                            Layout.fillWidth: true; Layout.preferredWidth: 1
                            label: "Run Time"
                            value: Telemetry ? Telemetry.runTime : ""
                        }
                        // Replaces "Dist Clear": distance since the last DTC
                        // reset says nothing you act on from the driver's seat,
                        // whereas the charging state does — the caption is the
                        // point, the voltage on its own does not tell you
                        // whether the alternator is putting anything back.
                        StatCell {
                            Layout.fillWidth: true; Layout.preferredWidth: 1
                            label: "Battery"
                            value: Telemetry ? Telemetry.batteryText : ""
                            valueColor: Telemetry ? Telemetry.batteryColor : ""
                            caption: Telemetry ? Telemetry.batteryStatusText : ""
                        }
                        StatCell {
                            Layout.fillWidth: true; Layout.preferredWidth: 1
                            label: "Warmups"
                            value: Telemetry ? Telemetry.warmups : ""
                        }
                        StatCell {
                            Layout.fillWidth: true; Layout.preferredWidth: 1
                            label: "Codes"
                            value: Telemetry ? Telemetry.dtcCount : ""
                        }
                    }
                }
            }
        }
    }


    // The car's entrance waits for the page to actually be on screen: booting
    // with the engine already running lands on Dashboard, and the animation
    // would otherwise be spent behind it.
    onVisibleChanged: if (visible) { carImage.startIntro(); root._cardsIntro(); }

    // Settings → Animation → Intro: the music and OBD cards rise into place
    // once per page open. One short animation, nothing left running.
    function _cardsIntro() {
        if (!Nav || !Nav.introAnimationEnabled()) return;
        cardsIntroAnim.stop();
        bottomSlide.y = 40;
        bottomRow.opacity = 0;
        cardsIntroAnim.start();
    }
    ParallelAnimation {
        id: cardsIntroAnim
        NumberAnimation { target: bottomSlide; property: "y"; to: 0; duration: 350; easing.type: Easing.OutCubic }
        NumberAnimation { target: bottomRow; property: "opacity"; to: 1; duration: 350; easing.type: Easing.OutCubic }
    }

    // main.qml no longer drives this — the page arms its own session prompt.
    Component.onCompleted: {
        if (Music && Music.sessionAvailable && !Music.playing)
            root.showSession();
        if (visible) carImage.startIntro();
    }
}
