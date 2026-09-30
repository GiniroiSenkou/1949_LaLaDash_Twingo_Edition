"""STN1170 session: one adapter, PID polling and raw CAN monitoring interleaved.

The car's vLinker FS is an STN1170 (OBDLink-class). Measured on the reference car (Twingo III),
2026-09-15: the OBD socket carries the body bus (lights, doors, …) alongside
the powertrain, and the adapter monitors it losslessly once its UART is at
2 Mbaud (`STBR`) — 122 621 frames in 90 s. At the stock 115200 the bus
overflows it in 1.4 s.

python-obd owns the serial port (it opens it in `ELM327.__init__`). This
session *borrows* that port after the PID connection is up: it identifies
the chip (`STI`), lifts the UART rate, and then runs a monitor thread that
streams `STMA` (or `STM` behind `STFAP` pass filters) into a `FrameStore`.
`safe_query()` in obd_connection.py wraps every PID query in
`pid_window()`, which pauses the monitor (any byte stops it), lets the
query run, and resumes `STMA` once queries have been quiet for
`resume_idle_s` — so the hub's burst of ≤4 queries per tick costs one
pause, not four. python-obd strips spaces and tolerates CAF0 padding, so
`ATCAF0 ATS0` stays set for both modes; nothing is toggled per window.

Nothing here runs on the Qt thread. The decoder hook (`on_frames`) is
called on the monitor thread at most every `decode_every_s`.

`register_fake_adapter()` makes `fake://<capture.txt>` URLs open
Tools/protocol_fake.py — the dev-PC stand-in for the car.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Callable, Deque, Dict, List, Optional

import serial


def register_fake_adapter() -> None:
    """Let serial_for_url('fake://…') resolve to the fake STN1170."""
    if "Tools" not in serial.protocol_handler_packages:
        serial.protocol_handler_packages.append("Tools")


# ── frame store ───────────────────────────────────────────────────────────
class _Rec:
    __slots__ = ("last", "prev", "count", "first_ts", "last_ts", "byte_change_ts", "recent")

    def __init__(self, data: bytes, ts: float) -> None:
        self.last = data
        self.prev = data
        self.count = 1
        self.first_ts = ts
        self.last_ts = ts
        self.byte_change_ts = [0.0] * 8
        self.recent: Deque[float] = deque()


class FrameStore:
    """Last/previous payload per ID plus rate and per-byte change times.

    Pi budget: `record()` is a dict lookup, a compare and a deque append —
    it runs ~1400×/s on the monitor thread. Reads (`snapshot`, `detail`)
    are for the debugger sheet at 2 Hz and copy out under the lock.
    """

    RATE_WINDOW_S = 2.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frames: Dict[int, _Rec] = {}
        self.total = 0
        self.last_frame_ts = 0.0

    def record(self, ident: int, data: bytes, ts: float) -> None:
        with self._lock:
            self.total += 1
            self.last_frame_ts = ts
            rec = self._frames.get(ident)
            if rec is None:
                self._frames[ident] = _Rec(data, ts)
                self._frames[ident].recent.append(ts)
                return
            if data != rec.last:
                n = min(len(data), len(rec.last), 8)
                for i in range(n):
                    if data[i] != rec.last[i]:
                        rec.byte_change_ts[i] = ts
                for i in range(n, min(len(data), 8)):
                    rec.byte_change_ts[i] = ts
                rec.prev = rec.last
                rec.last = data
            rec.count += 1
            rec.last_ts = ts
            rec.recent.append(ts)
            cutoff = ts - self.RATE_WINDOW_S
            while rec.recent and rec.recent[0] < cutoff:
                rec.recent.popleft()

    def reset(self) -> None:
        with self._lock:
            self._frames.clear()
            self.total = 0
            self.last_frame_ts = 0.0

    def ids(self) -> List[int]:
        with self._lock:
            return sorted(self._frames.keys())

    def last(self, ident: int):
        with self._lock:
            rec = self._frames.get(ident)
            return (rec.last, rec.last_ts) if rec else None

    def unique_id_count(self) -> int:
        with self._lock:
            return len(self._frames)

    def rate(self, now: Optional[float] = None) -> float:
        """Frames/s over the last window, across all IDs."""
        now = now or time.monotonic()
        with self._lock:
            n = 0
            cutoff = now - self.RATE_WINDOW_S
            for rec in self._frames.values():
                n += sum(1 for t in rec.recent if t >= cutoff)
        return n / self.RATE_WINDOW_S

    def snapshot(self, now: Optional[float] = None, changed_within_s: float = 2.0) -> List[dict]:
        """One dict per ID for the debugger's Live table, sorted by ID."""
        now = now or time.monotonic()
        out: List[dict] = []
        with self._lock:
            for ident in sorted(self._frames.keys()):
                rec = self._frames[ident]
                cutoff = now - self.RATE_WINDOW_S
                hz = sum(1 for t in rec.recent if t >= cutoff) / self.RATE_WINDOW_S
                changed = [
                    (now - rec.byte_change_ts[i]) <= changed_within_s
                    for i in range(min(len(rec.last), 8))
                ]
                out.append({
                    "id": ident,
                    "idHex": f"{ident:03X}",
                    "count": rec.count,
                    "hz": round(hz, 1),
                    "data": rec.last.hex().upper(),
                    "bytes": [f"{b:02X}" for b in rec.last],
                    "changed": changed,
                    "ageMs": int((now - rec.last_ts) * 1000),
                })
        return out

    def detail(self, ident: int, now: Optional[float] = None) -> Optional[dict]:
        now = now or time.monotonic()
        with self._lock:
            rec = self._frames.get(ident)
            if rec is None:
                return None
            bits = []
            for i, b in enumerate(rec.last[:8]):
                prev_b = rec.prev[i] if i < len(rec.prev) else b
                bits.append({
                    "byte": i,
                    "hex": f"{b:02X}",
                    "bits": [(b >> k) & 1 for k in range(7, -1, -1)],
                    "flipped": [((b ^ prev_b) >> k) & 1 for k in range(7, -1, -1)],
                    "changedAgoMs": int((now - rec.byte_change_ts[i]) * 1000) if rec.byte_change_ts[i] else -1,
                })
            return {
                "id": ident, "idHex": f"{ident:03X}", "count": rec.count,
                "data": rec.last.hex().upper(), "bytes": bits,
                "firstSeenAgoS": round(now - rec.first_ts, 1),
            }


