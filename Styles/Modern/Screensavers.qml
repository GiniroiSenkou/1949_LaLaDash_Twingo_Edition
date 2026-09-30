import QtQuick
import "components" as UI

/**
 * Parked and driving screensavers, on the flat ground colour rather than the
 * legacy gradient. Everything inside is gated on `Nav.screensaverActive`, so
 * nothing here animates or ticks while the app is in normal use.
 *
 * The driving half is the "grudge2.0" instrument cluster: a rev dial left, a
 * speed dial right, and one card between them carrying the AirPlay mirror
 * when a phone is connected and the trip readout when it is not.
 *
 * Vertical space is the scarce resource on a 1280x800 panel, so both status
 * readouts are islands: pills sized to what they carry, gone entirely when
 * there is nothing to say. Driving: the warnings island sits under the clock
 * at the top, the alerts island under the car at the bottom of the centre
 * column. Parked: now-playing over the clock, the alert island under it, the
 * date small in the top-right corner.
 *
 * Appearing is animated once (`driving.intro`, a single NumberAnimation): the
 * dials come in from their own edges and the car travels down from where the
 * Dashboard draws it, so the change reads as the same car moving rather than a
 * new screen dropping over the old one. Nothing here loops.
 */
Item {
    id: root
    visible: Nav.screensaverActive

    /** Alert severity -> colour. Same mapping as HomePage and SidePanel: the
        three lists render the same `Alerts.model`, so a warning must not read
        as neutral here just because the screensaver is up. */
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

    // Colour bands and full-scale for the two bargraphs, straight from the
    // telemetry registry. Read once — they are constants, not live values.
    readonly property var speedBand: Telemetry ? Telemetry.gaugeBand("Speed") : []
    readonly property var rpmBand:   Telemetry ? Telemetry.gaugeBand("RPM")   : []

    /** Width of the centre card, and of the warning strip pinned above it —
        the design puts the chips over the mirror, not across the dials. */
    readonly property int centreW: Math.round(Math.min(width * 0.335, 440))

    /** Shape of the mirror as the bridge will actually place it, so the card
        that frames it can be cut to the same cloth. Falls back to 9:16 —
        upright — which is how a phone is held unless it is being used for
        video. */
    readonly property real mirrorAspect:
        (AirPhone && AirPhone.mirrorAspect > 0) ? AirPhone.mirrorAspect : (9.0 / 16.0)

    /** Numeral colour for a dial: plain ink until the value reaches its warn
        threshold, then the telemetry band colour. Colouring it by band at all
        times paints a perfectly healthy 2400 rpm bright green, which trains
        you to ignore the one thing the colour is for. */
    function _valueColor(value, band, bandColor) {
        if (band.length === 4 && value >= band[0]) return bandColor;
        return Theme.ink;
    }

    /**
     * The newest alert as a pill: severity stripe, its Dash icon, the text
     * (wrapping to two lines when long), and a "+N" for the rest. Both
     * screensavers draw one; `k` scales it (1 = the Dashboard's line).
     *
     * Read through a Repeater rather than an index lookup: Alerts.model is a
     * QAbstractListModel, which has no get() — the roles are only reachable
     * from a delegate. Same pattern HomePage and SidePanel use. Only index 0
     * is drawn; the rest collapse to zero width, which is what keeps the pill
     * the size of what it is actually showing.
     */
    component AlertIsland: Rectangle {
        id: island
        property real k: 1.0
        property int  maxW: 640

        readonly property bool hasAlerts: Alerts && Alerts.model && Alerts.model.count > 0
        readonly property int  padX:   Math.round(17 * k)
        readonly property int  padY:   Math.round(11 * k)
        readonly property int  iconPx: Math.round(22 * k)
        readonly property int  fontPx: Math.round(15 * k)

        visible: hasAlerts
        width: visible ? Math.min(maxW, islandRow.implicitWidth + padX * 2) : 0
        height: visible ? Math.max(Math.round(44 * k), islandRow.implicitHeight + padY * 2) : 0
        radius: Math.min(height / 2, Math.round(22 * k))
        color: Theme.surface

        Behavior on width {
            enabled: root.visible
            NumberAnimation { duration: 220; easing.type: Easing.OutCubic }
        }

        Row {
            id: islandRow
            anchors.centerIn: parent
            spacing: 0

            Repeater {
                model: Alerts ? Alerts.model : null

                Row {
                    visible: index === 0
                    spacing: Math.round(7 * island.k)

                    readonly property color tone: root._priorityColor(model.priority)
                    /** Room left for the text once the stripe, icon and count
                        have taken theirs. */
                    readonly property int textMaxW: island.maxW - island.padX * 2
                                                    - islandStripe.width - island.iconPx
                                                    - (islandCount.visible ? islandCount.implicitWidth + spacing : 0)
                                                    - spacing * 2

                    Rectangle {
                        id: islandStripe
                        width: Math.max(3, Math.round(1.6 * island.k))
                        height: Math.round(island.iconPx * 0.9)
                        radius: width / 2
                        color: parent.tone
                        anchors.verticalCenter: parent.verticalCenter
                    }

                    Image {
                        width: island.iconPx
                        height: island.iconPx
                        sourceSize.width: island.iconPx * 2
                        sourceSize.height: island.iconPx * 2
                        fillMode: Image.PreserveAspectFit
                        asynchronous: true
                        cache: true
                        anchors.verticalCenter: parent.verticalCenter
                        visible: status === Image.Ready
                        source: model.icon
                                ? ("file://" + iconsPath + "/Dash/" + model.icon)
                                : ""
                    }

                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        width: Math.min(implicitWidth, parent.textMaxW)
                        text: model.text !== undefined ? model.text : ""
                        color: parent.tone
                        font.family: Theme.fontFamily
                        font.pixelSize: island.fontPx
                        font.weight: Font.DemiBold
                        wrapMode: Text.WordWrap
                        maximumLineCount: 2
                        elide: Text.ElideRight
                    }

                    Text {
                        id: islandCount
                        anchors.verticalCenter: parent.verticalCenter
                        visible: Alerts && Alerts.model.count > 1
                        text: visible ? ("+" + (Alerts.model.count - 1)) : ""
                        color: Theme.inkFaint
                        font.family: Theme.fontFamily
                        font.pixelSize: Math.round(13 * island.k * 0.85)
                        font.weight: Font.Bold
                    }
                }
            }
        }
    }

    Rectangle {
        anchors.fill: parent
        color: Theme.ground

        MouseArea {
            anchors.fill: parent
            onClicked: Nav.hideScreensaver()
        }
    }

    // ── parked: what is playing, the clock, one alert; date in the corner ──
    // The car is off and the radio usually is not, so besides the time the
    // things worth knowing off this screen are what is playing and whether
    // anything wants attention. Now-playing sits over the clock, the newest
    // alert under it; the date is small in the top-right corner — it changes
    // once a day and does not need to compete with any of those.
    Text {
        id: ssDate
        visible: Nav.screensaverMode === "parked"
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.topMargin: 22
        anchors.rightMargin: 30
        color: Theme.inkDim
        font.family: Theme.fontFamily
        font.pixelSize: Math.max(22, Math.min(root.height * 0.04, 32))
        font.weight: Font.DemiBold
    }

    // The clock is pinned to the centre of the screen; what is playing hangs
    // above it and the alert below, so neither of them appearing or going
    // away moves the time (a Column centred as a group did exactly that).
    Item {
        id: parked
        visible: Nav.screensaverMode === "parked"
        anchors.fill: parent

        // ── now playing ─────────────────────────────────────────────────
        // Collapses to nothing when there is no track, so a parked car with
        // the radio off still gets the plain clock the design started from.
        Item {
            id: nowPlaying
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.bottom: ssClock.top
            anchors.bottomMargin: 14
            readonly property string trackTitle: Music ? (Music.title || "") : ""
            readonly property string trackArtist: Music ? (Music.artist || "") : ""
            readonly property bool   shown: trackTitle !== ""

            visible: shown
            width: Math.min(root.width - 80, 620)
            height: visible ? 98 : 0

            Rectangle {
                anchors.fill: parent
                radius: Theme.radiusCard
                color: Theme.surface

                Image {
                    id: npArt
                    anchors.left: parent.left
                    anchors.leftMargin: 14
                    anchors.verticalCenter: parent.verticalCenter
                    width: 70
                    height: 70
                    sourceSize.width: 140
                    sourceSize.height: 140
                    fillMode: Image.PreserveAspectCrop
                    asynchronous: true
                    cache: true
                    clip: true
                    visible: status === Image.Ready
                    source: Music ? (Music.albumArtUrl || "") : ""
                }

                // Stands in for missing art so the row keeps its shape.
                Rectangle {
                    anchors.fill: npArt
                    visible: !npArt.visible
                    radius: 8
                    color: Theme.surfaceRaised

                    Text {
                        anchors.centerIn: parent
                        text: "\u266a"
                        color: Theme.inkGhost
                        font.family: Theme.fontFamily
                        font.pixelSize: 30
                    }
                }

                Column {
                    anchors.left: npArt.right
                    anchors.leftMargin: 16
                    anchors.right: npState.left
                    anchors.rightMargin: 12
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 4

                    Text {
                        width: parent.width
                        text: nowPlaying.trackTitle
                        color: Theme.ink
                        font.family: Theme.fontFamily
                        font.pixelSize: 22
                        font.weight: Font.DemiBold
                        elide: Text.ElideRight
                    }

                    Text {
                        width: parent.width
                        visible: nowPlaying.trackArtist !== ""
                        text: nowPlaying.trackArtist
                        color: Theme.inkFaint
                        font.family: Theme.fontFamily
                        font.pixelSize: 16
                        elide: Text.ElideRight
                    }
                }

                // Static, not a pulsing dot: an animation here would keep the
                // render thread awake for the whole time the car is parked.
                Row {
                    id: npState
                    anchors.right: parent.right
                    anchors.rightMargin: 16
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 8

                    Rectangle {
                        width: 8; height: 8; radius: 4
                        anchors.verticalCenter: parent.verticalCenter
                        color: (Music && Music.playing) ? Theme.ok : Theme.inkGhost
                    }

                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        text: (Music && Music.playing) ? "Playing" : "Paused"
                        color: Theme.inkFaint
                        font.family: Theme.fontFamily
                        font.pixelSize: 13
                        font.letterSpacing: 0.8
                    }
                }
            }
        }

        Text {
            id: ssClock
            color: Theme.ink
            font.family: Theme.fontFamily
            font.pixelSize: Math.max(86, Math.min(root.height * 0.16, 150))
            font.weight: Font.Bold
            anchors.centerIn: parent

            Timer {
                interval: 1000
                running: root.visible && Nav.screensaverMode === "parked"
                repeat: true
                triggeredOnStart: true
                onTriggered: {
                    var d = new Date();
                    ssClock.text = Qt.formatTime(d, "HH:mm");
                    ssDate.text = Qt.formatDate(d, "dddd d MMMM yyyy");
                }
            }
        }

        // ── one alert ───────────────────────────────────────────────────
        // The newest only (plus a count); a parked car's list is for the
        // Home page, this is the glance version.
        AlertIsland {
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.top: ssClock.bottom
            anchors.topMargin: 14
            k: 1.4
            maxW: Math.min(root.width - 80, 620)
        }
    }

    // ══ driving: the grudge2.0 cluster ══════════════════════════════════
    Item {
        id: driving
        visible: Nav.screensaverMode === "driving"
        anchors.fill: parent

        // Same readiness rule as AirPlayPage: the window being live inside our
        // window beats the PulseAudio-derived `active`, which trails it.
        readonly property bool mirrorLive:
            AirPhone ? (AirPhone.active || AirPhone.mirrorWindowReady) : false
        readonly property int  lightCount: Telemetry ? Telemetry.activeDashLightKeys.length : 0
        readonly property bool hasAlerts:  Alerts && Alerts.model && Alerts.model.count > 0

        // Glance sizes. The islands started life at the Dashboard's sizes
        // (26 px lights, a 15 px alert line), which on a screen read from the
        // driver's seat at arm's length was a row of dots and a caption. The
        // lights are drawn at twice that, the alert at one and a half — 3x
        // was tried and read as a billboard under the car.
        readonly property real lightScale: 2.0
        readonly property real alertScale: 1.5
        readonly property int  lightIcon:  Math.round(26 * lightScale)

        // ── entry animation ─────────────────────────────────────────────
        // 0 = off screen, 1 = in place. One driver for the whole cluster: the
        // dials come in from their own edges and the car travels down from
        // where the Dashboard draws it, so the transition reads as the same
        // car moving rather than a new screen appearing over it.
        //
        // A single NumberAnimation, not a loop, and `running` is never left
        // true — it stops itself at 1 and the item is not visible otherwise.
        property real intro: 0.0
        /** How far the car falls, ~ the gap between the Dashboard's centre and
            the cluster's lower stack. */
        readonly property real carTravel: Math.round(driving.height * 0.20)

        function playIntro() {
            introAnim.stop();
            if (Nav && !Nav.animationEnabled()) {
                driving.intro = 1.0;
                return;
            }
            driving.intro = 0.0;
            introAnim.start();
        }

        NumberAnimation {
            id: introAnim
            target: driving
            property: "intro"
            to: 1.0
            duration: 420
            easing.type: Easing.OutCubic
        }

        // ── mirror sizing ───────────────────────────────────────────────
        // Fraction of the card width each dial is allowed to sit over. The
        // dials are drawn above the card, and the video is inset by the same
        // amount, so the two never fight for the same pixels — see the note
        // on rpmZone about why the video cannot simply be put underneath.
        readonly property real dialInset: 0.10

        // Chrome inside the card that is not video: the header sits 14 px
        // down and is 22 px tall, the surface starts 10 px below it, and
        // there is a 12 px margin at the bottom. 14+22+10+12.
        readonly property int cardChromeH: 58

        // Fit the picture to the card's own aspect rather than dropping it
        // into a fixed box. A fixed box is what produced the black bars: the
        // sink letterboxes whatever it is given, so an upright phone in a
        // wide card is bars down both sides and a wide phone in a tall card
        // is bars top and bottom. Sizing the card from the source instead
        // means the card *is* the picture and there is nothing left to bar.
        readonly property real _vidMaxH: Math.max(1, cluster.height - cardChromeH)
        readonly property real _vidMaxW:
            Math.max(1, cluster.width * 0.94 * (1 - 2 * dialInset))
        readonly property real _vidW:
            Math.max(1, Math.min(_vidMaxW, _vidMaxH * root.mirrorAspect))
        readonly property real _vidH: _vidW / root.mirrorAspect

        // ── clock ───────────────────────────────────────────────────────
        // Where the warning strip used to sit. Time is the one thing worth a
        // fixed place at the top of a screen you glance at; the lights moved
        // down to an island that only exists when something is lit.
        Text {
            id: ssDrivingClock
            anchors.top: parent.top
            anchors.topMargin: 14
            anchors.horizontalCenter: parent.horizontalCenter
            color: Theme.inkDim
            font.family: Theme.fontFamily
            font.pixelSize: 34
            font.weight: Font.DemiBold
            font.letterSpacing: 1.5

            Timer {
                // 5 s, not 1 s: this shows HH:MM, so a per-second tick would be
                // 59 wake-ups out of 60 that change nothing. Worst case the
                // minute is 5 s late.
                interval: 5000
                running: root.visible && Nav.screensaverMode === "driving"
                repeat: true
                triggeredOnStart: true
                onTriggered: ssDrivingClock.text = Qt.formatTime(new Date(), "HH:mm")
            }
        }

        // ── warning island ──────────────────────────────────────────────
        // A pill that is only as wide as it needs to be and does not exist at
        // all when nothing is lit. The previous strip held nine empty wells and
        // a "NO WARNINGS" label permanently, which is a lot of screen spent
        // saying that nothing is happening.
        Rectangle {
            id: warnStrip
            anchors.top: ssDrivingClock.bottom
            anchors.topMargin: 12
            anchors.horizontalCenter: parent.horizontalCenter
            width: visible ? warnRow.implicitWidth + Math.round(13 * driving.lightScale) * 2 : 0
            height: visible ? Math.round(42 * driving.lightScale) : 0
            visible: driving.lightCount > 0
            radius: height / 2
            color: Theme.surface

            // Grows and shrinks with the number of lights rather than jumping.
            Behavior on width {
                enabled: root.visible
                NumberAnimation { duration: 220; easing.type: Easing.OutCubic }
            }

            Row {
                id: warnRow
                anchors.centerIn: parent
                spacing: Math.round(8 * driving.lightScale)

                Repeater {
                    model: Telemetry ? Telemetry.activeDashLightKeys : []

                    Image {
                        // Nine is what fits; more than that and the car has
                        // bigger problems than a tidy screensaver.
                        visible: index < 9 && status === Image.Ready
                        width: visible ? driving.lightIcon : 0
                        height: driving.lightIcon
                        sourceSize.width: driving.lightIcon * 2
                        sourceSize.height: driving.lightIcon * 2
                        fillMode: Image.PreserveAspectFit
                        asynchronous: true
                        cache: true
                        anchors.verticalCenter: parent.verticalCenter
                        source: iconsPath + "/Dash/" + modelData + ".png"
                    }
                }
            }
        }

        // ── the cluster row ─────────────────────────────────────────────
        Item {
            id: cluster
            anchors.top: warnStrip.visible ? warnStrip.bottom : ssDrivingClock.bottom
            anchors.topMargin: 10
            anchors.bottom: parent.bottom
            anchors.bottomMargin: 12
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: 14
            anchors.rightMargin: 14

            // ── centre column: card, car, alert island ──────────────────
            // One stack so the car sits directly under the widget it belongs
            // to and the alert island directly under that. The dials flank the
            // column, not the card, so they keep their distance whatever the
            // column is carrying.
            Column {
                id: centreColumn
                anchors.horizontalCenter: parent.horizontalCenter
                anchors.verticalCenter: parent.verticalCenter
                width: driving.mirrorLive
                       ? Math.round(driving._vidW / (1 - 2 * driving.dialInset))
                       : root.centreW
                spacing: 10
                opacity: driving.intro

                Rectangle {
                    id: centreCard
                    anchors.horizontalCenter: parent.horizontalCenter
                    width: parent.width
                    height: driving.mirrorLive
                            ? Math.round(driving._vidH + driving.cardChromeH)
                            : Math.min(cluster.height * 0.45,
                                       cardHead.height + tripBlock.implicitHeight + 40)
                    radius: Theme.radiusCard
                    color: Theme.surface

                    // Header belongs to the mirror. Without one the card labels
                    // itself from its own content, and a second "TRIP" over the
                    // top of the big one is just noise on a small screen.
                    Item {
                        id: cardHead
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: driving.mirrorLive ? 14 : 0
                        visible: driving.mirrorLive
                        height: visible ? 22 : 0

                        Row {
                            anchors.left: parent.left
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: 8

                            // The mockup's own screen glyph, drawn rather than
                            // loaded: airphone.png is a black-on-transparent
                            // icon and would vanish at this size on a dark card.
                            Rectangle {
                                width: 16
                                height: 12
                                radius: 2
                                color: "transparent"
                                border.width: 1.5
                                border.color: Theme.accent
                                anchors.verticalCenter: parent.verticalCenter
                            }

                            Text {
                                text: "AIRPLAY MIRROR"
                                color: Theme.accent
                                font.family: Theme.fontFamily
                                font.pixelSize: 12
                                font.weight: Font.Bold
                                font.letterSpacing: 1.2
                                anchors.verticalCenter: parent.verticalCenter
                            }

                            Text {
                                visible: driving.mirrorLive && text !== ""
                                text: AirPhone ? AirPhone.deviceName : ""
                                color: Theme.inkGhost
                                font.family: Theme.fontFamily
                                font.pixelSize: 12
                                anchors.verticalCenter: parent.verticalCenter
                            }
                        }

                        Row {
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: 7
                            visible: driving.mirrorLive

                            Rectangle {
                                width: 7; height: 7; radius: 3.5
                                color: Theme.ok
                                anchors.verticalCenter: parent.verticalCenter
                            }

                            Text {
                                text: "MIRRORING"
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: 11
                                font.letterSpacing: 1
                                anchors.verticalCenter: parent.verticalCenter
                            }
                        }
                    }

                    // The rect rpiplay's window is moved into. Nothing is drawn
                    // here from QML — it exists to be measured.
                    Item {
                        id: mirrorSurface
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: cardHead.bottom
                        anchors.bottom: parent.bottom
                        // Inset by the dial band on each side so the picture
                        // stops where the dials begin. They are drawn over the
                        // card, not over the video: see the note on rpmZone.
                        anchors.leftMargin: Math.round(parent.width * driving.dialInset)
                        anchors.rightMargin: Math.round(parent.width * driving.dialInset)
                        anchors.bottomMargin: 12
                        anchors.topMargin: 10

                        onWidthChanged:  driving._syncMirror()
                        onHeightChanged: driving._syncMirror()
                        onXChanged:      driving._syncMirror()
                        onYChanged:      driving._syncMirror()
                    }

                    // Trip readout — the card's content whenever no phone is on.
                    // Compact: this is reference information, not the thing you
                    // are looking at while driving. The dials are.
                    Column {
                        id: tripBlock
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.top: cardHead.bottom
                        anchors.topMargin: 12
                        visible: !driving.mirrorLive
                        spacing: Math.round(digitSize * 0.30)

                        // Sized off the card width, since the card's height is
                        // derived from this block — deriving it from the height
                        // too would be a binding loop.
                        readonly property int digitSize:
                            Math.max(26, Math.min(46, Math.round(centreCard.width * 0.125)))

                        Column {
                            spacing: 0
                            anchors.horizontalCenter: parent.horizontalCenter

                            Text {
                                text: "TRIP"
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: Math.max(11, Math.round(tripBlock.digitSize * 0.28))
                                font.weight: Font.Bold
                                font.letterSpacing: 1.6
                                anchors.horizontalCenter: parent.horizontalCenter
                            }

                            Row {
                                spacing: 8
                                anchors.horizontalCenter: parent.horizontalCenter

                                UI.LedDigits {
                                    // todayKm arrives as "12.4 km"; the unit is
                                    // split off so the digits keep their grid.
                                    text: Telemetry ? String(Telemetry.todayKm).replace(" km", "")
                                                    : "0.0"
                                    pixelSize: tripBlock.digitSize
                                    color: Theme.ink
                                    anchors.verticalCenter: parent.verticalCenter
                                }

                                Text {
                                    text: "km"
                                    color: Theme.inkFaint
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Math.round(tripBlock.digitSize * 0.34)
                                    font.weight: Font.DemiBold
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                            }

                            // Today's fuel cost; hidden until a price is known.
                            Text {
                                visible: Trip ? Trip.todayCostText !== "—" : false
                                text: Trip ? Trip.todayCostText : ""
                                color: Theme.inkDim
                                font.family: Theme.fontFamily
                                font.pixelSize: Math.max(13, Math.round(tripBlock.digitSize * 0.34))
                                font.weight: Font.DemiBold
                                anchors.horizontalCenter: parent.horizontalCenter
                            }
                        }

                        Column {
                            spacing: 0
                            anchors.horizontalCenter: parent.horizontalCenter

                            Text {
                                text: "ENGINE ON"
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: Math.max(11, Math.round(tripBlock.digitSize * 0.28))
                                font.weight: Font.Bold
                                font.letterSpacing: 1.6
                                anchors.horizontalCenter: parent.horizontalCenter
                            }

                            UI.LedDigits {
                                text: Telemetry ? Telemetry.runTimeText : "\u2014"
                                pixelSize: tripBlock.digitSize
                                color: Theme.ink
                                anchors.horizontalCenter: parent.horizontalCenter
                            }
                        }
                    }
                }

                // The rear view belongs under the middle widget: it is the same
                // car the dials are reading, so it sits in the same column.
                Image {
                    id: ssCar
                    anchors.horizontalCenter: parent.horizontalCenter
                    width: Math.min(parent.width, 300)
                    height: Math.max(90, Math.min(driving.height * 0.22, 180))
                    fillMode: Image.PreserveAspectFit
                    smooth: true
                    asynchronous: true
                    cache: true
                    sourceSize.width: 340
                    sourceSize.height: 220
                    visible: status === Image.Ready
                    source: {
                        if (!Modding || Modding.preset === "Quiet")
                            return "file://" + iconsPath + "/back.png";
                        var p = Modding.carBackPath || (iconsPath + "/back.png");
                        return String(p).startsWith("file://") ? p : "file://" + p;
                    }
                    onStatusChanged: {
                        if (status === Image.Error)
                            source = "file://" + iconsPath + "/back.png";
                    }

                    // Comes down from where the Dashboard draws it — the same
                    // car, moving to its place in the cluster, rather than a
                    // second copy appearing somewhere else.
                    transform: Translate { y: -(1 - driving.intro) * driving.carTravel }
                }

                // Trip + engine-on, compactly, but only while the mirror is
                // covering the big version in the centre card. Showing both at
                // once would spend a third of a small screen saying one thing.
                Rectangle {
                    id: tripChip
                    anchors.horizontalCenter: parent.horizontalCenter
                    visible: driving.mirrorLive
                    width: parent.width
                    height: visible ? 44 : 0
                    radius: Theme.radiusControl
                    color: Theme.surface

                    Row {
                        anchors.centerIn: parent
                        spacing: 18

                        Row {
                            spacing: 7
                            anchors.verticalCenter: parent.verticalCenter
                            Text {
                                text: "TRIP"
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: 11
                                font.letterSpacing: 1
                                anchors.verticalCenter: parent.verticalCenter
                            }
                            Text {
                                text: Telemetry ? Telemetry.todayKm : "0.0 km"
                                color: Theme.ink
                                font.family: Theme.fontFamily
                                font.pixelSize: 18
                                font.weight: Font.Bold
                                anchors.verticalCenter: parent.verticalCenter
                            }
                        }

                        Row {
                            spacing: 7
                            anchors.verticalCenter: parent.verticalCenter
                            Text {
                                text: "ENGINE"
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: 11
                                font.letterSpacing: 1
                                anchors.verticalCenter: parent.verticalCenter
                            }
                            Text {
                                text: Telemetry ? Telemetry.runTimeText : "\u2014"
                                color: Theme.ink
                                font.family: Theme.fontFamily
                                font.pixelSize: 18
                                font.weight: Font.Bold
                                anchors.verticalCenter: parent.verticalCenter
                            }
                        }
                    }
                }

                // ── alert island ────────────────────────────────────────
                // Same shape as the warning island at the top: a pill sized to
                // what it carries, gone entirely when there is nothing to say.
                // Drawn at alertScale; it may run wider than the centre column
                // — this low on the screen the dials are finished, so the
                // width beside the column is free.
                AlertIsland {
                    anchors.horizontalCenter: parent.horizontalCenter
                    k: driving.alertScale
                    maxW: Math.min(Math.round(driving.width * 0.5), 640)
                }
            }

            // ── rev dial ────────────────────────────────────────────────
            // The dials are stacked above the card and reach across its outer
            // band, so the cluster reads as two instruments sitting on top of
            // one large display rather than three tiles in a row.
            //
            // They overlap the *card*, never the video. rpiplay's window is a
            // real X11 child of the app window, and an X child is always drawn
            // above its parent's own content — there is no QML z-order that
            // puts a scene-graph item in front of it. So the picture is inset
            // by the same band the dials reach across, and the overlap happens
            // on card background.
            Item {
                id: rpmZone
                z: 2
                anchors.left: parent.left
                anchors.right: centreColumn.left
                anchors.rightMargin: driving.mirrorLive
                                     ? -Math.round(centreColumn.width * driving.dialInset)
                                     : 6
                anchors.top: parent.top
                anchors.bottom: parent.bottom

                // Slides in from its own edge. One Translate driven by the
                // shared intro value, so both dials and the car move together.
                transform: Translate { x: -(1 - driving.intro) * (rpmZone.width + 40) }

                UI.DialGauge {
                    id: rpmDial
                    anchors.centerIn: parent
                    width:  Math.min(rpmZone.width - 6, rpmZone.height)
                    height: width
                    litColor: Theme.accent
                    labels: ["0", "1", "2", "3", "4", "5", "6"]
                    value:     Telemetry ? Telemetry.rpm : 0
                    maxValue:  root.rpmBand.length === 4 ? root.rpmBand[3] : 6500
                    warnAt:    root.rpmBand.length === 4 ? root.rpmBand[0] : 5000
                    dangerAt:  root.rpmBand.length === 4 ? root.rpmBand[1] : 5500
                    redlineAt: root.rpmBand.length === 4 ? root.rpmBand[2] : 5800

                    Column {
                        anchors.centerIn: parent
                        spacing: 0

                        UI.LedDigits {
                            text: Telemetry ? Telemetry.rpm.toFixed(0) : "0"
                            pixelSize: Math.max(30, Math.round(rpmDial.radius * 0.46))
                            color: Telemetry ? root._valueColor(Telemetry.rpm, root.rpmBand,
                                                               Telemetry.rpmColor)
                                             : Theme.ink
                            anchors.horizontalCenter: parent.horizontalCenter
                        }

                        Text {
                            text: "RPM"
                            color: Theme.inkFaint
                            font.family: Theme.fontFamily
                            font.pixelSize: Math.max(10, Math.round(rpmDial.radius * 0.11))
                            font.letterSpacing: 1.4
                            anchors.horizontalCenter: parent.horizontalCenter
                        }

                        Text {
                            // Gear, as the mockup shows it under the numeral.
                            text: {
                                if (!Telemetry) return "";
                                var g = Telemetry.gear;
                                return g > 0 ? String(g) : "N";
                            }
                            color: Theme.accent
                            font.family: Theme.fontFamily
                            font.pixelSize: Math.max(16, Math.round(rpmDial.radius * 0.22))
                            font.weight: Font.Bold
                            topPadding: Math.round(rpmDial.radius * 0.06)
                            anchors.horizontalCenter: parent.horizontalCenter
                        }
                    }
                }
            }

            // ── speed dial ──────────────────────────────────────────────
            Item {
                id: speedZone
                z: 2
                anchors.left: centreColumn.right
                anchors.leftMargin: driving.mirrorLive
                                    ? -Math.round(centreColumn.width * driving.dialInset)
                                    : 6
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.bottom: parent.bottom

                transform: Translate { x: (1 - driving.intro) * (speedZone.width + 40) }

                UI.DialGauge {
                    id: speedDial
                    anchors.centerIn: parent
                    width:  Math.min(speedZone.width - 6, speedZone.height)
                    height: width
                    // Was Theme.ink — a white sweep reads as chrome, not as a
                    // reading. Green below the warn band, and the zone colours
                    // take over above it exactly as they do on the rev dial.
                    litColor: Theme.ok
                    labels: ["0", "30", "60", "90", "120", "150", "180"]
                    value:     Telemetry ? Telemetry.speed : 0
                    maxValue:  root.speedBand.length === 4 ? root.speedBand[3] : 180
                    warnAt:    root.speedBand.length === 4 ? root.speedBand[0] : 120
                    dangerAt:  root.speedBand.length === 4 ? root.speedBand[1] : 130
                    redlineAt: root.speedBand.length === 4 ? root.speedBand[2] : 140

                    Column {
                        anchors.centerIn: parent
                        spacing: 0

                        UI.LedDigits {
                            text: Telemetry ? Telemetry.speed.toFixed(0) : "0"
                            pixelSize: Math.max(30, Math.round(speedDial.radius * 0.50))
                            color: Telemetry ? root._valueColor(Telemetry.speed, root.speedBand,
                                                               Telemetry.speedColor)
                                             : Theme.ink
                            anchors.horizontalCenter: parent.horizontalCenter
                        }

                        Text {
                            text: "KM/H"
                            color: Theme.inkFaint
                            font.family: Theme.fontFamily
                            font.pixelSize: Math.max(10, Math.round(speedDial.radius * 0.11))
                            font.letterSpacing: 1.4
                            anchors.horizontalCenter: parent.horizontalCenter
                        }
                    }
                }
            }
        }

        // ── AirPlay surface handover ────────────────────────────────────
        // The mirror is a real X11 window the bridge moves about, so exactly
        // one place may own its geometry at a time. While this screensaver is
        // up it is on top of everything, so it claims the surface; on the way
        // out it releases it, and AirPlayPage re-claims when it next syncs.
        function _syncMirror() {
            if (!AirPhone || !driving.visible || !root.visible) return;
            if (!mirrorSurface.width || !mirrorSurface.height) return;
            var p = mirrorSurface.mapToItem(null, 0, 0);
            AirPhone.setVideoGeometry(Math.round(p.x), Math.round(p.y),
                                      Math.round(mirrorSurface.width),
                                      Math.round(mirrorSurface.height));
            AirPhone.setVideoVisible(AirPhone.running);
        }

        onVisibleChanged: {
            if (visible) {
                _syncMirror();
                driving.playIntro();
            } else if (AirPhone) {
                AirPhone.setVideoVisible(false);
            }
        }
    }
}
