# LaLaDash architecture

Developer notes: how the app is put together, the rules the code follows, and the traps that
have already been hit. Read the section for the area you are changing before touching it.

## What this is

LaLaDash is a PyQt6 car dashboard running on a Raspberry Pi 4B; vehicle-specific numbers come from a profile in `Data/vehicles/` (the default is the reference car it was developed on). Target screen is 1280×800. UI is QML; backend is Python. The Pi target matters: code should not poll when an event will do, and inactive pages should cost ~nothing.

## Running

There is no test runner or build step. The app is launched directly.

There *is* a QML linter, even though nothing in the repo configures it:
`/usr/lib/qt6/bin/qmllint <file.qml>` catches syntax errors, Qt-version
mismatches (e.g. `topLeftRadius` needs 6.7, this is 6.4) and bad property
assignments without launching anything. Run it on any .qml you touch — the
`unqualified access` warnings for `Theme`, `Nav`, `iconsPath` etc. are expected
noise, since those are context properties injected by `main.py`. Only `error:`
lines and property-not-found warnings are real.

- Dev: `python3 main.py` (or `python3 main_qml.py`, which just re-imports `main.main`).
- On-device (**what actually runs — verified 2026-09-09**): `~/.config/systemd/user/laladash.service` runs `Installation/start_laladash.sh`, which exports `QT_QPA_PLATFORM=xcb` + `DISPLAY=:0` and runs **`main.py`**. The Pi session is **labwc (Wayland) + Xwayland**, so Qt renders through XWayland into a compositor — *not* eglfs.
  - `Installation/start_laladash_qml.sh` (eglfs/GLES2 + `main_qml.py`) is the alternative kiosk path; the Pi does not use it. It has no desktop session, so it exports the per-user `DBUS_SESSION_BUS_ADDRESS` explicitly.
- Dependencies: `requirements.txt` (PyQt6, python-vlc, pedalboard, sounddevice, numpy, mutagen, obd, pyserial, pygame, pydbus, python-xlib). `Installation/install.sh` is the master installer for the Pi (apt + pip).
- Individual ad-hoc tests under `tests/` are plain scripts; run from the repo root with `PYTHONPATH=. python3 tests/<name>.py`. There is no `pytest` configuration.

The MPRIS player must land on the **per-user** D-Bus bus, not a private auto-launched one, or the car head unit's `mpris-proxy` cannot see it. Under the current labwc session the systemd user unit inherits `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus` already (verified in the running process). `start_laladash_qml.sh` exports it explicitly because eglfs has no desktop session — keep that export if the eglfs path is ever revived.

## Deployment target

Target hardware is a Raspberry Pi 4B driving a 1280×800 panel. Deploy helpers read their host/key from `Installation/local.env` (see `local.env.example`).

## Visual styles

The UI is skinned. `[UI] style` in `Data/settings.ini` names the active style
(default `Modern`); `main.py` resolves it through `Modules/Runtime/styles.py` and
exposes `stylePath` to QML. `main.qml` is a bare `ApplicationWindow` plus **one
`Loader`** that loads `Styles/<Style>/Shell.qml`.

That single Loader is the whole point: exactly one style is ever parsed or
instantiated, so an inactive style costs nothing — no objects, no textures, no
timers. Do not add a second style to the scene graph to "preview" it.

- **Switching styles takes effect on the next start.** `Settings.setStyleName()`
  persists the choice; `Settings.restartApp()` applies it. There is deliberately
  no live switch.
- **`Styles/Modern/`** is the current skin: `Shell.qml`, `StatusStrip.qml`,
  `Dock.qml`, `SidePanel.qml`, `Screensavers.qml`, `components/` (11 shared
  controls), `pages/` (one per Nav index), `sheets/` (overlays).
- **`Styles/Modern/CONTRACT.md` is required reading before touching any modern
  page** — palette tokens, the component table, and the rules about `sourceSize`
  and gating animations.
