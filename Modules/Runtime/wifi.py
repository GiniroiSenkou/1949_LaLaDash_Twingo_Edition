"""nmcli-backed Wi-Fi control.

Everything here shells out, so nothing in this module may be called from the Qt
main thread — `WifiManager` owns a single worker thread and every public entry
point queues onto it, calling back with the result. The bridge above turns those
callbacks into signals.

One worker rather than a thread per action, because the actions are ordered:
a scan started while a connect is still negotiating returns a list that does not
yet know about the network being joined.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional


# nmcli's terse output escapes the field separator and the escape character
# itself with a backslash. SSIDs really do contain colons, so a plain split()
# corrupts them.
def _split_terse(line: str, sep: str = ":") -> List[str]:
    fields: List[str] = []
    buf: List[str] = []
    escaped = False
    for ch in line:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == sep:
            fields.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    fields.append("".join(buf))
    return fields


def _run(args: List[str], timeout: float = 20.0) -> tuple[int, str, str]:
    try:
        cp = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return cp.returncode, cp.stdout or "", cp.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except Exception as e:
        return 1, "", str(e)


def list_wifi_ifaces() -> List[str]:
    """Every wifi-type device nmcli currently sees, in the order it reports
    them — unlike `WifiManager._wifi_iface()`, this is a full enumeration, not
    just the first match. It's how a second (USB dongle) adapter is noticed
    at all; see `WifiAdapterWatcher` below."""
    rc, out, _ = _run(["nmcli", "-t", "-f", "DEVICE,TYPE", "device"], timeout=8)
    if rc != 0:
        return []
    ifaces = []
    for line in out.splitlines():
        parts = _split_terse(line)
        if len(parts) >= 2 and parts[1].strip() == "wifi":
            ifaces.append(parts[0].strip())
    return ifaces


class WifiManager:
    """Serialised nmcli front-end. All methods return immediately.

    `iface`, when given, pins every scan/connect/disconnect to that device
    (`nmcli ... ifname <iface>`) instead of whichever one nmcli would pick by
    default. This is what lets a second, occasional USB Wi-Fi dongle be
    driven independently of the car's onboard adapter: the primary
    `WifiManager()` (iface=None) is untouched, and a second instance
    constructed with the dongle's interface name gets its own worker queue —
    a slow connect on one adapter must never stall a scan on the other.
    """

    def __init__(self, iface: Optional[str] = None) -> None:
        self._iface = iface or None
        self._queue: "deque[Callable[[], None]]" = deque()
        self._event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stop = False
        self._busy = False
        self._lock = threading.Lock()

    @property
    def iface(self) -> Optional[str]:
        return self._iface

    def set_iface(self, iface: Optional[str]) -> None:
        """Re-pin (or unpin) the device. Read by the worker at the start of
        each job, so it takes effect from the next scan/connect on."""
        self._iface = iface or None

    # ── lifecycle ─────────────────────────────────────────────────────────
    @staticmethod
    def available() -> bool:
        return shutil.which("nmcli") is not None

    def _submit(self, fn: Callable[[], None]) -> None:
        self._queue.append(fn)
        if self._thread is None or not self._thread.is_alive():
            self._stop = False
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="WifiManager"
            )
            self._thread.start()
        self._event.set()

    def _loop(self) -> None:
        while not self._stop:
            if not self._queue:
                self._event.wait(1.0)
                self._event.clear()
                continue
            try:
                fn = self._queue.popleft()
            except IndexError:
                continue
            with self._lock:
                self._busy = True
            try:
                fn()
            except Exception:
                pass
            finally:
                with self._lock:
                    self._busy = False

    def stop(self) -> None:
        self._stop = True
        self._event.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=1.0)
        self._thread = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy or bool(self._queue)

    # ── reads ─────────────────────────────────────────────────────────────
    @staticmethod
    def _saved_ssids() -> set:
        """SSIDs NetworkManager already holds a profile for."""
        rc, out, _ = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], timeout=8)
        saved = set()
        if rc != 0:
            return saved
        for line in out.splitlines():
            parts = _split_terse(line)
            if len(parts) >= 2 and "wireless" in parts[1]:
                saved.add(parts[0])
        return saved

    def _scan_once(self, rescan: bool) -> List[Dict]:
        ifname_args = ["ifname", self._iface] if self._iface else []
        if rescan:
            # Fails with "scanning not allowed immediately following previous
            # scan" if called too often. The cached list is still returned.
            _run(["nmcli", "dev", "wifi", "rescan"] + ifname_args, timeout=15)

        rc, out, _ = _run(
            ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,FREQ", "dev", "wifi", "list"]
            + ifname_args,
            timeout=15,
        )
        if rc != 0:
            return []

        saved = WifiManager._saved_ssids()
        seen: Dict[str, Dict] = {}
        for line in out.splitlines():
            parts = _split_terse(line)
            if len(parts) < 4:
                continue
            in_use = parts[0].strip() == "*"
            ssid = parts[1].strip()
            if not ssid:
                continue  # hidden network — nothing to show or tap
            try:
                signal = int(parts[2].strip() or 0)
            except ValueError:
                signal = 0
            security = parts[3].strip()
            try:
                freq = int((parts[4] if len(parts) > 4 else "").split()[0])
            except (ValueError, IndexError):
                freq = 0

            entry = {
                "ssid": ssid,
                "signal": max(0, min(100, signal)),
                "security": security,
                "secured": bool(security and security != "--"),
                "active": in_use,
                "saved": ssid in saved,
                "band": "5 GHz" if freq >= 4000 else ("2.4 GHz" if freq else ""),
            }
            # The same SSID shows once per band/BSSID; keep the strongest, but
            # never let a weaker duplicate hide that one of them is connected.
            prev = seen.get(ssid)
            if prev is None:
                seen[ssid] = entry
            else:
                if entry["signal"] > prev["signal"]:
                    entry["active"] = entry["active"] or prev["active"]
                    seen[ssid] = entry
                elif entry["active"]:
                    prev["active"] = True

        rows = list(seen.values())
        rows.sort(key=lambda r: (not r["active"], not r["saved"], -r["signal"]))
        return rows

    def scan(self, callback: Callable[[List[Dict]], None], rescan: bool = True) -> None:
        def _work():
            callback(self._scan_once(rescan))

        self._submit(_work)

    # ── writes ────────────────────────────────────────────────────────────
    def connect(self, ssid: str, password: str,
                callback: Callable[[bool, str], None]) -> None:
        """Join `ssid`. An empty password reuses the saved profile."""
        ssid = str(ssid or "")
        password = str(password or "")

        def _work():
            if not ssid:
                callback(False, "No network selected")
                return

            ifname_args = ["ifname", self._iface] if self._iface else []

            # A stored profile knows the password already, and `connection up`
            # keeps whatever else was configured for it (static IP, priority).
            if not password and ssid in self._saved_ssids():
                rc, _, err = _run(
                    ["nmcli", "connection", "up", "id", ssid] + ifname_args, timeout=45
                )
                if rc == 0:
                    callback(True, f"Connected to {ssid}")
                    return
                callback(False, self._explain(err) or f"Could not join {ssid}")
                return

            args = ["nmcli", "dev", "wifi", "connect", ssid]
            if password:
                args += ["password", password]
            args += ifname_args
            rc, _, err = _run(args, timeout=60)
            if rc == 0:
                callback(True, f"Connected to {ssid}")
            else:
                callback(False, self._explain(err) or f"Could not join {ssid}")

        self._submit(_work)

    def disconnect(self, ssid: str, callback: Callable[[bool, str], None]) -> None:
        def _work():
            target = str(ssid or "").strip()
            if target:
                rc, _, err = _run(["nmcli", "connection", "down", "id", target], timeout=20)
                if rc == 0:
                    callback(True, f"Disconnected from {target}")
                    return
                callback(False, self._explain(err) or "Disconnect failed")
                return
            # No SSID given: drop whatever the wireless device is on. A
            # manager bound to a specific adapter always targets that one;
            # the primary (iface=None) manager auto-detects, exactly as
            # before this class supported multiple adapters at all.
            iface = self._iface or self._wifi_iface()
            if not iface:
                callback(False, "No Wi-Fi device")
                return
            rc, _, err = _run(["nmcli", "device", "disconnect", iface], timeout=20)
            callback(rc == 0, "Disconnected" if rc == 0
                     else (self._explain(err) or "Disconnect failed"))

        self._submit(_work)

    def forget(self, ssid: str, callback: Callable[[bool, str], None]) -> None:
        def _work():
            target = str(ssid or "").strip()
            if not target:
                callback(False, "No network selected")
                return
            rc, _, err = _run(["nmcli", "connection", "delete", "id", target], timeout=20)
            callback(rc == 0, f"Forgot {target}" if rc == 0
                     else (self._explain(err) or "Could not forget it"))

        self._submit(_work)

    def set_radio(self, on: bool, callback: Callable[[bool, str], None]) -> None:
        def _work():
            rc, _, err = _run(["nmcli", "radio", "wifi", "on" if on else "off"], timeout=15)
            callback(rc == 0, ("Wi-Fi on" if on else "Wi-Fi off") if rc == 0
                     else (self._explain(err) or "Radio change failed"))

        self._submit(_work)

    # ── helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def _wifi_iface() -> str:
        """First wifi-type device nmcli reports. Fine for the primary
        (unbound) manager, which only ever needs "the" adapter; a second
        adapter is invisible to this on purpose — see list_wifi_ifaces()."""
        ifaces = list_wifi_ifaces()
        return ifaces[0] if ifaces else ""

    @staticmethod
    def _explain(stderr: str) -> str:
        """Turn nmcli's stderr into something readable on a car screen."""
        text = (stderr or "").strip().splitlines()
        if not text:
            return ""
        msg = text[-1].strip()
        low = msg.lower()
        if "secrets were required" in low or "no secrets" in low:
            return "Wrong password"
        if "not authorized" in low or "not permitted" in low:
            return "Not permitted — check polkit"
        if "no network with ssid" in low:
            return "Network out of range"
        if "timed out" in low or "timeout" in low:
            return "Timed out"
        # nmcli prefixes most failures with "Error: "
        if msg.lower().startswith("error:"):
            msg = msg[6:].strip()
        return msg[:120]


