from __future__ import annotations

import os
import configparser
import threading
import time
from collections import deque
from threading import RLock
from typing import List, Optional, Tuple

from preferences import DATA_SETTINGS

try:
    import can  # type: ignore
except Exception:
    can = None


class _RxStats:
    """Cheap reception counters for the passive listener thread.

    Pi 4B budget: the listener only touches these fields once per received
    frame (a set.add, two int increments, one timestamp, one deque append),
    all under a short lock. No frame data is copied or decoded.
    """

    __slots__ = ("total", "last_frame_ts", "unique_ids", "_recent_ts", "_lock")

    def __init__(self):
        self.total: int = 0
        self.last_frame_ts: float = 0.0
        self.unique_ids: set = set()
        # Timestamps of recent frames for a rolling rate window (≤5 s).
        # Bounded by trimming on read; worst case ~5×typical_fps entries.
        self._recent_ts: deque = deque()
        self._lock = threading.Lock()

    def record(self, arb_id: int, ts: float) -> None:
        with self._lock:
            self.total += 1
            self.last_frame_ts = ts
            self.unique_ids.add(int(arb_id))
            self._recent_ts.append(ts)

    def reset(self) -> None:
        with self._lock:
            self.total = 0
            self.last_frame_ts = 0.0
            self.unique_ids.clear()
            self._recent_ts.clear()

    def snapshot(self) -> dict:
        """Read-side: trims the rolling window, returns a flat dict."""
        now = time.monotonic()
        window = 5.0
        with self._lock:
            # Drop timestamps older than the window; cheap because deque is
            # already time-ordered.
            cutoff = now - window
            while self._recent_ts and self._recent_ts[0] < cutoff:
                self._recent_ts.popleft()
            recent_count = len(self._recent_ts)
            total = self.total
            last_ts = self.last_frame_ts
            unique = len(self.unique_ids)
        return {
            "total": total,
            "fps": (recent_count / window) if recent_count else 0.0,
            "unique_ids": unique,
            "last_frame_ts": last_ts,
            "age_ms": int((now - last_ts) * 1000) if last_ts > 0 else -1,
        }


class _CANSession:
    """Small connection wrapper exposing an OBD-like API."""

    def __init__(self, bus, *, interface: str, channel: str, bitrate: int):
        self._bus = bus
        self._interface = str(interface or "")
        self._channel = str(channel or "")
        self._bitrate = int(bitrate)
        self._closed = False

    def is_connected(self) -> bool:
        return (not self._closed) and (self._bus is not None)

    def close(self):
        if self._closed:
            return
        try:
            if self._bus is not None and hasattr(self._bus, "shutdown"):
                self._bus.shutdown()
            elif self._bus is not None and hasattr(self._bus, "close"):
                self._bus.close()
        except Exception:
            pass
        self._closed = True
        self._bus = None