- Colours come from `bridges/theme_bridge.py`, which selects a palette from
  `Modules/Runtime/styles.py` and lets `[Modern]` in settings.ini override any
  single token. The few legacy token names still exposed (`textPrimary`, `baseColorA`, …)
  are remapped onto the active palette, so a stray legacy token still renders
  correctly. Type scale lives in `[ModernFonts]` keyed by the same font-group
  roles, so the Font editor keeps working.
- Adding a style = one dict in `styles.py` + a `Styles/<Name>/Shell.qml`.

## Architecture

### Entry point and layers

`main.py` is the QML bootstrap. It:

1. Starts the audio daemon (`Modules.Music.audio_daemon_mgr`) and the `TelemetryCoordinator` singleton.
2. Constructs ten bridge objects (`bridges/*.py`) that subclass `QObject` and expose Python state to QML via `pyqtProperty`/`pyqtSignal`/`pyqtSlot`.
3. Starts `_init_mpris` BEFORE `_init_music_player` — the proxy supervisor waits for the bridge to own the D-Bus name before launching `mpris-proxy`.
4. Loads `main.qml`, registers each bridge as a context property (`Theme`, `Settings`, `Telemetry`, `Nav`, `Alerts`, `Music`, `Modding`, `Soundboard`, `OBD`, `AirPhone`), and uses `QJSEngine.setObjectOwnership(..., CppOwnership)` so QML cannot GC the bridges.
5. Runs a 350 ms `QTimer` driving `_EngineStatePoll.poll()` — this mirrors the old widget-era engine state machine: RPM hysteresis (50 on / 5 off), fuel band hysteresis, coolant alerts, uphill/cold/turnoff badges from telemetry, dash-light → alert mapping (`_DASH_LIGHT_ALERTS`), and hidden-metric → alert mapping (`_HIDDEN_METRIC_ALERTS`). Engine on auto-navigates to page 1 (Dashboard); engine off goes to page 0 (Home).

### Screensavers

Two of them, and the rules differ:

- **Parked** requires the engine off *and* `Telemetry.rpm < 1` *and*
  `Telemetry.speed < 1`. `!Nav.engineOn` alone is a state-machine flag with
  hysteresis in it, so on its own it let the parked screensaver arrive over a
  car that was still rolling.
- **Driving** requires the engine on and speed > 8 km/h, and the **Dashboard is
  the one page with a manual override**: tapping the car toggles it
  (`Nav.toggleDrivingScreensaver()`), and having turned it off there sets
  `Nav.drivingSuppressed`, which stops the idle timer forcing it back *on that
  page only*. Turning it on again clears the pin. A manually raised screensaver
  also survives the shell's "speed dropped, put it away" rule
  (`Nav.screensaverManual`), or asking for it at a red light would be pointless.

**Idle activity comes from an application-wide event filter**
(`_ActivityFilter` in `main.py`) feeding `Nav.notifyActivity()`, never from a
`TapHandler` in the QML tree. A handler at the shell root never sees a press a
child `MouseArea` grabs — which is nearly every button, list row and dock item —
so pressing things did not count as use and the screensaver arrived mid-use.
Changing page counts as activity too. `notifyActivity()` ignores a press within
400 ms of the screensaver being raised, so the press that raises one is not also
the press that dismisses it.

`main.qml` is a frameless 1280×800 `ApplicationWindow` whose one `Loader` loads
`Styles/Modern/Shell.qml`: `StatusStrip` + a `StackLayout` of always-loaded pages +
`SidePanel` (overlay) + `Dock` + `Screensavers`. Page order matches
`Nav.currentPage` (0=Home, 1=Dashboard, 2=Engine, 3=AirPlay, 4=Music,
5=Soundboard, 6=Settings, 7=70mai dashcam, 8=action cam, 9=Trip; 7 and 8 have a
dock pill only while `[Dashcam]` / `[ActionCam] enabled`).

