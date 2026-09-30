#!/usr/bin/env python3
"""Fullscreen "Loading" cover shown while LaLaDash starts.

LaLaDash takes about 8 s on the Pi to reach a mapped window (music library
scan, bridge construction, OBD handshake). Without this the driver watches the
bare desktop for that whole time after power-on, which reads as a failed boot.

Deliberately standalone and import-light: PyQt6 is on screen in ~0.3 s, while
importing anything from the app itself would drag in the very startup cost this
is here to hide. It reads settings.ini with plain configparser for the same
reason -- `Modules.Runtime.settings_cache` is the right door everywhere inside
the app, but not from a process whose entire job is to beat the app on screen.

Exits by itself as soon as the real window is mapped, so nothing has to
remember to kill it; the launcher still does on the way out as a backstop.
"""

from __future__ import annotations

import configparser
import os
import sys

from PyQt6.QtCore import Qt, QTimer, QRectF
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QWidget

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.join(APP_DIR, "Data", "settings.ini")

# Matches the Modern skin's ground so the handover to the real window is not a
# colour jump. Overridden by [Style] base_color for the accent only.
GROUND = "#0e0f11"
ACCENT = "#989aee"
TITLE = "LaLaDash"

# The real window's title. The splash must not match its own window here.
APP_WM_NAME = "LaLaDash"
SPLASH_WM_NAME = "LaLaDashSplash"

# Give up and get out of the way even if the app never appears, rather than
# leaving a cover the driver cannot dismiss.
MAX_SECONDS = 90


def _accent() -> str:
    try:
        cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        cp.read(SETTINGS)
        value = (cp.get("Style", "base_color", fallback="") or "").strip()
        if value.startswith("#") and len(value) in (4, 7):
            return value
    except Exception:
        pass
    return ACCENT


class Splash(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(SPLASH_WM_NAME)
        self.setStyleSheet("background: %s;" % GROUND)
        self._angle = 0.0
        self._accent = QColor(_accent())
        self._ground = QColor(GROUND)

        self._spin = QTimer(self)
        self._spin.setInterval(33)
        self._spin.timeout.connect(self._advance)
        self._spin.start()

        # Watching for the app's window is what ends the splash. 250 ms is
        # frequent enough that the overlap is invisible and cheap enough that
        # it never competes with the start-up it is waiting on.
        self._elapsed = 0.0
        self._watch = QTimer(self)
        self._watch.setInterval(250)
        self._watch.timeout.connect(self._check_app)
        self._watch.start()

    def _advance(self):
        self._angle = (self._angle + 6.0) % 360.0
        self.update()

    def _check_app(self):
        self._elapsed += 0.25
        if self._elapsed >= MAX_SECONDS or _app_window_mapped():
            QApplication.instance().quit()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self._ground)

        cx = self.width() / 2.0
        cy = self.height() / 2.0
        radius = 34.0

        track = QColor(self._accent)
        track.setAlpha(46)
        pen = QPen(track, 5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawEllipse(QRectF(cx - radius, cy - radius - 26,
                                   radius * 2, radius * 2))

        pen = QPen(self._accent, 5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(
            QRectF(cx - radius, cy - radius - 26, radius * 2, radius * 2),
            int(-self._angle * 16), int(100 * 16),
        )

        painter.setPen(QColor("#f2f3f5"))
        font = QFont()
        font.setPointSize(21)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.drawText(
            QRectF(0, cy + 34, self.width(), 40),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            TITLE,
        )

        dots = "." * (1 + int(self._angle / 120.0) % 3)
        painter.setPen(QColor("#8b9099"))
        font.setPointSize(12)
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        painter.drawText(
            QRectF(0, cy + 74, self.width(), 30),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            "Loading" + dots,
        )
        painter.end()


def _app_window_mapped() -> bool:
    """True once a viewable X window titled exactly `LaLaDash` exists.

    Matched exactly so the splash cannot mistake itself for the app; without
    python-xlib the splash simply rides out MAX_SECONDS.
    """
    try:
        from Xlib import display as xdisplay, X
    except Exception:
        return False
    try:
        disp = xdisplay.Display()
    except Exception:
        return False
    try:
        def walk(win, depth=0):
            if depth > 4:
                return False
            try:
                children = win.query_tree().children
            except Exception:
                return False
            for child in children:
                try:
                    name = child.get_wm_name()
                    if (name == APP_WM_NAME
                            and child.get_attributes().map_state == X.IsViewable):
                        return True
                except Exception:
                    pass
                if walk(child, depth + 1):
                    return True
            return False

        return walk(disp.screen().root)
    finally:
        try:
            disp.close()
        except Exception:
            pass


def main() -> int:
    app = QApplication(sys.argv)
    splash = Splash()
    splash.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
    splash.showFullScreen()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
