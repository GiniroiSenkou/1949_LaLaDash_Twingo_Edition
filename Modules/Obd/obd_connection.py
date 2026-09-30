# GTR/obd_connection.py
from __future__ import annotations
import glob
import os
from threading import RLock, Lock
from typing import List, Optional, Tuple

import logging
import time
import obd
logging.getLogger("obd").setLevel(logging.CRITICAL)
import serial.tools.list_ports
from preferences import DATA_SETTINGS
from Modules.Runtime import settings_cache
from Modules.Obd.stn_session import StnSession, register_fake_adapter

register_fake_adapter()   # lets [OBD] port = fake://<capture> run the CAN chain on the dev PC
# Kept for Diagnostic deep scan (DiagnosticGUI -> manager.scan_connect())
SCAN_BAUDS = [115200, 38400, 9600]
DEFAULT_BAUD = 115200

# Global lock so concurrent GUI pages don't hammer the adapter
_query_lock = Lock()
# One monitor-scope change at a time; the debugger's open/close can overlap.
_scope_lock = Lock()


class _NoRepeatOBD(obd.OBD):
    """python-OBD with the fast-mode "repeat last command" shortcut removed.

    In fast mode python-OBD sends a bare CR when a query repeats the previous
    command, relying on the ELM327 to re-run it. The car's vLinker FS (reports
    "ELM327 v2.3") answers that CR with `?`, so every consecutive same-PID
    query came back null. Measured on the reference car (Twingo III), 2026-09-15, ignition on:

        fast=True,  RPM x12 back-to-back ............  0/12 non-null
        fast=True,  RPM/SPEED alternating ............ 11/12
        fast=False, RPM x12 .......................... 12/12 at 14 Hz
        fast=True,  full resend every time ("010C1") . 20/20 at 50 Hz

    RPM is polled more often than anything else, so it was the one hit
    hardest: the read only succeeded when some other PID happened to go out
    in between, and each null flipped engine_running off for a tick. This
    keeps the frame-count hint (the actual speed win) and just never sends
    the bare CR. Overrides a name-mangled private, hence the odd spelling.
    """

    def _OBD__build_command_string(self, cmd):
        cmd_string = cmd.command
        frame_counts = getattr(self, "_OBD__frame_counts", {})
        if self.fast and cmd.fast and (cmd in frame_counts):
            cmd_string += str(frame_counts[cmd]).encode()
        return cmd_string


def default_serial_port() -> str:
    """Best guess for the ELM327's device node when settings carry none.

    /dev/serial/by-id/ names are stable across re-enumeration (the vLinker
    is `usb-www.vgatemall.com_vLinker_FS_<serial>-if00-port0`), ttyUSBn is
    not — so prefer by-id when the kernel offers it."""
    for pattern in ("/dev/serial/by-id/*", "/dev/ttyUSB*", "/dev/ttyACM*"):
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return "/dev/ttyUSB0"


def safe_query(command, *, force: bool = True):
    """
    Thread-safe wrapper around python-OBD query using the shared connection.
    Returns the python-OBD Response or None on failure/not connected.
    Never raises. Minimal impact: one query per call.
    """
    try:
        with _query_lock:
            mgr = OBDManager()
            conn = mgr.get_connection()
            if not (conn and conn.is_connected()):
                return None
            session = mgr.get_session()
            if session is not None and session.monitor_active:
                # Pause the raw CAN monitor for this query (resumes on its own
                # after ~150 ms of PID silence — one pause per burst).
                with session.pid_window():
                    return conn.query(command, force=force)
            return conn.query(command, force=force)
    except Exception:
        return None


def _triplog(channel, event, **fields):
    try:
        from Modules.Runtime.trip_logger import log as _tl
        _tl(channel, event, **fields)
    except Exception:
        pass