### Live code

Everything in the repo is reachable from `main.py`; the pre-QML QWidget app and
the pre-modern QML skin have been removed. `music.py` holds `MusicPlayer`, which
`MusicBridge` drives through `_MusicGUIShim` (there is no widget GUI).
`Modules/Music/audio_daemon.py` is started as a subprocess by
`audio_daemon_mgr.py`, and `Tools/protocol_fake.py` is loaded by `fake://` ports,
so neither shows up as a normal import.

### Bridges (`bridges/`)

Each bridge owns one subsystem and is the only path between Python and QML for that domain. Pattern:

- State held in private attributes.
- Read-only state exposed as `pyqtProperty` with a paired `changed`-style signal.
- Commands exposed as `pyqtSlot`.
- Background updates either push into Qt via signals (preferred — e.g. `TelemetryCoordinator.changed` → `TelemetryBridge._on_changed`) or via a coalescing `QTimer` to keep the QML re-bind rate sane on the Pi (`TelemetryBridge` rate-limits to ~12 Hz).

`MusicBridge` is the load-bearing one: it owns the `MusicPlayer` (via `_MusicGUIShim`), the MPRIS controller, and a polling timer; it is also responsible for handing the controller to `MPRISBridge` before the proxy is started.

### Runtime services (`Modules/Runtime/`)

- `wifi.py` — nmcli front-end on its own serialised worker. Every entry point
  returns immediately and calls back; nothing here may run on the Qt thread (a
  connect can take most of a minute). nmcli's terse output escapes `:` and `\`,
  and SSIDs contain colons — use `_split_terse`, never `str.split`. With two
  adapters the primary `WifiManager` is pinned to its own device
  (`set_iface`) — unpinned, nmcli merges both radios' scans and `connection
  up` picks either device.
- Network throughput / IPv4 / per-device SSID come from `_SystemInfoCache`
  (`bridges/settings_bridge.py`, 2 s worker: `/proc/net/dev` deltas + one
  ioctl, no subprocess) as `Settings.netRateText/netIp/netIface` and
  `Settings.netInfo(iface)`, notified by `netChanged`. The dock shows the rate
  while parked and the alert summary while driving.
- Cameras. `camstream.py` is the one GStreamer worker (`rtsp://`, `udp://`
  MPEG-TS, `v4l2://`, `appsrc://` sources → RGB frames → a QQuickImageProvider
  per camera: `image://dashcam/<rev>`, `image://actioncam/<rev>`) and
  `bridges/cam_common.py` (`CamBridgeBase`) the rule both camera bridges obey:
  **nothing exists unless needed** — disabled = no thread, no timer; the link
  is watched only while the car is awake (engine on / OBD connected) with
  "Connect at boot" on, or the page is open; status every 30 s (5 s with the
  page showing); the stream only while the page is on screen with no
  screensaver over it (or the reverse overlay is up). PyQt binds a property's
  notify signal to the declaring class, so the shared properties come from
  `cam_props(changed)` in each subclass, not from the base.
  - `dashcam.py` / `bridges/dashcam_bridge.py` (`Dashcam`, Nav 7, dock pill
    "70mai"): the 70mai over its Wi-Fi hotspot — `HotspotLink` (nmcli profile
    pinned to `[Dashcam] iface`, never the default route), signed CGI on :80
    (`md5("<cmd>.cgi?"+query+token)`, pairing = one press on the camera's
    power button), RTSP on :554 (`detect_rtsp`). Its Bluetooth is useless.
  - `actioncam.py` / `bridges/actioncam_bridge.py` (`ActionCam`, Nav 8, pill
    "GoPro"/"Insta360"): `[ActionCam] kind × link`. GoPro = Open GoPro HTTP
    on :8080, identical over the USB network (`UsbNetLink`, camera `.51`,
    vendor 2672 only — never reconfigures another USB adapter) and its AP
    (`HotspotLink`; `GoProBle` turns the AP on over BlueZ, reads SSID/pw);
    webcam / preview = MPEG-TS over UDP to us; `GOPRO_SETTINGS` (ISO, shutter,
    WB, EV… — rows appear only when the camera reports the id). Insta360 =
    Webcam mode over USB (`UvcLink`, picture only) or TCP 6666 protobuf
    (`Insta360Wifi`: published framing; record/photo/stop with all-default
    bodies; battery/live need the model's messages in
    `Data/insta360_messages.json`). Reverse view: CAN `reverse` →
    `Telemetry.reverseGear` → the picture over any page (`Shell.qml`).
  - Connect/disconnect alerts: `CamBridgeBase._set_state` edge-detects the
    online transition → subclass `_link_alert` → `dashcam_link` / `gopro_link`
    / `insta360_link` (icons `GUI/Icons/Dash/{70mai,gopro,insta360}.png`),
    wired via `set_alerts_bridge` in main.py.
  - On the car: `Tools/dashcam_probe.sh` (ignition on) and
    `Tools/actioncam_probe.sh usb|uvc|wifi` first; both write a report home.
    Never charge a camera from the Pi's USB — the supply already browns out.
