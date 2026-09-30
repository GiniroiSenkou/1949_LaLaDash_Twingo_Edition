"""`Dashcam` — the 70mai over its Wi-Fi hotspot, for QML.

Built on `CamBridgeBase` (bridges/cam_common.py), which holds the "nothing
exists unless it is needed" machinery. What this adds:

- the link: a `DashcamLink` (nmcli) created only when enabled; `autostart`
  makes the camera's NetworkManager profile autoconnect (NM does the
  scanning) and the base polls the link state at 20 s only while the car is
  awake (engine on / OBD connected — the camera powers with ignition);
- the client: the signed CGI API, pairing with the camera's power button,
  a status job (getsdstate, getfilecount, the event list, `client.cgi
  register` — the camera has no push) every 30 s, 5 s with the page showing;
- the RTSP URL for the base's stream, found once per camera and remembered.

Every nmcli/HTTP call runs on the link's worker; results come back through
`_apply`. Frames reach QML through the `dashcam` image provider.
"""
from __future__ import annotations

import time
from typing import List, Optional

from PyQt6.QtCore import pyqtProperty, pyqtSignal, pyqtSlot

from Modules.Runtime.dashcam import (
    DEFAULT_PASSWORD, FILE_FRONT_EVENT, FILE_FRONT_NORMAL, DashcamClient, DashcamError,
    DashcamLink, detect_rtsp, file_stamp, human_size, new_user_id, pick, sd_summary,
    summarise,
)
from bridges.cam_common import CamBridgeBase, cam_props, frame_prop

# "Recording" is inferred: the normal-clip count moving between polls. Clips
# are 1–3 min, so a count that has not moved for this long means stopped.
REC_STALE_S = 300


