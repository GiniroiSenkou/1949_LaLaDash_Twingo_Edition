"""Vehicle-specific constants, kept out of the code.

`Data/vehicles/default.json` describes the reference vehicle. A user profile
named by `[Vehicle] profile` in settings.ini is overlaid on it key by key.
Read once at import: nothing here is a hot path and nothing polls.
"""
from __future__ import annotations

import json
import os
from typing import Any

_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DIR = os.path.join(_BASE, "Data", "vehicles")


def _read(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _selected_name() -> str:
    try:
        from Modules.Runtime import settings_cache
        name = settings_cache.get(
            "Vehicle", "profile", fallback="",
            path=os.path.join(_BASE, "Data", "settings.ini"),
        )
        return os.path.basename(str(name or "").strip())  # no path tricks
    except Exception:
        return ""


def _load() -> dict:
    prof = _read(os.path.join(_DIR, "default.json"))
    name = _selected_name()
    if name and name != "default":
        prof = _merge(prof, _read(os.path.join(_DIR, name + ".json")))
    return prof


PROFILE: dict = _load()


def get(path: str, default: Any = None) -> Any:
    """`get("engine.displacement_m3")` — dotted lookup into the profile."""
    node: Any = PROFILE
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node
