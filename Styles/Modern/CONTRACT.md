# Modern skin — component contract

Read this before writing any page under `Styles/Modern/pages/`.

## Rules

1. **Keep every legacy control.** Labels, text and bridge calls come from the
   legacy page. Only the styling and layout change. If a legacy control has no
   place in the mockup, invent one in the mockup's idiom — do not drop it.
2. **Use the palette, never a literal colour.** No `#rrggbb` in page files.
3. **Use the components below.** Do not re-implement buttons, toggles or sliders.
4. **Every `Image` declares `sourceSize`.** Icons are 1024×1024 on disk; without
   it each one becomes a 4 MB texture on the Pi.
5. **Gate every loop.** Any `SequentialAnimation`/`Timer` must have
   `running:` bound to something that is false when the page is not visible.
   A page that animates while hidden is a bug on the Pi.
6. **Qt is 6.4.** No `topLeftRadius`/per-corner radius, no `Qt5Compat` effects,
   no `required property` in delegates.
7. Null-guard bridges the way legacy does: `Music ? Music.title : ""`.

## Import

```qml
import QtQuick
import QtQuick.Layouts
import "../components" as UI
```

## Palette (`Theme.*`)

```
ground surface surfaceRaised surfaceHover surfacePressed
accent accentHover accentPressed accentInk accentSoft
ink inkDim inkFaint inkGhost hairline outline
ok okSoft okBg  warn  danger dangerSoft dangerBg
scrim sheet
statusH dockH radiusCard radiusControl radiusPill gap pad
fontFamily  fontSize(role)  fontWeight(role)
```

Depth is a surface step, never a border or shadow:
`ground` (page) → `surface` (card) → `surfaceRaised` (control inside a card).

Font roles still work: `pages_primary_text`, `pages_secondary_text`,
`widgets_primary_text`, `widgets_secondary_text`, `widgets_tertiary_text`,
`settings_primary_text`, `settings_secondary_text`, `buttons_medium_text`,
`buttons_multiselect_text`, `buttons_push_text`, `top_bar_text`.

## Components

| Component | Key properties | Signals |
|---|---|---|
| `UI.Card` | `raised` | — |
| `UI.PillButton` | `text` `iconSource` `iconSize` `variant`(raised/accent/ghost/outline/danger) `checked` `enabled` `fontSize` `radius` | `clicked()` `pressAndHold()` |
| `UI.IconButton` | `iconSource` `glyph` `glyphSize` `iconSize` `checked` `activeColor` `radius` | `clicked()` `pressAndHold()` |
| `UI.Toggle` | `checked` `enabled` | `toggled(bool)` |
| `UI.Slider` | `value`(0..1) `enabled` `fillColor` `trackHeight` `knobSize` | `moved(real)` `released(real)` `pressed(real)` |
| `UI.Segmented` | `model` `labelKey` `valueKey` `currentValue` `clearable` `segmentHeight` | `selected(value)` |
| `UI.StatTile` | `label` `value` `unit` `valueColor` `hero` `valueSize` `iconSource` | — |
| `UI.ListRow` | `label` `sub` `clickable` `enabled` `divider` `minHeight` + default child = right-hand control | `clicked()` |
| `UI.Sheet` | `title` `subtitle` `opened` `showClose` `footer` + default child = body | `closed()` |
| `UI.TileGrid` | `model` `columns` `currentValue` `labelKey` `valueKey` `badgeSize` | `picked(value)` |
| `UI.SectionLabel` | `label` | — |
| `UI.LedBar` | `value` `maxValue` `warnAt` `dangerAt` `redlineAt` `segments` `segmentGap` `dimOpacity` | — |
| `UI.LedDigits` | `text` `pixelSize` `color` `shadowOpacity` `shadowScale` `showShadow` | — |
| `UI.DialGauge` | `value` `maxValue` `warnAt` `dangerAt` `redlineAt` `startAngle` `endAngle` `tickCount` `majorEvery` `labels` `litColor` `dimOpacity` `filled` `litBoost` `showHead` + default child = centre content | — |
| `UI.LiveFrame` | `provider` `rev` `active` `fillMode` `showing` — double-buffered live picture from an image provider (a plain `Image` blanks between frames) | — |
| `UI.TouchKeyboard` | `enterLabel` `enterEnabled` `keyLayer`(letters/symbols) `shifted` `capsLock` | `keyPressed(string)` `backspace()` `submitted()` |