class DashcamBridge(CamBridgeBase):
    SECTION = "Dashcam"
    SERVICE = "dashcam"
    PAGE_INDEX = 7
    LINKED_STATES = ("online", "needs_pairing", "pairing")
    STREAM_STATES = ("online",)

    changed = pyqtSignal()
    frameChanged = pyqtSignal()
    (enabled, autostart, liveOnOpen, state, statusText, lastError, busy, linked,
     pageVisible, streamActive, streamState, streamError, streamSource) = cam_props(changed)
    frameRev = frame_prop(frameChanged)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._link: Optional[DashcamLink] = None
        self._client: Optional[DashcamClient] = None
        self._cam_ip = ""
        self._my_ip = ""
        self._profile = ""
        self._model = ""
        self._firmware = ""
        self._sd_text = ""
        self._sd_pct = -1
        self._recording = -1         # -1 unknown · 0 no · 1 yes
        self._normal_count: Optional[int] = None
        self._normal_count_t = 0.0
        self._event_count = 0
        self._events: List[dict] = []
        self._gps_text = ""
        self._detected: List[dict] = []
        self._rtsp_url = ""
        self._pairing = False
        self._pair_abort = False
        self._attr_done = False

    # ── connect / disconnect notification ─────────────────────────────────
    def _link_alert(self, connected: bool) -> None:
        model = self._model or "70mai"
        if connected:
            self._alerts.setAlert("dashcam_link", f"{model} connected", "70mai.png", "info", True)
        else:
            self._alerts.setAlert("dashcam_link", f"{model} disconnected", "70mai.png", "warning", True)

    # ── base hooks ────────────────────────────────────────────────────────
    def _worker(self):
        return self._link

    def _ensure_worker(self) -> None:
        if self._link is None:
            self._link = DashcamLink(self.iface)

    def _stop_worker(self) -> None:
        if self._link is not None:
            self._link.stop()
            self._link = None
        self._client = None
        self._attr_done = False

    def _on_car_awake(self, awake: bool) -> None:
        if awake and self.enabled and self.autostart and self._link is None:
            self._ensure_worker()
        if awake and self._link is not None and self._state in ("off", "scanning", "error"):
            self._set_state("scanning")
            self._submit(self._job_status)

    def _on_autostart_boot(self) -> None:
        self._ensure_worker()
        self._submit(self._job_profile_flags)

    def _on_enabled(self, on: bool) -> None:
        if on and self.autostart:
            self._ensure_worker()
            self._submit(self._job_profile_flags)

    def _on_autostart(self, on: bool) -> None:
        self._ensure_worker()
        self._submit(self._job_profile_flags)
        if on and self._car_awake:
            self._submit(self._job_status)

    def _on_page_opened(self) -> None:
        if self._state in ("off", "error"):
            self._set_state("scanning")

    def _stream_source(self) -> str:
        if self._rtsp_url:
            return self._rtsp_url
        self._submit(self._job_rtsp)
        return ""

    # ── settings-backed properties ────────────────────────────────────────
    @pyqtProperty(str, notify=changed)
    def ssidSetting(self) -> str:
        """"" = auto (the strongest 70mai_* in range)."""
        return self._get("ssid", "")

    @pyqtProperty(str, notify=changed)
    def password(self) -> str:
        return self._get("password", DEFAULT_PASSWORD) or DEFAULT_PASSWORD

    @pyqtProperty(str, notify=changed)
    def iface(self) -> str:
        return self._get("iface", "wlan0") or "wlan0"

    @pyqtProperty(bool, notify=changed)
    def paired(self) -> bool:
        return bool(self._get("token", ""))

    @pyqtSlot(str)
    def setSsid(self, ssid: str) -> None:
        self._set("ssid", (ssid or "").strip())
        self._profile = ""
        self.changed.emit()

    @pyqtSlot(str)
    def setPassword(self, pw: str) -> None:
        self._set("password", (pw or "").strip() or DEFAULT_PASSWORD)
        self.changed.emit()

    @pyqtSlot(str)
    def setIface(self, iface: str) -> None:
        iface = (iface or "").strip() or "wlan0"
        self._set("iface", iface)
        if self._link is not None:
            self._link.set_iface(iface)
        self._profile = ""
        self.changed.emit()

    @pyqtSlot()
    def forgetPairing(self) -> None:
        self._set("token", "")
        if self._client is not None:
            self._client.token = ""
        self._attr_done = False
        if self._state == "online":
            self._set_state("needs_pairing", "Not paired — press Pair")
        self.changed.emit()

    # ── live state properties ─────────────────────────────────────────────
    @pyqtProperty(str, notify=changed)
    def camIp(self) -> str:
        return self._cam_ip

    @pyqtProperty(str, notify=changed)
    def ssid(self) -> str:
        """The network actually in use / that would be used."""
        if self.ssidSetting:
            return self.ssidSetting
        return self._detected[0]["ssid"] if self._detected else ""

    @pyqtProperty(str, notify=changed)
    def detectedSsid(self) -> str:
        return self._detected[0]["ssid"] if self._detected else ""

    @pyqtProperty(list, notify=changed)
    def detectedNetworks(self) -> list:
        return list(self._detected)

    @pyqtProperty(str, notify=changed)
    def model(self) -> str:
        return self._model

    @pyqtProperty(str, notify=changed)
    def firmware(self) -> str:
        return self._firmware

    @pyqtProperty(str, notify=changed)
    def sdText(self) -> str:
        return self._sd_text

    @pyqtProperty(int, notify=changed)
    def sdPercent(self) -> int:
        return self._sd_pct

    @pyqtProperty(int, notify=changed)
    def recording(self) -> int:
        return self._recording

    @pyqtProperty(int, notify=changed)
    def eventCount(self) -> int:
        return self._event_count

    @pyqtProperty(list, notify=changed)
    def events(self) -> list:
        return list(self._events)


    @pyqtProperty(str, notify=changed)
    def gpsText(self) -> str:
        return self._gps_text


    @pyqtProperty(bool, notify=changed)
    def pairing(self) -> bool:
        return self._pairing

    # ── commands ──────────────────────────────────────────────────────────
    @pyqtSlot()
    def detect(self) -> None:
        if not self.enabled:
            return
        self._ensure_worker()
        self._submit(self._job_detect)

    @pyqtSlot()
    def connectNow(self) -> None:
        if not self.enabled:
            return
        self._ensure_worker()
        self._set_state("joining", "Joining the camera's Wi-Fi…")
        self._submit(self._job_connect)

    @pyqtSlot()
    def disconnectNow(self) -> None:
        self._stop_stream()
        if self._link is None:
            return
        self._submit(self._job_disconnect)

    @pyqtSlot()
    def pair(self) -> None:
        if not self.enabled or self._pairing:
            return
        self._ensure_worker()
        self._pair_abort = False
        self._submit(self._job_pair)

    @pyqtSlot()
    def cancelPair(self) -> None:
        self._pair_abort = True

    @pyqtSlot()
    def snapshot(self) -> None:
        if self._client is None or self._state != "online":
            return
        self._submit(self._job_snapshot)

    # ── jobs (worker thread) ──────────────────────────────────────────────
    def _resolve_ssid(self, link: DashcamLink) -> str:
        pinned = self.ssidSetting
        if pinned:
            return pinned
        found = link.detect()
        self._apply.emit(lambda: self._set_detected(found))
        return found[0][0] if found else ""

    def _set_detected(self, found) -> None:
        self._detected = [{"ssid": s, "signal": sig} for s, sig in found]
        self.changed.emit()

    def _job_detect(self) -> None:
        link = self._link
        if link is None:
            return
        found = link.detect()
        self._apply.emit(lambda: self._set_detected(found))

    def _job_profile_flags(self) -> None:
        """Keep the NM profile's autoconnect in step with [Dashcam] autostart
        (NM does the joining when autostart is on)."""
        link = self._link
        if link is None:
            return
        ssid = self.ssidSetting or (self._detected[0]["ssid"] if self._detected else "")
        if not ssid:
            return
        name, err = link.ensure_profile(ssid, self.password, self.autostart)
        if name:
            self._profile = name
        elif err:
            print(f"[dashcam] profile: {err}", flush=True)

    def _job_connect(self) -> None:
        link = self._link
        if link is None:
            return
        ssid = self._resolve_ssid(link)
        if not ssid:
            self._apply.emit(lambda: self._set_state(
                "error", "No 70mai network in range", "Is the camera on and its Wi-Fi enabled?"))
            return
        name, err = link.ensure_profile(ssid, self.password, self.autostart)
        if not name:
            self._apply.emit(lambda: self._set_state("error", "Wi-Fi profile failed", err))
            return
        self._profile = name
        ok, err = link.up(name)
        if not ok:
            self._apply.emit(lambda: self._set_state("error", f"Could not join {ssid}", err))
            return
        self._job_status()

    def _job_disconnect(self) -> None:
        link = self._link
        if link is None:
            return
        name = self._profile or link.profile_for_ssid(self.ssid)
        if name:
            link.down(name)
        self._client = None
        self._attr_done = False
        self._apply.emit(lambda: self._set_state("off", "Disconnected"))

    def _link_up(self, link: DashcamLink):
        """(base_url, my_ip) when our adapter is on the camera's network,
        else None. Only the camera counts: the adapter may well be on the
        house Wi-Fi instead."""
        up = link.linked(extra_names=(self._profile, self.ssidSetting))
        if up is None:
            return None
        gw, my_ip = up
        return (f"http://{gw}", my_ip)

    def _job_status(self) -> None:
        link = self._link
        if link is None:
            return
        up = self._link_up(link)
        if up is None:
            if self._client is not None:
                self._client = None
                self._attr_done = False
            wanted = "Waiting for the camera's Wi-Fi…" if (self.autostart and self._car_awake) \
                else "Not connected"
            self._apply.emit(lambda: self._link_lost(wanted))
            return
        base, my_ip = up
        token = self._get("token", "")
        if self._client is None or self._client.base_url != base.rstrip("/"):
            self._client = DashcamClient(base, token, self._get("sign_variant", "?"))
            self._attr_done = False
        client = self._client
        client.token = token
        if not token:
            self._apply.emit(lambda: self._linked(base, my_ip, "needs_pairing",
                                                  "Connected — pair the camera to read it"))
            return
        try:
            snap = {}
            if not self._attr_done:
                snap["attr"] = client.device_attr()
            snap["sd"] = client.sd_state()
            snap["count"] = client.file_count()
            snap["events"] = client.file_list(FILE_FRONT_EVENT, 1, 5)
            if self._page_visible:
                try:
                    snap["gps"] = client.gps()
                except DashcamError:
                    snap["gps"] = {}
            try:
                client.register(my_ip)
            except DashcamError:
                pass
        except DashcamError as e:
            msg = str(e)
            if "-6677" in msg:
                self._apply.emit(lambda: self._linked(base, my_ip, "needs_pairing",
                                                      "Camera rejected the pairing — pair again"))
            else:
                self._apply.emit(lambda: self._linked(base, my_ip, "error", "Camera not answering", msg))
            return
        self._apply.emit(lambda: self._absorb(base, my_ip, snap))

    def _link_lost(self, status: str) -> None:
        was_linked = self.linked
        self._stop_stream()
        self._cam_ip = ""
        if was_linked or self._state == "error":
            self._set_state("scanning" if self._watch_wanted() else "off", status)
        elif self._state == "joining":
            self._set_state("error", status)
        else:
            self._status = status
            self.changed.emit()
            self._rearm()

    def _linked(self, base: str, my_ip: str, state: str, status: str, error: str = "") -> None:
        self._cam_ip = base.replace("http://", "")
        self._my_ip = my_ip
        self._set_state(state, status, error)

    def _absorb(self, base: str, my_ip: str, snap: dict) -> None:
        self._cam_ip = base.replace("http://", "")
        self._my_ip = my_ip
        attr = snap.get("attr")
        if isinstance(attr, dict):
            self._model = pick(attr, "model", "name", "devicename", "product") or "70mai"
            self._firmware = pick(attr, "version", "firmware", "fw", "soft")
            self._attr_done = True
        sd = snap.get("sd") or {}
        self._sd_text, self._sd_pct = sd_summary(sd)
        count = snap.get("count") or {}
        try:
            n = int(pick(count, str(FILE_FRONT_NORMAL), "normal", "front", default="-1"))
        except ValueError:
            n = -1
        now = time.monotonic()
        if n >= 0:
            if self._normal_count is None or n != self._normal_count:
                if self._normal_count is not None:
                    self._recording = 1
                self._normal_count = n
                self._normal_count_t = now
            elif now - self._normal_count_t > REC_STALE_S:
                self._recording = 0
        try:
            self._event_count = int(pick(count, str(FILE_FRONT_EVENT), "event", default="0"))
        except ValueError:
            self._event_count = 0
        rows = []
        for row in snap.get("events") or []:
            name = str(row.get("name", ""))
            try:
                size = human_size(float(row.get("size", 0)))
            except (TypeError, ValueError):
                size = ""
            rows.append({"name": name, "time": file_stamp(name), "size": size,
                         "path": str(row.get("path", ""))})
        self._events = rows
        if "gps" in snap:
            g = snap["gps"] or {}
            lat, lon = pick(g, "lat"), pick(g, "lon", "lng")
            sats = pick(g, "sat")
            if lat and lon:
                self._gps_text = f"{lat}, {lon}" + (f" · {sats} sats" if sats else "")
            else:
                self._gps_text = summarise(g, 60) if g else "No fix"
        rec = {1: "Recording", 0: "Not recording", -1: ""}[self._recording]
        status = " · ".join(x for x in (self._model or "70mai", rec, self._sd_text) if x)
        self._set_state("online", status)

    def _job_pair(self) -> None:
        link = self._link
        if link is None:
            return
        up = self._link_up(link)
        if up is None:
            self._apply.emit(lambda: self._set_state("error", "Join the camera's Wi-Fi first"))
            return
        base, my_ip = up
        client = DashcamClient(base, "", self._get("sign_variant", "?"))
        self._client = client
        user_id = self._get("user_id", "") or new_user_id()
        self._set("user_id", user_id)

        def _prompt():
            self._pairing = True
            self._set_state("pairing", "Press the camera's power button now…")

        try:
            token = client.pair(user_id, wait_s=60.0, on_prompt=lambda: self._apply.emit(_prompt),
                                should_abort=lambda: self._pair_abort)
            accepted = client.probe_variant()
        except DashcamError as e:
            msg = str(e)
            self._apply.emit(lambda: self._pair_finished(False, "Pairing failed", msg))
            return
        self._set("token", token)
        self._set("sign_variant", client.variant)
        if not accepted:
            self._apply.emit(lambda: self._pair_finished(
                True, "Paired, but the camera rejects signed calls", "sign variant not accepted"))
            return
        self._attr_done = False
        self._apply.emit(lambda: self._pair_finished(True, "Paired ✓"))
        self._job_status()

    def _pair_finished(self, ok: bool, status: str, error: str = "") -> None:
        self._pairing = False
        self._set_state("online" if ok else "needs_pairing", status, error)

    def _job_snapshot(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            r = client.snapshot().get("Result")
            name = pick(r, "name") if isinstance(r, dict) else ""
            msg = f"Photo saved{(' · ' + name) if name else ''}"
            self._apply.emit(lambda: self._note(msg))
        except DashcamError as e:
            msg = str(e)
            self._apply.emit(lambda: self._note(f"Snapshot failed: {msg}"))

    def _job_rtsp(self) -> None:
        ip = self._cam_ip
        saved = self._get("rtsp_path", "")
        url = ""
        if ip and saved:
            url = f"rtsp://{ip}:554{saved}"
        elif ip:
            url = detect_rtsp(ip)
            if url:
                self._set("rtsp_path", url.split(":554", 1)[1])
        if not url:
            self._apply.emit(lambda: self._stream_note("error", "No live stream found on this camera"))
            return

        def _got():
            self._rtsp_url = url
            self._start_stream(url)
        self._apply.emit(_got)
