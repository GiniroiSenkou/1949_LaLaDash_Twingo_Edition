"""CAN debugger bridge — `Can` in QML (Styles/Modern/sheets/CanDebuggerSheet.qml).

Everything the sheet shows comes from the STN session's FrameStore and the
signal map (Modules/Obd/can_signals.py). Lists are built here, already
windowed and sorted, and refreshed on a 2 Hz timer that only runs while the
sheet is open — nothing on this bridge costs anything when it is closed,
and opening it is also what switches the monitor from "mapped IDs only" to
"everything" (OBDManager.set_monitor_everything).

Record: baseline window (hands off) then action window (do the thing),
captured from the monitor thread into a list; analysed with
can_signals.analyze_toggle() and merged across repeats of the same action
(merge_runs) so a bit that answers every time floats to the top.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtProperty, pyqtSignal, pyqtSlot

from preferences import BASE_DIR
from Modules.Obd.can_signals import CATALOG_BY_KEY, analyze_toggle, merge_runs, signal_map

# The old QWidget debugger's action list, trimmed to
# what a given car actually has and what the catalog can be bound to.
ACTION_GROUPS: List[tuple] = [
    ("Lights", ["Low beam on", "High beam on", "Position lights on", "Front fog on",
                "Rear fog on", "Left indicator", "Right indicator", "Hazards"]),
    ("Pedals", ["Brake pressed", "Clutch pressed", "Accelerator pressed"]),
    ("Doors", ["Driver door open", "Passenger door open", "Boot open"]),
    ("Safety", ["Seatbelt unbuckled", "Handbrake up"]),
    ("Engine", ["Ignition on", "Engine start", "Engine stop", "Reverse gear"]),
    ("Other", ["Horn", "Wipers on", "Eco mode"]),
]
# Action → catalog signal it most likely is, for the one-tap assign.
ACTION_SIGNAL = {
    "Low beam on": "low_beam", "High beam on": "high_beam", "Position lights on": "position_lights",
    "Front fog on": "fog_front", "Rear fog on": "fog_rear", "Left indicator": "turn_left",
    "Right indicator": "turn_right", "Hazards": "hazard", "Brake pressed": "brake",
    "Clutch pressed": "clutch", "Driver door open": "doors_open", "Passenger door open": "doors_open",
    "Boot open": "doors_open", "Seatbelt unbuckled": "seatbelt", "Handbrake up": "handbrake",
    "Ignition on": "ignition", "Engine start": "engine_on", "Reverse gear": "reverse",
    "Eco mode": "eco",
}


class CanBridge(QObject):
    liveChanged = pyqtSignal()
    statusChanged = pyqtSignal()
    mapChanged = pyqtSignal()
    recordChanged = pyqtSignal()
    detailChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._obd_bridge = None
        self._open = False
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._refresh)
        self._live: List[dict] = []
        self._selected: int = -1
        self._detail: dict = {}
        self._status_text = "Not connected"
        self._fps = 0.0
        self._unique = 0
        self._duty = 0
        # Record state
        self._rec_action = ""
        self._rec_phase = "idle"
        self._rec_frames: List[tuple] = []
        self._rec_lock = threading.Lock()
        self._rec_t0 = 0.0
        self._rec_baseline_s = 5.0
        self._rec_action_s = 8.0
        self._rec_runs: Dict[str, List[List[dict]]] = {}
        self._rec_results: List[dict] = []
        self._rec_timer = QTimer(self)
        self._rec_timer.setSingleShot(True)
        self._rec_timer.timeout.connect(self._record_step)
        # Raw capture
        self._raw_frames: Optional[List[tuple]] = None
        self._raw_path = ""
        self._raw_timer = QTimer(self)
        self._raw_timer.setSingleShot(True)
        self._raw_timer.timeout.connect(self._finish_raw_capture)

    def set_obd_bridge(self, bridge) -> None:
        self._obd_bridge = bridge

    # ── helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _manager():
        from Modules.Obd.obd_connection import OBDManager
        return OBDManager.instance()

    def _session(self):
        return self._manager().get_session()

    def _feed(self):
        return self._manager().get_feed()

    # ── sheet lifecycle ──────────────────────────────────────────────────
    @pyqtSlot()
    def open(self):
        self._open = True
        try:
            self._manager().set_monitor_everything(True)
        except Exception:
            pass
        sess = self._session()
        if sess is not None:
            sess.capture_sink = self._on_frame
        self._refresh()
        self._timer.start()

    @pyqtSlot()
    def close(self):
        self._open = False
        self._timer.stop()
        self._rec_timer.stop()
        if self._rec_phase != "idle":
            self._rec_phase = "idle"
            self.recordChanged.emit()
        sess = self._session()
        if sess is not None:
            sess.capture_sink = None
        try:
            self._manager().set_monitor_everything(False)
        except Exception:
            pass


    # ── connection / status ──────────────────────────────────────────────
    @pyqtProperty(bool, notify=statusChanged)
    def connected(self):
        sess = self._session()
        return sess is not None and sess.is_stn

    @pyqtProperty(bool, notify=statusChanged)
    def monitoring(self):
        sess = self._session()
        return sess is not None and sess.monitor_active

    @pyqtProperty(str, notify=statusChanged)
    def statusText(self):
        return self._status_text

    @pyqtProperty(str, notify=statusChanged)
    def presetText(self):
        """The connection recipe that worked in the car, for the Live tab card."""
        from Modules.Runtime import settings_cache
        from preferences import DATA_SETTINGS
        port = settings_cache.get("OBD", "port", fallback="", path=DATA_SETTINGS) or "auto (by-id)"
        uart = settings_cache.getint("OBD", "can_uart_baud", fallback=2000000, path=DATA_SETTINGS)
        return f"STN1170 / vLinker FS · {port} · 115200 → STBR {uart} · ATSP6 ATH1 ATCAF0 ATS0 · STMA"

    @pyqtProperty(bool, notify=statusChanged)
    def isReplay(self):
        """True on the dev PC's fake adapter — the frames are a recording, not the car."""
        from Modules.Runtime import settings_cache
        from preferences import DATA_SETTINGS
        port = settings_cache.get("OBD", "port", fallback="", path=DATA_SETTINGS) or ""
        return port.startswith("fake://")

    @pyqtProperty(str, notify=statusChanged)
    def replayText(self):
        if not self.isReplay:
            return ""
        from Modules.Runtime import settings_cache
        from preferences import DATA_SETTINGS
        port = settings_cache.get("OBD", "port", fallback="", path=DATA_SETTINGS) or ""
        name = port[len("fake://"):].split("?")[0].rsplit("/", 1)[-1]
        return (f"REPLAY — this is the recording {name} looping, not the car. "
                "In it: low beam on at 16.6 s, high beam 32.5 s, both off 44.0 s, hazards 44.9–61.5 s.")

    @pyqtProperty(str, notify=recordChanged)
    def recordHint(self):
        """What to do right now, spelled out for the Record tab."""
        a = self._rec_action or "the action"
        if self._rec_phase == "baseline":
            return f"Baseline: touch nothing for {self.recordSecondsLeft} s — the bus at rest is what the action gets compared against."
        if self._rec_phase == "action":
            return f"NOW: {a} — do it and hold it until the countdown ends ({self.recordSecondsLeft} s)."
        if self._rec_phase == "done":
            n = len(self._rec_runs.get(self._rec_action, []))
            return (f"Run {n} done. Bits that were steady in the baseline and moved during the action, "
                    f"best first. Record again ({a} once more) so bits that answer every time rise to the top, "
                    "then Assign the top hit and watch it in Assign → Live as you toggle the real thing.")
        return ("Pick an action on the left, tap Record, keep your hands off during the baseline, "
                "then do the action when it says NOW.")

    @pyqtProperty(float, notify=statusChanged)
    def fps(self):
        return self._fps

    @pyqtProperty(int, notify=statusChanged)
    def uniqueIds(self):
        return self._unique

    @pyqtProperty(int, notify=statusChanged)
    def dutyPercent(self):
        return self._duty

    @pyqtSlot()
    def connectNow(self):
        """Connect through the OBD bridge (its worker thread); the STN session
        attaches automatically when [OBD] protocol_mode is can."""
        if self._obd_bridge is not None:
            try:
                self._obd_bridge.connect()
            except Exception:
                pass

    @pyqtSlot()
    def disconnectNow(self):
        if self._obd_bridge is not None:
            try:
                self._obd_bridge.disconnect()
            except Exception:
                pass

    # ── live table ───────────────────────────────────────────────────────
    @pyqtProperty(list, notify=liveChanged)
    def liveIds(self):
        return list(self._live)

    @pyqtProperty(str, notify=detailChanged)
    def selectedId(self):
        return f"{self._selected:03X}" if self._selected >= 0 else ""

    @pyqtProperty("QVariantMap", notify=detailChanged)
    def detail(self):
        return dict(self._detail)

    @pyqtSlot(str)
    def selectId(self, id_hex):
        try:
            self._selected = int(str(id_hex), 16) if id_hex else -1
        except ValueError:
            self._selected = -1
        self._refresh_detail()
        self.detailChanged.emit()

    def _refresh_detail(self):
        sess = self._session()
        if sess is None or self._selected < 0:
            self._detail = {}
            return
        d = sess.store.detail(self._selected) or {}
        if d:
            smap = signal_map()
            d["status"] = smap.id_status(self._selected)
            d["label"] = smap.id_label(self._selected)
            key = f"0x{self._selected:03X}"
            d["signals"] = [
                {"key": k, "label": CATALOG_BY_KEY[k][1], "status": s.get("status", ""),
                 "byte": int(s.get("byte", s.get("start_byte", 0)) or 0), "bit": int(s.get("bit", 0) or 0),
                 "type": s.get("type", "bool")}
                for k, s in smap.signals.items() if s.get("id") == key and k in CATALOG_BY_KEY
            ]
        self._detail = d

    def _refresh(self):
        if self._rec_phase in ("baseline", "action"):
            self.recordChanged.emit()   # countdown text
        sess = self._session()
        if sess is None:
            new_status = "Not connected — Connect starts the OBD link; the CAN monitor needs [OBD] protocol = CAN-BUS and an STN adapter"
            self._live = []
            self._fps = 0.0
            self._unique = 0
            self._duty = 0
        else:
            st = sess.stats_snapshot()
            self._fps = float(st["fps"])
            self._unique = int(st["unique_ids"])
            self._duty = int(round(100 * sess.duty_cycle()))
            try:
                from Modules.Runtime.trip_logger import log as _tl
                _tl("can", "stats", fps=round(self._fps, 1), ids=self._unique, duty=self._duty)
            except Exception:
                pass
            mode = "all IDs" if self._manager()._monitor_everything else "mapped IDs"
            if not sess.is_stn:
                new_status = f"Adapter is not an STN ({sess.identity or 'no STI reply'}); raw monitoring unavailable"
            elif not sess.monitor_active:
                new_status = f"{sess.identity} · UART {sess.uart_baud} · monitor idle"
            else:
                new_status = (f"{sess.identity} · UART {sess.uart_baud} · {mode} · "
                              f"{self._fps:.0f} fps · {self._unique} IDs · streaming {self._duty}%"
                              + (f" · {sess.last_error}" if sess.last_error else ""))
            smap = signal_map()
            rows = sess.store.snapshot()
            for r in rows:
                r["status"] = smap.id_status(r["id"])
                r["label"] = smap.id_label(r["id"])
            self._live = rows
        if new_status != self._status_text:
            self._status_text = new_status
        self.statusChanged.emit()
        self.liveChanged.emit()
        if self._selected >= 0:
            self._refresh_detail()
            self.detailChanged.emit()
        self.mapChanged.emit()   # catalog rows carry live values

    # ── catalog / assign ─────────────────────────────────────────────────
    @pyqtProperty(list, notify=mapChanged)
    def catalog(self):
        feed = self._feed()
        live = feed.live if feed is not None else {}
        return signal_map().catalog_rows(live)

    @pyqtSlot(str, str, int, int, result=bool)
    def assignBool(self, key, id_hex, byte, bit):
        ok = signal_map().assign(str(key), {"id": str(id_hex), "type": "bool", "byte": int(byte), "bit": int(bit)})
        self._after_map_change()
        return ok


    @pyqtSlot(str, result=bool)
    def confirm(self, key):
        ok = signal_map().set_status(str(key), "confirmed")
        self._after_map_change()
        return ok

    @pyqtSlot(str, result=bool)
    def unconfirm(self, key):
        ok = signal_map().set_status(str(key), "candidate")
        self._after_map_change()
        return ok

    @pyqtSlot(str, result=bool)
    def clear(self, key):
        ok = signal_map().clear(str(key))
        self._after_map_change()
        return ok


    def _after_map_change(self):
        try:
            self._manager().apply_monitor_scope_async()
        except Exception:
            pass
        self._refresh_detail()
        self.detailChanged.emit()
        self.mapChanged.emit()

    @pyqtProperty(list, constant=True)
    def actionGroups(self):
        return [{"group": g, "actions": list(a)} for g, a in ACTION_GROUPS]


    # ── record ───────────────────────────────────────────────────────────
    def _on_frame(self, ts: float, ident: int, data: bytes) -> None:
        """Monitor-thread sink while recording or raw-capturing."""
        if self._rec_phase in ("baseline", "action"):
            with self._rec_lock:
                self._rec_frames.append((ts, ident, data))
        raw = self._raw_frames
        if raw is not None:
            raw.append((ts, ident, data))

    @pyqtProperty(str, notify=recordChanged)
    def recordPhase(self):
        return self._rec_phase

    @pyqtProperty(str, notify=recordChanged)
    def recordAction(self):
        return self._rec_action

    @pyqtProperty(int, notify=recordChanged)
    def recordRuns(self):
        return len(self._rec_runs.get(self._rec_action, []))

    @pyqtProperty(list, notify=recordChanged)
    def recordResults(self):
        return list(self._rec_results)

    @pyqtProperty(int, notify=recordChanged)
    def recordSecondsLeft(self):
        if self._rec_phase == "baseline":
            return max(0, int(self._rec_baseline_s - (time.monotonic() - self._rec_t0)))
        if self._rec_phase == "action":
            return max(0, int(self._rec_baseline_s + self._rec_action_s - (time.monotonic() - self._rec_t0)))
        return 0

    @pyqtSlot(str, float, float)
    def startRecord(self, action, baseline_s, action_s):
        sess = self._session()
        if sess is None or not sess.monitor_active:
            return
        if action != self._rec_action:
            self._rec_results = []
        self._rec_action = str(action)
        self._rec_baseline_s = max(2.0, float(baseline_s))
        self._rec_action_s = max(2.0, float(action_s))
        with self._rec_lock:
            self._rec_frames = []
        self._rec_t0 = time.monotonic()
        self._rec_phase = "baseline"
        self.recordChanged.emit()
        self._rec_timer.start(int(self._rec_baseline_s * 1000))

    def _record_step(self):
        if self._rec_phase == "baseline":
            self._rec_phase = "action"
            self.recordChanged.emit()
            self._rec_timer.start(int(self._rec_action_s * 1000))
            return
        if self._rec_phase == "action":
            with self._rec_lock:
                frames = list(self._rec_frames)
                self._rec_frames = []
            t0 = self._rec_t0
            rel = [(t - t0, i, d) for t, i, d in frames]
            res = analyze_toggle(rel, self._rec_baseline_s, self._rec_baseline_s,
                                 self._rec_baseline_s + self._rec_action_s)
            runs = self._rec_runs.setdefault(self._rec_action, [])
            runs.append(res)
            merged = merge_runs(runs)
            sig = ACTION_SIGNAL.get(self._rec_action, "")
            for r in merged:
                r["signal"] = sig
            self._rec_results = merged
            self._rec_phase = "done"
            self.recordChanged.emit()

    @pyqtSlot()
    def cancelRecord(self):
        self._rec_timer.stop()
        self._rec_phase = "idle"
        with self._rec_lock:
            self._rec_frames = []
        self.recordChanged.emit()

    @pyqtSlot()
    def resetRecord(self):
        self._rec_timer.stop()
        self._rec_phase = "idle"
        self._rec_runs.pop(self._rec_action, None)
        self._rec_results = []
        self.recordChanged.emit()

    # ── raw capture to file ──────────────────────────────────────────────
    @pyqtProperty(str, notify=recordChanged)
    def rawCapturePath(self):
        return self._raw_path

    @pyqtProperty(bool, notify=recordChanged)
    def rawCapturing(self):
        return self._raw_frames is not None

    @pyqtSlot(float)
    def startRawCapture(self, seconds):
        sess = self._session()
        if sess is None or not sess.monitor_active or self._raw_frames is not None:
            return
        os.makedirs(os.path.join(BASE_DIR, "Data", "can_captures"), exist_ok=True)
        self._raw_path = os.path.join(BASE_DIR, "Data", "can_captures",
                                      time.strftime("live_%Y-%m-%d_%H%M%S.txt"))
        self._raw_frames = []
        self._raw_t0 = time.monotonic()
        self.recordChanged.emit()
        self._raw_timer.start(int(max(1.0, float(seconds)) * 1000))

    def _finish_raw_capture(self):
        frames = self._raw_frames
        self._raw_frames = None
        if frames is None:
            return
        sess = self._session()
        try:
            with open(self._raw_path, "w") as f:
                f.write(f"# STI: {sess.identity if sess else ''}\n")
                f.write(f"# UART: {sess.uart_baud if sess else ''}\n")
                f.write(f"# start {time.strftime('%H:%M:%S')}\n")
                for t, i, d in frames:
                    f.write(f"{t - self._raw_t0:.3f} {i:03X} {d.hex().upper()}\n")
                f.write(f"# end frames={len(frames)}\n")
            print(f"[can] raw capture: {len(frames)} frames → {self._raw_path}", flush=True)
            try:
                from Modules.Runtime.trip_logger import log as _tl
                _tl("can", "capture", frames=len(frames))
            except Exception:
                pass
        except Exception as exc:
            print(f"[can] raw capture failed: {exc}", flush=True)
        self.recordChanged.emit()