- `pi_update.py` — `PiUpdater` runs `apt-get update → full-upgrade →
  autoclean` (sudo -n, `APT::Status-Fd` progress) on a worker thread for
  Settings → System → Update Raspberry Pi; state lives in `SettingsBridge`
  (`piUpdate*`) so it survives leaving the page and a `StatusStrip` chip shows
  it everywhere. `LALADASH_PIUPDATE_DEMO=1` rehearses the bar without apt.
- `trip_logger.py` / `trip_analyzer.py` — Settings → Debugger → Trip logging:
  an opt-in per-subsystem recorder (`log(channel,event,**fields)` is a no-op
  unless recording that channel; a daemon writer batches JSON-lines to
  `~/Documents/laladash_logs/`, telemetry trace downsampled via a
  `TelemetryCoordinator` subscription, size-capped) and a schema-free
  `summarize()` report. `bridges/triplog_bridge.py` (`TripLog`) drives it; the
  report shows in `TripReportSheet.qml` and via `Tools/trip_report.py`. Inert
  until Start. Producers add one gated `trip_logger.log(...)` line each.
- `services.py` — Settings → Services master switches for the extras
  (`[Services] dashcam / actioncam / airphone / steering_keys`,
  default on). Off = the Settings section is not listed, the dock pill / page
  is gone and the process is stopped: each feature's own `enabled` is ANDed
  with `service_on()` (camera bridges, `AirPhone.enabled`,
  `Music.mediaKeysEnabled`), and `Settings.servicesChanged` makes main.py
  tear the running ones down. QML re-filters on `Settings.servicesRev`.
- `telemetry_coordinator.py` — singleton thread that polls the active telemetry source. Cadence is state-dependent: 50 ms in sim mode, ~120 ms (~8 Hz) with real OBD, 500 ms when idle. RPM is rate-limited to 5 Hz inside `telemetry.py` and interpolated in the UI. Subscribers register via `subscribe(callback)` and the coordinator emits `changed(snap: dict)`.
- `settings_cache.py` — reads/writes `Data/settings.ini` with caching. Always use
  this rather than reading the INI directly. Scalar reads (`get`/`getboolean`/
  `getint`/`getfloat`) go through `_shared_parser()` and hand back the **cached
  parser itself** — treat it as read-only; call `load_copy()` if you intend to
  `set()` anything. `shared()` is the public alias, for read-only bulk access.
  Cache entries are re-stat'ed at most every 500 ms (`_STAT_TTL_S`), so an
  *external* edit of the INI takes up to that long to be seen; in-process writes
  go through `save()`/`set_value()` and are immediate. This matters: cloning on
  every read cost 951 µs a call and was the app's single largest hot-path cost.
  A duplicate/corrupt section no longer wipes everything: a failed strict parse
  of a non-empty file is re-read with `configparser(strict=False)` (last value
  wins) and the file healed via `_atomic_write` before the `.bak` fallback.
  `save()` also refuses to *drop* sections: a cfg with fewer sections than the
  file on disk (a stale/near-empty read) is rejected, so one bad write can't wipe
  every other setting (the "all my toggles reset on boot" failure).
