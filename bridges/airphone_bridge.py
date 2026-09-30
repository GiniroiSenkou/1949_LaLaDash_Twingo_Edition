from __future__ import annotations

import os
import subprocess
import threading
import shutil

from PyQt6.QtCore import QObject, QTimer, pyqtProperty, pyqtSignal, pyqtSlot

from Modules.Runtime import settings_cache
from Modules.Runtime.services import service_on
from preferences import DATA_SETTINGS


class AirPhoneBridge(QObject):
    """Manages rpiplay subprocess lifecycle and exposes AirPlay state to QML."""

    changed = pyqtSignal()

    # Emitted from the rpiplay stdout reader thread the moment a phone
    # completes its handshake. Queued onto the Qt thread by the connection in
    # __init__, because everything it leads to touches the X11 connection.
    _clientArrived = pyqtSignal()

    # Same thread, same reason, for the phone going away.
    _clientLeft = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc = None
        self._running = False
        self._active = False
        self._device_name = ""
        self._last_error = ""
        self._last_log = ""
        self._command_line = ""
        self._exit_code = 0
        self._renderers = None

        # Embedding of rpiplay's own video window (see _place_video_window).
        self._host_window = None      # the app's QQuickWindow
        self._video_win = None        # rpiplay's X11 window id
        self._video_reparented = False
        # The area QML offers the mirror, not the mirror's own rect. The
        # window is fitted inside it by _fit_rect() so the picture keeps the
        # phone's shape instead of being letterboxed by the sink into a
        # landscape box with black bars down both sides.
        self._video_area = (0, 0, 0, 0)
        self._video_shown = False
        self._xdisp = False           # False = not tried yet, None = no X11
        self._embed_note = ""

        # Source shape, sampled from the sink's own window before we resize
        # it: rpiplay sizes that window to the incoming stream, so it is a
        # free read of whether the phone is held portrait or landscape.
        self._source_w = 0
        self._source_h = 0
        # "auto" follows the phone; "portrait"/"landscape" pin it.
        self._aspect_mode = (settings_cache.get(
            "AirPlay", "video_aspect", fallback="auto", path=DATA_SETTINGS
        ) or "auto").strip().lower()
        if self._aspect_mode not in ("auto", "portrait", "landscape"):
            self._aspect_mode = "auto"
        # Mirror filling the whole app window, chrome and dock included.
        self._video_fullscreen = False
        # True once the sink's window exists AND is inside the app window.
        # The page uses it to leave the "waiting" state, because the
        # PulseAudio check behind `active` lags a live picture by seconds.
        self._mirror_ready = False

        # Poll only runs while rpiplay subprocess is alive or an external
        # mirror is currently active. Started by `start()` / re-armed by
        # _poll() if state warrants; stopped when there's nothing to watch.
        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self._poll)

        # Short-lived fast retry, armed only between a phone connecting and
        # its video window being adopted. The 3 s status poll is far too slow
        # for that hand-off: the sink maps its window a few hundred ms after
        # the handshake, and until it is adopted it sits over the dashboard.
        # Stops itself as soon as the window is a child of the app window, so
        # it never runs while the mirror is idle.
        self._adopt_timer = QTimer(self)
        self._adopt_timer.setInterval(250)
        self._adopt_timer.timeout.connect(self._try_adopt_video_window)
        # Only runs while fullscreen holds the button grab; see _set_input_grab.
        self._tap_timer = QTimer(self)
        self._tap_timer.setInterval(60)
        self._tap_timer.timeout.connect(self._pump_x_events)

        # Music bridge, handed over by main.py so an incoming stream can duck
        # playback. Optional: the receiver works without it.
        self._music_bridge = None
        self._ducked = False
        # rpiplay's stream volume, applied to its PulseAudio sink-inputs.
        self._volume = max(0, min(150, settings_cache.getint(
            "AirPlay", "volume", fallback=100, path=DATA_SETTINGS
        )))
        # pactl calls are subprocesses; keep them off the Qt event loop.
        self._vol_lock = threading.Lock()

        self._clientArrived.connect(self._begin_video_adoption)
        self._clientLeft.connect(self._end_video_session)
        self._clientArrived.connect(self._duck_music)
        self._clientArrived.connect(self._apply_volume_async)
        self._clientLeft.connect(self._unduck_music)

    # ── properties ────────────────────────────────────────────────────────

    @pyqtProperty(bool, notify=changed)
    def enabled(self):
        # ANDed with the Settings → Services master switch: off there means
        # no page, no section, no rpiplay.
        return service_on("airphone") and settings_cache.getboolean(
            "AirPlay", "enabled", fallback=False, path=DATA_SETTINGS)

    @pyqtProperty(bool, notify=changed)
    def serviceOn(self):
        return service_on("airphone")

    @pyqtProperty(bool, notify=changed)
    def autostart(self):
        """Run the receiver from boot. Off by default — `enabled` only means
        the feature is available, not that rpiplay should be resident."""
        return settings_cache.getboolean(
            "AirPlay", "autostart", fallback=False, path=DATA_SETTINGS
        )

    @pyqtSlot(bool)
    def setAutostart(self, on):
        settings_cache.set_value(
            "AirPlay", "autostart", "true" if bool(on) else "false", path=DATA_SETTINGS
        )
        self.changed.emit()

    @pyqtProperty(bool, notify=changed)
    def running(self):
        return self._running

    @pyqtProperty(bool, notify=changed)
    def active(self):
        return self._active

    @pyqtProperty(str, notify=changed)
    def deviceName(self):
        return self._device_name

    @pyqtProperty(str, notify=changed)
    def statusText(self):
        if not self.enabled:
            return "AirPlay disabled"
        if self._active:
            suffix = f" — {self._device_name}" if self._device_name else ""
            return f"Mirroring{suffix}"
        if self._running:
            return "Waiting for connection…"
        if self._last_error:
            return self._last_error
        return "AirPlay stopped"

    @pyqtProperty(str, notify=changed)
    def lastError(self):
        return self._last_error

    @pyqtProperty(str, notify=changed)
    def lastLog(self):
        return self._last_log

    @pyqtProperty(str, notify=changed)
    def commandLine(self):
        return self._command_line


    @pyqtProperty(bool, notify=changed)
    def rpiplayAvailable(self):
        return shutil.which("rpiplay") is not None

    # Renderer preference, best first. `gstreamer` leads deliberately: the
    # `rpi` renderer is RPiPlay's OpenMAX/MMAL path, which needs the legacy
    # Broadcom firmware graphics stack. This Pi runs `vc4-kms-v3d` under
    # labwc, where OpenMAX is gone — picking it there is how you get a
    # receiver that connects and then shows nothing. `dummy` is never chosen;
    # it is only ever what is left when the build has nothing else.
    _VIDEO_RENDERER_ORDER = ("gstreamer", "rpi")
    _AUDIO_RENDERER_ORDER = ("gstreamer", "rpi")

    def _probe_renderers(self):
        """Renderers this rpiplay binary was actually built with.

        Returns ``{"video": [...], "audio": [...]}``. RPiPlay prints two
        "Available renderers:" blocks in ``-h`` — video first, then audio —
        and compiles each renderer in at build time, so this is a property of
        the binary and is probed once and cached.

        A build with only `dummy` accepts the iPhone's stream and throws every
        frame away: the phone says it is mirroring while the screen stays
        black. That reads as a bug in this app, so it is worth detecting.
        """
        if self._renderers is not None:
            return self._renderers
        found = {"video": [], "audio": []}
        try:
            binary = shutil.which("rpiplay")
            if binary:
                out = subprocess.run([binary, "-h"], capture_output=True, text=True,
                                     timeout=5, env=self._launch_env())
                text = (out.stdout or "") + (out.stderr or "")
                # Anchor on the list header, not on "-vr renderer" — that
                # string also appears in the Usage line.
                blocks = text.split("Available renderers:")[1:]
                for kind, block in zip(("video", "audio"), blocks):
                    for line in block.splitlines()[1:]:
                        stripped = line.strip()
                        # The block ends at the next option line.
                        if not stripped or stripped.startswith("-"):
                            break
                        found[kind].append(stripped.split(":", 1)[0].strip())
        except Exception as exc:
            print(f"WARNING: rpiplay renderer probe failed: {exc}", flush=True)
        self._renderers = found
        return found

    def _pick_renderer(self, kind):
        """Best renderer of `kind`, or "" to leave rpiplay on its default.

        ``[AirPlay] video_renderer`` / ``audio_renderer`` in settings.ini wins
        if it names something the binary actually has — an override that
        silently selects a missing renderer would be worse than no override.
        """
        available = self._probe_renderers().get(kind) or []
        wanted = (settings_cache.get("AirPlay", f"{kind}_renderer", fallback="",
                                     path=DATA_SETTINGS) or "").strip()
        if wanted and wanted in available:
            return wanted
        order = (self._VIDEO_RENDERER_ORDER if kind == "video"
                 else self._AUDIO_RENDERER_ORDER)
        for name in order:
            if name in available:
                return name
        return ""


    @pyqtProperty(bool, notify=changed)
    def videoRendererAvailable(self):
        """False when the build has nothing but `dummy` — mirroring is black."""
        renderers = self._probe_renderers().get("video") or []
        if not renderers:
            return True          # could not tell; do not cry wolf
        return any(r != "dummy" for r in renderers)

    @pyqtProperty(str, notify=changed)
    def rendererNote(self):
        if self.videoRendererAvailable:
            return ""
        return ("This rpiplay build has no video renderer — only \"dummy\", "
                "which receives the stream and discards every frame. The phone "
                "will say it is mirroring and the screen will stay black. "
                "Rebuild rpiplay with -DCMAKE_BUILD_TYPE=Release and the "
                "GStreamer renderer enabled (gstreamer1.0-plugins-base/-good "
                "and libgstreamer1.0-dev must be present at configure time).")

    # ── embedding rpiplay's video window ──────────────────────────────────
    #
    # RPiPlay's GStreamer renderer opens its own top-level window, so a
    # working receiver drops a bare video window over the dashboard instead of
    # filling the AirPlay page. There is no rpiplay flag for this — the sink
    # is created inside the renderer — so the window is adopted from outside:
    # reparent it into the app's own window, then move and size it to the
    # page's content rect.
    #
    # X11 only, which is what the Pi runs (QT_QPA_PLATFORM=xcb under
    # Xwayland). Everything degrades to a no-op: without python-xlib, or on a
    # Wayland-native session, the receiver still works exactly as before — it
    # just keeps its own window, and `embedNote` says so.
    #
    # Position and size go in ONE ConfigureWindow call, deliberately. Issued
    # separately — as `xdotool windowmove` then `windowsize` does — the video
    # sink reasserts whichever attribute the second call did not set, so the
    # window ends up either the right size in the wrong place or the reverse.
    # Measured against a `videotestsrc ! autovideosink` stand-in, which is the
    # same kind of window the renderer creates.

    def set_host_window(self, window):
        """Adopt the app's QQuickWindow as the reparent target."""
        self._host_window = window

    def _xdisplay(self):
        """Cached X11 connection, or None when there is no usable one."""
        if self._xdisp is not False:
            return self._xdisp
        self._xdisp = None
        try:
            from Xlib import display as _xdisplay_mod
            self._xdisp = _xdisplay_mod.Display()
        except Exception as exc:
            self._embed_note = (
                "Mirror cannot be embedded: python-xlib is unavailable "
                f"({type(exc).__name__}). rpiplay keeps its own window.")
        return self._xdisp

    def _host_win_id(self):
        """The app's X11 window, but only if it is actually on screen.

        Qt keeps more than one X window alive — there is a 1x1 unmapped helper
        beside the real one. Reparenting into that gives a child whose map
        state is IsUnviewable: the mirror is placed perfectly and is invisible,
        with nothing anywhere reporting an error. Checking here means a wrong
        handle costs the embedding, not the picture.
        """
        try:
            if self._host_window is None:
                return 0
            wid = int(self._host_window.winId())
            if wid <= 0:
                return 0
            disp = self._xdisplay()
            if disp is None:
                return 0
            from Xlib import X
            attrs = disp.create_resource_object("window", wid).get_attributes()
            if attrs.map_state != X.IsViewable:
                self._embed_note = (
                    "Mirror cannot be embedded: the app window is not "
                    "viewable. rpiplay keeps its own window.")
                return 0
            return wid
        except Exception:
            return 0

    def _find_video_window(self):
        """rpiplay's video window, or None while it has not mapped one.

        Matched on _NET_WM_PID rather than on the window name: the sink names
        its window after whichever GStreamer element it picked, which varies
        by build. The largest mapped match wins, so a tiny utility window
        cannot be mistaken for the video surface.
        """
        if self._proc is None or self._proc.poll() is not None:
            return None
        disp = self._xdisplay()
        if disp is None:
            return None
        pid = self._proc.pid
        host = self._host_win_id()
        try:
            from Xlib import X
            atom = disp.intern_atom("_NET_WM_PID")
            best, best_area = None, -1

            def walk(win, depth=0):
                nonlocal best, best_area
                if depth > 4:
                    return
                try:
                    children = win.query_tree().children
                except Exception:
                    return
                for child in children:
                    try:
                        prop = child.get_full_property(atom, X.AnyPropertyType)
                        if prop and prop.value and int(prop.value[0]) == pid:
                            if int(child.id) != host:
                                geo = child.get_geometry()
                                area = int(geo.width) * int(geo.height)
                                if area > best_area:
                                    best, best_area = child, area
                    except Exception:
                        pass
                    walk(child, depth + 1)

            walk(disp.screen().root)
            return best
        except Exception:
            return None

    @pyqtSlot(int, int, int, int)
    def setVideoGeometry(self, x, y, w, h):
        """The area the caller offers the mirror, in app-window coords."""
        area = (int(x), int(y), max(1, int(w)), max(1, int(h)))
        if area == self._video_area:
            return
        self._video_area = area
        self._place_video_window()

    # ── orientation ───────────────────────────────────────────────────────

    def _effective_aspect(self) -> str:
        """'portrait' or 'landscape' — the user's pin, else the source."""
        if self._aspect_mode in ("portrait", "landscape"):
            return self._aspect_mode
        if self._source_w > 0 and self._source_h > 0:
            return "portrait" if self._source_h > self._source_w else "landscape"
        # Nothing sampled yet. Phones mirror portrait far more often than not,
        # and guessing landscape is the worse miss: it fills the whole area, so
        # the first frames arrive stretched across the page before the sample
        # lands, which is exactly the flash this is meant to avoid.
        return "portrait"

    def _aspect_ratio(self) -> float:
        """width / height of the shape currently being applied."""
        if (self._aspect_mode == "auto"
                and self._source_w > 0 and self._source_h > 0):
            return self._source_w / float(self._source_h)
        # No measurement to honour, so use the shape's nominal ratio:
        # 9:16 upright, 16:9 across.
        return 9.0 / 16.0 if self._effective_aspect() == "portrait" else 16.0 / 9.0

    def _fit_rect(self, area):
        """Largest rect of the wanted shape that fits centred inside `area`."""
        ax, ay, aw, ah = area
        if aw <= 1 or ah <= 1:
            return area
        ratio = self._aspect_ratio()
        w = aw
        h = int(round(w / ratio))
        if h > ah:
            h = ah
            w = int(round(h * ratio))
        w = max(1, min(w, aw))
        h = max(1, min(h, ah))
        return (ax + (aw - w) // 2, ay + (ah - h) // 2, w, h)

    @pyqtProperty(str, notify=changed)
    def videoAspectMode(self):
        """'auto' | 'portrait' | 'landscape' — the user's pin."""
        return self._aspect_mode


    @pyqtProperty(float, notify=changed)
    def mirrorAspect(self):
        """width / height of the mirror as it will actually be placed.

        QML sizes the card that frames the mirror from this. Without it the
        card is a fixed box and the sink letterboxes inside it, which is what
        the black bars down the sides of the driving screensaver were.
        """
        return float(self._aspect_ratio())

    @pyqtProperty(bool, notify=changed)
    def mirrorWindowReady(self):
        """True once the sink's window is live inside the app window.

        `active` is inferred from a PulseAudio sink input and can trail the
        picture by a poll or two; a muted mirror never flips it at all. The
        page would sit on "Waiting for iPhone…" over a running mirror, which
        is the flash users see on opening the page.
        """
        return self._mirror_ready

    @pyqtSlot(str)
    def setVideoAspectMode(self, mode):
        mode = (mode or "auto").strip().lower()
        if mode not in ("auto", "portrait", "landscape"):
            mode = "auto"
        if mode == self._aspect_mode:
            return
        self._aspect_mode = mode
        settings_cache.set_value("AirPlay", "video_aspect", mode, path=DATA_SETTINGS)
        self._place_video_window()
        self.changed.emit()

    # ── fullscreen ────────────────────────────────────────────────────────

    @pyqtProperty(bool, notify=changed)
    def videoFullscreen(self):
        return self._video_fullscreen

    @pyqtSlot(bool)
    def setVideoFullscreen(self, on):
        on = bool(on)
        if on == self._video_fullscreen:
            return
        self._video_fullscreen = on
        self._set_input_grab(on)
        # QML re-sends the area for the new mode; changed drives that binding.
        self.changed.emit()


    def _set_input_grab(self, on: bool):
        """Route taps to us while the mirror covers the screen.

        rpiplay's window is a real X child, so it — not Qt — receives every
        touch inside it. With the mirror filling the screen there is no QML
        left to tap, and no way back out. Grabbing the button on the app
        window reports presses anywhere in it (children included) to this
        connection instead, which is only safe to do because RPiPlay is
        display-only and never forwards input to the phone.

        Released again the moment fullscreen ends, and on stop()/disconnect,
        so a stuck grab cannot outlive the mode that needs it.
        """
        disp = self._xdisplay()
        if disp is None:
            return
        host = self._host_win_id()
        if not host:
            return
        try:
            from Xlib import X
            win = disp.create_resource_object("window", host)
            if on:
                win.grab_button(
                    X.AnyButton, X.AnyModifier, False, X.ButtonPressMask,
                    X.GrabModeAsync, X.GrabModeAsync, X.NONE, X.NONE,
                )
                self._tap_timer.start()
            else:
                self._tap_timer.stop()
                win.ungrab_button(X.AnyButton, X.AnyModifier)
            disp.sync()
        except Exception as exc:
            self._embed_note = f"Fullscreen input grab failed: {type(exc).__name__}"

    def _pump_x_events(self):
        """Drain the grabbed presses; any one of them leaves fullscreen."""
        disp = self._xdisplay()
        if disp is None:
            return
        try:
            from Xlib import X
            tapped = False
            for _ in range(disp.pending_events()):
                event = disp.next_event()
                if event.type == X.ButtonPress:
                    tapped = True
            if tapped:
                self.setVideoFullscreen(False)
        except Exception:
            pass

    @pyqtSlot(bool)
    def setVideoVisible(self, shown):
        """Hide the mirror when the AirPlay page is not the one on screen.

        Without this the video window stays over whatever page you navigate
        to: it is not part of the QML scene and knows nothing about the dock.
        """
        shown = bool(shown)
        if shown == self._video_shown:
            return
        self._video_shown = shown
        self._place_video_window()

    def _place_video_window(self):
        disp = self._xdisplay()
        if disp is None:
            return
        win = self._video_win
        if win is None:
            win = self._find_video_window()
            if win is None:
                return
            self._video_win = win
            self._video_reparented = False
            # Sample the shape now, while the window is still the size the
            # sink gave it. After the configure() below it is whatever we
            # made it, and the stream's own dimensions are gone.
            try:
                geo = win.get_geometry()
                if int(geo.width) > 1 and int(geo.height) > 1:
                    self._source_w = int(geo.width)
                    self._source_h = int(geo.height)
            except Exception:
                pass

        try:
            from Xlib import X
            host = self._host_win_id()
            x, y, w, h = self._fit_rect(self._video_area)
            if host and not self._video_reparented:
                # Reparenting is what puts it *inside* the page rather than
                # leaving a borderless window floating in the right place: it
                # then moves, stacks, hides and clips with the app window.
                #
                # This happens before the visibility check on purpose. A
                # window that is still a child of the root floats over
                # whatever page is on screen every time the sink maps it —
                # and the sink maps itself, on connect and on every format
                # renegotiation, without asking us. Unmapping it only wins
                # the race until the next map. Once it is a child of the app
                # window it is clipped by it and cannot float again, so adopt
                # it the moment it is found even if the page is hidden.
                win.reparent(disp.create_resource_object("window", host), x, y)
                self._video_reparented = True

            if not self._video_shown:
                win.unmap()
                disp.sync()
                self._update_mirror_ready()
                return

            if w > 1 and h > 1:
                win.configure(x=x, y=y, width=w, height=h,
                              stack_mode=X.Above)
            win.map()
            disp.sync()
        except Exception as exc:
            # A window that died between the search and here is the normal
            # case (rpiplay makes a new one per session), so drop it and let
            # the next poll re-adopt rather than treating it as fatal.
            self._forget_video_window()
            self._embed_note = f"Mirror embed failed: {type(exc).__name__}: {exc}"
        self._update_mirror_ready()

    def _update_mirror_ready(self):
        """Publish mirror-readiness, emitting only on a real transition."""
        ready = self._video_win is not None and self._video_reparented
        if ready != self._mirror_ready:
            self._mirror_ready = ready
            self.changed.emit()

    def _forget_video_window(self):
        self._video_win = None
        self._video_reparented = False
        self._source_w = 0
        self._source_h = 0

    def _begin_video_adoption(self):
        """A phone just connected — start hunting for the window it will map.

        rpiplay builds a fresh video window per session, so anything cached
        from the previous one is stale and must go before the search starts.
        """
        self._forget_video_window()
        self._update_mirror_ready()
        if self._proc is not None and self._proc.poll() is None:
            if not self._adopt_timer.isActive():
                self._adopt_timer.start()

    def _end_video_session(self):
        """Phone gone — take the frozen last frame off the screen.

        The window is unmapped rather than left to the next poll because the
        frame it is still holding is a picture of someone's phone, and it sits
        on top of the whole scene graph until something moves it.
        """
        self._adopt_timer.stop()
        # A fullscreen mirror with no phone behind it is a black screen with
        # an input grab on it, so drop the mode with the session.
        self.setVideoFullscreen(False)
        disp = self._xdisplay()
        win = self._video_win
        if disp is not None and win is not None:
            try:
                win.unmap()
                disp.sync()
            except Exception:
                pass
        self._forget_video_window()
        self._update_mirror_ready()
        self.changed.emit()

    def _try_adopt_video_window(self):
        """Retry adoption until the window exists and is reparented."""
        if self._proc is None or self._proc.poll() is not None:
            self._adopt_timer.stop()
            return
        self._place_video_window()
        if self._video_win is not None and self._video_reparented:
            self._adopt_timer.stop()

    @pyqtProperty(str, notify=changed)
    def embedNote(self):
        """Why the mirror is in its own window, "" when it is embedded."""
        return self._embed_note

    # ── slots ─────────────────────────────────────────────────────────────

    def _launch_env(self):
        """Environment for rpiplay, with a soname shim if one is needed.

        Distributions disagree about libplist's soname: a binary built against
        libplist-2.0.so.3 dies with exit 127 on a box that ships .so.4, even
        though the two are compatible enough to run. Rather than touching
        /usr/lib, drop symlinks in Installation/libshim/ and put that on
        LD_LIBRARY_PATH for this child process only. No-op when the loader is
        already satisfied.
        """
        env = dict(os.environ)
        try:
            binary = shutil.which("rpiplay")
            if not binary:
                return env
            out = subprocess.run(["ldd", binary], capture_output=True, text=True, timeout=5).stdout
            missing = [
                line.split("=>")[0].strip()
                for line in out.splitlines()
                if "not found" in line
            ]
            if not missing:
                return env
            shim = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "Installation", "libshim")
            os.makedirs(shim, exist_ok=True)
            libdirs = ["/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu",
                       "/usr/lib/arm-linux-gnueabihf", "/usr/lib", "/usr/local/lib"]
            linked = []
            for soname in missing:
                # libfoo-2.0.so.3 -> match any libfoo-2.0.so.N present
                stem = soname.rsplit(".so.", 1)[0] + ".so."
                target = None
                for d in libdirs:
                    try:
                        cands = sorted(f for f in os.listdir(d) if f.startswith(stem))
                    except OSError:
                        continue
                    if cands:
                        target = os.path.join(d, cands[-1])
                        break
                if target:
                    link = os.path.join(shim, soname)
                    if not os.path.islink(link) or os.readlink(link) != target:
                        if os.path.lexists(link):
                            os.remove(link)
                        os.symlink(target, link)
                    linked.append(f"{soname} -> {os.path.basename(target)}")
            if linked:
                self._last_log = "library shim: " + ", ".join(linked)
                prev = env.get("LD_LIBRARY_PATH", "")
                env["LD_LIBRARY_PATH"] = shim + (os.pathsep + prev if prev else "")
        except Exception as e:
            print(f"WARNING: rpiplay shim probe failed: {e}", flush=True)
        return env

    # ── volume / ducking ──────────────────────────────────────────────────
    def set_music_bridge(self, bridge):
        self._music_bridge = bridge

    @pyqtProperty(int, notify=changed)
    def volume(self):
        """Stream volume for the AirPlay audio, 0-150%."""
        return self._volume

    @pyqtSlot(int)
    def setVolume(self, value):
        vol = max(0, min(150, int(value)))
        if vol == self._volume:
            return
        self._volume = vol
        settings_cache.set_value("AirPlay", "volume", str(vol), path=DATA_SETTINGS)
        self._apply_volume_async()
        self.changed.emit()

    def _apply_volume_async(self):
        """Push the stored volume onto rpiplay's sink-inputs, off-thread."""
        vol = self._volume

        def _work():
            if not self._vol_lock.acquire(blocking=False):
                return
            try:
                from Modules.Music.audio_router import (
                    get_sink_input_indices_by_app, set_sink_input_linear,
                )
                for idx in get_sink_input_indices_by_app("rpiplay"):
                    set_sink_input_linear(idx, vol / 100.0)
            except Exception:
                pass
            finally:
                self._vol_lock.release()

        threading.Thread(target=_work, daemon=True, name="AirPlayVolume").start()

    @pyqtProperty(int, notify=changed)
    def duckPercent(self):
        """How far music drops while an AirPlay stream is playing, 0-100%."""
        return max(0, min(100, settings_cache.getint(
            "AirPlay", "duck_percent", fallback=35, path=DATA_SETTINGS
        )))

    @pyqtSlot(int)
    def setDuckPercent(self, value):
        pct = max(0, min(100, int(value)))
        settings_cache.set_value("AirPlay", "duck_percent", str(pct), path=DATA_SETTINGS)
        # Re-apply live so the slider is audible while a stream is running.
        if self._ducked and self._music_bridge is not None:
            try:
                if pct <= 0:
                    self._music_bridge.releaseExternalDuck()
                    self._ducked = False
                else:
                    self._music_bridge.duckForExternal(pct)
            except Exception:
                pass
        self.changed.emit()

    @pyqtProperty(bool, notify=changed)
    def musicDucked(self):
        return self._ducked

    def _duck_music(self):
        if self._ducked or self._music_bridge is None:
            return
        pct = self.duckPercent
        if pct <= 0:
            return
        try:
            self._music_bridge.duckForExternal(pct)
            self._ducked = True
        except Exception:
            return
        self.changed.emit()

    def _unduck_music(self):
        if not self._ducked or self._music_bridge is None:
            return
        try:
            self._music_bridge.releaseExternalDuck()
        except Exception:
            pass
        self._ducked = False
        self.changed.emit()

    @pyqtSlot()
    def start(self):
        if self._proc and self._proc.poll() is None:
            return
        self._last_error = ""
        self._last_log = ""
        self._exit_code = 0
        if shutil.which("rpiplay") is None:
            self._running = False
            self._last_error = "rpiplay not found on PATH."
            self.changed.emit()
            return
        name = settings_cache.get(
            "AirPlay", "device_name", fallback="LaLaDash", path=DATA_SETTINGS
        ) or "LaLaDash"
        cmd = ["rpiplay", "-n", name, "-l"]
        # Name the renderers explicitly rather than trusting the default —
        # rpiplay's default is whichever was compiled in first, and on a build
        # that still carries `dummy` that default is a black screen.
        video = self._pick_renderer("video")
        audio = self._pick_renderer("audio")
        if not settings_cache.getboolean("AirPlay", "video_enabled", fallback=True, path=DATA_SETTINGS):
            # Audio-only. rpiplay has no "video off" switch; the dummy video
            # renderer *is* the switch. (This used to append a bare `-a`,
            # which takes a hdmi|analog|off argument it never got.)
            video = "dummy"
        if video:
            cmd += ["-vr", video]
        if audio:
            cmd += ["-ar", audio]
        self._command_line = " ".join(cmd)
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=self._launch_env(),
            )
            self._running = True
            threading.Thread(target=self._read_output, daemon=True).start()
            if not self._timer.isActive():
                self._timer.start()
            self.changed.emit()
        except FileNotFoundError:
            self._running = False
            self._last_error = "rpiplay not found on PATH."
            self.changed.emit()
        except Exception as e:
            self._running = False
            self._last_error = f"AirPlay start failed: {e}"
            self.changed.emit()

    @pyqtSlot()
    def stop(self):
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        self.setVideoFullscreen(False)
        self._forget_video_window()
        self._unduck_music()
        self._running = False
        self._active = False
        self._device_name = ""
        if self._timer.isActive():
            self._timer.stop()
        if self._adopt_timer.isActive():
            self._adopt_timer.stop()
        self._mirror_ready = False
        self.changed.emit()

    # ── internal ──────────────────────────────────────────────────────────

    def _poll(self):
        changed = False

        # Check process still alive
        if self._proc is not None:
                alive = self._proc.poll() is None
                if alive != self._running:
                    self._running = alive
                    if not alive:
                        try:
                            self._exit_code = int(self._proc.returncode or 0)
                        except Exception:
                            self._exit_code = 0
                        if self._exit_code:
                            self._last_error = f"rpiplay exited with code {self._exit_code}"
                        self._active = False
                        self._device_name = ""
                changed = True

        # Detect active mirror stream via PulseAudio
        try:
            from Modules.Music.audio_router import get_sink_input_indices_by_app
            now_active = len(get_sink_input_indices_by_app("rpiplay")) > 0
            if now_active != self._active:
                self._active = now_active
                if now_active:
                    # The stream's sink-input only exists once audio is
                    # flowing, so this is the first moment its volume can be
                    # set — and the duck is armed here too, because an external
                    # mirror never produces a "client connected" log line.
                    self._apply_volume_async()
                    self._duck_music()
                else:
                    self._device_name = ""
                    self._unduck_music()
                changed = True
        except Exception:
            pass

        # rpiplay only creates its video window once a phone connects, and
        # creates a new one per session, so adoption is retried here rather
        # than done once at start.
        if self._proc is not None and self._proc.poll() is None:
            if not self._video_win:
                self._place_video_window()
        elif self._video_win:
            self._forget_video_window()
        self._update_mirror_ready()

        if changed:
            self.changed.emit()

        # Nothing left to watch — stop the 3 s wake-up loop.
        if self._proc is None and not self._active and self._timer.isActive():
            self._timer.stop()

    def _read_output(self):
        """Parse rpiplay stdout for client connect/disconnect lines."""
        try:
            for line in self._proc.stdout:
                text = line.strip()
                if text:
                    self._last_log = text
                low = line.lower()
                if "client" in low and "connected" in low:
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        self._device_name = parts[1].strip()
                    # Adopt the incoming video window now rather than on the
                    # next 3 s poll, which is long enough for it to be seen
                    # floating over whatever page is on screen.
                    self._clientArrived.emit()
                    self.changed.emit()
                elif "client" in low and "disconnect" in low:
                    self._device_name = ""
                    # rpiplay leaves its window up holding the last decoded
                    # frame, so without this the dashboard keeps showing a
                    # frozen phone screen after the phone is long gone.
                    self._clientLeft.emit()
                    self.changed.emit()
                elif text:
                    self.changed.emit()
        except Exception:
            pass
