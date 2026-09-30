"""`TripLog` — Settings → Debugger → Trip logging, for QML.

Thin bridge over `Modules/Runtime/trip_logger.py` (the recorder) and
`trip_analyzer.py` (the report). Owns no data itself: channel selection lives
in `[TripLog]`, the recorder holds the live counters, the analyzer reads a
saved file. A 1 s timer refreshes the "recording · size · records" line only
while recording; `analyze()` runs on a one-shot thread so a large file never
blocks the Qt thread.
"""
from __future__ import annotations

import os
import threading
import time

from PyQt6.QtCore import QObject, QTimer, Qt, pyqtProperty, pyqtSignal, pyqtSlot

from Modules.Runtime import settings_cache
from Modules.Runtime.trip_logger import CHANNELS, CHANNEL_LABELS, TripLogger
from Modules.Runtime import trip_analyzer
from preferences import DATA_SETTINGS, TRIP_LOG_DIR


class TripLogBridge(QObject):
    changed = pyqtSignal()
    sessionsChanged = pyqtSignal()
    analysisReady = pyqtSignal(str, "QVariant")   # name, summary dict
    _analysisDone = pyqtSignal(str, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._logger = TripLogger.instance()
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._analysisDone.connect(self._emit_analysis, Qt.ConnectionType.QueuedConnection)

    # ── channels / config ────────────────────────────────────────────────
    @pyqtProperty(bool, notify=changed)
    def recording(self) -> bool:
        return self._logger.recording

    @pyqtProperty(bool, notify=changed)
    def capped(self) -> bool:
        return self._logger.capped

    @pyqtProperty(list, notify=changed)
    def channels(self) -> list:
        active = TripLogger.config_channels()
        return [{"key": k, "label": CHANNEL_LABELS.get(k, k), "active": k in active}
                for k in CHANNELS]

    @pyqtProperty(int, notify=changed)
    def activeCount(self) -> int:
        return len(TripLogger.config_channels())

    @pyqtProperty(int, notify=changed)
    def sampleHz(self) -> int:
        return int(round(TripLogger.config_sample_hz()))

    @pyqtSlot(str, bool)
    def setChannel(self, key: str, on: bool) -> None:
        if key not in CHANNELS:
            return
        active = TripLogger.config_channels()
        if on:
            active.add(key)
        else:
            active.discard(key)
        TripLogger.set_config_channels(active)
        self.changed.emit()

    @pyqtSlot(int)
    def setSampleHz(self, hz: int) -> None:
        settings_cache.set_value("TripLog", "sample_hz", str(max(1, min(10, int(hz)))), path=DATA_SETTINGS)
        self.changed.emit()

    # ── recording ─────────────────────────────────────────────────────────
    @pyqtProperty(str, notify=changed)
    def sessionText(self) -> str:
        if not self._logger.recording:
            return ""
        mb = self._logger.bytes_written / (1024 * 1024)
        size = f"{mb:.1f} MB" if mb >= 0.1 else f"{self._logger.bytes_written / 1024:.0f} KB"
        return f"Recording · {size} · {self._logger.record_count} rec"

    @pyqtSlot()
    def start(self) -> None:
        if self._logger.recording:
            return
        active = TripLogger.config_channels()
        if not active:
            return
        protocol = settings_cache.get("OBD", "protocol_mode", fallback="", path=DATA_SETTINGS)
        style = settings_cache.get("UI", "style", fallback="Modern", path=DATA_SETTINGS)
        if self._logger.start(active, header={"protocol": protocol, "style": style}):
            self._timer.start()
        self.changed.emit()

    @pyqtSlot()
    def stop(self) -> None:
        if not self._logger.recording:
            return
        self._logger.stop()
        self._timer.stop()
        self.changed.emit()
        self.sessionsChanged.emit()   # the new file joins the list

    def _tick(self) -> None:
        if self._logger.recording:
            self.changed.emit()
            if self._logger.capped:      # cap tripped the recorder itself
                self._timer.stop()
                self.sessionsChanged.emit()
        else:
            self._timer.stop()

    # ── saved sessions ────────────────────────────────────────────────────
    @pyqtProperty(list, notify=sessionsChanged)
    def sessions(self) -> list:
        out = []
        try:
            names = [n for n in os.listdir(TRIP_LOG_DIR) if n.endswith(".jsonl")]
        except OSError:
            names = []
        for n in sorted(names, reverse=True):
            path = os.path.join(TRIP_LOG_DIR, n)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            # trip_YYYYmmdd-HHMMSS.jsonl → "2026-09-18 00:18"
            date = n
            stem = n[len("trip_"):-len(".jsonl")] if n.startswith("trip_") else n
            if len(stem) >= 15 and stem[8] == "-":
                d, tm = stem[:8], stem[9:15]
                date = f"{d[:4]}-{d[4:6]}-{d[6:8]} {tm[:2]}:{tm[2:4]}"
            out.append({"name": n, "date": date, "size": size,
                        "sizeText": trip_analyzer.human_size(size)})
        return out

    @pyqtSlot()
    def refreshSessions(self) -> None:
        self.sessionsChanged.emit()

    @pyqtSlot(str, result=str)
    def pathHint(self, name: str) -> str:
        return os.path.join(TRIP_LOG_DIR, os.path.basename(name))

    @pyqtSlot(str)
    def deleteSession(self, name: str) -> None:
        path = os.path.join(TRIP_LOG_DIR, os.path.basename(name))
        try:
            if os.path.commonpath([os.path.abspath(path), os.path.abspath(TRIP_LOG_DIR)]) == os.path.abspath(TRIP_LOG_DIR):
                os.remove(path)
        except OSError:
            pass
        self.sessionsChanged.emit()

    # ── analyze (off the Qt thread) ───────────────────────────────────────
    @pyqtSlot(str)
    def analyze(self, name: str) -> None:
        path = os.path.join(TRIP_LOG_DIR, os.path.basename(name))

        def _work():
            try:
                rep = trip_analyzer.summarize(path)
            except Exception as exc:
                rep = {"name": name, "error": str(exc), "records": 0, "sections": []}
            self._analysisDone.emit(name, rep)

        threading.Thread(target=_work, daemon=True, name="TripAnalyze").start()

    def _emit_analysis(self, name, rep) -> None:
        self.analysisReady.emit(name, rep)

    # ── lifecycle ──────────────────────────────────────────────────────────
    def shutdown(self) -> None:
        try:
            self._logger.stop()
        except Exception:
            pass
        self._timer.stop()
