"""What every camera bridge shares: the "nothing unless needed" machinery.

`CamBridgeBase` owns

- the settings-backed `enabled / autostart / liveOnOpen` trio,
- the state machine's public face (`state, statusText, lastError, busy,
  linked`) and `_set_state()`,
- the one QTimer whose cadence follows the rules — off while disabled or
  when there is nothing to watch, `LINK_WATCH_S` while unlinked and the car
  is awake (or the page is open), `STATUS_IDLE_S` while linked,
  `STATUS_PAGE_S` while the page is actually showing,
- the worker plumbing: `_submit(job)` runs a callable on the subclass's
  serialised worker and `_apply.emit(fn)` brings results back to the Qt
  thread,
- the picture: `startView/stopView/setPageVisible`, the `StreamWorker`
  lifetime (page showing, no screensaver over it, camera online), the
  latest frame under a lock and the `FrameImageProvider` for QML.

PyQt binds a property's notify signal to the class that declares it, so the
shared properties are not declared here: a subclass declares its own
`changed` / `frameChanged` signals and takes the shared ones with

    state, statusText, … = cam_props(changed)
    frameRev = frame_prop(frameChanged)

Subclasses (`DashcamBridge`, `ActionCamBridge`) add their link, their
client, the jobs, and answer four questions:

    _worker()          the object with `submit()` (or None when nothing exists)
    _watch_wanted()    should the unlinked state be polled right now?
    _job_status()      the periodic job (runs on the worker)
    _stream_source()   the camstream source string, or "" to fetch it first
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QTimer, Qt, pyqtProperty, pyqtSignal, pyqtSlot

from Modules.Runtime import settings_cache
from Modules.Runtime.camstream import FrameImageProvider, StreamWorker
from Modules.Runtime.services import service_on
from preferences import DATA_SETTINGS


class CamBridgeBase(QObject):
    changed = pyqtSignal()
    frameChanged = pyqtSignal()
    # worker → Qt thread: a callable to run here (queued: the emitter lives
    # on another thread).
    _apply = pyqtSignal(object)
    _frameArrived = pyqtSignal()

    SECTION = "Camera"
    SERVICE = ""          # Settings → Services master switch (services.py)
    PAGE_INDEX = -1
    STATUS_IDLE_S = 30
    STATUS_PAGE_S = 5
    LINK_WATCH_S = 20
    # States in which the camera is reachable enough to poll it.
    LINKED_STATES = ("online",)
    # States that keep the stream alive.
    STREAM_STATES = ("online",)
    STREAM_FPS = 15
    STREAM_LATENCY_MS = 200

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._nav = None
        self._alerts = None
        self._state = "off"
        self._status = ""
        self._error = ""
        self._job_busy = False
        self._page_visible = False
        self._view_wanted = False
        self._car_awake = False
        self._stream: Optional[StreamWorker] = None
        self._stream_state = ""
        self._stream_error = ""
        self._stream_source_used = ""

        self._frame = None
        self._frame_lock = threading.Lock()
        self._frame_rev = 0
        self._frame_pending = False

        self._timer = QTimer(self)
        self._timer.setSingleShot(False)
        self._timer.timeout.connect(self._tick)

        self._apply.connect(self._on_apply, Qt.ConnectionType.QueuedConnection)
        self._frameArrived.connect(self._on_frame_arrived, Qt.ConnectionType.QueuedConnection)

    # ── settings helpers ──────────────────────────────────────────────────
    def _get(self, key: str, fallback: str = "") -> str:
        return str(settings_cache.get(self.SECTION, key, fallback=fallback, path=DATA_SETTINGS) or "")

    def _getbool(self, key: str, fallback: bool = False) -> bool:
        return settings_cache.getboolean(self.SECTION, key, fallback=fallback, path=DATA_SETTINGS)

    def _set(self, key: str, value) -> None:
        settings_cache.set_value(self.SECTION, key, value, path=DATA_SETTINGS)

    # ── wiring from main.py ───────────────────────────────────────────────
    def set_alerts_bridge(self, alerts) -> None:
        """AlertsBridge, for the camera's connect/disconnect notification."""
        self._alerts = alerts

    def _link_alert(self, connected: bool) -> None:
        """Raise the connect/disconnect alert. Subclasses supply the key,
        label and icon; base does nothing."""
        pass

    def set_navigation_bridge(self, nav) -> None:
        self._nav = nav
        # A screensaver over the page: the picture is not seen, so the stream
        # is put away and the status poll drops to the idle cadence; both
        # come back when the screensaver goes.
        try:
            nav.screensaverChanged.connect(self._on_screensaver)
        except Exception:
            pass

    def image_provider(self) -> FrameImageProvider:
        return FrameImageProvider(self.latest_frame)

    def latest_frame(self):
        with self._frame_lock:
            return self._frame

    @pyqtSlot(bool, bool)
    def noteCarState(self, engine_on: bool, obd_connected: bool) -> None:
        awake = bool(engine_on or obd_connected)
        if awake == self._car_awake:
            return
        self._car_awake = awake
        self._on_car_awake(awake)
        self._rearm()

    def boot(self) -> None:
        """Once after the QML loaded. Only autostart brings anything up."""
        if self.enabled and self.autostart:
            self._on_autostart_boot()
            self._rearm()

    def shutdown(self) -> None:
        self._stop_stream()
        self._timer.stop()
        self._stop_worker()

    # ── hooks for subclasses ──────────────────────────────────────────────
    def _worker(self):
        return None

    def _ensure_worker(self) -> None:
        pass

    def _stop_worker(self) -> None:
        pass

    def _on_car_awake(self, awake: bool) -> None:
        pass

    def _on_autostart_boot(self) -> None:
        pass

    def _on_enabled(self, on: bool) -> None:
        pass

    def _on_autostart(self, on: bool) -> None:
        pass

    def _on_page_opened(self) -> None:
        pass

    def _watch_wanted(self) -> bool:
        return self._page_visible or (self.autostart and self._car_awake)

    def _job_status(self) -> None:
        pass

    def _stream_source(self) -> str:
        """camstream source for the live picture, or "" when it must be
        looked up first (the subclass then calls `_start_stream(src)` itself)."""
        return ""

    def _on_stream_stopped(self) -> None:
        pass

    # ── shared property getters (declared per subclass by cam_props()) ────
    def _p_enabled(self) -> bool:
        return self._getbool("enabled", False) and (not self.SERVICE or service_on(self.SERVICE))

    def _p_autostart(self) -> bool:
        return self._getbool("autostart", False)

    def _p_live_on_open(self) -> bool:
        return self._getbool("live_on_open", True)

    def _p_state(self) -> str:
        return self._state

    def _p_status(self) -> str:
        return self._status

    def _p_error(self) -> str:
        return self._error

    def _p_busy(self) -> bool:
        return self._job_busy

    def _p_linked(self) -> bool:
        return self._state in self.LINKED_STATES

    def _p_page_visible(self) -> bool:
        return self._page_visible

    def _p_stream_active(self) -> bool:
        return self._stream is not None

    def _p_stream_state(self) -> str:
        return self._stream_state

    def _p_stream_error(self) -> str:
        return self._stream_error

    def _p_stream_source(self) -> str:
        return self._stream_source_used

    def _p_frame_rev(self) -> int:
        return self._frame_rev

    # Plain-Python access for the base's own logic.
    enabled = property(_p_enabled)
    autostart = property(_p_autostart)
    liveOnOpen = property(_p_live_on_open)
    linked = property(_p_linked)

    @pyqtSlot(bool)
    def setEnabled(self, on: bool) -> None:
        self._set("enabled", "true" if on else "false")
        if not on:
            self._teardown()
        else:
            self._on_enabled(True)
            self._rearm()
        self.changed.emit()

    def _teardown(self) -> None:
        """Everything off, as if never enabled."""
        self._stop_stream()
        self._timer.stop()
        self._stop_worker()
        self._on_enabled(False)
        self._set_state("off", "")
        # The dock pill goes with the toggle; do not leave the user on a
        # page that no longer exists.
        try:
            if self._nav is not None and self.PAGE_INDEX >= 0 \
                    and self._nav.currentPage == self.PAGE_INDEX:
                self._nav.navigateTo(0)
        except Exception:
            pass

    @pyqtSlot()
    def refreshService(self) -> None:
        """Settings → Services flipped: off tears down, on lets the module's
        own `enabled` flag bring things back."""
        if not self.enabled:
            self._teardown()
        else:
            self._on_enabled(True)
            self._rearm()
        self.changed.emit()

    @pyqtSlot(bool)
    def setAutostart(self, on: bool) -> None:
        self._set("autostart", "true" if on else "false")
        if self.enabled:
            self._on_autostart(on)
        self._rearm()
        self.changed.emit()

    @pyqtSlot(bool)
    def setLiveOnOpen(self, on: bool) -> None:
        self._set("live_on_open", "true" if on else "false")
        self.changed.emit()

    # ── page / view ───────────────────────────────────────────────────────
    @pyqtSlot()
    def startView(self) -> None:
        self._view_wanted = True
        self._maybe_start_stream()

    @pyqtSlot()
    def stopView(self) -> None:
        self._view_wanted = False
        self._stop_stream()

    @pyqtSlot(bool)
    def setPageVisible(self, visible: bool) -> None:
        visible = bool(visible)
        if visible == self._page_visible:
            return
        self._page_visible = visible
        if visible:
            if self.enabled:
                self._ensure_worker()
                if self.liveOnOpen:
                    self._view_wanted = True
                self._on_page_opened()
                self._submit(self._job_status)
                self._maybe_start_stream()   # already online: no need to wait for the poll
            self._rearm()
        else:
            self._view_wanted = False
            self._stop_stream()
            self._rearm()
        self.changed.emit()

    @pyqtSlot()
    def refresh(self) -> None:
        if not self.enabled:
            return
        self._ensure_worker()
        self._submit(self._job_status)

    def _on_screensaver(self) -> None:
        if not self._page_visible:
            return
        if self._showing():
            self._rearm()
            self._maybe_start_stream()
        else:
            self._stop_stream()
            self._rearm()

    def _showing(self) -> bool:
        """The page is on screen with nothing over it."""
        if not self._page_visible:
            return False
        try:
            return not (self._nav is not None and self._nav.screensaverActive)
        except Exception:
            return True

    # ── timer ─────────────────────────────────────────────────────────────
    def _rearm(self) -> None:
        if not self.enabled or self._worker() is None:
            self._timer.stop()
            return
        if self.linked:
            interval = self.STATUS_PAGE_S if self._showing() else self.STATUS_IDLE_S
        elif self._watch_wanted():
            interval = self.LINK_WATCH_S
        else:
            self._timer.stop()
            return
        ms = interval * 1000
        if not self._timer.isActive() or self._timer.interval() != ms:
            self._timer.start(ms)

    def _tick(self) -> None:
        if self._worker() is None or self._job_busy:
            return
        self._submit(self._job_status)

    # ── worker plumbing ───────────────────────────────────────────────────
    def _submit(self, job: Callable[[], None]) -> None:
        worker = self._worker()
        if worker is None:
            return
        self._job_busy = True
        self.changed.emit()

        def _run():
            try:
                job()
            finally:
                self._apply.emit(self._job_done)

        worker.submit(_run)

    def _job_done(self) -> None:
        self._job_busy = False
        self.changed.emit()

    def _on_apply(self, fn) -> None:
        try:
            fn()
        except Exception as exc:
            print(f"[{self.SECTION.lower()}] apply failed: {type(exc).__name__}: {exc}", flush=True)

    def _note(self, text: str) -> None:
        self._status = text
        self.changed.emit()

    def _set_state(self, state: str, status: Optional[str] = None, error: str = "") -> None:
        # Connection / disconnection notification on the online edge, once per
        # transition (the subclass supplies the alert key / label / icon).
        was_online = self._state == "online"
        now_online = state == "online"
        self._state = state
        if status is not None:
            self._status = status
        self._error = error
        if now_online != was_online and self._alerts is not None:
            try:
                self._link_alert(now_online)
            except Exception:
                pass
        self.changed.emit()
        self._rearm()
        # The page may have asked for the picture before the camera was
        # readable (pairing, link still coming up): start it now.
        if state in self.STREAM_STATES:
            if self._view_wanted and self._stream is None:
                self._maybe_start_stream()
        elif state != "pairing":
            self._stop_stream()

    # ── stream ────────────────────────────────────────────────────────────
    def _maybe_start_stream(self) -> None:
        if not self._view_wanted or self._stream is not None \
                or self._state not in self.STREAM_STATES:
            return
        if self._page_visible and not self._showing():
            return
        src = self._stream_source()
        if src:
            self._start_stream(src)

    def _start_stream(self, source: str) -> None:
        if self._stream is not None or not self._view_wanted:
            return
        if self._page_visible and not self._showing():
            return
        self._stream_source_used = source
        self._stream_error = ""
        self._stream = StreamWorker(source, self._on_frame, self._on_stream_state,
                                    fps=self.STREAM_FPS, latency_ms=self.STREAM_LATENCY_MS)
        self._stream.start()
        self.changed.emit()

    def _stop_stream(self) -> None:
        s = self._stream
        if s is None:
            return
        self._stream = None
        s.stop()
        with self._frame_lock:
            self._frame = None
        self._stream_state = ""
        self.changed.emit()
        self._on_stream_stopped()

    def _on_frame(self, data: bytes, w: int, h: int, stride: int) -> None:
        # GStreamer thread.
        with self._frame_lock:
            self._frame = (data, w, h, stride)
            if self._frame_pending:
                return
            self._frame_pending = True
        self._frameArrived.emit()

    def _on_frame_arrived(self) -> None:
        with self._frame_lock:
            self._frame_pending = False
        self._frame_rev += 1
        self.frameChanged.emit()

    def _on_stream_state(self, text: str) -> None:
        # GStreamer thread.
        self._apply.emit(lambda: self._stream_note(text))

    def _stream_note(self, text: str, error: str = "") -> None:
        if text.startswith("error:"):
            self._stream_error = text[6:].strip()
            self._stream_state = "error"
        else:
            self._stream_state = text
            if error:
                self._stream_error = error
                self._stream_state = "error"
        self.changed.emit()


