"""Action cameras — GoPro (Open GoPro) and Insta360 — over USB or Wi-Fi.

Why this exists
---------------
A GoPro or an Insta360 in the car is a rear/side view and a trip recorder
the dashboard can drive. Unlike the 70mai there are several ways in, and
they differ by model, so this module is a set of small parts the
`ActionCam` bridge (bridges/actioncam_bridge.py) composes:

- `UsbNetLink`   — a GoPro on a USB cable ("GoPro Connect" USB mode) shows up
                   as a USB-ethernet device (`usb0` / `enx…`, USB vendor 2672).
                   The camera runs DHCP: we get 172.2X.1YZ.5x, the camera is
                   `.51`. The NetworkManager profile is `never-default` — the
                   camera would otherwise hand the Pi a dead default route.
- `UvcLink`      — an Insta360 in its Webcam mode (or any UVC camera) is a
                   `/dev/videoN`; picture only, nothing to control.
- `GoProHttp`    — Open GoPro 2.0 over HTTP, identical on USB and Wi-Fi:
                   state, shutter, keep-alive, preview / webcam stream to a
                   UDP port on our side (MPEG-TS), media list.
- `GoProBle`     — the one thing Wi-Fi needs that USB does not: the camera's
                   AP is off until a BLE write turns it on. BlueZ over pydbus,
                   the bus `bt_autoconnect.py` already uses.
- `Insta360Wifi` — the community-reverse-engineered protobuf protocol on TCP
                   6666 (control + in-band live H.264/H.265). See its docstring
                   for how far that goes.

Wi-Fi joining itself is `dashcam.HotspotLink` with the camera's SSID prefix.
Everything here blocks (nmcli, HTTP, sockets): worker thread only.
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

from Modules.Runtime.wifi import _run, _split_terse

GOPRO_USB_VENDOR = "2672"
GOPRO_HTTP_PORT = 8080
GOPRO_WIFI_IP = "10.5.5.9"
GOPRO_STREAM_PORT = 8554
GOPRO_USB_PROFILE = "LaLaDash-GoPro-USB"
GOPRO_SSID_PREFIX = "GP|GoPro|HERO"
INSTA360_SSID_PREFIX = "Insta360|X3|X4|X5|ONE|Ace"
INSTA360_WIFI_IP = "192.168.42.1"
INSTA360_WIFI_PORT = 6666
INSTA360_DEFAULT_PASSWORD = "88888888"

# Open GoPro status ids (camera/state → "status")
ST_BATTERY_BARS = "2"
ST_BUSY = "8"
ST_ENCODING = "10"
ST_PROGRESS_S = "13"
ST_CLIENTS = "31"
ST_SD_STATUS = "33"      # 0 ok · 1 full · 2 removed · 3 format error · 4 busy · 8 swapped
ST_REMAIN_PHOTOS = "34"
ST_REMAIN_VIDEO_S = "35"
ST_VIDEOS_TOTAL = "39"
ST_REMAIN_KB = "54"
ST_AP_ON = "69"
ST_BATTERY_PCT = "70"
ST_PRESET_GROUP = "89"   # 1000 video · 1001 photo · 1002 timelapse
ST_LENS_TYPE = "105"

# webcam status codes
WEBCAM_OFF, WEBCAM_IDLE, WEBCAM_HIGH, WEBCAM_LOW = 0, 1, 2, 3

# Camera settings the page can change: (id, label, group, {option: label}).
# Ids are the camera's own settings table (Open GoPro `setting?setting=<id>
# &option=<value>`; the Protune ids are the same table the older gpControl
# API used and still exist on HERO9+). A row is only shown when the camera
# reports that id in /gopro/camera/state "settings", so a model that lacks
# one simply does not offer it. HERO12+ needs Controls = Pro (175) before
# the manual ones take; HERO9–11 need Protune (10 / 21) on.
_WB = {0: "Auto", 4: "Native", 8: "2300 K", 9: "2800 K", 1: "3000 K", 10: "3200 K", 5: "4000 K",
       11: "4500 K", 6: "4800 K", 12: "5000 K", 2: "5500 K", 7: "6000 K", 3: "6500 K"}
_EV = {0: "+2", 1: "+1.5", 2: "+1", 3: "+0.5", 4: "0", 5: "−0.5", 6: "−1", 7: "−1.5", 8: "−2"}
_SHARP = {0: "High", 1: "Medium", 2: "Low"}
_COLOR = {0: "GoPro", 1: "Flat", 2: "Natural", 3: "Vibrant"}
_ONOFF = {0: "Off", 1: "On"}
_SHUTTER_V = {0: "Auto", 1: "1/24", 2: "1/25", 3: "1/30", 4: "1/48", 5: "1/50", 6: "1/60", 7: "1/90",
              8: "1/96", 9: "1/100", 10: "1/120", 11: "1/180", 12: "1/192", 13: "1/200", 14: "1/240",
              15: "1/360", 16: "1/400", 17: "1/480", 18: "1/960"}
_SHUTTER_P = {0: "Auto", 1: "1/125", 2: "1/250", 3: "1/500", 4: "1/1000", 5: "1/2000"}
GOPRO_SETTINGS = [
    # exposure / picture
    (175, "Controls",         "Exposure", {0: "Easy (auto)", 1: "Pro (manual)"}),
    (10,  "Protune",          "Exposure", _ONOFF),
    (74,  "ISO mode",         "Exposure", {0: "Max", 1: "Lock"}),
    (13,  "ISO max",          "Exposure", {8: "100", 7: "200", 2: "400", 4: "800", 1: "1600", 3: "3200", 0: "6400"}),
    (102, "ISO min",          "Exposure", {8: "100", 7: "200", 2: "400", 4: "800", 1: "1600", 3: "3200", 0: "6400"}),
    (73,  "Shutter",          "Exposure", _SHUTTER_V),
    (15,  "EV comp",          "Exposure", _EV),
    (11,  "White balance",    "Exposure", _WB),
    (12,  "Colour",           "Exposure", _COLOR),
    (14,  "Sharpness",        "Exposure", _SHARP),
    # video
    (2,   "Resolution",       "Video", {1: "4K", 4: "2.7K", 6: "2.7K 4:3", 7: "1440p", 9: "1080p", 12: "720p",
                                         18: "4K 4:3", 24: "5K", 25: "5K 4:3", 26: "5.3K 8:7", 27: "5.3K 4:3",
                                         28: "4K 8:7", 100: "5.3K", 107: "5.3K", 108: "4K", 109: "4K 4:3",
                                         110: "1080p", 111: "2.7K"}),
    (3,   "Frame rate",       "Video", {0: "240", 1: "120", 2: "100", 5: "60", 6: "50", 7: "48", 8: "30",
                                         9: "25", 10: "24", 13: "200"}),
    (121, "Lens",             "Video", {0: "Wide", 2: "Narrow", 3: "SuperView", 4: "Linear", 7: "Max SuperView",
                                         8: "Linear + horizon levelling", 9: "HyperView", 10: "Linear + horizon lock",
                                         11: "Max HyperView"}),
    (135, "HyperSmooth",      "Video", {0: "Off", 1: "On", 2: "High", 3: "Boost", 4: "Auto boost", 100: "Standard"}),
    (134, "Anti-flicker",     "Video", {2: "60 Hz", 3: "50 Hz"}),
    (184, "Profile",          "Video", {0: "Standard", 1: "HDR", 2: "Log", 101: "HLG HDR"}),
    (183, "Bit depth",        "Video", {0: "8-bit", 2: "10-bit"}),
    (182, "Bit rate",         "Video", {0: "Standard", 1: "High"}),
    # photo
    (21,  "Protune (photo)",  "Photo", _ONOFF),
    (24,  "ISO max (photo)",  "Photo", {3: "100", 2: "200", 1: "400", 0: "800", 4: "1600", 5: "3200"}),
    (75,  "ISO min (photo)",  "Photo", {3: "100", 2: "200", 1: "400", 0: "800", 4: "1600", 5: "3200"}),
    (97,  "Shutter (photo)",  "Photo", _SHUTTER_P),
    (26,  "EV comp (photo)",  "Photo", _EV),
    (22,  "White balance (photo)", "Photo", _WB),
    (23,  "Colour (photo)",   "Photo", _COLOR),
    (25,  "Sharpness (photo)", "Photo", _SHARP),
    (122, "Lens (photo)",     "Photo", {19: "Narrow", 100: "Max SuperView", 101: "Wide", 102: "Linear"}),
]
GOPRO_SETTINGS_BY_ID = {row[0]: row for row in GOPRO_SETTINGS}


def gopro_settings_model(state: dict) -> List[dict]:
    """Rows for the settings sheet: every table entry the camera actually
    reports, with its current value and the option list."""
    settings = state.get("settings") or {}
    out = []
    for sid, label, group, options in GOPRO_SETTINGS:
        if str(sid) not in settings:
            continue
        try:
            cur = int(settings[str(sid)])
        except (TypeError, ValueError):
            continue
        opts = [{"value": v, "label": lbl} for v, lbl in options.items()]
        if cur not in options:
            opts.append({"value": cur, "label": str(cur)})
        out.append({"id": sid, "label": label, "group": group, "value": cur,
                    "valueLabel": options.get(cur, str(cur)), "options": opts})
    return out


class CamError(Exception):
    pass


def human_size_kb(kb: float) -> str:
    n = float(kb) * 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def hms(seconds: float) -> str:
    s = int(max(0, seconds))
    h, m, sec = s // 3600, (s // 60) % 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def gopro_ip_from_serial(serial: str) -> str:
    """172.2X.1YZ.51 from the camera's serial number (last three digits XYZ)."""
    digits = re.sub(r"\D", "", serial or "")
    if len(digits) < 3:
        return ""
    x, y, z = digits[-3], digits[-2], digits[-1]
    return f"172.2{x}.1{y}{z}.51"


