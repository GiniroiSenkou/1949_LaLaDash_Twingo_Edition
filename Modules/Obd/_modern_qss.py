"""
Shared "Modern" QWidget skin for the OBD tool windows.

The OBD tool windows (``obd_simulator``, ``engine_test_window``)
are the only QWidget surfaces left in the app; everything else is QML skinned by
``Styles/Modern``. This module gives them the same look without duplicating a
single colour literal: the palette is imported from
``Modules.Runtime.styles.MODERN``, so editing that dict re-skins these windows
too.

Usage from a window::

    from Modules.Obd._modern_qss import apply_modern, set_surface, set_role

    apply_modern(self)                 # once, on the top-level widget
    set_surface(some_frame, "card")    # depth = a surface step, never a border
    set_role(some_label, "dim")

Design rules (mirrors ``Styles/Modern/CONTRACT.md``):

* Flat surfaces only — no gradients, no glass, no drop shadows.
* Depth is a surface step: ``ground`` -> ``surface`` -> ``surfaceRaised``.
* Borders only as hairlines; never as the thing that defines a panel.
* Buttons sit on ``surfaceRaised``, hover to ``surfaceHover``, and fill with
  ``accent`` + ``accentInk`` text when checked or marked primary.
* Data (logs, tables, byte dumps) is monospace on ``surface``.

Everything is driven by Qt dynamic properties so the whole skin is one
stylesheet installed on the window, rather than a per-widget ``setStyleSheet``
at every call site. Changing a property after the widget is shown requires a
``repolish()``.
"""

from __future__ import annotations

import os
from typing import Optional

from PyQt6.QtCore import QStandardPaths, Qt
from PyQt6.QtGui import QColor, QFontDatabase, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import QApplication, QPushButton, QSizePolicy, QWidget

from Modules.Runtime.styles import MODERN as M

__all__ = [
    "M",
    "apply_modern",
    "modern_qss",
    "ensure_fonts",
    "set_surface",
    "set_role",
    "set_tone",
    "set_btn_role",
    "modern_button",
    "repolish",
    "MONO_STACK",
]


_FONTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "Fonts",
)

_MANROPE_FILES = (
    "Manrope-Regular.ttf",
    "Manrope-Medium.ttf",
    "Manrope-SemiBold.ttf",
    "Manrope-Bold.ttf",
)

#: Family stack used for anything that is data rather than prose.
MONO_STACK = '"JetBrains Mono", "DejaVu Sans Mono", "Liberation Mono", "Consolas", monospace'

_UI_STACK: Optional[str] = None
_ARROWS: Optional[tuple] = None


def ensure_fonts() -> str:
    """Register the bundled Manrope faces once and return the UI family stack.

    Falls back to the platform sans stack if the TTFs are missing or the font
    database rejects them, so the windows still render on a bare system.
    """
    global _UI_STACK
    if _UI_STACK is not None:
        return _UI_STACK

    family = ""
    if QApplication.instance() is not None:
        for name in _MANROPE_FILES:
            path = os.path.join(_FONTS_DIR, name)
            if not os.path.exists(path):
                continue
            try:
                fid = QFontDatabase.addApplicationFont(path)
            except Exception:
                continue
            if fid < 0:
                continue
            try:
                fams = QFontDatabase.applicationFontFamilies(fid)
            except Exception:
                fams = []
            if fams and not family:
                family = str(fams[0])

    if not family:
        family = str(M.get("fontFamily") or "Manrope")

    _UI_STACK = f'"{family}", "Inter", "Noto Sans", "DejaVu Sans", sans-serif'
    return _UI_STACK


