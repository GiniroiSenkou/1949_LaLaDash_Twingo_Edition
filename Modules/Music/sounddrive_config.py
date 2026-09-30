from __future__ import annotations

import configparser


DEFAULT_SOUND_DRIVE_METHOD = "damuci"
DEFAULT_SOUND_DRIVE_PERFORMANCE = "rpi4_safe"
DEFAULT_MIX_MODE = "Mode A"
DEFAULT_SOUND_DRIVE_PLAYER_ACTIVE = False

_BOOLEAN_TRUE = {"1", "true", "yes", "on"}

_MUSIC_ALIASES = {
    "sound_drive": ("sounddrive", "sound_drive_enabled", "sounddrive_enabled"),
    "auto_mix": ("automix",),
    "mix_mode": ("mode", "sound_drive_mode", "sounddrive_mode", "mixmode"),
    "sound_drive_method": ("sounddrive_method", "method"),
    "sound_drive_performance": ("sounddrive_performance", "performance", "performance_mode"),
    "sound_drive_metadata_cache": (
        "metadata_cache",
        "sounddrive_metadata_cache",
        "sounddrive_save_metadata",
    ),
    "sound_drive_player_active": (
        "player_sound_drive",
        "player_sounddrive",
        "sounddrive_player_active",
        "sound_drive_active",
    ),
}


def _bool_str(value: bool) -> str:
    return "1" if bool(value) else "0"


def _read_raw(section: configparser.SectionProxy, key: str, fallback: str = "") -> str:
    raw = section.get(key, fallback=fallback)
    return str(raw or "").strip()


def _read_bool(section: configparser.SectionProxy, key: str, default: bool) -> bool:
    raw = _read_raw(section, key, _bool_str(default)).lower()
    return raw in _BOOLEAN_TRUE


def _canonical_method(raw: str) -> str:
    clean = str(raw or "").strip().lower()
    if clean in {"new_method", "realtime", "real_time", "new"}:
        return "realtime"
    if clean in {"damuci", "damuci_sound", "damuci sound"}:
        return DEFAULT_SOUND_DRIVE_METHOD
    if clean == "":
        return ""
    return DEFAULT_SOUND_DRIVE_METHOD


def _canonical_performance(raw: str) -> str:
    clean = str(raw or "").strip().lower()
    if clean in {"full", "full_power", "high"}:
        return "full_power"
    return DEFAULT_SOUND_DRIVE_PERFORMANCE


def _canonical_mix_mode(raw: str) -> str:
    clean = str(raw or "").strip().upper()
    if clean in {"A", "MODE A", "MIX A"}:
        return "Mode A"
    if clean in {"B", "MODE B", "MIX B"}:
        return "Mode B"
    if clean in {"C", "MODE C", "MIX C"}:
        return "Mode C"
    if clean in {"D", "MODE D", "MIX D"}:
        return "Mode D"
    if clean in {"E", "MODE E", "MIX E"}:
        return "Mode E"
    return DEFAULT_MIX_MODE


def _read_with_aliases(section: configparser.SectionProxy, key: str, fallback: str = "") -> str:
    names = (key,) + tuple(_MUSIC_ALIASES.get(key, ()))
    for name in names:
        raw = _read_raw(section, name, "")
        if raw:
            return raw
    return fallback


def normalize_music_settings(cfg: configparser.ConfigParser) -> dict[str, object]:
    if not cfg.has_section("Music"):
        cfg.add_section("Music")
    music = cfg["Music"]
    return {
        "sound_drive": _read_bool(
            music,
            "sound_drive",
            _read_with_aliases(music, "sound_drive", "0").lower() in _BOOLEAN_TRUE,
        ),
        "auto_mix": _read_bool(
            music,
            "auto_mix",
            _read_with_aliases(music, "auto_mix", "0").lower() in _BOOLEAN_TRUE,
        ),
        "mix_mode": _canonical_mix_mode(_read_with_aliases(music, "mix_mode", DEFAULT_MIX_MODE)),
        "sound_drive_method": _canonical_method(
            _read_with_aliases(music, "sound_drive_method", DEFAULT_SOUND_DRIVE_METHOD)
        ),
        "sound_drive_performance": _canonical_performance(
            _read_with_aliases(
                music,
                "sound_drive_performance",
                DEFAULT_SOUND_DRIVE_PERFORMANCE,
            )
        ),
        "sound_drive_metadata_cache": _read_bool(
            music,
            "sound_drive_metadata_cache",
            _read_with_aliases(music, "sound_drive_metadata_cache", "0").lower() in _BOOLEAN_TRUE,
        ),
        "sound_drive_player_active": _read_bool(
            music,
            "sound_drive_player_active",
            _read_with_aliases(
                music,
                "sound_drive_player_active",
                _bool_str(DEFAULT_SOUND_DRIVE_PLAYER_ACTIVE),
            ).lower() in _BOOLEAN_TRUE,
        ),
    }


def apply_normalized_music_settings(
    cfg: configparser.ConfigParser,
    values: dict[str, object] | None = None,
) -> dict[str, object]:
    normalized = normalize_music_settings(cfg)
    if values:
        normalized.update(values)
    if not cfg.has_section("Music"):
        cfg.add_section("Music")
    music = cfg["Music"]
    music["sound_drive"] = _bool_str(bool(normalized["sound_drive"]))
    music["auto_mix"] = _bool_str(bool(normalized["auto_mix"]))
    music["mix_mode"] = _canonical_mix_mode(str(normalized["mix_mode"]))
    music["sound_drive_method"] = _canonical_method(str(normalized["sound_drive_method"]))
    music["sound_drive_performance"] = _canonical_performance(
        str(normalized["sound_drive_performance"])
    )
    music["sound_drive_metadata_cache"] = _bool_str(
        bool(normalized["sound_drive_metadata_cache"])
    )
    music["sound_drive_player_active"] = _bool_str(
        bool(normalized["sound_drive_player_active"])
    )

    for canonical, aliases in _MUSIC_ALIASES.items():
        for alias in aliases:
            if alias != canonical and cfg.has_option("Music", alias):
                cfg.remove_option("Music", alias)
    return normalized


def sounddrive_player_toggle_visible(
    *,
    sound_drive_enabled: bool,
    current_track_key: str,
) -> bool:
    return bool(sound_drive_enabled and str(current_track_key or "").strip())
