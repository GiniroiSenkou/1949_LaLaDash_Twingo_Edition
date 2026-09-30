import QtQuick
import QtQuick.Layouts
import "../components" as UI
import "../sheets" as Sheets

/**
 * Settings — the largest page in the app: 6 groups, 21 sections, ~100 rows.
 *
 * Shape (mockup SETTINGS): a 288 px nav card on the left, a detail card on the
 * right. The detail card is header / scrolling body / footer actions; the body
 * is a stack of plain Columns, one per section, each gated on
 * `activeSection === "<name>"`.
 *
 * That gating is deliberate and must stay. A StackLayout would keep every
 * section's bindings live and force a layout pass over all 21 of them on every
 * theme or telemetry tick; `visible` lets Column skip them entirely, which is
 * the only reason a page this size is affordable on the Pi 4B.
 *
 * Every control here kept the old Settings page's labels,
 * same bridge calls, same ini section/key pairs. Only two things changed:
 *   · Appearance → UI was a catalog of the *legacy* button presets, which no
 *     longer exist in this skin. It is now a gallery of the modern components.
 *   · Appearance → Themes gained a "Style" row, because the skin is now
 *     selectable at runtime.
 */
Item {
    id: root

    // ── selection ──────────────────────────────────────────────────────
    // `eval @settingsPage activeSection = "70mai"` from the debug probe.
    objectName: "settingsPage"
    property string activeSection: ""

    /** Dummy dependency: touching Settings.revision in a binding makes every
        getBool()/getValue() row re-evaluate after any setValue(). Do not
        remove — those are slots, not properties, so nothing else notifies. */
    readonly property int _settingsRev: Settings ? Settings.revision : 0
    /** Alert severity -> colour, the mapping HomePage/SidePanel use. */
    function _alertTone(p) {
        switch (String(p || "info")) {
        case "critical":
        case "danger":  return Theme.danger;
        case "warning":
        case "warn":    return Theme.warn;
        case "success": return Theme.ok;
        default:        return Theme.accent;
        }
    }

    // Bumped by the Connections block below on every Settings.wifiChanged.
    property int _wifiRev: 0
    // Bumped on Settings.netChanged (2 s) — only while Connectivity is the
    // open section, so the per-adapter rows stay live and cost nothing else.
    property int _netRev: 0

    // ── Animation section state (read on entry, like legacy) ────────────
    property bool animMasterEnabled: true
    property bool animIntroEnabled: false
    property bool animSettingsEnabled: false
    property bool animSidePanelEnabled: false
    property bool animHomeWidgetsEnabled: true
    property bool parkingScreenSaverEnabled: false
    property bool drivingScreenSaverEnabled: false
    property int  parkingTimeoutSec: 20
    property int  drivingTimeoutSec: 20

    // ── Debugger section state ─────────────────────────────────────────
    property bool debuggerMprisEnabled: false

    // ── About readouts (refreshed by a timer while About is open) ───────
    property string aboutCpuUsage: "N/A"
    property string aboutAppRam: "N/A"
    property string aboutThreads: "N/A"
    property string aboutCpuTemp: "N/A"
    property string aboutGpuName: "N/A"
    property string aboutGpuUsage: "N/A"
    property string aboutGpuTemp: "N/A"
    property string aboutRamUsed: "N/A"
    property string aboutRamFree: "N/A"
    property string aboutUptime: "N/A"
    property string aboutModel: "N/A"
    property string aboutPower: "N/A"
    property string aboutSysCpu: "N/A"
    property string aboutGpuLoad: "N/A"
    property string aboutCma: "N/A"

    // ── Soundboard slot picker state ───────────────────────────────────
    property int  sbPickerSlot: 0
    property bool sbPickerFiles: false

    // ── Total-km editor state ────────────────────────────────────────────
    property bool   totalKmEditorOpen: false
    property string totalKmDraft: ""

    // ── Trip day boundary ────────────────────────────────────────────────
    // The trip day ends at 04:00 by default, not midnight: coming home at
    // 01:30 is still the same evening out, and a TRIP readout that zeroes
    // itself halfway through the drive home is worse than useless.
    // _settingsRev is touched so the row re-reads after setString().
    readonly property int _dayStartHour: {
        var raw = (Settings && root._settingsRev >= 0)
            ? Settings.getString("Trips", "day_start_hour", "4")
            : "4";
        var v = parseInt(raw);
        return (isNaN(v) || v < 0 || v > 12) ? 4 : v;
    }
    readonly property var _dayStartChoices: [0, 3, 4, 5, 6, 7]
    // [Fuel] keys for the Trip page (fuel_ledger.py reads the same defaults).
    function _fuelSetting(key, def) {
        var raw = (Settings && root._settingsRev >= 0)
            ? Settings.getString("Fuel", key, String(def))
            : String(def);
        var v = parseFloat(raw);
        return isNaN(v) ? def : v;
    }
    function _cycleFuelSetting(key, def, opts) {
        if (!Settings)
            return;
        var i = opts.indexOf(root._fuelSetting(key, def));
        Settings.setString("Fuel", key, String(opts[(i + 1) % opts.length]));
    }

    // ═══════════════════════════════════════════════════════════════════
    //  Section side effects — identical set to the legacy page
    // ═══════════════════════════════════════════════════════════════════
    onActiveSectionChanged: {
        if (activeSection === "Animation")
            syncAnimationSettings();
        if (activeSection === "Debugger")
            syncDebuggerSettings();
        if (activeSection === "Engine Sound" && Modding)
            Modding.refreshEngineOutputOptions();
        if (activeSection === "Music" && Music)
            Music.refreshSplitAudioDevices();
        if (activeSection === "Split Audio" && Music)
            Music.refreshSplitAudioDevices();
        if (activeSection === "Audio" && Music)
            Music.refreshAudioPlayers();
        if (activeSection === "Soundboard" && Soundboard)
            Soundboard.refreshCatalog();
        if (activeSection === "About")
            refreshAbout();
    }

    function _asBool(value, fallbackValue) {
        var raw = String(value === undefined || value === null ? "" : value).trim().toLowerCase();
        if (raw === "")
            return fallbackValue;
        return raw === "1" || raw === "true" || raw === "yes" || raw === "on";
    }

    function _boundedTimeout(value, fallbackValue) {
        var clean = parseInt(value);
        if (!isFinite(clean) || clean <= 0)
            clean = fallbackValue;
        return Math.max(5, Math.min(300, clean));
    }

    function syncAnimationSettings() {
        var masterDefault = Nav ? Nav.animationEnabled() : true;
        if (!Settings) {
            animMasterEnabled = masterDefault;
            animIntroEnabled = false;
            animSettingsEnabled = false;
            animSidePanelEnabled = false;
            parkingScreenSaverEnabled = false;
            drivingScreenSaverEnabled = false;
            parkingTimeoutSec = 20;
            drivingTimeoutSec = 20;
            return;
        }
        animMasterEnabled = _asBool(Settings.getValue("UI", "animation_master_enabled"), masterDefault);
        animIntroEnabled = _asBool(Settings.getValue("UI", "home_intro_enabled"), false);
        animSettingsEnabled = _asBool(Settings.getValue("UI", "settings_page_animation_enabled"), false);
        animSidePanelEnabled = _asBool(Settings.getValue("UI", "side_panel_animation_enabled"), false);
        animHomeWidgetsEnabled = _asBool(Settings.getValue("UI", "home_widgets_enabled"), true);
        parkingScreenSaverEnabled = _asBool(Settings.getValue("UI", "parking_screensaver_enabled"), false);
        drivingScreenSaverEnabled = _asBool(Settings.getValue("UI", "driving_screensaver_enabled"), false);
        parkingTimeoutSec = _boundedTimeout(Settings.getInt("UI", "parking_screensaver_timeout_sec"), 20);
        drivingTimeoutSec = _boundedTimeout(Settings.getInt("UI", "driving_screensaver_timeout_sec"), 20);
    }

    function syncDebuggerSettings() {
        if (!Settings) {
            debuggerMprisEnabled = false;
            return;
        }
        debuggerMprisEnabled = _asBool(Settings.getValue("Debug", "mpris_debug"), false);
    }

    function refreshAbout() {
        if (!Settings)
            return;
        aboutCpuUsage = Settings.cpuUsagePercent();
        aboutAppRam = Settings.appRamUsed();
        aboutThreads = Settings.appThreads();
        aboutCpuTemp = Settings.cpuTemp();
        aboutGpuName = Settings.gpuName();
        aboutGpuUsage = Settings.gpuUsage();
        aboutGpuTemp = Settings.gpuTemp();
        aboutRamUsed = Settings.ramUsed();
        aboutRamFree = Settings.ramFree();
        aboutUptime = Settings.uptime();
        aboutModel = Settings.deviceModel();
        aboutPower = Settings.powerStatus();
        aboutSysCpu = Settings.sysCpuUsage();
        aboutGpuLoad = Settings.gpuLoad();
        aboutCma = Settings.cmaFree();
    }

    function _log(line) {
        diagLog.text = diagLog.text + "\n[" + Qt.formatTime(new Date(), "hh:mm:ss") + "] " + line;
    }

    function _fontSize(key) {
        if (!Settings)
            return 14;
        var raw = Settings.fontGroupRaw(key);
        return raw ? raw.size : 14;
    }

    function _sbSlotName(slot) {
        var favs = Soundboard ? Soundboard.favorites : [];
        return (favs && favs.length > slot && favs[slot] && favs[slot].name) ? favs[slot].name : "";
    }

    function _sbFolders() {
        var out = [];
        if (!Soundboard)
            return out;
        var folders = Soundboard.folders;
        for (var i = 0; i < folders.length; ++i)
            out.push({ label: folders[i], value: folders[i], icon: Soundboard.logoForFolder(folders[i]) });
        return out;
    }

    // ═══════════════════════════════════════════════════════════════════
    //  Navigation model
    // ═══════════════════════════════════════════════════════════════════
    // Extras (Steering Keys, AirPhone, 70mai, Action cam) are
    // listed only while their Services master switch is on — off means the
    // section, the page and the process are all gone (Modules/Runtime/services.py).
    readonly property var extraSections: ({
        "Steering Keys": "steering_keys", "AirPhone": "airphone", "70mai": "dashcam",
        "Action cam": "actioncam"
    })
    function _serviceSection(name) {
        var svc = extraSections[name];
        return !svc || !Settings || Settings.serviceOn(svc);
    }
    readonly property var navGroups: {
        var rev = Settings ? Settings.servicesRev : 0;   // dependency: re-filter on a flip
        return navGroupsAll.map(function (g) {
            return { group: g.group, items: g.items.filter(_serviceSection) };
        });
    }
    readonly property var navGroupsAll: [
        { group: "Core",       items: ["Connectivity", "Steering Keys", "AirPhone", "70mai", "Action cam"] },
        { group: "Appearance", items: ["Themes", "Display", "Animation", "Font", "UI"] },
        { group: "Driving",    items: ["Engine Sound", "Driving Mode", "Alerts", "Status", "Trips"] },
        { group: "Media",      items: ["Music", "Lyrics", "Split Audio", "Audio", "Soundboard"] },
        { group: "Tools",      items: ["Obd", "Diagnose", "Debugger"] },
        { group: "Hardware",   items: ["About", "Services", "System"] }
    ]

    readonly property var sectionHints: ({
        "Connectivity":  "Wireless radios and pairing",
        "Steering Keys": "Car buttons MPRIS does not carry",
        "AirPhone":      "AirPlay receiver for iPhone",
        "70mai":         "Dashcam over its Wi-Fi hotspot",
        "Action cam":    "GoPro or Insta360, USB or Wi-Fi",
        "Themes":        "Skin, colours and glass",
        "Display":       "Window mode and resolution",
        "Animation":     "Motion and screensavers",
        "Font":          "Fifteen typography groups",
        "UI":            "Modern component gallery",
        "Engine Sound":  "Preset, output channel and volume",
        "Driving Mode":  "Beginner assistance",
        "Alerts":        "What reaches the alert widgets",
        "Status":        "Live telemetry read-out",
        "Trips":         "Distance, run time and counters",
        "Music":         "Playback, output and Sound Drive",
        "Lyrics":        "How lyrics are rendered",
        "Split Audio":   "Mirror playback to a second output",
        "Audio":         "Live per-player volume",
        "Soundboard":    "Quick slots and volume",
        "Obd":           "Adapter, protocol and connection",
        "Diagnose":      "Read-only diagnostic commands",
        "Debugger":      "Test tools, developer switches and logs",
        "About":         "Process, hardware and system",
        "Services":      "Background services and helpers",
        "System":        "Application and power actions"
    })

    readonly property var actionSections: [
        "Themes", "Trips", "Engine Sound", "Music", "Split Audio", "Audio",
        "Soundboard", "Obd", "Diagnose", "About"
    ]

    function _hasActions(name) {
        return actionSections.indexOf(name) >= 0;
    }

    // Settings → Animation → Settings Animation: the page slides in from the
    // right each time it opens. One short animation, nothing left running.
    onVisibleChanged: {
        if (!visible || !Nav || !Nav.settingsAnimationEnabled()) return;
        settingsIntroAnim.stop();
        contentSlide.x = width * 0.35;
        content.opacity = 0;
        settingsIntroAnim.start();
    }
    ParallelAnimation {
        id: settingsIntroAnim
        NumberAnimation { target: contentSlide; property: "x"; to: 0; duration: 300; easing.type: Easing.OutCubic }
        NumberAnimation { target: content; property: "opacity"; to: 1; duration: 300; easing.type: Easing.OutCubic }
    }

    // ═══════════════════════════════════════════════════════════════════
    //  Layout — 288 px nav card + detail card
    // ═══════════════════════════════════════════════════════════════════
    Item {
        id: content
        anchors.fill: parent
        anchors.leftMargin: 22
        anchors.rightMargin: 22
        anchors.topMargin: 6
        transform: Translate { id: contentSlide }

        // ── LEFT: grouped nav ──────────────────────────────────────────
        UI.Card {
            id: navCard
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: 288
            radius: 22

            Flickable {
                id: navFlick
                anchors.fill: parent
                anchors.margins: 12
                clip: true
                contentWidth: width
                contentHeight: navColumn.implicitHeight
                boundsBehavior: Flickable.StopAtBounds

                Column {
                    id: navColumn
                    width: navFlick.width
                    spacing: 2

                    Repeater {
                        model: root.navGroups

                        Column {
                            width: navColumn.width
                            spacing: 2

                            // Group header — 30 px, uppercase, tracked out.
                            Item {
                                width: parent.width
                                height: 30

                                Text {
                                    anchors.left: parent.left
                                    anchors.leftMargin: 16
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: modelData.group.toUpperCase()
                                    color: Theme.inkDim
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 13
                                    font.weight: Font.Bold
                                    font.letterSpacing: 1.3
                                }
                            }

                            Repeater {
                                model: modelData.items

                                Rectangle {
                                    id: navRow
                                    readonly property bool active: root.activeSection === modelData
                                    width: navColumn.width
                                    height: 44
                                    radius: 13
                                    color: active
                                           ? (navMa.pressed ? Theme.accentPressed : Theme.accent)
                                           : (navMa.pressed ? Theme.surfacePressed
                                                            : (navMa.containsMouse ? Theme.surfaceHover : "transparent"))
                                    Behavior on color { ColorAnimation { duration: 110 } }

                                    Text {
                                        anchors.left: parent.left
                                        anchors.leftMargin: 16
                                        anchors.right: parent.right
                                        anchors.rightMargin: 12
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: modelData
                                        color: navRow.active ? Theme.accentInk : Theme.ink
                                        font.family: Theme.fontFamily
                                        font.pixelSize: Theme.fontSize("settings_primary")
                                        font.weight: navRow.active ? Font.Bold : Font.DemiBold
                                        elide: Text.ElideRight
                                    }

                                    MouseArea {
                                        id: navMa
                                        anchors.fill: parent
                                        hoverEnabled: true
                                        onClicked: root.activeSection = modelData
                                    }
                                }
                            }
                        }
                    }

                    Item { width: 1; height: 10 }
                }
            }
        }

        // ── RIGHT: detail ──────────────────────────────────────────────
        UI.Card {
            id: detailCard
            anchors.left: navCard.right
            anchors.leftMargin: 18
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            radius: 22

            // Header: 26 px bold title + a dim hint, over a hairline.
            Item {
                id: detailHeader
                anchors.top: parent.top
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.topMargin: 22
                anchors.leftMargin: 26
                anchors.rightMargin: 26
                height: 50

                Text {
                    id: headerTitle
                    anchors.left: parent.left
                    anchors.bottom: parent.bottom
                    anchors.bottomMargin: 14
                    text: root.activeSection === "" ? "Settings" : root.activeSection
                    color: Theme.ink
                    font.family: Theme.fontFamily
                    font.pixelSize: 26
                    font.weight: Font.Bold
                }

                Text {
                    anchors.left: headerTitle.right
                    anchors.leftMargin: 14
                    anchors.right: parent.right
                    anchors.baseline: headerTitle.baseline
                    text: root.activeSection === ""
                          ? "Pick a group on the left"
                          : (root.sectionHints[root.activeSection] || "")
                    color: Theme.inkFaint
                    font.family: Theme.fontFamily
                    font.pixelSize: 14
                    elide: Text.ElideRight
                }

                Rectangle {
                    anchors.bottom: parent.bottom
                    anchors.left: parent.left
                    anchors.right: parent.right
                    height: 1
                    color: Theme.hairline
                }
            }

            // Footer: per-section action pills.
            Row {
                id: detailFooter
                anchors.bottom: parent.bottom
                anchors.left: parent.left
                anchors.bottomMargin: 22
                anchors.leftMargin: 26
                spacing: 10
                height: 50
                visible: root._hasActions(root.activeSection)

                UI.PillButton {
                    visible: root.activeSection === "Themes"
                    width: visible ? implicitWidth : 0
                    height: 50
                    text: "Restart now"
                    variant: "accent"
                    onClicked: if (Settings) Settings.restartApp()
                }

                UI.PillButton {
                    visible: root.activeSection === "Themes"
                    width: visible ? implicitWidth : 0
                    height: 50
                    text: "Reload Theme"
                    onClicked: if (Settings) Settings.reload()
                }

                UI.PillButton {
                    visible: root.activeSection === "Trips"
                    width: visible ? implicitWidth : 0
                    height: 50
                    text: "Reset Today"
                    variant: "danger"
                    onClicked: {
                        // Used to write Data/settings.ini [Trips] today_km —
                        // a store trips_store.py (the actual source of
                        // Telemetry.todayKm) never reads, so this did
                        // nothing. Route it into the real store instead.
                        if (Settings) Settings.resetTodayKm();
                    }
                }

                UI.PillButton {
                    visible: root.activeSection === "Engine Sound"
                    width: visible ? implicitWidth : 0
                    height: 50
                    text: "Refresh Outputs"
                    onClicked: if (Modding) Modding.refreshEngineOutputOptions()
                }

                UI.PillButton {
                    visible: root.activeSection === "Music" || root.activeSection === "Split Audio"
                    width: visible ? implicitWidth : 0
                    height: 50
                    text: "Refresh Devices"
                    onClicked: if (Music) Music.refreshSplitAudioDevices()
                }

                UI.PillButton {
                    visible: root.activeSection === "Audio"
                    width: visible ? implicitWidth : 0
                    height: 50
                    text: "Refresh Players"
                    onClicked: if (Music) Music.refreshAudioPlayers()
                }

                UI.PillButton {
                    visible: root.activeSection === "Soundboard"
                    width: visible ? implicitWidth : 0
                    height: 50
                    text: "Refresh Catalog"
                    onClicked: if (Soundboard) Soundboard.refreshCatalog()
                }

                UI.PillButton {
                    visible: root.activeSection === "Obd"
                    width: visible ? implicitWidth : 0
                    height: 50
                    text: "Quick Connect"
                    variant: "accent"
                    onClicked: if (OBD) OBD.connect()
                }

                UI.PillButton {
                    visible: root.activeSection === "Obd"
                    width: visible ? implicitWidth : 0
                    height: 50
                    text: "Deep Scan"
                    onClicked: if (OBD) OBD.deepScan()
                }

                UI.PillButton {
                    visible: root.activeSection === "Obd"
                    width: visible ? implicitWidth : 0
                    height: 50
                    text: "Disconnect"
                    onClicked: if (OBD) OBD.disconnect()
                }

                UI.PillButton {
                    visible: root.activeSection === "Obd"
                    width: visible ? implicitWidth : 0
                    height: 50
                    text: "Test Connection"
                    onClicked: if (OBD) OBD.testConnection()
                }

                UI.PillButton {
                    visible: root.activeSection === "Diagnose"
                    width: visible ? implicitWidth : 0
                    height: 50
                    text: "Clear Log"
                    onClicked: diagLog.text = "Diagnostic log..."
                }


                UI.PillButton {
                    visible: root.activeSection === "About"
                    width: visible ? implicitWidth : 0
                    height: 50
                    text: "Refresh"
                    onClicked: root.refreshAbout()
                }
            }

            // Body: one Column per section, gated on `visible`.
            Flickable {
                id: detailBody
                anchors.top: detailHeader.bottom
                anchors.topMargin: 6
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                anchors.leftMargin: 26
                anchors.rightMargin: 26
                anchors.bottomMargin: 22 + (detailFooter.visible ? detailFooter.height + 14 : 0)
                clip: true
                contentWidth: width
                contentHeight: bodyCol.implicitHeight + 12
                boundsBehavior: Flickable.StopAtBounds

                Column {
                    id: bodyCol
                    width: detailBody.width
                    spacing: 0

                    // ═══════════════════════════════════════════════════
                    //  CORE · Connectivity
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "Connectivity"
                        width: bodyCol.width
                        spacing: 0

                        UI.ListRow {
                            label: "Wi-Fi"
                            sub: "Wireless radio"
                            UI.Toggle {
                                checked: Settings && root._wifiRev >= 0 ? Settings.wifiEnabled() : false
                                onToggled: function (v) { if (Settings) Settings.setWifiEnabled(v); }
                            }
                        }

                        // Where traffic actually goes right now (default
                        // route) — may be Ethernet on the bench.
                        UI.ListRow {
                            label: "IP address"
                            sub: Settings && Settings.netIface
                                 ? ("Default route via " + Settings.netIface + "  ·  " + Settings.netRateText)
                                 : "No network"
                            ValueText { text: Settings && Settings.netIp ? Settings.netIp : "—" }
                        }

                        // One row per radio: which device, what it is on, its
                        // address and live throughput. The USB row only
                        // exists while a dongle is plugged in.
                        UI.ListRow {
                            id: netRowPi
                            readonly property var net: (root._netRev, Settings) ? Settings.netInfo(Settings.wifiAdapterName) : ({})
                            label: "Pi built-in" + (Settings && Settings.wifiAdapterName ? "  ·  " + Settings.wifiAdapterName : "")
                            sub: net && net.ssid ? ("On " + net.ssid + (net.isDefault ? "  ·  default route" : ""))
                                 : (Settings && Settings.wifiStatus ? Settings.wifiStatus : "Not connected")
                            Column {
                                spacing: 2
                                ValueText {
                                    anchors.right: parent.right
                                    text: netRowPi.net && netRowPi.net.ip ? netRowPi.net.ip : "—"
                                }
                                Text {
                                    anchors.right: parent.right
                                    text: netRowPi.net && netRowPi.net.rateText ? netRowPi.net.rateText : ""
                                    color: Theme.inkFaint; font.family: Theme.fontFamily; font.pixelSize: 13
                                }
                            }
                        }

                        UI.ListRow {
                            id: netRowUsb
                            visible: Settings ? Settings.hasSecondWifiAdapter : false
                            readonly property var net: (root._netRev, Settings) ? Settings.netInfo(Settings.secondWifiAdapterName) : ({})
                            label: "USB adapter" + (Settings && Settings.secondWifiAdapterName ? "  ·  " + Settings.secondWifiAdapterName : "")
                            sub: net && net.ssid ? ("On " + net.ssid + (net.isDefault ? "  ·  default route" : ""))
                                 : (Settings && Settings.wifi2Status ? Settings.wifi2Status : "Not connected")
                            Column {
                                spacing: 2
                                ValueText {
                                    anchors.right: parent.right
                                    text: netRowUsb.net && netRowUsb.net.ip ? netRowUsb.net.ip : "—"
                                }
                                Text {
                                    anchors.right: parent.right
                                    text: netRowUsb.net && netRowUsb.net.rateText ? netRowUsb.net.rateText : ""
                                    color: Theme.inkFaint; font.family: Theme.fontFamily; font.pixelSize: 13
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Networks"
                            sub: Settings && Settings.hasSecondWifiAdapter
                                 ? "Scan, join, disconnect or forget — switch between the two radios inside"
                                 : "Scan, join with the on-screen keyboard, disconnect or forget"
                            clickable: true
                            onClicked: wifiSheet.opened = true
                            Row {
                                spacing: 10
                                ValueText {
                                    height: 44
                                    verticalAlignment: Text.AlignVCenter
                                    color: Theme.inkFaint
                                    text: Settings && Settings.wifiAvailable
                                          ? (Settings.wifiNetworks.length > 0
                                             ? Settings.wifiNetworks.length + " in range" : "")
                                          : "nmcli missing"
                                }
                                UI.PillButton {
                                    text: "Manage"
                                    implicitWidth: 120
                                    height: 44
                                    radius: Theme.radiusControl
                                    enabled: Settings ? Settings.wifiAvailable : false
                                    onClicked: wifiSheet.opened = true
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Bluetooth"
                            sub: "Pairing and accessory connection"
                            UI.Toggle {
                                checked: Settings ? Settings.bluetoothEnabled() : false
                                onToggled: function (v) { if (Settings) Settings.setBluetoothEnabled(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Connected"
                            sub: "Bluetooth device"
                            ValueText { text: Settings ? (Settings.bluetoothDevice() || "Not connected") : "N/A" }
                        }

                        // The head unit and the Pi boot together on ignition;
                        // the app keeps trying until the R-Link's A2DP link
                        // is up and re-tries after a drop.
                        UI.ListRow {
                            label: "Auto-connect head unit"
                            sub: Music ? Music.btAudioStatus : "N/A"
                            UI.Toggle {
                                checked: Music ? Music.btAudioEnabled : false
                                onToggled: function (v) { if (Music) Music.setBtAudioEnabled(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Connect now"
                            sub: "Retry the head unit link immediately"
                            divider: false
                            UI.PillButton {
                                text: "Connect"
                                enabled: Music ? (Music.btAudioEnabled && !Music.btAudioConnected) : false
                                onClicked: if (Music) Music.btAudioConnectNow()
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  CORE · Steering Keys
                    // ═══════════════════════════════════════════════════
                    // The transport keys already work over MPRIS. This page is
                    // for the ones that do not reach it: the stalk's Mode/OK
                    // and the two telephone buttons. Which code the car sends
                    // for each is car-specific, so "Last button" is the way to
                    // find out — press it and read the code.
                    Column {
                        visible: root.activeSection === "Steering Keys"
                        width: bodyCol.width
                        spacing: 0

                        UI.ListRow {
                            label: "Wheel & head-unit buttons"
                            sub: "Reads Bluetooth key events the media proxy drops"
                            UI.Toggle {
                                checked: Music ? Music.mediaKeysEnabled : false
                                onToggled: function (v) { if (Music) Music.setMediaKeysEnabled(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Input source"
                            sub: "Bluetooth input devices being read"
                            ValueText {
                                text: Music ? Music.mediaKeyDevices : "---"
                                color: Music && Music.mediaKeysListening ? Theme.okSoft : Theme.inkFaint
                            }
                        }

                        UI.ListRow {
                            label: "Last button"
                            sub: "Press a wheel button — its code appears here"
                            ValueText {
                                text: Music && Music.lastMediaKey ? Music.lastMediaKey : "Nothing yet"
                                color: Music && Music.lastMediaKey ? Theme.ink : Theme.inkGhost
                            }
                        }

                        UI.ListRow {
                            label: "Lower music on Answer"
                            sub: "How far playback drops when the call button is pressed"
                            Row {
                                spacing: 10
                                ValueText {
                                    text: (Music ? Music.callDuckPercent : 0) + "%"
                                    height: 44
                                    verticalAlignment: Text.AlignVCenter
                                }
                                UI.PillButton {
                                    text: "−"; implicitWidth: 64; height: 44; radius: Theme.radiusControl
                                    onClicked: if (Music) Music.setCallDuckPercent((Music.callDuckPercent || 0) - 10)
                                }
                                UI.PillButton {
                                    text: "+"; implicitWidth: 64; height: 44; radius: Theme.radiusControl
                                    onClicked: if (Music) Music.setCallDuckPercent((Music.callDuckPercent || 0) + 10)
                                }
                            }
                        }

                        Block {
                            UI.SectionLabel { label: "Bindings" }

                            // One cycling pill per key rather than a Segmented:
                            // there are eleven actions, and eleven segments in a
                            // row this narrow are both unreadable and wider than
                            // the space, which makes Segmented's centred trough
                            // spill over the key name.
                            Column {
                                width: parent.width
                                spacing: 8

                                Repeater {
                                    model: Music ? Music.mediaKeyBindings : []

                                    Item {
                                        width: parent.width
                                        height: 46

                                        Text {
                                            anchors.left: parent.left
                                            anchors.right: bindPill.left
                                            anchors.rightMargin: 14
                                            anchors.verticalCenter: parent.verticalCenter
                                            text: modelData.name + "  (" + modelData.code + ")"
                                            color: Theme.inkDim
                                            font.family: Theme.fontFamily
                                            font.pixelSize: 15
                                            elide: Text.ElideRight
                                        }

                                        UI.PillButton {
                                            id: bindPill
                                            anchors.right: parent.right
                                            anchors.verticalCenter: parent.verticalCenter
                                            width: 220
                                            height: 40
                                            radius: Theme.radiusControl
                                            fontSize: 14
                                            checked: modelData.action !== ""
                                            text: modelData.label
                                            onClicked: {
                                                if (!Music) return;
                                                var acts = Music.mediaKeyActions;
                                                var at = 0;
                                                for (var i = 0; i < acts.length; ++i)
                                                    if (acts[i].value === modelData.action) { at = i; break; }
                                                var next = acts[(at + 1) % acts.length].value;
                                                Music.setMediaKeyBinding(modelData.code, next);
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  CORE · AirPhone
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "AirPhone"
                        width: bodyCol.width
                        spacing: 0

                        UI.ListRow {
                            label: "Air for iPhone"
                            sub: "Allow the AirPlay receiver — start it from the AirPlay page"
                            UI.Toggle {
                                checked: Settings && root._settingsRev >= 0 && Settings.getBool("AirPlay", "enabled")
                                onToggled: function (v) {
                                    if (Settings) Settings.setValue("AirPlay", "enabled", v ? "true" : "false");
                                    // Turning it off stops a running receiver;
                                    // turning it on no longer launches one.
                                    // rpiplay is a resident process for
                                    // something wanted occasionally.
                                    if (!v && AirPhone) AirPhone.stop();
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Start receiver at boot"
                            sub: "Off: rpiplay only runs after Start AirPlay is pressed"
                            UI.Toggle {
                                checked: AirPhone ? AirPhone.autostart : false
                                onToggled: function (v) { if (AirPhone) AirPhone.setAutostart(v); }
                            }
                        }

                        UI.ListRow {
                            label: "AirPlay Status"
                            ValueText { text: AirPhone ? AirPhone.statusText : "---" }
                        }

                        UI.ListRow {
                            label: "rpiplay"
                            sub: "Receiver binary"
                            ValueText {
                                text: AirPhone ? (AirPhone.rpiplayAvailable ? "Available" : "Missing") : "---"
                                color: AirPhone && AirPhone.rpiplayAvailable ? Theme.okSoft : Theme.dangerSoft
                            }
                        }

                        UI.ListRow {
                            label: "AirPlay Volume"
                            sub: "Level of the incoming stream"
                            Row {
                                spacing: 10
                                ValueText {
                                    text: (AirPhone ? AirPhone.volume : 0) + "%"
                                    height: 44
                                    verticalAlignment: Text.AlignVCenter
                                }
                                UI.PillButton {
                                    text: "−"; implicitWidth: 64; height: 44; radius: Theme.radiusControl
                                    onClicked: if (AirPhone) AirPhone.setVolume((AirPhone.volume || 0) - 5)
                                }
                                UI.PillButton {
                                    text: "+"; implicitWidth: 64; height: 44; radius: Theme.radiusControl
                                    onClicked: if (AirPhone) AirPhone.setVolume((AirPhone.volume || 0) + 5)
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Lower music while streaming"
                            sub: AirPhone && AirPhone.duckPercent > 0
                                 ? "Music drops to " + (100 - AirPhone.duckPercent) + "% of its level"
                                 : "Off — music keeps its level"
                            Row {
                                spacing: 10
                                ValueText {
                                    text: (AirPhone ? AirPhone.duckPercent : 0) + "%"
                                    color: AirPhone && AirPhone.musicDucked ? Theme.accent : Theme.inkDim
                                    height: 44
                                    verticalAlignment: Text.AlignVCenter
                                }
                                UI.PillButton {
                                    text: "−"; implicitWidth: 64; height: 44; radius: Theme.radiusControl
                                    onClicked: if (AirPhone) AirPhone.setDuckPercent((AirPhone.duckPercent || 0) - 5)
                                }
                                UI.PillButton {
                                    text: "+"; implicitWidth: 64; height: 44; radius: Theme.radiusControl
                                    onClicked: if (AirPhone) AirPhone.setDuckPercent((AirPhone.duckPercent || 0) + 5)
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Last Log"
                            divider: false
                            ValueText { text: AirPhone ? (AirPhone.lastError || AirPhone.lastLog || "---") : "---" }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  CORE · 70mai
                    // ═══════════════════════════════════════════════════
                    // The dashcam module. Off = nothing runs; on = a "70mai"
                    // pill after Horn opens its page. `autostart` is the
                    // "connect at boot" service switch (bridges/dashcam_bridge.py).
                    Column {
                        id: dashcamSection
                        visible: root.activeSection === "70mai"
                        width: bodyCol.width
                        spacing: 0

                        readonly property bool on: Dashcam ? Dashcam.enabled : false
                        readonly property string state: Dashcam ? Dashcam.state : "off"
                        readonly property bool linked: Dashcam ? Dashcam.linked : false

                        // Detect once when the section is opened, so the
                        // network row can say what it sees.
                        onVisibleChanged: if (visible && on && Dashcam) Dashcam.detect()

                        UI.ListRow {
                            label: "70mai dashcam"
                            sub: "Adds the 70mai page to the dock — live view, status, clips"
                            UI.Toggle {
                                checked: dashcamSection.on
                                onToggled: function (v) { if (Dashcam) Dashcam.setEnabled(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Connect at boot"
                            sub: "Joins the camera's Wi-Fi whenever the car is awake; off: only from the 70mai page"
                            enabled: dashcamSection.on
                            UI.Toggle {
                                checked: Dashcam ? Dashcam.autostart : false
                                enabled: dashcamSection.on
                                onToggled: function (v) { if (Dashcam) Dashcam.setAutostart(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Camera network"
                            sub: !Dashcam ? "" : Dashcam.ssidSetting
                                 ? "Pinned — Auto follows the strongest 70mai_* in range"
                                 : (Dashcam.detectedSsid ? "Auto  ·  " + Dashcam.detectedSsid + " detected"
                                                         : "Auto  ·  the strongest 70mai_* in range")
                            clickable: dashcamSection.on
                            enabled: dashcamSection.on
                            onClicked: dashcamNetSheet.opened = true
                            Row {
                                spacing: 10
                                ValueText {
                                    height: 44
                                    verticalAlignment: Text.AlignVCenter
                                    text: Dashcam ? (Dashcam.ssidSetting || "Auto") : "—"
                                }
                                UI.PillButton {
                                    text: "Pick"
                                    implicitWidth: 100
                                    height: 44
                                    radius: Theme.radiusControl
                                    enabled: dashcamSection.on
                                    onClicked: { if (Dashcam) Dashcam.detect(); dashcamNetSheet.opened = true; }
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Password"
                            sub: "Default 12345678 — or join the camera once from Networks so it is saved"
                            enabled: dashcamSection.on
                            Rectangle {
                                width: 220
                                height: 44
                                radius: Theme.radiusControl
                                color: Theme.surfaceRaised
                                TextInput {
                                    id: dashcamPwInput
                                    anchors.fill: parent
                                    anchors.leftMargin: 12
                                    anchors.rightMargin: 12
                                    color: Theme.ink
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 15
                                    verticalAlignment: TextInput.AlignVCenter
                                    clip: true
                                    enabled: dashcamSection.on
                                    text: Dashcam ? Dashcam.password : ""
                                    onEditingFinished: if (Dashcam) Dashcam.setPassword(text)
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Adapter"
                            sub: "Which radio joins the camera — the other keeps the house / phone link"
                            visible: Settings ? Settings.hasSecondWifiAdapter : false
                            enabled: dashcamSection.on
                            UI.Segmented {
                                model: [
                                    { label: "Pi built-in · " + (Settings ? Settings.wifiAdapterName : "wlan0"),
                                      value: Settings ? Settings.wifiAdapterName : "wlan0" },
                                    { label: "USB · " + (Settings ? Settings.secondWifiAdapterName : "wlan1"),
                                      value: Settings ? Settings.secondWifiAdapterName : "wlan1" }
                                ]
                                currentValue: Dashcam ? Dashcam.iface : "wlan0"
                                onSelected: function (v) { if (Dashcam) Dashcam.setIface(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Networks"
                            sub: "First time: join the camera's Wi-Fi here so NetworkManager saves the password"
                            clickable: true
                            onClicked: wifiSheet.opened = true
                            UI.PillButton {
                                text: "Manage"
                                implicitWidth: 120
                                height: 44
                                radius: Theme.radiusControl
                                enabled: Settings ? Settings.wifiAvailable : false
                                onClicked: wifiSheet.opened = true
                            }
                        }

                        UI.ListRow {
                            label: "Status"
                            sub: !Dashcam ? "" : (Dashcam.statusText || (dashcamSection.on ? "Not connected" : "Off"))
                                 + (Dashcam.camIp ? "  ·  " + Dashcam.camIp : "")
                                 + (Dashcam.lastError ? "  ·  " + Dashcam.lastError : "")
                            Row {
                                spacing: 10
                                ValueText {
                                    height: 44
                                    verticalAlignment: Text.AlignVCenter
                                    color: dashcamSection.state === "online" ? Theme.ok
                                         : dashcamSection.state === "error" ? Theme.dangerSoft : Theme.inkFaint
                                    text: {
                                        var st = dashcamSection.state;
                                        return !dashcamSection.on ? "off"
                                             : st === "online" ? "online"
                                             : st === "needs_pairing" ? "not paired"
                                             : st === "pairing" ? "pairing…"
                                             : st === "joining" ? "joining…"
                                             : st === "scanning" ? "looking…"
                                             : st === "error" ? "error" : "not connected";
                                    }
                                }
                                UI.PillButton {
                                    text: dashcamSection.linked || dashcamSection.state === "joining" ? "Disconnect" : "Connect"
                                    implicitWidth: 130
                                    height: 44
                                    radius: Theme.radiusControl
                                    enabled: dashcamSection.on && Dashcam && !Dashcam.busy
                                    onClicked: {
                                        if (!Dashcam) return;
                                        if (dashcamSection.linked || dashcamSection.state === "joining") Dashcam.disconnectNow();
                                        else Dashcam.connectNow();
                                    }
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Pairing"
                            sub: Dashcam && Dashcam.paired
                                 ? "Paired — the camera accepts signed calls from this Pi"
                                 : "One-time: press Pair, then the camera's power button when asked"
                            enabled: dashcamSection.on
                            Row {
                                spacing: 10
                                ValueText {
                                    height: 44
                                    verticalAlignment: Text.AlignVCenter
                                    color: Dashcam && Dashcam.paired ? Theme.ok : Theme.warn
                                    text: Dashcam ? (Dashcam.pairing ? "press the button…" : Dashcam.paired ? "paired ✓" : "not paired") : "—"
                                }
                                UI.PillButton {
                                    text: Dashcam && Dashcam.pairing ? "Cancel" : "Pair"
                                    implicitWidth: 100
                                    height: 44
                                    radius: Theme.radiusControl
                                    variant: Dashcam && Dashcam.pairing ? "outline" : "accent"
                                    enabled: dashcamSection.on && dashcamSection.linked
                                    onClicked: {
                                        if (!Dashcam) return;
                                        if (Dashcam.pairing) Dashcam.cancelPair(); else Dashcam.pair();
                                    }
                                }
                                UI.PillButton {
                                    text: "Forget"
                                    implicitWidth: 100
                                    height: 44
                                    radius: Theme.radiusControl
                                    variant: "outline"
                                    visible: Dashcam ? Dashcam.paired : false
                                    onClicked: if (Dashcam) Dashcam.forgetPairing()
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Live view on open"
                            sub: "Start the picture as soon as the 70mai page shows; off: the Live view button"
                            enabled: dashcamSection.on
                            divider: false
                            UI.Toggle {
                                checked: Dashcam ? Dashcam.liveOnOpen : true
                                enabled: dashcamSection.on
                                onToggled: function (v) { if (Dashcam) Dashcam.setLiveOnOpen(v); }
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  CORE · Action cam
                    // ═══════════════════════════════════════════════════
                    // GoPro (Open GoPro over USB or its Wi-Fi) or Insta360
                    // (Webcam mode over USB, or its Wi-Fi). Same shape as the
                    // 70mai section; bridges/actioncam_bridge.py.
                    Column {
                        id: acamSection
                        visible: root.activeSection === "Action cam"
                        width: bodyCol.width
                        spacing: 0

                        readonly property bool on: ActionCam ? ActionCam.enabled : false
                        readonly property string kind: ActionCam ? ActionCam.kind : "gopro"
                        readonly property string link: ActionCam ? ActionCam.link : "usb"
                        readonly property bool wifi: link === "wifi"
                        readonly property string state: ActionCam ? ActionCam.state : "off"
                        readonly property bool linked: ActionCam ? ActionCam.linked : false

                        onVisibleChanged: if (visible && on && wifi && ActionCam) ActionCam.detect()

                        UI.ListRow {
                            label: "Action cam"
                            sub: "Adds a " + (ActionCam ? ActionCam.label : "camera") + " page to the dock — live view, recording, status"
                            UI.Toggle {
                                checked: acamSection.on
                                onToggled: function (v) { if (ActionCam) ActionCam.setEnabled(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Camera"
                            sub: acamSection.kind === "gopro"
                                 ? "HERO9 Black or newer — Open GoPro API"
                                 : "X3 / X4 / X5, Ace Pro, ONE RS — Webcam mode over USB; Wi-Fi control is per model"
                            enabled: acamSection.on
                            UI.Segmented {
                                model: [ { label: "GoPro", value: "gopro" }, { label: "Insta360", value: "insta360" } ]
                                currentValue: acamSection.kind
                                onSelected: function (v) { if (ActionCam) ActionCam.setKind(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Link"
                            sub: acamSection.link === "usb"
                                 ? (acamSection.kind === "gopro"
                                    ? "USB cable — camera in GoPro Connect mode. Power the camera separately, the Pi cannot charge it"
                                    : "USB cable — camera in Webcam mode. Power it separately, the Pi cannot charge it")
                                 : "The camera's own hotspot on the adapter below"
                            enabled: acamSection.on
                            UI.Segmented {
                                model: [ { label: "USB", value: "usb" }, { label: "Wi-Fi", value: "wifi" } ]
                                currentValue: acamSection.link
                                onSelected: function (v) { if (ActionCam) ActionCam.setLink(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Connect at boot"
                            sub: "Looks for the camera whenever the car is awake; off: only from its page"
                            enabled: acamSection.on
                            UI.Toggle {
                                checked: ActionCam ? ActionCam.autostart : false
                                enabled: acamSection.on
                                onToggled: function (v) { if (ActionCam) ActionCam.setAutostart(v); }
                            }
                        }

                        // ── Wi-Fi rows ─────────────────────────────────
                        UI.ListRow {
                            visible: acamSection.wifi
                            label: "Camera network"
                            sub: !ActionCam ? "" : ActionCam.ssidSetting
                                 ? "Pinned"
                                 : (ActionCam.detectedNetworks.length > 0
                                    ? "Auto  ·  " + ActionCam.detectedNetworks[0].ssid + " detected"
                                    : "Auto  ·  the strongest " + (acamSection.kind === "gopro" ? "GP…/GoPro…" : "Insta360…") + " in range")
                            clickable: acamSection.on
                            enabled: acamSection.on
                            onClicked: acamNetSheet.opened = true
                            Row {
                                spacing: 10
                                ValueText {
                                    height: 44
                                    verticalAlignment: Text.AlignVCenter
                                    text: ActionCam ? (ActionCam.ssidSetting || "Auto") : "—"
                                }
                                UI.PillButton {
                                    text: "Pick"
                                    implicitWidth: 100
                                    height: 44
                                    radius: Theme.radiusControl
                                    enabled: acamSection.on
                                    onClicked: { if (ActionCam) ActionCam.detect(); acamNetSheet.opened = true; }
                                }
                            }
                        }

                        UI.ListRow {
                            visible: acamSection.wifi
                            label: "Password"
                            sub: acamSection.kind === "gopro"
                                 ? "Shown on the camera under Connections — or read over Bluetooth by Pair below"
                                 : "Default 88888888 — or join once from Networks so it is saved"
                            enabled: acamSection.on
                            Rectangle {
                                width: 220
                                height: 44
                                radius: Theme.radiusControl
                                color: Theme.surfaceRaised
                                TextInput {
                                    id: acamPwInput
                                    anchors.fill: parent
                                    anchors.leftMargin: 12
                                    anchors.rightMargin: 12
                                    color: Theme.ink
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 15
                                    verticalAlignment: TextInput.AlignVCenter
                                    clip: true
                                    enabled: acamSection.on
                                    text: ActionCam ? ActionCam.password : ""
                                    onEditingFinished: if (ActionCam) ActionCam.setPassword(text)
                                }
                            }
                        }

                        UI.ListRow {
                            visible: acamSection.wifi && Settings && Settings.hasSecondWifiAdapter
                            label: "Adapter"
                            sub: Dashcam && Dashcam.enabled && Dashcam.iface === (ActionCam ? ActionCam.iface : "")
                                 ? "Same radio as the 70mai — one hotspot per adapter, pick the other one"
                                 : "Which radio joins the camera"
                            enabled: acamSection.on
                            UI.Segmented {
                                model: [
                                    { label: "Pi built-in · " + (Settings ? Settings.wifiAdapterName : "wlan0"),
                                      value: Settings ? Settings.wifiAdapterName : "wlan0" },
                                    { label: "USB · " + (Settings ? Settings.secondWifiAdapterName : "wlan1"),
                                      value: Settings ? Settings.secondWifiAdapterName : "wlan1" }
                                ]
                                currentValue: ActionCam ? ActionCam.iface : "wlan1"
                                onSelected: function (v) { if (ActionCam) ActionCam.setIface(v); }
                            }
                        }

                        UI.ListRow {
                            visible: acamSection.wifi && acamSection.kind === "gopro"
                            label: "Bluetooth"
                            sub: ActionCam && ActionCam.bleMac
                                 ? "Paired " + ActionCam.bleMac + " — Wi-Fi on wakes the camera's hotspot" + (ActionCam.bleNote ? "  ·  " + ActionCam.bleNote : "")
                                 : "Pair once: camera › Preferences › Connections › Connect Device › GoPro Quik App" + (ActionCam && ActionCam.bleNote ? "  ·  " + ActionCam.bleNote : "")
                            enabled: acamSection.on
                            Row {
                                spacing: 10
                                UI.PillButton {
                                    text: "Pair"
                                    implicitWidth: 100
                                    height: 44
                                    radius: Theme.radiusControl
                                    variant: ActionCam && ActionCam.bleMac ? "outline" : "accent"
                                    enabled: acamSection.on && ActionCam && !ActionCam.busy
                                    onClicked: if (ActionCam) ActionCam.pairBle()
                                }
                                UI.PillButton {
                                    text: "Wi-Fi on"
                                    implicitWidth: 110
                                    height: 44
                                    radius: Theme.radiusControl
                                    visible: ActionCam ? ActionCam.bleMac !== "" : false
                                    enabled: acamSection.on && ActionCam && !ActionCam.busy
                                    onClicked: if (ActionCam) ActionCam.wakeWifi()
                                }
                                UI.PillButton {
                                    text: "Forget"
                                    implicitWidth: 100
                                    height: 44
                                    radius: Theme.radiusControl
                                    variant: "outline"
                                    visible: ActionCam ? ActionCam.bleMac !== "" : false
                                    onClicked: if (ActionCam) ActionCam.forgetBle()
                                }
                            }
                        }

                        UI.ListRow {
                            visible: acamSection.wifi
                            label: "Networks"
                            sub: "Join the camera's Wi-Fi here once so NetworkManager saves the password"
                            clickable: true
                            onClicked: wifiSheet.opened = true
                            UI.PillButton {
                                text: "Manage"
                                implicitWidth: 120
                                height: 44
                                radius: Theme.radiusControl
                                enabled: Settings ? Settings.wifiAvailable : false
                                onClicked: wifiSheet.opened = true
                            }
                        }

                        // ── stream ─────────────────────────────────────
                        UI.ListRow {
                            visible: acamSection.kind === "gopro"
                            label: "Picture"
                            sub: ActionCam && ActionCam.stream === "webcam"
                                 ? "Webcam mode: sharp, but the camera does not record meanwhile"
                                 : "Preview: small, works alongside recording on newer models"
                            enabled: acamSection.on
                            UI.Segmented {
                                model: [
                                    { label: "Preview",     value: "preview" },
                                    { label: "Webcam 720p", value: "webcam7" },
                                    { label: "Webcam 1080p", value: "webcam12" }
                                ]
                                currentValue: !ActionCam ? "webcam12"
                                            : ActionCam.stream === "preview" ? "preview"
                                            : (ActionCam.webcamRes === 7 ? "webcam7" : "webcam12")
                                onSelected: function (v) {
                                    if (!ActionCam) return;
                                    if (v === "preview") { ActionCam.setStream("preview"); return; }
                                    ActionCam.setWebcamRes(v === "webcam7" ? 7 : 12);
                                    ActionCam.setStream("webcam");
                                }
                            }
                        }

                        UI.ListRow {
                            visible: acamSection.kind === "gopro"
                            label: "Field of view"
                            sub: "Webcam picture"
                            enabled: acamSection.on
                            UI.Segmented {
                                model: [
                                    { label: "Wide",   value: 0 },
                                    { label: "Linear", value: 4 },
                                    { label: "Narrow", value: 2 },
                                    { label: "SuperView", value: 3 }
                                ]
                                currentValue: ActionCam ? ActionCam.webcamFov : 0
                                onSelected: function (v) { if (ActionCam) ActionCam.setWebcamFov(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Status"
                            sub: !ActionCam ? "" : (ActionCam.statusText || (acamSection.on ? "Not connected" : "Off"))
                                 + (ActionCam.camIp ? "  ·  " + ActionCam.camIp : "")
                                 + (ActionCam.lastError ? "  ·  " + ActionCam.lastError : "")
                            Row {
                                spacing: 10
                                ValueText {
                                    height: 44
                                    verticalAlignment: Text.AlignVCenter
                                    color: acamSection.state === "online" ? Theme.ok
                                         : acamSection.state === "error" ? Theme.dangerSoft : Theme.inkFaint
                                    text: {
                                        var st = acamSection.state;
                                        return !acamSection.on ? "off"
                                             : st === "online" ? "online"
                                             : st === "joining" ? "joining…"
                                             : st === "scanning" ? "looking…"
                                             : st === "error" ? "error" : "not connected";
                                    }
                                }
                                UI.PillButton {
                                    text: acamSection.linked || acamSection.state === "joining" ? "Disconnect" : "Connect"
                                    implicitWidth: 130
                                    height: 44
                                    radius: Theme.radiusControl
                                    enabled: acamSection.on && ActionCam && !ActionCam.busy
                                    onClicked: {
                                        if (!ActionCam) return;
                                        if (acamSection.linked || acamSection.state === "joining") ActionCam.disconnectNow();
                                        else ActionCam.connectNow();
                                    }
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Live view on open"
                            sub: "Start the picture as soon as the page shows; off: the Live view button"
                            enabled: acamSection.on
                            UI.Toggle {
                                checked: ActionCam ? ActionCam.liveOnOpen : true
                                enabled: acamSection.on
                                onToggled: function (v) { if (ActionCam) ActionCam.setLiveOnOpen(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Show when reversing"
                            sub: "The live picture over the Dashboard while reverse is engaged — needs the CAN \"Reverse gear\" signal mapped in the CAN Debugger"
                            enabled: acamSection.on
                            divider: false
                            UI.Toggle {
                                checked: ActionCam ? ActionCam.reverseView : false
                                enabled: acamSection.on
                                onToggled: function (v) { if (ActionCam) ActionCam.setReverseView(v); }
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  APPEARANCE · Themes
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "Themes"
                        width: bodyCol.width
                        spacing: 0

                        // New in the modern skin: the skin itself is a setting.
                        UI.ListRow {
                            label: "Style"
                            sub: "Applied on next start"
                            UI.Segmented {
                                model: Settings ? Settings.availableStyles : []
                                currentValue: Settings ? Settings.styleName : ""
                                onSelected: function (v) { if (Settings) Settings.setStyleName(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Light Theme"
                            sub: "Light surfaces with dark text, applied immediately"
                            UI.Toggle {
                                checked: Theme ? Theme.lightTheme : false
                                onToggled: function (v) { if (Theme) Theme.setLightTheme(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Glass Effect"
                            sub: "Enable frosted-glass transparency on widget containers"
                            UI.Toggle {
                                checked: Settings ? Settings.glassEnabled : false
                                onToggled: function (v) {
                                    if (!Settings) return;
                                    Settings.setValue("Style", "glass_effect_enabled", v ? "true" : "false");
                                    Settings.reload();
                                }
                            }
                        }

                        Block { UI.SectionLabel { label: "Colors" } }

                        UI.ListRow {
                            label: "Primary Color"
                            sub: "base_color_a → base_color_b"
                            clickable: true
                            onClicked: colorSheet.open("Primary Color", "base_color_a", "base_color_b")
                            Swatch {
                                colorA: Settings ? Settings.baseColorA : "#2a2f3a"
                                colorB: Settings ? Settings.baseColorB : "#2a2f3a"
                            }
                        }

                        UI.ListRow {
                            label: "Secondary Color"
                            sub: "secondary_color_a → secondary_color_b"
                            clickable: true
                            onClicked: colorSheet.open("Secondary Color", "secondary_color_a", "secondary_color_b")
                            Swatch {
                                colorA: Settings ? Settings.secondaryColorA : "#2a2f3a"
                                colorB: Settings ? Settings.secondaryColorB : "#2a2f3a"
                            }
                        }

                        UI.ListRow {
                            label: "Third Color"
                            sub: "third_color_a → third_color_b"
                            clickable: true
                            onClicked: colorSheet.open("Third Color", "third_color_a", "third_color_b")
                            Swatch {
                                colorA: Settings ? Settings.thirdColorA : "#2a2f3a"
                                colorB: Settings ? Settings.thirdColorB : "#2a2f3a"
                            }
                        }

                        // Single colours — native picker, no sheet.
                        UI.ListRow {
                            label: "TopBar Color"
                            sub: "Native picker · topbar_color"
                            clickable: true
                            onClicked: {
                                if (!Settings) return;
                                var hex = Settings.pickColor(Settings.topbarColor.toString());
                                if (hex) Settings.setStyleColor("topbar_color", hex);
                            }
                            Swatch {
                                colorA: Settings ? Settings.topbarColor : "#2a2f3a"
                                colorB: Settings ? Settings.topbarColor : "#2a2f3a"
                            }
                        }

                        UI.ListRow {
                            label: "Progress Bar"
                            sub: "Native picker · progress_color"
                            clickable: true
                            divider: false
                            onClicked: {
                                if (!Settings) return;
                                var hex = Settings.pickColor(Settings.progressColor.toString());
                                if (hex) Settings.setStyleColor("progress_color", hex);
                            }
                            Swatch {
                                colorA: Settings ? Settings.progressColor : "#2ec27e"
                                colorB: Settings ? Settings.progressColor : "#2ec27e"
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  APPEARANCE · Display
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "Display"
                        width: bodyCol.width
                        spacing: 0

                        UI.ListRow {
                            label: "Fullscreen"
                            sub: "Borderless full-screen window"
                            UI.Toggle {
                                checked: Settings ? Settings.fullscreenEnabled : false
                                onToggled: function (v) { if (Settings) Settings.setFullscreenEnabled(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Resolution"
                            sub: Settings ? ("Currently " + Settings.resolutionOverride) : "native"
                            divider: false
                            UI.Segmented {
                                model: [
                                    { label: "1280×720", value: "1280x720" },
                                    { label: "1280×800", value: "1280x800" },
                                    { label: "Native",   value: "native" }
                                ]
                                currentValue: Settings ? Settings.resolutionOverride : "native"
                                onSelected: function (v) { if (Settings) Settings.setResolutionOverride(v); }
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  APPEARANCE · Animation
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "Animation"
                        width: bodyCol.width
                        spacing: 0

                        UI.ListRow {
                            label: "Master Animation"
                            sub: "Enable or disable all interface animations"
                            UI.Toggle {
                                checked: root.animMasterEnabled
                                onToggled: function (v) {
                                    root.animMasterEnabled = v;
                                    if (Settings) Settings.setValue("UI", "animation_master_enabled", v ? "1" : "0");
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Intro"
                            sub: "Animate Home music and navigate cards from bottom on page open"
                            enabled: root.animMasterEnabled
                            UI.Toggle {
                                checked: root.animIntroEnabled
                                enabled: root.animMasterEnabled
                                onToggled: function (v) {
                                    root.animIntroEnabled = v;
                                    if (Settings) Settings.setValue("UI", "home_intro_enabled", v ? "1" : "0");
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Settings Animation"
                            sub: "Open Settings from right to left"
                            enabled: root.animMasterEnabled
                            UI.Toggle {
                                checked: root.animSettingsEnabled
                                enabled: root.animMasterEnabled
                                onToggled: function (v) {
                                    root.animSettingsEnabled = v;
                                    if (Settings) Settings.setValue("UI", "settings_page_animation_enabled", v ? "1" : "0");
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Side Panel Animation"
                            sub: "Animate side panel transitions when showing and hiding"
                            enabled: root.animMasterEnabled
                            UI.Toggle {
                                checked: root.animSidePanelEnabled
                                enabled: root.animMasterEnabled
                                onToggled: function (v) {
                                    root.animSidePanelEnabled = v;
                                    if (Settings) Settings.setValue("UI", "side_panel_animation_enabled", v ? "1" : "0");
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Home Widgets"
                            sub: "Show OBD status and session restore cards on the home page"
                            UI.Toggle {
                                checked: root.animHomeWidgetsEnabled
                                onToggled: function (v) {
                                    root.animHomeWidgetsEnabled = v;
                                    if (Settings) Settings.setHomeWidgetsEnabled(v);
                                }
                            }
                        }

                        Block { UI.SectionLabel { label: "Screensaver" } }

                        UI.ListRow {
                            label: "Enable Parking Screen Saver"
                            sub: "Show the parked screensaver after an idle timeout"
                            UI.Toggle {
                                checked: root.parkingScreenSaverEnabled
                                onToggled: function (v) {
                                    root.parkingScreenSaverEnabled = v;
                                    if (Settings) Settings.setValue("UI", "parking_screensaver_enabled", v ? "true" : "false");
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Parking Timeout"
                            sub: "5 – 300 seconds"
                            enabled: root.parkingScreenSaverEnabled
                            SliderControl {
                                value: (root.parkingTimeoutSec - 5) / 295.0
                                readout: root.parkingTimeoutSec + "s"
                                onMoved: function (v) {
                                    root.parkingTimeoutSec = root._boundedTimeout(Math.round(5 + v * 295), 20);
                                }
                                onReleased: function (v) {
                                    root.parkingTimeoutSec = root._boundedTimeout(Math.round(5 + v * 295), 20);
                                    if (Settings) Settings.setValue("UI", "parking_screensaver_timeout_sec",
                                                                    root.parkingTimeoutSec.toString());
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Enable Driving Screen Saver"
                            sub: "Show the driving screensaver above 8 km/h"
                            UI.Toggle {
                                checked: root.drivingScreenSaverEnabled
                                onToggled: function (v) {
                                    root.drivingScreenSaverEnabled = v;
                                    if (Settings) Settings.setValue("UI", "driving_screensaver_enabled", v ? "true" : "false");
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Driving Timeout"
                            sub: "5 – 300 seconds"
                            divider: false
                            enabled: root.drivingScreenSaverEnabled
                            SliderControl {
                                value: (root.drivingTimeoutSec - 5) / 295.0
                                readout: root.drivingTimeoutSec + "s"
                                onMoved: function (v) {
                                    root.drivingTimeoutSec = root._boundedTimeout(Math.round(5 + v * 295), 20);
                                }
                                onReleased: function (v) {
                                    root.drivingTimeoutSec = root._boundedTimeout(Math.round(5 + v * 295), 20);
                                    if (Settings) Settings.setValue("UI", "driving_screensaver_timeout_sec",
                                                                    root.drivingTimeoutSec.toString());
                                }
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  APPEARANCE · Font — 15 groups, each opens the sheet
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "Font"
                        width: bodyCol.width
                        spacing: 0

                        Repeater {
                            model: [
                                { key: "settings_primary",          title: "Settings Menu" },
                                { key: "settings_primary_text",     title: "Settings Label" },
                                { key: "settings_secondary_text",   title: "Settings Secondary" },
                                { key: "settings_tertiary_text",    title: "Settings Caption" },
                                { key: "top_bar_text",              title: "Top Bar Text" },
                                { key: "buttons_push_text",         title: "Buttons Push Text" },
                                { key: "buttons_squareaction_text", title: "Buttons Square Text" },
                                { key: "buttons_multiselect_text",  title: "Buttons Multi Text" },
                                { key: "buttons_medium_text",       title: "Buttons Medium Text" },
                                { key: "buttons_popup_text",        title: "Buttons Popup Text" },
                                { key: "widgets_primary_text",      title: "Widgets Primary Text" },
                                { key: "widgets_secondary_text",    title: "Widgets Secondary Text" },
                                { key: "widgets_tertiary_text",     title: "Widgets Tertiary Text" },
                                { key: "pages_primary_text",        title: "Pages Primary Text" },
                                { key: "pages_secondary_text",      title: "Pages Secondary Text" }
                            ]

                            UI.ListRow {
                                label: modelData.title
                                sub: modelData.key
                                clickable: true
                                divider: index < 14
                                onClicked: fontSheet.open(modelData.key, modelData.title)
                                ValueChevron {
                                    value: root._settingsRev >= 0 ? (root._fontSize(modelData.key) + " px") : ""
                                }
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  APPEARANCE · UI — modern component gallery
                    //
                    //  Legacy's UI page was a catalog of the QWidget-era
                    //  ThemedButton presets (small/medium/wide/big/glass/
                    //  square/multi). None of those exist in this skin, and
                    //  none of them were settings. Rebuilt as a live gallery
                    //  of the components pages are actually built from, so it
                    //  still earns its place at design review.
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "UI"
                        width: bodyCol.width
                        spacing: 0

                        Block {
                            UI.SectionLabel { label: "PillButton — variants" }
                            Flow {
                                width: parent.width
                                spacing: 10
                                UI.PillButton { text: "raised";  variant: "raised" }
                                UI.PillButton { text: "accent";  variant: "accent" }
                                UI.PillButton { text: "ghost";   variant: "ghost" }
                                UI.PillButton { text: "outline"; variant: "outline" }
                                UI.PillButton { text: "danger";  variant: "danger" }
                                UI.PillButton { text: "checked"; variant: "raised"; checked: true }
                                UI.PillButton { text: "disabled"; variant: "raised"; enabled: false }
                            }
                        }

                        Block {
                            UI.SectionLabel { label: "PillButton — with icon, and IconButton" }
                            Row {
                                spacing: 10
                                UI.PillButton {
                                    text: "Soundboard"
                                    iconSource: iconsPath + "/soundboard.png"
                                    iconSize: 22
                                }
                                UI.IconButton { glyph: "♫"; glyphSize: 22 }
                                UI.IconButton { glyph: "▶"; glyphSize: 20; checked: true }
                                UI.IconButton {
                                    iconSource: iconsPath + "/options.png"
                                    iconSize: 24
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Toggle"
                            sub: "56 × 32 · accent track when on"
                            Row {
                                spacing: 14
                                UI.Toggle { checked: true }
                                UI.Toggle { checked: false }
                                UI.Toggle { checked: true; enabled: false }
                            }
                        }

                        UI.ListRow {
                            label: "Slider"
                            sub: "0…1 · moved() while dragging, released() on commit"
                            SliderControl {
                                id: gallerySlider
                                value: 0.62
                                readout: Math.round(value * 100) + "%"
                                onMoved: function (v) { gallerySlider.value = v; }
                            }
                        }

                        UI.ListRow {
                            label: "Segmented"
                            sub: "One pill per option, accent marks the selection"
                            UI.Segmented {
                                id: gallerySeg
                                model: ["Quiet", "Normal", "Sport"]
                                currentValue: "Normal"
                                onSelected: function (v) { gallerySeg.currentValue = v; }
                            }
                        }

                        Block {
                            divider: false
                            UI.SectionLabel { label: "StatTile" }
                            Row {
                                spacing: 14
                                UI.StatTile {
                                    width: (parent.parent.width - 28) / 3
                                    label: "Speed"
                                    value: "72"
                                    unit: "km/h"
                                }
                                UI.StatTile {
                                    width: (parent.parent.width - 28) / 3
                                    label: "Coolant"
                                    value: "88"
                                    unit: "°C"
                                    valueColor: Theme.ok
                                }
                                UI.StatTile {
                                    width: (parent.parent.width - 28) / 3
                                    label: "RPM"
                                    value: "3120"
                                    hero: true
                                    valueSize: 44
                                }
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  DRIVING · Engine Sound
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "Engine Sound"
                        width: bodyCol.width
                        spacing: 0

                        UI.ListRow {
                            label: "Hybrid"
                            sub: "Enable hybrid electric + engine behavior"
                            UI.Toggle {
                                checked: Modding ? Modding.hybridActive : false
                                onToggled: function (v) { if (Modding) Modding.setHybridActive(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Current Preset"
                            sub: Modding ? Modding.preset : "---"
                            UI.Segmented {
                                model: Modding ? Modding.presets : ["Quiet", "Normal", "Sport", "JDM", "Extreme"]
                                currentValue: Modding ? Modding.preset : ""
                                onSelected: function (v) { if (Modding) Modding.setPreset(v); }
                            }
                        }

                        Block {
                            UI.SectionLabel { label: "Output Channel" }
                            // "System Default" is auto: it resolves to a wired
                            // output when one exists. Bluetooth (the R-Link)
                            // adds ~300 ms, which the driver hears as engine lag.
                            Text {
                                width: parent.width
                                text: Modding ? ("Auto → " + Modding.engineOutputResolved
                                                 + " · Bluetooth outputs add ~300 ms of lag")
                                              : ""
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: 14
                                wrapMode: Text.WordWrap
                            }
                            Flow {
                                width: parent.width
                                spacing: 8
                                Repeater {
                                    model: Modding ? Modding.engineOutputOptions : []
                                    UI.PillButton {
                                        text: modelData.label || modelData.value || "?"
                                        height: 46
                                        radius: Theme.radiusControl
                                        checked: Modding ? (Modding.engineOutputChannel || "") === (modelData.value || "") : false
                                        onClicked: {
                                            if (!Modding) return;
                                            var cur = Modding.engineOutputChannel || "";
                                            Modding.setEngineOutputChannel(cur === (modelData.value || "") ? "" : (modelData.value || ""));
                                        }
                                    }
                                }
                                Text {
                                    visible: !Modding || Modding.engineOutputOptions.length === 0
                                    text: "No output channels reported."
                                    color: Theme.inkFaint
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 14
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Master Volume"
                            sub: "Engine sound output level"
                            divider: false
                            SliderControl {
                                value: Modding ? Modding.masterVolume / 100.0 : 0.5
                                readout: (Modding ? Modding.masterVolume : 50) + "%"
                                onReleased: function (v) { if (Modding) Modding.setMasterVolume(Math.round(v * 100)); }
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  DRIVING · Driving Mode
                    // ═══════════════════════════════════════════════════
                    Column {
                        id: drivingModeSection
                        visible: root.activeSection === "Driving Mode"
                        width: bodyCol.width
                        spacing: 0

                        readonly property bool noobOn:
                            Settings && root._settingsRev >= 0 && Settings.getBool("SoundFeatures", "noob_mode_enabled")
                        property int speedLimit: {
                            var v = Settings && root._settingsRev >= 0 ? Settings.getInt("SoundFeatures", "noob_speed_limit") : 0;
                            return v >= 30 && v <= 150 ? v : 90;
                        }

                        UI.ListRow {
                            label: "Noob Mode"
                            sub: "Beginner driving hints in the Alerts area while the engine runs"
                            UI.Toggle {
                                checked: drivingModeSection.noobOn
                                onToggled: function (v) {
                                    if (Settings) Settings.setValue("SoundFeatures", "noob_mode_enabled", v ? "true" : "false");
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Starting"
                            sub: "Low revs when pulling away, and a stall right after moving off"
                            visible: drivingModeSection.noobOn
                            UI.Toggle {
                                checked: Settings && root._settingsRev >= 0 && Settings.getBool("SoundFeatures", "noob_starting")
                                onToggled: function (v) {
                                    if (Settings) Settings.setValue("SoundFeatures", "noob_starting", v ? "true" : "false");
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Rpm"
                            sub: "Shift up / shift down hints from the gear bands"
                            visible: drivingModeSection.noobOn
                            UI.Toggle {
                                checked: Settings && root._settingsRev >= 0 && Settings.getBool("SoundFeatures", "noob_rpm")
                                onToggled: function (v) {
                                    if (Settings) Settings.setValue("SoundFeatures", "noob_rpm", v ? "true" : "false");
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Speeding"
                            sub: "Alert above your speed limit"
                            visible: drivingModeSection.noobOn
                            UI.Toggle {
                                checked: Settings && root._settingsRev >= 0 && Settings.getBool("SoundFeatures", "noob_speeding")
                                onToggled: function (v) {
                                    if (Settings) Settings.setValue("SoundFeatures", "noob_speeding", v ? "true" : "false");
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Speed Limit"
                            sub: "30 – 150 km/h"
                            visible: drivingModeSection.noobOn
                            enabled: Settings && root._settingsRev >= 0 && Settings.getBool("SoundFeatures", "noob_speeding")
                            SliderControl {
                                value: (drivingModeSection.speedLimit - 30) / 120.0
                                readout: drivingModeSection.speedLimit + " km/h"
                                onMoved: function (v) {
                                    drivingModeSection.speedLimit = Math.round((30 + v * 120) / 5) * 5;
                                }
                                onReleased: function (v) {
                                    drivingModeSection.speedLimit = Math.round((30 + v * 120) / 5) * 5;
                                    if (Settings) Settings.setValue("SoundFeatures", "noob_speed_limit",
                                                                    drivingModeSection.speedLimit.toString());
                                }
                            }
                        }

                        UI.ListRow {
                            label: "UpHill"
                            sub: "On a climb, hint to shift down when the revs are too low"
                            visible: drivingModeSection.noobOn
                            divider: false
                            UI.Toggle {
                                checked: Settings && root._settingsRev >= 0 && Settings.getBool("SoundFeatures", "noob_uphill")
                                onToggled: function (v) {
                                    if (Settings) Settings.setValue("SoundFeatures", "noob_uphill", v ? "true" : "false");
                                }
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  DRIVING · Alerts
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "Alerts"
                        width: bodyCol.width
                        spacing: 0

                        UI.ListRow {
                            label: "UpHill"
                            sub: "When enabled, uphill-assist activity is pushed to the shared alerts widgets."
                            UI.Toggle {
                                checked: !Settings || (root._settingsRev >= 0
                                         && root._asBool(Settings.getValue("Alerts", "uphill_enabled"), true))
                                onToggled: function (v) {
                                    if (Settings) Settings.setValue("Alerts", "uphill_enabled", v ? "true" : "false");
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Freeze"
                            sub: "When enabled, cold-engine/freeze warnings are shown in the shared alerts widgets."
                            UI.Toggle {
                                checked: !Settings || (root._settingsRev >= 0
                                         && root._asBool(Settings.getValue("Alerts", "freeze_enabled"), true))
                                onToggled: function (v) {
                                    if (Settings) Settings.setValue("Alerts", "freeze_enabled", v ? "true" : "false");
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Dash Lights"
                            sub: "When enabled, active dash light warnings are shown in the shared alerts widgets."
                            UI.Toggle {
                                checked: !Settings || (root._settingsRev >= 0
                                         && root._asBool(Settings.getValue("Alerts", "dash_lights_enabled"), true))
                                onToggled: function (v) {
                                    if (Settings) Settings.setValue("Alerts", "dash_lights_enabled", v ? "true" : "false");
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Alert Volume"
                            sub: "Fixed chime level — deliberately not full volume"
                            Row {
                                spacing: 10
                                ValueText {
                                    text: (Alerts ? Alerts.alertVolumePercent : 0) + "%"
                                    height: 44
                                    verticalAlignment: Text.AlignVCenter
                                }
                                UI.PillButton {
                                    text: "−"; implicitWidth: 64; height: 44; radius: Theme.radiusControl
                                    onClicked: if (Alerts) Alerts.setAlertVolumePercent((Alerts.alertVolumePercent || 0) - 5)
                                }
                                UI.PillButton {
                                    text: "+"; implicitWidth: 64; height: 44; radius: Theme.radiusControl
                                    onClicked: if (Alerts) Alerts.setAlertVolumePercent((Alerts.alertVolumePercent || 0) + 5)
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Alert Ducking"
                            sub: "How far music and engine sound drop while a chime plays"
                            divider: false
                            Row {
                                spacing: 10
                                ValueText {
                                    text: (Alerts ? Alerts.alertDuckPercent : 0) + "%"
                                    height: 44
                                    verticalAlignment: Text.AlignVCenter
                                }
                                UI.PillButton {
                                    text: "−"; implicitWidth: 64; height: 44; radius: Theme.radiusControl
                                    onClicked: if (Alerts) Alerts.setAlertDuckPercent((Alerts.alertDuckPercent || 0) - 5)
                                }
                                UI.PillButton {
                                    text: "+"; implicitWidth: 64; height: 44; radius: Theme.radiusControl
                                    onClicked: if (Alerts) Alerts.setAlertDuckPercent((Alerts.alertDuckPercent || 0) + 5)
                                }
                            }
                        }

                        // Per-alert catalog: which alerts actually show, and
                        // which additionally play a chosen sound. The three
                        // toggles above stay the master gate for whole
                        // categories (dash lights, uphill, freeze) exactly as
                        // before — this is a layer on top of that, not a
                        // replacement: an alert only ever shows when both its
                        // category switch and its own Tracked toggle are on.
                        Block {
                            UI.SectionLabel { label: "Alert Sounds" }

                            Column {
                                width: parent.width
                                spacing: 10

                                Repeater {
                                    model: Alerts ? Alerts.alertCatalog : []

                                    Item {
                                        width: parent.width
                                        height: 60

                                        // The alert as Home draws it — severity
                                        // stripe, its Dash icon, the text — so
                                        // what is being switched on or off is
                                        // recognisable at a glance, not a label
                                        // to decode.
                                        readonly property color tone: root._alertTone(modelData.priority)

                                        Rectangle {
                                            id: previewCard
                                            anchors.left: parent.left
                                            anchors.right: controlsRow.left
                                            anchors.rightMargin: 14
                                            anchors.verticalCenter: parent.verticalCenter
                                            height: 52
                                            radius: Theme.radiusControl
                                            color: Theme.surfaceRaised
                                            opacity: modelData.tracked ? 1.0 : 0.45

                                            Rectangle {
                                                id: previewStripe
                                                anchors.left: parent.left
                                                anchors.leftMargin: 12
                                                anchors.verticalCenter: parent.verticalCenter
                                                width: 3; height: 24; radius: 1.5
                                                color: parent.parent.tone
                                            }
                                            Image {
                                                id: previewIcon
                                                anchors.left: previewStripe.right
                                                anchors.leftMargin: 10
                                                anchors.verticalCenter: parent.verticalCenter
                                                width: 26; height: 26
                                                sourceSize.width: 52; sourceSize.height: 52
                                                fillMode: Image.PreserveAspectFit
                                                asynchronous: true
                                                cache: true
                                                visible: modelData.icon !== "" && status === Image.Ready
                                                source: modelData.icon ? ("file://" + iconsPath + "/Dash/" + modelData.icon) : ""
                                            }
                                            Column {
                                                anchors.left: previewIcon.visible ? previewIcon.right : previewStripe.right
                                                anchors.leftMargin: 10
                                                anchors.right: parent.right
                                                anchors.rightMargin: 12
                                                anchors.verticalCenter: parent.verticalCenter
                                                spacing: 1
                                                Text {
                                                    width: parent.width
                                                    text: modelData.label
                                                    color: previewCard.parent.tone
                                                    font.family: Theme.fontFamily
                                                    font.pixelSize: 15
                                                    font.weight: Font.DemiBold
                                                    elide: Text.ElideRight
                                                }
                                                Text {
                                                    width: parent.width
                                                    text: modelData.category
                                                          + (modelData.soundEnabled && modelData.soundName
                                                             ? " · " + modelData.soundName : "")
                                                    color: Theme.inkFaint
                                                    font.family: Theme.fontFamily
                                                    font.pixelSize: 12
                                                    elide: Text.ElideRight
                                                }
                                            }
                                        }

                                        Row {
                                            id: controlsRow
                                            anchors.right: parent.right
                                            anchors.verticalCenter: parent.verticalCenter
                                            spacing: 16

                                            Column {
                                                spacing: 2
                                                anchors.verticalCenter: parent.verticalCenter
                                                Text {
                                                    anchors.horizontalCenter: parent.horizontalCenter
                                                    text: "Track"
                                                    color: Theme.inkFaint
                                                    font.family: Theme.fontFamily
                                                    font.pixelSize: 10
                                                }
                                                UI.Toggle {
                                                    anchors.horizontalCenter: parent.horizontalCenter
                                                    checked: modelData.tracked
                                                    onToggled: function (v) {
                                                        if (Alerts) Alerts.setAlertTracked(modelData.key, v);
                                                    }
                                                }
                                            }

                                            Column {
                                                spacing: 2
                                                anchors.verticalCenter: parent.verticalCenter
                                                Text {
                                                    anchors.horizontalCenter: parent.horizontalCenter
                                                    text: "Sound"
                                                    color: Theme.inkFaint
                                                    font.family: Theme.fontFamily
                                                    font.pixelSize: 10
                                                }
                                                UI.Toggle {
                                                    anchors.horizontalCenter: parent.horizontalCenter
                                                    enabled: modelData.tracked
                                                    checked: modelData.soundEnabled
                                                    onToggled: function (v) {
                                                        if (Alerts) Alerts.setAlertSoundEnabled(modelData.key, v);
                                                    }
                                                }
                                            }

                                            UI.PillButton {
                                                width: 96
                                                height: 40
                                                radius: Theme.radiusControl
                                                fontSize: 13
                                                anchors.verticalCenter: parent.verticalCenter
                                                visible: modelData.soundEnabled
                                                text: modelData.soundName ? "Change" : "Pick"
                                                onClicked: alertSoundPicker.openFor("alert", -1, modelData.key)
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  DRIVING · Status — 18 read-only telemetry rows
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "Status"
                        width: bodyCol.width
                        spacing: 0

                        UI.ListRow {
                            label: "OBD Status"
                            ValueText { text: Telemetry ? Telemetry.obdStatus : "---" }
                        }
                        UI.ListRow {
                            label: "Connected"
                            ValueText {
                                text: Telemetry ? (Telemetry.connected ? "Yes" : "No") : "No"
                                color: Telemetry && Telemetry.connected ? Theme.okSoft : Theme.dangerSoft
                            }
                        }
                        UI.ListRow {
                            label: "RPM"
                            ValueText { text: Telemetry ? Telemetry.rpm.toFixed(0) : "0" }
                        }
                        UI.ListRow {
                            label: "Speed"
                            ValueText { text: Telemetry ? Telemetry.speed.toFixed(0) + " km/h" : "0" }
                        }
                        UI.ListRow {
                            label: "Coolant Temp"
                            ValueText { text: Telemetry ? Telemetry.coolantTemp.toFixed(1) + " °C" : "---" }
                        }
                        UI.ListRow {
                            label: "Oil Temp"
                            ValueText { text: Telemetry ? Telemetry.oilTemp.toFixed(1) + " °C" : "---" }
                        }
                        UI.ListRow {
                            label: "Oil Status"
                            ValueText { text: Telemetry ? Telemetry.oilTempStatus : "---" }
                        }
                        UI.ListRow {
                            label: "Intake Temp"
                            ValueText { text: Telemetry ? Telemetry.intakeTemp.toFixed(1) + " °C" : "---" }
                        }
                        UI.ListRow {
                            label: "Throttle"
                            ValueText { text: Telemetry ? Telemetry.throttle.toFixed(1) + "%" : "---" }
                        }
                        UI.ListRow {
                            label: "Throttle Source"
                            ValueText { text: Telemetry ? Telemetry.throttleSource : "---" }
                        }
                        UI.ListRow {
                            label: "Voltage"
                            ValueText { text: Telemetry ? Telemetry.voltage.toFixed(2) + " V" : "---" }
                        }
                        UI.ListRow {
                            label: "Engine Load"
                            ValueText { text: Telemetry ? Telemetry.engineLoad.toFixed(1) + "%" : "---" }
                        }
                        UI.ListRow {
                            label: "MAF"
                            ValueText { text: Telemetry ? Telemetry.maf.toFixed(2) + " g/s" : "---" }
                        }
                        UI.ListRow {
                            label: "Gear"
                            ValueText { text: Telemetry ? Telemetry.gear.toString() : "---" }
                        }
                        UI.ListRow {
                            label: "Fuel Level"
                            ValueText { text: Telemetry ? Telemetry.fuelText : "---" }
                        }
                        UI.ListRow {
                            label: "Fuel Raw"
                            ValueText { text: Telemetry ? Telemetry.fuelRawLevel.toFixed(1) + "%" : "---" }
                        }
                        UI.ListRow {
                            label: "Run Time"
                            ValueText { text: Telemetry ? Telemetry.runTime : "---" }
                        }
                        UI.ListRow {
                            label: "DTC Count"
                            divider: false
                            ValueText { text: Telemetry ? Telemetry.dtcCount : "---" }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  DRIVING · Trips
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "Trips"
                        width: bodyCol.width
                        spacing: 0

                        UI.ListRow {
                            label: "Total Distance"
                            sub: "Tap to correct the odometer by hand"
                            clickable: true
                            onClicked: {
                                root.totalKmDraft = Telemetry
                                    ? String(Telemetry.totalKm).replace(" km", "")
                                    : "";
                                root.totalKmEditorOpen = true;
                            }
                            ValueText { text: Telemetry ? Telemetry.totalKm : "0.0 km" }
                        }
                        UI.ListRow {
                            label: "Today"
                            ValueText { text: Telemetry ? Telemetry.todayKm : "0.0 km" }
                        }
                        UI.ListRow {
                            label: "Today Date"
                            sub: "The day key today's distance is filed under"
                            ValueText { text: Telemetry ? Telemetry.todayDate : "" }
                        }
                        UI.ListRow {
                            label: "Day starts at"
                            sub: "When Today resets — 04:00 keeps a late drive home on the same day"
                            clickable: true
                            onClicked: {
                                if (!Settings)
                                    return;
                                var opts = root._dayStartChoices;
                                var i = opts.indexOf(root._dayStartHour);
                                var next = opts[(i + 1) % opts.length];
                                Settings.setString("Trips", "day_start_hour", String(next));
                            }
                            ValueText {
                                text: root._dayStartHour === 0
                                    ? "Midnight"
                                    : (root._dayStartHour < 10 ? "0" : "")
                                      + root._dayStartHour + ":00"
                            }
                        }
                        UI.ListRow {
                            label: "Tank size"
                            sub: "Turns the gauge's % into litres and euros on the Trip page"
                            clickable: true
                            onClicked: root._cycleFuelSetting("tank_litres", 35, [30, 35, 40, 45, 50, 55])
                            ValueText { text: root._fuelSetting("tank_litres", 35) + " L" }
                        }
                        UI.ListRow {
                            label: "Refuel detection"
                            sub: "How far the tank must rise across a stop to ask for the price"
                            clickable: true
                            onClicked: root._cycleFuelSetting("refuel_min_pct", 8, [5, 8, 10, 15, 20])
                            ValueText { text: "+" + root._fuelSetting("refuel_min_pct", 8) + " %" }
                        }
                        UI.ListRow {
                            label: "Trip continues after a stop of"
                            sub: "A shorter stop (fuel, a shop) stays part of the same trip"
                            clickable: true
                            onClicked: root._cycleFuelSetting("trip_merge_min", 10, [0, 5, 10, 15, 20, 30])
                            ValueText {
                                text: root._fuelSetting("trip_merge_min", 10) === 0
                                      ? "Never" : "up to " + root._fuelSetting("trip_merge_min", 10) + " min"
                            }
                        }
                        UI.ListRow {
                            label: "Fuel"
                            sub: Telemetry ? ("Raw " + Telemetry.fuelRawLevel.toFixed(1) + "% · " + Telemetry.fuelCalibrationSummary) : ""
                            ValueText { text: Telemetry ? Telemetry.fuelText : "---" }
                        }
                        // The SoC is a resting-voltage estimate (no shunt on
                        // the car): the full point is the battery's chemistry,
                        // not the highest voltage ever seen — that would be
                        // the alternator.
                        UI.ListRow {
                            label: "Battery charge"
                            sub: Telemetry ? Telemetry.batterySocDetail : ""
                            ValueText { text: Telemetry ? Telemetry.batterySocText : "--%" }
                        }
                        // "Auto" learns where this battery rests when full:
                        // after ≥ 45 min on the alternator and ≥ 4 h of rest
                        // (surface charge gone), the first settled key-on
                        // reading becomes the 100 % point. A hand-picked type
                        // switches the learning off.
                        UI.ListRow {
                            label: "Battery 100 % point"
                            sub: Telemetry
                                 ? (Telemetry.batteryFullMode === "auto"
                                    ? Telemetry.batteryLearnedText
                                    : ("Fixed at " + Telemetry.batteryFullVolts.toFixed(2) + " V"))
                                 : ""
                            UI.Segmented {
                                model: [ { label: "Auto (learn)", value: "auto" },
                                         { label: "Flooded · 12.65 V", value: "12.65" },
                                         { label: "AGM / EFB · 12.85 V", value: "12.85" } ]
                                currentValue: !Telemetry ? "auto"
                                              : (Telemetry.batteryFullMode === "auto" ? "auto"
                                                 : (Telemetry.batteryFullVolts >= 12.75 ? "12.85" : "12.65"))
                                onSelected: function (v) {
                                    if (!Telemetry) return;
                                    if (v === "auto") Telemetry.setBatteryFullMode("auto");
                                    else Telemetry.setBatteryFullVolts(parseFloat(v));
                                }
                            }
                        }
                        // Teach the gauge curve from the car's own dash: tap
                        // how many of its 6 bars are lit right now and the
                        // current raw reading is pinned to that level.
                        UI.ListRow {
                            label: "Dash gauge shows"
                            sub: "Tap the number of bars lit on the instrument cluster"
                            Row {
                                spacing: 6
                                Repeater {
                                    model: 7
                                    UI.PillButton {
                                        text: index === 0 ? "R" : String(index)
                                        enabled: Telemetry ? Telemetry.fuelRawLevel > 0 : false
                                        onClicked: {
                                            if (Telemetry && Telemetry.recordFuelGaugeBars(index))
                                                root._log("Fuel gauge: " + (index === 0 ? "reserve" : index + "/6 bars") + " at raw " + Telemetry.fuelRawLevel.toFixed(1) + "%");
                                        }
                                    }
                                }
                                // Not gated on a live reading: a bad curve has
                                // to be undoable with the engine off.
                                UI.PillButton {
                                    text: "Reset"
                                    onClicked: {
                                        if (Telemetry && Telemetry.clearFuelCalibration())
                                            root._log("Fuel gauge calibration cleared \u2014 back to offset/scale");
                                    }
                                }
                            }
                        }
                        UI.ListRow {
                            label: "Run Time"
                            ValueText { text: Telemetry ? Telemetry.runTime : "N/A" }
                        }
                        UI.ListRow {
                            label: "Distance Since Clear"
                            ValueText { text: Telemetry ? Telemetry.distSinceClear : "N/A" }
                        }
                        UI.ListRow {
                            label: "Warmups Since Clear"
                            ValueText { text: Telemetry ? Telemetry.warmups : "N/A" }
                        }
                        UI.ListRow {
                            label: "Speed"
                            divider: false
                            ValueText { text: Telemetry ? (Telemetry.speed.toFixed(0) + " km/h") : "0 km/h" }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  MEDIA · Music
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "Music"
                        width: bodyCol.width
                        spacing: 0

                        Block {
                            UI.SectionLabel { label: "Output" }
                            Flow {
                                width: parent.width
                                spacing: 8

                                UI.PillButton {
                                    text: "Default"
                                    height: 44
                                    radius: Theme.radiusControl
                                    checked: Music ? !Music.splitAudioEnabled : true
                                    onClicked: if (Music) Music.toggleSplitAudio(false)
                                }

                                Repeater {
                                    model: Music ? Music.splitAudioDeviceList : []
                                    UI.PillButton {
                                        text: modelData.displayName || modelData.name || modelData.id
                                        height: 44
                                        radius: Theme.radiusControl
                                        checked: Music && Music.splitAudioEnabled && Music.splitAudioDeviceId === modelData.id
                                        onClicked: {
                                            if (!Music) return;
                                            var same = Music.splitAudioEnabled && Music.splitAudioDeviceId === modelData.id;
                                            if (same) {
                                                Music.toggleSplitAudio(false);
                                            } else {
                                                Music.setSplitAudioDevice(modelData.id);
                                                Music.toggleSplitAudio(true);
                                            }
                                        }
                                    }
                                }

                                UI.PillButton {
                                    text: "↻"
                                    height: 44
                                    radius: Theme.radiusControl
                                    onClicked: if (Music) Music.refreshSplitAudioDevices()
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Fade Skip"
                            sub: "Crossfade when skipping track"
                            UI.Toggle {
                                checked: Music ? Music.fadeSkipEnabled : false
                                onToggled: function (v) { if (Music) Music.setFadeSkip(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Fade Tracks"
                            sub: "Automatic crossfade near end"
                            UI.Toggle {
                                checked: Music ? Music.fadeTracksEnabled : false
                                onToggled: function (v) { if (Music) Music.setFadeTracks(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Sound Drive"
                            sub: "Global override for all player SoundDrive controls"
                            UI.Toggle {
                                checked: Music ? Music.soundDriveEnabled : false
                                onToggled: function (v) { if (Music) Music.setSoundDriveEnabled(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Method"
                            sub: "How SoundDrive reshapes the mix"
                            visible: Music ? Music.soundDriveEnabled : false
                            UI.Segmented {
                                model: [
                                    { label: "Damuci Sound", value: "damuci" },
                                    { label: "New Method",   value: "realtime" }
                                ]
                                currentValue: Music ? Music.soundDriveMethod : ""
                                clearable: true
                                onSelected: function (v) { if (Music) Music.setSoundDriveMethod(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Mix Mode"
                            sub: "Damuci stem behaviour"
                            visible: Music ? (Music.soundDriveEnabled && Music.soundDriveMethod === "damuci") : false
                            UI.Segmented {
                                model: ["Mode A", "Mode B", "Mode C", "Mode D", "Mode E"]
                                currentValue: Music ? Music.mixMode : ""
                                onSelected: function (v) { if (Music) Music.setMixMode(v); }
                            }
                        }

                        Block {
                            visible: Music ? (Music.soundDriveEnabled && Music.soundDriveMethod === "damuci") : false
                            Text {
                                width: parent.width
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: 14
                                wrapMode: Text.WordWrap
                                text: {
                                    if (!Music) return "";
                                    if (Music.mixMode === "Mode A") return "Speed Wave — Bass, Drums & Melody open with speed. Vocals fade in with throttle (amplitude only). Stereo widens with speed.";
                                    if (Music.mixMode === "Mode B") return "Throttle Pulse — Bass, Drums & Melody react to pedal pressure. Vocals emerge muffled→clear with speed. Gear-ups give a brief bright flash.";
                                    if (Music.mixMode === "Mode C") return "Spatial — Bass, Drums & Melody open with speed. Vocals rise from a distant reverb room with throttle. Braking ducks drums/bass; vocals pan with steering.";
                                    if (Music.mixMode === "Mode D") return "Spatial+Distance Slow — Clone of Mode C with stronger distant character near standstill. Playback is 0.85x at 0 km/h and builds progressively to normal speed.";
                                    if (Music.mixMode === "Mode E") return "Spatial+Distance — Exactly Mode D behavior but without speed slowdown.";
                                    return "";
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Performance"
                            sub: "Safe defaults for Raspberry Pi 4 with optional full-power mode"
                            visible: Music ? (Music.soundDriveEnabled && Music.soundDriveMethod === "realtime") : false
                            UI.Segmented {
                                model: [
                                    { label: "Raspberry Pi 4 (Safe)", value: "rpi4_safe" },
                                    { label: "Full Power",            value: "full_power" }
                                ]
                                currentValue: Music ? Music.soundDrivePerformance : ""
                                onSelected: function (v) { if (Music) Music.setSoundDrivePerformance(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Metadata Cache"
                            sub: "Optional lightweight JSON cache for BPM, beat grid and energy envelopes"
                            visible: Music ? (Music.soundDriveEnabled && Music.soundDriveMethod === "realtime") : false
                            divider: false
                            UI.Toggle {
                                checked: Music ? Music.soundDriveMetadataCache : false
                                onToggled: function (v) { if (Music) Music.setSoundDriveMetadataCache(v); }
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  MEDIA · Lyrics
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "Lyrics"
                        width: bodyCol.width
                        spacing: 0

                        Block {
                            spacing: 4
                            Text {
                                width: parent.width
                                text: "Lyrics display"
                                color: Theme.ink
                                font.family: Theme.fontFamily
                                font.pixelSize: 18
                                font.weight: Font.Bold
                            }
                            Text {
                                width: parent.width
                                text: "How should lyrics be rendered while a song plays?"
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: 14
                                wrapMode: Text.WordWrap
                            }
                        }

                        Repeater {
                            model: [
                                { label: "Smooth Wave",    value: "smooth_wave",
                                  desc: "Proximity magnifier — the centre line is biggest, neighbours taper down. Continuous, never steps." },
                                { label: "Single Focus",   value: "single_focus",
                                  desc: "Only the active line grows. Every other line stays at the same size so rows don't shift." },
                                { label: "Solo Spotlight", value: "solo_spotlight",
                                  desc: "Only the active line is shown — big, centred. The rest of the lyrics are hidden." },
                                { label: "Solo Preview",   value: "solo_preview",
                                  desc: "Active line big and centred, with the next line smaller below as an anticipation." }
                            ]

                            UI.ListRow {
                                label: modelData.label
                                sub: modelData.desc
                                divider: index < 3
                                UI.PillButton {
                                    readonly property bool active: Music && Music.lyricsDisplayMode === modelData.value
                                    text: active ? "Active" : "Use"
                                    checked: active
                                    implicitWidth: 110
                                    height: 44
                                    onClicked: if (Music) Music.setLyricsDisplayMode(modelData.value)
                                }
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  MEDIA · Split Audio
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "Split Audio"
                        width: bodyCol.width
                        spacing: 0

                        UI.ListRow {
                            label: "Split Audio"
                            sub: "Mirror playback to a second output device"
                            UI.Toggle {
                                checked: Music ? Music.splitAudioEnabled : false
                                onToggled: function (v) {
                                    if (!Music) return;
                                    Music.toggleSplitAudio(v);
                                    if (v && !Music.splitAudioConfigured)
                                        Music.refreshSplitAudioDevices();
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Turn split off if an output drops"
                            sub: "Bluetooth leaving mid-track otherwise doubles playback onto the remaining device"
                            UI.Toggle {
                                checked: Music ? Music.splitDropGuard : true
                                onToggled: function (v) { if (Music) Music.setSplitDropGuard(v); }
                            }
                        }

                        Block {
                            UI.SectionLabel { label: "Devices" }
                            Flow {
                                width: parent.width
                                spacing: 8
                                Repeater {
                                    model: Music ? Music.splitAudioDeviceList : []
                                    UI.PillButton {
                                        text: modelData.displayName || modelData.name || modelData.id
                                        height: 44
                                        radius: Theme.radiusControl
                                        checked: Music && Music.splitAudioDeviceId === modelData.id
                                        onClicked: {
                                            if (!Music) return;
                                            Music.setSplitAudioDevice(Music.splitAudioDeviceId === modelData.id ? "" : modelData.id);
                                        }
                                    }
                                }
                                UI.PillButton {
                                    text: "↻ Refresh"
                                    height: 44
                                    radius: Theme.radiusControl
                                    onClicked: if (Music) Music.refreshSplitAudioDevices()
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Split Vol"
                            sub: "Level of the mirrored output"
                            visible: Music ? Music.splitAudioConfigured : false
                            Row {
                                spacing: 10
                                ValueText {
                                    text: (Music ? Music.splitAudioVolume : 0) + "%"
                                    height: 44
                                    verticalAlignment: Text.AlignVCenter
                                }
                                UI.PillButton {
                                    text: "−"; implicitWidth: 64; height: 44; radius: Theme.radiusControl
                                    onClicked: if (Music) Music.setSplitAudioVolume((Music.splitAudioVolume || 0) - 5)
                                }
                                UI.PillButton {
                                    text: "+"; implicitWidth: 64; height: 44; radius: Theme.radiusControl
                                    onClicked: if (Music) Music.setSplitAudioVolume((Music.splitAudioVolume || 0) + 5)
                                }
                            }
                        }

                        // Level is what actually separates front from back on
                        // ordinary stereo music: a hard L/R channel split sends
                        // near-identical content to both ends of the car.
                        UI.ListRow {
                            label: "Front / Rear balance"
                            sub: Music && Music.splitBalance === 0
                                 ? "Even across both outputs"
                                 : (Music && Music.splitBalance < 0
                                    ? Math.abs(Music.splitBalance) + "% toward the front"
                                    : (Music ? Music.splitBalance : 0) + "% toward the rear")
                            visible: Music ? Music.splitBalanceAvailable : false
                            Row {
                                spacing: 10
                                UI.PillButton {
                                    text: "Front"; implicitWidth: 78; height: 44; radius: Theme.radiusControl
                                    onClicked: if (Music) Music.setSplitBalance((Music.splitBalance || 0) - 10)
                                }
                                UI.PillButton {
                                    text: "Even"; implicitWidth: 78; height: 44; radius: Theme.radiusControl
                                    checked: Music && Music.splitBalance === 0
                                    onClicked: if (Music) Music.setSplitBalance(0)
                                }
                                UI.PillButton {
                                    text: "Rear"; implicitWidth: 78; height: 44; radius: Theme.radiusControl
                                    onClicked: if (Music) Music.setSplitBalance((Music.splitBalance || 0) + 10)
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Delay"
                            sub: Music && Music.splitDelayStatus
                                 ? Music.splitDelayStatus
                                 : "Offset between the two outputs · Auto measures it"
                            Row {
                                spacing: 10
                                ValueText {
                                    text: (Music ? Music.splitAudioDelayMs : 0) + " ms"
                                    height: 44
                                    verticalAlignment: Text.AlignVCenter
                                }
                                UI.PillButton {
                                    text: "Auto"; implicitWidth: 80; height: 44; radius: Theme.radiusControl
                                    checked: Music && Music.splitAutoDelay
                                    onClicked: if (Music) Music.probeSplitDelay()
                                }
                                UI.PillButton {
                                    text: "−"; implicitWidth: 64; height: 44; radius: Theme.radiusControl
                                    onClicked: if (Music) Music.setSplitAudioDelay((Music.splitAudioDelayMs || 0) - 25)
                                }
                                UI.PillButton {
                                    text: "+"; implicitWidth: 64; height: 44; radius: Theme.radiusControl
                                    onClicked: if (Music) Music.setSplitAudioDelay((Music.splitAudioDelayMs || 0) + 25)
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Invert delay direction"
                            sub: "Delay the main output instead of the secondary"
                            UI.Toggle {
                                checked: Music ? Music.splitAudioInvertDelay : false
                                onToggled: function (v) { if (Music) Music.setSplitAudioInvertDelay(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Invert Left / Right"
                            sub: "Swap left and right channels on all outputs"
                            UI.Toggle {
                                checked: Music ? Music.splitAudioInvertLR : false
                                onToggled: function (v) { if (Music) Music.setSplitAudioInvertLR(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Split engine sound as well"
                            sub: "Route engine audio to rear speakers, music to front"
                            visible: Music && Music.splitAudioConfigured && Music.splitAudio4chActive
                            divider: false
                            UI.Toggle {
                                checked: Music ? Music.splitEngineSound : false
                                onToggled: function (v) { if (Music) Music.setSplitEngineSound(v); }
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  MEDIA · Audio
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "Audio"
                        width: bodyCol.width
                        spacing: 0

                        Block {
                            Text {
                                width: parent.width
                                text: "Active players expose their current volume here. Drag the slider to change the live output volume."
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: 14
                                wrapMode: Text.WordWrap
                            }
                        }

                        Repeater {
                            model: Music ? Music.audioPlayers : []

                            UI.ListRow {
                                label: modelData.name
                                sub: modelData.group
                                SliderControl {
                                    value: modelData.volume / 150.0
                                    readout: modelData.volume + "%"
                                    onReleased: function (v) {
                                        if (Music) Music.setAudioPlayerVolume(modelData.id, Math.round(v * 150));
                                    }
                                }
                            }
                        }

                        Block {
                            visible: Music ? Music.audioPlayers.length === 0 : true
                            divider: false
                            Text {
                                width: parent.width
                                text: "No active audio players detected."
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: 15
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  MEDIA · Soundboard
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "Soundboard"
                        width: bodyCol.width
                        spacing: 0

                        Block {
                            Text {
                                width: parent.width
                                text: "Assign favourite sounds to quick slots."
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: 14
                                wrapMode: Text.WordWrap
                            }
                        }

                        Repeater {
                            model: 5

                            UI.ListRow {
                                readonly property string slotName: root._sbSlotName(index)
                                label: "Slot " + (index + 1)
                                sub: slotName !== "" ? slotName : "Empty"
                                clickable: true
                                onClicked: {
                                    root.sbPickerSlot = index;
                                    root.sbPickerFiles = false;
                                    sbPicker.opened = true;
                                }
                                Row {
                                    spacing: 10
                                    UI.PillButton {
                                        text: "Clear"
                                        visible: parent.parent.slotName !== ""
                                        width: visible ? implicitWidth : 0
                                        implicitWidth: 96
                                        height: 44
                                        variant: "ghost"
                                        onClicked: if (Soundboard) Soundboard.clearFavorite(index)
                                    }
                                    ValueChevron { value: parent.parent.slotName !== "" ? "Change" : "Choose" }
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Volume"
                            sub: "Soundboard playback level"
                            divider: false
                            SliderControl {
                                value: Soundboard ? Soundboard.volume : 0.5
                                readout: (Soundboard ? Math.round(Soundboard.volume * 100) : 50) + "%"
                                onReleased: function (v) { if (Soundboard) Soundboard.setVolume(v); }
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  TOOLS · Obd
                    // ═══════════════════════════════════════════════════
                    Column {
                        id: obdSection
                        visible: root.activeSection === "Obd"
                        width: bodyCol.width
                        spacing: 0

                        // Page-gated polling: only ask the bridge to refresh CAN
                        // stats while this section is on screen, and only pay for
                        // diagnostics-only PIDs while someone is looking at them.
                        // Pi 4B budget — do not remove.
                        onVisibleChanged: {
                            if (!OBD) return;
                            if (visible) OBD.activateCanStatsPolling();
                            else         OBD.deactivateCanStatsPolling();
                            OBD.setDiagnosticsVisible(visible);
                        }
                        Component.onCompleted: {
                            if (visible && OBD) {
                                OBD.activateCanStatsPolling();
                                OBD.setDiagnosticsVisible(true);
                            }
                        }

                        readonly property bool isCan: OBD && OBD.protocol === "can"
                        property int canBackendRev: 0
                        readonly property string canBackend: {
                            var _r = canBackendRev;
                            return Settings ? (Settings.getValue("OBD", "can_backend") || "stn") : "stn";
                        }
                        readonly property bool isStn: canBackend !== "python-can"

                        Block {
                            Text {
                                width: parent.width
                                text: "Connection and adapter controls."
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: 14
                            }
                        }

                        UI.ListRow {
                            label: "Protocol"
                            sub: "Adapter family"
                            UI.Segmented {
                                model: [
                                    { label: "OBD2",    value: "obd2" },
                                    { label: "CAN-BUS", value: "can" }
                                ]
                                currentValue: OBD ? OBD.protocol : ""
                                clearable: true
                                onSelected: function (v) { if (OBD) OBD.setProtocol(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Status"
                            sub: OBD && OBD.connecting ? "Handshaking…" : "Adapter link"
                            ValueText {
                                text: OBD ? (OBD.connected ? "Connected" : "Disconnected") : "Unknown"
                                color: (OBD && OBD.connected) ? Theme.okSoft : Theme.dangerSoft
                            }
                        }

                        UI.ListRow {
                            label: "Port"
                            sub: "Serial device"
                            visible: !obdSection.isCan
                            ValueText { text: OBD ? (OBD.port || "—") : "—" }
                        }

                        UI.ListRow {
                            label: "Baud"
                            sub: "Serial speed"
                            visible: !obdSection.isCan
                            ValueText { text: OBD ? OBD.baud.toString() : "—" }
                        }

                        // CAN-BUS on the car's adapter (an STN1170) is the same
                        // serial link as OBD2 plus a raw monitor riding it —
                        // no separate driver. python-can (slcan/socketcan) stays
                        // selectable for other hardware.
                        UI.ListRow {
                            label: "CAN backend"
                            sub: obdSection.isStn
                                 ? "STN1170 / vLinker FS: PIDs + raw monitor on one adapter — the connection that worked in the car"
                                 : "python-can driver (slcan / socketcan hardware)"
                            visible: obdSection.isCan
                            UI.Segmented {
                                model: [ { label: "STN adapter", value: "stn" }, { label: "python-can", value: "python-can" } ]
                                currentValue: obdSection.canBackend
                                onSelected: function (v) { if (Settings) { Settings.setValue("OBD", "can_backend", v); obdSection.canBackendRev++; } }
                            }
                        }

                        // Status only — the debugger itself opens from
                        // Tools > Debugger, with the other developer tools.
                        UI.ListRow {
                            label: "Monitor"
                            sub: Can ? Can.statusText : ""
                            visible: obdSection.isCan && obdSection.isStn
                            ValueText {
                                text: Can && Can.monitoring ? "Monitoring"
                                      : (Can && Can.connected ? "Connected" : "Off")
                            }
                        }

                        UI.ListRow {
                            label: "CAN Interface"
                            sub: "Driver backend"
                            visible: obdSection.isCan && !obdSection.isStn
                            UI.Segmented {
                                model: ["slcan", "socketcan"]
                                currentValue: OBD ? OBD.canInterface : ""
                                onSelected: function (v) { if (OBD) OBD.setCanInterface(v); }
                            }
                        }

                        UI.ListRow {
                            label: "CAN Channel"
                            sub: "e.g. /dev/ttyUSB0 or can0"
                            visible: obdSection.isCan && !obdSection.isStn
                            Rectangle {
                                width: 260
                                height: 44
                                radius: Theme.radiusControl
                                color: Theme.surfaceRaised

                                TextInput {
                                    id: canChannelInput
                                    anchors.fill: parent
                                    anchors.leftMargin: 12
                                    anchors.rightMargin: 12
                                    color: Theme.ink
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 15
                                    verticalAlignment: TextInput.AlignVCenter
                                    clip: true
                                    text: OBD ? OBD.canChannel : ""
                                    onEditingFinished: if (OBD) OBD.setCanChannel(text)

                                    Text {
                                        visible: canChannelInput.text === ""
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: "e.g. /dev/ttyUSB0 or can0"
                                        color: Theme.inkGhost
                                        font: canChannelInput.font
                                    }
                                }
                            }
                        }

                        UI.ListRow {
                            label: "CAN Bitrate (bps)"
                            sub: "Bus speed"
                            visible: obdSection.isCan && !obdSection.isStn
                            UI.Segmented {
                                model: [
                                    { label: "125k",  value: 125000 },
                                    { label: "250k",  value: 250000 },
                                    { label: "500k",  value: 500000 },
                                    { label: "1000k", value: 1000000 }
                                ]
                                currentValue: OBD ? OBD.canBitrate : 0
                                onSelected: function (v) { if (OBD) OBD.setCanBitrate(v); }
                            }
                        }

                        // Live RX stats — the difference between "bus open" and
                        // "bus open and actually receiving".
                        UI.ListRow {
                            label: "CAN traffic"
                            visible: obdSection.isCan
                            sub: {
                                if (!OBD || !OBD.connected) return "Not connected";
                                if (OBD.canIsReceiving)
                                    return OBD.canFramesPerSec.toFixed(0) + " fps"
                                         + " · " + OBD.canUniqueIds + " unique IDs"
                                         + (OBD.canLastFrameAgeMs >= 0
                                            ? " · last frame " + OBD.canLastFrameAgeMs + " ms ago"
                                            : "");
                                return "Bus open, no frames yet"
                                     + (OBD.canLastFrameAgeMs > 0
                                        ? " (" + Math.floor(OBD.canLastFrameAgeMs / 1000) + " s)"
                                        : "");
                            }
                            Row {
                                spacing: 12

                                Rectangle {
                                    width: 14
                                    height: 14
                                    radius: 7
                                    y: 15
                                    color: {
                                        if (!OBD || !OBD.connected) return Theme.danger;
                                        if (OBD.canIsReceiving) return Theme.ok;
                                        return Theme.warn;
                                    }
                                }

                                UI.PillButton {
                                    text: "Listen 3 s"
                                    implicitWidth: 130
                                    height: 44
                                    radius: Theme.radiusControl
                                    enabled: OBD && OBD.connected && OBD.protocol === "can"
                                    onClicked: if (OBD) OBD.probeCanTraffic()
                                }
                            }
                        }

                        Block {
                            visible: obdSection.isCan && OBD && (OBD.canLastError !== "" || OBD.canProbeResult !== "")
                            spacing: 6
                            Text {
                                width: parent.width
                                visible: OBD && OBD.canLastError !== ""
                                text: OBD ? OBD.canLastError : ""
                                color: Theme.warn
                                font.family: Theme.fontFamily
                                font.pixelSize: 14
                                wrapMode: Text.WordWrap
                            }
                            Text {
                                width: parent.width
                                visible: OBD && OBD.canProbeResult !== ""
                                text: OBD ? OBD.canProbeResult : ""
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: 14
                                wrapMode: Text.WordWrap
                            }
                        }

                        Block {
                            visible: OBD && OBD.connectionErrorText !== ""
                            Text {
                                width: parent.width
                                text: OBD ? OBD.connectionErrorText : ""
                                color: Theme.dangerSoft
                                font.family: Theme.fontFamily
                                font.pixelSize: 14
                                wrapMode: Text.WordWrap
                            }
                        }

                        UI.ListRow {
                            label: "Port Selection"
                            sub: OBD && OBD.availablePorts && OBD.availablePorts.length > 0
                                 ? (OBD.availablePorts.length + " port(s) found")
                                 : "No ports scanned yet"
                            UI.PillButton {
                                text: "Scan Ports"
                                implicitWidth: 140
                                height: 44
                                onClicked: if (OBD) OBD.scanPorts()
                            }
                        }

                        Block {
                            visible: OBD && OBD.availablePorts && OBD.availablePorts.length > 0
                            divider: false
                            Flow {
                                width: parent.width
                                spacing: 8
                                Repeater {
                                    model: OBD ? OBD.availablePorts : []
                                    UI.PillButton {
                                        text: modelData
                                        height: 44
                                        radius: Theme.radiusControl
                                        checked: OBD && OBD.port === modelData
                                        onClicked: if (OBD) OBD.setPort(modelData)
                                    }
                                }
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  TOOLS · Diagnose
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "Diagnose"
                        width: bodyCol.width
                        spacing: 0

                        Block {
                            Text {
                                width: parent.width
                                text: "Advanced read-only diagnostic commands."
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: 14
                            }
                        }

                        UI.ListRow {
                            label: "OBD Status"
                            ValueText {
                                text: OBD ? (OBD.connected ? "Connected" : "Disconnected") : "Unknown"
                                color: (OBD && OBD.connected) ? Theme.okSoft : Theme.dangerSoft
                            }
                        }

                        Block {
                            UI.SectionLabel { label: "Commands" }
                            Grid {
                                width: parent.width
                                columns: 2
                                spacing: 10

                                Repeater {
                                    model: [
                                        "Adapter Info", "Read VIN",
                                        "Stored DTCs", "Pending DTCs",
                                        "Permanent DTCs", "Freeze-frame",
                                        "Supported PIDs", "One-shot Snapshot",
                                        "Sensor Health Pack", "Performance Snapshot"
                                    ]

                                    UI.PillButton {
                                        width: (bodyCol.width - 10) / 2
                                        height: 50
                                        radius: Theme.radiusControl
                                        text: modelData
                                        onClicked: root._log(OBD ? OBD.runDiagnostic(modelData) : "OBD bridge unavailable.")
                                    }
                                }
                            }
                        }

                        Block {
                            UI.SectionLabel { label: "Manual PID" }
                            Row {
                                width: parent.width
                                spacing: 10

                                Rectangle {
                                    width: parent.width - pidRunBtn.width - 10
                                    height: 50
                                    radius: Theme.radiusControl
                                    color: Theme.surfaceRaised

                                    TextInput {
                                        id: pidInput
                                        anchors.fill: parent
                                        anchors.leftMargin: 14
                                        anchors.rightMargin: 14
                                        color: Theme.ink
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 15
                                        verticalAlignment: TextInput.AlignVCenter
                                        clip: true

                                        Text {
                                            visible: pidInput.text === ""
                                            anchors.verticalCenter: parent.verticalCenter
                                            text: "Manual PID (e.g. 010C)"
                                            color: Theme.inkGhost
                                            font: pidInput.font
                                        }
                                    }
                                }

                                UI.PillButton {
                                    id: pidRunBtn
                                    text: "Run PID"
                                    implicitWidth: 130
                                    height: 50
                                    variant: "accent"
                                    onClicked: root._log(OBD ? OBD.runManualPid(pidInput.text) : "OBD bridge unavailable.")
                                }
                            }
                        }

                        Block {
                            divider: false
                            UI.SectionLabel { label: "Log" }
                            Rectangle {
                                width: parent.width
                                height: 240
                                radius: Theme.radiusControl
                                color: Theme.surfaceRaised

                                Flickable {
                                    id: diagFlick
                                    anchors.fill: parent
                                    anchors.margins: 12
                                    clip: true
                                    contentWidth: width
                                    contentHeight: diagLog.implicitHeight
                                    boundsBehavior: Flickable.StopAtBounds

                                    Text {
                                        id: diagLog
                                        width: diagFlick.width
                                        text: "Diagnostic log..."
                                        color: Theme.inkDim
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 13
                                        wrapMode: Text.Wrap
                                    }
                                }
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  TOOLS · Debugger
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "Debugger"
                        width: bodyCol.width
                        spacing: 0

                        // Developer tools. The simulator and engine test are
                        // separate windows (bench only — under labwc they open
                        // behind the app); the two sheets are in-app.
                        Block {
                            UI.SectionLabel { label: "Tools" }
                            Flow {
                                width: parent.width
                                spacing: 10
                                UI.PillButton {
                                    text: "Lights Simulator"; height: 50
                                    variant: "accent"
                                    onClicked: lightsSheet.opened = true
                                }
                                UI.PillButton {
                                    text: "CAN Debugger"; height: 50
                                    onClicked: canSheet.opened = true
                                }
                                UI.PillButton {
                                    text: "OBD Simulator"; height: 50
                                    onClicked: if (OBD) OBD.openSimulator()
                                }
                                UI.PillButton {
                                    text: "Engine Test"; height: 50
                                    onClicked: if (OBD) OBD.openEngineTest()
                                }
                            }
                        }

                        // ── Trip logging ───────────────────────────────
                        // Silent per-subsystem recorder (bridges/triplog_bridge.py).
                        // Tick channels, press Start; a background thread writes a
                        // JSON-lines file you read back as a report or pull to the
                        // dev PC. Inert until Start — costs nothing otherwise.
                        Block {
                            UI.SectionLabel { label: "Trip logging" }
                            Text {
                                width: parent.width
                                text: "Record chosen subsystems to a file while driving, read it back as a report. Low CPU; nothing runs until you press Start."
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: 13
                                wrapMode: Text.WordWrap
                            }
                        }

                        Repeater {
                            model: TripLog ? TripLog.channels : []
                            UI.ListRow {
                                label: modelData.label
                                minHeight: 52
                                UI.Toggle {
                                    checked: modelData.active
                                    enabled: !(TripLog && TripLog.recording)
                                    onToggled: function (v) { if (TripLog) TripLog.setChannel(modelData.key, v); }
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Telemetry sample rate"
                            sub: "How often the telemetry trace is sampled (the other channels log on events)"
                            enabled: !(TripLog && TripLog.recording)
                            UI.Segmented {
                                model: [ { label: "1 Hz", value: 1 }, { label: "2 Hz", value: 2 }, { label: "5 Hz", value: 5 } ]
                                currentValue: TripLog ? TripLog.sampleHz : 2
                                onSelected: function (v) { if (TripLog) TripLog.setSampleHz(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Recording"
                            sub: {
                                if (!TripLog) return "";
                                if (TripLog.recording) return TripLog.sessionText + (TripLog.capped ? "  ·  size cap reached" : "");
                                if (TripLog.activeCount === 0) return "Tick at least one channel above";
                                return TripLog.activeCount + " channel" + (TripLog.activeCount === 1 ? "" : "s") + " selected";
                            }
                            UI.PillButton {
                                text: TripLog && TripLog.recording ? "Stop" : "Start"
                                variant: TripLog && TripLog.recording ? "danger" : "accent"
                                implicitWidth: 130
                                height: 44
                                enabled: TripLog ? (TripLog.recording || TripLog.activeCount > 0) : false
                                onClicked: {
                                    if (!TripLog) return;
                                    if (TripLog.recording) TripLog.stop(); else TripLog.start();
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Saved logs"
                            sub: TripLog && TripLog.sessions.length > 0
                                 ? TripLog.sessions.length + " session" + (TripLog.sessions.length === 1 ? "" : "s")
                                 : "No recordings yet"
                            divider: TripLog && TripLog.sessions.length > 0
                            UI.PillButton {
                                text: "Refresh"; implicitWidth: 110; height: 40
                                onClicked: if (TripLog) TripLog.refreshSessions()
                            }
                        }

                        Repeater {
                            model: TripLog ? TripLog.sessions : []
                            UI.ListRow {
                                label: modelData.date
                                sub: modelData.sizeText + "  ·  " + modelData.name
                                clickable: true
                                divider: index < (TripLog ? TripLog.sessions.length - 1 : 0)
                                onClicked: { tripReportSheet.open(modelData.name); }
                                Row {
                                    spacing: 8
                                    UI.PillButton {
                                        text: "Report"; implicitWidth: 100; height: 40
                                        onClicked: tripReportSheet.open(modelData.name)
                                    }
                                    UI.PillButton {
                                        text: "Delete"; variant: "outline"; implicitWidth: 96; height: 40
                                        onClicked: if (TripLog) TripLog.deleteSession(modelData.name)
                                    }
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Mpris Metadata"
                            sub: "Use debug metadata mode for MPRIS bridge logging"
                            UI.Toggle {
                                checked: root.debuggerMprisEnabled
                                onToggled: function (v) {
                                    root.debuggerMprisEnabled = v;
                                    if (Settings) Settings.setValue("Debug", "mpris_debug", v ? "1" : "0");
                                }
                            }
                        }

                        Block {
                            visible: root.debuggerMprisEnabled
                            divider: false
                            UI.SectionLabel { label: "MPRIS Live Metadata" }
                        }

                        UI.ListRow {
                            label: "Title"
                            visible: root.debuggerMprisEnabled
                            ValueText { text: Music ? Music.title : "" }
                        }
                        UI.ListRow {
                            label: "Artist"
                            visible: root.debuggerMprisEnabled
                            ValueText { text: Music ? Music.artist : "" }
                        }
                        UI.ListRow {
                            label: "Position"
                            visible: root.debuggerMprisEnabled
                            ValueText { text: Music ? Music.positionText : "" }
                        }
                        UI.ListRow {
                            label: "Duration"
                            visible: root.debuggerMprisEnabled
                            ValueText { text: Music ? Music.durationText : "" }
                        }
                        UI.ListRow {
                            label: "Status"
                            visible: root.debuggerMprisEnabled
                            ValueText { text: Music ? (Music.playing ? "Playing" : "Paused/Stopped") : "—" }
                        }
                        UI.ListRow {
                            label: "Art URL"
                            visible: root.debuggerMprisEnabled
                            ValueText { text: Music ? Music.albumArtUrl : "" }
                        }

                        Block {
                            visible: root.debuggerMprisEnabled
                            Text {
                                width: parent.width
                                text: Settings ? (Settings.mprisCommandLog() || "No incoming MPRIS commands logged yet.") : ""
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: 13
                                wrapMode: Text.Wrap
                            }
                        }

                        UI.ListRow {
                            label: "Sound Log"
                            sub: "Write engine-sound decisions to the runtime log"
                            UI.Toggle {
                                checked: Modding ? Modding.soundLogEnabled : false
                                onToggled: function (v) { if (Modding) Modding.setSoundLog(v); }
                            }
                        }

                        UI.ListRow {
                            label: "Test Mode"
                            sub: "Play a synthetic drive through the engine sound (needs a preset other than Quiet; resets at restart)"
                            divider: false
                            UI.Toggle {
                                checked: Modding ? Modding.testModeEnabled : false
                                onToggled: function (v) { if (Modding) Modding.setTestMode(v); }
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  HARDWARE · About
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "About"
                        width: bodyCol.width
                        spacing: 0

                        UI.ListRow { label: "App";      ValueText { text: "LaLaDash" } }
                        UI.ListRow { label: "Version";  ValueText { text: "QML 1.0" } }
                        UI.ListRow { label: "Platform"; ValueText { text: Qt.platform.os } }

                        Block { UI.SectionLabel { label: "LaLaDash Process" } }

                        UI.ListRow { label: "CPU Usage"; ValueText { text: root.aboutCpuUsage } }
                        UI.ListRow { label: "RAM Usage"; ValueText { text: root.aboutAppRam } }
                        UI.ListRow { label: "Threads";   ValueText { text: root.aboutThreads } }

                        Block { UI.SectionLabel { label: "Hardware" } }

                        UI.ListRow { label: "System CPU";      ValueText { text: root.aboutSysCpu } }
                        UI.ListRow { label: "CPU Temp";        ValueText { text: root.aboutCpuTemp } }
                        UI.ListRow { label: "GPU";             ValueText { text: root.aboutGpuName } }
                        UI.ListRow { label: "GPU Usage";       ValueText { text: root.aboutGpuUsage } }
                        UI.ListRow { label: "GPU Load";        ValueText { text: root.aboutGpuLoad } }
                        UI.ListRow { label: "GPU Memory (CMA) Free"; ValueText { text: root.aboutCma } }
                        UI.ListRow { label: "GPU Temp";        ValueText { text: root.aboutGpuTemp } }
                        UI.ListRow { label: "Total RAM Used";  ValueText { text: root.aboutRamUsed } }
                        UI.ListRow { label: "Total RAM Free";  ValueText { text: root.aboutRamFree } }
                        UI.ListRow { label: "Power Supply";    ValueText { text: root.aboutPower; color: root.aboutPower.indexOf("NOW") === 0 ? Theme.danger : Theme.ink } }

                        Block { UI.SectionLabel { label: "System" } }

                        UI.ListRow { label: "Uptime";       ValueText { text: root.aboutUptime } }
                        UI.ListRow {
                            label: "Device Model"
                            divider: false
                            ValueText { text: root.aboutModel }
                        }

                        // Gated on the section being open — a 3 s poll behind a
                        // hidden page is exactly the kind of thing that costs
                        // idle CPU on the Pi.
                        Timer {
                            interval: 3000
                            running: root.activeSection === "About"
                            repeat: true
                            onTriggered: root.refreshAbout()
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  HARDWARE · Services
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "Services"
                        width: bodyCol.width
                        spacing: 0

                        Block {
                            Text {
                                width: parent.width
                                text: "Extra pages. Off = the section, the page and anything it runs are gone; on = its own settings decide."
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: 14
                                wrapMode: Text.WordWrap
                            }
                        }

                        Repeater {
                            model: [
                                { svc: "dashcam",       label: "70mai dashcam",   sub: "Settings › 70mai, the 70mai page and its Wi-Fi link" },
                                { svc: "actioncam",     label: "Action cam",      sub: "Settings › Action cam, the GoPro / Insta360 page, its link and stream" },
                                { svc: "airphone",      label: "AirPhone",        sub: "Settings › AirPhone, the AirPlay page and the rpiplay receiver" },
                                { svc: "steering_keys", label: "Steering keys",   sub: "Settings › Steering Keys and the Bluetooth button listener" }
                            ]
                            UI.ListRow {
                                label: modelData.label
                                sub: modelData.sub
                                UI.Toggle {
                                    checked: (Settings ? Settings.servicesRev : 0, Settings ? Settings.serviceOn(modelData.svc) : true)
                                    onToggled: function (v) { if (Settings) Settings.setServiceOn(modelData.svc, v); }
                                }
                            }
                        }

                        Block {
                            Text {
                                width: parent.width
                                text: "Background services and runtime helpers."
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: 14
                                wrapMode: Text.WordWrap
                            }
                        }

                        UI.ListRow {
                            label: "Restart Audio"
                            sub: "Bounce the audio daemon and reopen the output"
                            UI.PillButton {
                                text: "Restart"
                                implicitWidth: 130
                                height: 44
                                onClicked: confirmSheet.open("Restart Audio?", function () {
                                    if (Settings) Settings.restartAudio();
                                })
                            }
                        }

                        UI.ListRow {
                            label: "Restart MPRIS"
                            sub: "Re-own the D-Bus name and relaunch mpris-proxy"
                            UI.PillButton {
                                text: "Restart"
                                implicitWidth: 130
                                height: 44
                                onClicked: confirmSheet.open("Restart MPRIS Bridge?", function () {
                                    if (Settings) Settings.restartMprisBridge();
                                })
                            }
                        }

                        UI.ListRow {
                            label: "Delete Cache"
                            sub: "Drop asset, catalog and artwork caches"
                            divider: false
                            UI.PillButton {
                                text: "Delete"
                                variant: "danger"
                                implicitWidth: 130
                                height: 44
                                onClicked: confirmSheet.open("Delete all caches?", function () {
                                    if (Settings) Settings.clearCaches();
                                })
                            }
                        }
                    }

                    // ═══════════════════════════════════════════════════
                    //  HARDWARE · System
                    // ═══════════════════════════════════════════════════
                    Column {
                        visible: root.activeSection === "System"
                        width: bodyCol.width
                        spacing: 0

                        Block {
                            Text {
                                width: parent.width
                                text: "Application and power actions."
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: 14
                                wrapMode: Text.WordWrap
                            }
                        }

                        UI.ListRow {
                            label: "Close App"
                            sub: "Close leaves a blank screen until the next boot; Close → Desktop brings the Raspberry Pi desktop up instead (a LaLaDash icon there comes back here)"
                            Row {
                                spacing: 8
                                UI.PillButton {
                                    text: "Close"
                                    implicitWidth: 110
                                    height: 44
                                    onClicked: confirmSheet.open("Close App: Are you sure?", function () {
                                        if (Settings) Settings.quitApp();
                                    })
                                }
                                UI.PillButton {
                                    text: "Close → Desktop"
                                    implicitWidth: 160
                                    height: 44
                                    variant: "accent"
                                    onClicked: confirmSheet.open("Close and show the Raspberry Pi desktop?", function () {
                                        if (Settings) Settings.quitToDesktop();
                                    })
                                }
                            }
                        }

                        UI.ListRow {
                            label: "Restart App"
                            sub: "Relaunch through the service (splash, then the app) — the only way a style change takes effect"
                            UI.PillButton {
                                text: "Restart"
                                implicitWidth: 130
                                height: 44
                                onClicked: confirmSheet.open("Restart App: Are you sure?", function () {
                                    if (Settings) Settings.restartApp();
                                })
                            }
                        }

                        UI.ListRow {
                            label: "Reboot"
                            sub: "Restart the whole device"
                            UI.PillButton {
                                text: "Reboot"
                                variant: "danger"
                                implicitWidth: 130
                                height: 44
                                onClicked: confirmSheet.open("Reboot: Restart the device?", function () {
                                    if (Settings) Settings.rebootSystem();
                                })
                            }
                        }

                        UI.ListRow {
                            label: "Shutdown"
                            sub: "Power the device off"
                            UI.PillButton {
                                text: "Shutdown"
                                variant: "danger"
                                implicitWidth: 130
                                height: 44
                                onClicked: confirmSheet.open("Shutdown: Power off the device?", function () {
                                    if (Settings) Settings.shutdownSystem();
                                })
                            }
                        }

                        // Update Raspberry Pi: apt update → full-upgrade →
                        // autoclean, in order, on a background worker. The bar
                        // is bound to Settings.piUpdateProgress, which lives in
                        // the bridge — so it keeps moving if you leave and come
                        // back, and the status strip shows it from any page.
                        UI.ListRow {
                            label: "Update Raspberry Pi"
                            divider: false
                            sub: {
                                if (!Settings) return "";
                                if (!Settings.piUpdateAvailable) return "apt not available on this device";
                                if (Settings.piUpdateRunning) return "Runs in the background — you can leave this page";
                                if (Settings.piUpdateResult === "ok") return "Up to date. A reboot may be needed for a new kernel.";
                                if (Settings.piUpdateResult !== "") return Settings.piUpdateResult;
                                return "Runs apt update, full-upgrade and autoclean in order";
                            }
                            UI.PillButton {
                                text: Settings && Settings.piUpdateRunning ? "Updating…"
                                      : (Settings && Settings.piUpdateResult !== "" ? "Run again" : "Update")
                                variant: "accent"
                                implicitWidth: 140
                                height: 44
                                busy: Settings ? Settings.piUpdateRunning : false
                                enabled: Settings ? (Settings.piUpdateAvailable && !Settings.piUpdateRunning) : false
                                onClicked: confirmSheet.open(
                                    "Update the Raspberry Pi now? This downloads and installs all system updates and can take several minutes.",
                                    function () { if (Settings) Settings.startPiUpdate(); })
                            }
                        }

                        // Progress + last line, shown while running and after.
                        Item {
                            width: parent.width
                            visible: Settings && (Settings.piUpdateRunning || Settings.piUpdateResult !== "")
                            height: visible ? updCol.implicitHeight + 20 : 0

                            Column {
                                id: updCol
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.leftMargin: 16
                                anchors.rightMargin: 16
                                anchors.top: parent.top
                                anchors.topMargin: 4
                                spacing: 8

                                Row {
                                    width: parent.width
                                    Text {
                                        width: parent.width - pctText.width
                                        text: Settings ? (Settings.piUpdateStage || "") : ""
                                        color: Settings && Settings.piUpdateResult !== "" && Settings.piUpdateResult !== "ok"
                                               ? Theme.dangerSoft : Theme.ink
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 14
                                        font.weight: Font.DemiBold
                                        elide: Text.ElideRight
                                    }
                                    Text {
                                        id: pctText
                                        text: Settings ? Math.round(Settings.piUpdateProgress * 100) + " %" : ""
                                        color: Theme.inkFaint
                                        font.family: Theme.fontFamily
                                        font.pixelSize: 14
                                        font.weight: Font.DemiBold
                                    }
                                }

                                Rectangle {
                                    width: parent.width
                                    height: 10
                                    radius: 5
                                    color: Theme.surfaceRaised
                                    Rectangle {
                                        width: parent.width * Math.max(0, Math.min(1, Settings ? Settings.piUpdateProgress : 0))
                                        height: parent.height
                                        radius: 5
                                        color: Settings && Settings.piUpdateResult !== "" && Settings.piUpdateResult !== "ok"
                                               ? Theme.danger : Theme.ok
                                        Behavior on width { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
                                    }
                                }

                                Text {
                                    width: parent.width
                                    visible: text !== ""
                                    text: Settings ? (Settings.piUpdateLine || "") : ""
                                    color: Theme.inkFaint
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 12
                                    elide: Text.ElideRight
                                }

                                UI.PillButton {
                                    text: "Dismiss"
                                    variant: "outline"
                                    implicitWidth: 120
                                    height: 38
                                    visible: Settings && !Settings.piUpdateRunning && Settings.piUpdateResult !== ""
                                    onClicked: if (Settings) Settings.dismissPiUpdate()
                                }
                            }
                        }
                    }

                    // ── nothing selected ───────────────────────────────
                    Item {
                        visible: root.activeSection === ""
                        width: bodyCol.width
                        height: 220

                        Text {
                            anchors.centerIn: parent
                            text: "Select a menu on the left."
                            color: Theme.inkFaint
                            font.family: Theme.fontFamily
                            font.pixelSize: Theme.fontSize("pages_secondary_text")
                        }
                    }
                }
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════
    //  Overlays
    // ═══════════════════════════════════════════════════════════════════
    Sheets.WifiSheet {
        id: wifiSheet
        z: 60
        onClosed: opened = false
    }

    Sheets.CanDebuggerSheet {
        id: canSheet
        objectName: "canSheet"   // reachable from the debug probe: `eval @canSheet opened = true`
        z: 60
        onClosed: opened = false
    }

    // Camera-network picker for Settings › 70mai: Auto (the strongest
    // 70mai_* in range) or one pinned SSID from the last scan.
    UI.Sheet {
        id: dashcamNetSheet
        objectName: "dashcamNetSheet"
        z: 60
        title: "Camera network"
        subtitle: Dashcam && Dashcam.busy ? "Scanning…" : "Networks starting with 70mai on " + (Dashcam ? Dashcam.iface : "")
        onClosed: opened = false

        Column {
            width: parent.width
            spacing: 8

            UI.ListRow {
                label: "Auto"
                sub: "Whatever 70mai_* is strongest when connecting"
                clickable: true
                onClicked: { if (Dashcam) Dashcam.setSsid(""); dashcamNetSheet.opened = false; }
                ValueText { text: Dashcam && !Dashcam.ssidSetting ? "✓" : "" ; color: Theme.accent }
            }

            Repeater {
                model: Dashcam ? Dashcam.detectedNetworks : []
                UI.ListRow {
                    label: modelData.ssid
                    sub: "Signal " + modelData.signal + " %"
                    clickable: true
                    onClicked: { if (Dashcam) Dashcam.setSsid(modelData.ssid); dashcamNetSheet.opened = false; }
                    ValueText { text: Dashcam && Dashcam.ssidSetting === modelData.ssid ? "✓" : ""; color: Theme.accent }
                }
            }

            Text {
                visible: !Dashcam || Dashcam.detectedNetworks.length === 0
                width: parent.width
                text: Dashcam && Dashcam.busy ? "" : "No 70mai network in range — is the camera on and its Wi-Fi enabled?"
                color: Theme.inkFaint
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSize("settings_secondary_text")
                wrapMode: Text.Wrap
            }

            UI.PillButton {
                text: "Scan again"
                height: 44
                enabled: Dashcam && Dashcam.enabled && !Dashcam.busy
                onClicked: if (Dashcam) Dashcam.detect()
            }
        }
    }

    // Camera-network picker for Settings › Action cam (Wi-Fi link).
    UI.Sheet {
        id: acamNetSheet
        objectName: "acamNetSheet"
        z: 60
        title: "Camera network"
        subtitle: ActionCam && ActionCam.busy ? "Scanning…" : "Networks on " + (ActionCam ? ActionCam.iface : "")
        onClosed: opened = false

        Column {
            width: parent.width
            spacing: 8

            UI.ListRow {
                label: "Auto"
                sub: "Whatever camera network is strongest when connecting"
                clickable: true
                onClicked: { if (ActionCam) ActionCam.setSsid(""); acamNetSheet.opened = false; }
                ValueText { text: ActionCam && !ActionCam.ssidSetting ? "✓" : "" ; color: Theme.accent }
            }

            Repeater {
                model: ActionCam ? ActionCam.detectedNetworks : []
                UI.ListRow {
                    label: modelData.ssid
                    sub: "Signal " + modelData.signal + " %"
                    clickable: true
                    onClicked: { if (ActionCam) ActionCam.setSsid(modelData.ssid); acamNetSheet.opened = false; }
                    ValueText { text: ActionCam && ActionCam.ssidSetting === modelData.ssid ? "✓" : ""; color: Theme.accent }
                }
            }

            Text {
                visible: !ActionCam || ActionCam.detectedNetworks.length === 0
                width: parent.width
                text: ActionCam && ActionCam.busy ? "" : "No camera network in range — is its Wi-Fi on?"
                color: Theme.inkFaint
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSize("settings_secondary_text")
                wrapMode: Text.Wrap
            }

            UI.PillButton {
                text: "Scan again"
                height: 44
                enabled: ActionCam && ActionCam.enabled && !ActionCam.busy
                onClicked: if (ActionCam) ActionCam.detect()
            }
        }
    }

    Sheets.TripReportSheet {
        id: tripReportSheet
        objectName: "tripReportSheet"
        z: 60
        onClosed: opened = false
    }

    Sheets.LightsSimSheet {
        id: lightsSheet
        objectName: "lightsSheet"   // `eval @lightsSheet opened = true`
        z: 60
        onClosed: opened = false
    }

    // Settings.wifiSSID()/wifiEnabled() are slots, not notifying properties,
    // so a binding on them never re-runs by itself. Bumping this on
    // wifiChanged is what makes those two rows live.
    Connections {
        target: Settings
        ignoreUnknownSignals: true
        function onWifiChanged() { root._wifiRev++; }
        function onNetChanged() { if (root.activeSection === "Connectivity") root._netRev++; }
    }

    // Shared with SoundboardPage's picker in name only — this is a separate
    // instance opened in "alert" mode from the Alert Sounds Repeater above.
    Sheets.SoundPickerSheet {
        id: alertSoundPicker
        z: 60
        onClosed: opened = false
    }

    Sheets.FontEditorSheet {
        id: fontSheet
        z: 50
    }

    Sheets.ColorEditorSheet {
        id: colorSheet
        z: 50
    }

    // Soundboard slot picker — folders, then files. Two Items gated on
    // `sbPickerFiles`, so the unopened half costs nothing.
    UI.Sheet {
        id: sbPicker
        z: 60
        title: "Slot " + (root.sbPickerSlot + 1)
        subtitle: root.sbPickerFiles
                  ? ((Soundboard ? Soundboard.currentFolder : "") + " · select a file to assign the slot")
                  : "Select a folder"

        footer: UI.PillButton {
            visible: root.sbPickerFiles
            width: visible ? implicitWidth : 0
            text: "Back"
            implicitWidth: 110
            onClicked: root.sbPickerFiles = false
        }

        Item {
            anchors.fill: parent

            // ── folders ────────────────────────────────────────────────
            Item {
                anchors.fill: parent
                visible: !root.sbPickerFiles

                UI.TileGrid {
                    anchors.fill: parent
                    columns: 5
                    badgeSize: 74
                    model: root._sbFolders()
                    labelKey: "label"
                    valueKey: "value"
                    onPicked: function (v) {
                        if (Soundboard) Soundboard.selectFolder(v);
                        root.sbPickerFiles = true;
                    }
                }

                Text {
                    anchors.centerIn: parent
                    visible: !Soundboard || Soundboard.folders.length === 0
                    text: "No sound folders found in Sounds/."
                    color: Theme.inkFaint
                    font.family: Theme.fontFamily
                    font.pixelSize: 16
                }
            }

            // ── files ──────────────────────────────────────────────────
            Item {
                anchors.fill: parent
                visible: root.sbPickerFiles

                ListView {
                    anchors.fill: parent
                    clip: true
                    spacing: 8
                    boundsBehavior: Flickable.StopAtBounds
                    model: root.sbPickerFiles && Soundboard ? Soundboard.files : []

                    delegate: UI.PillButton {
                        width: ListView.view.width
                        height: 60
                        radius: Theme.radiusControl
                        text: modelData
                        onClicked: {
                            if (!Soundboard) return;
                            var path = Soundboard.pathForIndex(index);
                            if (path)
                                Soundboard.setFavorite(root.sbPickerSlot, path, modelData);
                            sbPicker.close();
                        }
                    }
                }

                Text {
                    anchors.centerIn: parent
                    visible: !Soundboard || Soundboard.files.length === 0
                    text: "No sounds in this folder."
                    color: Theme.inkFaint
                    font.family: Theme.fontFamily
                    font.pixelSize: 16
                }
            }
        }

        onClosed: root.sbPickerFiles = false
    }

    // Hand-correct the odometer. trips_store.py also picks up a direct edit
    // of ~/Documents/laladash_total_km.txt on its own (see peek_trips_snapshot
    // / _sync_total_km_from_disk) — this sheet is the no-SSH-needed path to
    // the same file.
    UI.Sheet {
        id: totalKmSheet
        z: 60
        title: "Total Distance"
        subtitle: "Odometer correction"
        opened: root.totalKmEditorOpen
        onClosed: root.totalKmEditorOpen = false

        Column {
            anchors.fill: parent
            spacing: 18

            Rectangle {
                width: parent.width
                height: 64
                radius: Theme.radiusControl
                color: Theme.surface

                Text {
                    anchors.centerIn: parent
                    text: (root.totalKmDraft || "0") + " km"
                    color: Theme.ink
                    font.family: Theme.fontFamily
                    font.pixelSize: 28
                    font.weight: Font.Bold
                }
            }

            UI.NumericKeypad {
                width: Math.min(parent.width, 420)
                anchors.horizontalCenter: parent.horizontalCenter
                enterLabel: "Save"
                // A bare "." or empty field isn't a value worth saving.
                enterEnabled: root.totalKmDraft !== "" && !isNaN(parseFloat(root.totalKmDraft))
                onDigitPressed: function (ch) {
                    // One decimal point only.
                    if (ch === "." && root.totalKmDraft.indexOf(".") !== -1)
                        return;
                    root.totalKmDraft += ch;
                }
                onBackspace: root.totalKmDraft = root.totalKmDraft.slice(0, -1)
                onSubmitted: {
                    if (Settings && !isNaN(parseFloat(root.totalKmDraft)))
                        Settings.setTotalKm(parseFloat(root.totalKmDraft));
                    root.totalKmEditorOpen = false;
                }
            }
        }
    }

    Sheets.ConfirmSheet {
        id: confirmSheet
        z: 70
    }

    // ═══════════════════════════════════════════════════════════════════
    //  Inline pieces
    // ═══════════════════════════════════════════════════════════════════

    /** Right-hand read-only value. */
    component ValueText: Text {
        color: Theme.ink
        font.family: Theme.fontFamily
        font.pixelSize: 17
        font.weight: Font.DemiBold
        width: Math.min(implicitWidth, 420)
        elide: Text.ElideRight
    }

    /** Value + chevron, for rows that open something. */
    component ValueChevron: Row {
        id: chevronRow
        property string value: ""
        spacing: 12

        Text {
            text: chevronRow.value
            color: Theme.ink
            font.family: Theme.fontFamily
            font.pixelSize: 17
            font.weight: Font.DemiBold
            width: Math.min(implicitWidth, 380)
            elide: Text.ElideRight
        }

        Text {
            text: "›"
            color: Theme.inkGhost
            font.family: Theme.fontFamily
            font.pixelSize: 20
        }
    }

    /** A→B colour preview chip plus a chevron. */
    component Swatch: Row {
        id: swatchRow
        property color colorA: "#2a2f3a"
        property color colorB: "#2a2f3a"
        spacing: 12

        Rectangle {
            width: 96
            height: 40
            radius: Theme.radiusControl - 4
            gradient: Gradient {
                orientation: Gradient.Horizontal
                GradientStop { position: 0.0; color: swatchRow.colorA }
                GradientStop { position: 1.0; color: swatchRow.colorB }
            }
            border.width: 1
            border.color: Theme.outline
        }

        Text {
            text: "›"
            color: Theme.inkGhost
            font.family: Theme.fontFamily
            font.pixelSize: 20
            height: 40
            verticalAlignment: Text.AlignVCenter
        }
    }

    /** Slider + right-aligned readout, sized to sit in a ListRow's control slot. */
    component SliderControl: Item {
        id: sliderCtl
        property real value: 0.0
        property string readout: ""
        signal moved(real v)
        signal released(real v)

        width: 330
        height: 34

        UI.Slider {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            width: 258
            value: sliderCtl.value
            onMoved: function (v) { sliderCtl.moved(v); }
            onReleased: function (v) { sliderCtl.released(v); }
        }

        Text {
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            width: 60
            horizontalAlignment: Text.AlignRight
            text: sliderCtl.readout
            color: Theme.ink
            font.family: Theme.fontFamily
            font.pixelSize: 16
            font.weight: Font.DemiBold
        }
    }

    /**
     * Full-width block for the things that are not rows — section labels,
     * button flows, log panels. Same vertical rhythm and hairline as ListRow
     * so a section reads as one list.
     */
    component Block: Item {
        id: blk
        default property alias items: blkCol.data
        property bool divider: true
        property int spacing: 10

        implicitWidth: parent ? parent.width : 400
        implicitHeight: blkCol.implicitHeight + 26

        Column {
            id: blkCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: blk.spacing
        }

        Rectangle {
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            anchors.right: parent.right
            height: 1
            color: Theme.hairline
            visible: blk.divider
        }
    }
}