# ── property factories ────────────────────────────────────────────────────
CAM_PROP_NAMES = ("enabled", "autostart", "liveOnOpen", "state", "statusText", "lastError",
                  "busy", "linked", "pageVisible", "streamActive", "streamState",
                  "streamError", "streamSource")


def cam_props(changed):
    """The shared properties bound to a subclass's `changed` signal, in
    `CAM_PROP_NAMES` order — unpack them in the class body."""
    B = CamBridgeBase
    return (
        pyqtProperty(bool, fget=B._p_enabled, notify=changed),
        pyqtProperty(bool, fget=B._p_autostart, notify=changed),
        pyqtProperty(bool, fget=B._p_live_on_open, notify=changed),
        pyqtProperty(str, fget=B._p_state, notify=changed),
        pyqtProperty(str, fget=B._p_status, notify=changed),
        pyqtProperty(str, fget=B._p_error, notify=changed),
        pyqtProperty(bool, fget=B._p_busy, notify=changed),
        pyqtProperty(bool, fget=B._p_linked, notify=changed),
        pyqtProperty(bool, fget=B._p_page_visible, notify=changed),
        pyqtProperty(bool, fget=B._p_stream_active, notify=changed),
        pyqtProperty(str, fget=B._p_stream_state, notify=changed),
        pyqtProperty(str, fget=B._p_stream_error, notify=changed),
        pyqtProperty(str, fget=B._p_stream_source, notify=changed),
    )


def frame_prop(frame_changed):
    return pyqtProperty(int, fget=CamBridgeBase._p_frame_rev, notify=frame_changed)
