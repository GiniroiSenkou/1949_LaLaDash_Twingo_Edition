import QtQuick
import "../components" as UI

/**
 * Trip report — the summary of one recorded session
 * (bridges/triplog_bridge.py `analyze()` → `trip_analyzer.summarize`).
 *
 * `open(name)` kicks off the parse on a worker thread; `analysisReady` fills
 * the sheet. Per channel: a header (count · span), min/max/avg tiles for its
 * numeric fields, event tallies, and a short timeline. The raw .jsonl stays
 * on disk (path shown) to pull to the dev PC for deeper analysis.
 */
UI.Sheet {
    id: sheet
    title: "Trip report"
    subtitle: sheet._name

    property string _name: ""
    property var _report: null
    property bool _loading: false

    function open(name) {
        sheet._name = name;
        sheet._report = null;
        sheet._loading = true;
        if (TripLog) TripLog.analyze(name);
        sheet.opened = true;
    }

    Connections {
        target: TripLog
        ignoreUnknownSignals: true
        function onAnalysisReady(name, report) {
            if (name === sheet._name) { sheet._report = report; sheet._loading = false; }
        }
    }

    Column {
        width: parent.width
        spacing: 10

        Text {
            visible: sheet._loading
            width: parent.width
            text: "Reading…"
            color: Theme.inkFaint
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSize("settings_secondary_text")
        }

        // overview
        Text {
            visible: !sheet._loading && sheet._report
            width: parent.width
            text: {
                if (!sheet._report) return "";
                var r = sheet._report;
                if (r.error) return "Could not read: " + r.error;
                var dur = r.duration_s || 0;
                var mins = Math.floor(dur / 60), secs = Math.round(dur % 60);
                return r.records + " records · " + (mins > 0 ? mins + "m " : "") + secs + "s · "
                       + (r.header && r.header.protocol ? "protocol " + r.header.protocol : "");
            }
            color: Theme.ink
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSize("settings_secondary_text")
            wrapMode: Text.Wrap
        }

        Text {
            visible: !sheet._loading && sheet._report && !sheet._report.error
            width: parent.width
            text: "File: " + (TripLog ? TripLog.pathHint(sheet._name) : sheet._name)
            color: Theme.inkFaint
            font.family: Theme.fontFamily
            font.pixelSize: 12
            elide: Text.ElideMiddle
        }

        // per-channel cards
        Repeater {
            model: (sheet._report && sheet._report.sections) ? sheet._report.sections : []
            UI.Card {
                width: parent.width
                height: secCol.implicitHeight + padding * 2

                Column {
                    id: secCol
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: parent.padding
                    spacing: 8

                    UI.SectionLabel {
                        label: modelData.label + "  ·  " + modelData.count
                               + (modelData.span_s ? "  ·  " + modelData.span_s + "s" : "")
                        width: parent.width
                    }

                    // metric tiles: field  min / avg / max  (last)
                    Repeater {
                        model: modelData.metrics ? modelData.metrics : []
                        Row {
                            width: secCol.width
                            spacing: 10
                            Text {
                                width: 90
                                text: modelData.field
                                color: Theme.inkFaint
                                font.family: Theme.fontFamily
                                font.pixelSize: 13
                                elide: Text.ElideRight
                            }
                            Text {
                                width: parent.width - 100
                                text: "min " + modelData.min + "   avg " + modelData.avg
                                      + "   max " + modelData.max + "   (last " + modelData.last + ")"
                                color: Theme.ink
                                font.family: Theme.fontFamily
                                font.pixelSize: 13
                                font.weight: Font.DemiBold
                                elide: Text.ElideRight
                            }
                        }
                    }

                    // event tallies
                    Text {
                        visible: modelData.events !== undefined
                        width: parent.width
                        text: {
                            if (!modelData.events) return "";
                            var out = [];
                            for (var k in modelData.events) out.push(k + " ×" + modelData.events[k]);
                            return out.join("    ");
                        }
                        color: Theme.inkDim
                        font.family: Theme.fontFamily
                        font.pixelSize: 13
                        wrapMode: Text.Wrap
                    }
                }
            }
        }

        Text {
            visible: !sheet._loading && sheet._report && sheet._report.records === 0 && !sheet._report.error
            width: parent.width
            text: "This session has no records for its channels."
            color: Theme.inkFaint
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSize("settings_secondary_text")
        }
    }
}
