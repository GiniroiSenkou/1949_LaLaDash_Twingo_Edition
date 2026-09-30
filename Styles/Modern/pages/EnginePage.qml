import QtQuick
import QtQuick.Layouts
import "../components" as UI
import "../sheets" as Sheets

/**
 * Engine page — the sound-pack / modding screen (Nav index 2).
 *
 * Mockup shape: a main card (pack header · artwork · mode row) beside a 388 px
 * rail listing the sound layers.
 *
 * Everything the page drives:
 *   · Modding.activate()/deactivate() tied to component life — the page is
 *     always loaded, so this is what actually starts and stops the audio feed.
 *   · Car name; logo and artwork both open the picker.
 *   · The ‹ › arrows step through the loaded brand's own models.
 *   · The five-preset bar.
 *   · The seven sound-layer states as compact chips, which the legacy skin drew as text along a
 *     parabolic arc over the car and the mockup lays out as a list.
 */
Item {
    id: root

    Component.onCompleted: Modding.activate()
    Component.onDestruction: Modding.deactivate()

    readonly property var presets: ["Quiet", "Normal", "Sport", "JDM", "Extreme"]

    // ── stepping through the loaded brand's models ─────────────────────────
    // ModdingBridge stores the selection as "<brand>/<model>" and splits it back
    // out: `carBrand` and `carName` are the two folder names verbatim, so
    // carName can be matched straight against carModels(carBrand). Both notify
    // on carChanged, so these re-solve exactly once per car change — the one
    // listdir per change is why they are cached here rather than called inline.
    readonly property string carBrand: Modding ? Modding.carBrand : ""
    readonly property string carModel: Modding ? Modding.carName : ""
    readonly property var brandModels: (Modding && carBrand !== "")
                                       ? (Modding.carModels(carBrand) || []) : []
    readonly property bool canStepCar: brandModels.length > 1

    /** Load the model `delta` places away in the brand folder, wrapping. */
    function _stepCar(delta) {
        var models = root.brandModels;
        if (!Modding || models.length < 2)
            return;
        var i = models.indexOf(root.carModel);
        var next = (i < 0) ? (delta > 0 ? 0 : models.length - 1)
                           : ((i + delta) % models.length + models.length) % models.length;
        if (models[next] === root.carModel)
            return;
        Modding.loadCar(root.carBrand, models[next]);
    }

    readonly property var soundLayers: [
        { key: "engine",       label: "Engine",       feature: "" },
        { key: "coast",        label: "Coast",        feature: "" },
        { key: "engine_start", label: "Engine Start", feature: "" },
        { key: "turbo",        label: "Turbo",        feature: "turbo" },
        { key: "transmission", label: "Transmission", feature: "transmission" },
        { key: "pops",         label: "POPs",         feature: "pops" },
        { key: "flutter",      label: "Flutter",      feature: "flutter" }
    ]

    /** Legacy semantics: Quiet mutes everything; a layer with no feature flag
        is always on; otherwise the matching Modding feature toggle decides. */
    function _layerActive(entry) {
        if (!Modding || Modding.preset === "Quiet")
            return false;
        if (entry.feature === "")
            return true;
        if (entry.feature === "turbo")
            return Modding.turboEnabled;
        if (entry.feature === "transmission")
            return Modding.transmissionEnabled;
        if (entry.feature === "pops")
            return Modding.popsEnabled;
        if (entry.feature === "flutter")
            return Modding.flutterEnabled;
        return false;
    }

    function _layerHasAsset(entry) {
        var avail = Modding ? Modding.soundAvailability : null;
        return !!(avail && avail[entry.key]);
    }

    /** Legacy colour code, unchanged: neutral while Quiet, faint when the layer
        is off, green when it is on and the sample exists, red when it is on but
        the pack is missing the file. */
    function _layerColor(entry) {
        if (!Modding || Modding.preset === "Quiet")
            return Theme.ink;
        if (!_layerActive(entry))
            return Theme.inkFaint;
        return _layerHasAsset(entry) ? Theme.ok : Theme.danger;
    }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 22
        anchors.rightMargin: 22
        anchors.topMargin: 6
        spacing: 22

        // ── main card ──────────────────────────────────────────────────
        UI.Card {
            Layout.fillWidth: true
            Layout.fillHeight: true

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 24
                spacing: 12

                // header: brand badge · model name · prev/next model
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 14

                    Image {
                        id: carLogo
                        property string wanted: Modding && Modding.carLogoPath
                                                ? "file://" + Modding.carLogoPath : ""
                        Layout.preferredWidth: 46
                        Layout.preferredHeight: 54
                        fillMode: Image.PreserveAspectFit
                        smooth: true
                        asynchronous: true
                        source: wanted
                        sourceSize.width: 108
                        sourceSize.height: 108
                        // Re-arm the binding: a fallback below assigns `source`
                        // directly, which would otherwise stick across cars.
                        onWantedChanged: source = wanted
                        onStatusChanged: if (status === Image.Error)
                                             source = iconsPath + "/side.png"

                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: carPicker.show()
                        }
                    }

                    Text {
                        Layout.fillWidth: true
                        text: root.carModel
                        color: Theme.ink
                        font.family: Theme.fontFamily
                        font.pixelSize: 28
                        font.weight: Font.Bold
                        elide: Text.ElideRight
                    }

                    // Step through the models of the brand folder that is
                    // already loaded; the picker is for changing brand.
                    UI.PillButton {
                        Layout.preferredWidth: 46
                        Layout.preferredHeight: 46
                        radius: 23
                        text: "‹"
                        variant: "raised"
                        fontSize: 20
                        textColor: Theme.inkDim
                        enabled: root.canStepCar
                        onClicked: root._stepCar(-1)
                    }

                    UI.PillButton {
                        Layout.preferredWidth: 46
                        Layout.preferredHeight: 46
                        radius: 23
                        text: "›"
                        variant: "raised"
                        fontSize: 20
                        textColor: Theme.inkDim
                        enabled: root.canStepCar
                        onClicked: root._stepCar(1)
                    }
                }

                // artwork
                Item {
                    Layout.fillWidth: true
                    Layout.fillHeight: true

                    Image {
                        id: carArt
                        property string wanted: Modding && Modding.carImagePath
                                                ? "file://" + Modding.carImagePath : ""
                        anchors.centerIn: parent
                        width: Math.min(parent.width, 560)
                        height: parent.height * 0.86
                        fillMode: Image.PreserveAspectFit
                        smooth: true
                        asynchronous: true
                        source: wanted
                        sourceSize.width: 560
                        sourceSize.height: 420
                        onWantedChanged: source = wanted
                        onStatusChanged: if (status === Image.Error)
                                             source = iconsPath + "/side.png"

                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: carPicker.show()
                        }
                    }
                }

                // preset row — five across, as mocked
                RowLayout {
                    id: presetRow
                    Layout.fillWidth: true
                    Layout.preferredHeight: 56
                    spacing: 8

                    Repeater {
                        model: root.presets

                        UI.PillButton {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 56
                            radius: Theme.radiusControl
                            text: modelData
                            variant: "raised"
                            // Sized off the row, never off the button's own width:
                            // that would feed implicitWidth back into the layout.
                            fontSize: Math.max(11, Math.min(16,
                                          Math.round(presetRow.width / root.presets.length * 0.17)))
                            fontWeight: Font.Bold
                            checked: Modding && Modding.preset === modelData
                            onClicked: Modding.setPreset(modelData)
                        }
                    }
                }
            }
        }

        // ── sound-layer rail ───────────────────────────────────────────
        UI.Card {
            // 388 as mocked, but the side panel can take a third of the page —
            // give the main card the room back instead of overflowing it, and
            // stand the rail down entirely once the panel is actually open.
            visible: !(Nav && Nav.sidePanelOpen)
            Layout.preferredWidth: visible ? Math.min(388, root.width * 0.36) : 0
            Layout.fillHeight: true

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 20
                spacing: 8

                Text {
                    Layout.fillWidth: true
                    Layout.bottomMargin: 4
                    text: "Sound layers"
                    color: Theme.ink
                    font.family: Theme.fontFamily
                    font.pixelSize: 15
                    font.weight: Font.Bold
                }

                // Compact: one chip per layer, the legacy colour code on the
                // text (neutral while Quiet, faint when off, green with a
                // sample, red without) and a tint behind it — read-only, as
                // before: there is no per-layer setter. Half the rail.
                Flow {
                    Layout.fillWidth: true
                    spacing: 10

                    Repeater {
                        model: root.soundLayers

                        Rectangle {
                            id: layerChip
                            readonly property bool isActive: root._layerActive(modelData)
                            readonly property bool hasAsset: root._layerHasAsset(modelData)
                            readonly property color stateColor: root._layerColor(modelData)
                            width: chipText.implicitWidth + 30
                            height: 42
                            radius: 21
                            color: !isActive ? Theme.surfaceRaised
                                 : (hasAsset ? Theme.okBg : Theme.dangerBg)
                            Behavior on color { ColorAnimation { duration: 200 } }

                            Row {
                                id: chipText
                                anchors.centerIn: parent
                                spacing: 7
                                Rectangle {
                                    width: 8; height: 8; radius: 4
                                    anchors.verticalCenter: parent.verticalCenter
                                    color: layerChip.stateColor
                                    Behavior on color { ColorAnimation { duration: 200 } }
                                }
                                Text {
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: modelData.label
                                    color: layerChip.stateColor
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 16
                                    font.weight: Font.DemiBold
                                    Behavior on color { ColorAnimation { duration: 200 } }
                                }
                            }
                        }
                    }
                }

                Item { Layout.fillHeight: true }   // the layers get the top half

                Rectangle { Layout.fillWidth: true; Layout.bottomMargin: 6; height: 1; color: Theme.hairline }

                // ── engine audio: the other half ──────────────────────
                // The volume you reach for while driving, what the sound
                // comes out of, and proof that it follows the revs: the RPM
                // and throttle the engine sound is actually fed, plus how far
                // behind the adapter they run.
                Text {
                    Layout.fillWidth: true
                    text: "Engine audio"
                    color: Theme.ink
                    font.family: Theme.fontFamily
                    font.pixelSize: 15
                    font.weight: Font.Bold
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 10
                    Text {
                        text: "Volume"
                        color: Theme.inkFaint
                        font.family: Theme.fontFamily
                        font.pixelSize: 13
                        Layout.preferredWidth: 64
                    }
                    UI.Slider {
                        id: volSlider
                        Layout.fillWidth: true
                        value: Modding ? Modding.masterVolume / 100 : 0
                        enabled: Modding ? Modding.engineSoundOn : false
                        onReleased: function (v) { if (Modding) Modding.setMasterVolume(Math.round(v * 100)); }
                    }
                    Text {
                        text: Modding ? Math.round(Modding.masterVolume) + " %" : "—"
                        color: Theme.ink
                        font.family: Theme.fontFamily
                        font.pixelSize: 14
                        font.weight: Font.DemiBold
                        Layout.preferredWidth: 44
                        horizontalAlignment: Text.AlignRight
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 10
                    Text {
                        text: "Output"
                        color: Theme.inkFaint
                        font.family: Theme.fontFamily
                        font.pixelSize: 13
                        Layout.preferredWidth: 64
                    }
                    Text {
                        Layout.fillWidth: true
                        text: Modding ? (Modding.engineOutputResolved || "system default") : "—"
                        color: Theme.ink
                        font.family: Theme.fontFamily
                        font.pixelSize: 13
                        elide: Text.ElideMiddle
                    }
                }

                // live feed bars: what the engine sound hears
                component FeedBar: RowLayout {
                    property string label: ""
                    property real frac: 0
                    property string valueText: ""
                    property color barColor: Theme.accent
                    Layout.fillWidth: true
                    spacing: 10
                    Text {
                        text: label
                        color: Theme.inkFaint
                        font.family: Theme.fontFamily
                        font.pixelSize: 13
                        Layout.preferredWidth: 64
                    }
                    Rectangle {
                        Layout.fillWidth: true
                        height: 12
                        radius: 6
                        color: Theme.surfaceRaised
                        Rectangle {
                            width: parent.width * Math.max(0, Math.min(1, frac))
                            height: parent.height
                            radius: 6
                            color: barColor
                        }
                    }
                    Text {
                        text: valueText
                        color: Theme.ink
                        font.family: Theme.fontFamily
                        font.pixelSize: 13
                        font.weight: Font.DemiBold
                        Layout.preferredWidth: 64
                        horizontalAlignment: Text.AlignRight
                    }
                }

                FeedBar {
                    label: "RPM"
                    frac: Modding ? Modding.rpm / 7000 : 0
                    valueText: Modding ? Math.round(Modding.rpm) + "" : "—"
                    barColor: Modding && Modding.rpm > 5500 ? Theme.danger : Theme.accent
                }
                FeedBar {
                    label: "Throttle"
                    frac: Modding ? Modding.throttle / 100 : 0
                    valueText: Modding ? Math.round(Modding.throttle) + " %" : "—"
                    barColor: Theme.ok
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 10
                    Text {
                        text: "Lag"
                        color: Theme.inkFaint
                        font.family: Theme.fontFamily
                        font.pixelSize: 13
                        Layout.preferredWidth: 64
                    }
                    Text {
                        Layout.fillWidth: true
                        text: {
                            if (!Modding) return "—";
                            var ms = Modding.rpmLatencyMs;
                            if (!(ms > 0)) return "adapter → sound: no live RPM yet";
                            return "adapter → sound " + Math.round(ms) + " ms"
                                   + (ms > 400 ? "  ·  Bluetooth output?" : "");
                        }
                        color: Modding && Modding.rpmLatencyMs > 400 ? Theme.warn : Theme.ink
                        font.family: Theme.fontFamily
                        font.pixelSize: 13
                        elide: Text.ElideRight
                    }
                }

                Item { Layout.fillHeight: true }
            }
        }
    }

    // ── car picker ─────────────────────────────────────────────────────
    // Full-screen: the Sheet reparents itself to the window and owns its own
    // stacking, so no z here — setting one would break that binding.
    Sheets.CarPickerSheet {
        id: carPicker
    }
}