# ── USB ethernet (GoPro Connect) ──────────────────────────────────────────
class UsbNetLink:
    """Finds and brings up the GoPro's USB network. All methods block."""

    @staticmethod
    def candidates() -> List[Tuple[str, bool]]:
        """USB network interfaces as (name, is_gopro_by_vendor). A GoPro is
        vendor 2672; any other usb*/enx* only counts once it already carries
        a 172.2X address — the app never reconfigures a random USB adapter."""
        try:
            names = sorted(os.listdir("/sys/class/net"))
        except OSError:
            return []
        out = []
        for n in names:
            if not (n.startswith("usb") or n.startswith("enx")):
                continue
            vid = ""
            for rel in ("device/../idVendor", "device/../../idVendor"):
                try:
                    with open(os.path.join("/sys/class/net", n, rel)) as f:
                        vid = f.read().strip().lower()
                    break
                except OSError:
                    continue
            out.append((n, vid == GOPRO_USB_VENDOR))
        out.sort(key=lambda t: not t[1])
        return out

    @classmethod
    def find(cls) -> Tuple[str, str]:
        """(iface, our_ip) of the GoPro's USB network if it is there: a
        vendor-2672 device (brought up if needed) or any USB adapter that
        already sits on 172.2X. ("", "") otherwise."""
        for iface, is_gopro in cls.candidates():
            state, ip = cls.addr(iface)
            if ip.startswith("172.2"):
                return iface, ip
            if is_gopro:
                ip, err = cls.ensure_up(iface)
                if ip:
                    return iface, ip
                if err:
                    print(f"[actioncam] usb {iface}: {err}", flush=True)
        return "", ""

    @staticmethod
    def addr(iface: str) -> Tuple[str, str]:
        """(state, first IPv4) of `iface` from nmcli."""
        rc, out, _ = _run(["nmcli", "-t", "-f", "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS",
                           "dev", "show", iface], timeout=8)
        state, ip = "", ""
        if rc != 0:
            return state, ip
        for line in out.splitlines():
            parts = _split_terse(line)
            if len(parts) < 2:
                continue
            key, val = parts[0].strip(), ":".join(parts[1:]).strip()
            if key == "GENERAL.STATE":
                state = val
            elif key.startswith("IP4.ADDRESS") and not ip:
                ip = val.split("/")[0]
        return state, ip

    @classmethod
    def ensure_up(cls, iface: str) -> Tuple[str, str]:
        """Make sure NM has our never-default profile on `iface` and it is up.
        Returns (our_ip, error)."""
        state, ip = cls.addr(iface)
        if state.startswith("100") and ip.startswith("172.2"):
            return ip, ""
        rc, out, _ = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], timeout=8)
        have = any(_split_terse(l)[0] == GOPRO_USB_PROFILE for l in out.splitlines()) if rc == 0 else False
        flags = ["ipv4.method", "auto", "ipv4.never-default", "yes", "ipv4.route-metric", "700",
                 "ipv6.method", "disabled", "connection.autoconnect", "yes",
                 "connection.interface-name", iface]
        if have:
            _run(["nmcli", "connection", "modify", GOPRO_USB_PROFILE] + flags, timeout=15)
        else:
            rc, _, err = _run(["nmcli", "connection", "add", "type", "ethernet", "con-name",
                               GOPRO_USB_PROFILE, "ifname", iface] + flags, timeout=15)
            if rc != 0:
                return "", err.strip() or "could not create the USB profile"
        rc, _, err = _run(["nmcli", "connection", "up", "id", GOPRO_USB_PROFILE], timeout=40)
        if rc != 0:
            return "", (err.strip().splitlines() or ["could not bring the USB link up"])[-1]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            state, ip = cls.addr(iface)
            if ip:
                return ip, ""
            time.sleep(0.5)
        return "", "no address from the camera"

    @staticmethod
    def camera_ip(our_ip: str) -> str:
        parts = our_ip.split(".")
        return ".".join(parts[:3] + ["51"]) if len(parts) == 4 else ""