- `asset_cache.py`, `catalog_cache.py` — memoization for car assets / catalogs.

Telemetry snapshot is a nested dict: `{"RPM": {"value": float, "text": str, "unit": str, "color": str, "badges": [...]}, ...}`. Helpers in `_EngineStatePoll` (`_val`, `_entry`, `_badge`) demonstrate the access pattern — always tolerate missing keys and the legacy flat-number form.

### Theming and colors

`Theme` is a context property backed by `bridges/theme_bridge.py`, which reads through `SettingsBridge` from `[Style]` and `[FontSettings]` in `Data/settings.ini`.

**Light theme.** `Modules/Runtime/styles.py` carries `MODERN_LIGHT` next to `MODERN` (same tokens) as the style's `palette_light`. `Theme.setLightTheme(bool)` persists `[UI] light_theme` and re-emits `Theme.changed`, so every page switches live — which only works because pages use tokens. Never hard-code a UI colour in QML; a telemetry value with no colour of its own must fall back to `Theme.ink` (see `_value_text_color`, which returns "" for "no override"). Per-key `[Modern]` overrides apply to the dark palette only.

The Python side has the parallel set of helpers in `preferences.py`:

- `read_base_rgb()`, `lighten_rgb`, `darken_rgb`, `read_style_color`.
- `load_font_group(name)` for the font-group system (defaults in `FONT_GROUP_DEFAULTS`).
- Status / dash-light / surface / text color constants (`COLOR_STATUS_*`, `TEXT_*`, `SURFACE_*`) — change here and everywhere updates.

Never copy these helpers into another file — every file imports from `preferences.py`.

### Persisted state

- `Data/settings.ini` — all user settings (style, fonts, OBD, alerts, UI toggles). Read through `settings_cache`.
  Sections added since the QML rewrite: `[audio] drop_guard / balance`,
  `[AirPlay] volume / duck_percent / autostart`, `[SteeringKeys] enabled /
  call_duck_percent / key_<code>` (a `key_<code>` of `""` unbinds a default),
  `[Bluetooth] play_on_connect`, `[Trips] day_start_hour`,
  `[Fuel] tank_litres / refuel_min_pct / trip_merge_min` (35 / 8 / 10).
- `Data/music.json`, `Data/music_session.json`, `Data/can_protocol_map.json` — library and session state.
- `~/Documents/laladash_km.json` — odometer, deliberately **outside** the repo so PC→car syncs never overwrite the real km. Seed value: `KM_INITIAL_TOTAL_KM` in `preferences.py`.
  The trip day rolls at **04:00**, not midnight (`[Trips] day_start_hour`, 0-12)
  — a drive that ends at 01:30 belongs to the evening it started in. The day
  key is the *only* thing that resets `today_km`, so on a Pi whose clock does
  not advance between drives (no RTC, no time sync in the car) "Today" stacks
  across real days; `trips_store` logs `[trips] system clock is N h behind …`
  when it detects that, and Settings → Trips shows the stored day key.
- `~/Documents/laladash_fuel.json` — the fuel ledger (`Modules/Settings/fuel_ledger.py`),
  outside the repo for the same reason: tank average €/L (weighted by what was
  left vs. added at each refuel), refuels, finished trips, the open trip
  checkpoint, `k_flow`. Week/month totals group by the 04:00 day key, so they
  are only as right as the Pi clock.

### Trip page (Nav 9)

