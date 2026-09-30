"""Keep the car's Bluetooth head unit (e.g. Renault R-Link) connected.

Why this exists
---------------
The Pi and the head unit power up together on ignition. Whichever side tries
to connect first finds the other still booting: on 2026-09-15 bluetoothd
logged `Unable to get Hands-Free Voice gateway SDP record: Host is down`
13 s after boot and nothing on either side ever retried, so the car stayed
silent until someone connected by hand. A warm reboot with the head unit
already awake connected by itself at +10 s — the pairing is fine, it is
purely a timing race.

What it does
------------
A daemon thread (same shape as `Modules/Runtime/wifi.py`'s
`WifiAdapterWatcher`) asks BlueZ over the system bus for paired *and*
trusted devices that advertise the A2DP Audio Sink UUID — that is the head
unit, never a phone or a keyboard — and, when none is connected, calls
`org.bluez.Device1.Connect()`: exactly what `bluetoothctl connect <mac>`
does by hand, so the same profiles (A2DP, AVRCP, HFP) come up. Nothing is
paired or trusted from here; that stays a one-time manual step.

Cadence: first try 3 s after start, then every 5 s for the first three
minutes (the head unit is still booting), then every 30 s for the life of
the app. While connected each tick is a single D-Bus property read — no
subprocess, no GLib main loop of its own. `kick()` wakes the loop early
(the sink watcher calls it when a `bluez_output.*` sink vanishes).

pydbus synchronous calls do not need a running main loop, which is why this
does not have to share the MPRIS bridge's GLib loop or run one of its own.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

AUDIO_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"

_BLUEZ = "org.bluez"
_DEVICE_IFACE = "org.bluez.Device1"


def _mac_to_path_fragment(mac: str) -> str:
    return "dev_" + mac.strip().upper().replace(":", "_")


class BtAudioAutoConnect:
    FIRST_DELAY_S = 3.0
    FAST_POLL_S = 5.0
    FAST_WINDOW_S = 180.0
    SLOW_POLL_S = 30.0
    # After a kick(), don't re-check more often than this — a Bluetooth
    # teardown produces a burst of sink events.
    _KICK_MIN_GAP_S = 2.0

    def __init__(self, on_change: Optional[Callable[[], None]] = None, device: str = "") -> None:
        self._on_change = on_change
        # Optional MAC pin ([Bluetooth] device). Empty = any trusted audio sink.
        self._pin = _mac_to_path_fragment(device) if device else ""
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._bus = None
        self._status = "idle"
        self._connected_name = ""
        self._attempts = 0
        self._started_at = 0.0
        self._last_check = 0.0
        self._last_error = ""

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="BtAudioAutoConnect"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=1.5)
        self._thread = None

    def kick(self) -> None:
        """Re-check now (well, within _KICK_MIN_GAP_S)."""
        self._wake.set()

    # ── state for the UI ──────────────────────────────────────────────────
    @property
    def status(self) -> str:
        """idle | unavailable | no-device | connecting | connected"""
        with self._lock:
            return self._status

    @property
    def connected_name(self) -> str:
        with self._lock:
            return self._connected_name

    @property
    def attempts(self) -> int:
        with self._lock:
            return self._attempts

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    def _set(self, status: str, name: str = "", error: Optional[str] = None) -> None:
        with self._lock:
            changed = (status != self._status) or (name != self._connected_name)
            self._status = status
            self._connected_name = name
            if error is not None:
                self._last_error = error
        if changed and self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                pass

    # ── BlueZ ─────────────────────────────────────────────────────────────
    def _get_bus(self):
        if self._bus is None:
            from pydbus import SystemBus  # type: ignore
            self._bus = SystemBus()
        return self._bus

    def _audio_targets(self) -> list[tuple[str, dict]]:
        """(object_path, Device1 props) for every paired+trusted audio sink,
        pinned one first if configured, connected ones first otherwise."""
        bus = self._get_bus()
        objs = bus.get(_BLUEZ, "/").GetManagedObjects()
        out: list[tuple[str, dict]] = []
        for path, ifaces in objs.items():
            dev = ifaces.get(_DEVICE_IFACE)
            if not dev:
                continue
            if self._pin and not path.endswith(self._pin):
                continue
            if not (dev.get("Paired") and dev.get("Trusted")):
                continue
            if AUDIO_SINK_UUID not in (dev.get("UUIDs") or []):
                continue
            out.append((path, dev))
        out.sort(key=lambda pd: not pd[1].get("Connected", False))
        return out

    def _check_once(self) -> bool:
        """One pass. Returns True when a connect attempt was made."""
        try:
            targets = self._audio_targets()
        except Exception as exc:
            self._bus = None
            self._set("unavailable", error=str(exc))
            return False

        if not targets:
            self._set("no-device")
            return False

        path, props = targets[0]
        name = str(props.get("Alias") or props.get("Name") or path.rsplit("/", 1)[-1])
        if props.get("Connected"):
            if self._attempts:
                print(f"[bluetooth] {name} connected", flush=True)
            with self._lock:
                self._attempts = 0
            self._set("connected", name)
            return False

        with self._lock:
            self._attempts += 1
            n = self._attempts
        self._set("connecting", name)
        try:
            self._get_bus().get(_BLUEZ, path).Connect()
            print(f"[bluetooth] connected to {name} (attempt {n})", flush=True)
            self._set("connected", name, error="")
        except Exception as exc:
            msg = str(exc).strip().splitlines()[-1] if str(exc).strip() else "unknown error"
            # First failure and then every tenth: the head unit being off is
            # the normal case, not something to log 120 times an hour.
            if n == 1 or n % 10 == 0:
                print(f"[bluetooth] connect to {name} failed (attempt {n}): {msg}", flush=True)
            self._set("connecting", name, error=msg)
        return True

    # ── thread ────────────────────────────────────────────────────────────
    def _loop(self) -> None:
        if self._stop.wait(self.FIRST_DELAY_S):
            return
        while not self._stop.is_set():
            now = time.monotonic()
            if now - self._last_check >= self._KICK_MIN_GAP_S:
                self._last_check = now
                try:
                    self._check_once()
                except Exception as exc:
                    self._set("unavailable", error=str(exc))
            fast = (time.monotonic() - self._started_at) < self.FAST_WINDOW_S
            self._wake.clear()
            self._wake.wait(self.FAST_POLL_S if fast else self.SLOW_POLL_S)
