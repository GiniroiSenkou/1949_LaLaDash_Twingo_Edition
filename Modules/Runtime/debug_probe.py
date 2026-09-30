"""Dev-only introspection channel for LaLaDash.

Enabled only when ``LALADASH_DEBUG=1``. ``main.py`` imports this module from
inside that guard, so a normal run never loads it and pays nothing.

Transport is a ``QLocalServer`` on ``$XDG_RUNTIME_DIR/laladash-debug.sock``,
driven by ``newConnection``/``readyRead`` — no polling timer, per the project
convention of preferring events over ``QTimer``. One command per connection;
the server replies and closes, so multi-line replies need no framing.

Talk to it with ``Tools/probe_client.py``:

    LALADASH_DEBUG=1 python3 main.py &
    python3 Tools/probe_client.py grab /tmp/shot.png
    python3 Tools/probe_client.py page 4
    python3 Tools/probe_client.py stats

Commands:
    ping            -> "pong"
    grab [path]     -> "OK <path> <w>x<h>"; PNG of the live window
    page <n>        -> navigate the StackLayout (0..6)
    sim on|off [rpm] [speed]
                    -> drive the telemetry simulator (needed to hold Dashboard)
    music play <substring> | restore | toggle | next | prev
                    -> drive playback (track-dependent UI needs a loaded track)
    screensaver parked|driving|off
                    -> show a screensaver without waiting out the idle timer
    lights on <key,...> | off
                    -> force dash-light overrides (warning UI needs a fault)
    stats           -> JSON: frame timing, stall count, page, CPU/RAM/GPU
    stacks          -> Python stack of every live thread
    eval <js>       -> evaluate a JS expression on the root window (bridges
                       and root ids in scope): e.g. `eval Can.liveIds.length`

When the main thread wedges, the socket cannot answer — the server runs on
that same thread. For that case ``SIGUSR1`` dumps every thread's stack to
stderr straight from the C signal handler, which needs no event loop:

    kill -USR1 $(pgrep -f "python3 main.py")
"""

import faulthandler
import json
import os
import signal
import sys
import threading
import time
import traceback
from collections import deque

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtNetwork import QLocalServer
from PyQt6.QtQuick import QQuickWindow

try:  # PyQt6 >= 6.x ships sip as a submodule; older layouts expose it top-level
    from PyQt6 import sip
except ImportError:  # pragma: no cover
    import sip

# Keep ~4 s of frames at 60 Hz.
_FRAME_HISTORY = 240
# The stall detector measures its own lateness; anything beyond the threshold
# means the Qt event loop was blocked and could not service the timer.
_STALL_TIMER_MS = 250
_STALL_THRESHOLD_MS = 150


def install_signal_dump():
    """SIGUSR1 -> all-thread traceback on stderr.

    The `stacks` command covers the normal case, but it is served from the Qt
    event loop, so it is useless in exactly the situation where a stack dump
    matters most: a main thread stuck in a long C call or a busy loop.
    faulthandler writes from the signal handler itself and so still works.
    """
    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
    except Exception as exc:            # not available on every platform
        print(f"[debug_probe] SIGUSR1 dump unavailable: {exc}", flush=True)


def socket_path() -> str:
    """Absolute path of the control socket.

    ``LALADASH_DEBUG_SOCK`` overrides it, which is what lets several dev
    instances run side by side without fighting over one socket.
    """
    override = os.environ.get("LALADASH_DEBUG_SOCK")
    if override:
        return override
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(runtime, "laladash-debug.sock")


