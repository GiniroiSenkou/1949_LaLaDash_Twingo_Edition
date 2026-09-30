from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot

from preferences import DATA_SETTINGS, BASE_DIR
from Modules.Runtime import settings_cache

SOUNDS_DIR = Path(BASE_DIR) / "Sounds"
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}
FAVORITE_SLOTS = 5


class SoundboardBridge(QObject):
    """Exposes soundboard folders, files, favorites, and playback to QML."""

    catalogChanged = pyqtSignal()
    playbackChanged = pyqtSignal()
    favoritesChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._folders: list[str] = []
        self._files: list[str] = []
        self._file_paths: list[str] = []
        self._current_folder = ""
        self._playing = False
        self._current_file = ""
        self._volume = 0.75
        self._favorites: list[dict] = []  # [{name, path}, ...]
        self._player = None  # pygame mixer or similar

        self._load_volume()
        self._load_favorites()
        self._scan_catalog()

    # ── catalog ───────────────────────────────────────────────────────────
    def _scan_catalog(self):
        self._folders = []
        if SOUNDS_DIR.is_dir():
            for entry in sorted(SOUNDS_DIR.iterdir()):
                if entry.is_dir():
                    self._folders.append(entry.name)
        self.catalogChanged.emit()


    @pyqtProperty(str, constant=True)
    def fallbackLogo(self):
        return str(Path(BASE_DIR) / "GUI" / "Icons" / "soundboard.png")

    @pyqtProperty(list, notify=catalogChanged)
    def folders(self):
        return self._folders

    @pyqtProperty(list, notify=catalogChanged)
    def files(self):
        return self._files

    @pyqtProperty(str, notify=catalogChanged)
    def currentFolder(self):
        return self._current_folder

    @pyqtSlot(str)
    def selectFolder(self, folder_name):
        folder_path = SOUNDS_DIR / folder_name
        if not folder_path.is_dir():
            return
        self._current_folder = folder_name
        self._files = []
        self._file_paths = []
        for f in sorted(folder_path.iterdir()):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
                self._files.append(f.stem.replace("_", " ").replace("-", " ").strip() or f.stem)
                self._file_paths.append(str(f))
        settings_cache.set_value("Soundboard", "last_played_folder", folder_name, path=DATA_SETTINGS)
        self.catalogChanged.emit()

    @pyqtSlot()
    def refreshCatalog(self):
        self._scan_catalog()

    # ── playback ──────────────────────────────────────────────────────────
    @pyqtProperty(bool, notify=playbackChanged)
    def playing(self):
        return self._playing


    @pyqtProperty(float, notify=playbackChanged)
    def volume(self):
        return self._volume

    @pyqtSlot(int)
    def playFile(self, index):
        if index < 0 or index >= len(self._file_paths):
            return
        path = self._file_paths[index]
        self._play_audio(path)
        self._current_file = self._files[index] if index < len(self._files) else ""
        self._playing = True
        self.playbackChanged.emit()

    @pyqtSlot(str)
    def playPath(self, path):
        self._play_audio(path)
        self._current_file = Path(path).stem if path else ""
        self._playing = True
        self.playbackChanged.emit()

    @pyqtSlot()
    def playRandom(self):
        """Play a random sound from the last-used folder (or first valid folder)."""
        import random
        last_folder = settings_cache.get("Soundboard", "last_played_folder", fallback="", path=DATA_SETTINGS)
        folder = last_folder if last_folder and (SOUNDS_DIR / last_folder).is_dir() else ""
        if not folder:
            for f in self._folders:
                fp = SOUNDS_DIR / f
                if fp.is_dir() and any(c.suffix.lower() in AUDIO_EXTS for c in fp.iterdir() if c.is_file()):
                    folder = f
                    break
        if not folder:
            return
        fp = SOUNDS_DIR / folder
        audio_files = [c for c in fp.iterdir() if c.is_file() and c.suffix.lower() in AUDIO_EXTS]
        if audio_files:
            pick = random.choice(audio_files)
            self._play_audio(str(pick))
            self._current_file = pick.stem
            self._playing = True
            self.playbackChanged.emit()

    @pyqtSlot()
    def stop(self):
        self._stop_audio()
        self._playing = False
        self._current_file = ""
        self.playbackChanged.emit()

    @pyqtSlot(float)
    def setVolume(self, v):
        self._volume = max(0.0, min(1.0, v))
        settings_cache.set_value("Soundboard", "volume", str(self._volume), path=DATA_SETTINGS)
        try:
            import pygame
            if pygame.mixer.get_init():
                for ch_i in range(pygame.mixer.get_num_channels()):
                    ch = pygame.mixer.Channel(ch_i)
                    ch.set_volume(self._volume)
        except Exception:
            pass
        self.playbackChanged.emit()

    # ── favorites ─────────────────────────────────────────────────────────
    @pyqtProperty(list, notify=favoritesChanged)
    def favorites(self):
        return self._favorites

    @pyqtSlot(int, str, str)
    def setFavorite(self, slot, path, name):
        if slot < 0 or slot >= FAVORITE_SLOTS:
            return
        while len(self._favorites) <= slot:
            self._favorites.append({"name": "", "path": ""})
        self._favorites[slot] = {"name": name, "path": path}
        self._save_favorites()
        self.favoritesChanged.emit()

    @pyqtSlot(int)
    def clearFavorite(self, slot):
        self.setFavorite(slot, "", "")

    @pyqtSlot(int)
    def playFavorite(self, slot):
        if slot < 0 or slot >= len(self._favorites):
            return
        fav = self._favorites[slot]
        path = fav.get("path", "")
        if path and os.path.isfile(path):
            self.playPath(path)

    @pyqtSlot(int, result=str)
    def pathForIndex(self, index):
        if 0 <= index < len(self._file_paths):
            return self._file_paths[index]
        return ""

    @pyqtSlot(str, result=str)
    def logoForFolder(self, folder_name):
        folder = SOUNDS_DIR / str(folder_name or "")
        logo = folder / "logo.png"
        if logo.is_file():
            return str(logo)
        return self.fallbackLogo

    # ── internal audio ────────────────────────────────────────────────────
    def _play_audio(self, path):
        try:
            import pygame
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            pygame.mixer.music.load(path)
            pygame.mixer.music.set_volume(self._volume)
            pygame.mixer.music.play()
        except Exception:
            pass

    def _stop_audio(self):
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
        except Exception:
            pass

    # ── persistence ───────────────────────────────────────────────────────
    def _load_volume(self):
        try:
            self._volume = float(
                settings_cache.get("Soundboard", "volume", fallback="0.75", path=DATA_SETTINGS)
            )
        except (TypeError, ValueError):
            self._volume = 0.75

    def _load_favorites(self):
        self._favorites = []
        for i in range(FAVORITE_SLOTS):
            name = settings_cache.get("Soundboard", f"favorite_{i}_name", fallback="", path=DATA_SETTINGS) or ""
            path = settings_cache.get("Soundboard", f"favorite_{i}_path", fallback="", path=DATA_SETTINGS) or ""
            self._favorites.append({"name": name, "path": path})

    def _save_favorites(self):
        for i, fav in enumerate(self._favorites):
            settings_cache.set_value("Soundboard", f"favorite_{i}_name", fav.get("name", ""), path=DATA_SETTINGS)
            settings_cache.set_value("Soundboard", f"favorite_{i}_path", fav.get("path", ""), path=DATA_SETTINGS)