`DialGauge` is the instrument-cluster dial used by the driving screensaver.
Its lit sweep is the tick marks themselves, not a drawn arc: the ticks are
created once and never move, so a telemetry update changes `opacity` and
`color` on N `Rectangle`s and nothing re-lays out. Do not reach for `Canvas`
here — an arc repainted at the telemetry rate is the idle-render-loop bug in
another costume.

`TouchKeyboard` is the only way to type on the device: the Pi boots straight
into the app with no desktop shell behind it, so there is no system keyboard.
It emits characters and leaves the caller owning the text it is building — see
`sheets/WifiSheet.qml`. Note the property is `keyLayer`, not `layer`: `Item.layer`
is FINAL and shadowing it makes the whole component fail to load.

`DialGauge`'s lit sweep is `filled` by default — lit ticks widen and lengthen
until the swept part reads as a band — and ramps in brightness *along the
sweep* (`_litColorAt`/`_litOpacityAt`) rather than filling flat, capped by
`showHead`, a single rotated marker standing in for a needle. Keep `litBoost`
near 1.2: past that the accent blue washes out to near-white and stops reading
as a colour.

The ramp is a function of the tick index **only**, never of the current value.
A gradient that tracked the head would put all 61 colour bindings on the
telemetry signal — ~1200 colour computations a second across the two dials on a
Pi 4B. As written, each tick computes its colour once and re-runs only when it
crosses the lit threshold.

`LedDigits` draws each glyph in a fixed-width cell over a soft shadow **of
itself** — a copy at `shadowScale`, so it reads as a halo round the edges. It
used to draw an unlit "8" behind every cell and pad short values with more of
them to hold a fixed width; that turned 800 rpm into something that read as
8800. Do not reintroduce placeholder glyphs: the cell already holds the width.

`LedBar` / `LedDigits` are the 80s LED gauge pair. `LedDigits` carries the
cluster's numerals; `LedBar` is currently unused by any page (the rev-ladder
screensaver it was built for was replaced by the dial cluster) and is kept as
a component. Feed the thresholds from `Telemetry.gaugeBand("<Metric>")`
— `(warn, danger, redline, full-scale)` straight out of `GAUGE_BANDS` in
`Modules/Obd/telemetry.py`, the same tuple the colour lambda uses — and colour
the digits with the matching `Telemetry.<metric>Color`. Do not retype the
numbers: the bar's zones and the value's colour agreeing is the whole point,
and `LedBar` guarantees the last lit segment is never milder than the value's
own band.

### Examples

```qml
UI.ListRow {
    label: "Wi-Fi"; sub: "Wireless radio and network scan"
    UI.Toggle {
        checked: Settings.wifiEnabled()
        onToggled: Settings.setWifiEnabled(value)
    }
}

UI.Segmented {
    model: ["Quiet", "Normal", "Sport", "JDM", "Extreme"]
    currentValue: Modding.preset
    onSelected: function (v) { Modding.setPreset(v); }
}

UI.Sheet {
    id: picker
    title: "Library"; subtitle: Music.tagList.length + " collections"
    opened: root.libraryOpen
    onClosed: root.libraryOpen = false
    UI.TileGrid {
        anchors.fill: parent
        columns: 8
        model: Music.tagList
        labelKey: "label"; valueKey: "name"
        onPicked: function (v) { Music.filterByTag(v); picker.close(); }
    }
}
```

## Mockup reference

`LalaDash Modern.dc.html` in the repo root. Geometry worth copying: page padding
22, card radius 20–22, control radius 13–14, pill radius 22, grid gap 14–16,
card padding 20–28. Hero numerals 74 px bold; section labels 13 px bold
uppercase with 0.10em tracking; row labels 18 px semibold over 14 px `inkFaint`.

## Page shape

Each page is a root `Item` filling its slot in the shell's `StackLayout`. The
shell already paints `ground` behind you and owns the 52 px status strip and the
88 px dock — do not draw either, and do not paint a background unless the page
needs a different one.
