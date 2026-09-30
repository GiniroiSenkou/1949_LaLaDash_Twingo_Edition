import os
import configparser
from typing import Dict, Tuple, Optional

from PyQt6.QtGui import QFont

from Modules.Runtime import settings_cache


# Base path = directory where main.py lives
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "."))
ROOT_DIR = os.path.dirname(BASE_DIR)  # one level up from /LaLaDash

# Music folder (one level above main.py folder)
MUSIC_FOLDER = os.path.join(ROOT_DIR, "Music")
STEMS_FOLDER = os.path.join(ROOT_DIR, "Stems")
LYRICS_FOLDER = os.path.join(ROOT_DIR, "Lyrics")
try:
    os.makedirs(LYRICS_FOLDER, exist_ok=True)
except OSError:
    pass

DATA_SETTINGS = os.path.join(BASE_DIR, "Data/settings.ini")
MUSIC_JSON = os.path.join(BASE_DIR, "Data/music.json")

# A fresh clone has no settings.ini (it is per-user and gitignored): seed it
# from the shipped defaults once, before anything reads it.
try:
    if not os.path.exists(DATA_SETTINGS):
        import shutil
        _default_ini = os.path.join(BASE_DIR, "Data", "settings.default.ini")
        if os.path.exists(_default_ini):
            shutil.copyfile(_default_ini, DATA_SETTINGS)
except OSError:
    pass

# Every audio container the library / player will look at. The old code
# only knew .mp3/.wav/.flac in three separate hard-coded tuples, so an
# .m4a/.ogg/.opus track could be tagged in music.json (added by hand or by
# the legacy GUI) yet never resolve to a path and never get artwork. One
# list, imported everywhere — matched case-insensitively at every call
# site.
AUDIO_EXTENSIONS = (
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus",
    ".wma", ".aiff", ".aif", ".alac", ".mp4",
)

# Odometer storage lives OUTSIDE the project tree so syncing the repo from
# the dev PC to the car never overwrites the car's real accumulated km.
KM_STORE_PATH = os.path.join(os.path.expanduser("~"), "Documents", "laladash_km.json")
# The car's total odometer, split into its own plain-text file (just the
# number, nothing else) so it can be corrected by hand — in a text editor over
# SSH, or via Settings -> Trips -> Total Distance — without touching the JSON
# blob above, which still holds today's running total and the fuel-tracking
# bookkeeping.
KM_TOTAL_TXT_PATH = os.path.join(os.path.expanduser("~"), "Documents", "laladash_total_km.txt")
# Trip-log recordings (Settings → Debugger → Trip logging). Outside the repo
# so a deploy never syncs or deletes the car's captures.
# Fuel prices, refuels and finished trips (Trip page). Outside the repo for
# the same reason as the km store: a PC→car sync must never overwrite it.
FUEL_LEDGER_PATH = os.path.join(os.path.expanduser("~"), "Documents", "laladash_fuel.json")
TRIP_LOG_DIR = os.path.join(os.path.expanduser("~"), "Documents", "laladash_logs")
# Seeded into KM_TOTAL_TXT_PATH on first run if the file doesn't exist yet.
KM_INITIAL_TOTAL_KM = 0.0  # set your odometer in ~/Documents/laladash_total_km.txt
TAGS_GUI = os.path.join(BASE_DIR, "GUI/Icons/Tags")

icons_size = 300
icons_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "GUI/Icons/"))
screen_resolution = (1280, 800)
DEFAULT_FONT_FAMILY = "Arial"


DEFAULT_TYPOGRAPHY: Dict[str, Tuple[int, int]] = {
    "huge": (40, int(QFont.Weight.Normal)),
    "huge_bold": (40, int(QFont.Weight.Bold)),
    "medium": (30, int(QFont.Weight.Normal)),
    "medium_bold": (30, int(QFont.Weight.Bold)),
    "small": (20, int(QFont.Weight.Normal)),
    "small_bold": (20, int(QFont.Weight.Bold)),
    "very_small": (10, int(QFont.Weight.Normal)),
    "very_small_bold": (10, int(QFont.Weight.Bold)),
    "title": (24, int(QFont.Weight.Bold)),
    "subtitle": (16, int(QFont.Weight.Normal)),
    "body": (14, int(QFont.Weight.Normal)),
    "caption": (12, int(QFont.Weight.Normal)),
    "button": (15, int(QFont.Weight.DemiBold)),
}

_WEIGHT_NAME_TO_VALUE = {
    "thin": int(QFont.Weight.Thin),
    "extralight": int(QFont.Weight.ExtraLight),
    "light": int(QFont.Weight.Light),
    "normal": int(QFont.Weight.Normal),
    "medium": int(QFont.Weight.Medium),
    "demibold": int(QFont.Weight.DemiBold),
    "bold": int(QFont.Weight.Bold),
    "extrabold": int(QFont.Weight.ExtraBold),
    "black": int(QFont.Weight.Black),
}