# ── UVC (Insta360 Webcam mode) ────────────────────────────────────────────
class UvcLink:
    @staticmethod
    def find(match: str = "insta360") -> Tuple[str, str]:
        """(/dev/videoN, name) of the first capture node whose UVC name
        contains `match` (case-insensitive; "" = any camera). Only index-0
        nodes: a UVC camera also exposes a metadata node that cannot stream."""
        root = "/sys/class/video4linux"
        try:
            nodes = sorted(os.listdir(root), key=lambda n: int(re.sub(r"\D", "", n) or 0))
        except OSError:
            return "", ""
        needle = (match or "").lower()
        for n in nodes:
            try:
                with open(os.path.join(root, n, "name")) as f:
                    name = f.read().strip()
                with open(os.path.join(root, n, "index")) as f:
                    idx = f.read().strip()
            except OSError:
                continue
            if idx not in ("", "0"):
                continue
            if needle and needle not in name.lower():
                continue
            dev = "/dev/" + n
            if os.path.exists(dev):
                return dev, name
        return "", ""


# ── Open GoPro HTTP ───────────────────────────────────────────────────────
class GoProHttp:
    """Open GoPro 2.0 over HTTP. `base` is http://<ip>:8080."""

    def __init__(self, ip: str, timeout: float = 3.0) -> None:
        self.ip = ip
        self.base = f"http://{ip}:{GOPRO_HTTP_PORT}"
        self.timeout = timeout
        self.last_url = ""

    # The camera's HTTP server goes away for a moment around mode changes
    # (leaving webcam mode, applying a setting): the connect is *refused*,
    # not slow. Every call here is an idempotent GET, so a refused/reset
    # connect is retried a few times before it counts as a failure.
    RETRIES = 4
    RETRY_GAP_S = 0.6

    def _get(self, path: str, params: Optional[Dict[str, object]] = None,
             timeout: Optional[float] = None) -> object:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: str(v) for k, v in params.items()})
        self.last_url = url
        req = urllib.request.Request(url, headers={"User-Agent": "LaLaDash"})
        body = b""
        for attempt in range(self.RETRIES):
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                    body = r.read()
                break
            except urllib.error.HTTPError as e:
                raise CamError(f"HTTP {e.code} on {path}")
            except (urllib.error.URLError, socket.timeout, OSError) as e:
                reason = getattr(e, "reason", e)
                text = str(reason).lower()
                transient = any(k in text for k in ("refused", "reset", "unreachable", "timed out"))
                if transient and attempt < self.RETRIES - 1:
                    time.sleep(self.RETRY_GAP_S)
                    continue
                raise CamError(f"{reason}")
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return {"raw": body[:200]}

    # -- control ----------------------------------------------------------
    def state(self) -> dict:
        d = self._get("/gopro/camera/state")
        return d if isinstance(d, dict) else {}

    def keep_alive(self) -> None:
        self._get("/gopro/camera/keep_alive")

    def wired_usb(self, on: bool) -> None:
        # HERO11+ only; older firmware answers 404 — harmless.
        try:
            self._get("/gopro/camera/control/wired_usb", {"p": 1 if on else 0})
        except CamError as e:
            if "404" not in str(e):
                raise

    def shutter(self, on: bool) -> None:
        self._get(f"/gopro/camera/shutter/{'start' if on else 'stop'}", timeout=6.0)

    def set_preset_group(self, group: int) -> None:
        self._get("/gopro/camera/presets/set_group", {"id": group})

    def set_setting(self, setting_id: int, option: int) -> None:
        self._get("/gopro/camera/setting", {"setting": int(setting_id), "option": int(option)},
                  timeout=6.0)

    def stream_start(self, port: int = GOPRO_STREAM_PORT) -> None:
        self._get("/gopro/camera/stream/start", {"port": port}, timeout=6.0)

    def stream_stop(self) -> None:
        self._get("/gopro/camera/stream/stop")

    def webcam_start(self, res: int = 12, fov: int = 0, port: int = GOPRO_STREAM_PORT) -> None:
        self._get("/gopro/webcam/start", {"res": res, "fov": fov, "port": port, "protocol": "TS"},
                  timeout=8.0)


    def webcam_stop(self) -> None:
        self._get("/gopro/webcam/stop")

    def webcam_exit(self) -> None:
        self._get("/gopro/webcam/exit")


    def media_list(self, limit: int = 5) -> List[dict]:
        """Newest `limit` files: [{name, dir, size, time}], newest first."""
        d = self._get("/gopro/media/list", timeout=8.0)
        rows: List[dict] = []
        for folder in (d.get("media") or []) if isinstance(d, dict) else []:
            dname = str(folder.get("d", ""))
            for f in folder.get("fs") or []:
                try:
                    size = int(f.get("s", 0))
                except (TypeError, ValueError):
                    size = 0
                try:
                    cre = int(f.get("cre", 0))
                except (TypeError, ValueError):
                    cre = 0
                rows.append({"name": str(f.get("n", "")), "dir": dname, "size": size, "cre": cre})
        rows.sort(key=lambda r: (r["cre"], r["name"]), reverse=True)
        out = []
        for r in rows[:limit]:
            out.append({
                "name": r["name"], "dir": r["dir"],
                "size": human_size_kb(r["size"] / 1024.0) if r["size"] else "",
                "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["cre"])) if r["cre"] else "",
            })
        return out


