# CAN captures from the reference car's OBD socket

> The capture `.txt` files are personal and gitignored. Record your own with
> `Tools/can_capture.py`; `Tools/protocol_fake.py` replays any of them.

Recorded with `Tools/can_capture.py` through the vLinker FS. The adapter is an
**STN1170 v4.3.2** (`STI`), i.e. an OBDLink-class chip: it switches its UART
to 2 Mbaud with `STBR` (volatile — `ATZ` puts it back on 115200) and monitors
with `STMA`. At the stock 115200 the bus overflows the adapter in 1.4 s
(`BUFFER FULL`) and ELM `ATMA` aborts — the first attempt got 887 frames.

The app owns the port while it runs, so the recorder needs
`systemctl --user stop laladash.service` first (the screen goes dark).

## reference_2026-09-15_lights_ignition-on.txt

90 s, ignition on, engine off (`ATRV` 12.2 V), 122 621 frames, 45 IDs,
~1360 frames/s, no overflow. Bus: ISO 15765-4 CAN 11-bit 500 kbit/s on pins
6/14 — and it **does** carry the body computer, not just the powertrain.

Lights were toggled during the capture (low beam → high beam → low → off →
hazards → brake taps). What moved, and what it means:

| ID | byte.bit | rate | meaning | evidence |
|---|---|---|---|---|
| `0x5DE` | b0.1 (`0x02`) | ~6 Hz | **low beam on** | 16.6↑ 32.5↓ 42.9↑ 44.0↓ |
| `0x5DE` | b0.0 (`0x01`) | | **high beam on** | 32.5↑ 42.9↓ (`0x07` for one frame while the stalk passes through) |
| `0x5DE` | b0.5 + b0.6 (`0x60`) | | **turn indicators, left / right lamp** | both blinking at 0.34 s, 44.9→61.5 (hazards) |
| `0x5DE` | b0.2 (`0x04`) | | on from the start, off with the rest at 44.0 — DRL / position lights (to confirm) | |
| `0x211` | b5.3 / b5.4 | 25 Hz | cluster echo of low / high beam | same transition times |
| `0x55D` | b5.6 / b5.5 | ~6 Hz | second echo of low / high beam | same transition times |
| `0x350` | b7.4 | ~6 Hz | hazard switch active | 44.9↑ 61.7↓ |
| `0x12E` | b2.2 | 50 Hz | short pulses at 65.3 / 65.7 / 66.0 / 72.3 — brake pedal taps (to confirm) | |

Always-changing bits that are *not* signals: `0x511` (rolling counters in
b1–b6), `0x352` b4.3 / b5.3–5.5 (alive counter at 0.32 s), `0x350` b3
(incrementing counter).

Static during this capture (engine off): `0x186` `00003063290021` (engine
frame — RPM field will move with the engine running), `0x1F6`, `0x214`,
`0x1B0`, `0x217`, `0x2B7`, `0x29C`, `0x4F8`, `0x45C`, `0x634`, `0x2B9`,
`0x218`, `0x4FA`, `0x671`, `0x68B`, `0x4DE`, `0x5D7`, `0x5DA`, `0x648`,
`0x65C`, `0x66A`, `0x5DF`, `0x666`, `0x657`.

## reference_2026-09-18_quick_lights.txt

20 s, ignition on, engine off (`ATRV` 11.9 V), 28 624 frames, **52 IDs**,
~1430 frames/s, no overflow, UART 2 Mbaud. A short opportunistic window (the
car was about to be driven), so only the *tail* of the light toggling landed
inside it — everything happened in the first 2.7 s.

**Captured with the app stopped**, and that is the important operational
finding: the app's own STN session dies constantly in the car
(`Bad file descriptor` / `Device disconnected while reading`) because the Pi
is browning out (`get_throttled` 0x50005, under-voltage every 5–18 s), but
with `laladash.service` stopped the screen and audio are off, draw drops, and
`Tools/can_capture.py` records cleanly. **Capture with the service stopped;
do not trust the in-app CAN monitor until the 5 V supply is fixed.**

`0x5DE` b0 value timeline — the whole light story in one column:

| t | b0 | meaning |
|---|---|---|
| 0.04 | `0x64` | position/DRL (b0.2) + both indicator lamps (b0.5+b0.6) |
| 0.04 | `0x60` | **position/DRL off**, indicators still lit |
| 0.25 → 2.65 | `0x60` ↔ `0x00` every ~0.34 s | **hazards blinking** |
| 2.67 | `0x00` | hazards off, light system idle for the remaining 17 s |

Confirmed by this capture (were "to confirm" before):

- **`0x350` b7.4 = hazard switch** — byte7 `0x55`→`0x45` at **2.69 s**, i.e.
  0.02 s after the lamps stop blinking at 2.67 s. Independent cross-check of
  both signals; this pair can be marked `confirmed` in `can_signal_map.json`.
- **`0x5DE` b0.5 + b0.6 = indicator lamps** — the 0.34 s blink, both together
  (hazards), matching the 09-15 observation exactly.
- **`0x5DE` b0.2 = position / DRL** — seen on, then switched off.
- **`0x211` b5 = cluster light echo** — constant `0x00` for the whole run,
  consistent with low/high beam never being lit (see below).

Not obtained here: **low and high beam never came on** (`0x5DE` b0.0/b0.1 and
`0x211` b5.3/b5.4 never changed), so no per-side indicator split either —
hazards drive b0.5 and b0.6 *together*, and telling left from right still
needs one indicator at a time.

**7 new IDs** appear vs 09-15 (a strict superset, nothing lost):
`0x2BC` `0x500` `0x552` `0x563` `0x575` `0x62C` `0x653`. `0x2BC` b3.0–b3.2
move continuously — likely chassis/steering, not yet identified.

## Still to capture

Ignition-on only (no engine needed):

- **doors**, **seatbelt**, **handbrake**, **fog lights**
- **one indicator at a time** — which of b0.5 / b0.6 is left
- **low / high beam** — a clean on→off→on while recording
- **reverse gear** — needed by the reverse-camera overlay
  (`Telemetry.reverseGear`), never yet captured
- **the warning lamps** — the bulb-check phase, so start the capture *before*
  turning the key

Engine running: RPM / throttle / clutch bytes (`0x186` is static at
`00003063290021` with the engine off).