def _stepper_arrows() -> tuple[str, str]:
    """Render the spin-box chevrons once and return their file paths.

    Qt's stylesheet engine paints a solid box for a CSS border-triangle on a
    sub-control, and styling ``::up-button`` at all suppresses the platform
    style's own arrow — so the only way to get a real arrow on a themed
    stepper is to hand it an image. Two 9x9 pixmaps, cached next to the app's
    other cache data and keyed by ink colour, cost nothing to keep.

    Returns ``("", "")`` if they cannot be written; the stepper then renders as
    a plain chip, which still works.
    """
    global _ARROWS
    if _ARROWS is not None:
        return _ARROWS

    _ARROWS = ("", "")
    if QApplication.instance() is None:
        return _ARROWS
    try:
        root = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.CacheLocation
        ) or os.path.join(os.path.expanduser("~"), ".cache")
        cache = os.path.join(root, "laladash-qss")
        os.makedirs(cache, exist_ok=True)

        ink = QColor(M["ink"])
        tag = ink.name().lstrip("#")
        paths = []
        for name, up in (("up", True), ("down", False)):
            path = os.path.join(cache, f"stepper-{name}-{tag}.png")
            if not os.path.exists(path):
                pm = QPixmap(18, 18)
                pm.fill(Qt.GlobalColor.transparent)
                painter = QPainter(pm)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                tri = QPainterPath()
                if up:
                    tri.moveTo(4.5, 11.0)
                    tri.lineTo(9.0, 6.0)
                    tri.lineTo(13.5, 11.0)
                else:
                    tri.moveTo(4.5, 7.0)
                    tri.lineTo(9.0, 12.0)
                    tri.lineTo(13.5, 7.0)
                tri.closeSubpath()
                painter.fillPath(tri, ink)
                painter.end()
                if not pm.save(path, "PNG"):
                    return _ARROWS
            paths.append(path.replace("\\", "/"))
        _ARROWS = (paths[0], paths[1])
    except Exception:
        _ARROWS = ("", "")
    return _ARROWS


