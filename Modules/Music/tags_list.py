"""Tag → icon discovery for the Music page.

The recognized tag set is derived at import time from the PNG files in
`GUI/Icons/Tags/` (the path constant lives in preferences.py). Filenames
are taken as the canonical tag keys (lowercase, `_`-separated); dropping
a new PNG into that folder adds a new tag with zero code changes.

Three sentinels are NOT real tags — they are rendered directly by the
bridge — and are excluded from `TAG_ICONS`:
    default.png   used for the always-on "All" row
    stems.png     used for the "Stems" virtual row
    untagged.png  fallback when a JSON tag has no matching icon
"""

import os

from preferences import TAGS_GUI

_SENTINEL_STEMS = {"default", "stems", "untagged"}


def canonical_tag_key(value) -> str:
    """Normalize any tag string (JSON value, UI selection, icon filename)
    into the canonical form: lowercase, with spaces and `-` collapsed to `_`."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return text.replace(" ", "_").replace("-", "_")


def tag_display_label(key) -> str:
    """Convert a canonical tag key into its UI label.

    `fairy_tail`     → "Fairy Tail"
    `hoodie_&_co`    → "Hoodie & Co"
    `hunter_x_hunter`→ "Hunter X Hunter"

    Each `_`-separated token is capitalized; tokens that are pure
    punctuation (e.g. `&`) pass through untouched.
    """
    text = str(key or "").strip()
    if not text:
        return ""
    parts = []
    for token in text.split("_"):
        if not token:
            continue
        if token.isalnum():
            parts.append(token.capitalize())
        else:
            # Mixed punctuation (e.g. "&") — preserve as-is.
            parts.append(token)
    return " ".join(parts)


def _discover_tag_icons() -> dict:
    """Scan TAGS_GUI for PNGs and return {canonical_key: absolute_path}."""
    try:
        entries = os.listdir(TAGS_GUI)
    except OSError:
        return {}
    out: dict[str, str] = {}
    for name in entries:
        if not name.lower().endswith(".png"):
            continue
        stem = name[:-4]
        key = canonical_tag_key(stem)
        if not key or key in _SENTINEL_STEMS:
            continue
        out[key] = os.path.join(TAGS_GUI, name)
    return out


# Canonical map. Built once at import; the folder rarely changes during a
# session and a restart is the natural way to pick up new icons.
TAG_ICONS: dict = _discover_tag_icons()