def _parse_typography_value(raw: str, fallback: Tuple[int, int]) -> Tuple[int, int]:
    s = str(raw or "").strip()
    if not s:
        return fallback
    if "," in s:
        left, right = [x.strip() for x in s.split(",", 1)]
        try:
            size = max(8, min(96, int(left)))
        except Exception:
            size = fallback[0]
        if right.isdigit():
            weight = int(right)
        else:
            weight = _WEIGHT_NAME_TO_VALUE.get(right.lower(), fallback[1])
        return size, int(weight)
    try:
        size = max(8, min(96, int(s)))
        return size, fallback[1]
    except Exception:
        return fallback


def _load_typography() -> Dict[str, Tuple[int, int]]:
    out: Dict[str, Tuple[int, int]] = dict(DEFAULT_TYPOGRAPHY)
    cfg = settings_cache.load_copy(DATA_SETTINGS)
    if not cfg.has_section("Typography"):
        return out

    sec = cfg["Typography"]
    for role, default in DEFAULT_TYPOGRAPHY.items():
        out[role] = _parse_typography_value(sec.get(role, ""), default)
    return out


TYPOGRAPHY: Dict[str, Tuple[int, int]] = _load_typography()


def read_font_family(fallback: str = DEFAULT_FONT_FAMILY) -> str:
    cfg = settings_cache.load_copy(DATA_SETTINGS)
    family = cfg.get("FontSettings", "family", fallback=fallback).strip()
    return family or fallback


