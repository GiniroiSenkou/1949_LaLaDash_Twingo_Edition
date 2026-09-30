from __future__ import annotations

import threading
import time
import weakref
from typing import Callable, Optional

from PyQt6.QtCore import QObject, pyqtSignal


class TelemetryCoordinator(QObject):
    changed = pyqtSignal(dict)

    _instance: Optional["TelemetryCoordinator"] = None

    def __init__(self) -> None:
        super().__init__()
        self._snapshot: dict = {}
        self._callbacks: list[Callable[[dict], None]] = []
        self._lock = threading.RLock()
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._poll_interval_active = 0.05   # 50 ms — sim mode (no serial I/O)
        self._poll_interval_obd    = 0.12   # ~8 Hz when a real OBD adapter drives
        self._poll_interval_idle   = 0.50   # 500 ms when neither
        self._connected = False
        self._sim_enabled = False
        # OBD reconnect while idle. The boot attempt (main._boot_obd) fails
        # whenever the Pi is up before the ignition — the adapter answers but
        # the ECU does not — and nothing retried, so the car stayed
        # "Disconnected" until someone pressed Deep Scan. The ~4 s handshake
        # runs here, on this thread, which has nothing else to do while
        # disconnected; the first retry waits one interval so it never races
        # the boot attempt.
        self._reconnect_interval_s = 15.0
        self._last_reconnect_try = 0.0

    @classmethod
    def instance(cls) -> "TelemetryCoordinator":
        if cls._instance is None:
            cls._instance = TelemetryCoordinator()
        return cls._instance

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._last_reconnect_try = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True, name="TelemetryCoordinator")
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.5)
        self._thread = None

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        # Use WeakMethod for bound methods, weakref.ref for plain functions
        try:
            ref = weakref.WeakMethod(callback)
        except TypeError:
            ref = weakref.ref(callback)
        # Avoid duplicates. Use == not is: a re-accessed bound method (and the
        # one WeakMethod reconstructs) is a fresh object each time, so `is`
        # never matches — but `==` compares (__self__, __func__) and does.
        for existing in self._callbacks:
            if existing() == callback:
                return
        self._callbacks.append(ref)

    def unsubscribe(self, callback: Callable[[dict], None]) -> None:
        self._callbacks = [
            r for r in self._callbacks
            if r() is not None and r() != callback
        ]

    def snapshot(self) -> dict:
        # Returns a defensive copy. Callers may mutate freely.
        with self._lock:
            snap = self._snapshot
        if not snap:
            hub_cls = self._hub_class()
            if hub_cls is not None:
                try:
                    snap = hub_cls.instance().snapshot() or {}
                except Exception:
                    snap = {}
        return dict(snap)

    def connected(self) -> bool:
        with self._lock:
            return bool(self._connected)

    def sim_enabled(self) -> bool:
        with self._lock:
            return bool(self._sim_enabled)

    def _run(self) -> None:
        t0 = time.monotonic()
        self._poll_once()
        elapsed = time.monotonic() - t0
        while True:
            # Pick the wake-up cadence by state:
            #   • real OBD connected → ~8 Hz. A serial ELM327 cannot deliver
            #     PIDs at 20 Hz anyway (RPM is gated in telemetry.py, and the
            #     RPM needle interpolates internally), and a slower tick lets
            #     this thread actually sleep instead of spinning in blocking
            #     serial reads and starving the Qt UI of the GIL.
            #   • sim mode (no serial I/O) → 20 Hz, kept smooth for the needle.
            #   • neither → idle backoff, avoids wake-ups while OBD is unplugged.
            if self._connected:
                interval = self._poll_interval_obd
            elif self._sim_enabled:
                interval = self._poll_interval_active
            else:
                interval = self._poll_interval_idle
            # The interval is the tick *period*, not the gap after the work:
            # sleeping 120 ms on top of 30-60 ms of serial reads made the real
            # period ~170 ms, and against a 200 ms RPM interval that aliased
            # RPM down to ~3 Hz. Subtracting the work keeps the period honest.
            if self._stop_evt.wait(max(0.01, interval - elapsed)):
                return
            t0 = time.monotonic()
            if not self._connected and not self._sim_enabled:
                self._maybe_reconnect(t0)
            self._poll_once()
            elapsed = time.monotonic() - t0

    def _maybe_reconnect(self, now: float) -> None:
        if now - self._last_reconnect_try < self._reconnect_interval_s:
            return
        self._last_reconnect_try = now
        try:
            from Modules.Obd.obd_connection import OBDManager
            mgr = OBDManager.instance()
            if mgr.reconnect_if_idle():
                print(f"[OBD] auto-connected on {mgr.get_port()} @ {mgr.get_baudrate()}", flush=True)
        except Exception:
            pass

    def _poll_once(self) -> None:
        snap: dict = {}
        hub_cls = self._hub_class()
        if hub_cls is not None:
            try:
                hub = hub_cls.instance()
                hub.update()
                snap = hub.snapshot()
                connected = bool(getattr(hub, "obd_connected", False))
                sim_enabled = bool(getattr(hub, "sim_enabled", False))
            except Exception:
                snap = {}
                connected = False
                sim_enabled = False
        else:
            connected = False
            sim_enabled = False
        out = snap if isinstance(snap, dict) else {}
        # Snapshot is treated as read-only by all subscribers (TelemetryBridge,
        # NavigationBridge, ModdingBridge) — share a single reference instead of
        # copying it 4× per tick. snapshot() makes a defensive copy on read.
        with self._lock:
            self._snapshot = out
            self._connected = connected
            self._sim_enabled = sim_enabled
        self.changed.emit(out)
        live = []
        for ref in self._callbacks:
            cb = ref()
            if cb is not None:
                try:
                    cb(out)
                except Exception:
                    pass
                live.append(ref)
        self._callbacks = live

    def _hub_class(self):
        try:
            from Modules.Obd.telemetry import TelemetryHub
        except Exception:
            return None
        return TelemetryHub
