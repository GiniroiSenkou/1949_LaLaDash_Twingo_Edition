"""`ActionCam` — a GoPro or an Insta360 over USB or Wi-Fi, for QML.

Built on `CamBridgeBase` (bridges/cam_common.py). Four combinations, one
bridge (`[ActionCam] kind` × `link`):

    gopro    × usb   Open GoPro over the camera's USB network (UsbNetLink)
    gopro    × wifi  Open GoPro over its AP (HotspotLink; BLE wakes the AP)
    insta360 × usb   the camera in Webcam mode = a UVC device, picture only
    insta360 × wifi  TCP 6666 protobuf — transport only until the model's
                     messages are captured (see actioncam.Insta360Wifi)

The base's rules apply: nothing exists while disabled; the link is watched
(a /sys listing + one nmcli call for USB, the NM link state for Wi-Fi) only
while `autostart` and the car is awake, or the page is open; status every
30 s (5 s with the page showing); the stream — MPEG-TS over UDP from the
GoPro, v4l2 from a UVC camera — only while the page is on screen or the
reverse-gear overlay is up. Webcam mode is exited when the stream stops so
the GoPro can record again.
"""
from __future__ import annotations

import time
from typing import List, Optional

from PyQt6.QtCore import pyqtProperty, pyqtSignal, pyqtSlot

from Modules.Runtime.actioncam import (
    GOPRO_SSID_PREFIX, GOPRO_STREAM_PORT, GOPRO_WIFI_IP, INSTA360_DEFAULT_PASSWORD,
    INSTA360_SSID_PREFIX, INSTA360_WIFI_IP, CamError, GoProBle, GoProHttp, Insta360Wifi,
    GOPRO_SETTINGS_BY_ID, UsbNetLink, UvcLink, gopro_settings_model, gopro_summary, hms,
)
from Modules.Runtime.dashcam import HotspotLink
from bridges.cam_common import CamBridgeBase, cam_props, frame_prop

KINDS = ("gopro", "insta360")
LINKS = ("usb", "wifi")
LABELS = {"gopro": "GoPro", "insta360": "Insta360"}
WIFI_PROFILE = {"gopro": "LaLaDash-GoPro", "insta360": "LaLaDash-Insta360"}
WIFI_PREFIX = {"gopro": GOPRO_SSID_PREFIX, "insta360": INSTA360_SSID_PREFIX}
BLE_WAKE_MIN_S = 60


