import QtQuick
import "../components" as UI

/**
 * Action cam › camera settings — exposure (Controls Easy/Pro, Protune, ISO,
 * shutter, EV, white balance, colour, sharpness), video and photo groups.
 *
 * Rows come from `ActionCam.settingsModel`: only the settings this camera
 * reports, each with its current value and option list, refreshed by the
 * bridge's status poll (5 s while the page shows). A change is optimistic —
 * the row shows it at once — and the camera's answer corrects it on the
 * next poll. Long option lists (white balance, shutter) step with ‹ › ; short
 * ones are a Segmented. GoPro only: an Insta360 has no settings over the
 * links this app speaks.
 */
UI.Sheet {
    id: sheet
    title: "Camera settings"
    subtitle: !ActionCam ? "" : ActionCam.settingsAvailable
              ? (ActionCam.label + " · " + ActionCam.statusText)
              : (ActionCam.kind === "gopro" ? "Connect the camera first" : "Not available for this camera")

    readonly property var rows: ActionCam ? ActionCam.settingsModel : []
    readonly property var groups: ["Exposure", "Video", "Photo"]

    function _step(row, dir) {
        var opts = row.options;
        var i = 0;
        for (var k = 0; k < opts.length; k++) if (opts[k].value === row.value) { i = k; break; }
        i = (i + dir + opts.length) % opts.length;
        ActionCam.setSetting(row.id, opts[i].value);
    }

    Column {
        width: parent.width
        spacing: 6

        Text {
            visible: sheet.rows.length === 0
            width: parent.width
            text: !ActionCam ? "" : ActionCam.kind !== "gopro"
                  ? "Settings are only reachable on a GoPro (Open GoPro). Change the Insta360 on the camera itself."
                  : (ActionCam.state === "online" ? "The camera reported no adjustable settings yet — Refresh."
                                                  : "Connect the camera to read its settings.")
            color: Theme.inkFaint
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSize("settings_secondary_text")
            wrapMode: Text.Wrap
        }

        Text {
            visible: sheet.rows.length > 0
            width: parent.width
            text: "Only what this camera exposes over Open GoPro is listed (a HERO9 offers ISO limits, resolution, frame rate, lens, HyperSmooth; shutter, white balance and EV stay on the camera itself). Changing one pauses the live picture for a moment. Video rows apply to video, photo rows to photos."
            color: Theme.inkFaint
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSize("settings_secondary_text")
            wrapMode: Text.Wrap
        }

        Repeater {
            model: sheet.groups
            Column {
                readonly property string group: modelData
                readonly property var groupRows: sheet.rows.filter(function (r) { return r.group === group; })
                visible: groupRows.length > 0
                width: parent.width
                spacing: 0

                UI.SectionLabel { label: parent.group; width: parent.width }

                Repeater {
                    model: parent.groupRows
                    UI.ListRow {
                        readonly property var row: modelData
                        readonly property bool few: row.options.length <= 4
                        label: row.label
                        sub: few ? "" : row.valueLabel
                        minHeight: 60

                        // few options → segmented; many → stepper
                        UI.Segmented {
                            visible: few
                            model: row.options
                            labelKey: "label"
                            valueKey: "value"
                            currentValue: row.value
                            onSelected: function (v) { ActionCam.setSetting(row.id, v); }
                        }
                        Row {
                            visible: !few
                            spacing: 8
                            UI.PillButton {
                                text: "‹"
                                implicitWidth: 52
                                height: 44
                                radius: Theme.radiusControl
                                onClicked: sheet._step(row, -1)
                            }
                            Rectangle {
                                width: 150
                                height: 44
                                radius: Theme.radiusControl
                                color: Theme.surfaceRaised
                                Text {
                                    anchors.centerIn: parent
                                    text: row.valueLabel
                                    color: Theme.ink
                                    font.family: Theme.fontFamily
                                    font.pixelSize: Theme.fontSize("buttons_medium_text")
                                    font.weight: Font.DemiBold
                                }
                            }
                            UI.PillButton {
                                text: "›"
                                implicitWidth: 52
                                height: 44
                                radius: Theme.radiusControl
                                onClicked: sheet._step(row, 1)
                            }
                        }
                    }
                }
            }
        }

        UI.PillButton {
            visible: sheet.rows.length > 0
            text: "Refresh"
            height: 44
            enabled: ActionCam && !ActionCam.busy
            onClicked: if (ActionCam) ActionCam.refresh()
        }
    }
}
