"""Camera pictures for QML: one GStreamer worker, many kinds of source.

Shared by the 70mai (`dashcam.py`) and the action cam (`actioncam.py`). The
worker turns a *source* into RGB frames handed to a callback; the bridges
keep the latest frame and QML draws it through `FrameImageProvider`
(`Image { source: "image://<name>/" + rev; cache: false }`).

Sources, by URL scheme:

    rtsp://host:554/path      70mai — rtspsrc, 200 ms latency
    udp://@:8554              GoPro — MPEG-TS over UDP to us (preview / webcam)
    v4l2:///dev/video0        any UVC camera (Insta360 in Webcam mode)
    appsrc://h264|h265        elementary stream pushed in with `push()`
                              (Insta360 over Wi-Fi hands NAL units over TCP)
    test://                   videotestsrc (bench check of the frame path)

The tail is the same for all of them: rate-capped to `fps`, converted to
RGBA, `appsink`. Nothing here exists between `stop()` and the next `start()`;
the pipeline lives on its own thread and never touches Qt.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional, Tuple

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QImage
from PyQt6.QtQuick import QQuickImageProvider

FrameCb = Callable[[bytes, int, int, int], None]   # data, w, h, stride


def source_head(source: str, latency_ms: int = 200) -> str:
    """The GStreamer description that produces decoded raw video for `source`."""
    s = (source or "").strip()
    if s.startswith("rtsp://"):
        return (f"rtspsrc location={s} latency={latency_ms} drop-on-latency=true "
                f"! decodebin")
    if s.startswith("udp://"):
        # udp://@:8554 or udp://0.0.0.0:8554 — the port is what matters.
        port = 8554
        tail = s[len("udp://"):]
        if ":" in tail:
            try:
                port = int(tail.rsplit(":", 1)[1].split("/")[0])
            except ValueError:
                pass
        return (f"udpsrc port={port} buffer-size=2097152 "
                f"caps=\"video/mpegts,systemstream=(boolean)true\" "
                f"! tsdemux ! queue max-size-buffers=0 max-size-time=200000000 ! decodebin")
    if s.startswith("v4l2://"):
        dev = s[len("v4l2://"):] or "/dev/video0"
        return f"v4l2src device={dev} ! decodebin"
    if s.startswith("appsrc://"):
        codec = s[len("appsrc://"):] or "h264"
        parse = "h265parse" if "265" in codec else "h264parse"
        return (f"appsrc name=feed is-live=true format=time do-timestamp=true "
                f"caps=video/x-{'h265' if '265' in codec else 'h264'},stream-format=byte-stream,alignment=nal "
                f"! {parse} ! decodebin")
    if s.startswith("test://"):
        return "videotestsrc is-live=true pattern=smpte ! video/x-raw,width=640,height=480,framerate=15/1"
    raise ValueError(f"unknown camera source {source!r}")


class StreamWorker:
    """`source` → RGB frames on GStreamer's thread.

    `on_frame(data, w, h, stride)` runs on the streaming thread — copy, do not
    touch Qt. `on_state(text)` gets "connecting" / "playing" / "reconnecting" /
    "error: …". `stop()` tears the pipeline down; `start()` after that builds
    a new one. Reconnects with back-off until stopped.
    """

    # RGBA, not RGB: Qt uploads RGBA8888 as-is, an RGB888 frame is converted
    # on every upload — at 1080p that alone was a frame's worth of time.
    TAIL = ("! videorate drop-only=true ! video/x-raw,framerate={fps}/1 ! videoconvert "
            "! video/x-raw,format=RGBA ! appsink name=sink emit-signals=true "
            "sync={sync} drop=true max-buffers=2")

    def __init__(self, source: str, on_frame: FrameCb, on_state: Callable[[str], None],
                 fps: int = 15, latency_ms: int = 200) -> None:
        self.source = source
        self.fps = max(1, int(fps))
        self.latency = max(0, int(latency_ms))
        self._on_frame = on_frame
        self._on_state = on_state
        self._pipeline = None
        self._feed = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.frames = 0

    @staticmethod
    def available() -> Tuple[bool, str]:
        try:
            import gi  # noqa: F401
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst  # noqa: F401
            return (True, "")
        except Exception as exc:
            return (False, f"GStreamer Python bindings missing ({type(exc).__name__})")

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="CamStream")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=3.0)
        self._thread = None

    def push(self, data: bytes) -> bool:
        """appsrc sources only: hand in one chunk of elementary stream."""
        feed = self._feed
        if feed is None:
            return False
        try:
            from gi.repository import Gst
            buf = Gst.Buffer.new_wrapped(bytes(data))
            return feed.emit("push-buffer", buf) == Gst.FlowReturn.OK
        except Exception:
            return False

    # -- internals --------------------------------------------------------
    def _run(self) -> None:
        ok, why = self.available()
        if not ok:
            self._on_state("error: " + why)
            return
        try:
            head = source_head(self.source, self.latency)
        except ValueError as exc:
            self._on_state(f"error: {exc}")
            return
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        # A live UDP/appsrc feed has no clock to sync against; syncing an
        # RTSP or v4l2 source keeps frame pacing smooth.
        sync = "false" if self.source.startswith(("udp://", "appsrc://")) else "true"
        desc = head + " " + self.TAIL.format(fps=self.fps, sync=sync)
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._session(Gst, desc)
                backoff = 1.0
            except Exception as exc:
                self._on_state(f"error: {exc}")
            if self._stop.is_set():
                break
            self._on_state("reconnecting")
            if self._stop.wait(backoff):
                break
            backoff = min(8.0, backoff * 2)

    def _session(self, Gst, desc: str) -> None:
        pipeline = Gst.parse_launch(desc)
        self._pipeline = pipeline
        self._feed = pipeline.get_by_name("feed")
        sink = pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)
        bus = pipeline.get_bus()
        pipeline.set_state(Gst.State.PLAYING)
        self._on_state("connecting")
        try:
            while not self._stop.is_set():
                msg = bus.timed_pop_filtered(
                    300 * Gst.MSECOND,
                    Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.STATE_CHANGED)
                if msg is None:
                    continue
                if msg.type == Gst.MessageType.ERROR:
                    err, _dbg = msg.parse_error()
                    raise RuntimeError(err.message if err else "pipeline error")
                if msg.type == Gst.MessageType.EOS:
                    raise RuntimeError("stream ended")
                if msg.type == Gst.MessageType.STATE_CHANGED and msg.src == pipeline:
                    _old, new, _pending = msg.parse_state_changed()
                    if new == Gst.State.PLAYING:
                        self._on_state("playing")
        finally:
            self._feed = None
            pipeline.set_state(Gst.State.NULL)
            self._pipeline = None

    def _on_sample(self, sink):
        from gi.repository import Gst
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        st = sample.get_caps().get_structure(0)
        w = st.get_value("width")
        h = st.get_value("height")
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = bytes(info.data)
        finally:
            buf.unmap(info)
        stride = len(data) // h if h else 0
        self.frames += 1
        try:
            self._on_frame(data, int(w), int(h), int(stride))
        except Exception:
            pass
        return Gst.FlowReturn.OK


class FrameImageProvider(QQuickImageProvider):
    """`image://<name>/<rev>` → the latest RGBA frame from `getter()`
    (a `(data, w, h, stride)` tuple or None)."""

    def __init__(self, getter: Callable[[], Optional[tuple]]) -> None:
        super().__init__(QQuickImageProvider.ImageType.Image)
        self._getter = getter

    def requestImage(self, _id: str, requested: QSize):  # noqa: N802
        frame = self._getter()
        if frame is None:
            img = QImage(16, 9, QImage.Format.Format_RGBA8888)
            img.fill(Qt.GlobalColor.black)
            return img, img.size()
        data, w, h, stride = frame
        # QImage wraps the buffer; copy() before `data` goes out of scope.
        img = QImage(data, w, h, stride, QImage.Format.Format_RGBA8888).copy()
        return img, img.size()