def modern_qss() -> str:
    """Build the whole skin from the ``MODERN`` palette."""
    ui = ensure_fonts()
    up_png, down_png = _stepper_arrows()
    arrows = ""
    if up_png and down_png:
        arrows = (
            "QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {"
            f" image: url({up_png}); width: 9px; height: 9px; }}\n"
            "QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {"
            f" image: url({down_png}); width: 9px; height: 9px; }}"
        )
    rc = int(M["radiusCard"])
    rk = int(M["radiusControl"])
    rp = int(M["radiusPill"])

    return f"""
/* ── base ─────────────────────────────────────────────────────────────── */
* {{
    font-family: {ui};
    outline: none;
}}
QWidget {{
    background: transparent;
    color: {M['ink']};
    font-size: 14px;
}}
QWidget#ModernRoot,
QFrame[surface="ground"] {{
    background: {M['ground']};
}}

/* ── surfaces (depth is a step, never a border) ───────────────────────── */
QFrame[surface="card"] {{
    background: {M['surface']};
    border: none;
    border-radius: {rc}px;
}}
QFrame[surface="raised"] {{
    background: {M['surfaceRaised']};
    border: none;
    border-radius: {rk}px;
}}
QFrame[surface="sunken"] {{
    background: {M['ground']};
    border: none;
    border-radius: {rk}px;
}}
QFrame[surface="plain"] {{
    background: transparent;
    border: none;
}}
QFrame[surface="dashed"] {{
    background: {M['ground']};
    border: 1px dashed {M['outline']};
    border-radius: {rk}px;
}}
QFrame[surface="dashed"]:hover {{
    border: 1px dashed {M['accent']};
}}
QFrame[surface="row"] {{
    background: {M['surfaceRaised']};
    border: none;
    border-radius: 12px;
}}
QFrame[surface="hairline"] {{
    background: {M['hairline']};
    border: none;
}}

/* ── text ─────────────────────────────────────────────────────────────── */
QLabel {{
    background: transparent;
    border: none;
    color: {M['ink']};
}}
QLabel[role="title"] {{ font-size: 20px; font-weight: 700; letter-spacing: 0.2px; }}
QLabel[role="section"] {{ font-size: 15px; font-weight: 600; }}
QLabel[role="field"] {{ font-size: 13px; font-weight: 600; color: {M['inkDim']}; }}
QLabel[role="dim"] {{ font-size: 13px; color: {M['inkDim']}; }}
QLabel[role="faint"] {{ font-size: 12px; color: {M['inkFaint']}; }}
QLabel[role="ghost"] {{ font-size: 12px; color: {M['inkGhost']}; }}
QLabel[role="value"] {{ font-size: 24px; font-weight: 700; }}
QLabel[role="mono"] {{ font-family: {MONO_STACK}; font-size: 12px; color: {M['inkDim']}; }}

QLabel[tone="accent"] {{ color: {M['accent']}; }}
QLabel[tone="ok"] {{ color: {M['ok']}; }}
QLabel[tone="warn"] {{ color: {M['warn']}; }}
QLabel[tone="danger"] {{ color: {M['danger']}; }}

/* Bit cells in the CAN inspector — a chip, not a label. */
QLabel[role="bit"] {{
    background: {M['surfaceRaised']};
    border: none;
    border-radius: 6px;
    color: {M['inkGhost']};
    font-family: {MONO_STACK};
    font-size: 11px;
    font-weight: 600;
}}
QLabel[role="bit"][tone="on"] {{
    background: {M['okBg']};
    color: {M['ok']};
}}

/* ── buttons ──────────────────────────────────────────────────────────── */
QPushButton {{
    background: {M['surfaceRaised']};
    color: {M['ink']};
    border: none;
    border-radius: {rk}px;
    padding: 8px 16px;
    font-size: 14px;
    font-weight: 600;
}}
QPushButton:hover {{ background: {M['surfaceHover']}; }}
QPushButton:pressed {{ background: {M['surfacePressed']}; }}
QPushButton:disabled {{ background: {M['surface']}; color: {M['inkGhost']}; }}
QPushButton:checked {{ background: {M['accent']}; color: {M['accentInk']}; }}
QPushButton:checked:hover {{ background: {M['accentHover']}; }}
QPushButton:checked:pressed {{ background: {M['accentPressed']}; }}

QPushButton[btn="primary"] {{ background: {M['accent']}; color: {M['accentInk']}; }}
QPushButton[btn="primary"]:hover {{ background: {M['accentHover']}; }}
QPushButton[btn="primary"]:pressed {{ background: {M['accentPressed']}; }}
QPushButton[btn="primary"]:disabled {{ background: {M['surface']}; color: {M['inkGhost']}; }}

QPushButton[btn="ok"] {{ background: {M['okBg']}; color: {M['ok']}; }}
QPushButton[btn="ok"]:hover {{ background: {M['surfaceHover']}; color: {M['okSoft']}; }}

QPushButton[btn="warn"] {{ background: rgba(245,185,66,0.14); color: {M['warn']}; }}
QPushButton[btn="warn"]:hover {{ background: {M['surfaceHover']}; }}

QPushButton[btn="danger"] {{ background: {M['dangerBg']}; color: {M['danger']}; }}
QPushButton[btn="danger"]:hover {{ background: {M['surfaceHover']}; color: {M['dangerSoft']}; }}

QPushButton[btn="ghost"] {{ background: transparent; color: {M['inkDim']}; }}
QPushButton[btn="ghost"]:hover {{ background: {M['surfaceHover']}; color: {M['ink']}; }}

QPushButton[btn="pill"] {{ border-radius: {rp}px; }}
QPushButton[btn="quiet"] {{
    background: {M['surface']};
    color: {M['inkDim']};
    font-size: 12px;
    font-weight: 600;
    padding: 6px 10px;
    border-radius: 10px;
    text-align: left;
}}
QPushButton[btn="quiet"]:hover {{ background: {M['surfaceHover']}; color: {M['ink']}; }}
QPushButton[btn="quiet"]:pressed {{ background: {M['surfacePressed']}; }}
QPushButton[btn="quiet"]:disabled {{ background: {M['surface']}; color: {M['inkGhost']}; }}
QPushButton[btn="chip"] {{
    background: {M['surfaceRaised']};
    color: {M['inkDim']};
    font-size: 12px;
    font-weight: 600;
    padding: 4px 8px;
    border-radius: 10px;
}}
QPushButton[btn="chip"]:hover {{ background: {M['surfaceHover']}; color: {M['ink']}; }}
QPushButton[btn="chip"]:checked {{ background: {M['accent']}; color: {M['accentInk']}; }}

/* ── inputs ───────────────────────────────────────────────────────────── */
QLineEdit, QSpinBox, QDoubleSpinBox {{
    background: {M['ground']};
    color: {M['ink']};
    border: 1px solid {M['hairline']};
    border-radius: {rk}px;
    padding: 7px 10px;
    font-size: 13px;
    selection-background-color: {M['accent']};
    selection-color: {M['accentInk']};
}}
/* leave room for the stepper chips so the value never runs under them */
QSpinBox, QDoubleSpinBox {{ padding-right: 26px; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {M['accent']};
}}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {M['inkGhost']};
}}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 18px;
    margin: 3px 3px 0 0;
    border: none;
    border-radius: 5px;
    background: {M['surfaceRaised']};
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 18px;
    margin: 0 3px 3px 0;
    border: none;
    border-radius: 5px;
    background: {M['surfaceRaised']};
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {M['surfaceHover']};
}}
{arrows}

QCheckBox {{
    background: transparent;
    color: {M['ink']};
    font-size: 13px;
    spacing: 9px;
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 6px;
    border: 1px solid {M['outline']};
    background: {M['surfaceRaised']};
}}
QCheckBox::indicator:hover {{ border: 1px solid {M['accent']}; }}
QCheckBox::indicator:checked {{
    border: 1px solid {M['accent']};
    background: {M['accent']};
}}

/* ── sliders ──────────────────────────────────────────────────────────── */
QSlider::groove:horizontal {{
    height: 4px;
    background: {M['outline']};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {M['accent']};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 18px;
    height: 18px;
    margin: -8px 0;
    background: {M['ink']};
    border-radius: 9px;
}}
QSlider::handle:horizontal:hover {{ background: {M['accentHover']}; }}
QSlider::handle:horizontal:pressed {{ background: {M['accentPressed']}; }}

/* ── data surfaces ────────────────────────────────────────────────────── */
QPlainTextEdit, QTextEdit {{
    background: {M['surface']};
    border: none;
    border-radius: {rk}px;
    padding: 10px;
    color: {M['inkDim']};
    font-family: {MONO_STACK};
    font-size: 12px;
    selection-background-color: {M['accent']};
    selection-color: {M['accentInk']};
}}

QTableWidget, QTableView {{
    background: {M['surface']};
    alternate-background-color: {M['surface']};
    border: none;
    border-radius: {rk}px;
    color: {M['ink']};
    gridline-color: {M['hairline']};
    font-family: {MONO_STACK};
    font-size: 12px;
}}
QTableWidget::item, QTableView::item {{
    padding: 3px 6px;
    border: none;
}}
QTableWidget::item:selected, QTableView::item:selected {{
    background: {M['accentSoft']};
    color: {M['ink']};
}}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{
    background: {M['surfaceRaised']};
    color: {M['inkDim']};
    border: none;
    border-bottom: 1px solid {M['hairline']};
    padding: 7px 6px;
    font-family: {ui};
    font-size: 12px;
    font-weight: 600;
}}
QTableCornerButton::section {{
    background: {M['surfaceRaised']};
    border: none;
}}

QProgressBar {{
    background: {M['ground']};
    border: none;
    border-radius: 4px;
}}
QProgressBar::chunk {{
    background: {M['accent']};
    border-radius: 4px;
}}

/* ── tabs ─────────────────────────────────────────────────────────────── */
QTabWidget::pane {{
    background: transparent;
    border: none;
}}
QTabWidget::tab-bar {{ alignment: left; }}
QTabBar {{ background: transparent; border: none; }}
QTabBar::tab:!selected {{ margin-top: 0; }}
QTabBar::tab {{
    background: transparent;
    color: {M['inkDim']};
    border: none;
    border-radius: {rk}px;
    padding: 9px 20px;
    margin: 0 4px 6px 0;
    font-size: 14px;
    font-weight: 600;
}}
QTabBar::tab:hover {{ background: {M['surfaceHover']}; color: {M['ink']}; }}
QTabBar::tab:selected {{ background: {M['accent']}; color: {M['accentInk']}; }}

/* ── scrolling ────────────────────────────────────────────────────────── */
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {M['outline']};
    border-radius: 5px;
    min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{ background: {M['inkFaint']}; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {M['outline']};
    border-radius: 5px;
    min-width: 32px;
}}
QScrollBar::handle:horizontal:hover {{ background: {M['inkFaint']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0; height: 0; background: none; border: none;
}}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

/* ── splitters, tooltips, dialogs ─────────────────────────────────────── */
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 10px; }}
QSplitter::handle:vertical {{ height: 10px; }}
QSplitter::handle:hover {{ background: {M['hairline']}; }}

QToolTip {{
    background: {M['surfaceRaised']};
    color: {M['ink']};
    border: 1px solid {M['hairline']};
    border-radius: 8px;
    padding: 6px 9px;
}}

QMessageBox, QDialog {{ background: {M['surface']}; }}
QMessageBox QLabel {{ color: {M['ink']}; }}
"""


