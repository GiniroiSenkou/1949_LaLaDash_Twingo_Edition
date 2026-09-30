from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor

from bridges.settings_bridge import SettingsBridge
from Modules.Runtime import settings_cache, styles
from preferences import DATA_SETTINGS


class ThemeBridge(QObject):
    """
    Theme constants + dynamic colors exposed as a context property.
    Exposed to QML as the `Theme` context property.

    Carries two layers:
      * the legacy token set, derived from the user's [Style] colors;
      * the active style's flat palette (see Modules/Runtime/styles.py).
    While a style other than "Legacy" is active the overlapping legacy tokens are
    remapped onto that palette, so shared components render correctly either way.
    """

    changed = pyqtSignal()

    def __init__(self, settings: SettingsBridge, parent=None):
        super().__init__(parent)
        self._s = settings
        self._style_name, self._style = styles.resolve(settings.styleName)
        self._pal_cache = None
        self._font_cache = None
        # Settings → Themes → Light Theme. Falls back to the old "Enable Dark
        # Mode" key, which meant dark *text* — i.e. the same light look.
        self._light = settings_cache.getboolean(
            "UI", "light_theme",
            fallback=settings_cache.getboolean("UI", "dark_mode_enabled", fallback=False, path=DATA_SETTINGS),
            path=DATA_SETTINGS,
        )
        settings.themeChanged.connect(self._on_settings_changed)

    # ── active style palette ──────────────────────────────────────────────
    def _on_settings_changed(self):
        self._pal_cache = None
        self._font_cache = None
        self.changed.emit()

    @property
    def _pal(self) -> dict:
        """Palette for the active style, with per-key overrides from settings.ini."""
        if self._pal_cache is None:
            if self._light and self._style.get("palette_light"):
                # Per-key [Modern] overrides are tuned for the dark skin, so
                # the light palette is used as shipped.
                self._pal_cache = dict(self._style["palette_light"])
                return self._pal_cache
            pal = dict(self._style["palette"])
            section = self._style.get("ini_section")
            if section:
                for key in list(pal.keys()):
                    override = self._s.getValue(section, key)
                    if override:
                        pal[key] = override
            self._pal_cache = pal
        return self._pal_cache

    def _c(self, key: str) -> QColor:
        """Palette entry as a QColor. Accepts #rrggbb and rgba(r,g,b,a)."""
        raw = str(self._pal.get(key, "")).strip()
        if raw.startswith("rgba(") and raw.endswith(")"):
            parts = [p.strip() for p in raw[5:-1].split(",")]
            if len(parts) == 4:
                try:
                    return QColor(int(float(parts[0])), int(float(parts[1])),
                                  int(float(parts[2])), int(float(parts[3]) * 255))
                except ValueError:
                    pass
        c = QColor(raw)
        return c if c.isValid() else QColor("#ff00ff")

    def _i(self, key: str) -> int:
        try:
            return int(self._pal.get(key, 0))
        except (TypeError, ValueError):
            return 0

    @pyqtProperty(str, notify=changed)
    def styleName(self):
        return self._style_name

    @pyqtProperty(bool, notify=changed)
    def lightTheme(self):
        return self._light

    @pyqtSlot(bool)
    def setLightTheme(self, on):
        on = bool(on)
        if on == self._light:
            return
        self._light = on
        settings_cache.set_value("UI", "light_theme", "true" if on else "false", path=DATA_SETTINGS)
        self._pal_cache = None
        self.changed.emit()

    # ── modern palette tokens ─────────────────────────────────────────────
    @pyqtProperty(QColor, notify=changed)
    def ground(self):
        return self._c("ground")

    @pyqtProperty(QColor, notify=changed)
    def surface(self):
        return self._c("surface")

    @pyqtProperty(QColor, notify=changed)
    def surfaceRaised(self):
        return self._c("surfaceRaised")

    @pyqtProperty(QColor, notify=changed)
    def surfaceHover(self):
        return self._c("surfaceHover")

    @pyqtProperty(QColor, notify=changed)
    def surfacePressed(self):
        return self._c("surfacePressed")

    @pyqtProperty(QColor, notify=changed)
    def accent(self):
        return self._c("accent")

    @pyqtProperty(QColor, notify=changed)
    def accentHover(self):
        return self._c("accentHover")

    @pyqtProperty(QColor, notify=changed)
    def accentPressed(self):
        return self._c("accentPressed")

    @pyqtProperty(QColor, notify=changed)
    def accentInk(self):
        return self._c("accentInk")

    @pyqtProperty(QColor, notify=changed)
    def accentSoft(self):
        return self._c("accentSoft")

    @pyqtProperty(QColor, notify=changed)
    def ink(self):
        return self._c("ink")

    @pyqtProperty(QColor, notify=changed)
    def inkDim(self):
        return self._c("inkDim")

    @pyqtProperty(QColor, notify=changed)
    def inkFaint(self):
        return self._c("inkFaint")

    @pyqtProperty(QColor, notify=changed)
    def inkGhost(self):
        return self._c("inkGhost")

    @pyqtProperty(QColor, notify=changed)
    def hairline(self):
        return self._c("hairline")

    @pyqtProperty(QColor, notify=changed)
    def outline(self):
        return self._c("outline")

    @pyqtProperty(QColor, notify=changed)
    def ok(self):
        return self._c("ok")

    @pyqtProperty(QColor, notify=changed)
    def okSoft(self):
        return self._c("okSoft")

    @pyqtProperty(QColor, notify=changed)
    def okBg(self):
        return self._c("okBg")

    @pyqtProperty(QColor, notify=changed)
    def warn(self):
        return self._c("warn")

    @pyqtProperty(QColor, notify=changed)
    def danger(self):
        return self._c("danger")

    @pyqtProperty(QColor, notify=changed)
    def dangerSoft(self):
        return self._c("dangerSoft")

    @pyqtProperty(QColor, notify=changed)
    def dangerBg(self):
        return self._c("dangerBg")

    @pyqtProperty(QColor, notify=changed)
    def scrim(self):
        return self._c("scrim")

    @pyqtProperty(QColor, notify=changed)
    def sheet(self):
        return self._c("sheet")

    @pyqtProperty(int, notify=changed)
    def statusH(self):
        return self._i("statusH")

    @pyqtProperty(int, notify=changed)
    def dockH(self):
        return self._i("dockH")

    @pyqtProperty(int, notify=changed)
    def radiusCard(self):
        return self._i("radiusCard")

    @pyqtProperty(int, notify=changed)
    def radiusControl(self):
        return self._i("radiusControl")

    @pyqtProperty(int, notify=changed)
    def radiusPill(self):
        return self._i("radiusPill")

    @pyqtProperty(int, notify=changed)
    def gap(self):
        return self._i("gap")

    @pyqtProperty(int, notify=changed)
    def pad(self):
        return self._i("pad")

    # ── Colors from settings ──────────────────────────────────────────────
    @pyqtProperty(QColor, notify=changed)
    def baseColor(self):
        return self._s.baseColor

    @pyqtProperty(QColor, notify=changed)
    def baseLightened(self):
        return self._s.baseLightened

    @pyqtProperty(QColor, notify=changed)
    def baseDarkened(self):
        return self._s.baseDarkened

    @pyqtProperty(QColor, notify=changed)
    def pressedColor(self):
        return self._s.pressedColor

    @pyqtProperty(QColor, notify=changed)
    def secondaryColor(self):
        return self._s.secondaryColor

    @pyqtProperty(QColor, notify=changed)
    def thirdColor(self):
        return self._s.thirdColor

    @pyqtProperty(QColor, notify=changed)
    def topbarColor(self):
        return self._s.topbarColor

    @pyqtProperty(QColor, notify=changed)
    def topColor(self):
        return self._s.topColor

    @pyqtProperty(QColor, notify=changed)
    def mainColor(self):
        return self._s.mainColor

    @pyqtProperty(QColor, notify=changed)
    def widgetContainerColor(self):
        return self._s.widgetContainerColor

    @pyqtProperty(QColor, notify=changed)
    def extraColor(self):
        return self._s.extraColor

    @pyqtProperty(QColor, notify=changed)
    def baseColorA(self):
        # Legacy page backgrounds are a baseColorA→baseColorB gradient; the modern
        # palette is flat, so both ends collapse onto the ground colour.
        return self._c("ground")

    @pyqtProperty(QColor, notify=changed)
    def baseColorB(self):
        return self._c("ground")

    @pyqtProperty(QColor, notify=changed)
    def secondaryColorA(self):
        return self._s.secondaryColorA

    @pyqtProperty(QColor, notify=changed)
    def secondaryColorB(self):
        return self._s.secondaryColorB

    @pyqtProperty(QColor, notify=changed)
    def thirdColorA(self):
        return self._s.thirdColorA

    @pyqtProperty(QColor, notify=changed)
    def thirdColorB(self):
        return self._s.thirdColorB


    @pyqtProperty(float, notify=changed)
    def glassOpacity(self):
        return self._s.glassOpacity

    @pyqtProperty(bool, notify=changed)
    def glassEnabled(self):
        return self._s.glassEnabled

    @pyqtProperty(str, notify=changed)
    def progressColor(self):
        return self._s.progressColor


    # ── Semantic text colors ─────────────────────────────────────────────
    # Remapped onto the active palette so shared components read correctly.
    @pyqtProperty(QColor, notify=changed)
    def textPrimary(self):
        return self._c("ink")

    @pyqtProperty(QColor, notify=changed)
    def textSecondary(self):
        return self._c("inkDim")

    @pyqtProperty(QColor, notify=changed)
    def textTertiary(self):
        return self._c("inkFaint")

    @pyqtProperty(QColor, notify=changed)
    def textMuted(self):
        return self._c("inkGhost")


    # ── Status colors ─────────────────────────────────────────────────────
    @pyqtProperty(QColor, notify=changed)
    def statusGreen(self):
        return self._c("ok")

    @pyqtProperty(QColor, notify=changed)
    def statusYellow(self):
        return self._c("warn")

    @pyqtProperty(QColor, constant=True)
    def statusOrange(self):
        return QColor("#f97316")

    @pyqtProperty(QColor, notify=changed)
    def statusRed(self):
        return self._c("danger")

    @pyqtProperty(QColor, constant=True)
    def statusNA(self):
        return QColor("#3a3f4a")


    @pyqtProperty(str, constant=True)
    def surfaceDialog(self):
        return "rgba(20,20,26,0.96)"


    @pyqtProperty(QColor, notify=changed)
    def surfaceDark(self):
        return self._c("ground")


    # ── Font helpers ──────────────────────────────────────────────────────
    @pyqtProperty(str, notify=changed)
    def fontFamily(self):
        fam = self._pal.get("fontFamily")
        return fam if fam else self._s.fontFamily

    def _fonts(self) -> dict:
        """Active style's type scale, with per-role overrides from settings.ini."""
        if self._font_cache is None:
            scale = dict(self._style.get("fonts") or {})
            section = self._style.get("font_section")
            if section:
                for role in list(scale.keys()):
                    raw = self._s.getValue(section, role + "_size")
                    if raw:
                        try:
                            scale[role] = int(float(raw))
                        except ValueError:
                            pass
            self._font_cache = scale
        return self._font_cache

    @pyqtSlot(str, result=int)
    def fontSize(self, role):
        scale = self._fonts()
        if role in scale:
            return scale[role]
        info = self._s.fontGroup(role)
        return info.get("size", 14) if info else 14

    @pyqtSlot(str, result=int)
    def fontWeight(self, role):
        info = self._s.fontGroup(role)
        return info.get("weight", 400) if info else 400