class OBDManager:
    """
    Singleton connection manager.

    Policy:
      • quick_connect(): one attempt on settings.ini → [OBD] port/baud, with
        working defaults when the section is missing (auto_connect on, the
        first /dev/serial/by-id node, 115200). The old "no [OBD] section →
        do nothing" rule meant one wiped INI on the Pi (2026-09) silently
        killed boot auto-connect *and* the Home-page Retry button, and only
        Deep Scan — which never saved what it found — could get the car back.
        - If auto_connect is false, or no device node exists, or the attempt fails → returns False.
        - Accepts legacy args/kwargs (preferred_port, baud, …) to stay source-compatible, but ignores them.
      • scan_connect(): verbose sweep over ports×bauds for Diagnose page only.
      • Both persist the (port, baud) that worked, so the next boot is a quick_connect.
    """
    _instance: "OBDManager" | None = None

    @classmethod
    def instance(cls) -> "OBDManager":
        return cls()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._conn = None
            cls._instance._baud = None
            cls._instance._port = None
            cls._instance._lock = RLock()
            # Raw CAN monitoring on the same adapter (STN1170 only); see
            # stn_session.py. Built after each successful connect when
            # [OBD] protocol_mode = can.
            cls._instance._session = None
            cls._instance._feed = None
        return cls._instance

    # ---------- CAN monitor (STN session) ----------
    def get_session(self) -> Optional[StnSession]:
        return self._session

    def get_feed(self):
        return self._feed

    def can_monitor_enabled(self) -> bool:
        mode = (settings_cache.get("OBD", "protocol_mode", fallback="obd2", path=self._settings_path()) or "obd2").strip().lower()
        backend = (settings_cache.get("OBD", "can_backend", fallback="stn", path=self._settings_path()) or "stn").strip().lower()
        return mode == "can" and backend == "stn"

    def _attach_session(self, conn) -> None:
        """After a PID connection: identify the chip, lift the UART rate and,
        if the signal map has bindings, start monitoring them. Runs on the
        connecting thread (never the Qt thread)."""
        try:
            port = conn.interface._ELM327__port
        except Exception:
            return
        try:
            uart = settings_cache.getint("OBD", "can_uart_baud", fallback=2000000, path=self._settings_path())
            session = StnSession(port, log=lambda m: print(m, flush=True), uart_baud=uart)
            # setup() talks to the adapter; a PID query in flight would read its replies.
            with _query_lock:
                if not session.setup():
                    return
            from Modules.Obd.can_feed import CanFeed
            feed = CanFeed()
            session.on_frames = feed.on_frames
            self._session = session
            self._feed = feed
            self.apply_monitor_scope()
        except Exception as exc:
            print(f"[can] session setup failed: {exc}", flush=True)
            self._session = None
            self._feed = None

    def drop_session(self) -> None:
        """Leave CAN-BUS mode without touching the PID link."""
        session = self._session
        self._session = None
        self._feed = None
        if session is not None:
            try:
                with _query_lock:
                    session.close()
            except Exception:
                pass
            try:
                from Modules.Layout.dash_lights_hub import DashLightsHub
                DashLightsHub.instance().set_can_active([])
            except Exception:
                pass

    _monitor_everything = False   # the debugger sheet sets this while open

    def set_monitor_everything(self, on: bool) -> None:
        self._monitor_everything = bool(on)
        self.apply_monitor_scope_async()

    def apply_monitor_scope_async(self) -> None:
        """For Qt-thread callers: the monitor join plus an STBR handshake
        take seconds."""
        import threading
        threading.Thread(target=self.apply_monitor_scope, name="StnScope", daemon=True).start()

    def apply_monitor_scope(self) -> None:
        """Monitor all IDs while the debugger is open, only the mapped IDs
        otherwise (STFAP filters keep the Pi's serial parsing cheap), and
        nothing at all when the map is empty and nobody is looking.

        The UART rate follows the scope, and that is what lets raw CAN run
        alongside the PID link: 2 Mbaud exists only while STMA is genuinely
        needed (the debugger sheet), and normal driving stays on the 115200
        python-obd opened with. Previously the session lifted the shared port
        to 2 Mbaud for the whole run, so any reconnect re-opened at 115200
        against a 2 Mbaud adapter and heard nothing — `no adapter answering
        on … at 115200 or any STN rate`.

        Order matters: the monitor is stopped before the rate moves, because
        `stop_monitor()` joins the reader thread and the switch needs the
        port to itself.
        """
        with _scope_lock:
            self._apply_monitor_scope()

    def _apply_monitor_scope(self) -> None:
        session = self._session
        if session is None or not session.is_stn:
            return
        try:
            ids = None
            if not self._monitor_everything:
                from Modules.Obd.can_signals import signal_map
                ids = signal_map().bound_ids(statuses=("confirmed", "candidate"))

            # No PID query may touch the port from here until the monitor is
            # back: safe_query only pauses for a *running* monitor, so without
            # this the poller wrote into the STBR handshake and could leave the
            # adapter at 2 Mbaud with the port at 115200 — connected, no frames.
            with _query_lock:
                session.stop_monitor()
                port_dead = not session.alive
                if not port_dead:
                    want_fast = bool(self._monitor_everything)
                    if not session.ensure_uart(fast=want_fast) and want_fast:
                        # STMA at 115200 fills the adapter's buffer in ~1.4 s, so a
                        # failed lift means the debugger will show torn frames. Say
                        # so plainly rather than letting it look like a bus problem.
                        session.last_error = (
                            f"UART stuck at {session.uart_baud}; monitoring every ID "
                            "needs 2 Mbaud — expect BUFFER FULL / dropped frames"
                        )
                        print(f"[can] {session.last_error}", flush=True)

                    if self._monitor_everything:
                        session.start_monitor(filters=None)
                    elif ids:
                        session.start_monitor(filters=ids)
            if port_dead:
                # Port closed under us — drop the session so the next connect
                # builds a fresh one instead of writing to a dead handle.
                self.drop_session()
                print("[can] session dropped: adapter port closed", flush=True)
        except Exception as exc:
            print(f"[can] monitor scope failed: {exc}", flush=True)

    def stats_snapshot(self) -> dict:
        """Same surface CANConnectionManager offers OBDBridge."""
        if self._session is not None:
            return self._session.stats_snapshot()
        return {"total": 0, "fps": 0.0, "unique_ids": 0, "last_frame_ts": 0.0, "age_ms": -1}

    def frames_total(self) -> int:
        return int(self.stats_snapshot()["total"])

    def unique_id_count(self) -> int:
        return int(self.stats_snapshot()["unique_ids"])

    def last_error(self) -> str:
        if self._session is not None and self._session.last_error:
            return self._session.last_error
        return self._connect_error

    _connect_error: str = ""

    # ---------- Public: quiet status for normal pages ----------
    def quick_connect(self, *args, timeout: float = 1.0, **kwargs) -> bool:
        """
        Attempt a single connection strictly from settings.ini.
        Ignores any provided (port, baud, preferred_port, ...) arguments by design.

        Returns True on success, False otherwise.
        """
        # NOTE: args/kwargs intentionally ignored to preserve legacy call sites without changing behavior.
        with self._lock:
            self._close()

            cfg_port, cfg_baud, cfg_auto = self._read_obd_settings()
            if not cfg_auto:
                # Auto connect disabled
                return False
            if not cfg_port or not (cfg_port.startswith("fake://") or os.path.exists(cfg_port)):
                # No adapter plugged in — skip the 1 s ATZ wait entirely.
                self._connect_error = f"No adapter at {cfg_port or '(no port set)'}"
                return False

            # Ask the adapter itself first, with short timeouts. python-obd
            # opens the port with a 10 s read timeout, so an adapter that
            # is not answering at cfg_baud costs 11 s per attempt inside it
            # — and an STN left at the session's 2 Mbaud by a kill or a
            # brownout answers nothing at 115200 and stays that way (OBD pin
            # 16 keeps it powered with the car off). This settles both in
            # under 3 s: reset it at whichever rate it is on, or give up.
            if not self._adapter_ready(cfg_port, cfg_baud):
                self._connect_error = "Adapter not answering"
                return False
            self._connect_error = ""

            try:
                # fast=True lets the ELM327 return as soon as the expected
                # response frames arrive instead of waiting out the full
                # timeout on every PID — the dominant per-query speed win.
                c = _NoRepeatOBD(portstr=cfg_port, baudrate=cfg_baud, timeout=timeout, fast=True)
                if c.is_connected():
                    self._conn, self._port, self._baud = c, cfg_port, cfg_baud
                    self._persist(cfg_port, cfg_baud)
                    _triplog("obd", "connect", port=cfg_port, baud=cfg_baud,
                             protocol=str(getattr(c, "protocol_name", lambda: "")() or ""))
                    if self.can_monitor_enabled():
                        self._attach_session(c)
                    return True
                # The adapter answered (we got this far) but the car did not:
                # ignition off, or the bus asleep.
                self._connect_error = "Adapter OK, car not answering — ignition on?"
                _triplog("obd", "no_car", port=cfg_port)
                c.close()
                return False
            except Exception as exc:
                self._connect_error = f"Connect failed: {exc}"
                _triplog("obd", "error", msg=str(exc))
                return False

    # UART rates StnSession may have switched the adapter to (its STBR
    # ladder), fastest first — the order it tries them in.
    _STN_RATES = (2000000, 1000000, 921600, 500000, 230400)

    @staticmethod
    def _probe_port(port: str, rate: int, timeout: float):
        """Open the adapter for a short probe, exclusively wherever the OS
        allows it.

        This enforces in-process what the Pi deployment notes demand of other processes:
        never a second handle on ttyUSB0. Two handles share one termios, so a
        probe opening at 115200 re-clocks the UART underneath a live 2 Mbaud
        session and both sides read garbage — which looks exactly like a dead
        adapter. Falls back to a plain open where exclusivity is unsupported
        (pyserial URL handlers, the fake adapter).
        """
        kw = dict(baudrate=rate, timeout=timeout, write_timeout=0.5)
        try:
            return serial.serial_for_url(port, exclusive=True, **kw)
        except TypeError:
            return serial.serial_for_url(port, **kw)

    @staticmethod
    def _answers(port: str, rate: int) -> bool:
        """Does an ELM/STN answer ATI at this rate? Any byte first, to stop
        a monitor a killed session may have left streaming."""
        with OBDManager._probe_port(port, rate, 0.1) as s:
            s.write(b"\r")
            time.sleep(0.25)
            s.reset_input_buffer()
            s.write(b"ATI\r")
            reply = b""
            deadline = time.monotonic() + 0.6
            while time.monotonic() < deadline and b">" not in reply:
                reply += s.read(64)
            return b"ELM" in reply or b"STN" in reply or b"OK" in reply

    def _adapter_ready(self, port: str, cfg_baud: int) -> bool:
        """True when the adapter answers at cfg_baud — resetting it from any
        of its fast rates first if that is where it was left."""
        try:
            if self._answers(port, cfg_baud):
                return True
        except Exception:
            return False
        for rate in self._STN_RATES:
            if rate == cfg_baud:
                continue
            try:
                if not self._answers(port, rate):
                    continue
                with OBDManager._probe_port(port, rate, 0.3) as s:
                    s.write(b"ATZ\r")     # resets the STN's UART to its stored default
                    time.sleep(1.2)
            except Exception:
                continue
            try:
                ok = self._answers(port, cfg_baud)
            except Exception:
                ok = False
            print(f"[obd] adapter was answering at {rate} baud (left there by an earlier "
                  f"session) — ATZ, back to {cfg_baud}: {'ok' if ok else 'still silent'}", flush=True)
            return ok
        print(f"[obd] no adapter answering on {port} at {cfg_baud} or any STN rate", flush=True)
        return False

    def port_present(self) -> bool:
        """Cheap check for the reconnect loop: is there anything to talk to?"""
        port, _baud, auto = self._read_obd_settings()
        return bool(auto and port and (port.startswith("fake://") or os.path.exists(port)))

    def reconnect_if_idle(self) -> bool:
        """One quick_connect() for the coordinator's retry loop — but only if
        no other connect (boot thread, Retry button, Deep Scan) is in flight,
        so it can never tear down a link somebody else just brought up.
        Returns True only when *this* call established the connection."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self.is_connected() or not self.port_present():
                return False
            return self.quick_connect()
        finally:
            self._lock.release()

    # Both accessors give up instead of queueing behind a connect in
    # progress: quick_connect() holds the lock for seconds, and the telemetry
    # thread calling these every tick would otherwise stall with it — which
    # is what froze the whole hub ("tick 10000 ms") during a reconnect.
    def is_connected(self) -> bool:
        if not self._lock.acquire(timeout=0.05):
            return False
        try:
            return bool(self._conn and self._conn.is_connected())
        finally:
            self._lock.release()

    def get_connection(self):
        if not self._lock.acquire(timeout=0.05):
            return None
        try:
            return self._conn
        finally:
            self._lock.release()

    def get_port(self) -> Optional[str]:
        return self._port

    def get_baudrate(self) -> Optional[int]:
        return self._baud

    def disconnect(self) -> None:
        with self._lock:
            self._close()

    def test_connection(self) -> bool:
        with self._lock:
            return bool(self._conn and self._conn.is_connected())

    # ---------- Diagnose-only: verbose scan ----------
    def scan_connect(
        self,
        ports: Optional[List[str]] = None,
        bauds: Optional[List[int]] = None,
        timeout: float = 1.0,
    ) -> Tuple[bool, List[str]]:
        """
        Try all (port, baud) combos and return (connected?, trace_lines_for_gui).
        Use this from the Diagnose page to see attempts in a log box.
        """
        trace: List[str] = []
        ports = ports or self._serial_ports()
        bauds = bauds or SCAN_BAUDS

        if not ports:
            trace.append("🔌 No serial ports found.")
            return False, trace

        with self._lock:
            self._close()
            for port in ports:
                for b in bauds:
                    trace.append(f"→ Trying {port} @ {b}…")
                    try:
                        c = _NoRepeatOBD(portstr=port, baudrate=b, timeout=timeout, fast=True)
                        if c.is_connected():
                            self._conn, self._port, self._baud = c, port, b
                            self._persist(port, b)
                            if self.can_monitor_enabled():
                                self._attach_session(c)
                            trace.append(f"✅ Connected on {port} @ {b} (saved to settings).")
                            return True, trace
                        c.close()
                        trace.append("   … no link")
                    except Exception as e:
                        trace.append(f"   ⚠️ {e}")
            trace.append("🚫 No stable connection found.")
            return False, trace

    # ---------- Helpers ----------
    def _serial_ports(self) -> List[str]:
        return [p.device for p in serial.tools.list_ports.comports()]

    def _close(self):
        session = self._session
        self._session = None
        self._feed = None
        if session is not None:
            try:
                session.close()   # stops the monitor, ATZ back to 115200
            except Exception:
                pass
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            pass
        port, baud = self._port, self._baud
        self._conn = None
        self._port = None
        self._baud = None
        # A session had the adapter at 2 Mbaud. Make sure it really came back
        # — the next connect (ours or a fresh boot's) expects 115200, and the
        # adapter does not reset on its own.
        if session is not None and port:
            try:
                ok = self._adapter_ready(port, baud or 115200)
            except Exception:
                ok = False
            print(f"[can] session closed; adapter back at {baud or 115200}: "
                  f"{'ok' if ok else 'NOT answering'}", flush=True)
            _triplog("obd", "close", restored=bool(ok))

    def _settings_path(self) -> str:
        """
        Locate settings.ini. Prefer project root (parent of this module),
        fall back to this directory.
        """
        return DATA_SETTINGS

    def _read_obd_settings(self) -> Tuple[str, int, bool]:
        """
        Returns (port, baud, auto_connect_flag) from settings.ini, through
        settings_cache like everything else. Missing values get the defaults
        that actually match the car (auto on, 115200, whatever serial node is
        plugged in). A configured port that no longer exists falls back to
        the plugged-in node too — ttyUSB numbering is not stable.
        """
        path = self._settings_path()
        auto = settings_cache.getboolean("OBD", "auto_connect", fallback=True, path=path)
        port = (settings_cache.get("OBD", "port", fallback="", path=path) or "").strip()
        baud = settings_cache.getint("OBD", "baud", fallback=DEFAULT_BAUD, path=path) or DEFAULT_BAUD
        if not port or not (port.startswith("fake://") or os.path.exists(port)):
            port = default_serial_port()
        return port, int(baud), bool(auto)

    def _persist(self, port: str, baud: int) -> None:
        """Remember what worked so the next boot is a plain quick_connect().
        One write, and only when something actually changed."""
        path = self._settings_path()
        try:
            same = (
                settings_cache.get("OBD", "port", fallback="", path=path) == port
                and settings_cache.getint("OBD", "baud", fallback=0, path=path) == int(baud)
                and settings_cache.getboolean("OBD", "auto_connect", fallback=False, path=path)
            )
            if same:
                return
            cfg = settings_cache.load_copy(path)
            if not cfg.has_section("OBD"):
                cfg.add_section("OBD")
            cfg.set("OBD", "auto_connect", "true")
            cfg.set("OBD", "port", port)
            cfg.set("OBD", "baud", str(int(baud)))
            settings_cache.save(cfg, path)
        except Exception:
            pass


# Minimal CLI smoke test (quiet)
if __name__ == "__main__":
    mgr = OBDManager()
    ok = mgr.quick_connect()
    print("✅ OBD connected" if ok else "🚫 OBD not connected")