class CANConnectionManager:
    """
    Passive CAN manager with the same minimal surface as OBDManager.

    Settings keys in [OBD]:
      - protocol_mode = canbus|obd2
      - auto_connect = true|false
      - can_interface = slcan|socketcan|... (default: slcan)
      - can_channel = /dev/ttyUSB0|can0|... (fallback: port)
      - can_bitrate = 500000 (fallback: baud)
    """

    _instance: "CANConnectionManager" | None = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._conn = None
            cls._instance._channel = None
            cls._instance._bitrate = None
            cls._instance._interface = None
            cls._instance._last_error = ""
            cls._instance._lock = RLock()
            # Proof-of-life: passive RX stats + the daemon listener that
            # feeds them. Started on each successful open, stopped on close.
            cls._instance._stats = _RxStats()
            cls._instance._listener: Optional[threading.Thread] = None
            cls._instance._listener_stop = threading.Event()
        return cls._instance

    def last_error(self) -> str:
        return self._last_error

    # ── RX stats (proof-of-life signals) ────────────────────────────────
    def frames_total(self) -> int:
        return self._stats.snapshot()["total"]


    def unique_id_count(self) -> int:
        return int(self._stats.snapshot()["unique_ids"])


    def stats_snapshot(self) -> dict:
        return self._stats.snapshot()

    def _start_listener(self, session: "_CANSession") -> None:
        """Spawn the daemon RX thread. Cheap: a single blocking recv loop."""
        self._listener_stop = threading.Event()

        def _run(bus_ref, stop_evt):
            # Pi 4B: bus.recv blocks in the kernel for `timeout` seconds —
            # no Python-level spin. We catch CanError / OSError to update
            # last_error so a bitrate mismatch shows up in the UI.
            while not stop_evt.is_set():
                try:
                    msg = bus_ref.recv(timeout=0.5)
                except Exception as exc:
                    # Don't spin on persistent errors: brief sleep, record,
                    # try again. If the bus is truly gone, _close() will
                    # set stop_evt.
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    time.sleep(0.25)
                    continue
                if msg is None:
                    continue
                try:
                    arb_id = int(getattr(msg, "arbitration_id", 0))
                except Exception:
                    arb_id = 0
                # `msg.timestamp` is epoch-seconds from python-can; we want
                # monotonic for the rolling window. The difference between
                # now and monotonic-now is constant, so use monotonic here.
                self._stats.record(arb_id, time.monotonic())

        t = threading.Thread(
            target=_run, args=(session._bus, self._listener_stop),
            name="CANRxListener", daemon=True,
        )
        self._listener = t
        t.start()

    def _stop_listener(self) -> None:
        try:
            self._listener_stop.set()
        except Exception:
            pass
        # Don't join — the recv timeout (0.5 s) bounds the thread's exit;
        # we don't want to block the GUI/close path on it.
        self._listener = None

    def quick_connect(self, *args, timeout: float = 1.0, **kwargs) -> bool:
        _ = args, kwargs
        with self._lock:
            self._close()

            interface, channel, bitrate, auto = self._read_can_settings()
            if not auto:
                self._last_error = "auto_connect is disabled"
                return False
            if can is None:
                self._last_error = "python-can library not installed"
                return False
            if not channel:
                self._last_error = "CAN channel not configured (set can_channel in Settings)"
                return False

            bus = self._open_bus(interface=interface, channel=channel, bitrate=bitrate, timeout=timeout)
            if bus is None:
                self._last_error = f"Failed to open {interface}:{channel} @ {bitrate} bps"
                return False
            self._conn = _CANSession(bus, interface=interface, channel=channel, bitrate=bitrate)
            self._channel = channel
            self._bitrate = bitrate
            self._interface = interface
            self._last_error = ""
            self._stats.reset()
            self._start_listener(self._conn)
            return True

    def scan_connect(
        self,
        ports: Optional[List[str]] = None,
        bauds: Optional[List[int]] = None,
        timeout: float = 1.0,
    ) -> Tuple[bool, List[str]]:
        trace: List[str] = []
        if can is None:
            trace.append("python-can unavailable.")
            return False, trace

        pref_iface, pref_channel, pref_bitrate, _auto = self._read_can_settings()
        channels: List[str] = []
        if isinstance(ports, list) and ports:
            channels.extend([str(p) for p in ports if str(p).strip()])
        if pref_channel:
            channels.append(pref_channel)
        channels.extend(["/dev/ttyUSB0", "/dev/ttyUSB1", "can0", "vcan0"])
        # Keep insertion order, remove duplicates
        channels = list(dict.fromkeys([c for c in channels if c]))

        bitrates: List[int] = []
        if isinstance(bauds, list) and bauds:
            for b in bauds:
                try:
                    bitrates.append(int(b))
                except Exception:
                    pass
        try:
            bitrates.append(int(pref_bitrate))
        except Exception:
            pass
        bitrates.extend([500000, 250000, 125000])
        bitrates = list(dict.fromkeys([b for b in bitrates if b > 0]))

        interfaces = [pref_iface, "slcan", "socketcan"]
        interfaces = list(dict.fromkeys([str(i) for i in interfaces if str(i).strip()]))

        with self._lock:
            self._close()
            for interface in interfaces:
                for channel in channels:
                    for bitrate in bitrates:
                        trace.append(f"→ Trying {interface}:{channel} @ {bitrate}…")
                        bus = self._open_bus(
                            interface=interface,
                            channel=channel,
                            bitrate=bitrate,
                            timeout=timeout,
                        )
                        if bus is None:
                            trace.append("   … no link")
                            continue
                        self._conn = _CANSession(bus, interface=interface, channel=channel, bitrate=bitrate)
                        self._channel = channel
                        self._bitrate = bitrate
                        self._interface = interface
                        self._stats.reset()
                        self._start_listener(self._conn)
                        trace.append(f"✅ Connected on {interface}:{channel} @ {bitrate}.")
                        return True, trace
            trace.append("🚫 No stable CAN connection found.")
            return False, trace

    def disconnect(self):
        with self._lock:
            self._close()

    def is_connected(self) -> bool:
        with self._lock:
            return bool(self._conn and self._conn.is_connected())

    def get_connection(self):
        with self._lock:
            return self._conn

    def get_port(self) -> Optional[str]:
        return self._channel

    def get_baudrate(self) -> Optional[int]:
        return self._bitrate

    def _close(self):
        # Stop the listener first so it doesn't try to recv() on a closed
        # bus and spam last_error.
        self._stop_listener()
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None
        self._channel = None
        self._bitrate = None
        self._interface = None

    def _settings_path(self) -> str:
        return DATA_SETTINGS

    def _read_can_settings(self) -> Tuple[str, str, int, bool]:
        cfg = configparser.ConfigParser()
        cfg_path = self._settings_path()

        interface = "slcan"
        channel = ""
        bitrate = 500000
        auto_connect = True

        try:
            if os.path.exists(cfg_path):
                cfg.read(cfg_path, encoding="utf-8")
        except Exception:
            try:
                cfg.read(cfg_path)
            except Exception:
                pass

        if cfg.has_section("OBD"):
            try:
                auto_connect = cfg.getboolean("OBD", "auto_connect", fallback=True)
            except Exception:
                auto_connect = True
            interface = cfg.get("OBD", "can_interface", fallback=interface).strip() or interface
            channel = cfg.get("OBD", "can_channel", fallback="").strip()
            if not channel:
                channel = cfg.get("OBD", "port", fallback="").strip()
            try:
                bitrate = cfg.getint("OBD", "can_bitrate", fallback=bitrate)
            except Exception:
                bitrate = 500000
            if bitrate <= 0:
                try:
                    bitrate = max(1, cfg.getint("OBD", "baud", fallback=500000))
                except Exception:
                    bitrate = 500000

        if not channel:
            channel = "/dev/ttyUSB0"

        return interface, channel, int(bitrate), bool(auto_connect)

    def _open_bus(self, *, interface: str, channel: str, bitrate: int, timeout: float):
        if can is None:
            return None

        kwargs = {
            "interface": str(interface),
            "channel": str(channel),
        }
        # socketcan often ignores bitrate if interface already configured,
        # while slcan usually needs it for adapter init.
        if str(interface).lower() != "socketcan":
            kwargs["bitrate"] = int(bitrate)

        bus = None
        try:
            bus = can.Bus(**kwargs)
            try:
                bus.recv(timeout=float(max(0.0, timeout)))
            except Exception:
                pass
            return bus
        except Exception:
            try:
                if bus is not None:
                    if hasattr(bus, "shutdown"):
                        bus.shutdown()
                    elif hasattr(bus, "close"):
                        bus.close()
            except Exception:
                pass
            return None
