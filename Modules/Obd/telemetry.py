# Modules/telemetry.py
import os
import threading
import time
import configparser
from collections import deque
from typing import Any, Dict, Optional, List, Tuple

import obd
from Modules.Obd.obd_connection import OBDManager, safe_query
from Modules.Runtime import settings_cache
from Modules.Runtime import vehicle_profile as _vp
from preferences import (
    DATA_SETTINGS,
    COLOR_STATUS_GREEN,
    COLOR_STATUS_YELLOW,
    COLOR_STATUS_ORANGE,
    COLOR_STATUS_RED,
    COLOR_STATUS_NA,
    TEXT_VALUE_BLUE,
)

# =========================
# Colors — aliases pointing to centralized constants in preferences.py
# =========================
ColorGREEN  = COLOR_STATUS_GREEN
ColorYELLOW = COLOR_STATUS_YELLOW
ColorORANGE = COLOR_STATUS_ORANGE
ColorRED    = COLOR_STATUS_RED
ColorNA     = COLOR_STATUS_NA

def band_higher_4(v: float, green_max: float, yellow_max: float, orange_max: float) -> str:
    if v <= green_max:  return ColorGREEN
    if v <= yellow_max: return ColorYELLOW
    if v <= orange_max: return ColorORANGE
    return ColorRED

def band_lower_4(v: float, red_min: float, orange_min: float, yellow_min: float) -> str:
    if v < red_min:      return ColorRED
    if v < orange_min:   return ColorORANGE
    if v < yellow_min:   return ColorYELLOW
    return ColorGREEN

# Gauge bands: (warn, danger, redline, full-scale).
#
# The first three are the thresholds band_higher_4 uses to colour the value;
# the fourth is where a bargraph ends. They live here, beside METRICS and
# feeding the colour lambdas below, so a gauge's scale and its colour can
# never drift apart — the driving screensaver reads them through
# TelemetryBridge.gaugeBand().
GAUGE_BANDS: dict[str, tuple[float, float, float, float]] = {
    k: tuple(v) for k, v in _vp.get("gauge_bands").items()
}


def band_range_4(v: float, g_lo: float, g_hi: float, y_margin: float, o_margin: float) -> str:
    if g_lo <= v <= g_hi:
        return ColorGREEN
    d = (g_lo - v) if v < g_lo else (v - g_hi)
    if d <= y_margin: return ColorYELLOW
    if d <= o_margin: return ColorORANGE
    return ColorRED


def _cfg_load_settings() -> configparser.ConfigParser:
    """Read-only view of Data/settings.ini, shared through settings_cache.

    This used to open and parse the 8.4 KB INI from disk on every call, and
    `update()` calls it (via reload_driving_flags) on every tick — 8 Hz with
    real OBD, 20 Hz in sim. That parse holds the GIL, so it stalled the Qt main
    thread from the coordinator thread. Every caller here only reads, so the
    cached parser can be shared directly.
    """
    try:
        return settings_cache.shared(DATA_SETTINGS)
    except Exception:
        cfg = configparser.ConfigParser()
        try:
            if os.path.exists(DATA_SETTINGS):
                cfg.read(DATA_SETTINGS, encoding="utf-8")
        except Exception:
            pass
        return cfg