def apply_modern(widget: QWidget, *, root: bool = True) -> None:
    """Install the skin on a top-level tool window.

    ``root`` paints the window itself on ``ground``; pass ``root=False`` for a
    widget that is embedded in something already painted.
    """
    ensure_fonts()
    if root:
        widget.setObjectName("ModernRoot")
        widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    widget.setStyleSheet(modern_qss())


def repolish(widget: QWidget) -> None:
    """Re-evaluate stylesheet rules after a dynamic property changed."""
    style = widget.style()
    if style is None:
        return
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def _set_prop(widget: QWidget, name: str, value: Optional[str]) -> None:
    widget.setProperty(name, value)
    if widget.isVisible():
        repolish(widget)


def set_surface(widget: QWidget, kind: str) -> None:
    """One of: card, raised, sunken, plain, dashed, row, ground, hairline."""
    _set_prop(widget, "surface", kind)


def set_role(widget: QWidget, role: str) -> None:
    """Label role: title, section, field, dim, faint, ghost, value, mono, bit."""
    _set_prop(widget, "role", role)


def set_tone(widget: QWidget, tone: Optional[str]) -> None:
    """Semantic colour: accent, ok, warn, danger, on (bit chips) — or None."""
    _set_prop(widget, "tone", tone)


def set_btn_role(btn: QPushButton, role: Optional[str]) -> None:
    """Button role: primary, ok, warn, danger, ghost, pill, quiet, chip."""
    _set_prop(btn, "btn", role)