`Modules/Runtime/trip_recorder.py` is fed by the 350 ms engine poll: a trip
starts at engine-on, pauses at engine-off and is finalised after
`[Fuel] trip_merge_min` off (a fuel stop continues it; an unsynced-clock reboot
never does). Litres are a speed-density estimate for the profile's engine (MAP, IAT,
RPM, commanded λ; overrun cut = 0) × `k_flow`, which each confirmed refuel
re-learns from the real gauge drop. Eco scores (acceleration / gears /
anticipation / idle) and "worth checking" notes (fuel trims, idle stability,
charging voltage, warm-up, coolant peak, MIL) are heuristics; a note seen in
3 of the last 5 trips is "recurring".

Refuel detection uses the **unsmoothed** `Fuel Level.calibrated_value` — the
gauge value is median-of-3 + 0.05 EMA on 60 s samples and would take 20+ min
to show a refuel. Around a stop `TelemetryHub.set_fuel_watch()` reads the tank
every 5 s (20 min after stop, 3 min after start); a rise past
`refuel_min_pct` held ≥ 12 s sets `pending_refuel`, snaps the gauge
(`snap_fuel`) and, at engine-on, main.py opens the Trip page instead of the
Dashboard for the €/L prompt. Engine-off after ≥ 0.5 km opens it for the recap
(and an OBD drop does not pull it back to Home). `TripBridge` (`Trip`) caches
everything; its timer runs only while the page is visible (1 s) or the engine
runs (5 s, for the cost line on Home and the driving screensaver).

### AirPlay

`rpiplay` is a resident process for something wanted occasionally, so it stays
**off until asked for**. `[AirPlay] enabled` only means the receiver is
*available*; `[AirPlay] autostart` (default false) is what runs it from boot.
Neither opening the AirPlay page nor turning the feature on in Settings starts
it — that is the "Start AirPlay" button's job. Both of those used to start it,
which is why the process was effectively always up.

### MPRIS / car head unit

The MPRIS path is fragile. Constraints (do not break):

- `MPRISBridge` must own the D-Bus name before `mpris-proxy` is launched — `main.py` enforces the order.
- The launcher script must pin `DBUS_SESSION_BUS_ADDRESS` to the per-user bus.
- Metadata is pushed on track change (not polled). The proxy supervisor re-resolves the bus on each retry and never publishes on a private bus.

`mpris-proxy` carries only the AVRCP **transport** commands (play/pause/next/
previous/stop). The reference car's stalk Mode/OK button and its two telephone keys
never reach MPRIS at all — they arrive as kernel input events, because BlueZ
turns AVRCP passthrough and HID keys from a paired device into an input device
on `BUS_BLUETOOTH`. `Modules/Music/media_keys.py` reads `/dev/input/event*`
filtered on that bus (the filter is what keeps the touchscreen out) and unpacks
`struct input_event` by hand — deliberately no `evdev` dependency. Reading an
evdev node does not consume the event, so nothing is stolen from other readers.

- Transport key codes are **unbound by default**. MPRIS already delivers those;
  binding them here too would act twice on one press.
- Which code a given car sends is not knowable in advance. Settings → Steering
  Keys shows the last code received so it can be bound by pressing it.
- Needs read access to `/dev/input/event*` (the `input` group). Without it the
  listener opens nothing, reports "No Bluetooth input device", and is otherwise
  inert.

### Split audio

`AudioRouter` builds a null sink (`gtr_bus`) and loopbacks it to both outputs.
Two failure modes are handled and must stay handled:

- **A leg disappearing.** When the head unit's A2DP link drops, PulseAudio
  removes `bluez_output.*` and moves every stream aimed at it — our front-leg
  loopback included — onto the new default sink, which is the device the rear
  leg already feeds. Both legs land on one device, the rear still delay-
  compensated for Bluetooth, and you hear doubled, echoing playback. `SinkWatcher`
  (`pactl subscribe`, event-driven — do not replace it with a timer) drives
  `MusicBridge._check_split_legs()`, which tears the mirror down. `[audio]
  drop_guard`, default on. An empty sink list means pactl failed; treat that as
  "don't know", never as a teardown.