def gopro_summary(state: dict) -> dict:
    """The few numbers the page shows, from /gopro/camera/state."""
    st = state.get("status") or {}
    def _i(key, default=-1):
        try:
            return int(st.get(key, default))
        except (TypeError, ValueError):
            return default
    pct = _i(ST_BATTERY_PCT)
    if pct < 0:
        bars = _i(ST_BATTERY_BARS)
        pct = {0: 5, 1: 25, 2: 60, 3: 100}.get(bars, -1)
    sd_status = _i(ST_SD_STATUS)
    remain_kb = _i(ST_REMAIN_KB)
    sd_text = {0: "", 1: "SD full", 2: "No SD card", 3: "SD format error", 4: "SD busy"}.get(sd_status, "")
    if remain_kb >= 0:
        sd_text = (sd_text + " · " if sd_text else "") + f"{human_size_kb(remain_kb)} free"
    # Which id carries the preset group differs by firmware: the HERO9
    # reports it in 96 (89 is something else there), newer ones in 89.
    group = _i("96")
    if group not in (1000, 1001, 1002):
        group = _i(ST_PRESET_GROUP)
    mode = {1000: "Video", 1001: "Photo", 1002: "Timelapse"}.get(group, "")
    return {
        "battery": pct,
        "encoding": _i(ST_ENCODING) == 1,
        "busy": _i(ST_BUSY) == 1,
        "progress_s": _i(ST_PROGRESS_S, 0),
        "sd_text": sd_text,
        "sd_ok": sd_status == 0,
        "remain_video_s": _i(ST_REMAIN_VIDEO_S),
        "videos_total": _i(ST_VIDEOS_TOTAL),
        "mode": mode,
        "group": group,
        "ap_on": _i(ST_AP_ON) == 1,
    }


