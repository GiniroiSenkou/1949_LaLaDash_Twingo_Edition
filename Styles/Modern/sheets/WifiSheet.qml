import QtQuick
import "../components" as UI

/**
 * Wi-Fi — scan, join, disconnect, forget, with the on-screen keyboard.
 *
 * The Pi boots straight into the app with no desktop shell behind it, so this
 * is the only place a network password can be typed on the device.
 *
 * Two panes side by side: the network list on the left stays visible while the
 * keyboard is up on the right, because on a 1280x800 panel a full-width
 * keyboard would push the list off screen and you would be typing a password
 * with no reminder of which network you picked.
 *
 * All of Settings' Wi-Fi calls are asynchronous — nmcli is a subprocess and a
 * join can take most of a minute — so the sheet reflects Settings.wifiBusy and
 * Settings.wifiStatus rather than assuming anything succeeded.
 *
 * `adapter` picks which backing adapter the whole sheet talks to: "primary"
 * (the car's onboard Wi-Fi) or "secondary" (an occasional longer-range USB
 * dongle, only ever present when Settings.hasSecondWifiAdapter). One sheet
 * for both: a switch in the header flips `adapter`, and a status card above
 * the list always says which device is being driven, what it is on, its
 * address and its live throughput — so there is no guessing which radio a
 * tap will land on. Everything below routes through the
 * `_status`/`_networks`/`_busy`/`_available` properties and the
 * `_scan`/`_connect`/`_disconnect`/`_forget` functions rather than calling
 * `Settings.wifi*` directly, so the list/keyboard/password-flow layout itself
 * never needs to know which adapter it's driving.
 */
