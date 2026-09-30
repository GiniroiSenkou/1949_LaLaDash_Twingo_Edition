"""
Style registry — one palette per visual style.

A style is a palette dict here plus a folder under ``Styles/<Name>/`` containing a
``Shell.qml``. Exactly one style is loaded at boot (``[UI] style`` in settings.ini);
nothing belonging to an inactive style is ever parsed, instantiated or textured.

To add a style: add an entry to ``STYLES`` and create ``Styles/<Name>/Shell.qml``.
"""

from __future__ import annotations

DEFAULT_STYLE = "Modern"


# ── Modern ────────────────────────────────────────────────────────────────
# Flat dark surfaces, one accent, no gradients or glass. Derived from the
# "LalaDash Modern" mockup. Hover/pressed/active are explicit so they stay
# selectable rather than being computed at the call site.
MODERN = {
    "ground":          "#0e0f11",
    "surface":         "#16181b",
    "surfaceRaised":   "#1b1d21",
    "surfaceHover":    "#22252a",
    "surfacePressed":  "#2a2e34",

    "accent":          "#6fb2ff",
    "accentHover":     "#8cc2ff",
    "accentPressed":   "#5a9ee6",
    "accentInk":       "#0e0f11",
    "accentSoft":      "rgba(111,178,255,0.14)",

    "ink":             "#eef1f4",
    "inkDim":          "rgba(238,241,244,0.62)",
    "inkFaint":        "rgba(238,241,244,0.50)",
    "inkGhost":        "rgba(238,241,244,0.35)",
    "hairline":        "rgba(238,241,244,0.08)",
    "outline":         "rgba(238,241,244,0.18)",

    "ok":              "#5ddc8a",
    "okSoft":          "#7fe6a4",
    "okBg":            "rgba(93,220,138,0.12)",
    "warn":            "#f5b942",
    "danger":          "#ff6b6b",
    "dangerSoft":      "#ff9d9d",
    "dangerBg":        "rgba(255,107,107,0.12)",

    "scrim":           "rgba(0,0,0,0.65)",
    "sheet":           "#0e0f11",

    "fontFamily":      "Manrope",

    # Dimensions
    "statusH":         52,
    "dockH":           88,
    "radiusCard":      20,
    "radiusControl":   14,
    "radiusPill":      22,
    "gap":             16,
    "pad":             22,
}

# Light variant of MODERN (Settings → Themes → Light Theme). Same tokens, so
# every page follows it live; only the colours change, dimensions are shared.
# Cards (surface) sit on a slightly darker ground and tiles inside them
# (surfaceRaised) are inset a shade darker again, mirroring the dark skin.
MODERN_LIGHT = dict(MODERN)
MODERN_LIGHT.update({
    "ground":          "#e4e7ec",
    "surface":         "#f7f8fa",
    "surfaceRaised":   "#eaedf1",
    "surfaceHover":    "#e0e4ea",
    "surfacePressed":  "#d4d9e0",

    "accent":          "#1d6fd6",
    "accentHover":     "#3584e4",
    "accentPressed":   "#185bb0",
    "accentInk":       "#ffffff",
    "accentSoft":      "rgba(29,111,214,0.12)",

    "ink":             "#14171b",
    "inkDim":          "rgba(20,23,27,0.70)",
    "inkFaint":        "rgba(20,23,27,0.58)",
    "inkGhost":        "rgba(20,23,27,0.40)",
    "hairline":        "rgba(20,23,27,0.09)",
    "outline":         "rgba(20,23,27,0.20)",

    "ok":              "#16904c",
    "okSoft":          "#1fa85b",
    "okBg":            "rgba(22,144,76,0.12)",
    "warn":            "#b97a00",
    "danger":          "#d13a3a",
    "dangerSoft":      "#dc5252",
    "dangerBg":        "rgba(209,58,58,0.10)",

    "scrim":           "rgba(20,23,27,0.40)",
    "sheet":           "#f7f8fa",
})

# Type scale for the modern skin. Keyed by the same font-group roles the app
# already uses, so the Font editor keeps working unchanged — it just edits
# [ModernFonts] instead of [FontSettings] while this style is active.
MODERN_FONTS = {
    "settings_primary":          17,
    "settings_primary_text":     18,
    "settings_secondary_text":   14,
    "settings_tertiary_text":    13,
    "top_bar_text":              17,
    "buttons_push_text":         16,
    "buttons_squareaction_text": 15,
    "buttons_multiselect_text":  15,
    "buttons_medium_text":       15,
    "buttons_popup_text":        15,
    "widgets_primary_text":      20,
    "widgets_secondary_text":    15,
    "widgets_tertiary_text":     13,
    "pages_primary_text":        26,
    "pages_secondary_text":      15,
}


STYLES = {
    "Modern": {
        "palette": MODERN,
        "palette_light": MODERN_LIGHT,
        "fonts": MODERN_FONTS,
        "ini_section": "Modern",
        "font_section": "ModernFonts",
    },
}


def resolve(name: str) -> tuple[str, dict]:
    """Return (canonical_name, style_dict) for ``name``, falling back to default."""
    if not name:
        return DEFAULT_STYLE, STYLES[DEFAULT_STYLE]
    for key in STYLES:
        if key.lower() == str(name).strip().lower():
            return key, STYLES[key]
    return DEFAULT_STYLE, STYLES[DEFAULT_STYLE]


def names() -> list[str]:
    return list(STYLES.keys())