# ── GoPro BLE (BlueZ over pydbus) ─────────────────────────────────────────
class GoProBle:
    """Turns the camera's Wi-Fi AP on and reads its SSID/password over BLE.

    Open GoPro: service FEA6, command characteristic
    b5f90072-…, "AP control" = 03 17 01 01 (on) / 03 17 01 00 (off);
    WiFi AP SSID/password are readable characteristics b5f90002-… /
    b5f90003-…. First-time pairing needs the camera in
    "Preferences › Connections › Connect Device › GoPro Quik App".
    """

    CMD_CHAR = "b5f90072-aa8d-11e3-9046-0002a5d5c51b"
    SSID_CHAR = "b5f90002-aa8d-11e3-9046-0002a5d5c51b"
    PASS_CHAR = "b5f90003-aa8d-11e3-9046-0002a5d5c51b"
    AP_ON = [0x03, 0x17, 0x01, 0x01]
    AP_OFF = [0x03, 0x17, 0x01, 0x00]

    def __init__(self, mac: str = "") -> None:
        self.mac = (mac or "").upper()

    @staticmethod
    def _bus():
        import pydbus
        return pydbus.SystemBus()

    @staticmethod
    def _objects(bus) -> dict:
        return bus.get("org.bluez", "/")["org.freedesktop.DBus.ObjectManager"].GetManagedObjects()

    def _device_path(self, bus) -> str:
        for path, ifaces in self._objects(bus).items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev:
                continue
            if self.mac and dev.get("Address", "").upper() == self.mac:
                return path
            if not self.mac and str(dev.get("Name", "")).startswith("GoPro") and dev.get("Paired"):
                self.mac = dev.get("Address", "").upper()
                return path
        return ""

    def pair(self, wait_s: float = 25.0, on_note: Optional[Callable[[str], None]] = None) -> str:
        """Discover a GoPro in pairing mode, pair and trust it. Returns the MAC."""
        bus = self._bus()
        adapter = bus.get("org.bluez", "/org/bluez/hci0")
        try:
            adapter.SetDiscoveryFilter({"Transport": ("s", "le")})
        except Exception:
            pass
        try:
            adapter.StartDiscovery()
        except Exception as exc:
            if "InProgress" not in str(exc):
                raise CamError(f"Bluetooth discovery failed: {exc}")
        if on_note:
            on_note("Looking for a GoPro in pairing mode…")
        path = ""
        deadline = time.monotonic() + wait_s
        try:
            while time.monotonic() < deadline and not path:
                for p, ifaces in self._objects(bus).items():
                    dev = ifaces.get("org.bluez.Device1")
                    if dev and str(dev.get("Name", "")).startswith("GoPro"):
                        path = p
                        self.mac = dev.get("Address", "").upper()
                        break
                if not path:
                    time.sleep(1.0)
        finally:
            try:
                adapter.StopDiscovery()
            except Exception:
                pass
        if not path:
            raise CamError("no GoPro found — is it in Connect Device › GoPro Quik App?")
        dev = bus.get("org.bluez", path)
        if on_note:
            on_note(f"Pairing with {dev.Name}…")
        if not dev.Paired:
            try:
                dev.Pair()
            except Exception as exc:
                raise CamError(f"pairing failed: {exc}")
        dev.Trusted = True
        return self.mac

    def _with_device(self, fn, wait_s: float = 12.0):
        bus = self._bus()
        path = self._device_path(bus)
        if not path:
            raise CamError("GoPro not paired over Bluetooth")
        dev = bus.get("org.bluez", path)
        connected_here = False
        if not dev.Connected:
            try:
                dev.Connect()
                connected_here = True
            except Exception as exc:
                raise CamError(f"Bluetooth connect failed: {exc}")
        deadline = time.monotonic() + wait_s
        while not dev.ServicesResolved and time.monotonic() < deadline:
            time.sleep(0.3)
        if not dev.ServicesResolved:
            raise CamError("Bluetooth services not resolved (camera off?)")
        try:
            return fn(bus, path)
        finally:
            if connected_here:
                try:
                    dev.Disconnect()
                except Exception:
                    pass

    def _char(self, bus, dev_path: str, uuid: str):
        for path, ifaces in self._objects(bus).items():
            ch = ifaces.get("org.bluez.GattCharacteristic1")
            if ch and path.startswith(dev_path) and ch.get("UUID", "").lower() == uuid:
                return bus.get("org.bluez", path)
        raise CamError(f"characteristic {uuid[:8]} not found")

    def enable_ap(self, on: bool = True) -> None:
        def _do(bus, path):
            ch = self._char(bus, path, self.CMD_CHAR)
            ch.WriteValue(self.AP_ON if on else self.AP_OFF, {})
        self._with_device(_do)

    def read_ap(self) -> Tuple[str, str]:
        """(ssid, password) of the camera's AP."""
        def _do(bus, path):
            ssid = bytes(self._char(bus, path, self.SSID_CHAR).ReadValue({})).decode("utf-8", "replace")
            pw = bytes(self._char(bus, path, self.PASS_CHAR).ReadValue({})).decode("utf-8", "replace")
            return ssid.strip("\x00"), pw.strip("\x00")
        return self._with_device(_do)