def _ui_getbool(key: str, default: bool = False) -> bool:
    cfg = _cfg_load_settings()
    raw = cfg.get("UI", key, fallback="1" if default else "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


# =========================
# Tooltips
# =========================
TOOLTIPS: Dict[str, str] = {
    "RPM": """Definition: Engine speed (revolutions per minute).
Why it matters: Too low → stall risk; too high → mechanical stress.
Sensor: Crankshaft position sensor to ECU.
In your project (OBD):
- Idle: Good 700–900 rpm. Low <600 (shaky idle, near stall). High >1100 (surging, clutch/grab feels odd).
- Cruise: Good 2000–3000 rpm (quiet, smooth). High for speed ⇒ short gear / clutch slip (engine noisy, speed doesn’t rise equally).
- Near redline: OK brief 5500–6200 (strong pull). Very high sustained ⇒ harsh sound, vibration, power tapers.""",

    "Speed": """Definition: Vehicle road speed from ECU (km/h).
Why it matters: Basis for gear calc, ABS/ESC, navigation.
Sensor: Wheel speed / vehicle speed sensor via ABS/ECU.
In your project (OBD):
- Contextual metric. Mismatch vs cluster/GPS (reads too high/low) feels like “wrong speed shown”. Dropouts feel like sudden jumps or frozen value while moving.""",

    "Coolant": """Definition: Engine coolant temperature (°C).
Why it matters: Too cold → inefficient; too hot → damage risk.
Sensor: ECT sensor in coolant passage.
In your project (OBD):
- Good: 85–95 °C (stable temp, normal heater performance).
- OK (fan cycling): 100–105 °C (fan audible; temp nudges down/up).
- High >110 °C (hot smell, reduced power, possible warning; cabin heat may spike then fade). Low <70 °C (sluggish warm-up, higher fuel use).""",

    "Intake Air": """Definition: Temperature of incoming air (°C).
Why it matters: Cooler air = denser = more power.
Sensor: IAT sensor (in MAF/MAP tract).
In your project (OBD):
- Moving: Good ambient to ambient+10–20 °C (engine feels crisp).
- Heat-soak: OK ambient+20–40 °C after a stop (slightly lazy throttle).
- High >60 °C while moving (flat response, knock-retard feel). Very low near freezing (strong, eager pull).""",

    "Oil Temp": """Definition: Engine oil temperature (°C).
Why it matters: Oil must be warm to protect; too hot thins oil.
Sensor: Oil temp sensor (if equipped) or ECU estimate.
In your project (OBD):
- Good: 90–110 °C (smooth, quiet, confident pulls).
- Cool 70–90 °C (needs gentle throttle; feels a bit stiff).
- Hot >120 °C (engine feels stressed under load; smell; power may soften).""",

    "Manifold Press": """Definition: Manifold absolute pressure (kPa) — load/boost.
Why it matters: Shows vacuum health and boost on turbo engines.
Sensor: MAP sensor.
Reference engine (OBD) — TCe 90 turbo petrol (H4Bt):
- Idle: Good 30–50 kPa (vacuum, steady idle). High >60 kPa at idle (rough idle).
- Cruise: Good 70–100 kPa (light throttle, partial vacuum).
- Moderate acceleration: 110–150 kPa (boost building, feels responsive).
- WOT boost: Good 155–195 kPa (strong turbo pull). Low <130 kPa at WOT (weak boost, flat feel, possible boost leak or wastegate issue).
- Overboost >200 kPa (unusual for stock; possible fault).
ECO mode: boost is limited — expect ≤140 kPa even under hard throttle.""",

    "Baro Press": """Definition: Ambient barometric pressure (kPa).
Why it matters: Altitude/fueling corrections.
Sensor: Baro (standalone or in MAP).
In your project (OBD):
- Sea level Good ~98–103 kPa. Implausible low at sea level feels like slightly off fueling (sluggish or over-rich smell); high altitude naturally lower with softer power.""",

    "Throttle Pos": """Definition: Throttle plate opening (%).
Why it matters: Controls airflow/power.
Sensor: Throttle position sensor(s) / ETC.
In your project (OBD):
- Idle Good ~0–5% (stable idle). High idle >7% (surging, creep feel).
- Cruise 10–35% (linear response).
- WOT 90–100% (full power feel). If WOT shows low %, car feels capped / won’t rev freely.""",

    "Rel. Throttle": """Definition: Throttle opening normalized to learned min/max (%).
Why it matters: Removes calibration drift, shows real intent.
Sensor: ECU-computed from TPS.
In your project (OBD):
- Tracks “Throttle Pos” bands. If it lags driver feel, you’ll sense delayed response; if it jumps, throttle feels snatchy.""",

    "Accel Pedal D": """Definition: Driver accelerator pedal position (%).
Why it matters: Compare driver demand vs actual throttle.
Sensor: Dual-track pedal sensor.
In your project (OBD):
- 0–100% smooth sweep (predictable response).
- Flat spots/jumps feel like hesitation or sudden lurch; mismatch to throttle feels like “I press but nothing, then it goes.”""",

    "Absolute Load": """Definition: ECU-calculated load (% of max).
Why it matters: High load = higher temps/fuel.
Sensor: Derived from MAF/MAP, RPM, VE.
In your project (OBD):
- Idle Good 15–30% (calm idle). High >40% at idle feels rough/misfiring.
- Cruise Good 25–50% (easy, quiet). Very high at light throttle feels bogged.
- WOT Good 85–100% (strong pull). Low <75% at WOT feels restricted/lifeless.""",

    "Timing Adv": """Definition: Ignition timing advance (°BTDC).
Why it matters: Too little → hot exhaust/weak power; too much → knock.
Sensor: ECU control with knock feedback.
In your project (OBD):
- Idle Good ~5–15° (stable, smooth). Very low/negative feels lumpy, weak.
- Cruise Good 25–40° (easy power). Persistent low advance feels dull, hotter sound.
- WOT 10–25° (varies). Knock-retard feels like brief stutters/softening under load.""",

    "Fuel Level": """Definition: Estimated fuel in tank (%).
Why it matters: Low fuel can starve pump on corners and heat it.
Sensor: Tank sender, ECU-smoothed.
In your project (OBD):
- Good 30–80% (no starvation).
- Low <10–12% (on hard turns or braking: brief stumble or cut).
- Full 90–100% (heavier feel from weight).""",

    "Fuel Pressure": """Definition: Force of fuel delivery to injectors.
Why it matters: Too low → lean/misfire; too high → rich/fouling.
Sensor: Fuel rail pressure sensor.
In your project (OBD) — Port injection (vac-ref):
- Idle Good 300–350 kPa (3.0–3.5 bar). Low <280 (hesitation, shaky idle). High >400 (rich smell, dull throttle).
- Load/WOT Good 350–400 kPa. Low <320 (falls flat at high rpm). High >450 (sooty exhaust, sluggish).
Direct-Injection:
- Idle Good 5–7 MPa (50–70 bar). Low (stumble at tip-in). Load Good 10–20+ MPa; Low feels like power fades above mid-rpm.""",

    "Rail P (vac)": """Definition: Vacuum-referenced port-injection rail pressure (kPa).
Why it matters: Keeps injector delta-P constant vs manifold vacuum.
Sensor: Rail pressure + vac reference/regulator.
In your project (OBD):
- Matches Fuel Pressure bands. Low at idle feels rough/lean; high feels rich and lazy; swings feel like surging.""",

    "Rail P (DI)": """Definition: High-pressure rail for direct injection (kPa/MPa).
Why it matters: Must rise fast with load.
Sensor: DI rail pressure sensor + cam-driven HP pump.
In your project (OBD):
- Idle Good 5–7 MPa (smooth). Low (tremble at idle, hard hot restarts).
- Load Good 10–20+ MPa. Low under acceleration feels like strong pull begins then fades, possible misfire at high rpm.""",

    "Equiv. Ratio": """Definition: Air–fuel equivalence λ (1.00 = stoich).
Why it matters: Lean → hot/knock; rich → power loss/emissions.
Sensor: Wideband O₂ + ECU calc.
In your project (OBD):
- Idle/Cruise Good λ ≈ 1.00±0.03 (clean, smooth). Lean >1.10 (surge, ping on climb). Rich <0.90 (soft response, fuel smell).
- WOT Good λ ~0.85–0.92 (strong, cool pull). Too lean at WOT feels edgy, rattly; too rich feels muffled, flat.""",

    "ECU Voltage": """Definition: ECU supply voltage (V).
Why it matters: Low → resets/sensor errors; high → damage risk.
Sensor: ECU internal reading.
In your project (OBD):
- Running Good 13.8–14.7 V (lights bright, starter crisp).
- Low <12.5 V running (dim lights, sluggish throttle/idle oddities).
- High >15.0 V (whine from alternator, electronics act strange).""",

    "ELM Voltage": """Definition: Voltage at OBD adapter (V).
Why it matters: Should match ECU voltage closely.
Sensor: Adapter ADC.
In your project (OBD):
- Good within ~0.2–0.4 V of ECU. Large drop >0.7 V feels like random OBD disconnects or flaky readings while car itself feels normal.""",

    "Run Time": """Definition: Seconds since engine start.
Why it matters: Context for warm-up/drive cycles.
Sensor: ECU timer.
In your project (OBD):
- Not a health band. Early minutes feel like richer, slightly lazy response; after warm-up response sharpens.""",

    "Dist. since clear": """Definition: Kilometers since DTCs were cleared.
Why it matters: Readiness monitors need drive time.
Sensor: ECU counter.
In your project (OBD):
- Low distance after a sale/repair may feel fine but indicates monitors not proven; expect monitors to set after varied driving.""",

    "Warmups since clear": """Definition: Number of full warm-up cycles since DTC clear.
Why it matters: Indicates system has been exercised.
Sensor: ECU counter.
In your project (OBD):
- Low count means “recent reset”; car may feel normal but tests (readiness) not complete.""",

    "DTC Count": """Definition: Number of stored diagnostic trouble codes (mode-01 PID 01), plus MIL lamp state.
Why it matters: A lit MIL (check-engine) is the ECU asking for attention.
Sensor: ECU status word.
In your project (OBD):
- Polled every 30 s; the check-engine alert follows the MIL bit, the tile shows the count.""",

    "Gear": """Definition: Estimated gear from RPM/Speed ratio (with hysteresis).
Why it matters: Useful for shift points and slip detection.
Sensor: Derived (no direct sensor on many manuals).
In your project (OBD):
- Stable gear at steady throttle feels consistent.
- If gear jumps at constant speed, you feel flare (engine revs rise without speed = clutch slip) or brief wheelspin on bumps.""",
}


# =========================
# Formatting helpers
# =========================
def _fmt_int(x: float) -> str:    return f"{int(round(x))}"
def _fmt_pct(x: float) -> str:    return f"{int(round(x))}%"
def _fmt_2dp(x: float) -> str:    return f"{x:.2f}"
def _fmt_1dp(x: float) -> str:    return f"{x:.1f}"

MetricEntry = Dict[str, Any]

# =========================
# Metric registry (thresholds come from the vehicle profile)
# =========================
METRIC_REGISTRY: Dict[str, MetricEntry] = {
    # Powertrain — warning bands from Data/vehicles/default.json (reference: small turbo petrol)
    #
    # RPM: warn near the 6 000 rpm redline, not at routine cruising rpm.
    "RPM":          {"cmd": obd.commands.RPM,                 "unit": "rpm", "fmt": _fmt_int, "color": lambda v: band_higher_4(v, *GAUGE_BANDS["RPM"][:3]), "alpha": 0.0,  "engine_only": False},
    "Speed":        {"cmd": obd.commands.SPEED,               "unit": "km/h","fmt": _fmt_int, "color": lambda v: band_higher_4(v, *GAUGE_BANDS["Speed"][:3]),   "alpha": 0.0,  "engine_only": False},
    # Coolant: green 85-100 °C (normal op + fan cycling), margins 10/20 °C each side.
    # red fires below 65 °C (very cold — blue text overlay kicks in first) or above 120 °C.
    "Coolant":      {"cmd": obd.commands.COOLANT_TEMP,        "unit": "°C",  "fmt": _fmt_int, "color": lambda v: band_range_4(v, *_vp.get("metric_bands.Coolant")),   "alpha": 0.10, "engine_only": False},
    "Intake Air":   {"cmd": obd.commands.INTAKE_TEMP,         "unit": "°C",  "fmt": _fmt_int, "color": lambda v: band_higher_4(v, *_vp.get("metric_bands.Intake Air")),       "alpha": 0.10, "engine_only": False},
    # Oil Temp: two-sided band — cold oil is also bad. green 85-120 °C.
    # red below 65 °C (cold start — blue text overlay) or above 140 °C (danger).
    "Oil Temp":     {"cmd": obd.commands.OIL_TEMP,            "unit": "°C",  "fmt": _fmt_int, "color": lambda v: band_range_4(v, *_vp.get("metric_bands.Oil Temp")),   "alpha": 0.10, "engine_only": False},
    # Manifold Press: reference engine WOT boost ≈ 170-200 kPa absolute.
    # Old 100/120/140 kPa would show orange/red during any meaningful boost pull.
    "Manifold Press":{"cmd": obd.commands.INTAKE_PRESSURE,    "unit": "kPa", "fmt": _fmt_int, "color": lambda v: band_higher_4(v, *_vp.get("metric_bands.Manifold Press")),    "alpha": 0.25, "engine_only": True},
    "Baro Press":   {"cmd": obd.commands.BAROMETRIC_PRESSURE, "unit": "kPa", "fmt": _fmt_int, "color": lambda v: band_range_4(v, 80, 105, 5, 10),    "alpha": 0.05, "engine_only": False},

    # Driver input / load
    "Throttle Pos": {"cmd": obd.commands.THROTTLE_POS,        "unit": "%",   "fmt": _fmt_pct, "color": lambda v: band_higher_4(v, 60, 80, 90),       "alpha": 0.25, "engine_only": True},
    "Rel. Throttle":{"cmd": obd.commands.RELATIVE_THROTTLE_POS,"unit": "%",  "fmt": _fmt_pct, "color": lambda v: band_higher_4(v, 60, 80, 90),       "alpha": 0.25, "engine_only": True},
    "Accel Pedal D":{"cmd": obd.commands.ACCELERATOR_POS_D,   "unit": "%",   "fmt": _fmt_pct, "color": lambda v: band_higher_4(v, 60, 80, 90),       "alpha": 0.25, "engine_only": True},
    "Absolute Load":{"cmd": obd.commands.ABSOLUTE_LOAD,       "unit": "%",   "fmt": _fmt_pct, "color": lambda v: band_higher_4(v, 70, 85, 92),       "alpha": 0.25, "engine_only": True},
    "Timing Adv":   {"cmd": obd.commands.TIMING_ADVANCE,      "unit": "°",   "fmt": _fmt_1dp, "color": lambda v: band_range_4(v, 5, 45, 8, 16),      "alpha": 0.25, "engine_only": True},

    # Fuel / electrical
    "Fuel Level":   {"cmd": obd.commands.FUEL_LEVEL,          "unit": "%",   "fmt": _fmt_pct, "color": lambda v: band_lower_4(v, 10, 15, 25),        "alpha": 0.05, "engine_only": False},
    "Fuel Pressure":{"cmd": obd.commands.FUEL_PRESSURE,       "unit": "kPa", "fmt": _fmt_int, "color": lambda v: band_higher_4(v, 400, 450, 500),    "alpha": 0.25, "engine_only": True},
    "Rail P (vac)": {"cmd": obd.commands.FUEL_RAIL_PRESSURE_VAC,"unit":"kPa","fmt": _fmt_int, "color": lambda v: band_higher_4(v, 300, 400, 500),    "alpha": 0.25, "engine_only": True},
    "Rail P (DI)":  {"cmd": obd.commands.FUEL_RAIL_PRESSURE_DIRECT,"unit":"kPa","fmt": _fmt_int,"color": lambda v: band_higher_4(v, 8000, 10000, 12000),"alpha": 0.25,"engine_only": True},
    "Equiv. Ratio": {"cmd": obd.commands.COMMANDED_EQUIV_RATIO,"unit": "λ",  "fmt": _fmt_2dp, "color": lambda v: band_range_4(v, 0.98, 1.02, 0.06, 0.12),"alpha": 0.25,"engine_only": True},
    # Fuel trims (bank 1): how far the ECU corrects the base mixture. A long
    # term trim stuck past ±10 % is the classic sign of an intake air leak
    # (lean, +) or a rich fault (−) — the Trip page's "worth checking".
    "Fuel Trim S":  {"cmd": obd.commands.SHORT_FUEL_TRIM_1,   "unit": "%",   "fmt": _fmt_1dp, "color": lambda v: band_range_4(v, -10, 10, 5, 10),     "alpha": 0.25, "engine_only": True},
    "Fuel Trim L":  {"cmd": obd.commands.LONG_FUEL_TRIM_1,    "unit": "%",   "fmt": _fmt_1dp, "color": lambda v: band_range_4(v, -8, 8, 4, 8),        "alpha": 0.10, "engine_only": True},

    "ECU Voltage":  {"cmd": obd.commands.CONTROL_MODULE_VOLTAGE,"unit":"V",  "fmt": _fmt_2dp, "color": lambda v: band_range_4(v, 13.0, 14.7, 0.5, 1.0),"alpha": 0.10,"engine_only": False},
    "ELM Voltage":  {"cmd": obd.commands.ELM_VOLTAGE,          "unit":"V",   "fmt": _fmt_2dp, "color": lambda v: band_range_4(v, 13.0, 14.7, 0.5, 1.0),"alpha": 0.10,"engine_only": False},

    # Counters
    "Run Time":     {"cmd": obd.commands.RUN_TIME,             "unit":"s",   "fmt": _fmt_int, "color": lambda v: ColorGREEN,                           "alpha": 0.0,  "engine_only": True},
    "Dist. since clear":{"cmd": obd.commands.DISTANCE_SINCE_DTC_CLEAR,"unit":"km","fmt": _fmt_int, "color": lambda v: ColorGREEN,                    "alpha": 0.0,  "engine_only": False},
    "Warmups since clear":{"cmd": obd.commands.WARMUPS_SINCE_DTC_CLEAR,"unit":"", "fmt": _fmt_int, "color": lambda v: ColorGREEN,                    "alpha": 0.0,  "engine_only": False},
}


# =========================
# Poll cadence (seconds) per metric
# =========================
DEFAULT_POLL = 1.0
POLL_SECS: Dict[str, float] = {
    # These are ceilings, not promises: each tick queries what is due within
    # the per-tick budget (dynamic, from measured adapter latency — see
    # _query_budget()). RPM feeds the engine sound and always goes first.
    # The vLinker FS in the car answers in ~16 ms, so 10 Hz RPM costs the
    # bus nothing; a slow Bluetooth clone just gets the budget clamped down.
    "RPM": 0.10,                         # ~10 Hz on a fast adapter
    "Speed": 0.20,
    "Throttle Pos": 0.25,
    "Rel. Throttle": 0.35,
    "Accel Pedal D": 0.35,
    "Absolute Load": 0.35,
    "Timing Adv": 0.5,
    "Manifold Press": 0.5,
    "Equiv. Ratio": 0.25,
    "Fuel Trim S": 3.0,
    "Fuel Trim L": 3.0,
    # Keep thermal/electrical tiles closely in sync with dashboard rendering.
    "Coolant": 0.5,
    "Intake Air": 1.5,
    "Oil Temp": 0.6,
    "Baro Press": 2.0,
    "ECU Voltage": 0.6,
    "ELM Voltage": 5.0,                  # only the battery-tile fallback
    "Fuel Level": 60.0,                  # fuel changes on minute scale; 60 s halves ELM serial load
    "Run Time": 1.0,
    "Dist. since clear": 5.0,
    "Warmups since clear": 5.0,
    "DTC Count": 30.0,                   # mode-01 PID 01: MIL + stored-code count
}

# =========================
# General settings
# =========================
# =========================
# Eco + Warm-up + Turn-off safety
# =========================
ICON_COLD = "GUI/Icons/Dash/cold.png" if os.path.exists("GUI/Icons/Dash/cold.png") else "GUI/Icons/cold.png"
ICON_TURNOFF = "GUI/Icons/Dash/turn_off.png" if os.path.exists("GUI/Icons/Dash/turn_off.png") else "GUI/Icons/turn_off.png"

# Warm-up thresholds (°C) — blue text / cold badge until engine is truly warm.
# TCe 90 thermostat begins opening ~82-88 °C; oil is effective from ~80 °C.
ENGINE_COLD_COOLANT_C = 75.0   # blue overlay until coolant is clearly warm
ENGINE_COLD_OIL_C     = 65.0   # blue overlay until oil is clearly warm

# Hot thresholds (°C) — arms the idle-circulation LATCH after a demanding run.
# 85–87 °C is normal operating temperature for the TCe 90; anything at or above
# that means the turbo has been running fully loaded and needs oil circulation.
ENGINE_HOT_COOLANT_C = 87.0    # normal warm operating range — turbo fully loaded
ENGINE_HOT_OIL_C     = 90.0    # warm oil; not extreme but circulation is needed

# Direct "not safe right now" thresholds — checked against the CURRENT reading
# at the moment you stop, independent of the latch above.
# These sit higher: they indicate the engine is still actively dissipating heat
# and turning off now would cut the water pump / oil circulation mid-cooldown.
TURNOFF_UNSAFE_COOLANT_C = 97.0    # fan-cycling territory; coolant still very hot
TURNOFF_UNSAFE_OIL_C     = 110.0   # oil critically hot; turbo needs more circulation

# "Idle circulation" after a hot/long run (seconds).
# 60 s covers the H4Bt water-cooled CHRA cooldown at typical 85–90 °C driving.
MIN_IDLE_CIRCULATION_SEC = 60.0

# Minimum engine run time before the turn-off safety alert becomes active.
# The TCe 90 oil can't get meaningfully hot in under 5 minutes, so there's
# nothing to warn about when the engine was just started.
MIN_ENGINE_RUN_BEFORE_ALERT_SEC = 300.0  # 5 minutes

# Long run threshold — after this many seconds of running, require cooldown
# regardless of whether temperature thresholds were reached (e.g. sensor lag,
# or a 50-minute highway cruise at 85 °C that never peaked above ENGINE_HOT_COOLANT_C).
MIN_RUN_FOR_COOLDOWN_SEC = 1800.0  # 30 minutes

# Safety conditions for shutdown
SAFE_IDLE_RPM = 1100.0
SAFE_STOP_SPEED_KMH = 1.0

# UI text colour — alias pointing to the centralized constant
VALUE_TEXT_BLUE  = TEXT_VALUE_BLUE

STALE_AFTER = 2.0               # seconds → mark N/A
SPEED_DEADBAND = 1.0            # km/h → snap to 0

# Engine-off colouring for the supply-voltage tiles (see update()): green
# 12.0-13.2 V, yellow down to 11.6, orange down to 11.3, red below. Measured
# on the reference car with ignition on, engine off, R-Link + screen + Pi drawing:
# 12.1-12.2 V, which the charging band painted orange/red and turned into a
# stream of "OBD adapter volts critical" alerts.
_VOLTAGE_METRICS = ("ECU Voltage", "ELM Voltage")
RESTING_VOLTAGE_BAND = (12.0, 13.2, 0.4, 0.7)
FUEL_DROP_BLOCK_AT_STANDSTILL = 20.0  # % drop ignored when speed==0 (slosh)

# =========================
# Fuel calibration / LUT
# =========================
def _read_calibration() -> Dict[str, Any]:
    cfg = _cfg_load_settings()
    sect = "Calibration"
    off     = cfg.getfloat(sect, "fuel_offset", fallback=0.0)
    scale   = cfg.getfloat(sect, "fuel_scale", fallback=1.0)
    reserve = cfg.getfloat(sect, "fuel_reserve_percent", fallback=0.0)
    formula = cfg.get(sect, "fuel_formula", fallback="affine").strip().lower()
    raw_empty = cfg.getfloat(sect, "fuel_raw_empty", fallback=0.0)
    raw_full = cfg.getfloat(sect, "fuel_raw_full", fallback=100.0)
    use_lut = cfg.getboolean(sect, "fuel_use_lut", fallback=False)
    lutraw = cfg.get(sect, "fuel_lut", fallback="")  # "0:0, 10:12, 45:95, 100:100"
    points: List[Tuple[float, float]] = []
    if lutraw.strip():
        try:
            pairs = [p.strip() for p in lutraw.split(",")]
            for pair in pairs:
                if not pair:
                    continue
                a, b = pair.split(":")
                points.append((float(a.strip()), float(b.strip())))
            points.sort(key=lambda x: x[0])
        except Exception:
            points = []
    return {
        "offset": off, "scale": scale, "reserve": reserve,
        "formula": formula, "raw_empty": raw_empty, "raw_full": raw_full,
        "use_lut": use_lut, "lut": points
    }

def _lut_interp(x: float, pts: List[Tuple[float, float]]) -> float:
    if not pts:
        return x
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    # piecewise linear
    for i in range(1, len(pts)):
        x0, y0 = pts[i-1]
        x1, y1 = pts[i]
        if x0 <= x <= x1:
            if x1 == x0:
                return y0
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return x

class FuelCalibrator:
    def __init__(self):
        cal = _read_calibration()
        self.offset  = cal["offset"]
        self.scale   = cal["scale"]
        self.reserve = cal["reserve"]
        self.formula = cal["formula"]
        self.raw_empty = cal["raw_empty"]
        self.raw_full = cal["raw_full"]
        self.use_lut = cal["use_lut"]
        self.lut_pts = cal["lut"]

    def reload(self):
        cal = _read_calibration()
        self.offset  = cal["offset"]
        self.scale   = cal["scale"]
        self.reserve = cal["reserve"]
        self.formula = cal["formula"]
        self.raw_empty = cal["raw_empty"]
        self.raw_full = cal["raw_full"]
        self.use_lut = cal["use_lut"]
        self.lut_pts = cal["lut"]

    def apply(self, raw_pct: float) -> float:
        # Optional endpoint formula: map the observed raw sender range to 0..100.
        if self.formula in ("range", "endpoint", "endpoints"):
            span = self.raw_full - self.raw_empty
            val = 0.0 if abs(span) < 0.001 else ((raw_pct - self.raw_empty) / span) * 100.0
            val = (val + self.offset) * self.scale + self.reserve
        else:
            val = (raw_pct + self.offset) * self.scale + self.reserve

        # Optional LUT is treated as a calibrated output curve from raw ECU percent.
        if self.use_lut and self.lut_pts:
            val = _lut_interp(raw_pct, self.lut_pts)
        return max(0.0, min(100.0, val))


# =========================
# Gear estimator (with hysteresis)
# =========================
def _load_gear_bands() -> Dict[int, Dict[str, int]]:
    cfg = _cfg_load_settings()
    bands: Dict[int, Dict[str, int]] = {}
    if cfg.has_section("GearBands"):
        for g in range(1, 6):
            bands[g] = {
                "down_red":    cfg.getint("GearBands", f"{g}.down_red",    fallback=900),
                "down_orange": cfg.getint("GearBands", f"{g}.down_orange", fallback=1100),
                "down_yellow": cfg.getint("GearBands", f"{g}.down_yellow", fallback=1300),
                "up_yellow":   cfg.getint("GearBands", f"{g}.up_yellow",   fallback=2500),
                "up_orange":   cfg.getint("GearBands", f"{g}.up_orange",   fallback=3000),
                "up_red":      cfg.getint("GearBands", f"{g}.up_red",      fallback=4000),
            }
    return bands

GEAR_BANDS = _load_gear_bands()


class GearEstimator:
    """
    Estimate gear from ratio = (km/h) / (rpm/1000).
    Uses hysteresis and stability time to avoid flicker, with special care around 4th↔5th.
    Ratios here are 'km/h @ 1000 rpm' reference per gear (reference values in the vehicle profile).
    """
    def __init__(self):
        # Slightly tightened 4th ratio vs 5th to reduce premature 5th detection
        self.ref = {i + 1: float(r) for i, r in enumerate(_vp.get("gear_ratios_kmh_per_1000rpm"))}
        # Precompute boundaries (midpoints)
        self.bound_up = {g: (self.ref[g] + self.ref[g+1]) / 2.0 for g in range(1, 5)}
        # Extra margin near 4th→5th
        self.margin_default = 1.0
        self.margin_4to5 = 1.5

        self.last_gear: str = "N"
        self.last_commit_time: float = 0.0

        # Candidate/stability
        self.candidate: Optional[int] = None
        self.candidate_since: float = 0.0
        self.stable_secs = 0.40

        # Filters and thresholds
        self.ratio_ewma: Optional[float] = None
        self.ratio_alpha = 0.25
        self.min_speed_kph = 2.0
        self.min_rpm = 700.0
        self.stop_latch_secs = 10.0

    def _ratio(self, rpm: float, spd: float) -> Optional[float]:
        if rpm is None or spd is None or rpm < self.min_rpm or spd < self.min_speed_kph:
            return None
        r = spd / (rpm / 1000.0)
        if self.ratio_ewma is None:
            self.ratio_ewma = r
        else:
            self.ratio_ewma += self.ratio_alpha * (r - self.ratio_ewma)
        return self.ratio_ewma

    def _nearest(self, r: float) -> int:
        best_g, best_d = None, 1e9
        for g, ref in self.ref.items():
            d = abs(r - ref)
            if d < best_d:
                best_d, best_g = d, g
        return best_g or 1

    def _apply_hysteresis(self, current_g: Optional[int], r: float) -> int:
        # choose initial by boundaries
        g = self._nearest(r)
        if current_g is None:
            return g

        # Decide if we can move up or down past boundaries + margin
        margin = self.margin_4to5 if current_g == 4 else self.margin_default

        # Move up?
        if current_g < 5:
            up_b = self.bound_up[current_g]
            if r > (up_b + margin):
                return current_g + 1

        # Move down?
        if current_g > 1:
            down_b = self.bound_up[current_g - 1]
            if r < (down_b - margin):
                return current_g - 1

        # Stay
        return current_g

    def estimate(self, rpm: Optional[float], spd: Optional[float]) -> str:
        now = time.time()
        if rpm is None or spd is None or spd < self.min_speed_kph or rpm < self.min_rpm:
            # latch the last gear for a bit when coasting to stop
            if now - self.last_commit_time < self.stop_latch_secs and self.last_gear != "N":
                return self.last_gear
            return "N"

        r = self._ratio(rpm, spd)
        if r is None:
            return self.last_gear if self.last_gear != "N" else "N"

        current_g = None if self.last_gear == "N" else int(self.last_gear)
        target = self._apply_hysteresis(current_g, r)

        # Stability check
        if self.candidate != target:
            self.candidate = target
            self.candidate_since = now
        elif (now - self.candidate_since) >= self.stable_secs:
            # commit
            self.last_gear = str(self.candidate)
            self.last_commit_time = now

        return self.last_gear


# =========================
# Adaptive downshift (hill/load aware)
# =========================
def _read_gear_adapt() -> Dict[str, Any]:
    cfg = _cfg_load_settings()
    sect = "GearAdapt"
    return {
        "enabled": cfg.getboolean(sect, "enabled", fallback=True),
        # thresholds
        "load_hi": cfg.getfloat(sect, "load_hi", fallback=75.0),        # Absolute Load ≥ … (heavy)
        "thr_hi":  cfg.getfloat(sect, "thr_hi",  fallback=55.0),        # Throttle Pos ≥ … (pushing hard)
        "map_hi":  cfg.getfloat(sect, "map_hi",  fallback=85.0),        # Manifold kPa ≥ … (NA near WOT)
        "spd_drop_kphps_1": cfg.getfloat(sect, "spd_drop_kphps_1", fallback=-0.50),  # mild drop
        "spd_drop_kphps_2": cfg.getfloat(sect, "spd_drop_kphps_2", fallback=-0.80),  # strong drop
        # bumps
        "bump_1":  cfg.getint(sect, "bump_1", fallback=150),            # rpm
        "bump_2":  cfg.getint(sect, "bump_2", fallback=300),            # rpm
        # debounce / guards
        "min_rpm_considered": cfg.getfloat(sect, "min_rpm_considered", fallback=900.0),
        "min_speed_considered": cfg.getfloat(sect, "min_speed_considered", fallback=5.0),
    }

def _classify_gear_rpm_with_bump(gear: int, rpm: float, bump_rpm: int, eco_mode: bool = False) -> str:
    """
    Apply a positive bump to *down* thresholds only (hill/load assist),
    and optionally shift the up/down bands earlier when Eco mode is enabled.
    """
    b = GEAR_BANDS.get(gear)
    if not b:
        # Eco: earlier thresholds (approx)
        if eco_mode:
            return band_higher_4(rpm, 2000, 2800, 3300)
        return band_higher_4(rpm, 2500, 3500, 4000)

    # Eco adjustments: earlier upshift suggestion, and slightly later downshift suggestion (avoid nagging)
    eco_up = -800 if eco_mode else 0
    eco_down = -150 if eco_mode else 0

    down_red    = max(400, b["down_red"]    + bump_rpm + eco_down)
    down_orange = max(500, b["down_orange"] + bump_rpm + eco_down)
    down_yellow = max(600, b["down_yellow"] + bump_rpm + eco_down)

    up_yellow   = max(900, b["up_yellow"] + eco_up)
    up_orange   = max(1000, b["up_orange"] + eco_up)
    up_red      = max(1100, b["up_red"] + eco_up)

    if rpm < down_red:      return ColorRED
    if rpm < down_orange:   return ColorORANGE
    if rpm < down_yellow:   return ColorYELLOW
    if rpm <= up_yellow:    return ColorGREEN
    if rpm <= up_orange:    return ColorYELLOW
    if rpm <= up_red:       return ColorORANGE
    return ColorRED


# =========================
# Smoother
# =========================
class Smooth:
    def __init__(self, alpha: float, keep: int = 3):
        self.alpha = alpha
        self.buf = deque(maxlen=keep)
        self.y: Optional[float] = None

    def step(self, x: Optional[float]) -> Optional[float]:
        if x is None:
            return self.y
        self.buf.append(x)
        med = sorted(self.buf)[len(self.buf)//2]
        if self.y is None or self.alpha == 0.0:
            self.y = med
        else:
            self.y += self.alpha * (med - self.y)
        return self.y


class SpikeGuard(Smooth):
    """Pass-through with a one-sample plausibility check — for RPM and Speed.

    The median-of-3 above costs a full sample of lag on every ramp (120 ms at
    8 Hz), which the engine sound hears. What it was protecting against —
    a flaky ELM clone returning a garbage frame — the vLinker FS never does
    (0 % nulls, 16 ms round trips). So: pass every sample straight through,
    and only hold the previous value when a *single* sample jumps by more
    than `max_jump` against it; two agreeing samples in a row are believed
    (a real 2500 rpm step in 120 ms does not happen, a real stall does over
    several samples).
    """

    def __init__(self, max_jump: float):
        super().__init__(alpha=0.0, keep=2)
        self.max_jump = float(max_jump)
        self._pending: Optional[float] = None

    def step(self, x: Optional[float]) -> Optional[float]:
        if x is None:
            return self.y
        if self.y is None:
            self.y = float(x)
            return self.y
        if abs(float(x) - self.y) <= self.max_jump:
            self._pending = None
            self.y = float(x)
        elif self._pending is not None and abs(float(x) - self._pending) <= self.max_jump:
            # Second sample agrees with the outlier → it was real.
            self._pending = None
            self.y = float(x)
        else:
            self._pending = float(x)
        return self.y


# =========================
# TelemetryHub (singleton)
# =========================
class TelemetryHub:
    _instance: Optional["TelemetryHub"] = None

    @classmethod
    def instance(cls) -> "TelemetryHub":
        if cls._instance is None:
            cls._instance = TelemetryHub()
        return cls._instance

    def __init__(self):
        self.manager = OBDManager()
        self.connection = self.manager.get_connection()
        self.filters: Dict[str, Smooth] = {name: Smooth(METRIC_REGISTRY[name]["alpha"]) for name in METRIC_REGISTRY}
        # RPM/Speed feed the engine sound and the needle: no median lag there.
        self.filters["RPM"] = SpikeGuard(max_jump=2500.0)
        self.filters["Speed"] = SpikeGuard(max_jump=40.0)
        self.last_ts: Dict[str, float] = {}
        self.values: Dict[str, Optional[float]] = {name: None for name in METRIC_REGISTRY}
        self.units: Dict[str, str] = {name: METRIC_REGISTRY[name]["unit"] for name in METRIC_REGISTRY}
        self.colors: Dict[str, str] = {name: ColorGREEN for name in METRIC_REGISTRY}
        self.texts: Dict[str, str]  = {name: "—" for name in METRIC_REGISTRY}
        self.snapshot_cache: Dict[str, Dict[str, Any]] = {}
        self.engine_only = {name for name, e in METRIC_REGISTRY.items() if e.get("engine_only")}
        now = time.time()
        self.next_due: Dict[str, float] = {name: now for name in METRIC_REGISTRY}

        # Fuel calibration + overlay flags
        self.fuel_cal = FuelCalibrator()
        self.fuel_use_lut = self.fuel_cal.use_lut  # mirror ini default
        self.debug_show_raw_subtext = False        # drawer can toggle
        self._fuel_last_raw: Optional[float] = None
        self._fuel_last_calibrated: Optional[float] = None
        self._throttle_source: str = "Throttle Pos"

        # --- SIM (fake OBD) ---
        # Enabled ONLY via Options -> "OBD Simulator…"
        # Policy: SIM is never allowed while a real OBD connection is active.
        self.sim_enabled: bool = False
        self._sim_values: Dict[str, float] = {"RPM": 800.0, "Speed": 0.0}
        # Overrides for SIM mode: per-metric enable + value.
        # Note: METRIC_REGISTRY covers most metrics; Gear is handled separately.
        self._sim_overrides: Dict[str, Dict[str, Any]] = {name: {"enabled": False, "value": 0.0} for name in METRIC_REGISTRY}
        self._sim_overrides["Gear"] = {"enabled": False, "value": 0.0}

        # Gear
        self.gear = GearEstimator()

        # RPM smoothing (first-order filter + interpolation between OBD samples)
        self._rpm_smooth: float = 0.0
        self._rpm_smooth_ts: float = 0.0
        self._rpm_smooth_target: float = 0.0

        # Speed slope bookkeeping for hill/load detection
        self._prev_spd: Optional[float] = None
        self._prev_spd_ts: Optional[float] = None

        # Adaptive downshift (hill/load) config
        self._adapt_cfg = _read_gear_adapt()
        self._adapt_enabled = self._adapt_cfg["enabled"]
        self._adapt_active_bump = 0  # for tooltip/UI
        self._adapt_score = 0.0
        self._adapt_hold_until = 0.0

        # UI value scaling hint (2x main value size)
        self.value_scale = 2.0

        # Optional icon used to hint when adaptive thresholding is active (shown on RPM tile)
        self._adapt_icon_path = "GUI/Icons/Telemetry/uphill_assist.png"

        # Pseudo steering estimate (for Sound Drive stereo sway)
        self._prev_thr: Optional[float] = None
        self._steer_smoother = Smooth(alpha=0.25, keep=5)
        self._steer_value: float = 0.0

        # CAN clutch switch (optional — configured via [SoundFeatures] in settings.ini)
        # When frame_id is empty/0, clutch is not available (snapshot value = None).
        self._clutch_can_frame_id: int = 0
        self._clutch_can_byte: int = 0
        self._clutch_can_bit: int = 0
        self._clutch_state: Optional[bool] = None
        self._load_clutch_can_settings()

        # -------------------------
        # Driving modes / Eco flag
        # -------------------------
        self.eco_mode: bool = _ui_getbool("eco_mode_enabled", False)

        self.reload_driving_flags()

        # -------------------------
        # Warm-up / shutdown safety
        # -------------------------
        self._engine_running: bool = False
        self._engine_start_ts: Optional[float] = None

        # Remember if we have seen the engine "hot" in this run
        self._hot_seen: bool = False

        # When a hot run ends, we require a short idle period before safe shutdown
        self._hot_idle_start_ts: Optional[float] = None

        # Track last safe state for eventing
        self._turnoff_last_safe: Optional[bool] = None


    # -------------------------
    # CAN clutch switch
    # -------------------------
    def _load_clutch_can_settings(self):
        """Read optional [SoundFeatures] clutch_can_* keys from settings.ini."""
        cfg = _cfg_load_settings()
        try:
            self._clutch_can_frame_id = int(cfg.get("SoundFeatures", "clutch_can_frame_id", fallback="0"), 0)
            self._clutch_can_byte = int(cfg.get("SoundFeatures", "clutch_can_byte", fallback="0"))
            self._clutch_can_bit = int(cfg.get("SoundFeatures", "clutch_can_bit", fallback="0"))
        except (ValueError, configparser.Error):
            self._clutch_can_frame_id = 0

    # -------------------------
    # CAN-sourced values (Modules/Obd/can_feed.py)
    # -------------------------
    # metric name → (value, monotonic ts). A metric with a fresh CAN value is
    # taken from here in update() and its PID is not polled — this is how a
    # signal confirmed in the CAN debugger moves that reading off the
    # request/response path (and onto the bus's own broadcast rate).
    _CAN_FRESH_S = 1.0
    _can_values: Dict[str, Tuple[Any, float]] = {}
    _can_states: Dict[str, Any] = {}
    _can_lock = threading.Lock()

    def ingest_can(self, metrics: Dict[str, Any], states: Optional[Dict[str, Any]] = None,
                   clutch: Optional[bool] = None, ts: Optional[float] = None) -> None:
        """Called from the STN monitor thread; cheap dict writes only."""
        now = ts if ts is not None else time.monotonic()
        with self._can_lock:
            for k, v in metrics.items():
                self._can_values[k] = (v, now)
            if states:
                self._can_states.update(states)
            if clutch is not None:
                self._clutch_state = bool(clutch)

    def _can_fresh(self, name: str, now_mono: float) -> Optional[Any]:
        with self._can_lock:
            item = self._can_values.get(name)
        if item is None:
            return None
        v, ts = item
        return v if (now_mono - ts) <= self._CAN_FRESH_S else None

    def update_clutch_from_can(self, frame_id: int, data: bytes):
        """Called by CAN listener when a frame arrives. Updates clutch state
        if frame_id matches the configured clutch CAN frame."""
        if self._clutch_can_frame_id == 0 or frame_id != self._clutch_can_frame_id:
            return
        try:
            byte_val = data[self._clutch_can_byte]
            self._clutch_state = bool(byte_val & (1 << self._clutch_can_bit))
        except (IndexError, TypeError):
            pass

    # -------------------------
    # Eco mode (UI toggle)
    # -------------------------
    def reload_driving_mode(self) -> None:
        """Read driving flags from UI settings (Data/settings.ini)."""
        try:
            self.eco_mode = _ui_getbool("eco_mode_enabled", False)
        except Exception:
            self.eco_mode = False

    def reload_driving_flags(self) -> None:
        # Backward-compatible alias (older code expects this name)
        self.reload_driving_mode()


    # -------------------------
    # Warm-up helpers
    # -------------------------
    def _engine_is_cold(self) -> bool:
        """True when coolant or oil are below warm-up thresholds.
        Each sensor is checked independently so a missing oil-temp PID
        (common on many OBD adapters) does not disable the blue overlay.
        """
        c = self.values.get("Coolant")
        o = self.values.get("Oil Temp")
        cold_c = (float(c) < ENGINE_COLD_COOLANT_C) if c is not None else None
        cold_o = (float(o) < ENGINE_COLD_OIL_C)     if o is not None else None
        available = [x for x in (cold_c, cold_o) if x is not None]
        return any(available) if available else False

    def _engine_is_hot(self) -> bool:
        c = self.values.get("Coolant")
        o = self.values.get("Oil Temp")
        try:
            if c is None and o is None:
                return False
            if c is not None and float(c) >= ENGINE_HOT_COOLANT_C:
                return True
            if o is not None and float(o) >= ENGINE_HOT_OIL_C:
                return True
        except Exception:
            pass
        return False

    def _update_engine_run_state(self, engine_running: bool, now: float) -> None:
        # transitions
        if engine_running and not self._engine_running:
            self._engine_start_ts = now
            self._hot_seen = False
            self._hot_idle_start_ts = None

        if not engine_running and self._engine_running:
            # engine stopped
            self._hot_idle_start_ts = None

        self._engine_running = engine_running

        # hot detection — temperature threshold OR long run duration
        long_run = (
            self._engine_start_ts is not None
            and (now - self._engine_start_ts) >= MIN_RUN_FOR_COOLDOWN_SEC
        )
        if engine_running and (self._engine_is_hot() or long_run):
            self._hot_seen = True

    def _apply_cold_overlay(self, metric_name: str) -> None:
        """
        Add blue text + cold icon badge for key metrics during warm-up.
        Does NOT change background color bands.
        """
        if metric_name not in self.snapshot_cache:
            return
        if not self._engine_is_cold():
            # clear our cold badge if present
            entry = self.snapshot_cache[metric_name]
            badges = entry.get("badges", [])
            if isinstance(badges, list):
                entry["badges"] = [b for b in badges if b.get("id") != "engine_cold"]
            # restore to default (dashboard will use this later)
            entry.pop("value_text_color", None)
            self.snapshot_cache[metric_name] = entry
            return

        entry = self.snapshot_cache[metric_name]
        entry["value_text_color"] = VALUE_TEXT_BLUE

        badges = entry.get("badges", [])
        if not isinstance(badges, list):
            badges = []

        badges = [b for b in badges if b.get("id") != "engine_cold"]
        badges.append({
            "id": "engine_cold",
            "icon": ICON_COLD,
            "tooltip": "Engine cold – keep RPM low until warm",
            "position": "value-near",
        })
        entry["badges"] = badges
        self.snapshot_cache[metric_name] = entry

    # -------------------------
    # Turn-off safety
    # -------------------------
    def _parking_brake_active(self) -> bool:
        """True if any source reports the parking brake is engaged.

        Source 1: CAN/OBD snapshot key "Parking Brake" (populated when CAN bus is mapped).
        Sources 2+3: DashLightsHub "parking" key — set by camera detection or
                     manual/simulate override (Settings > Debugger).
        """
        try:
            val = self.values.get("Parking Brake")
            if val is not None and float(val) > 0:
                return True
        except Exception:
            pass
        try:
            from Modules.Layout.dash_lights_hub import DashLightsHub
            if "parking" in DashLightsHub.instance().get_active():
                return True
        except Exception:
            pass
        return False

    def _compute_turnoff(self, now: float) -> Dict[str, Any]:
        """
        Returns snapshot entry:
          - safe (bool)
          - icon (turn_off.png) when safe
          - text ("SAFE"/"WAIT")
          - reason + remaining (optional)
          - event ("safe"/"unsafe") on state changes
        """
        _suppress = {"safe": True, "icon": None, "text": "—", "reason": "",
                     "color": ColorGREEN, "tooltip": "", "value_scale": 1.0}

        # Guard 1: OBD must be connected — no real data otherwise.
        if not self.manager.is_connected():
            return _suppress

        # Guard 2: engine must have run for at least 5 minutes before this
        # alert becomes relevant (TCe oil can't get meaningfully hot sooner).
        if (self._engine_start_ts is None or
                (now - self._engine_start_ts) < MIN_ENGINE_RUN_BEFORE_ALERT_SEC):
            return _suppress

        rpm = self.values.get("RPM") or 0.0
        spd = self.values.get("Speed") or 0.0

        # Current temperature readings for direct safety checks
        def _safe_float(val):
            try:
                return float(val) if val is not None else None
            except (TypeError, ValueError):
                return None

        coolant_now = _safe_float(self.values.get("Coolant"))
        oil_now     = _safe_float(self.values.get("Oil Temp"))

        # must be essentially stopped & idling
        stopped = False
        try:
            stopped = float(spd) <= SAFE_STOP_SPEED_KMH
        except Exception:
            stopped = True

        # Parking brake (any source) is authoritative proof the car is parked.
        if self._parking_brake_active():
            stopped = True

        idling = False
        try:
            idling = float(rpm) <= SAFE_IDLE_RPM
        except Exception:
            idling = True

        safe = True
        reason = ""

        if not stopped:
            safe = False
            reason = "Moving"
            # Reset idle timer — turbo is under load again
            self._hot_idle_start_ts = None

        elif not idling:
            safe = False
            reason = "High RPM"
            # Reset idle timer — not truly idling
            self._hot_idle_start_ts = None

        elif coolant_now is not None and coolant_now >= TURNOFF_UNSAFE_COOLANT_C:
            # Coolant is still actively hot right now — water pump still needed.
            # Don't start the idle timer yet; wait for temp to drop first.
            safe = False
            reason = f"Coolant {int(coolant_now)}°C"
            self._hot_idle_start_ts = None

        elif oil_now is not None and oil_now >= TURNOFF_UNSAFE_OIL_C:
            # Oil is critically hot — turbo bearings need more circulation.
            safe = False
            reason = f"Oil {int(oil_now)}°C"
            self._hot_idle_start_ts = None

        elif self._hot_seen:
            # Temps have dropped to acceptable levels; run the idle-circulation timer.
            if self._hot_idle_start_ts is None:
                self._hot_idle_start_ts = now

            elapsed = now - self._hot_idle_start_ts
            if elapsed < MIN_IDLE_CIRCULATION_SEC:
                safe = False
                remaining = int(MIN_IDLE_CIRCULATION_SEC - elapsed)
                reason = f"Cooling oil ({remaining}s)"
            else:
                safe = True
                reason = ""

        entry: Dict[str, Any] = {
            "safe": safe,
            "icon": ICON_TURNOFF if safe else None,
            "text": "SAFE" if safe else "WAIT",
            "reason": reason,
            "color": ColorGREEN if safe else ColorORANGE,
            "tooltip": "Safe to turn off" if safe else "Wait before turning off (oil circulation / cooldown)",
            "value_scale": 1.0,
        }

        if self._turnoff_last_safe is None or self._turnoff_last_safe != safe:
            entry["event"] = "safe" if safe else "unsafe"
        self._turnoff_last_safe = safe
        return entry

        

    # -------------------------
    # SIM controls (fake OBD)
    # -------------------------
    def set_sim_enabled(self, enabled: bool) -> bool:
        """Enable/disable SIM. Returns True if the requested state was applied."""
        enabled = bool(enabled)

        # Never allow SIM while a real OBD connection is active.
        try:
            self.connection = self.manager.get_connection()
            if enabled and self.connection and self.connection.is_connected():
                self.sim_enabled = False
                self.obd_connected = True
                return False

        except Exception:
            pass

        self.sim_enabled = enabled
        return True

    def is_sim_enabled(self) -> bool:
        return bool(self.sim_enabled)

    def set_sim_value(self, name: str, value: float) -> None:
        if name not in ("RPM", "Speed"):
            return
        try:
            v = float(value)
        except Exception:
            return
        self._sim_values[name] = v

    def get_sim_value(self, name: str, default: float = 0.0) -> float:
        try:
            return float(self._sim_values.get(name, default))
        except Exception:
            return float(default)

    # -------------------------
    # SIM overrides (per-metric)
    # -------------------------
    def set_sim_override_enabled(self, name: str, enabled: bool) -> None:
        if name not in self._sim_overrides:
            return
        self._sim_overrides[name]["enabled"] = bool(enabled)

    def set_sim_override_value(self, name: str, value: float) -> None:
        if name not in self._sim_overrides:
            return
        try:
            self._sim_overrides[name]["value"] = float(value)
        except Exception:
            return

    def get_sim_override_enabled(self, name: str) -> bool:
        try:
            return bool(self._sim_overrides.get(name, {}).get("enabled", False))
        except Exception:
            return False

    def get_sim_override_value(self, name: str, default: float = 0.0) -> float:
        try:
            return float(self._sim_overrides.get(name, {}).get("value", default))
        except Exception:
            return float(default)


    def reload_fuel_calibration(self) -> None:
        self.fuel_cal.reload()
        self.fuel_use_lut = self.fuel_cal.use_lut
        # Fuel is polled once a minute; re-apply the new curve to the last
        # raw sample now so the gauge does not lag a calibration by that long.
        if self._fuel_last_raw is not None:
            try:
                v = self.fuel_cal.apply(self._fuel_last_raw)
                self._fuel_last_calibrated = v
                self.values["Fuel Level"] = v
                self.filters["Fuel Level"].y = v
            except Exception:
                pass

    # Internal: query → float or None
    def _q_num(self, cmd) -> Optional[float]:
        t0 = time.monotonic()
        resp = None
        try:
            resp = safe_query(cmd)
            if not resp or resp.is_null():
                return None
            val = getattr(resp.value, "magnitude", None)
            if val is None:
                v = resp.value
                if isinstance(v, (int, float)):
                    return float(v)
                return None
            return float(val)
        except Exception:
            return None
        finally:
            self._note_query(cmd, time.monotonic() - t0, resp is not None and not resp.is_null())

    # ---- adapter latency + timing diagnostics ------------------------------
    # EMA of the round-trip per query. Drives _query_budget() and the
    # optional timing log. Seeded pessimistically (a slow clone) so the first
    # ticks after connect stay at the old 2-query budget until measured.
    _lat_ema_s = 0.10
    _q_total = 0
    _q_null = 0
    _q_by_name: Dict[str, int] = {}
    _timing_last_log = 0.0
    _timing_enabled: Optional[bool] = None
    _timing_ticks = 0

    def _note_query(self, cmd, dt: float, ok: bool) -> None:
        self._lat_ema_s += 0.2 * (dt - self._lat_ema_s)
        self._q_total += 1
        if not ok:
            self._q_null += 1
        name = getattr(cmd, "name", "?")
        self._q_by_name[name] = self._q_by_name.get(name, 0) + 1

    def _query_budget(self) -> int:
        """Serial queries allowed this tick: keep the bus busy for at most
        about half the coordinator's ~120 ms period. 16 ms/query (vLinker FS
        on CAN) → 4; 100 ms/query (Bluetooth ELM clone) → 2, the old fixed
        value. Overridable with [OBD] max_queries_per_tick for experiments."""
        override = settings_cache.getint("OBD", "max_queries_per_tick", fallback=0, path=DATA_SETTINGS)
        if override > 0:
            return max(1, min(8, override))
        lat = max(0.005, self._lat_ema_s)
        return max(2, min(4, int(round(0.06 / lat))))

    def _timing_log(self, now: float) -> None:
        """[Debug] telemetry_timing = 1 → one line every 10 s with the numbers
        that matter for "the engine sound lags": real tick period, queries/s,
        null rate, and how often RPM/Speed actually refreshed. Off by default;
        the flag is re-read at the same 10 s cadence so it can be toggled live."""
        self._timing_ticks += 1
        if now - self._timing_last_log < 10.0:
            return
        span = now - self._timing_last_log if self._timing_last_log else 10.0
        self._timing_last_log = now
        self._timing_enabled = settings_cache.getboolean("Debug", "telemetry_timing", fallback=False, path=DATA_SETTINGS)
        if self._timing_enabled:
            ticks = max(1, self._timing_ticks)
            rpm_n = self._q_by_name.get("RPM", 0)
            spd_n = self._q_by_name.get("SPEED", 0)
            null_pct = (100.0 * self._q_null / self._q_total) if self._q_total else 0.0
            others = " ".join(
                f"{k}:{v}" for k, v in sorted(self._q_by_name.items()) if k not in ("RPM", "SPEED")
            )
            can_txt = ""
            try:
                sess = self.manager.get_session()
                if sess is not None and sess.monitor_active:
                    st = sess.stats_snapshot()
                    can_txt = (f" | CAN {st['fps']:.0f} fps {st['unique_ids']} ids "
                               f"duty {100 * sess.duty_cycle():.0f}% pauses {sess.pauses}")
            except Exception:
                can_txt = ""
            print(
                f"[telemetry] tick {1000.0 * span / ticks:.0f} ms | "
                f"{self._q_total / span:.1f} q/s, budget {self._query_budget()}, "
                f"lat {1000.0 * self._lat_ema_s:.0f} ms, null {null_pct:.0f}% | "
                f"RPM {rpm_n / span:.1f} Hz, Speed {spd_n / span:.1f} Hz | {others}{can_txt}",
                flush=True,
            )
        self._timing_ticks = 0
        self._q_total = 0
        self._q_null = 0
        self._q_by_name = {}

    def _should_poll(self, name: str, now: float) -> bool:
        interval = self._poll_interval_for(name)
        due = self.next_due.get(name, 0.0)
        # A due-time booked under a slower tier (3 s while the Dashboard was
        # hidden) must not hold a tile back once its faster tier applies.
        if now >= due or (due - now) > interval:
            self.next_due[name] = now + max(0.05, interval)
            return True
        return False

    # Maximum number of OBD serial queries per update() call — set per tick
    # from _query_budget(). Each query blocks the coordinator thread for the
    # adapter's round trip; capping keeps the tick period honest so the
    # engine-sound feed never waits behind a queue of slow PIDs.
    _MAX_QUERIES_PER_TICK = 2
    _queries_this_tick = 0

    # Thermal / electrical safety PIDs — must not be starved by RPM/Speed traffic.
    # When the per-tick budget is exhausted by tier-1 (RPM, Speed), tier-2 gets
    # priority over the rest of METRIC_REGISTRY on the next tick that has room.
    _THERMAL_PIDS = ("Coolant", "Oil Temp", "ECU Voltage")

    # Three tiers of "who needs this PID", replacing the old single
    # diagnostics-only gate that had Timing / Lambda / Intake / Pedal reading
    # N/A on the Dashboard in the car (2026-09-15) and starved the
    # hidden-metric alerts that read the same keys:
    #
    #   • _DIAG_ONLY_PIDS — no live consumer at all outside Settings →
    #     Diagnostics. Skipped unless that page is open.
    #   • _SLOW_ALWAYS_PIDS — feed main.py's _HIDDEN_METRIC_ALERTS (intake hot,
    #     boost, load, mixture, timing) so they must tick over even with the
    #     Dashboard hidden, but 3 s is plenty for an alert threshold.
    #   • _DASHBOARD_PIDS — drawn as tiles on the Dashboard page; polled at
    #     _DASHBOARD_BOOST_S while that page is showing (set_dashboard_visible()).
    _DIAG_ONLY_PIDS = frozenset({"Rail P (vac)", "Rail P (DI)", "Baro Press"})
    _SLOW_ALWAYS_PIDS = frozenset({
        "Intake Air", "Manifold Press", "Absolute Load", "Equiv. Ratio",
        "Timing Adv", "Fuel Pressure", "Accel Pedal D", "Fuel Trim S", "Fuel Trim L",
    })
    _SLOW_ALWAYS_S = 3.0
    _DASHBOARD_PIDS = frozenset({
        "Timing Adv", "Equiv. Ratio", "Manifold Press", "Absolute Load",
        "Accel Pedal D", "Intake Air",
    })
    _DASHBOARD_BOOST_S = 0.5
    # Secondary throttle source — only needed when the primary "Throttle Pos"
    # PID isn't answering (see _best_throttle_value()). Accel Pedal D used to
    # be here too, but the Dashboard draws pedal *and* throttle plate as two
    # tiles, so it is a first-class reading now.
    _THROTTLE_FALLBACK_PIDS = ("Rel. Throttle",)
    _diagnostics_visible = False
    _dashboard_visible = False

    def set_diagnostics_visible(self, visible: bool) -> None:
        """Gate the diagnostics-only PIDs. Called when the Settings →
        Diagnostics page is shown/hidden so they're only polled when on view."""
        self._diagnostics_visible = bool(visible)

    def set_dashboard_visible(self, visible: bool) -> None:
        """Boost the Dashboard's own tiles while that page is the one showing.
        main.py drives this from Nav.currentPage — the page itself has no hook."""
        self._dashboard_visible = bool(visible)

    # Refuel watch (Trip page): read the tank every 5 s instead of every 60 s
    # around an engine stop, so a refuel is seen before the car drives off.
    _fuel_watch = False
    _FUEL_WATCH_S = 5.0

    def set_fuel_watch(self, on: bool) -> None:
        self._fuel_watch = bool(on)

    def snap_fuel(self, pct: float) -> None:
        """Jump the fuel gauge to `pct` after a detected refuel — its
        median-of-3 + 0.05 smoothing on minute samples would otherwise take
        20+ minutes to climb there."""
        try:
            f = self.filters["Fuel Level"]
            f.buf.clear()
            f.buf.append(float(pct))
            f.y = float(pct)
            self.values["Fuel Level"] = float(pct)
        except Exception:
            pass

    def _poll_interval_for(self, name: str) -> float:
        """Effective cadence for a metric given the tiers above."""
        if name == "Fuel Level" and self._fuel_watch:
            return self._FUEL_WATCH_S
        if name in self._DASHBOARD_PIDS and self._dashboard_visible:
            return self._DASHBOARD_BOOST_S
        if name in self._SLOW_ALWAYS_PIDS and not self._diagnostics_visible:
            return self._SLOW_ALWAYS_S
        return POLL_SECS.get(name, DEFAULT_POLL)

    def _q_num_budgeted(self, cmd) -> float | None:
        """Like _q_num but respects the per-tick query budget."""
        if self._queries_this_tick >= self._MAX_QUERIES_PER_TICK:
            return None
        self._queries_this_tick += 1
        return self._q_num(cmd)

    # ---- MIL / DTC count ----------------------------------------------------
    _mil: Optional[bool] = None
    _dtc_count: Optional[int] = None
    _rpm_sample_mono: float = 0.0

    def _poll_status(self, now: float, live: bool) -> None:
        """Mode-01 PID 01 (obd.commands.STATUS): MIL lamp + stored-code count.

        One query every 30 s while a real ECU is answering. The Home page's
        "Codes" tile has read a "DTC Count" key that nothing produced (so it
        said 0 forever), and the Pi in the car has no camera, so the
        [DashCal] dash-light detector — the only other "check engine" source
        — can never run there. STATUS is a non-numeric response, hence not
        via _q_num(); it still counts against the per-tick budget."""
        if not live:
            self._mil = None
            self._dtc_count = None
        elif (self._queries_this_tick < self._MAX_QUERIES_PER_TICK
              and self._should_poll("DTC Count", now)):
            self._queries_this_tick += 1
            t0 = time.monotonic()
            resp = None
            try:
                resp = safe_query(obd.commands.STATUS)
                if resp is not None and not resp.is_null():
                    st = resp.value
                    self._mil = bool(getattr(st, "MIL", False))
                    self._dtc_count = int(getattr(st, "DTC_count", 0) or 0)
                    self.last_ts["DTC Count"] = now
            except Exception:
                pass
            self._note_query(obd.commands.STATUS, time.monotonic() - t0,
                             resp is not None and not resp.is_null())

        if self._dtc_count is None:
            self.snapshot_cache["DTC Count"] = {
                "value": None, "unit": "", "text": "N/A", "color": ColorNA,
                "tooltip": TOOLTIPS.get("DTC Count", ""), "value_scale": self.value_scale,
                "telemetry_color_locked": True,
            }
            self.snapshot_cache["MIL"] = {
                "value": None, "unit": "", "text": "N/A", "color": ColorNA,
                "value_scale": self.value_scale, "telemetry_color_locked": True,
            }
            return
        self.snapshot_cache["DTC Count"] = {
            "value": float(self._dtc_count), "unit": "", "text": str(self._dtc_count),
            "color": ColorRED if self._mil else (ColorYELLOW if self._dtc_count else ColorGREEN),
            "tooltip": TOOLTIPS.get("DTC Count", ""), "value_scale": self.value_scale,
            "telemetry_color_locked": True,
        }
        self.snapshot_cache["MIL"] = {
            "value": 1.0 if self._mil else 0.0, "unit": "",
            "text": "ON" if self._mil else "OFF",
            "color": ColorRED if self._mil else ColorGREEN,
            "value_scale": self.value_scale, "telemetry_color_locked": True,
        }

    def _compute_downshift_bump(self, now: float) -> int:
        """
        Decide whether to raise downshift bands by +bump rpm when the car is 'struggling':
          - high Absolute Load OR high Throttle Pos
          - and/or MAP high (near WOT on NA)
          - and speed trending down despite throttle (hill/drag)
        Returns 0, bump_1, or bump_2.
        """
        if not self._adapt_enabled:
            self._adapt_active_bump = 0
            return 0

        cfg = self._adapt_cfg
        rpm = self.values.get("RPM") or 0.0
        spd = self.values.get("Speed") or 0.0
        load = self.values.get("Absolute Load") or 0.0
        thr  = self._best_throttle_value()[0] or 0.0
        mapk = self.values.get("Manifold Press") or 0.0

        # compute speed derivative (kph per second)
        spd_slope = 0.0
        if self._prev_spd is not None and self._prev_spd_ts is not None:
            dt = max(0.05, now - self._prev_spd_ts)
            spd_slope = (spd - self._prev_spd) / dt

        # update the stored previous (after using it)
        self._prev_spd, self._prev_spd_ts = spd, now

        # Ignore very low speeds / rpms
        if spd < cfg["min_speed_considered"] or rpm < cfg["min_rpm_considered"]:
            self._adapt_active_bump = 0
            return 0

        # Signals of sustained uphill/load struggle.
        hi_load = load >= cfg["load_hi"]
        hi_thr = thr >= cfg["thr_hi"]
        hi_map = mapk >= cfg["map_hi"]      # NA: near ambient at WOT
        drop_strong = spd_slope <= cfg["spd_drop_kphps_2"]
        drop_mild = spd_slope <= cfg["spd_drop_kphps_1"]

        # Do not treat normal acceleration as uphill struggle.
        not_accelerating = spd_slope <= 0.20
        strong_struggle = bool((hi_load and hi_thr) and not_accelerating and (drop_strong or hi_map))
        mild_struggle = bool((hi_load and hi_thr) and (drop_mild or (hi_map and not_accelerating)))

        # Integrate a score to avoid rapid on/off oscillation under throttle blips.
        if strong_struggle:
            self._adapt_score = min(6.0, self._adapt_score + 2.0)
        elif mild_struggle:
            self._adapt_score = min(6.0, self._adapt_score + 1.0)
        else:
            self._adapt_score = max(0.0, self._adapt_score - 1.5)

        target_bump = 0
        if self._adapt_score >= 3.6:
            target_bump = int(cfg["bump_2"])
        elif self._adapt_score >= 1.8:
            target_bump = int(cfg["bump_1"])

        # Hold active state briefly so it doesn't chatter during short transients.
        current_bump = int(self._adapt_active_bump or 0)
        if (target_bump < current_bump) and (now < float(self._adapt_hold_until or 0.0)):
            target_bump = current_bump

        if target_bump > 0:
            self._adapt_hold_until = now + 1.25
        elif now >= float(self._adapt_hold_until or 0.0):
            self._adapt_hold_until = 0.0

        self._adapt_active_bump = int(target_bump)
        return int(target_bump)

    def _best_throttle_value(self) -> Tuple[Optional[float], str]:
        for name in ("Throttle Pos", "Rel. Throttle", "Accel Pedal D", "Absolute Load"):
            try:
                value = self.values.get(name)
                if value is not None:
                    return max(0.0, min(100.0, float(value))), name
            except Exception:
                continue
        return None, "N/A"

    def _apply_throttle_fallback_snapshot(self, engine_running: bool) -> None:
        value, source = self._best_throttle_value()
        self._throttle_source = source
        if value is None:
            return
        if not engine_running:
            return

        entry = self.snapshot_cache.get("Throttle Pos") or {}
        if entry.get("value") is not None and source == "Throttle Pos":
            entry["source"] = source
            self.snapshot_cache["Throttle Pos"] = entry
            self.snapshot_cache["Driver Demand"] = dict(entry)
            return

        try:
            color = METRIC_REGISTRY["Throttle Pos"]["color"](value)
        except Exception:
            color = ColorGREEN
        fallback_entry = {
            "value": value,
            "unit": "%",
            "text": _fmt_pct(value),
            "color": color,
            "tooltip": f"Canonical driver demand, sourced from {source}.",
            "value_scale": self.value_scale,
            "telemetry_color_locked": True,
            "source": source,
            "fallback": source != "Throttle Pos",
        }
        self.snapshot_cache["Throttle Pos"] = fallback_entry
        self.snapshot_cache["Driver Demand"] = dict(fallback_entry)
        self.values["Throttle Pos"] = value

    def update(self) -> None:
        # --- FIX: ensure variable always exists in every path (OBD/SIM/disconnected) ---
        engine_running = False

        self.connection = self.manager.get_connection()
        now = time.time()
        self._queries_this_tick = 0  # reset per-tick query budget
        self._MAX_QUERIES_PER_TICK = self._query_budget()

        # refresh driving flags
        self.reload_driving_flags()

        # SIM is never allowed while OBD is connected.
        if self.sim_enabled and self.connection and self.connection.is_connected():
            self.sim_enabled = False

        connected = bool(self.manager.is_connected())
        self.obd_connected = bool(connected)
        sim_mode = (not connected) and bool(self.sim_enabled)

        # Disconnected → Grey all (SIM can run only when disconnected)
        if not connected:
            if not self.sim_enabled:
                self.snapshot_cache = {
                    name: {
                        "value": None, "unit": self.units[name], "text": "N/A",
                        "color": ColorNA, "tooltip": TOOLTIPS.get(name, ""),
                        "value_scale": self.value_scale,
                    }
                    for name in METRIC_REGISTRY
                }
                self.snapshot_cache["Gear"] = {
                    "value": None, "unit": "", "text": "N/A",
                    "color": ColorNA, "tooltip": TOOLTIPS["Gear"],
                    "value_scale": self.value_scale,
                }
                self._poll_status(now, False)
                return

            # SIM mode while disconnected: fall through and run the SAME pipeline as OBD,
            # but we must avoid querying the ECU below.

        # Base RPM/Speed values (used by the unified pipeline). `rpm`/`spd`
        # are the best-known values for the logic below (engine state, gear,
        # steering); `rpm_read`/`spd_read` are only what this tick actually
        # sampled and are what the metric loop feeds the filters, so a tick
        # that did not (successfully) poll neither re-feeds a stale value nor
        # refreshes its timestamp — STALE_AFTER keeps meaning what it says.
        rpm = None
        spd = None
        rpm_read = None
        spd_read = None
        if sim_mode:
            rpm = self.get_sim_value("RPM", 0.0)
            spd = self.get_sim_value("Speed", 0.0)
            if spd is not None and spd <= SPEED_DEADBAND:
                spd = 0.0
            rpm_read, spd_read = rpm, spd
        else:
            # Use the same polling / query logic as normal OBD reads
            rpm = self.values.get("RPM")
            spd = self.values.get("Speed")
            prequeried_thermal: Dict[str, float] = {}
            if connected:
                self._timing_log(now)
                mono = time.monotonic()
                can_rpm = self._can_fresh("RPM", mono)
                can_spd = self._can_fresh("Speed", mono)
                if can_rpm is not None:
                    rpm = rpm_read = float(can_rpm)
                    self._rpm_sample_mono = mono
                if can_spd is not None:
                    spd = spd_read = 0.0 if float(can_spd) <= SPEED_DEADBAND else float(can_spd)
                # RPM and Speed are highest priority — query them first within budget.
                # A missed frame keeps the last value (staleness is handled by
                # last_ts below); a None here would flip engine_running off for
                # the tick, blanking every engine-only tile and gating the
                # engine sound — which is exactly what the adapter's "?" reply
                # to repeated commands did until _NoRepeatOBD (obd_connection).
                if can_rpm is None and self._should_poll("RPM", now):
                    rpm_read = self._q_num_budgeted(obd.commands.RPM)
                    if rpm_read is not None:
                        rpm = rpm_read
                        self._rpm_sample_mono = time.monotonic()
                # Tier-2: if any thermal/safety PID is overdue (due-time + one full
                # interval ago), take a budget slot for it BEFORE Speed. Prevents
                # thermal starvation when RPM+Speed compete for both slots.
                if self._queries_this_tick < self._MAX_QUERIES_PER_TICK:
                    for tname in self._THERMAL_PIDS:
                        tinterval = POLL_SECS.get(tname, DEFAULT_POLL)
                        tdue = self.next_due.get(tname, 0.0)
                        if now >= tdue + tinterval:
                            tcmd = METRIC_REGISTRY[tname]["cmd"]
                            tval = self._q_num_budgeted(tcmd)
                            if tval is not None:
                                prequeried_thermal[tname] = tval
                                self.next_due[tname] = now + max(0.05, tinterval)
                            break
                if (can_spd is None and self._queries_this_tick < self._MAX_QUERIES_PER_TICK
                        and self._should_poll("Speed", now)):
                    spd_read = self._q_num_budgeted(obd.commands.SPEED)
                    if spd_read is not None:
                        if spd_read <= SPEED_DEADBAND:
                            spd_read = 0.0
                        spd = spd_read

        # --- FIX: compute engine_running once, after rpm is known ---
        # A last-known RPM only counts while it is fresh: once the ECU stops
        # answering (ignition off) the engine must read as off within
        # STALE_AFTER, not stay "running" on the final sample forever.
        try:
            rpm_fresh = rpm_read is not None or (now - self.last_ts.get("RPM", 0.0)) <= STALE_AFTER
            engine_running = (rpm is not None and rpm_fresh and float(rpm) > 500.0)
        except Exception:
            engine_running = False

        # update warm-up / shutdown state
        self._update_engine_run_state(engine_running, now)

        # Derived "Steering" estimate
        # ----------------------------
        thr_cur = self._best_throttle_value()[0]
        spd_cur = self.values.get("Speed")

        steer_raw = 0.0

        if thr_cur is not None and spd_cur is not None and spd_cur >= 20.0:
            # Compare throttle vs last sample: release vs press
            if self._prev_thr is not None:
                delta_thr = thr_cur - self._prev_thr
                # Small deadband to avoid noise
                if delta_thr <= -3.0:
                    # Releasing throttle → bias one side (e.g. left)
                    steer_raw = -0.7
                elif delta_thr >= 3.0:
                    # Pressing throttle while already at speed → other side (right)
                    steer_raw = 0.7

        # Smooth steering value into -1..+1 band
        steer_smoothed = self._steer_smoother.step(steer_raw)
        if steer_smoothed is None:
            steer_smoothed = 0.0
        self._steer_value = max(-1.0, min(1.0, float(steer_smoothed)))

        # Remember last throttle for next tick
        if thr_cur is not None:
            self._prev_thr = thr_cur

        # In sim mode derive secondary OBD metrics from RPM so that the full
        # pipeline (uphill assist, pops/flutters, steering estimate, etc.) runs
        # identically to a real ECU connection.  User overrides still win.
        _sim_thr_pct  = 0.0
        _sim_abs_load = 0.0
        _sim_map_kpa  = 40.0
        if sim_mode and rpm is not None:
            _r = max(0.0, float(rpm))
            # Steady-state throttle: 0 % at idle (800), ~100 % at redline (6000)
            _sim_thr_pct  = min(100.0, max(0.0, (_r - 800.0) / 52.0))
            # Absolute load runs a steeper curve (turbo airflow)
            _sim_abs_load = min(100.0, max(0.0, (_r - 600.0) / 45.0))
            # MAP: idle ~40 kPa, full boost ~190 kPa
            _sim_map_kpa  = 40.0 + (_sim_thr_pct / 100.0) * 150.0

        # Iterate metrics
        for name, meta in METRIC_REGISTRY.items():
            cmd = meta["cmd"]

            # choose value source
            v_raw = None
            if sim_mode:
                # SIM: user overrides take priority; fall back to derived values.
                if self.get_sim_override_enabled(name):
                    v_raw = self.get_sim_override_value(name, 0.0)
                elif name == "RPM":
                    v_raw = rpm
                elif name == "Speed":
                    v_raw = spd
                elif name == "Throttle Pos":   v_raw = _sim_thr_pct
                elif name == "Rel. Throttle":  v_raw = _sim_thr_pct
                elif name == "Accel Pedal D":  v_raw = _sim_thr_pct
                elif name == "Absolute Load":  v_raw = _sim_abs_load
                elif name == "Manifold Press": v_raw = _sim_map_kpa
                elif name == "Coolant":        v_raw = 90.0   # normal operating temp
                elif name == "Intake Air":     v_raw = 25.0   # ambient
                elif name == "ECU Voltage":    v_raw = 14.2
                elif name == "ELM Voltage":    v_raw = 14.2
                else:
                    v_raw = None
            else:
                if name == "RPM":
                    v_raw = rpm_read
                elif name == "Speed":
                    v_raw = spd_read
                elif name in prequeried_thermal:
                    # Already queried during tier-2 priority pass above.
                    v_raw = prequeried_thermal[name]
                elif (can_v := self._can_fresh(name, time.monotonic())) is not None:
                    # Confirmed on the CAN map — no PID query for this one.
                    v_raw = float(can_v)
                elif name in self._DIAG_ONLY_PIDS and not self._diagnostics_visible:
                    # No live consumer — skip the serial query (and leave
                    # next_due untouched). Last value is kept until it goes stale.
                    v_raw = None
                elif name in self._THROTTLE_FALLBACK_PIDS and self.values.get("Throttle Pos") is not None:
                    # Primary throttle PID is answering — fallbacks not needed.
                    v_raw = None
                else:
                    # Budget check *before* _should_poll: that call books the
                    # next due-time, so asking with no budget left used to
                    # push a due metric out by a whole interval — 60 s for
                    # Fuel Level, 5 s for Warmups — every time the tick was
                    # already full. Now it simply stays due for the next tick.
                    if (self._queries_this_tick < self._MAX_QUERIES_PER_TICK
                            and self._should_poll(name, now)):
                        v_raw = self._q_num_budgeted(cmd)

            # --- Fuel specific rules ---
            fuel_raw_sample = None
            oil_using_last_good = False
            if name == "Fuel Level":
                if v_raw is not None:
                    fuel_raw_sample = v_raw

                    # apply calibration (affine + optional LUT)
                    cal = self.fuel_cal.apply(v_raw)

                    # at standstill: allow refuel jumps (increase), block big
                    # drops (slosh/ECU smoothing). Compared calibrated-to-
                    # calibrated: the old raw-vs-calibrated compare saw every
                    # sample as a huge drop once the LUT lifted the reading,
                    # fed the calibrated value back in as raw, and ran the
                    # gauge up to 100 %.
                    prev = self.values.get(name)
                    if prev is not None and (spd == 0.0 or spd is None):
                        if (cal - prev) < -FUEL_DROP_BLOCK_AT_STANDSTILL:
                            cal = prev  # ignore big negative jump while parked

                    v_raw = cal
                    self._fuel_last_raw = fuel_raw_sample
                    self._fuel_last_calibrated = v_raw
            elif name == "Oil Temp" and v_raw is None and connected and self.values.get(name) is not None:
                # Some ECUs/adapters occasionally miss this PID. Keep the last
                # real value while connected, and surface the stale state below.
                oil_using_last_good = True

            # smoothing
            if v_raw is not None:
                v_s = self.filters[name].step(v_raw)
                if v_s is not None:
                    self.values[name] = v_s
                    self.last_ts[name] = now
            else:
                v_s = self.values.get(name)

            # engine-only masking + staleness
            show_na = False

            # Engine-only metrics still disappear when engine is off
            if name in self.engine_only and not engine_running:
                show_na = True

            # Fuel Level: keep last reading forever (don’t mark stale)
            if name == "Fuel Level":
                pass
            else:
                # All other metrics: apply stale-after rule. Oil temp keeps
                # its last connected value to avoid UI flicker from missed PID reads.
                # The limit scales with the metric's own cadence: a flat 2 s
                # against Warmups/Distance polled every 5 s meant those tiles
                # read N/A for 3 of every 5 seconds on the car.
                stale_limit = max(STALE_AFTER, 2.5 * self._poll_interval_for(name))
                if name == "Oil Temp" and connected and self.values.get(name) is not None:
                    show_na = False
                elif self.last_ts.get(name, 0) and (now - self.last_ts[name] > stale_limit):
                    show_na = True

            if v_s is None or show_na:
                snap_entry = {
                    "value": None, "unit": self.units[name], "text": "N/A",
                    "color": ColorNA, "tooltip": TOOLTIPS.get(name, ""),
                    "value_scale": self.value_scale,
                    "telemetry_color_locked": True,
                }
                self.snapshot_cache[name] = snap_entry
                continue

            # color + text
            try:
                if name in _VOLTAGE_METRICS and not engine_running:
                    # No alternator → the registry's 13.0-14.7 V charging
                    # band is the wrong question. Ignition on, engine off,
                    # head unit + screen + Pi on the rail: ~12.2 V is normal.
                    color = band_range_4(v_s, *RESTING_VOLTAGE_BAND)
                else:
                    color = meta["color"](v_s)
            except Exception:
                color = ColorGREEN
            try:
                text = meta["fmt"](v_s)
            except Exception:
                text = f"{v_s}"

            snap_entry = {
                "value": v_s, "unit": self.units[name], "text": text,
                "color": color, "tooltip": TOOLTIPS.get(name, ""),
                "value_scale": self.value_scale,
                "telemetry_color_locked": True,
            }

            # ECO mode: tighten thresholds to guide economical driving.
            # TCe 90 ECO limits boost (~110-140 kPa max) and dampens throttle.
            # RPM in ECO: suggest upshifting earlier (warn from 3 500 rpm up).
            if self.eco_mode and v_s is not None:
                try:
                    if name == "RPM":
                        eco_color = band_higher_4(v_s, 3500, 4500, 5000)
                        color = eco_color
                        snap_entry["color"] = eco_color
                    elif name == "Manifold Press":
                        # ECO limits boost to roughly 110-140 kPa; flag it earlier
                        eco_color = band_higher_4(v_s, 110, 140, 160)
                        color = eco_color
                        snap_entry["color"] = eco_color
                except Exception:
                    pass

            # Add raw subtext overlay for Fuel Level if enabled
            if name == "Fuel Level" and self.debug_show_raw_subtext:
                if fuel_raw_sample is not None:
                    try:
                        raw_txt = f"{int(round(fuel_raw_sample))}% raw"
                    except Exception:
                        raw_txt = f"{fuel_raw_sample} raw"
                    snap_entry["subtext"] = raw_txt  # UI: render under main value, small font

                # also expose whether LUT is active (optional)
                snap_entry["calibration"] = {
                    "use_lut": bool(self.fuel_use_lut),
                    "offset": self.fuel_cal.offset,
                    "scale": self.fuel_cal.scale,
                    "reserve": self.fuel_cal.reserve,
                    "formula": self.fuel_cal.formula,
                    "raw_empty": self.fuel_cal.raw_empty,
                    "raw_full": self.fuel_cal.raw_full,
                    "lut_points": self.fuel_cal.lut_pts if self.fuel_use_lut else []
                }

            if name == "Fuel Level":
                snap_entry["raw_value"] = self._fuel_last_raw
                snap_entry["calibrated_value"] = self._fuel_last_calibrated
                snap_entry["calibration"] = {
                    "use_lut": bool(self.fuel_use_lut),
                    "offset": self.fuel_cal.offset,
                    "scale": self.fuel_cal.scale,
                    "reserve": self.fuel_cal.reserve,
                    "formula": self.fuel_cal.formula,
                    "raw_empty": self.fuel_cal.raw_empty,
                    "raw_full": self.fuel_cal.raw_full,
                    "lut_points": self.fuel_cal.lut_pts if self.fuel_use_lut else [],
                }

            if name == "Oil Temp" and oil_using_last_good:
                snap_entry["stale"] = True
                snap_entry["status"] = "Last valid oil temperature; latest PID read missed."

            self.colors[name] = color
            self.texts[name]  = text
            self.snapshot_cache[name] = snap_entry

        self._apply_throttle_fallback_snapshot(engine_running)

        # MIL lamp + stored DTC count (mode-01 PID 01)
        self._poll_status(now, connected and not sim_mode)

        # Derived: Gear (and adaptive bump) — a confirmed CAN gear signal wins
        # over the RPM/speed-ratio estimate.
        gear_str = self.gear.estimate(self.values.get("RPM"), self.values.get("Speed"))
        can_gear = self._can_fresh("Gear", time.monotonic())
        if can_gear is not None:
            gear_str = str(can_gear)

        # Compute adaptive downshift bump (hill/load aware)
        bump = self._compute_downshift_bump(now)

        if self.values.get("RPM") is None and self.values.get("Speed") is None:
            gear_entry = {
                "value": None, "unit": "", "text": "N/A",
                "color": ColorNA, "value_scale": self.value_scale,
                "telemetry_color_locked": True,
            }
        else:
            if gear_str == "N":
                gear_color = ColorYELLOW
            else:
                try:
                    # Use adaptive bands for the color only; detection remains hysteretic as before
                    gear_color = _classify_gear_rpm_with_bump(int(gear_str), self.values.get("RPM") or 0.0, bump, eco_mode=self.eco_mode)
                except Exception:
                    gear_color = ColorGREEN

            gear_entry = {
                "value": gear_str, "unit": "", "text": gear_str,
                "color": gear_color, "value_scale": self.value_scale,
                "telemetry_color_locked": True,
            }

        # Extend tooltip to show when bump is active
        tip = TOOLTIPS["Gear"]
        if bump:
            tip += f"\n\nUphill assist: +{bump} rpm on downshift bands (load/throttle/speed trend)."
        gear_entry["tooltip"] = tip
        self.snapshot_cache["Gear"] = gear_entry

        # RPM indicator: show a small badge when adaptive threshold is active
        # We *do not* change RPM value or smoothing; only hint visually.
        # When this RPM sample left the adapter (monotonic). ModdingBridge
        # reads it to measure adapter→engine-sound latency for the timing log.
        if "RPM" in self.snapshot_cache and self._rpm_sample_mono:
            self.snapshot_cache["RPM"]["sample_mono"] = self._rpm_sample_mono

        if "RPM" in self.snapshot_cache and self._adapt_enabled:
            rpm_entry = self.snapshot_cache["RPM"]
            badges = rpm_entry.get("badges", [])
            tip = "Uphill assist enabled"
            if bump:
                tip += f" (applying +{bump} rpm to downshift bands)"
            # ensure we don't duplicate the same badge each tick
            badges = [b for b in badges if b.get("id") != "uphill_assist"]
            badges.append({
                "id": "uphill_assist",
                "active": bool(bump > 0),
                "bump_rpm": int(bump),
                "icon": self._adapt_icon_path,
                "tooltip": tip,
                "position": "value-near",
            })
            rpm_entry["badges"] = badges
            self.snapshot_cache["RPM"] = rpm_entry

        # RPM_smooth: first-order low-pass filter so SoundDrive gets a smooth RPM
        # even when real OBD samples arrive slowly (≈1 s cadence in the field).
        raw_rpm_entry = self.snapshot_cache.get("RPM", {})
        raw_rpm = raw_rpm_entry.get("value")
        if raw_rpm is not None:
            dt = now - self._rpm_smooth_ts if self._rpm_smooth_ts else 0.0
            alpha = 0.25 if dt <= 0.3 else 0.6
            self._rpm_smooth_target = float(raw_rpm)
            self._rpm_smooth = self._rpm_smooth + alpha * (self._rpm_smooth_target - self._rpm_smooth)
            self._rpm_smooth_ts = now
        elif self._rpm_smooth_ts and (now - self._rpm_smooth_ts) < 3.0:
            # No new sample yet: interpolate toward last known target
            elapsed = now - self._rpm_smooth_ts
            alpha = min(1.0, elapsed * 2.0)  # ~0.5 s to fully reach target
            self._rpm_smooth = self._rpm_smooth + alpha * (self._rpm_smooth_target - self._rpm_smooth)
        self.snapshot_cache["RPM_smooth"] = {
            "value": self._rpm_smooth,
            "unit": "rpm",
            "text": str(int(self._rpm_smooth)),
            "color": raw_rpm_entry.get("color", ColorGREEN),
            "value_scale": self.value_scale,
        }

        # Warm-up overlays (blue text + cold icon)
        for _m in ("RPM", "Coolant", "Oil Temp"):
            self._apply_cold_overlay(_m)

        # Safe to turn off (independent status)
        self.snapshot_cache["TurnOff"] = self._compute_turnoff(now)

        # Pseudo Steering snapshot entry (for Sound Drive)
        self.snapshot_cache["Steering"] = {
            "value": self._steer_value,
            "unit": "",
            "text": f"{self._steer_value:.2f}",
            "color": ColorNA,
            "tooltip": "Pseudo steering estimate for Sound Drive (-1 = left, +1 = right).",
            "value_scale": 1.0,
        }

        # CAN-sourced metrics with no PID counterpart (e.g. "Battery SoC"):
        # they never pass through the registry loop above, so give them a
        # snapshot entry here while fresh.
        mono_now = time.monotonic()
        with self._can_lock:
            can_extra = {k: v for k, (v, ts) in self._can_values.items()
                         if k not in METRIC_REGISTRY and (mono_now - ts) <= self._CAN_FRESH_S}
        for k, v in can_extra.items():
            try:
                fv = float(v)
                self.snapshot_cache[k] = {"value": fv, "unit": "", "text": f"{fv:g}", "color": ColorGREEN, "value_scale": 1.0}
            except (TypeError, ValueError):
                pass

        # CAN-decoded states (brake, ignition, engine_on, reverse, hazard…):
        # "CAN <key>" entries, 1.0/0.0 for booleans, for alerts and the debugger.
        with self._can_lock:
            can_states = dict(self._can_states)
        for k, v in can_states.items():
            if isinstance(v, bool):
                self.snapshot_cache["CAN " + k] = {
                    "value": 1.0 if v else 0.0, "unit": "", "text": "ON" if v else "OFF",
                    "color": ColorGREEN if v else ColorNA, "value_scale": 1.0,
                    "telemetry_color_locked": True,
                }
            else:
                self.snapshot_cache["CAN " + k] = {
                    "value": v, "unit": "", "text": str(v), "color": ColorNA, "value_scale": 1.0,
                }

        # CAN clutch switch (None when not configured / no CAN data)
        self.snapshot_cache["Clutch"] = {
            "value": self._clutch_state,
            "unit": "",
            "text": "ON" if self._clutch_state else ("OFF" if self._clutch_state is not None else "N/A"),
            "color": ColorNA,
        }

    # Public API
    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        return dict(self.snapshot_cache)

    def value(self, name: str, default=0.0):
        """
        Canonical read API from snapshot_cache.
        - Returns float when possible.
        - If value is missing or not convertible, returns `default` unchanged.
        """
        try:
            entry = self.snapshot_cache.get(name) or {}
            v = entry.get("value", None)

            if v is None:
                return default

            return float(v)
        except Exception:
            return default


    def get(self, name: str) -> Optional[float]:
        return self.values.get(name)

