from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap


@lru_cache(maxsize=512)
def load_pixmap(path: str) -> QPixmap:
    if not path or not os.path.exists(path):
        return QPixmap()
    return QPixmap(path)


@lru_cache(maxsize=1024)
def scaled_pixmap(
    path: str,
    width: int,
    height: int,
    aspect_mode=Qt.AspectRatioMode.KeepAspectRatio,
    transform_mode=Qt.TransformationMode.SmoothTransformation,
) -> QPixmap:
    base = load_pixmap(path)
    if base.isNull():
        return QPixmap()
    return base.scaled(
        max(1, int(width)),
        max(1, int(height)),
        aspect_mode,
        transform_mode,
    )


def invalidate() -> None:
    load_pixmap.cache_clear()
    scaled_pixmap.cache_clear()