UI.Sheet {
    id: sheet

    property string adapter: "primary"

    title: "Wi-Fi"
    subtitle: sheet._status
    fullScreen: true

    /** Device name of the adapter in charge ("wlan0" / "wlan1"). */
    readonly property string _iface: !Settings ? ""
        : (adapter === "secondary" ? Settings.secondWifiAdapterName : Settings.wifiAdapterName)
    /** Bumped by Settings.netChanged (2 s worker) so the status card re-reads. */
    property int _netRev: 0
    readonly property var _net: (sheet._netRev, Settings && sheet.opened) ? Settings.netInfo(sheet._iface) : ({})

    Connections {
        target: Settings
        ignoreUnknownSignals: true
        function onNetChanged() { if (sheet.opened) sheet._netRev++; }
    }

    onAdapterChanged: if (opened) { pendingSsid = ""; password = ""; _scan(); }

    // ── adapter indirection ──────────────────────────────────────────────
    readonly property string _status: !Settings ? ""
        : (adapter === "secondary" ? Settings.wifi2Status : Settings.wifiStatus)
    readonly property var _networks: !Settings ? []
        : (adapter === "secondary" ? Settings.wifi2Networks : Settings.wifiNetworks)
    readonly property bool _busy: !!Settings
        && (adapter === "secondary" ? Settings.wifi2Busy : Settings.wifiBusy)
    // The primary adapter can be missing nmcli entirely; the secondary sheet
    // is only ever opened once Settings.hasSecondWifiAdapter is already true,
    // which itself requires a working nmcli — nothing further to check here.
    readonly property bool _available: adapter === "secondary" ? true
        : !!(Settings && Settings.wifiAvailable)

    function _scan() {
        if (!Settings) return;
        if (adapter === "secondary") Settings.scanWifi2(); else Settings.scanWifi();
    }
    function _connect(ssid, pw) {
        if (!Settings) return;
        if (adapter === "secondary") Settings.connectWifi2(ssid, pw);
        else Settings.connectWifi(ssid, pw);
    }
    function _disconnect(ssid) {
        if (!Settings) return;
        if (adapter === "secondary") Settings.disconnectWifi2(ssid);
        else Settings.disconnectWifi(ssid);
    }
    function _forget(ssid) {
        if (!Settings) return;
        if (adapter === "secondary") Settings.forgetWifi2(ssid);
        else Settings.forgetWifi(ssid);
    }

    /** SSID awaiting a password; "" while browsing. */
    property string pendingSsid: ""
    property bool pendingSecured: true
    property string password: ""
    property bool showPassword: false

    function _bars(signal) {
        if (signal >= 75) return "▁▃▅▇";
        if (signal >= 50) return "▁▃▅";
        if (signal >= 25) return "▁▃";
        return "▁";
    }

    function _pick(row) {
        if (row.active) {
            // Already on it — offer to leave rather than re-join.
            sheet._disconnect(row.ssid);
            return;
        }
        if (!row.secured || row.saved) {
            // Open network, or one NetworkManager already holds the key for.
            sheet._connect(row.ssid, "");
            return;
        }
        sheet.pendingSsid = row.ssid;
        sheet.pendingSecured = true;
        sheet.password = "";
    }

    function _submit() {
        if (sheet.pendingSsid === "" || sheet.password.length < 1)
            return;
        sheet._connect(sheet.pendingSsid, sheet.password);
        sheet.pendingSsid = "";
        sheet.password = "";
    }

    onOpenedChanged: {
        if (opened) {
            pendingSsid = "";
            password = "";
            showPassword = false;
            sheet._scan();
        }
    }

    headerControl: Row {
        spacing: 10

        // Which radio. Only shown when there are two; the labels carry the
        // device names so "Pi" vs "USB" is never a guess.
        UI.Segmented {
            visible: Settings ? Settings.hasSecondWifiAdapter : false
            segmentHeight: 34
            model: [
                { label: "Pi built-in" + (Settings && Settings.wifiAdapterName ? " · " + Settings.wifiAdapterName : ""), value: "primary" },
                { label: "USB" + (Settings && Settings.secondWifiAdapterName ? " · " + Settings.secondWifiAdapterName : ""), value: "secondary" }
            ]
            currentValue: sheet.adapter
            onSelected: function (v) { sheet.adapter = v; }
        }

        UI.PillButton {
            text: sheet._busy ? "Working…" : "↻ Scan"
            implicitWidth: 130
            height: 42
            radius: Theme.radiusControl
            enabled: !sheet._busy
            onClicked: sheet._scan()
        }
    }

    Item {
        anchors.fill: parent

        // ── left: networks ────────────────────────────────────────────────
        Item {
            id: listPane
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: sheet.pendingSsid !== ""
                   ? Math.floor(parent.width * 0.38)
                   : parent.width

            Text {
                id: emptyNote
                anchors.centerIn: parent
                width: parent.width - 40
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                visible: !sheet._available
                         || (sheet._networks.length === 0 && !sheet._busy)
                text: !sheet._available
                      ? "NetworkManager (nmcli) is not installed on this device."
                      : "No networks in range. Check the Wi-Fi radio is on, then scan again."
                color: Theme.inkFaint
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSize("pages_secondary_text")
            }

            // Status card: the device this sheet is driving right now.
            Rectangle {
                id: statusCard
                anchors.top: parent.top
                anchors.left: parent.left
                anchors.right: parent.right
                height: 64
                radius: Theme.radiusControl
                color: Theme.surfaceRaised

                Row {
                    anchors.left: parent.left
                    anchors.leftMargin: 16
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 22

                    Column {
                        spacing: 2
                        anchors.verticalCenter: parent.verticalCenter
                        Text {
                            text: (sheet.adapter === "secondary" ? "USB adapter" : "Pi built-in")
                                  + (sheet._iface ? "  ·  " + sheet._iface : "")
                            color: Theme.ink; font.family: Theme.fontFamily
                            font.pixelSize: 15; font.weight: Font.DemiBold
                        }
                        Text {
                            text: sheet._net && sheet._net.ssid ? ("On " + sheet._net.ssid) : "Not connected"
                            color: sheet._net && sheet._net.ssid ? Theme.okSoft : Theme.inkFaint
                            font.family: Theme.fontFamily; font.pixelSize: 13
                        }
                    }
                    Column {
                        spacing: 2
                        anchors.verticalCenter: parent.verticalCenter
                        Text {
                            text: "IP"
                            color: Theme.inkFaint; font.family: Theme.fontFamily; font.pixelSize: 11; font.letterSpacing: 1
                        }
                        Text {
                            text: sheet._net && sheet._net.ip ? sheet._net.ip : "—"
                            color: Theme.ink; font.family: Theme.fontFamily; font.pixelSize: 15
                        }
                    }
                    Column {
                        spacing: 2
                        anchors.verticalCenter: parent.verticalCenter
                        Text {
                            text: "THROUGHPUT"
                            color: Theme.inkFaint; font.family: Theme.fontFamily; font.pixelSize: 11; font.letterSpacing: 1
                        }
                        Text {
                            text: sheet._net && sheet._net.rateText ? sheet._net.rateText : "—"
                            color: Theme.ink; font.family: Theme.fontFamily; font.pixelSize: 15
                        }
                    }
                }
            }

            ListView {
                id: netList
                anchors.top: statusCard.bottom
                anchors.topMargin: 10
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                clip: true
                spacing: 8
                model: sheet._networks
                visible: !emptyNote.visible

                delegate: Rectangle {
                    width: netList.width
                    height: 68
                    radius: Theme.radiusControl
                    color: modelData.ssid === sheet.pendingSsid ? Theme.accentSoft
                         : rowTap.pressed ? Theme.surfacePressed
                         : Theme.surface

                    Text {
                        id: bars
                        anchors.left: parent.left
                        anchors.leftMargin: 16
                        anchors.verticalCenter: parent.verticalCenter
                        width: 46
                        text: sheet._bars(modelData.signal)
                        color: modelData.signal >= 50 ? Theme.ok : Theme.inkDim
                        font.family: Theme.fontFamily
                        font.pixelSize: 18
                    }

                    Column {
                        anchors.left: bars.right
                        anchors.leftMargin: 8
                        anchors.right: rowActions.left
                        anchors.rightMargin: 10
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 2

                        Text {
                            width: parent.width
                            text: modelData.ssid
                            color: Theme.ink
                            font.family: Theme.fontFamily
                            font.pixelSize: 17
                            font.weight: Font.DemiBold
                            elide: Text.ElideRight
                        }

                        Text {
                            width: parent.width
                            text: {
                                var bits = [];
                                if (modelData.active) bits.push("Connected");
                                else if (modelData.saved) bits.push("Saved");
                                bits.push(modelData.secured ? modelData.security : "Open");
                                if (modelData.band) bits.push(modelData.band);
                                bits.push(modelData.signal + "%");
                                return bits.join(" · ");
                            }
                            color: modelData.active ? Theme.okSoft : Theme.inkFaint
                            font.family: Theme.fontFamily
                            font.pixelSize: 13
                            elide: Text.ElideRight
                        }
                    }

                    Row {
                        id: rowActions
                        anchors.right: parent.right
                        anchors.rightMargin: 14
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 8

                        UI.PillButton {
                            width: 104
                            height: 40
                            radius: Theme.radiusControl
                            fontSize: 14
                            text: modelData.active ? "Disconnect"
                                : (modelData.saved || !modelData.secured ? "Connect" : "Password")
                            variant: modelData.active ? "outline" : "raised"
                            enabled: !sheet._busy
                            onClicked: sheet._pick(modelData)
                        }

                        UI.PillButton {
                            width: 84
                            height: 40
                            radius: Theme.radiusControl
                            fontSize: 14
                            text: "Forget"
                            variant: "danger"
                            visible: modelData.saved
                            enabled: !sheet._busy
                            onClicked: sheet._forget(modelData.ssid)
                        }
                    }

                    MouseArea {
                        id: rowTap
                        anchors.fill: parent
                        anchors.rightMargin: rowActions.width + 24
                        enabled: !sheet._busy
                        onClicked: sheet._pick(modelData)
                    }
                }
            }
        }

        // ── right: password entry ─────────────────────────────────────────
        Item {
            id: entryPane
            visible: sheet.pendingSsid !== ""
            anchors.left: listPane.right
            anchors.leftMargin: visible ? 20 : 0
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.bottom: parent.bottom

            Text {
                id: entryTitle
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                text: "Password for " + sheet.pendingSsid
                color: Theme.ink
                font.family: Theme.fontFamily
                font.pixelSize: 20
                font.weight: Font.Bold
                elide: Text.ElideRight
            }

            Rectangle {
                id: field
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: entryTitle.bottom
                anchors.topMargin: 12
                height: 56
                radius: Theme.radiusControl
                color: Theme.surface

                Text {
                    anchors.left: parent.left
                    anchors.leftMargin: 16
                    anchors.right: revealBtn.left
                    anchors.rightMargin: 10
                    anchors.verticalCenter: parent.verticalCenter
                    text: sheet.password.length === 0
                          ? "Tap the keys below"
                          : (sheet.showPassword
                             ? sheet.password
                             : "•".repeat(sheet.password.length))
                    color: sheet.password.length === 0 ? Theme.inkGhost : Theme.ink
                    font.family: Theme.fontFamily
                    font.pixelSize: 20
                    elide: Text.ElideLeft
                }

                UI.PillButton {
                    id: revealBtn
                    anchors.right: parent.right
                    anchors.rightMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    width: 84
                    height: 40
                    radius: Theme.radiusControl
                    fontSize: 14
                    variant: "ghost"
                    text: sheet.showPassword ? "Hide" : "Show"
                    onClicked: sheet.showPassword = !sheet.showPassword
                }
            }

            Row {
                id: entryActions
                anchors.left: parent.left
                anchors.top: field.bottom
                anchors.topMargin: 12
                spacing: 10

                UI.PillButton {
                    width: 120
                    height: 44
                    radius: Theme.radiusControl
                    text: "Cancel"
                    variant: "outline"
                    onClicked: {
                        sheet.pendingSsid = "";
                        sheet.password = "";
                        sheet.showPassword = false;
                    }
                }

                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: sheet.password.length > 0 && sheet.password.length < 8
                          ? "WPA keys are at least 8 characters"
                          : ""
                    color: Theme.warn
                    font.family: Theme.fontFamily
                    font.pixelSize: 13
                }
            }

            UI.TouchKeyboard {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: entryActions.bottom
                anchors.topMargin: 14
                enterLabel: "Join"
                enterEnabled: sheet.password.length >= 8 && !sheet._busy
                onKeyPressed: function (ch) { sheet.password += ch; }
                onBackspace: sheet.password = sheet.password.slice(0, -1)
                onSubmitted: sheet._submit()
            }
        }
    }
}