# ── session ───────────────────────────────────────────────────────────────
class StnSession:
    UART_RATES = (2000000, 1000000, 921600, 500000)

    def __init__(self, port, *, log: Optional[Callable[[str], None]] = None,
                 uart_baud: int = 2000000, resume_idle_s: float = 0.03,
                 decode_every_s: float = 0.05) -> None:
        self.port = port
        self.store = FrameStore()
        self.identity = ""
        self.is_stn = False
        self.uart_baud = int(getattr(port, "baudrate", 115200) or 115200)
        self._want_uart = int(uart_baud)
        self._log = log or (lambda m: None)
        self._resume_idle_s = float(resume_idle_s)
        self._decode_every_s = float(decode_every_s)
        self.on_frames: Optional[Callable[[FrameStore, float], None]] = None
        # Optional per-frame sink (ts, id, data) for the debugger's Record /
        # raw-capture; None when nobody is recording, so the hot path stays
        # a single attribute check.
        self.capture_sink: Optional[Callable[[float, int, bytes], None]] = None

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._monitor_wanted = False
        self._streaming = False          # monitor thread is reading frames right now
        self._pause_requested = False
        self._pid_waiters = 0
        self._resume_after = 0.0
        self._filters: Optional[List[int]] = None
        self._filters_dirty = True
        self._restart_requested = False
        self.last_error = ""
        self.pauses = 0
        self.bad_lines = 0
        # Streaming duty cycle: how much wall time the monitor actually
        # streams vs. sits paused for PID windows. Logged with the timing line.
        self._stream_since = 0.0
        self._stream_accum = 0.0
        self._duty_mark = time.monotonic()

    # ── low level ───────────────────────────────────────────────────────
    def _cmd(self, text: str, timeout: float = 1.0) -> str:
        """Send one AT/ST command and read up to the prompt. Caller holds
        the lock and the monitor is not streaming."""
        p = self.port
        try:
            p.reset_input_buffer()
        except Exception:
            pass
        p.write((text + "\r").encode())
        out = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = p.read(p.in_waiting or 1)
            if chunk:
                out += chunk
                if b">" in out:
                    break
            else:
                time.sleep(0.005)
        return out.decode(errors="ignore").replace("\r", " ").strip()

    def _read_until(self, needles, timeout: float) -> bytes:
        out = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self.port.read(self.port.in_waiting or 1)
            if chunk:
                out += chunk
                if any(n in out for n in needles):
                    break
            else:
                time.sleep(0.005)
        return out

    def _switch_uart(self, target: int) -> bool:
        """STN `STBR` handshake; reverts on any miss."""
        self._cmd("STBRT 2000", 0.5)
        try:
            self.port.reset_input_buffer()
        except Exception:
            pass
        self.port.write(f"STBR {target}\r".encode())
        if b"OK" not in self._read_until((b"OK", b"?"), 1.5):
            return False
        old = self.port.baudrate
        self.port.baudrate = target
        banner = self._read_until((b"STN", b"ELM", b"OBD"), 1.5)
        if not banner:
            self.port.baudrate = old
            time.sleep(0.3)
            return False
        self.port.write(b"\r")
        ok = self._read_until((b"OK", b">"), 1.5)
        if b"OK" in ok or b">" in ok:
            return True
        self.port.baudrate = old
        time.sleep(0.3)
        return False

    # ── port health ─────────────────────────────────────────────────────
    def _port_alive(self) -> bool:
        """The serial object belongs to python-obd, not to us. It can be
        closed underneath this session at any time — the PID link tearing
        down, or the adapter losing power on the OBD socket. Every port touch
        is gated on this so a dead handle is a clean "no" instead of
        `Could not configure port: (9, 'Bad file descriptor')`.
        """
        try:
            return bool(self.port is not None and getattr(self.port, "is_open", False))
        except Exception:
            return False

    @property
    def alive(self) -> bool:
        return self._port_alive()

    # ── lifecycle ───────────────────────────────────────────────────────
    def setup(self) -> bool:
        """Identify the chip and put it in raw-frame mode.

        Deliberately does **not** touch the UART rate — `ensure_uart()` owns
        that, driven by the monitor scope. Keeping the adapter at the rate
        python-obd opened with is what lets raw CAN coexist with the PID
        link: a reconnect can never land on a 2 Mbaud adapter and hear
        silence. Safe on a plain ELM327 (returns False, leaves it untouched).
        Caller: not on the Qt thread.
        """
        if not self._port_alive():
            self.last_error = "port closed"
            return False
        with self._lock:
            saved_timeout = self.port.timeout
            self.port.timeout = 0.05
            try:
                ident = self._cmd("STI", 1.0)
                self.identity = ident.replace(">", "").strip()
                self.is_stn = "STN" in self.identity.upper()
                if not self.is_stn:
                    self._log(f"[can] adapter is not an STN ({self.identity or 'no STI reply'}); raw monitoring unavailable")
                    return False
                for c in ("ATCAF0", "ATS0", "ATH1", "ATAL"):
                    self._cmd(c, 0.5)
                self.uart_baud = int(getattr(self.port, "baudrate", 115200) or 115200)
                self._log(f"[can] {self.identity} · UART {self.uart_baud} · CAF0/S0/H1")
                return True
            except Exception as exc:
                self.last_error = str(exc)
                self._log(f"[can] setup aborted: {exc}")
                return False
            finally:
                try:
                    self.port.timeout = saved_timeout
                except Exception:
                    pass

    def ensure_uart(self, fast: bool) -> bool:
        """Put the adapter on the rate the current monitor scope needs.

        `fast` means STMA (every ID — the debugger sheet), which genuinely
        needs 2 Mbaud: at 115200 the car's bus overflows the adapter in
        ~1.4 s. Filtered monitoring (STFAP + STM over the mapped IDs) fits
        inside 115200, so normal driving never leaves the stock rate and the
        PID link never desyncs.

        The caller must have the monitor stopped — `stop_monitor()` joins the
        reader thread, so doing it in here under the lock would deadlock.
        """
        if not self.is_stn or not self._port_alive():
            return False
        target = self._want_uart if fast else 115200
        if self.uart_baud == target:
            return True
        with self._lock:
            try:
                if target <= 115200:
                    # Prefer the STBR handshake for the way down too: ATZ
                    # resets the adapter outright and discards the protocol
                    # python-obd selected at connect, which breaks every PID
                    # query afterwards. ATZ is the fallback only.
                    if self._switch_uart(115200):
                        self.uart_baud = 115200
                        ok = True
                    else:
                        self.port.write(b"ATZ\r")
                        time.sleep(0.3)
                        self.port.baudrate = 115200
                        self.uart_baud = 115200
                        time.sleep(0.2)
                        for c in ("ATE0", "ATCAF0", "ATS0", "ATH1", "ATAL"):
                            self._cmd(c, 0.5)
                        self._log("[can] fell back to ATZ for 115200 — "
                                  "python-obd may re-search the protocol")
                        ok = True
                else:
                    ok = False
                    for rate in (target,) + tuple(r for r in self.UART_RATES if r < target):
                        if rate <= 115200:
                            break
                        if self._switch_uart(rate):
                            self.uart_baud = rate
                            ok = True
                            break
                self._log(f"[can] UART now {self.uart_baud} "
                          f"({'2 Mbaud for STMA' if fast else '115200, filtered'})")
                return ok
            except Exception as exc:
                self.last_error = str(exc)
                self._log(f"[can] UART change failed: {exc}")
                return False

    def close(self) -> None:
        self.stop_monitor()
        with self._lock:
            try:
                if self.uart_baud != 115200 and self._port_alive():
                    self.port.write(b"ATZ\r")      # resets the STN's UART to its stored 115200
                    time.sleep(0.3)
                    self.port.baudrate = 115200
                    self.uart_baud = 115200
            except Exception:
                pass

    # ── monitor control ─────────────────────────────────────────────────
    @property
    def monitor_active(self) -> bool:
        return self._monitor_wanted and self._thread is not None and self._thread.is_alive()

    @property
    def streaming(self) -> bool:
        return self._streaming


    def start_monitor(self, filters: Optional[List[int]] = None) -> None:
        if not self.is_stn:
            return
        with self._lock:
            self._filters = list(filters) if filters is not None else None
            self._filters_dirty = True
            self._monitor_wanted = True
            self._stop.clear()
            if self._pid_waiters <= 0:
                self._pause_requested = False   # stop_monitor() leaves it set
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="StnMonitor", daemon=True)
                self._thread.start()
            else:
                # Already streaming: drop out and re-enter so the new filter
                # set is applied (STFCP/STFAP happen in _enter_monitor_mode).
                self._restart_requested = True
            self._cond.notify_all()

    def stop_monitor(self) -> None:
        with self._lock:
            self._monitor_wanted = False
            self._pause_requested = True
            self._stop.set()
            self._cond.notify_all()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        with self._lock:
            self._streaming = False

    @contextmanager
    def pid_window(self):
        """Pause monitoring for a PID query; resume after a short quiet gap."""
        with self._cond:
            self._pid_waiters += 1
            self._pause_requested = True
            self._cond.notify_all()
            deadline = time.monotonic() + 2.0
            while self._streaming and time.monotonic() < deadline:
                self._cond.wait(0.02)
        try:
            yield
        finally:
            with self._cond:
                self._pid_waiters -= 1
                self._resume_after = time.monotonic() + self._resume_idle_s
                if self._pid_waiters <= 0:
                    self._pid_waiters = 0
                    self._pause_requested = False
                self._cond.notify_all()

    # ── monitor thread ──────────────────────────────────────────────────
    def _enter_monitor_mode(self) -> bool:
        """Caller holds the lock. Applies filters and starts STMA/STM."""
        p = self.port
        if self._filters_dirty:
            self._cmd("STFCP", 0.5)
            if self._filters:
                for ident in self._filters:
                    self._cmd(f"STFAP {ident:03X},7FF", 0.5)
            self._filters_dirty = False
        try:
            p.reset_input_buffer()
        except Exception:
            pass
        p.write(b"STM\r" if self._filters else b"STMA\r")
        return True

    def _leave_monitor_mode(self) -> None:
        """Caller holds the lock. Any byte stops the monitor; drain to prompt."""
        try:
            self.port.write(b"\r")
            self._read_until((b">",), 0.5)
            self.port.reset_input_buffer()
        except Exception:
            pass

    def _run(self) -> None:
        buf = b""
        last_decode = 0.0
        while not self._stop.is_set():
            with self._cond:
                while not self._stop.is_set() and (
                    not self._monitor_wanted or self._pause_requested
                    or time.monotonic() < self._resume_after
                ):
                    self._cond.wait(0.05)
                if self._stop.is_set():
                    break
                saved_timeout = self.port.timeout
                self.port.timeout = 0.02   # short reads so a pause request is seen fast
                self._enter_monitor_mode()
                self._streaming = True
                self._stream_since = time.monotonic()
                buf = b""
                self._cond.notify_all()

            # Streaming: this thread is the only one touching the port until
            # it sets _streaming False again.
            while True:
                if (self._pause_requested or self._stop.is_set() or not self._monitor_wanted
                        or self._restart_requested):
                    with self._cond:
                        self._leave_monitor_mode()
                        self.port.timeout = saved_timeout
                        self._streaming = False
                        self._stream_accum += time.monotonic() - self._stream_since
                        self._restart_requested = False
                        self.pauses += 1
                        self._cond.notify_all()
                    break
                try:
                    chunk = self.port.read(self.port.in_waiting or 1)
                except Exception as exc:
                    # The adapter went away mid-stream (unplugged, or the PID
                    # link closed the port under us). Stand down quietly and
                    # let OBDManager re-attach on the next connect, rather
                    # than raising out of the thread every 20 ms.
                    self.last_error = f"port lost: {exc}"
                    self._log(f"[can] monitor stopped — {self.last_error}")
                    with self._cond:
                        self._streaming = False
                        self._monitor_wanted = False
                        self._stop.set()
                        self._cond.notify_all()
                    return
                if not chunk:
                    continue
                buf += chunk
                now = time.monotonic()
                while b"\r" in buf:
                    line, buf = buf.split(b"\r", 1)
                    self._parse_line(line, now)
                if self.on_frames is not None and (now - last_decode) >= self._decode_every_s:
                    last_decode = now
                    try:
                        self.on_frames(self.store, now)
                    except Exception as exc:
                        self.last_error = f"decoder: {exc}"

    def _parse_line(self, raw: bytes, now: float) -> None:
        s = raw.strip().replace(b" ", b"")
        if not s or s in (b">", b"STMA", b"STM"):
            return
        if s.startswith(b"BUFFER") or s.startswith(b"<") or s in (b"STOPPED", b"?"):
            self.bad_lines += 1
            msg = s.decode(errors="ignore")
            if msg != self.last_error:
                self.last_error = msg
                self._log(f"[can] monitor: {msg}")
            if s.startswith(b"BUFFER"):
                # The adapter has stopped monitoring on its own; restart.
                with self._cond:
                    self._pause_requested = True
                    self._resume_after = time.monotonic() + 0.05
                    self._cond.notify_all()
            return
        try:
            n = len(s)
            if n < 3 or n % 2 == 0:
                # 11-bit: 3 hex chars + 2n → odd length. Even = 29-bit or junk.
                if n >= 8 and n % 2 == 0:
                    ident = int(s[:8], 16)
                    data = bytes.fromhex(s[8:].decode())
                else:
                    self.bad_lines += 1
                    return
            else:
                ident = int(s[:3], 16)
                data = bytes.fromhex(s[3:].decode())
        except ValueError:
            self.bad_lines += 1
            return
        self.store.record(ident, data, now)
        sink = self.capture_sink
        if sink is not None:
            try:
                sink(now, ident, data)
            except Exception:
                pass

    # ── stats surface shared with CANConnectionManager ──────────────────
    def duty_cycle(self, reset: bool = True) -> float:
        """Fraction of time spent streaming since the last call."""
        now = time.monotonic()
        acc = self._stream_accum
        if self._streaming:
            acc += now - self._stream_since
        span = max(1e-3, now - self._duty_mark)
        if reset:
            self._duty_mark = now
            self._stream_accum = 0.0
            if self._streaming:
                self._stream_since = now
        return max(0.0, min(1.0, acc / span))

    def stats_snapshot(self) -> dict:
        now = time.monotonic()
        last = self.store.last_frame_ts
        return {
            "total": self.store.total,
            "fps": self.store.rate(now),
            "unique_ids": self.store.unique_id_count(),
            "last_frame_ts": last,
            "age_ms": int((now - last) * 1000) if last > 0 else -1,
        }