class DebugProbe(QObject):
    """Introspection server bound to the live QQuickWindow and bridge set."""

    def __init__(self, window, bridges, parent=None):
        super().__init__(parent)
        # QQmlApplicationEngine.rootObjects() hands back the ApplicationWindow
        # wrapped as a plain QWindow, which has no grabWindow(). Re-cast it to
        # the real type so the scene-graph API is reachable.
        if not isinstance(window, QQuickWindow):
            try:
                window = sip.cast(window, QQuickWindow)
            except Exception as exc:
                print(f"[debug_probe] could not cast root to QQuickWindow: {exc}",
                      flush=True)
        self._window = window
        self._bridges = bridges or {}

        # ── Frame timing: one counter increment per presented frame ────────
        self._frames = deque(maxlen=_FRAME_HISTORY)
        self._last_frame = None
        self._frame_count = 0
        try:
            window.frameSwapped.connect(self._on_frame_swapped)
        except Exception as exc:  # pragma: no cover - platform dependent
            print(f"[debug_probe] no frameSwapped signal: {exc}", flush=True)

        # ── Main-thread stall detector ─────────────────────────────────────
        self._stalls = []
        self._stall_due = time.perf_counter() + _STALL_TIMER_MS / 1000.0
        self._stall_timer = QTimer(self)
        self._stall_timer.setInterval(_STALL_TIMER_MS)
        self._stall_timer.timeout.connect(self._on_stall_tick)
        self._stall_timer.start()

        # ── Control socket ─────────────────────────────────────────────────
        path = socket_path()
        QLocalServer.removeServer(path)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        if self._server.listen(path):
            print(f"[debug_probe] listening on {path}", flush=True)
        else:
            print(
                f"[debug_probe] listen failed on {path}: "
                f"{self._server.errorString()}",
                flush=True,
            )

    # ── Instruments ────────────────────────────────────────────────────────

    def _on_frame_swapped(self):
        now = time.perf_counter()
        if self._last_frame is not None:
            self._frames.append((now - self._last_frame) * 1000.0)
        self._last_frame = now
        self._frame_count += 1

    def _on_stall_tick(self):
        now = time.perf_counter()
        late_ms = (now - self._stall_due) * 1000.0
        self._stall_due = now + _STALL_TIMER_MS / 1000.0
        if late_ms > _STALL_THRESHOLD_MS:
            # The blocking call has already returned by the time we run, so a
            # stack here would only show this timer. Record when and how long;
            # use `stacks` while the app is actually hung to see the culprit.
            self._stalls.append({"at": time.strftime("%H:%M:%S"),
                                 "late_ms": round(late_ms)})
            del self._stalls[:-50]
            print(f"[debug_probe][STALL] event loop blocked {late_ms:.0f} ms",
                  flush=True)

    # ── Commands ───────────────────────────────────────────────────────────

    def _cmd_ping(self, _arg):
        return "pong"

    def _cmd_grab(self, arg):
        path = arg or os.path.join(
            os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "laladash-grab.png")
        grab = getattr(self._window, "grabWindow", None)
        if grab is None:
            return ("ERR root window is a plain QWindow; grabWindow() is "
                    "unavailable and the QQuickWindow cast failed")
        image = grab()
        if image is None or image.isNull():
            return "ERR grabWindow() returned a null image"
        if not image.save(path, "PNG"):
            return f"ERR could not write {path}"
        return f"OK {path} {image.width()}x{image.height()}"

    def _cmd_page(self, arg):
        nav = self._bridges.get("navigation")
        if nav is None:
            return "ERR no navigation bridge"
        try:
            index = int(arg)
        except (TypeError, ValueError):
            return "ERR page needs an integer 0..6"
        nav.navigateTo(index)
        return f"OK page {index}"

    def _cmd_sim(self, arg):
        """Drive the telemetry simulator so engine-dependent pages stay put.

        With no OBD and no sim, NavigationBridge treats telemetry as
        unavailable and snaps back to Home, which makes the Dashboard page
        impossible to inspect.
        """
        from Modules.Obd.telemetry import TelemetryHub

        parts = arg.split()
        if not parts:
            return "ERR usage: sim on|off [rpm] [speed]"
        hub = TelemetryHub.instance()
        wanted = parts[0].lower() in ("on", "1", "true", "yes")
        if not hub.set_sim_enabled(wanted):
            return "ERR sim refused (a real OBD connection is active)"
        for name, value in zip(("RPM", "Speed"), parts[1:3]):
            try:
                hub.set_sim_value(name, float(value))
            except ValueError:
                return f"ERR {name} value {value!r} is not a number"
        return (f"OK sim {'on' if wanted else 'off'} "
                f"rpm={hub.get_sim_value('RPM')} "
                f"speed={hub.get_sim_value('Speed')}")

    def _cmd_music(self, arg):
        """Drive playback so track-dependent UI can be inspected.

        Several parts of the music page only exist while a track is loaded —
        the tag row, the stems dot, the mode cycler — and there is otherwise no
        way to get a specific track playing from outside the app.

            music play <substring>   first library track matching, case-insensitive
            music restore            resume the saved session
            music toggle | next | prev
        """
        music = self._bridges.get("music")
        if music is None:
            return "ERR music bridge unavailable"

        parts = arg.split(None, 1)
        if not parts:
            return "ERR usage: music play <substring> | restore | toggle | next | prev"
        verb = parts[0].lower()

        if verb in ("restore", "toggle", "next", "prev"):
            slot = {"restore": "restoreSession", "toggle": "togglePlayPause",
                    "next": "next", "prev": "prev"}[verb]
            getattr(music, slot)()
            return f"OK music {verb}"

        if verb != "play":
            return f"ERR unknown music verb {verb!r}"
        if len(parts) < 2:
            return "ERR usage: music play <substring>"

        needle = parts[1].strip().lower()
        tracks = music.visibleTracks
        for index, track in enumerate(tracks):
            haystack = " ".join(str(track.get(k, "")) for k in
                                ("title", "artist", "fullTitle")).lower()
            if needle in haystack:
                music.selectFile(index)
                return f"OK music play [{index}] {track.get('fullTitle') or track.get('title')}"
        return f"ERR no track matching {needle!r} in {len(tracks)} visible tracks"

    def _cmd_screensaver(self, arg):
        """Show/hide the screensaver, and pick which one.

        Otherwise it is only reachable by leaving the app untouched for
        `[UI] screensaver_timeout_sec` (94 s by default), which makes the
        driving screensaver impractical to inspect — it also needs the sim
        running to have any speed or rpm to draw.

            screensaver parked | driving   show that one
            screensaver off                hide
        """
        nav = self._bridges.get("navigation")
        if nav is None:
            return "ERR navigation bridge unavailable"
        mode = (arg or "").strip().lower()
        if mode in ("off", "hide", "0"):
            nav.hideScreensaver()
            return "OK screensaver off"
        if mode not in ("parked", "driving"):
            return "ERR usage: screensaver parked|driving|off"
        nav.setScreensaverMode(mode)
        nav.showScreensaver()
        return f"OK screensaver {mode}"

    def _cmd_lights(self, arg):
        """Force dash-light overrides so warning UI can be inspected.

        The screensaver's warning strip and the Dashboard's light grid both
        render `Telemetry.activeDashLightKeys`, which only fills up when the
        car actually has a fault — untestable on a desk. This drives the same
        override path Settings > Debugger uses.

            lights on engine,oil,seatbelt   force those on
            lights off                      clear every override
        """
        from Modules.Layout.dash_lights_hub import DashLightsHub

        parts = arg.split(None, 1)
        hub = DashLightsHub.instance()
        if not parts or parts[0].lower() in ("off", "clear"):
            hub.clear_overrides()
            return "OK lights cleared"
        if parts[0].lower() != "on" or len(parts) < 2:
            return "ERR usage: lights on <key,key,...> | off"
        keys = [k.strip() for k in parts[1].replace(" ", ",").split(",") if k.strip()]
        for key in keys:
            hub.set_override(key, True)
        return f"OK lights on {','.join(keys)}"

    def _cmd_stats(self, _arg):
        frames = list(self._frames)
        data = {
            "frames_presented": self._frame_count,
            "frame_samples": len(frames),
            "stalls_recent": self._stalls[-10:],
            "stall_count": len(self._stalls),
        }
        if frames:
            ordered = sorted(frames)
            count = len(ordered)
            mean = sum(frames) / count
            data.update({
                "fps_mean": round(1000.0 / mean, 1) if mean else None,
                "frame_ms_mean": round(mean, 2),
                "frame_ms_p50": round(ordered[count // 2], 2),
                "frame_ms_p95": round(ordered[min(count - 1, int(count * 0.95))], 2),
                "frame_ms_max": round(ordered[-1], 2),
                "frames_over_20ms": sum(1 for f in frames if f > 20.0),
            })
        nav = self._bridges.get("navigation")
        if nav is not None:
            try:
                data["page"] = nav.currentPage
                data["page_key"] = nav.currentPageKey
                # Reported because an active screensaver is the usual reason
                # for a mystery idle-frame count: its clock ticks once a
                # second, and if it activated while you were not looking, the
                # frames are real and correct rather than a leak.
                data["screensaver"] = (nav.screensaverMode
                                       if nav.screensaverActive else "off")
                data["side_panel"] = nav.sidePanelOpen
            except Exception:
                pass
        # Reuse the existing 2 s worker-thread cache rather than adding another.
        sysinfo = getattr(self._bridges.get("settings"), "_sysinfo", None)
        if sysinfo is not None:
            for key in ("cpu_pct", "app_ram", "app_threads", "gpu_usage",
                        "gpu_temp"):
                try:
                    data[key] = sysinfo.get(key)
                except Exception:
                    pass
        return json.dumps(data, indent=2)

    def _cmd_stacks(self, _arg):
        names = {t.ident: t.name for t in threading.enumerate()}
        out = []
        for ident, frame in sys._current_frames().items():
            out.append(f"--- {names.get(ident, '?')} (tid {ident}) ---")
            out.append("".join(traceback.format_stack(frame)))
        return "\n".join(out)

    def _cmd_eval(self, arg):
        """Evaluate a JS expression against the window's root object — the
        bridges are in scope as context properties (`Settings`, `Can`, …)
        and any id on the root item resolves. Dev-only; how the headless
        harness opens sheets, sets `activeSection`, or reads a property."""
        from PyQt6.QtQml import QQmlExpression, QQmlEngine, qmlContext
        root = self._window
        scope = root
        # "@objectName expr" evaluates with that child as `this`/scope, so
        # ids private to a page or sheet (e.g. `opened`) are reachable.
        if arg.startswith("@"):
            name, _, arg = arg[1:].partition(" ")
            scope = root.findChild(QObject, name)
            if scope is None:
                return f"ERR no object named {name!r}"
        ctx = qmlContext(scope) or qmlContext(root)
        if ctx is None:
            return "ERR no QML context on the root window"
        expr = QQmlExpression(ctx, scope, arg or "undefined")
        value, is_undefined = expr.evaluate()
        if expr.hasError():
            return "ERR " + expr.error().toString()
        try:
            return "OK " + json.dumps(value, default=str)
        except Exception:
            return "OK " + repr(value)

    _HANDLERS = {
        "ping": _cmd_ping,
        "grab": _cmd_grab,
        "eval": _cmd_eval,
        "page": _cmd_page,
        "sim": _cmd_sim,
        "music": _cmd_music,
        "screensaver": _cmd_screensaver,
        "lights": _cmd_lights,
        "stats": _cmd_stats,
        "stacks": _cmd_stacks,
    }

    def _dispatch(self, line):
        name, _, arg = line.partition(" ")
        handler = self._HANDLERS.get(name.strip().lower())
        if handler is None:
            known = ", ".join(sorted(self._HANDLERS))
            return f"ERR unknown command {name!r} (known: {known})"
        try:
            return handler(self, arg.strip())
        except Exception:
            return "ERR " + traceback.format_exc(limit=4)

    # ── Socket plumbing ────────────────────────────────────────────────────

    def _on_new_connection(self):
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            sock.readyRead.connect(lambda s=sock: self._on_ready_read(s))
            sock.disconnected.connect(sock.deleteLater)

    def _on_ready_read(self, sock):
        while sock.canReadLine():
            line = bytes(sock.readLine()).decode("utf-8", "replace").strip()
            if not line:
                continue
            sock.write((self._dispatch(line) + "\n").encode("utf-8"))
            sock.flush()
            # One command per connection: closing frames the reply for us.
            sock.disconnectFromServer()

    def stop(self):
        try:
            self._stall_timer.stop()
        except Exception:
            pass
        try:
            self._server.close()
            QLocalServer.removeServer(socket_path())
        except Exception:
            pass