def modern_button(
    btn: QPushButton,
    *,
    text: Optional[str] = None,
    icon=None,
    icon_size: int = 16,
    role: Optional[str] = None,
    height: int = 38,
    min_width: int = 0,
    max_width: int = 0,
) -> QPushButton:
    """Size and tag a button for the modern skin.

    Deliberately does *not* call ``setStyleSheet`` — a per-widget sheet would
    win over the window sheet and re-fragment the styling. It also avoids
    ``setFixedSize`` so buttons size to their text instead of being clipped.
    """
    btn.setStyleSheet("")  # drop any legacy per-widget sheet
    if text is not None:
        btn.setText(text)
    if icon is not None:
        from PyQt6.QtCore import QSize
        from PyQt6.QtGui import QIcon

        btn.setIcon(icon if isinstance(icon, QIcon) else QIcon(str(icon)))
        btn.setIconSize(QSize(int(icon_size), int(icon_size)))

    btn.setMinimumHeight(int(height))
    btn.setMaximumHeight(int(height))
    if min_width:
        btn.setMinimumWidth(int(min_width))
    if max_width:
        btn.setMaximumWidth(int(max_width))
    btn.setSizePolicy(
        QSizePolicy.Policy.Preferred if not max_width else QSizePolicy.Policy.Expanding,
        QSizePolicy.Policy.Fixed,
    )
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if role:
        btn.setProperty("btn", role)
    return btn