- **pactl is a subprocess.** Nothing that shells out may sit behind a
  `pyqtProperty` — `splitAudioVolume` did, and every `splitAudioChanged` emit
  spawned a process on the Qt thread. Queue it with `MusicBridge._audio_op()`,
  which is one serialised worker because ordering matters (a volume write must
  not overtake a route change).

Front/rear separation is a **level** control (`set_front_rear_balance`), not
just the L/R channel split: on ordinary stereo music the two channels carry
near-identical content, so a hard split alone is inaudible as front-vs-back.

### CAN bus (`Modules/Obd/stn_session.py`, `can_signals.py`, `can_feed.py`)

The car's vLinker FS is an **STN1170**, and the OBD socket carries the body
bus (lights, doors…) alongside the powertrain. `protocol_mode = can` is *not*
a different transport: `OBDManager` keeps the python-obd PID link and attaches
a `StnSession` on the **same serial port** — `STBR` to 2 Mbaud (115200
overflows the adapter in 1.4 s), `STMA`/`STM`+`STFAP` monitoring into a
`FrameStore`, paused around each PID burst by `safe_query()`'s
`pid_window()`. `Data/can_signal_map.json` binds frame bits/bytes to a
catalog of signals; `confirmed` ones feed the hub (`ingest_can`, the PID for
that metric stops polling) and `DashLightsHub.set_can_active()` (the
camera-free dash-light source — the Pi has no camera). The Settings → Debugger
"CAN Debugger" sheet (`bridges/can_bridge.py`, `Can`) is how signals get
found, assigned and confirmed; opening it monitors every ID, closing it goes
back to the mapped-IDs filter. `[OBD] can_backend = python-can` keeps the old
slcan/socketcan path for non-ELM hardware.

Settings → Debugger → Tools also holds the **Lights Simulator**
(`Styles/Modern/sheets/LightsSimSheet.qml`, backed by `Settings.dashLight*`
over `DashLightsHub` overrides — Auto / ON / OFF per light, presets, a Cycle
and a Hazard-blink sequence). Overrides deliberately outlive the sheet so the
forced lights can be seen on the Dashboard and the driving screensaver; "Auto
(clear)" hands every light back to the CAN map / camera. The light catalog
(`DASH_LIGHT_CATALOG`) lives in `Modules/Layout/dash_lights_hub.py`.

Dev PC: `[OBD] port = fake://Data/can_captures/<capture>.txt[?speed=N]`
runs the whole chain on `Tools/protocol_fake.py`, an STN1170 emulator that
replays a real capture. The debug probe's `eval @canSheet opened = true`
opens the sheet headless.

### Audio / engine sound (`Modules/Modding/`)

Engine sound playback is event-driven from `TelemetryCoordinator`, not a 50 ms wall-clock timer. The engine stream's sink is resolved by `ModdingBridge._resolve_engine_sink()`: "Auto" prefers the wired USB card and never a `bluez_*` sink (A2DP adds ~300 ms the driver hears as engine lag). `AudioEngine` writes `[SoundFeatures]` only through `settings_cache` — it used to rewrite the whole INI non-atomically. Smoothing must be cadence-independent. Diagnostic-only PIDs are gated. `MODDING_TIMER_FALLBACK=1` is the explicit rollback switch to the old timer path. Coast samples are RPM-keyed and double as the engine-off cue; the 35% duck applied during coast is the correct default for 1–2 coast files (do not "fix" it). Split-audio: if the split toggle is off, `teardown_bus()` must run — never leave a mirror bus alive when the feature is disabled.