def typography_debug_enabled(role: str) -> bool:
    cfg = settings_cache.load_copy(DATA_SETTINGS)
    raw = cfg.get("TypographyDebug", role, fallback="0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def reload_typography() -> None:
    global TYPOGRAPHY
    global font_huge_size, font_medium_size, font_small_size, font_very_small_size
    global font_huge_bold, font_medium_bold, font_small_bold, font_very_small_bold
    global font_huge, font_medium, font_small, font_very_small

    TYPOGRAPHY = _load_typography()
    family = read_font_family()

    font_huge_size = TYPOGRAPHY["huge"][0]
    font_medium_size = TYPOGRAPHY["medium"][0]
    font_small_size = TYPOGRAPHY["small"][0]
    font_very_small_size = TYPOGRAPHY["very_small"][0]

    font_huge_bold = QFont(family, *TYPOGRAPHY["huge_bold"])
    font_medium_bold = QFont(family, *TYPOGRAPHY["medium_bold"])
    font_small_bold = QFont(family, *TYPOGRAPHY["small_bold"])
    font_very_small_bold = QFont(family, *TYPOGRAPHY["very_small_bold"])
    font_huge = QFont(family, *TYPOGRAPHY["huge"])
    font_medium = QFont(family, *TYPOGRAPHY["medium"])
    font_small = QFont(family, *TYPOGRAPHY["small"])
    font_very_small = QFont(family, *TYPOGRAPHY["very_small"])


def get_selected_car(ini_path: str = DATA_SETTINGS) -> tuple[str, str]:
    cfg = settings_cache.load_copy(ini_path)
    raw = cfg.get("SoundFeatures", "last_car", fallback="").strip()
    if "/" in raw:
        brand, model = raw.split("/", 1)
        return brand.strip(), model.strip()
    return "", raw


def is_car_theme_active(ini_path: str = DATA_SETTINGS) -> bool:
    cfg = settings_cache.load_copy(ini_path)
    enabled = cfg.get("SoundFeatures", "engine_sound_enabled", fallback="true").strip().lower()
    preset = cfg.get("SoundFeatures", "last_preset", fallback="Quiet").strip().lower()
    return (enabled in ("1", "true", "yes", "on")) and (preset != "quiet")


def resolve_car_asset(
    name: str,
    *,
    fallback_to_icons: bool = True,
    prefer_brand_for_logo: bool = True,
    require_theme_active: bool = True,
) -> str:
    """Resolve a car-specific asset path from selected car, fallback to GUI/Icons."""
    brand, model = get_selected_car()
    candidates: list[str] = []
    theme_active = is_car_theme_active() if require_theme_active else True
    if theme_active and brand and model:
        model_dir = os.path.join(BASE_DIR, "Cars", brand, model)
        brand_dir = os.path.join(BASE_DIR, "Cars", brand)
        if prefer_brand_for_logo and name.lower().startswith("logo"):
            candidates.extend([os.path.join(brand_dir, name), os.path.join(model_dir, name)])
        else:
            candidates.extend([os.path.join(model_dir, name), os.path.join(brand_dir, name)])
    elif theme_active and model:
        # legacy model-only format: search all brands
        cars_root = os.path.join(BASE_DIR, "Cars")
        if os.path.isdir(cars_root):
            for b in os.listdir(cars_root):
                model_dir = os.path.join(cars_root, b, model)
                if os.path.isdir(model_dir):
                    candidates.append(os.path.join(model_dir, name))
                    candidates.append(os.path.join(cars_root, b, name))
                    break

    if fallback_to_icons:
        candidates.append(os.path.join(icons_path, name))
    for p in candidates:
        if os.path.exists(p):
            return p
    return ""


# ─── Theme-colour helpers (single source of truth) ───────────────────────────
def _read_settings_cfg() -> configparser.ConfigParser:
    """Read Data/settings.ini once and return the ConfigParser."""
    return settings_cache.load_copy(DATA_SETTINGS)


def _hex_to_rgb(s: str, fallback: tuple) -> tuple:
    s = (s or "").strip()
    if s.startswith("#") and len(s) == 7:
        try:
            return (int(s[1:3], 16), int(s[3:5], 16), int(s[5:7], 16))
        except Exception:
            pass
    return fallback


def read_base_rgb() -> tuple[int, int, int]:
    """Return (r, g, b) from [Style] base_color in settings.ini."""
    cfg = _read_settings_cfg()
    hex_c = cfg.get("Style", "base_color", fallback="#2a2f3a").strip()
    return _hex_to_rgb(hex_c, (42, 47, 58))


def read_style_color(key: str, fallback: str = "#2a2f3a") -> tuple[int, int, int]:
    """Return (r, g, b) for any [Style] color key in settings.ini."""
    cfg = _read_settings_cfg()
    hex_c = cfg.get("Style", key, fallback=fallback).strip()
    return _hex_to_rgb(hex_c, _hex_to_rgb(fallback, (42, 47, 58)))


def read_style_bool(key: str, fallback: bool = False) -> bool:
    """Return a boolean from [Style] in settings.ini."""
    cfg = _read_settings_cfg()
    raw = cfg.get("Style", key, fallback="true" if fallback else "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def read_style_str(key: str, fallback: str = "") -> str:
    """Return a string from [Style] in settings.ini."""
    cfg = _read_settings_cfg()
    return cfg.get("Style", key, fallback=fallback).strip()


def read_style_int(key: str, fallback: int = 0) -> int:
    """Return an int from [Style] in settings.ini."""
    cfg = _read_settings_cfg()
    try:
        return int(float(cfg.get("Style", key, fallback=str(fallback)).strip()))
    except Exception:
        return fallback


def lighten_rgb(rgb: tuple, amt: int) -> tuple:
    """Return *rgb* with each channel increased by *amt*, clamped to 255."""
    return tuple(min(255, c + amt) for c in rgb)


def darken_rgb(rgb: tuple, amt: int) -> tuple:
    """Return *rgb* with each channel decreased by *amt*, clamped to 0."""
    return tuple(max(0, c - amt) for c in rgb)


# ═══════════════════════════════════════════════════════════════════════════════
# COLOUR CONSTANTS  (see UI_NAMING.txt → THEME VARIABLES)
# Change here → changes everywhere.
# ═══════════════════════════════════════════════════════════════════════════════

# --- Debug overlay colour (purple tint when debug flags are on) ---
DEBUG_COLOR = "#a855f7"

# --- Telemetry / status colours (used by OBD, home, settings, modding) ---
COLOR_STATUS_GREEN   = "#1f9d55"
COLOR_STATUS_YELLOW  = "#f59e0b"
COLOR_STATUS_ORANGE  = "#f97316"
COLOR_STATUS_RED     = "#dc2626"
COLOR_STATUS_NA      = "#3a3f4a"

# --- Modding engine-arc indicator colours ---
COLOR_INDICATOR_GREEN  = "#3cdc5a"
COLOR_INDICATOR_RED    = "#ff4646"

# --- Dash-light colours ---
COLOR_DASHLIGHT_ON     = "#c1121f"
COLOR_DASHLIGHT_OFF    = "#22c55e"
COLOR_DASHLIGHT_UNKNOWN = "#6b7280"

# --- Text colour tokens ---
TEXT_PRIMARY   = "rgba(255,255,255,0.92)"
TEXT_SECONDARY = "rgba(255,255,255,0.75)"
TEXT_TERTIARY  = "rgba(255,255,255,0.55)"
TEXT_MUTED     = "rgba(255,255,255,0.28)"
TEXT_WHITE     = "#ffffff"
TEXT_VALUE_BLUE = "#60a5fa"

# --- Surface / background tokens ---
SURFACE_DARK       = "#0b0f12"
SURFACE_DIALOG     = "rgba(20,20,26,0.96)"
SURFACE_ICON_BG    = "rgba(34,34,51,0.80)"

# --- Base-color fallback (used when settings.ini has no value) ---
BASE_COLOR_FALLBACK = "#2a2f3a"


# ═══════════════════════════════════════════════════════════════════════════════
# FONT GROUP SYSTEM  (see UI_NAMING.txt → FONT VARIABLES)
# Single reader function — replaces per-file duplicates.
# Reads from [FontSettings] in settings.ini, falls back to defaults below.
# ═══════════════════════════════════════════════════════════════════════════════

FONT_GROUP_DEFAULTS: Dict[str, Dict] = {
    # Bars
    "top_bar_text":             {"size": 15, "spacing": 0.0, "style": "bold"},
    # Settings
    "settings_primary":         {"size": 29, "spacing": 0.0, "style": "bold"},
    "settings_secondary":       {"size": 18, "spacing": 0.0, "style": "regular"},
    "settings_primary_text":    {"size": 16, "spacing": 0.0, "style": "bold"},
    "settings_secondary_text":  {"size": 14, "spacing": 0.0, "style": "regular"},
    "settings_tertiary_text":   {"size": 12, "spacing": 0.0, "style": "regular"},
    # Buttons
    "buttons_medium_text":      {"size": 16, "spacing": 0.0, "style": "bold"},
    "buttons_squareaction_text": {"size": 14, "spacing": 0.0, "style": "bold"},
    "buttons_multiselect_text": {"size": 18, "spacing": 0.0, "style": "bold"},
    "buttons_push_text":        {"size": 22, "spacing": 0.0, "style": "bold"},
    "buttons_popup_text":       {"size": 16, "spacing": 0.0, "style": "bold"},
    # Widgets
    "widgets_primary_text":     {"size": 20, "spacing": 0.0, "style": "bold"},
    "widgets_secondary_text":   {"size": 18, "spacing": 0.0, "style": "regular"},
    "widgets_tertiary_text":    {"size": 14, "spacing": 0.0, "style": "regular"},
    # Pages
    "pages_primary_text":       {"size": 20, "spacing": 0.0, "style": "bold"},
    "pages_secondary_text":     {"size": 16, "spacing": 0.0, "style": "regular"},
    "modding_primary":          {"size": 30, "spacing": 0.0, "style": "bold"},
}

_FONT_STYLE_MAP = {
    "regular":      (int(QFont.Weight.Normal),    False),
    "bold":         (int(QFont.Weight.Bold),       False),
    "italic":       (int(QFont.Weight.Normal),     True),
    "bold_italic":  (int(QFont.Weight.Bold),       True),
    "light":        (int(QFont.Weight.Light),      False),
    "light_italic": (int(QFont.Weight.Light),      True),
    "black":        (int(QFont.Weight.Black),      False),
}


def load_font_group(group: str, defaults: Optional[Dict] = None) -> Dict:
    """
    Read a named font group from [FontSettings] in settings.ini.

    Returns dict with keys: size, spacing, style, weight, italic, debug.
    If *defaults* is None the built-in FONT_GROUP_DEFAULTS entry is used.
    """
    if defaults is None:
        defaults = FONT_GROUP_DEFAULTS.get(group, {"size": 14, "spacing": 0.0, "style": "regular"})

    cfg = _read_settings_cfg()
    prefix = str(group or "").strip().lower().replace(" ", "_")

    size = int(defaults.get("size", 14))
    spacing = float(defaults.get("spacing", 0.0))
    style = str(defaults.get("style", "regular"))
    debug = False

    try:
        size = max(8, min(96, int(cfg.get("FontSettings", f"{prefix}_size", fallback=str(size)))))
    except Exception:
        pass
    try:
        spacing = max(-2.0, min(12.0, float(cfg.get("FontSettings", f"{prefix}_spacing", fallback=str(spacing)))))
    except Exception:
        pass
    try:
        style = cfg.get("FontSettings", f"{prefix}_style", fallback=style).strip().lower() or style
    except Exception:
        pass
    try:
        raw_dbg = cfg.get("FontSettings", f"{prefix}_debug", fallback="0").strip().lower()
        debug = raw_dbg in ("1", "true", "yes", "on")
    except Exception:
        pass

    if style not in _FONT_STYLE_MAP:
        style = "regular"
    weight, italic = _FONT_STYLE_MAP[style]

    return {
        "size": size,
        "spacing": spacing,
        "style": style,
        "weight": weight,
        "italic": italic,
        "debug": debug,
    }


# Keep backward-compatible globals populated on import.
reload_typography()
