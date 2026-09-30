"""pyserial URL handler emulating the car's vLinker FS (an STN1170).

    fake://Data/can_captures/reference_2026-09-15_lights_ignition-on.txt[?speed=1.0]

Registered by ``Modules.Obd.stn_session.register_fake_adapter()`` so that
``serial.serial_for_url("fake://…")`` — which is how python-obd opens its
port — hands back this object instead of a real device. The dev PC has no
adapter; this is how the whole CAN chain (session handshake, monitor / PID
multiplexing, frame store, decoder, dash-light ingestion, the debugger
sheet) runs against a real capture before the next drive.

What it emulates, from what the real adapter did on 2026-09-15:

- ELM327 command/response: echo until ``ATE0``, ``ATZ`` banner, ``ATI``,
  ``AT@1``, ``ATRV``, ``ATDP``/``ATDPN``, ``ATSP``, ``ATH``/``ATS``/``ATL``/
  ``ATCAF``/``ATAL`` state, ``?`` for anything unknown.
- STN extras: ``STI`` → ``STN1170 v4.3.2``; ``STBRT``; ``STBR n`` with the
  real handshake (``OK`` at the old rate, banner at the new rate, host CR
  within STBRT, ``OK``) — and if the host does not switch ``baudrate`` the
  banner is unreadable, exactly like the real thing; ``STFAP id,mask`` /
  ``STFCP`` pass filters; ``STMA`` / ``STM`` monitoring.
- Monitoring streams the capture file's frames at real-time pace (``speed``
  scales it) in the current ``ATH``/``ATS``/``ATCAF`` format; ``CAF1`` turns
  non-ISO-TP frames into ``<DATA ERROR`` like the real ELM does, so a session
  that forgets ``ATCAF0`` fails visibly. Any byte from the host stops it.
  ``BUFFER FULL`` is raised if the host lets more than ``BUFFER_LIMIT`` bytes
  pile up unread (the 115200 failure mode).
- Mode-01 PIDs from a small table (``pids``), formatted for ``CAF0`` or
  ``CAF1`` and ``S0``/``S1``. ``0100/0120/0140`` return the reference car's real
  supported-PID masks so python-obd's command discovery matches the car.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse, parse_qs

import serial
from serial.serialutil import SerialBase, SerialException, PortNotOpenError

BANNER = "ELM327 v2.3"
STI = "STN1170 v4.3.2"
BUFFER_LIMIT = 64 * 1024
DEFAULT_BAUD = 115200

# Real answers from the reference car (Twingo III) (2026-09-15 probe), used verbatim.
_SUPPORTED = {"00": "AE7EB813", "20": "F007A001", "40": "FED20011"}


class FakeStn:
    """The device side. One per fake:// URL; shared state across the pyserial
    wrapper so a test can poke ``pids`` / ``speed`` and read ``log``."""

    def __init__(self, capture_path: str, speed: float = 1.0) -> None:
        self.capture_path = capture_path
        self.speed = max(0.05, float(speed))
        self.frames: List[tuple] = []          # (t, id_hex, data_hex)
        self._load(capture_path)
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.out: List[tuple] = []              # device → host: (baud, bytes)
        self.inbuf = bytearray()                # host → device (partial line)
        self.dev_baud = DEFAULT_BAUD
        self.host_baud = DEFAULT_BAUD           # what the host object is set to
        self.echo = True
        self.headers = False
        self.spaces = True
        self.caf = True
        self.linefeeds = False
        self.protocol = "0"
        self.filters: List[tuple] = []          # (id, mask)
        self.monitoring = False
        self._mon_thread: Optional[threading.Thread] = None
        self._brd_pending: Optional[int] = None
        self._brd_deadline = 0.0
        self.brt_ms = 1000
        # Mode-01 PID → data bytes, seeded with the reference car's own answers from
        # the 2026-09-15 probe (ignition on, engine off). Tests overwrite
        # entries (e.g. pids["0C"] for RPM) to drive scenarios.
        self.pids: Dict[str, bytes] = {
            "01": bytes([0x00, 0x07, 0xE5, 0x00]),   # STATUS: MIL off, 0 DTCs
            "05": bytes([0x40 + 28]),                # coolant 28 °C
            "0A": bytes([0x00]),                     # fuel pressure 0
            "0B": bytes([0x0A]),                     # intake pressure 10 kPa
            "0C": bytes([0x00, 0x00]),               # RPM 0
            "0D": bytes([0x00]),                     # speed 0
            "0E": bytes([0x80]),                     # timing advance 0°
            "0F": bytes([0x40 + 31]),                # intake temp 31 °C
            "11": bytes([0x3A]),                     # throttle 22.7 %
            "1F": bytes([0x00, 0x00]),               # run time 0
            "22": bytes([0x00, 0x00]),               # rail pressure (vac)
            "23": bytes([0x00, 0x00]),               # rail pressure (direct)
            "2F": bytes([0x49]),                     # fuel 28.6 %
            "30": bytes([0xFF]),                     # warm-ups 255
            "31": bytes([0x45, 0x79]),               # distance since clear 17785 km
            "33": bytes([0x65]),                     # baro 101 kPa
            "42": bytes([0x2F, 0xD0]),               # ECU voltage 12.24 V
            "43": bytes([0x00, 0x00]),               # absolute load 0 %
            "44": bytes([0x7A, 0x80]),               # commanded equiv ratio ≈0.957
            "45": bytes([0x20]),                     # relative throttle 12.5 %
            "46": bytes([0x40 + 45]),                # ambient 45 °C
            "49": bytes([0x07]),                     # accelerator D 2.7 %
            "4A": bytes([0x04]),                     # accelerator E 1.6 %
            "5C": bytes([0x40 + 28]),                # oil temp 28 °C
        }
        self.log: List[str] = []
        self.frames_sent = 0
        self.buffer_full_events = 0

    # ---- capture --------------------------------------------------------
    def _load(self, path: str) -> None:
        with open(path) as f:
            for line in f:
                if line.startswith("#"):
                    continue
                p = line.split()
                if len(p) == 3:
                    try:
                        self.frames.append((float(p[0]), p[1].upper(), p[2].upper()))
                    except ValueError:
                        pass
        if not self.frames:
            raise ValueError(f"no frames in {path}")

    # ---- host → device ---------------------------------------------------
    def host_write(self, data: bytes) -> None:
        with self.cond:
            if self.monitoring:
                # Any character stops monitoring; the character itself is
                # consumed (the real ELM does the same).
                self.monitoring = False
                self.cond.notify_all()
                self._emit("\r>")
                return
            if self._brd_pending is not None:
                if b"\r" in data and self.host_baud == self._brd_pending and time.monotonic() <= self._brd_deadline:
                    self.dev_baud = self._brd_pending
                    self._brd_pending = None
                    self._emit("OK\r\r>")
                else:
                    # Timeout or wrong rate: revert like the real chip.
                    self._brd_pending = None
                    self.dev_baud = DEFAULT_BAUD
                    self.log.append("STBR reverted")
                return
            if self.host_baud != self.dev_baud:
                self.log.append(f"garbled write at host {self.host_baud} vs dev {self.dev_baud}")
                return
            self.inbuf += data
            while b"\r" in self.inbuf:
                line, _, rest = bytes(self.inbuf).partition(b"\r")
                self.inbuf = bytearray(rest)
                cmd = line.decode(errors="ignore").strip()
                if self.echo:
                    self._emit(cmd + "\r")
                self._handle(cmd)

    def _emit(self, text: str) -> None:
        # Caller holds the lock. Bytes are tagged with the rate they were sent
        # at; host_read() hands them over only if the host is at that rate by
        # the time it reads them (an FTDI buffers them either way), and drops
        # them as line noise otherwise. That is what lets the STBR banner,
        # sent at the new rate, be read after the host switches.
        self.out.append((self.dev_baud, text.encode()))
        self.cond.notify_all()

    def _handle(self, cmd: str) -> None:
        c = cmd.upper().replace(" ", "")
        self.log.append(c)
        if not c:
            self._emit("\r>")
            return
        if c == "ATZ":
            self.echo = True; self.headers = False; self.spaces = True; self.caf = True
            self.protocol = "0"; self.filters = []; self.dev_baud = DEFAULT_BAUD
            self._emit(f"\r\r{BANNER}\r\r>")
        elif c in ("ATE0", "ATE1"):
            self.echo = c.endswith("1"); self._emit("OK\r\r>")
        elif c in ("ATH0", "ATH1"):
            self.headers = c.endswith("1"); self._emit("OK\r\r>")
        elif c in ("ATS0", "ATS1"):
            self.spaces = c.endswith("1"); self._emit("OK\r\r>")
        elif c in ("ATL0", "ATL1"):
            self.linefeeds = c.endswith("1"); self._emit("OK\r\r>")
        elif c in ("ATCAF0", "ATCAF1"):
            self.caf = c.endswith("1"); self._emit("OK\r\r>")
        elif c in ("ATAL", "ATAT1", "ATAT2", "ATST", "ATSTFF"):
            self._emit("OK\r\r>")
        elif c.startswith("ATSP"):
            self.protocol = c[4:] or "0"; self._emit("OK\r\r>")
        elif c == "ATDPN":
            self._emit(("A6" if self.protocol in ("0", "6") else self.protocol) + "\r\r>")
        elif c == "ATDP":
            self._emit("ISO 15765-4 (CAN 11/500)\r\r>")
        elif c == "ATI":
            self._emit(f"{BANNER}\r\r>")
        elif c == "AT@1":
            self._emit("OBDII to RS232 Interpreter\r\r>")
        elif c == "ATRV":
            self._emit("12.2V\r\r>")
        elif c == "STI":
            self._emit(f"{STI}\r\r>")
        elif c.startswith("STBRT"):
            try:
                self.brt_ms = int(c[5:])
            except ValueError:
                pass
            self._emit("OK\r\r>")
        elif c.startswith("STBR"):
            try:
                target = int(c[4:])
            except ValueError:
                self._emit("?\r\r>"); return
            self._emit("OK\r")
            self._brd_pending = target
            self._brd_deadline = time.monotonic() + self.brt_ms / 1000.0
            # The banner goes out at the NEW rate: readable only once the host
            # has switched. Modelled by emitting after moving dev_baud.
            self.dev_baud = target
            self._emit(f"{STI}\r")
            # dev_baud stays at target only if the host confirms (host_write).
        elif c.startswith("STFAP"):
            try:
                idp, mask = c[5:].split(",")
                self.filters.append((int(idp, 16), int(mask, 16)))
                self._emit("OK\r\r>")
            except ValueError:
                self._emit("?\r\r>")
        elif c == "STFCP":
            self.filters = []; self._emit("OK\r\r>")
        elif c in ("STMA", "STM", "ATMA"):
            use_filters = c == "STM"
            if use_filters and not self.filters:
                self._emit("\r>")   # STM with no filters passes nothing
                return
            self.monitoring = True
            self._mon_gen = getattr(self, "_mon_gen", 0) + 1
            self._mon_thread = threading.Thread(target=self._monitor, args=(use_filters, self._mon_gen), daemon=True)
            self._mon_thread.start()
        elif c.startswith("01") and len(c) in (4, 5):
            self._pid(c[2:4])
        elif c.startswith("ST") or c.startswith("AT"):
            self._emit("?\r\r>")
        else:
            self._emit("NO DATA\r\r>")

    # ---- PID replies ----------------------------------------------------
    def _fmt(self, ident: str, data: bytes) -> str:
        payload = data
        if self.caf:
            # ELM CAF1 prints the ISO-TP PCI then the data bytes, no padding.
            hexs = [f"{len(payload):02X}"] + [f"{b:02X}" for b in payload]
        else:
            raw = bytes([len(payload)]) + payload
            raw = raw + b"\xAA" * (8 - len(raw))
            hexs = [f"{b:02X}" for b in raw]
        head = ident if self.headers else ""
        if self.spaces:
            return (head + " " if head else "") + " ".join(hexs)
        return head + "".join(hexs)

    def _pid(self, pid: str) -> None:
        pid = pid.upper()
        if pid in _SUPPORTED:
            data = bytes([0x41, int(pid, 16)]) + bytes.fromhex(_SUPPORTED[pid])
        elif pid in self.pids:
            data = bytes([0x41, int(pid, 16)]) + self.pids[pid]
        else:
            self._emit("NO DATA\r\r>")
            return
        if self.protocol == "0" and not getattr(self, "_searched", False):
            self._searched = True
            self._emit("SEARCHING...\r")
        self._emit(self._fmt("7E8", data) + "\r\r>")

    # ---- monitoring -----------------------------------------------------
    def _passes(self, ident: int, use_filters: bool) -> bool:
        if not use_filters:
            return True
        return any((ident & mask) == (fid & mask) for fid, mask in self.filters)

    def _monitor(self, use_filters: bool, gen: int = 0) -> None:
        # The bus keeps flowing while the host is not listening: the replay
        # clock starts at the first STMA and never stops, so a restart skips
        # whatever "happened" in between instead of rewinding to t=0.
        if not hasattr(self, "_replay_t0"):
            self._replay_t0 = time.monotonic()
        base = self.frames[0][0]
        span = (self.frames[-1][0] - base) + 0.01
        n = len(self.frames)

        def virtual_now() -> float:
            return ((time.monotonic() - self._replay_t0) * self.speed) % span

        # Position at the first frame not yet due.
        vt = virtual_now()
        i = 0
        while i < n and (self.frames[i][0] - base) < vt:
            i += 1
        if i >= n:
            i = 0
        while True:
            with self.cond:
                if not self.monitoring or getattr(self, "_mon_gen", gen) != gen:
                    return
            t, ident, data = self.frames[i]
            rel = t - base
            vt = virtual_now()
            # Wrapped around the file end → continue from the start.
            if i == 0 and vt > span / 2:
                time.sleep(0.005)
                continue
            if rel > vt:
                time.sleep(min((rel - vt) / self.speed, 0.05))
                continue
            i += 1
            if i >= n:
                i = 0
            if not self._passes(int(ident, 16), use_filters):
                continue
            with self.cond:
                if not self.monitoring or getattr(self, "_mon_gen", gen) != gen:
                    return
                if sum(len(d) for _, d in self.out) > BUFFER_LIMIT:
                    self.buffer_full_events += 1
                    self.monitoring = False
                    self.out.append((self.dev_baud, b"BUFFER FULL\r\r>"))
                    self.cond.notify_all()
                    return
                if self.caf:
                    line = "<DATA ERROR"
                else:
                    head = ident if self.headers else ""
                    if self.spaces:
                        bytes_ = [data[k:k + 2] for k in range(0, len(data), 2)]
                        line = (head + " " if head else "") + " ".join(bytes_)
                    else:
                        line = head + data
                self.out.append((self.dev_baud, (line + "\r").encode()))
                self.frames_sent += 1
                self.cond.notify_all()

    # ---- device → host ---------------------------------------------------
    def _drop_garbled(self) -> None:
        # Caller holds the lock: segments sent at a rate the host is not on.
        while self.out and self.out[0][0] != self.host_baud:
            baud, data = self.out.pop(0)
            self.log.append(f"garbled {len(data)}B sent at {baud}, host at {self.host_baud}")

    def host_read(self, size: int, timeout: Optional[float]) -> bytes:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self.cond:
            while True:
                self._drop_garbled()
                if self.out:
                    break
                if deadline is None:
                    self.cond.wait(0.1)
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return b""
                    self.cond.wait(min(remaining, 0.1))
            baud, data = self.out[0]
            chunk = data[:size]
            rest = data[size:]
            if rest:
                self.out[0] = (baud, rest)
            else:
                self.out.pop(0)
            return chunk

    def host_in_waiting(self) -> int:
        with self.lock:
            return sum(len(d) for b, d in self.out if b == self.host_baud)

    def host_reset_input(self) -> None:
        with self.lock:
            self.out.clear()


_DEVICES: Dict[str, FakeStn] = {}


def device_for(path: str, speed: float = 1.0, start_baud: int = 0, streaming: bool = False) -> FakeStn:
    """One FakeStn per capture path so tests and the session share state.

    ``start_baud`` / ``streaming`` model an adapter a killed session left
    behind: UART already at the fast rate, STMA still running. That is the
    state the car's vLinker was found in (OBD pin 16 keeps it powered with
    the car off, so nothing resets it) — `?start_baud=2000000&streaming=1`."""
    dev = _DEVICES.get(path)
    if dev is None:
        dev = FakeStn(path, speed=speed)
        _DEVICES[path] = dev
        if start_baud:
            dev.dev_baud = int(start_baud)
            dev.echo = False; dev.caf = False; dev.spaces = False; dev.headers = True
            if streaming:
                dev.host_baud = int(start_baud)   # nobody listening yet; frames pile up
                dev._handle("STMA")
                dev.host_baud = DEFAULT_BAUD
    return dev


class Serial(SerialBase):
    """pyserial front for FakeStn — what serial_for_url('fake://…') returns."""

    def __init__(self, *args, **kwargs):
        self._dev: Optional[FakeStn] = None
        super().__init__(*args, **kwargs)

    def from_url(self, url: str) -> str:
        parts = urlparse(url)
        if parts.scheme != "fake":
            raise SerialException(f"expected fake:// url, got {url!r}")
        path = (parts.netloc + parts.path).lstrip("/")
        if not path.startswith("/"):
            path = path  # relative to cwd (BASE_DIR after main.py's chdir)
        q = parse_qs(parts.query)
        speed = float(q.get("speed", ["1.0"])[0])
        start_baud = int(q.get("start_baud", ["0"])[0])
        streaming = q.get("streaming", ["0"])[0] in ("1", "true", "yes")
        self._dev = device_for(path, speed, start_baud, streaming)
        return path

    def open(self) -> None:
        if self._port is None:
            raise SerialException("Port must be configured before it can be used.")
        if self.is_open:
            raise SerialException("Port is already open.")
        self.from_url(self.portstr)
        self.is_open = True
        self._reconfigure_port()

    def close(self) -> None:
        if self.is_open:
            self.is_open = False
            dev = self._dev
            if dev is not None:
                with dev.cond:
                    dev.monitoring = False
                    dev.cond.notify_all()

    def _reconfigure_port(self) -> None:
        if self._dev is not None:
            with self._dev.lock:
                self._dev.host_baud = int(self._baudrate)

    @property
    def in_waiting(self) -> int:
        if not self.is_open:
            raise PortNotOpenError()
        return self._dev.host_in_waiting()

    def read(self, size: int = 1) -> bytes:
        if not self.is_open:
            raise PortNotOpenError()
        return self._dev.host_read(size, self._timeout)

    def write(self, data) -> int:
        if not self.is_open:
            raise PortNotOpenError()
        self._dev.host_write(bytes(data))
        return len(data)

    def reset_input_buffer(self) -> None:
        if not self.is_open:
            raise PortNotOpenError()
        self._dev.host_reset_input()

    def reset_output_buffer(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def cancel_read(self) -> None:
        pass

    def cancel_write(self) -> None:
        pass

    # SerialBase abstract bits we don't model
    def _update_break_state(self): pass
    def _update_rts_state(self): pass
    def _update_dtr_state(self): pass
    @property
    def cts(self): return True
    @property
    def dsr(self): return True
    @property
    def ri(self): return False
    @property
    def cd(self): return True