# ── Insta360 over Wi-Fi (TCP 6666) ────────────────────────────────────────
class Insta360Wifi:
    """Insta360 Wi-Fi protocol client — community-reverse-engineered.

    Wire format (RigacciOrg/insta360-wifi-api, ONE RS; the GO Ultra toolkit
    agrees): TCP 6666 on 192.168.42.1. Every packet is `<u32 LE length
    including itself>` + payload. A command payload is a 12-byte header
    `04 00 00 <code u16 LE> 02 <seq u24 LE> 80 00 00` followed by a protobuf
    message; the answer carries the same header with a response code (200
    ok / 500 error) and the same sequence. Two raw packets: sync
    `06 00 00 syNceNdinS` right after connect and keep-alive `05 00 00`
    every 2 s. Live video, once started, arrives on the same socket as
    packets prefixed `01 00 00` (raw H.265/H.264 Annex-B, GO Ultra).

    Commands whose protobuf body is empty or all-default are sent as-is
    (stop capture, capture status, stop live, take picture NORMAL, start
    capture NORMAL — the enum defaults in the published definitions);
    anything needing real fields (get options for battery/storage, start
    live stream with resolutions) needs bytes captured from the app for the
    user's model, read from `Data/insta360_messages.json` as
    `{"get_options": "<hex payload>", "live_start": "<hex>"}`. Until then
    those report "not mapped for this model".
    """

    CODE_START_LIVE = 1
    CODE_STOP_LIVE = 2
    CODE_TAKE_PICTURE = 3
    CODE_START_CAPTURE = 4
    CODE_STOP_CAPTURE = 5
    CODE_SET_OPTIONS = 7
    CODE_GET_OPTIONS = 8
    CODE_GET_FILE_LIST = 13
    CODE_CAPTURE_STATUS = 15
    RESP_OK = 200
    PKT_SYNC = b"\x06\x00\x00syNceNdinS"
    PKT_KEEPALIVE = b"\x05\x00\x00"
    HDR_CMD = b"\x04\x00\x00"
    VIDEO_PREFIX = b"\x01\x00\x00"
    # empty / all-default protobuf bodies
    BODIES = {
        "record_start": (CODE_START_CAPTURE, b""),
        "record_stop": (CODE_STOP_CAPTURE, b""),
        "photo": (CODE_TAKE_PICTURE, b""),
        "capture_status": (CODE_CAPTURE_STATUS, b""),
        "live_stop": (CODE_STOP_LIVE, b""),
    }

    def __init__(self, host: str = INSTA360_WIFI_IP, port: int = INSTA360_WIFI_PORT,
                 on_video: Optional[Callable[[bytes], None]] = None) -> None:
        self.host = host
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._on_video = on_video
        self._seq = 0
        self._buf = b""
        self.last_code: Optional[int] = None
        self.messages: Dict[str, Tuple[int, bytes]] = dict(self.BODIES)
        self._load_messages()

    def _load_messages(self) -> None:
        try:
            from preferences import BASE_DIR
            path = os.path.join(BASE_DIR, "Data", "insta360_messages.json")
            with open(path) as f:
                raw = json.load(f)
        except Exception:
            return
        codes = {"get_options": self.CODE_GET_OPTIONS, "live_start": self.CODE_START_LIVE,
                 "set_options": self.CODE_SET_OPTIONS, "file_list": self.CODE_GET_FILE_LIST,
                 "record_start": self.CODE_START_CAPTURE, "photo": self.CODE_TAKE_PICTURE}
        for k, v in raw.items():
            if isinstance(v, str) and k in codes:
                try:
                    self.messages[k] = (codes[k], bytes.fromhex(v))
                except ValueError:
                    pass

    @property
    def mapped(self) -> bool:
        return "get_options" in self.messages

    def has(self, kind: str) -> bool:
        return kind in self.messages

    # -- transport --------------------------------------------------------
    def connect(self, timeout: float = 4.0) -> None:
        self.close()
        s = socket.create_connection((self.host, self.port), timeout=timeout)
        s.settimeout(1.0)
        self._sock = s
        self._buf = b""
        self._raw(self.PKT_SYNC)
        self._raw(self.PKT_KEEPALIVE)

    def close(self) -> None:
        s = self._sock
        self._sock = None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def _raw(self, payload: bytes) -> None:
        if self._sock is None:
            raise CamError("not connected")
        pkt = (len(payload) + 4).to_bytes(4, "little") + payload
        try:
            self._sock.sendall(pkt)
        except OSError as exc:
            self.close()
            raise CamError(f"send failed: {exc}")

    def keepalive(self) -> None:
        self._raw(self.PKT_KEEPALIVE)

    def send(self, kind: str) -> int:
        """Send a command by name; returns its sequence number."""
        if kind not in self.messages:
            raise CamError(f"{kind} not mapped for this model")
        code, body = self.messages[kind]
        self._seq = (self._seq + 1) & 0xFFFFFF
        hdr = (self.HDR_CMD + code.to_bytes(2, "little") + b"\x02"
               + self._seq.to_bytes(3, "little") + b"\x80\x00\x00")
        self._raw(hdr + body)
        return self._seq

    def pump(self, max_s: float = 0.5) -> List[Tuple[int, int, bytes]]:
        """Read for up to `max_s`. Video packets go to `on_video`; command
        answers come back as (response_code, seq, body)."""
        if self._sock is None:
            return []
        out: List[Tuple[int, int, bytes]] = []
        deadline = time.monotonic() + max_s
        while time.monotonic() < deadline:
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                break
            except OSError as exc:
                self.close()
                raise CamError(f"link lost: {exc}")
            if not chunk:
                self.close()
                raise CamError("camera closed the connection")
            self._buf += chunk
            while len(self._buf) >= 4:
                length = int.from_bytes(self._buf[:4], "little")
                if length < 4 or length > 8_000_000:
                    self._buf = b""
                    break
                if len(self._buf) < length:
                    break
                pkt, self._buf = self._buf[4:length], self._buf[length:]
                if pkt.startswith(self.VIDEO_PREFIX):
                    if self._on_video:
                        self._on_video(pkt[len(self.VIDEO_PREFIX):])
                elif pkt.startswith(self.HDR_CMD) and len(pkt) >= 12:
                    code = int.from_bytes(pkt[3:5], "little")
                    seq = int.from_bytes(pkt[6:9], "little")
                    self.last_code = code
                    out.append((code, seq, pkt[12:]))
        return out

    def call(self, kind: str, wait_s: float = 3.0) -> bytes:
        """send + wait for the matching answer; raises on a non-200 code."""
        seq = self.send(kind)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            for code, rseq, body in self.pump(0.3):
                if rseq == seq:
                    if code != self.RESP_OK:
                        raise CamError(f"{kind}: camera answered {code}")
                    return body
        raise CamError(f"{kind}: no answer")


# ── selftest ──────────────────────────────────────────────────────────────
def _selftest() -> int:
    assert gopro_ip_from_serial("C3461324500123") == "172.21.123.51"
    assert UsbNetLink.camera_ip("172.23.138.52") == "172.23.138.51"
    s = gopro_summary({"status": {"70": 87, "10": 1, "8": 0, "33": 0, "54": 12345678, "89": 1000, "13": 75}})
    assert s["battery"] == 87 and s["encoding"] and s["mode"] == "Video" and "GB free" in s["sd_text"]
    assert hms(75) == "1:15" and hms(3725) == "1:02:05"
    print("actioncam selftest ok")
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "uvc":
        print(UvcLink.find(sys.argv[2] if len(sys.argv) > 2 else ""))
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "usb":
        print(UsbNetLink.candidates())
        sys.exit(0)
    sys.exit(_selftest())