Settings → Debugger has two engine-sound switches, both on `ModdingBridge`: **Sound Log** (`setSoundLog`, `[SoundFeatures] sound_log_enabled`) prints `AudioEngine.get_debug_state()` as `[sound] …` at most every 500 ms; **Test Mode** (`setTestMode`) drives the engine from a synthetic 24 s drive (`_test_sample`) on a 50 ms timer that runs only while test mode is on and a preset other than Quiet is active. Test Mode is session-only by design (reset to off at every start) so the car can never boot into fake engine sound.

### Noob Mode (Settings → Driving Mode)

`Modules/Runtime/noob_coach.py` is a pure rules class (`NoobCoach.update()` — numbers in, alert states out; tested in `tests/test_noob_coach.py`). `_EngineStatePoll._noob_tick()` in `main.py` feeds it from the existing 350 ms poll — no timer of its own — and forwards the states to `AlertsBridge` as the `noob_*` keys (catalog category "Coach"). With `[SoundFeatures] noob_mode_enabled` off it is one cached bool lookup. Rules: Starting (low revs pulling away in 1st; a stall within 1 s of running), Rpm (shift up/down from `[GearBands]` `up_orange`/`down_orange`, held 1.5 s), Speeding (`noob_speed_limit`, 3 km/h hysteresis), UpHill (the `uphill_assist` badge plus low revs). Texts are fixed strings (the static-alert-text rule).

## Conventions

- UI naming: `Styles/Modern/CONTRACT.md` — palette tokens, shared components and page rules for the live skin.
- Page-lifecycle hooks in QML use `onVisibleChanged` (pages stay loaded; visibility is the cue).
- When adding a feature, gate background work on the page being visible. A feature that polls while its page is hidden is broken on the Pi 4B.
- Default to event-driven (`pyqtSignal`, D-Bus, kernel waits) over `QTimer`. If you must use a timer, justify the interval and coalesce QML re-emits.
- Side-panel state (`_manual_side_panel_open`) persists across page changes and is only reset on engine on/off transitions.

## Common gotchas

- The Qt log filter in `main.py` swallows `OpenType support missing` and font-alias warnings. Don't add prints that would be filtered by the same patterns.
- `cleanup()` order on exit matters: stop the engine poll timer first, then the modding bridge, then music polling, then MPRIS, then the music player, then `TelemetryCoordinator`, then the audio daemon. The shutdown path in `main.py` is the canonical sequence.
- `_boot_obd` runs the ELM327 handshake on a daemon thread — never call `OBDManager.instance().quick_connect()` from the Qt main thread.
- PID cadence lives in `telemetry.py` tiers: `_DIAG_ONLY_PIDS` (Diagnostics page only), `_SLOW_ALWAYS_PIDS` (alert feeders, 3 s), `_DASHBOARD_PIDS` (0.5 s while the Dashboard is the page — `set_dashboard_visible()` from `main.py`). A metric a page draws must not sit in the diagnostics-only set; that is how Timing/Lambda/Pedal read N/A in the car.
- **Never hand a large list property to QML.** A QML `property var` assigned
  from a `pyqtProperty(list)` does *not* snapshot it — it holds a sequence
  wrapper that re-reads the property on **every index access**, and PyQt
  rebuilds the whole `QVariantList` on each read. `MusicPage` looping over a
  3470-track `Music.visibleTracks` snapshot to find the playing track
  therefore re-marshalled the entire library once per row and blocked the
  main thread for **33 s on a single track change** (measured 2027 getter
  calls for one `next()`). Do the windowing in Python and return only the
  rows QML will draw — see `MusicBridge.upNext()`. Any list getter that gets
  read more than once must also cache its materialised form
  (`_visible_items` / `_visible_index`).
- When the main thread wedges, the debug probe socket cannot answer — it is
  served from the same event loop. Use `kill -USR1 <pid>` instead: with
  `LALADASH_DEBUG=1` that dumps every thread's Python stack to stderr from
  the signal handler. Firing it repeatedly and tallying the frames is a
  poor-man's sampling profiler and is how the above was found.
