"""70mai dashcam over its own Wi-Fi hotspot.

Why this exists
---------------
The 70mai (A-series: A500S / A800S / A810 and the M300 family) is reachable
only through the hotspot it raises (`70mai_…`, WPA2, default key 12345678):
an HTTP CGI API on port 80 and an RTSP live stream on port 554 — that is the
whole phone app. Its Bluetooth is the iOS app's fast-connect plus the remote
button and carries no data, so it is not used here.

Three pieces, none of which exists until asked for:

- `HotspotLink`   — nmcli: find a camera's `<prefix>*` network on one adapter,
                    hold a NetworkManager profile for it, bring it up/down,
                    read whether it is up and the camera's address. Shared
                    with the action cam (`actioncam.py`); `DashcamLink` is the
                    70mai flavour.
- `DashcamClient` — the signed HTTP API. Every call is
                    `GET /cgi-bin/<cmd>.cgi?-k=v…&-timestamp=<unix>&-signkey=<md5>`
                    with `signkey = md5("<cmd>.cgi?" + "-k=v&…" + token)`.
                    The token comes from a one-time pairing (BindByBanya →
                    poll UserconfirmByBanya until the camera's power button is
                    pressed). Protocol per the A500S app reverse-engineering
                    (alu.dog) and the M300 toolbox (XuZhen86), which share the
                    same "Banya" CGI layer.
- the live picture is `camstream.StreamWorker` (shared with the action cam)
  on the RTSP URL found by `detect_rtsp()`.

Everything here shells out or blocks on the network: nothing may be called
from the Qt thread. `HotspotLink` owns a serialised worker exactly like
`WifiManager` (one queue — a status poll must not overtake a connect).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

from Modules.Runtime.wifi import _run, _split_terse

PAIR_KEY = "73VpsAfdety8FDd0"          # magic key for the pairing calls (op id -1)
PROFILE = "LaLaDash-Dashcam"           # our NetworkManager profile, when NM has none for the SSID
SSID_PREFIX = "70mai"
DEFAULT_PASSWORD = "12345678"
AUTH_FAIL = "-6677"                    # ResultCode: signkey/token rejected

# Where the live stream has been found so far, by model. Tried in order the
# first time the link is up; the winner is remembered in [Dashcam] rtsp_path.
RTSP_CANDIDATES = (
    "/liveRTSP/av4/track1",   # A800S
    "/liveRTSP/av4",          # 1S-style
    "/livestream/12",         # Pro / M300 / A500S (640x480)
    "/livestream/13",
    "/",
)

# getfilelist / getfilecount `-type`
FILE_FRONT_NORMAL = 0
FILE_FRONT_EVENT = 1
FILE_PHOTO = 3
FILE_REAR_EVENT = 6
FILE_REAR_NORMAL = 8


class DashcamError(Exception):
    pass


# ── signing ───────────────────────────────────────────────────────────────
def pair_key(payload: str) -> str:
    return hashlib.md5((payload + PAIR_KEY).encode()).hexdigest()


def sign(cmd: str, params: Dict[str, str], token: str, variant: str = "?") -> str:
    """signkey for a post-pairing call. `params` already holds -timestamp and
    is in the order it will be sent. `variant` is what sits between the
    command and the query: the M300 toolbox hashes `cmd?…`, the A500S app was
    seen sending `cmd?&…` — the client tries both once and keeps the one the
    camera accepts."""
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return hashlib.md5((cmd + variant + query + token).encode()).hexdigest()


def new_user_id() -> str:
    return "laladash" + secrets.token_hex(4)


# ── HTTP client ───────────────────────────────────────────────────────────
class DashcamClient:
    """Blocking, signed HTTP API. One instance per camera address."""

    def __init__(self, base_url: str, token: str = "", variant: str = "?",
                 timeout: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or ""
        self.variant = variant if variant in ("?", "?&") else "?"
        self.timeout = timeout
        self.last_url = ""

    # -- transport --------------------------------------------------------
    def _fetch(self, url: str) -> str:
        self.last_url = url
        req = urllib.request.Request(url, headers={"User-Agent": "LaLaDash"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise DashcamError(f"HTTP {e.code} on {url.split('?')[0]}")
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            reason = getattr(e, "reason", e)
            raise DashcamError(f"{reason}")

    @staticmethod
    def _parse(text: str) -> dict:
        text = text.strip()
        if not text:
            return {"ResultCode": "0", "Result": None}
        try:
            data = json.loads(text)
        except ValueError:
            # Some firmwares answer a bare number or "OK" on set-style calls.
            return {"ResultCode": "0", "Result": text}
        if isinstance(data, dict):
            return data
        return {"ResultCode": "0", "Result": data}

    def _url(self, cmd: str, params: Dict[str, str]) -> str:
        query = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}"
                         for k, v in params.items())
        return f"{self.base_url}/cgi-bin/{cmd}{self.variant}{query}"

    def call(self, cmd: str, params: Optional[Dict[str, object]] = None,
             signed: bool = True) -> dict:
        """Run `<cmd>.cgi`. Raises DashcamError on transport failure or a
        non-zero ResultCode (message carries the code)."""
        cmd = cmd if cmd.endswith(".cgi") else cmd + ".cgi"
        p: Dict[str, str] = {("-" + k if not k.startswith("-") else k): str(v)
                             for k, v in (params or {}).items()}
        if signed:
            if not self.token:
                raise DashcamError("not paired")
            p["-timestamp"] = str(int(time.time()))
            p["-signkey"] = sign(cmd, p, self.token, self.variant)
        data = self._parse(self._fetch(self._url(cmd, p)))
        code = str(data.get("ResultCode", "0"))
        if code not in ("0", "OK", ""):
            raise DashcamError(f"{cmd} ResultCode {code}")
        return data

    # -- pairing ----------------------------------------------------------
    def pair(self, user_id: str, wait_s: float = 60.0,
             on_prompt: Optional[Callable[[], None]] = None,
             should_abort: Optional[Callable[[], bool]] = None) -> str:
        """Bind this user id and wait for the camera's power button. Returns
        the token (also stored on the client)."""
        data = self._parse(self._fetch(self._url(
            "BindByBanya.cgi", {"-usr": user_id, "-signkey": pair_key(user_id)})))
        code = str(data.get("ResultCode", ""))
        result = data.get("Result") or {}
        if code != "0" or not isinstance(result, dict) or not result.get("Token"):
            raise DashcamError(f"bind refused (ResultCode {code or '?'})")
        token = str(result["Token"])
        stamp = str(result.get("timestamp") or "")
        if on_prompt:
            on_prompt()
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if should_abort and should_abort():
                raise DashcamError("pairing cancelled")
            try:
                d = self._parse(self._fetch(self._url(
                    "UserconfirmByBanya.cgi",
                    {"-timestamp": stamp, "-signkey": pair_key(stamp)})))
                if str(d.get("ResultCode", "")) == "0":
                    self.token = token
                    return token
            except DashcamError:
                pass
            time.sleep(1.0)
        raise DashcamError("no button press within %d s" % int(wait_s))

    def probe_variant(self) -> bool:
        """After pairing (or with a stored token): find which signing variant
        this firmware accepts. Leaves `variant` set; False = neither."""
        for v in ("?", "?&"):
            self.variant = v
            try:
                self.call("getdeviceattr")
                return True
            except DashcamError as e:
                if AUTH_FAIL not in str(e):
                    # Transport problem, not a signing one: keep the default.
                    self.variant = "?"
                    raise
        self.variant = "?"
        return False

    # -- commands ---------------------------------------------------------
    def register(self, my_ip: str) -> None:
        self.call("client", {"operation": "register", "ip": my_ip})

    def device_attr(self) -> dict:
        r = self.call("getdeviceattr").get("Result")
        return r if isinstance(r, dict) else {"value": r}

    def sd_state(self) -> dict:
        r = self.call("getsdstate").get("Result")
        return r if isinstance(r, dict) else {}

    def file_count(self) -> dict:
        r = self.call("getfilecount").get("Result")
        return r if isinstance(r, dict) else {}

    def file_list(self, ftype: int, start: int = 1, end: int = 5) -> List[dict]:
        r = self.call("getfilelist", {"start": start, "end": end, "type": ftype}).get("Result")
        return [x for x in r if isinstance(x, dict)] if isinstance(r, list) else []

    def snapshot(self) -> dict:
        return self.call("photo")

    def gps(self) -> dict:
        r = self.call("getgpsstatus").get("Result")
        return r if isinstance(r, dict) else ({"value": r} if r else {})

    def wifi(self) -> dict:
        r = self.call("getwifi").get("Result")
        return r if isinstance(r, dict) else {}


# ── helpers for unknown-shaped answers ────────────────────────────────────
def pick(d: dict, *needles: str, default: str = "") -> str:
    """First value whose key contains one of the needles (case-insensitive)."""
    if not isinstance(d, dict):
        return default
    low = {str(k).lower(): v for k, v in d.items()}
    for n in needles:
        for k, v in low.items():
            if n in k and v not in (None, ""):
                return str(v)
    return default


def summarise(d, limit: int = 90) -> str:
    """`k=v · k=v` for a dict we do not know the shape of."""
    if isinstance(d, dict):
        parts = [f"{k}={v}" for k, v in d.items() if not isinstance(v, (dict, list))]
        text = " · ".join(parts)
    else:
        text = str(d or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def human_size(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def sd_summary(sd: dict) -> Tuple[str, int]:
    """("SD OK · 61 % of 59.5 GB", 61) from getsdstate's Result. The unit of
    sdtotal/sdused is not documented; sizes are guessed from magnitude."""
    state = pick(sd, "sdstate", "state", default="")
    total = pick(sd, "sdtotal", "total")
    used = pick(sd, "sdused", "used")
    try:
        t = float(total)
        u = float(used)
    except (TypeError, ValueError):
        return (state or "SD ?", -1)
    if t <= 0:
        return (state or "SD ?", -1)
    scale = 1.0
    if t < 1e6:            # MB
        scale = 1024 * 1024
    elif t < 1e9:          # KB
        scale = 1024
    pct = int(round(100.0 * u / t))
    label = "SD OK" if "ok" in state.lower() else (state or "SD")
    return (f"{label} · {pct} % of {human_size(t * scale)}", pct)


_STAMP_RE = re.compile(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})")


def file_stamp(name: str) -> str:
    """`NO20210101-062950-000025.MP4` → `2021-01-01 06:29`."""
    m = _STAMP_RE.search(name or "")
    if not m:
        return name or ""
    y, mo, d, h, mi, _s = m.groups()
    return f"{y}-{mo}-{d} {h}:{mi}"


# ── Wi-Fi link (nmcli) ────────────────────────────────────────────────────
class HotspotLink:
    """Serialised nmcli front-end for a camera's own hotspot. All methods
    that touch nmcli run on the worker; `submit()` queues a job, results
    come back through the callback each job was given.

    `ssid_prefix` is what the camera's network starts with (`70mai`,
    `GP`/`GoPro`, `Insta360`); `profile_name` is the NetworkManager profile
    created when the Wi-Fi manager has not saved one for the SSID already.
    """

    def __init__(self, iface: str, ssid_prefix: str = SSID_PREFIX,
                 profile_name: str = PROFILE) -> None:
        self._iface = iface or ""
        self.ssid_prefix = ssid_prefix
        self.profile_name = profile_name
        self._queue: "deque[Callable[[], None]]" = deque()
        self._event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stop = False
        self._busy = False
        self._lock = threading.Lock()

    @property
    def iface(self) -> str:
        return self._iface

    def set_iface(self, iface: str) -> None:
        self._iface = iface or ""

    # -- worker -----------------------------------------------------------
    def submit(self, fn: Callable[[], None]) -> None:
        self._queue.append(fn)
        if self._thread is None or not self._thread.is_alive():
            self._stop = False
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="HotspotLink")
            self._thread.start()
        self._event.set()

    def _loop(self) -> None:
        while not self._stop:
            if not self._queue:
                # submit() sets the event, so this only bounds stop().
                self._event.wait(3.0)
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
            except Exception as exc:  # a job must never kill the worker
                print(f"[cam] job failed: {type(exc).__name__}: {exc}", flush=True)
            finally:
                with self._lock:
                    self._busy = False

    def stop(self) -> None:
        self._stop = True
        self._event.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=1.5)
        self._thread = None
        self._queue.clear()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy or bool(self._queue)

    # -- nmcli (worker thread only) ---------------------------------------
    def matches(self, ssid: str) -> bool:
        return any(ssid.lower().startswith(p.lower())
                   for p in self.ssid_prefix.split("|") if p)

    def detect(self, rescan: bool = True) -> List[Tuple[str, int]]:
        """[(ssid, signal)] of visible networks matching the prefix,
        strongest first."""
        args = ["nmcli", "-t", "-f", "SSID,SIGNAL", "dev", "wifi", "list"]
        if self._iface:
            args += ["ifname", self._iface]
        args += ["--rescan", "yes" if rescan else "no"]
        rc, out, _ = _run(args, timeout=25)
        found: Dict[str, int] = {}
        if rc != 0:
            return []
        for line in out.splitlines():
            parts = _split_terse(line)
            if len(parts) < 2:
                continue
            ssid = parts[0].strip()
            if not ssid or not self.matches(ssid):
                continue
            try:
                sig = int(parts[1])
            except ValueError:
                sig = 0
            found[ssid] = max(sig, found.get(ssid, 0))
        return sorted(found.items(), key=lambda kv: -kv[1])

    @staticmethod
    def profile_for_ssid(ssid: str) -> str:
        """Name of a saved NetworkManager profile for `ssid`, or ""."""
        rc, out, _ = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], timeout=8)
        if rc != 0:
            return ""
        names = []
        for line in out.splitlines():
            parts = _split_terse(line)
            if len(parts) >= 2 and "wireless" in parts[1]:
                names.append(parts[0])
        for name in names:
            rc, s, _ = _run(["nmcli", "-t", "-g", "802-11-wireless.ssid", "connection", "show", name], timeout=8)
            if rc == 0 and s.strip() == ssid:
                return name
        return ""

    def ensure_profile(self, ssid: str, password: str, autoconnect: bool) -> Tuple[str, str]:
        """Make sure NM has a profile for the camera pinned to our adapter
        that can never become the default route, and return (name, error).
        A profile the Wi-Fi manager saved earlier (the password lives in it)
        is reused; otherwise ours is created with `password`."""
        name = self.profile_for_ssid(ssid)
        flags = [
            "ipv4.never-default", "yes",
            "ipv4.route-metric", "700",
            "ipv6.method", "disabled",
            "connection.autoconnect", "yes" if autoconnect else "no",
            "connection.autoconnect-priority", "-10",
        ]
        if self._iface:
            flags += ["connection.interface-name", self._iface]
        if name:
            rc, _, err = _run(["nmcli", "connection", "modify", name] + flags, timeout=15)
            return (name, "" if rc == 0 else err.strip())
        args = ["nmcli", "connection", "add", "type", "wifi", "con-name", self.profile_name,
                "ssid", ssid]
        if self._iface:
            args += ["ifname", self._iface]
        if password:
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
        args += flags
        rc, _, err = _run(args, timeout=15)
        if rc != 0:
            return ("", err.strip() or "could not create the Wi-Fi profile")
        return (self.profile_name, "")

    def up(self, name: str) -> Tuple[bool, str]:
        args = ["nmcli", "connection", "up", "id", name]
        if self._iface:
            args += ["ifname", self._iface]
        rc, _, err = _run(args, timeout=50)
        return (rc == 0, "" if rc == 0 else _explain(err))

    def down(self, name: str) -> None:
        _run(["nmcli", "connection", "down", "id", name], timeout=20)

    def state(self) -> Dict[str, str]:
        """{'state': '100 (connected)', 'connection': name, 'gateway': ip,
        'address': ip} for our adapter."""
        info: Dict[str, str] = {"state": "", "connection": "", "gateway": "", "address": ""}
        if not self._iface:
            return info
        rc, out, _ = _run(["nmcli", "-t", "-f",
                           "GENERAL.STATE,GENERAL.CONNECTION,IP4.GATEWAY,IP4.ADDRESS",
                           "dev", "show", self._iface], timeout=8)
        if rc != 0:
            return info
        for line in out.splitlines():
            parts = _split_terse(line)
            if len(parts) < 2:
                continue
            key, val = parts[0].strip(), ":".join(parts[1:]).strip()
            if key == "GENERAL.STATE":
                info["state"] = val
            elif key == "GENERAL.CONNECTION":
                info["connection"] = val
            elif key == "IP4.GATEWAY":
                info["gateway"] = val
            elif key.startswith("IP4.ADDRESS") and not info["address"]:
                info["address"] = val.split("/")[0]
        return info

    def linked(self, extra_names=()) -> Optional[Tuple[str, str]]:
        """(gateway, our address) when the adapter is up on the camera's
        network — a profile we know or an SSID with the prefix — else None."""
        info = self.state()
        if not info.get("state", "").startswith("100"):
            return None
        conn = info.get("connection", "")
        ours = {n for n in (self.profile_name, *extra_names) if n}
        if conn not in ours and not self.matches(conn):
            return None
        gw = info.get("gateway", "")
        if not gw:
            return None
        return (gw, info.get("address", ""))


class DashcamLink(HotspotLink):
    """The 70mai flavour: `70mai_*` networks, profile `LaLaDash-Dashcam`."""

    def __init__(self, iface: str) -> None:
        super().__init__(iface, SSID_PREFIX, PROFILE)


def _explain(stderr: str) -> str:
    s = (stderr or "").strip()
    low = s.lower()
    if "secrets were required" in low or "no secrets" in low:
        return "Wrong password"
    if "no network with ssid" in low or "not found" in low:
        return "Camera network not in range"
    if "timeout" in low or "timed out" in low:
        return "Camera did not answer"
    return s.splitlines()[-1] if s else "Could not join"


# ── RTSP probing ──────────────────────────────────────────────────────────
def rtsp_describe_ok(url: str, timeout: float = 3.0) -> bool:
    """Bare RTSP OPTIONS/DESCRIBE over a socket: True when the server answers
    200 for this path. No GStreamer needed, so the probe script and the app
    share it."""
    u = urllib.parse.urlsplit(url)
    host, port = u.hostname or "", u.port or 554
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            req = (f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n"
                   f"User-Agent: LaLaDash\r\n\r\n")
            s.sendall(req.encode())
            data = s.recv(4096).decode("latin-1", "replace")
            return data.startswith("RTSP/1.0 200")
    except OSError:
        return False


def detect_rtsp(cam_ip: str, candidates=RTSP_CANDIDATES) -> str:
    for path in candidates:
        url = f"rtsp://{cam_ip}:554{path}"
        if rtsp_describe_ok(url):
            return url
    return ""


# ── live stream: see camstream.StreamWorker (shared with the action cam) ──
from Modules.Runtime.camstream import StreamWorker  # noqa: E402,F401  (re-export)


# ── command line: probe / pair / selftest ─────────────────────────────────
def _selftest() -> int:
    # The published example: md5("2001005" + "0c5269735ed4b129").
    got = hashlib.md5(("2001005" + "0c5269735ed4b129").encode()).hexdigest()
    want = "b3ef3dccf9ba266864a67003d27b5bb5"
    assert got == want, (got, want)
    # Signing shape used by the M300 toolbox.
    p = {"-start": "1", "-end": "5", "-type": "1", "-timestamp": "1700000000"}
    tok = "0123456789abcdef0123456789abcdef"
    assert sign("getfilelist.cgi", p, tok) == hashlib.md5(
        ("getfilelist.cgi?-start=1&-end=5&-type=1&-timestamp=1700000000" + tok).encode()).hexdigest()
    assert file_stamp("NO20210101-062950-000025.MP4") == "2021-01-01 06:29"
    assert sd_summary({"sdstate": "SDOK", "sdtotal": "61000", "sdused": "37210"})[1] == 61
    print("dashcam selftest ok")
    return 0


def _main(argv: List[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="70mai dashcam CLI (used by Tools/dashcam_probe.sh)")
    ap.add_argument("cmd", choices=["selftest", "pair", "call", "rtsp", "detect", "join", "leave",
                                    "state", "save"])
    ap.add_argument("--url", default="http://192.168.0.1", help="camera base URL")
    ap.add_argument("--token", default=os.environ.get("DASHCAM_TOKEN", ""))
    ap.add_argument("--user", default=os.environ.get("DASHCAM_USER", ""))
    ap.add_argument("--variant", default="?")
    ap.add_argument("--iface", default="wlan0")
    ap.add_argument("--ssid", default="", help="join: pin this SSID (default: strongest 70mai_*)")
    ap.add_argument("--password", default=DEFAULT_PASSWORD)
    ap.add_argument("args", nargs="*", help="call: <cmd> [k=v …]; rtsp: <cam ip>; save: k=v …")
    a = ap.parse_args(argv)
    if a.cmd == "selftest":
        return _selftest()
    if a.cmd == "detect":
        link = DashcamLink(a.iface)
        for ssid, sig in link.detect():
            print(f"{sig:3d}  {ssid}")
        return 0
    if a.cmd == "join":
        # The same path the app takes: reuse a saved profile or create ours.
        link = DashcamLink(a.iface)
        ssid = a.ssid
        if not ssid:
            found = link.detect()
            if not found:
                print("no 70mai network in range on", a.iface)
                return 2
            ssid = found[0][0]
        name, err = link.ensure_profile(ssid, a.password, autoconnect=False)
        if not name:
            print("profile:", err)
            return 2
        ok, err = link.up(name)
        print(f"{'joined' if ok else 'FAILED'} {ssid} via profile {name!r}" + (f": {err}" if err else ""))
        if not ok:
            return 2
        info = link.state()
        print(f"gateway {info.get('gateway','?')}  address {info.get('address','?')}")
        return 0
    if a.cmd == "leave":
        link = DashcamLink(a.iface)
        info = link.state()
        if info.get("connection"):
            link.down(info["connection"])
            print("left", info["connection"])
        return 0
    if a.cmd == "state":
        print(json.dumps(DashcamLink(a.iface).state()))
        return 0
    if a.cmd == "save":
        # Write [Dashcam] keys into Data/settings.ini (what the probe learned).
        from Modules.Runtime import settings_cache
        from preferences import DATA_SETTINGS
        for kv in a.args:
            k, _, v = kv.partition("=")
            settings_cache.set_value("Dashcam", k.strip(), v.strip(), path=DATA_SETTINGS)
            print(f"[Dashcam] {k.strip()} = {v.strip()}")
        return 0
    if a.cmd == "rtsp":
        ip = a.args[0] if a.args else urllib.parse.urlsplit(a.url).hostname
        for path in RTSP_CANDIDATES:
            url = f"rtsp://{ip}:554{path}"
            print(("OK   " if rtsp_describe_ok(url) else "no   ") + url)
        return 0
    client = DashcamClient(a.url, a.token, a.variant)
    if a.cmd == "pair":
        uid = a.user or new_user_id()
        print(f"user id {uid}")
        tok = client.pair(uid, on_prompt=lambda: print(">>> press the camera's power button now", flush=True))
        print(f"token {tok}")
        print(f"variant {'ok' if client.probe_variant() else 'NONE ACCEPTED'} {client.variant!r}")
        return 0
    if a.cmd == "call":
        if not a.args:
            ap.error("call needs a command name")
        name, kv = a.args[0], a.args[1:]
        params = dict(x.split("=", 1) for x in kv)
        signed = bool(a.token)
        try:
            print(json.dumps(client.call(name, params, signed=signed), indent=1))
        except DashcamError as e:
            print(f"ERROR {e}  ({client.last_url})")
            return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