class WifiAdapterWatcher:
    """Notices a second wifi-type device appearing/vanishing.

    Modeled directly on `Modules/Music/media_keys.py`'s `MediaKeyListener`:
    plain poll-and-diff on a daemon thread (nmcli has no push/event API worth
    using here, and there is no udev/pyudev dependency in this project), with
    a slower cadence while nothing has changed and a faster one right after a
    change — a plugged-in dongle should be usable within a few seconds, but
    there is no reason to shell out to nmcli every second forever once the
    adapter set has settled.

    The "primary" adapter is whichever interface was first seen; anything
    beyond that is reported as secondary. Once an interface has been seen as
    primary it keeps that role even if it briefly drops out and comes back
    (e.g. driver reset), so unplugging the *dongle* can never make the
    watcher decide the onboard adapter is suddenly "secondary".
    """

    IDLE_POLL_S = 6.0
    SETTLING_POLL_S = 2.0
    # How long to keep polling at the faster cadence after the last change,
    # before dropping back to idle.
    _SETTLE_WINDOW_S = 20.0

    def __init__(self, on_change: Callable[[], None]) -> None:
        self._on_change = on_change
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._primary_iface: Optional[str] = None
        self._secondary_iface: Optional[str] = None
        self._last_change_ts = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="WifiAdapterWatcher"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=1.5)
        self._thread = None

    @property
    def primary_iface(self) -> Optional[str]:
        with self._lock:
            return self._primary_iface

    @property
    def secondary_iface(self) -> Optional[str]:
        with self._lock:
            return self._secondary_iface

    def _poll_once(self) -> bool:
        """Returns True if the primary/secondary assignment changed."""
        ifaces = list_wifi_ifaces()
        with self._lock:
            if self._primary_iface and self._primary_iface not in ifaces:
                # The primary vanished (dongle was actually the first-seen
                # device, e.g. this run started with only it plugged in, or
                # the onboard adapter itself dropped out). Whatever is left
                # takes over as primary rather than leaving a dead reference.
                self._primary_iface = None
            if self._primary_iface is None and ifaces:
                self._primary_iface = ifaces[0]
            others = [i for i in ifaces if i != self._primary_iface]
            new_secondary = others[0] if others else None
            changed = new_secondary != self._secondary_iface
            self._secondary_iface = new_secondary
        return changed

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                changed = self._poll_once()
            except Exception:
                changed = False
            if changed:
                self._last_change_ts = time.monotonic()
                try:
                    self._on_change()
                except Exception:
                    pass
            settling = (time.monotonic() - self._last_change_ts) < self._SETTLE_WINDOW_S
            self._stop.wait(self.SETTLING_POLL_S if settling else self.IDLE_POLL_S)