class ActionCamBridge(CamBridgeBase):
    SECTION = "ActionCam"
    SERVICE = "actioncam"
    PAGE_INDEX = 8
    LINK_WATCH_S = 10
    LINKED_STATES = ("online",)
    STREAM_STATES = ("online",)

    changed = pyqtSignal()
    frameChanged = pyqtSignal()
    (enabled, autostart, liveOnOpen, state, statusText, lastError, busy, linked,
     pageVisible, streamActive, streamState, streamError, streamSource) = cam_props(changed)
    frameRev = frame_prop(frameChanged)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._link: Optional[HotspotLink] = None     # also the worker for USB
        self._gopro: Optional[GoProHttp] = None
        self._insta: Optional[Insta360Wifi] = None
        self._cam_ip = ""
        self._usb_iface = ""
        self._uvc_dev = ""
        self._uvc_name = ""
        self._battery = -1
        self._sd_text = ""
        self._sd_ok = True
        self._recording = -1
        self._progress_s = 0
        self._mode = ""
        self._clips: List[dict] = []
        self._detected: List[dict] = []
        self._controllable = False
        self._wired_done = False
        self._poll_no = 0
        self._reverse = False
        self._ble_note = ""
        self._ble_last_wake = 0.0
        self._cam_stream_on = False      # GoPro told to send (webcam/preview)
        self._settings_model: List[dict] = []
        self._miss = 0                   # consecutive failed status polls

    # ── settings-backed properties ────────────────────────────────────────
    @pyqtProperty(str, notify=changed)
    def kind(self) -> str:
        k = self._get("kind", "gopro").lower()
        return k if k in KINDS else "gopro"

    @pyqtProperty(str, notify=changed)
    def link(self) -> str:
        l = self._get("link", "usb").lower()
        return l if l in LINKS else "usb"

    @pyqtProperty(str, notify=changed)
    def label(self) -> str:
        return LABELS[self.kind]

    @pyqtProperty(str, notify=changed)
    def iface(self) -> str:
        return self._get("iface", "wlan1") or "wlan1"

    @pyqtProperty(str, notify=changed)
    def ssidSetting(self) -> str:
        return self._get("ssid", "")

    @pyqtProperty(str, notify=changed)
    def password(self) -> str:
        default = INSTA360_DEFAULT_PASSWORD if self.kind == "insta360" else ""
        return self._get("password", default) or default

    @pyqtProperty(str, notify=changed)
    def bleMac(self) -> str:
        return self._get("ble_mac", "")

    @pyqtProperty(str, notify=changed)
    def stream(self) -> str:
        s = self._get("stream", "webcam").lower()
        return s if s in ("preview", "webcam") else "webcam"

    @pyqtProperty(int, notify=changed)
    def webcamRes(self) -> int:
        try:
            return int(self._get("webcam_res", "12"))
        except ValueError:
            return 12

    @pyqtProperty(int, notify=changed)
    def webcamFov(self) -> int:
        try:
            return int(self._get("webcam_fov", "0"))
        except ValueError:
            return 0

    @pyqtProperty(str, notify=changed)
    def uvcMatch(self) -> str:
        return self._get("uvc_match", "Insta360")

    @pyqtProperty(bool, notify=changed)
    def reverseView(self) -> bool:
        return self._getbool("reverse_view", False)

    def _restart_after_setting(self) -> None:
        """A kind/link change invalidates everything found so far."""
        self._stop_stream()
        self._gopro = None
        self._insta = None
        self._cam_ip = ""
        self._usb_iface = ""
        self._uvc_dev = ""
        self._controllable = False
        self._wired_done = False
        if self._link is not None:
            self._link.stop()
            self._link = None
        if self.enabled:
            self._set_state("off", "")
            if self.autostart or self._page_visible:
                self._ensure_worker()
                self._submit(self._job_status)
        self._rearm()
        self.changed.emit()

    @pyqtSlot(str)
    def setKind(self, kind: str) -> None:
        kind = (kind or "").lower()
        if kind not in KINDS:
            return
        self._set("kind", kind)
        self._restart_after_setting()

    @pyqtSlot(str)
    def setLink(self, link: str) -> None:
        link = (link or "").lower()
        if link not in LINKS:
            return
        self._set("link", link)
        self._restart_after_setting()

    @pyqtSlot(str)
    def setIface(self, iface: str) -> None:
        self._set("iface", (iface or "").strip() or "wlan1")
        if self._link is not None:
            self._link.set_iface(self.iface)
        self.changed.emit()

    @pyqtSlot(str)
    def setSsid(self, ssid: str) -> None:
        self._set("ssid", (ssid or "").strip())
        self.changed.emit()

    @pyqtSlot(str)
    def setPassword(self, pw: str) -> None:
        self._set("password", (pw or "").strip())
        self.changed.emit()

    @pyqtSlot(str)
    def setStream(self, mode: str) -> None:
        mode = (mode or "").lower()
        if mode in ("preview", "webcam"):
            self._set("stream", mode)
            self._stop_stream()
            self.changed.emit()

    @pyqtSlot(int)
    def setWebcamRes(self, res: int) -> None:
        self._set("webcam_res", str(int(res)))
        self.changed.emit()

    @pyqtSlot(int)
    def setWebcamFov(self, fov: int) -> None:
        self._set("webcam_fov", str(int(fov)))
        self.changed.emit()

    @pyqtSlot(bool)
    def setReverseView(self, on: bool) -> None:
        self._set("reverse_view", "true" if on else "false")
        if not on and self._reverse and not self._page_visible:
            self._view_wanted = False
            self._stop_stream()
        self.changed.emit()

    @pyqtSlot()
    def forgetBle(self) -> None:
        self._set("ble_mac", "")
        self.changed.emit()

    # ── live state properties ─────────────────────────────────────────────
    @pyqtProperty(str, notify=changed)
    def camIp(self) -> str:
        return self._cam_ip

    @pyqtProperty(str, notify=changed)
    def linkText(self) -> str:
        if self.link == "usb":
            if self.kind == "insta360":
                return f"USB · {self._uvc_dev}" if self._uvc_dev else "USB"
            return f"USB · {self._usb_iface}" if self._usb_iface else "USB"
        return f"Wi-Fi · {self.iface}"

    @pyqtProperty(str, notify=changed)
    def ssid(self) -> str:
        if self.ssidSetting:
            return self.ssidSetting
        return self._detected[0]["ssid"] if self._detected else ""

    @pyqtProperty(list, notify=changed)
    def detectedNetworks(self) -> list:
        return list(self._detected)

    @pyqtProperty(int, notify=changed)
    def battery(self) -> int:
        return self._battery

    @pyqtProperty(str, notify=changed)
    def sdText(self) -> str:
        return self._sd_text

    @pyqtProperty(bool, notify=changed)
    def sdOk(self) -> bool:
        return self._sd_ok

    @pyqtProperty(int, notify=changed)
    def recording(self) -> int:
        return self._recording

    @pyqtProperty(str, notify=changed)
    def recordingText(self) -> str:
        if self._recording == 1:
            return "REC " + hms(self._progress_s) if self._progress_s else "REC"
        return ""

    @pyqtProperty(str, notify=changed)
    def mode(self) -> str:
        return self._mode

    @pyqtProperty(list, notify=changed)
    def clips(self) -> list:
        return list(self._clips)

    @pyqtProperty(bool, notify=changed)
    def controllable(self) -> bool:
        return self._controllable

    @pyqtProperty(str, notify=changed)
    def uvcName(self) -> str:
        return self._uvc_name

    @pyqtProperty(str, notify=changed)
    def bleNote(self) -> str:
        return self._ble_note

    @pyqtProperty(bool, notify=changed)
    def reverseActive(self) -> bool:
        return self._reverse

    @pyqtProperty(list, notify=changed)
    def settingsModel(self) -> list:
        """Camera settings the sheet can change (GoPro only): the entries of
        `GOPRO_SETTINGS` this camera reports, with current value + options."""
        return list(self._settings_model)

    @pyqtProperty(bool, notify=changed)
    def settingsAvailable(self) -> bool:
        return self.kind == "gopro" and self._state == "online" and bool(self._settings_model)

    @pyqtSlot(int, int)
    def setSetting(self, setting_id: int, option: int) -> None:
        if self.kind != "gopro" or self._gopro is None or self._state != "online":
            return
        sid, opt = int(setting_id), int(option)
        # Optimistic: the sheet shows the new value at once; the next status
        # poll corrects it if the camera refused.
        for row in self._settings_model:
            if row["id"] == sid:
                row["value"] = opt
                row["valueLabel"] = next((o["label"] for o in row["options"] if o["value"] == opt), str(opt))
        self.changed.emit()
        self._submit(lambda: self._job_set_setting(sid, opt))

    # ── connect / disconnect notification ─────────────────────────────────
    def _link_alert(self, connected: bool) -> None:
        insta = self.kind == "insta360"
        key = "insta360_link" if insta else "gopro_link"
        icon = "insta360.png" if insta else "gopro.png"
        label = self.label
        if connected:
            self._alerts.setAlert(key, f"{label} connected", icon, "info", True)
        else:
            self._alerts.setAlert(key, f"{label} disconnected", icon, "warning", True)

    # ── base hooks ────────────────────────────────────────────────────────
    def _worker(self):
        return self._link

    def _ensure_worker(self) -> None:
        if self._link is None:
            self._link = HotspotLink(self.iface, WIFI_PREFIX[self.kind], WIFI_PROFILE[self.kind])

    def _stop_worker(self) -> None:
        if self._link is not None:
            self._link.stop()
            self._link = None
        self._gopro = None
        if self._insta is not None:
            self._insta.close()
            self._insta = None

    def _on_car_awake(self, awake: bool) -> None:
        if awake and self.enabled and self.autostart:
            self._ensure_worker()
            if self._state in ("off", "scanning", "error"):
                self._set_state("scanning")
                self._submit(self._job_status)

    def _on_autostart_boot(self) -> None:
        self._ensure_worker()
        if self.link == "wifi":
            self._submit(self._job_profile_flags)

    def _on_enabled(self, on: bool) -> None:
        if on and self.autostart:
            self._ensure_worker()
            if self.link == "wifi":
                self._submit(self._job_profile_flags)

    def _on_autostart(self, on: bool) -> None:
        self._ensure_worker()
        if self.link == "wifi":
            self._submit(self._job_profile_flags)
        if on and self._car_awake:
            self._submit(self._job_status)

    def _on_page_opened(self) -> None:
        if self._state in ("off", "error"):
            self._set_state("scanning")

    def _watch_wanted(self) -> bool:
        return super()._watch_wanted() or (self._reverse and self.reverseView)

    def _showing(self) -> bool:
        if self._reverse and self.reverseView:
            return True
        return super()._showing()

    def _stream_source(self) -> str:
        if self.kind == "insta360" and self.link == "usb":
            return f"v4l2://{self._uvc_dev}" if self._uvc_dev else ""
        if self.kind == "gopro":
            if self._gopro is None:
                return ""
            self._submit(self._job_cam_stream_on)
            return ""
        # insta360 over wifi: needs the model's live-start message captured
        if self._insta is not None and self._insta.has("live_start"):
            self._submit(self._job_insta_live)
        else:
            self._stream_note("error", "Live over Wi-Fi is not mapped for this model — use Webcam mode over USB")
        return ""

    def _on_stream_stopped(self) -> None:
        if self._link is None:
            return
        if self.kind == "gopro" and self._cam_stream_on:
            self._submit(self._job_cam_stream_off)
        elif self.kind == "insta360" and self.link == "wifi" and self._cam_stream_on:
            self._submit(self._job_insta_live_off)

    # ── commands ──────────────────────────────────────────────────────────
    @pyqtSlot()
    def connectNow(self) -> None:
        if not self.enabled:
            return
        self._ensure_worker()
        self._set_state("joining" if self.link == "wifi" else "scanning",
                        "Joining the camera's Wi-Fi…" if self.link == "wifi" else "Looking for the camera…")
        self._submit(self._job_connect)

    @pyqtSlot()
    def disconnectNow(self) -> None:
        self._stop_stream()
        if self._link is None:
            return
        self._submit(self._job_disconnect)

    @pyqtSlot()
    def detect(self) -> None:
        if not self.enabled or self.link != "wifi":
            return
        self._ensure_worker()
        self._submit(self._job_detect)

    @pyqtSlot()
    def record(self) -> None:
        if self._state != "online" or not self._controllable:
            return
        self._submit(self._job_record)

    @pyqtSlot()
    def photo(self) -> None:
        if self._state != "online" or not self._controllable:
            return
        self._submit(self._job_photo)

    @pyqtSlot()
    def wakeWifi(self) -> None:
        """GoPro over Wi-Fi: turn the camera's AP on over Bluetooth."""
        if not self.enabled or self.kind != "gopro":
            return
        self._ensure_worker()
        self._submit(self._job_ble_wake)

    @pyqtSlot()
    def pairBle(self) -> None:
        if not self.enabled or self.kind != "gopro":
            return
        self._ensure_worker()
        self._submit(self._job_ble_pair)

    @pyqtSlot(bool)
    def noteReverse(self, active: bool) -> None:
        active = bool(active)
        if active == self._reverse:
            return
        self._reverse = active
        if not self.enabled or not self.reverseView:
            self.changed.emit()
            return
        if active:
            self._ensure_worker()
            self._view_wanted = True
            if self._state != "online":
                if self._state in ("off", "error"):
                    self._set_state("scanning")
                self._submit(self._job_status)
            self._maybe_start_stream()
        elif not self._page_visible:
            self._view_wanted = False
            self._stop_stream()
        self._rearm()
        self.changed.emit()

    # ── jobs (worker thread) ──────────────────────────────────────────────
    def _job_profile_flags(self) -> None:
        link = self._link
        if link is None:
            return
        ssid = self.ssidSetting or (self._detected[0]["ssid"] if self._detected else "")
        if not ssid:
            return
        name, err = link.ensure_profile(ssid, self.password, self.autostart)
        if err:
            print(f"[actioncam] profile: {err}", flush=True)

    def _job_detect(self) -> None:
        link = self._link
        if link is None:
            return
        found = link.detect()
        self._apply.emit(lambda: self._set_detected(found))

    def _set_detected(self, found) -> None:
        self._detected = [{"ssid": s, "signal": sig} for s, sig in found]
        self.changed.emit()

    def _job_connect(self) -> None:
        link = self._link
        if link is None:
            return
        if self.link == "wifi":
            ssid = self.ssidSetting
            if not ssid:
                found = link.detect()
                self._apply.emit(lambda: self._set_detected(found))
                ssid = found[0][0] if found else ""
            if not ssid:
                hint = ("Camera Wi-Fi off? Press Wi-Fi on (Bluetooth) or use the camera's Connect Device menu"
                        if self.kind == "gopro" else "Is the camera on with its Wi-Fi enabled?")
                self._apply.emit(lambda: self._set_state("error", f"No {self.label} network in range", hint))
                return
            name, err = link.ensure_profile(ssid, self.password, self.autostart)
            if not name:
                self._apply.emit(lambda: self._set_state("error", "Wi-Fi profile failed", err))
                return
            ok, err = link.up(name)
            if not ok:
                self._apply.emit(lambda: self._set_state("error", f"Could not join {ssid}", err))
                return
        self._job_status()

    def _job_disconnect(self) -> None:
        link = self._link
        if link is None:
            return
        if self._gopro is not None and self._cam_stream_on:
            self._job_cam_stream_off()
        if self.link == "wifi":
            name = link.profile_name
            info = link.state()
            if info.get("connection"):
                name = info["connection"]
            link.down(name)
        if self._insta is not None:
            self._insta.close()
        self._gopro = None
        self._controllable = False
        self._apply.emit(lambda: self._set_state("off", "Disconnected"))

    # -- where is the camera? -------------------------------------------
    def _locate(self):
        """(ip, base_url_override, note): ip None = not there, note says why."""
        if self.link == "usb":
            iface, our_ip = UsbNetLink.find()
            if not iface:
                return (None, None, "Plug the camera in (USB mode: GoPro Connect)")
            self._usb_iface = iface
            return (UsbNetLink.camera_ip(our_ip), None, "")
        link = self._link
        up = link.linked(extra_names=(self.ssidSetting,)) if link is not None else None
        if up is None:
            return (None, None, "Waiting for the camera's Wi-Fi…")
        gw, _my = up
        return (GOPRO_WIFI_IP if self.kind == "gopro" else (INSTA360_WIFI_IP or gw), None, "")

    def _job_status(self) -> None:
        self._poll_no += 1
        try:
            if self.kind == "insta360" and self.link == "usb":
                self._status_uvc()
            elif self.kind == "gopro":
                self._status_gopro()
            else:
                self._status_insta_wifi()
            self._miss = 0
        except CamError as e:
            msg = str(e)
            self._miss += 1
            # One bad poll while online is the camera changing mode (it
            # closes its HTTP port for a moment); only a second one in a row
            # means it is really gone.
            if self._state == "online" and self._miss < 2:
                self._apply.emit(lambda: self._note("Camera busy — " + msg))
                return
            self._apply.emit(lambda: self._lost("Camera not answering", msg))

    def _status_uvc(self) -> None:
        dev, name = UvcLink.find(self.uvcMatch)
        if not dev:
            self._apply.emit(lambda: self._lost(
                f"Set the {self.label} to Webcam mode and plug it in", ""))
            return

        def _ok():
            self._uvc_dev, self._uvc_name = dev, name
            self._controllable = False
            self._battery = -1
            self._sd_text = ""
            self._recording = -1
            self._mode = "Webcam"
            self._set_state("online", f"{name} · Webcam mode — picture only")
        self._apply.emit(_ok)

    def _status_gopro(self) -> None:
        ip, base, note = self._locate()
        if ip is None:
            # Wi-Fi: the AP may simply be off — nudge it over BLE now and then.
            if self.link == "wifi" and self.bleMac and self._car_awake and self.autostart \
                    and time.monotonic() - self._ble_last_wake > BLE_WAKE_MIN_S:
                self._ble_last_wake = time.monotonic()
                self._job_ble_wake(quiet=True)
            self._apply.emit(lambda: self._lost(note, ""))
            return
        if self._gopro is None or self._gopro.ip != ip:
            g = GoProHttp(ip)
            if base:
                g.base = base
            self._gopro = g
            self._wired_done = False
        g = self._gopro
        if self.link == "usb" and not self._wired_done and not base:
            g.wired_usb(True)
            self._wired_done = True
        raw_state = g.state()
        summary = gopro_summary(raw_state)
        # In webcam / preview mode the camera is "busy": every setting reads
        # 0 and every set is refused — keep the last real snapshot instead.
        settings_model = None if summary["busy"] else gopro_settings_model(raw_state)
        clips = None
        if self._page_visible or self._poll_no % 6 == 1:
            try:
                clips = g.media_list(5)
            except CamError:
                clips = None
        try:
            g.keep_alive()
        except CamError:
            pass

        def _ok():
            self._cam_ip = ip
            self._controllable = True
            self._battery = summary["battery"]
            self._sd_text = summary["sd_text"]
            self._sd_ok = summary["sd_ok"]
            self._recording = 1 if summary["encoding"] else 0
            self._progress_s = summary["progress_s"]
            self._mode = summary["mode"]
            if clips is not None:
                self._clips = clips
            if settings_model is not None:
                self._settings_model = settings_model
            parts = [self.label]
            if self._recording == 1:
                parts.append("REC " + hms(self._progress_s))
            if self._battery >= 0:
                parts.append(f"{self._battery} %")
            if self._sd_text:
                parts.append(self._sd_text)
            self._set_state("online", " · ".join(parts))
        self._apply.emit(_ok)

    def _status_insta_wifi(self) -> None:
        host, _base, note = self._locate()
        if host is None:
            self._apply.emit(lambda: self._lost(note, ""))
            return
        if self._insta is None:
            self._insta = Insta360Wifi(host, on_video=self._insta_video)
        insta = self._insta
        try:
            if not insta.connected:
                insta.connect()
            insta.keepalive()
            insta.pump(0.2)
        except OSError as exc:
            insta.close()
            raise CamError(f"camera port 6666: {exc}")
        mapped = insta.mapped

        def _ok():
            self._cam_ip = host
            # Record / photo go out with the published all-default bodies;
            # battery and storage need the model's get-options message.
            self._controllable = True
            self._battery = -1
            self._recording = -1
            self._mode = ""
            self._set_state("online", "Wi-Fi linked · record & photo" + ("" if mapped else
                            " · no battery/storage until this model's messages are captured"))
        self._apply.emit(_ok)

    def _lost(self, status: str, error: str) -> None:
        was = self.linked
        self._stop_stream()
        self._cam_ip = ""
        self._controllable = False
        if was or self._state in ("error", "joining"):
            self._set_state("scanning" if self._watch_wanted() else "off", status, error)
        else:
            self._status = status
            self._error = error
            self.changed.emit()
            self._rearm()

    # -- stream control on the camera ------------------------------------
    def _job_cam_stream_on(self) -> None:
        g = self._gopro
        if g is None:
            return
        port = GOPRO_STREAM_PORT
        try:
            if self.stream == "webcam":
                g.webcam_start(self.webcamRes, self.webcamFov, port)
            else:
                g.stream_start(port)
        except CamError as e:
            msg = str(e)
            if self.stream == "preview" and self._recording == 1:
                msg = "No preview while recording on this camera"
            self._apply.emit(lambda: self._stream_note("error", msg))
            return
        self._cam_stream_on = True
        self._apply.emit(lambda: self._start_stream(f"udp://@:{port}"))

    def _job_cam_stream_off(self) -> None:
        g = self._gopro
        self._cam_stream_on = False
        if g is None:
            return
        try:
            if self.stream == "webcam":
                g.webcam_stop()
                g.webcam_exit()
            else:
                g.stream_stop()
        except CamError:
            pass
        # Leaving webcam mode does not put the camera back into a shooting
        # preset by itself (HERO9: it sits in a limbo where every setting
        # reads 0 and is refused) — return it to Video.
        try:
            g.set_preset_group(1000)
        except CamError:
            pass

    # -- the camera's mode dance -------------------------------------------
    # Webcam / preview mode blocks settings, photos and recording ("busy",
    # HTTP 500 / 404), and the camera needs a moment after every preset
    # change. Every action therefore goes: leave the stream → set the preset
    # group → wait until not busy → act → back to Video → resume the stream.
    def _cam_ready(self, g: GoProHttp, timeout: float = 6.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = g.state()
                if not gopro_summary(last)["busy"]:
                    return last
            except CamError:
                pass
            time.sleep(0.4)
        return last

    def _leave_stream(self, g: GoProHttp) -> bool:
        """Stop the camera's stream if it runs; True when it has to be resumed."""
        resume = self._cam_stream_on or self._stream is not None
        if self._cam_stream_on:
            self._job_cam_stream_off()          # includes set_group(1000)
        if self._stream is not None:
            self._apply.emit(self._stop_stream)
        self._cam_ready(g)
        return resume

    def _enter_group(self, g: GoProHttp, group: int) -> None:
        try:
            g.set_preset_group(group)
        except CamError:
            pass
        self._cam_ready(g)

    def _resume_stream(self) -> None:
        self._apply.emit(self._maybe_start_stream)

    def _job_insta_live(self) -> None:
        insta = self._insta
        if insta is None:
            return
        try:
            insta.call("live_start")
        except CamError as e:
            msg = str(e)
            self._apply.emit(lambda: self._stream_note("error", msg))
            return
        self._cam_stream_on = True
        self._apply.emit(lambda: self._start_stream("appsrc://h265"))

    def _job_insta_live_off(self) -> None:
        self._cam_stream_on = False
        insta = self._insta
        if insta is None:
            return
        try:
            insta.call("live_stop")
        except CamError:
            pass

    def _insta_video(self, nal: bytes) -> None:
        s = self._stream
        if s is not None:
            s.push(nal)

    # -- record / photo ----------------------------------------------------
    def _job_record(self) -> None:
        if self.kind == "gopro" and self._gopro is not None:
            g = self._gopro
            try:
                if self._recording == 1:
                    g.shutter(False)
                    self._cam_ready(g)
                    self._status_gopro()
                    self._resume_stream()       # the picture comes back once it stopped
                    return
                resume = self._leave_stream(g)
                self._enter_group(g, 1000)
                g.shutter(True)
            except CamError as e:
                msg = str(e)
                self._apply.emit(lambda: self._note(f"Shutter failed: {msg}"))
                return
            time.sleep(0.8)
            self._status_gopro()
            # No preview while recording on this camera — nothing to resume.
        elif self._insta is not None:
            key = "record_stop" if self._recording == 1 else "record_start"
            try:
                self._insta.call(key)
            except CamError as e:
                msg = str(e)
                self._apply.emit(lambda: self._note(f"Record failed: {msg}"))
                return
            rec = 0 if key == "record_stop" else 1

            def _ok():
                self._recording = rec
                self._note("Recording" if rec else "Recording stopped")
            self._apply.emit(_ok)

    def _job_photo(self) -> None:
        if self.kind == "gopro" and self._gopro is not None:
            g = self._gopro
            resume = self._leave_stream(g)
            try:
                self._enter_group(g, 1001)
                g.shutter(True)
                time.sleep(1.5)
                self._cam_ready(g)
            except CamError as e:
                msg = str(e)
                self._apply.emit(lambda: self._note(f"Photo failed: {msg}"))
                self._enter_group(g, 1000)
                if resume:
                    self._resume_stream()
                return
            self._enter_group(g, 1000)
            self._apply.emit(lambda: self._note("Photo taken"))
            self._status_gopro()
            if resume:
                self._resume_stream()
        elif self._insta is not None:
            try:
                self._insta.call("photo")
                self._apply.emit(lambda: self._note("Photo taken"))
            except CamError as e:
                msg = str(e)
                self._apply.emit(lambda: self._note(f"Photo failed: {msg}"))

    def _job_set_setting(self, sid: int, opt: int) -> None:
        g = self._gopro
        if g is None:
            return
        # A setting applies only in a shooting preset, never in webcam mode
        # (HERO9 answers 500 there and reads every setting as 0): leave the
        # stream, go to the group the setting belongs to, apply, come back.
        resume = self._leave_stream(g)
        row = GOPRO_SETTINGS_BY_ID.get(sid)
        group = 1001 if (row and row[2] == "Photo") else 1000
        self._enter_group(g, group)
        try:
            g.set_setting(sid, opt)
        except CamError as e:
            msg = str(e)
            if "403" in msg:
                msg = "not available on this camera / in this mode"
            self._apply.emit(lambda: self._note(f"Setting refused: {msg}"))
        self._cam_ready(g)
        if group != 1000:
            self._enter_group(g, 1000)
        try:
            self._status_gopro()
            self._miss = 0
        except CamError as e:
            msg = str(e)
            self._apply.emit(lambda: self._note("Camera busy — " + msg))
        if resume:
            self._resume_stream()

    # -- Bluetooth ---------------------------------------------------------
    def _ble_say(self, text: str) -> None:
        def _do():
            self._ble_note = text
            self.changed.emit()
        self._apply.emit(_do)

    def _job_ble_pair(self) -> None:
        ble = GoProBle(self.bleMac)
        try:
            mac = ble.pair(on_note=self._ble_say)
            self._set("ble_mac", mac)
            try:
                ssid, pw = ble.read_ap()
                if ssid:
                    self._set("ssid", ssid)
                if pw:
                    self._set("password", pw)
                self._ble_say(f"Paired {mac} · Wi-Fi {ssid or '?'}")
            except CamError as e:
                self._ble_say(f"Paired {mac} · could not read its Wi-Fi name: {e}")
        except CamError as e:
            self._ble_say(f"Bluetooth: {e}")
        self._apply.emit(self.changed.emit)

    def _job_ble_wake(self, quiet: bool = False) -> None:
        ble = GoProBle(self.bleMac)
        try:
            ble.enable_ap(True)
            if not quiet:
                self._ble_say("Camera Wi-Fi switched on — joining…")
            time.sleep(3.0)
            if not quiet:
                self._job_connect()
        except CamError as e:
            if not quiet:
                self._ble_say(f"Bluetooth: {e}")
