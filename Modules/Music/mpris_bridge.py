# Modules/mpris_bridge.py
# MPRIS2 bridge for LaLaDash – Variant-safe + Debug toggle (Pi OS safe)
from __future__ import annotations

import os
import time
import threading
import configparser
from typing import Any, Dict, Optional

from gi.repository import GLib  # type: ignore
from pydbus import SessionBus   # type: ignore
from pydbus.generic import signal as dbus_signal  # type: ignore

# NOTE: keep bridge resilient.
try:
    from preferences import DATA_SETTINGS  # type: ignore
except Exception:
    DATA_SETTINGS = "Data/settings.ini"

MPRIS_BUS_NAME = "org.mpris.MediaPlayer2.LaLaDash"
MPRIS_OBJ_PATH = "/org/mpris/MediaPlayer2"


# ---------- Debug flag (settings.ini) ----------
_DBG_CACHE = {"t": 0.0, "enabled": False}

# Retries at 0.5 s each, so this is ~10 s of the name staying busy before it
# stops being a handover and starts being worth mentioning.
_BUSY_WARN_AFTER = 20


def _mpris_debug_enabled() -> bool:
    """
    Reads:
      [Debug]
      mpris_debug = 1/0
    Cached briefly to avoid hammering SD card.
    """
    now = time.time()
    if now - _DBG_CACHE["t"] < 0.5:
        return bool(_DBG_CACHE["enabled"])

    enabled = False
    try:
        cfg = configparser.ConfigParser()
        if os.path.exists(DATA_SETTINGS):
            cfg.read(DATA_SETTINGS, encoding="utf-8")
        enabled = (cfg.get("Debug", "mpris_debug", fallback="0") == "1")
    except Exception:
        enabled = False

    _DBG_CACHE["t"] = now
    _DBG_CACHE["enabled"] = enabled
    return enabled

def _dbg(msg: str):
    if _mpris_debug_enabled():
        print(f"🧪 [MPRIS-DBG] {msg}", flush=True)

def _command_log_path() -> str:
    base = os.path.dirname(os.path.abspath(DATA_SETTINGS or "Data/settings.ini"))
    return os.path.join(base, "mpris_commands.log")

# Set by MusicBridge so Settings → Steering Keys can show the buttons that
# arrive over AVRCP. BlueZ hands passthrough commands to the *registered MPRIS
# player* (us, through mpris-proxy) instead of injecting them into the
# /dev/input node MediaKeyListener reads. On a head unit that forwards only
# media transport — a factory head unit such as Renault's R-Link is one — these calls are the only
# evidence a wheel button was pressed at all.
_command_observer = None


def set_command_observer(fn) -> None:
    """Register a callable(name) invoked for every incoming transport command."""
    global _command_observer
    _command_observer = fn


def _log_command(name: str):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + f" {name}\n"
    try:
        path = _command_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception:
        pass
    _dbg(f"<- {name}")
    obs = _command_observer
    if obs is not None:
        try:
            obs(name)
        except Exception:
            pass


# ---------- Helpers ----------
def _clamp(x: float, a: float, b: float) -> float:
    return max(a, min(b, x))


def ensure_session_bus_env() -> str:
    """Make sure DBUS_SESSION_BUS_ADDRESS points at the real per-user bus.

    LaLaDash runs headless under eglfs with no desktop session. If this var
    is unset, GLib auto-launches a *private throwaway* session bus that only
    the LaLaDash process can see — mpris-proxy (which runs on the standard
    XDG user bus) then never discovers the org.mpris.MediaPlayer2.LaLaDash
    player and the car head unit sees nothing.

    Pin the variable (in os.environ, so child processes inherit it too) to
    the XDG runtime user bus so the bridge and mpris-proxy share one bus.
    Returns the resolved address, or "" if no real bus socket exists yet
    (e.g. early in boot) — the caller must NOT publish in that case, or it
    lands on an auto-launched private bus mpris-proxy can never see.
    """
    def _sock_from_addr(a: str) -> str:
        # Accept the first "unix:path=..." token of a (possibly multi-) address.
        for part in a.split(","):
            part = part.strip()
            if part.startswith("unix:path="):
                return part[len("unix:path="):].split(";")[0]
        return ""

    # 1) Honour an already-pinned address only if its socket really exists.
    addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if addr:
        sock = _sock_from_addr(addr)
        if not sock or os.path.exists(sock):
            return addr  # abstract sockets (no path) or live socket — trust it
        print(f"[MPRIS] DBUS_SESSION_BUS_ADDRESS points at missing socket {sock!r}; re-resolving")

    # 2) Resolve the standard XDG per-user bus socket.
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        try:
            runtime = f"/run/user/{os.getuid()}"
        except Exception:
            runtime = ""

    for cand in (runtime, f"/run/user/{os.getuid()}" if hasattr(os, "getuid") else ""):
        if not cand:
            continue
        os.environ.setdefault("XDG_RUNTIME_DIR", cand)
        sock = os.path.join(cand, "bus")
        if os.path.exists(sock):
            addr = f"unix:path={sock}"
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = addr
            return addr

    print("[MPRIS] NO SESSION BUS FOUND — will retry; not publishing on a private bus")
    return ""


# ---------- Variant helpers (NONE-PROOF) ----------
def _v_s(x) -> GLib.Variant:
    # Never allow None into GLib.Variant
    return GLib.Variant("s", "" if x is None else str(x))

def _v_b(x: bool) -> GLib.Variant:
    return GLib.Variant("b", bool(x))

def _v_d(x) -> GLib.Variant:
    # Never allow None
    return GLib.Variant("d", 0.0 if x is None else float(x))

def _v_x(x) -> GLib.Variant:
    # Never allow None
    return GLib.Variant("x", 0 if x is None else int(x))

def _v_as(xs) -> GLib.Variant:
    # Never allow None + never allow None elements
    if not xs:
        return GLib.Variant("as", [])
    safe = [str(s) for s in xs if s is not None]
    return GLib.Variant("as", safe)


def _wrap_metadata(md_raw: Dict[str, Any]) -> GLib.Variant:
    """
    MPRIS Metadata is itself a{sv}. Values must be Variants too.
    Common keys:
      xesam:title   -> s
      xesam:artist  -> as
      xesam:album   -> s
      mpris:artUrl  -> s
      mpris:length  -> x  (nanoseconds)
      mpris:trackid -> o  (object path)  <-- important for some AVRCP clients
    """
    out: Dict[str, GLib.Variant] = {}
    for k, v in (md_raw or {}).items():
        # Critical: skip None completely (no Variant(None))
        if v is None:
            continue
        try:
            if k == "xesam:artist":
                if isinstance(v, list):
                    out[k] = _v_as(v)
                else:
                    out[k] = _v_as([str(v)] if v else [])
            elif k == "mpris:length":
                out[k] = _v_x(v)
            elif k == "mpris:trackid":
                # object path variant: must not be None/empty
                trackid = str(v) if v else f"{MPRIS_OBJ_PATH}/track/0"
                out[k] = GLib.Variant("o", trackid)
            else:
                out[k] = _v_s(v)
        except Exception:
            # drop bad entries rather than crashing
            pass

    return GLib.Variant("a{sv}", out)


class _MPRISObject:
    """
    DBus object exported at /org/mpris/MediaPlayer2 implementing:
      - org.mpris.MediaPlayer2
      - org.mpris.MediaPlayer2.Player
      - org.freedesktop.DBus.Properties
      - org.freedesktop.DBus.Introspectable
    """

    IFACE_ROOT   = "org.mpris.MediaPlayer2"
    IFACE_PLAYER = "org.mpris.MediaPlayer2.Player"
    IFACE_PROPS  = "org.freedesktop.DBus.Properties"
    IFACE_INTRO  = "org.freedesktop.DBus.Introspectable"

    # pydbus reads this class-level attribute to register interfaces + methods.
    # Without it the object is NOT exported and mpris-proxy cannot call any method.
    dbus = """<node>
  <interface name="org.mpris.MediaPlayer2">
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
    <method name="Raise"/>
    <method name="Quit"/>
  </interface>

  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek">
      <arg direction="in" name="Offset" type="x"/>
    </method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri">
      <arg direction="in" name="Uri" type="s"/>
    </method>

    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>

  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get">
      <arg direction="in" name="interface_name" type="s"/>
      <arg direction="in" name="property_name" type="s"/>
      <arg direction="out" name="value" type="v"/>
    </method>
    <method name="GetAll">
      <arg direction="in" name="interface_name" type="s"/>
      <arg direction="out" name="properties" type="a{sv}"/>
    </method>
    <method name="Set">
      <arg direction="in" name="interface_name" type="s"/>
      <arg direction="in" name="property_name" type="s"/>
      <arg direction="in" name="value" type="v"/>
    </method>
    <signal name="PropertiesChanged">
      <arg name="interface_name" type="s"/>
      <arg name="changed_properties" type="a{sv}"/>
      <arg name="invalidated_properties" type="as"/>
    </signal>
  </interface>

  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect">
      <arg direction="out" name="xml_data" type="s"/>
    </method>
  </interface>
</node>
"""

    PropertiesChanged = dbus_signal()

    def __init__(self, controller: Any, identity: str = "LaLaDash"):
        self.c = controller
        self._identity = identity

        # Root interface
        self._has_tracklist = False

        # Player state
        self._playback_status = "Stopped"  # "Playing"|"Paused"|"Stopped"
        self._rate = 1.0
        self._min_rate = 1.0
        self._max_rate = 1.0

        self._metadata_raw: Dict[str, Any] = {}
        self._metadata_sig: str = ""   # signature of last-emitted metadata
        self._volume = 1.0
        self._last_pos_ns = 0

        # Capabilities
        self._can_go_next = True
        self._can_go_prev = True
        self._can_play     = True
        self._can_pause    = True
        self._can_seek     = True
        self._can_control  = True

    # -------- Introspection --------
    def Introspect(self) -> str:
        return type(self).dbus

    # -------- Properties wiring --------
    def _get_all_raw(self, iface: str) -> Dict[str, Any]:
        if iface == self.IFACE_ROOT:
            return {
                "CanQuit": False,
                "CanRaise": False,
                "HasTrackList": self._has_tracklist,
                "Identity": self._identity,
                "SupportedUriSchemes": [],
                "SupportedMimeTypes": [],
            }
        if iface == self.IFACE_PLAYER:
            return {
                "PlaybackStatus": self._playback_status,
                "Rate": self._rate,
                "Metadata": dict(self._metadata_raw),
                "Volume": float(self._volume),
                "Position": int(self.Position),
                "MinimumRate": self._min_rate,
                "MaximumRate": self._max_rate,
                "CanGoNext": self._can_go_next,
                "CanGoPrevious": self._can_go_prev,
                "CanPlay": self._can_play,
                "CanPause": self._can_pause,
                "CanSeek": self._can_seek,
                "CanControl": self._can_control,
            }
        return {}

    def _wrap_all_props(self, iface: str) -> Dict[str, GLib.Variant]:
        raw = self._get_all_raw(iface)
        if iface == self.IFACE_ROOT:
            return {
                "CanQuit": _v_b(raw["CanQuit"]),
                "CanRaise": _v_b(raw["CanRaise"]),
                "HasTrackList": _v_b(raw["HasTrackList"]),
                "Identity": _v_s(raw["Identity"]),
                "SupportedUriSchemes": _v_as(raw["SupportedUriSchemes"]),
                "SupportedMimeTypes": _v_as(raw["SupportedMimeTypes"]),
            }
        if iface == self.IFACE_PLAYER:
            return {
                "PlaybackStatus": _v_s(raw["PlaybackStatus"]),
                "Rate": _v_d(raw["Rate"]),
                "Metadata": _wrap_metadata(raw["Metadata"]),
                "Volume": _v_d(raw["Volume"]),
                "Position": _v_x(raw["Position"]),
                "MinimumRate": _v_d(raw["MinimumRate"]),
                "MaximumRate": _v_d(raw["MaximumRate"]),
                "CanGoNext": _v_b(raw["CanGoNext"]),
                "CanGoPrevious": _v_b(raw["CanGoPrevious"]),
                "CanPlay": _v_b(raw["CanPlay"]),
                "CanPause": _v_b(raw["CanPause"]),
                "CanSeek": _v_b(raw["CanSeek"]),
                "CanControl": _v_b(raw["CanControl"]),
            }
        return {}

    def _wrap_single_prop(self, iface: str, prop: str) -> Optional[GLib.Variant]:
        if iface == self.IFACE_ROOT:
            mp = {
                "CanQuit": ("b", False),
                "CanRaise": ("b", False),
                "HasTrackList": ("b", self._has_tracklist),
                "Identity": ("s", self._identity),
                "SupportedUriSchemes": ("as", []),
                "SupportedMimeTypes": ("as", []),
            }
        elif iface == self.IFACE_PLAYER:
            mp = {
                "PlaybackStatus": ("s", self._playback_status),
                "Rate": ("d", self._rate),
                "Metadata": ("a{sv}", _wrap_metadata(self._metadata_raw)),
                "Volume": ("d", float(self._volume)),
                "Position": ("x", int(self.Position)),
                "MinimumRate": ("d", self._min_rate),
                "MaximumRate": ("d", self._max_rate),
                "CanGoNext": ("b", self._can_go_next),
                "CanGoPrevious": ("b", self._can_go_prev),
                "CanPlay": ("b", self._can_play),
                "CanPause": ("b", self._can_pause),
                "CanSeek": ("b", self._can_seek),
                "CanControl": ("b", self._can_control),
            }
        else:
            return None

        if prop not in mp:
            return None

        typ, val = mp[prop]
        if typ == "a{sv}":
            # already variant
            return GLib.Variant("v", val)
        # ensure no None goes in:
        if val is None:
            if typ == "s":
                val = ""
            elif typ == "d":
                val = 0.0
            elif typ == "x":
                val = 0
            elif typ == "b":
                val = False
            elif typ == "as":
                val = []
        return GLib.Variant("v", GLib.Variant(typ, val))

    def Get(self, interface_name: str, property_name: str) -> GLib.Variant:
        v = self._wrap_single_prop(interface_name, property_name)
        if v is None:
            return GLib.Variant("v", _v_s(""))
        return v

    def GetAll(self, interface_name: str) -> Dict[str, GLib.Variant]:
        return self._wrap_all_props(interface_name)

    def Set(self, interface_name: str, property_name: str, value: GLib.Variant):
        # Only Rate and Volume are writable in our minimal impl
        try:
            inner = value.unpack()
        except Exception:
            inner = None

        if interface_name == self.IFACE_PLAYER and property_name == "Rate":
            try:
                if inner is not None:
                    self.Rate = float(inner)
            except Exception:
                pass
        if interface_name == self.IFACE_PLAYER and property_name == "Volume":
            try:
                if inner is not None:
                    self.Volume = float(inner)
            except Exception:
                pass

    def _emit_properties_changed(self, iface: str, changed: Dict[str, Any], invalidated: Optional[list[str]]):
        # Wrap changed -> a{sv}
        ch: Dict[str, GLib.Variant] = {}
        for k, v in (changed or {}).items():
            if v is None:
                # Avoid Variant(None). Skip.
                continue

            if k == "Metadata":
                ch[k] = _wrap_metadata(v or {})
            elif isinstance(v, bool):
                ch[k] = _v_b(v)
            elif isinstance(v, (int, float)):
                # MPRIS expects Volume/Rate as double; Position/length as x.
                if k in ("Position", "mpris:length"):
                    ch[k] = _v_x(v)
                else:
                    ch[k] = _v_d(v)
            else:
                ch[k] = _v_s(v)

        inv = invalidated if isinstance(invalidated, list) else []
        _dbg(f"-> PropertiesChanged iface={iface} keys={list(ch.keys())}")
        self.PropertiesChanged(iface, ch, inv)

    # -------- Root iface --------
    @property
    def CanQuit(self) -> bool: return False
    @property
    def CanRaise(self) -> bool: return False
    @property
    def HasTrackList(self) -> bool: return self._has_tracklist
    @property
    def Identity(self) -> str: return self._identity
    @property
    def SupportedUriSchemes(self): return []
    @property
    def SupportedMimeTypes(self): return []

    def Raise(self):  # optional
        _dbg("<- Raise()")

    def Quit(self):   # optional
        _dbg("<- Quit()")

    # -------- Player iface --------
    @property
    def PlaybackStatus(self) -> str:
        return self._playback_status

    @property
    def Rate(self) -> float:
        return self._rate

    @Rate.setter
    def Rate(self, val: float):
        self._rate = float(val)
        _dbg(f"<- Set Rate={self._rate}")
        self._emit_properties_changed(self.IFACE_PLAYER, {"Rate": self._rate}, [])

    @property
    def Metadata(self) -> Dict[str, Any]:
        return dict(self._metadata_raw)

    @property
    def Volume(self) -> float:
        return self._volume

    @Volume.setter
    def Volume(self, val: float):
        self._volume = _clamp(float(val), 0.0, 1.0)
        _dbg(f"<- Set Volume={self._volume:.3f}")
        try:
            if hasattr(self.c, "set_volume"):
                self.c.set_volume(self._volume)
        except Exception as e:
            _dbg(f"!! controller.set_volume failed: {e}")
        self._emit_properties_changed(self.IFACE_PLAYER, {"Volume": self._volume}, [])

    @property
    def Position(self) -> int:
        try:
            ms = self.c.query_position_ms()
            if ms is not None:
                self._last_pos_ns = int(ms) * 1_000_000
            return int(self._last_pos_ns)
        except Exception:
            return int(self._last_pos_ns)

    @property
    def MinimumRate(self) -> float: return self._min_rate
    @property
    def MaximumRate(self) -> float: return self._max_rate
    @property
    def CanGoNext(self) -> bool: return self._can_go_next
    @property
    def CanGoPrevious(self) -> bool: return self._can_go_prev
    @property
    def CanPlay(self) -> bool: return self._can_play
    @property
    def CanPause(self) -> bool: return self._can_pause
    @property
    def CanSeek(self) -> bool: return self._can_seek
    @property
    def CanControl(self) -> bool: return self._can_control

    # ---- transport commands (incoming from car via mpris-proxy) ----
    def Next(self):
        _log_command("Next")
        try:
            if hasattr(self.c, "next"):
                self.c.next()
            elif hasattr(self.c, "next_track"):
                self.c.next_track()
        except Exception as e:
            _dbg(f"!! controller.next failed: {e}")

    def Previous(self):
        _log_command("Previous")
        try:
            if hasattr(self.c, "previous"):
                self.c.previous()
            elif hasattr(self.c, "previous_track"):
                self.c.previous_track()
        except Exception as e:
            _dbg(f"!! controller.previous failed: {e}")

    def Pause(self):
        _log_command("Pause")
        try:
            if hasattr(self.c, "pause"):
                self.c.pause()
            elif hasattr(self.c, "play_pause"):
                self.c.play_pause()
        except Exception as e:
            _dbg(f"!! controller.pause failed: {e}")

    def PlayPause(self):
        _log_command("PlayPause")
        try:
            if hasattr(self.c, "play_pause"):
                self.c.play_pause()
        except Exception as e:
            _dbg(f"!! controller.play_pause failed: {e}")

    def Stop(self):
        _log_command("Stop")
        try:
            if hasattr(self.c, "stop"):
                self.c.stop()
        except Exception as e:
            _dbg(f"!! controller.stop failed: {e}")

    def Play(self):
        _log_command("Play")
        try:
            if hasattr(self.c, "play"):
                self.c.play()
        except Exception as e:
            _dbg(f"!! controller.play failed: {e}")

    def Seek(self, Offset: int):
        _log_command(f"Seek Offset={Offset}")
        try:
            if hasattr(self.c, "seek_by_us"):
                self.c.seek_by_us(int(Offset))
            elif hasattr(self.c, "seek_by_ms"):
                self.c.seek_by_ms(int(Offset / 1000))
        except Exception as e:
            _dbg(f"!! controller.seek failed: {e}")

    def SetPosition(self, TrackId: str, Position: int):
        _log_command(f"SetPosition TrackId={TrackId} Position={Position}")
        try:
            if hasattr(self.c, "set_position_us"):
                self.c.set_position_us(int(Position))
            elif hasattr(self.c, "set_position_ms"):
                self.c.set_position_ms(int(Position / 1000))
        except Exception as e:
            _dbg(f"!! controller.set_position failed: {e}")

    def OpenUri(self, Uri: str):
        _log_command(f"OpenUri Uri={Uri}")
        try:
            if hasattr(self.c, "open_uri"):
                self.c.open_uri(Uri)
        except Exception as e:
            _dbg(f"!! controller.open_uri failed: {e}")

    # ---- called by your app to push state outward ----
    def set_playback_status(self, status: str):
        status = "Stopped" if status is None else str(status)
        if status != self._playback_status:
            self._playback_status = status
            _dbg(f"-> PlaybackStatus={status}")
            self._emit_properties_changed(self.IFACE_PLAYER, {"PlaybackStatus": status}, [])

    def update_metadata(self, md: Dict[str, Any]):
        md2 = dict(md or {})

        # Remove None values (critical for Variant safety)
        md2 = {k: v for k, v in md2.items() if v is not None}

        # Force a stable trackid if missing/empty
        trackid = md2.get("mpris:trackid")
        if not trackid:
            md2["mpris:trackid"] = f"{MPRIS_OBJ_PATH}/track/0"

        # Suppress redundant PropertiesChanged emits — the 300 ms tick would
        # otherwise spam `Metadata` 3.3 ×/s even while a track is paused,
        # which some car AVRCP head units choke on. Use a content signature
        # since dicts contain non-hashable lists.
        sig = self._metadata_signature(md2)
        if sig == self._metadata_sig:
            self._metadata_raw = md2
            return
        self._metadata_sig = sig
        self._metadata_raw = md2

        _dbg(
            "-> Metadata "
            f"title={md2.get('xesam:title')!r} "
            f"artist={md2.get('xesam:artist')!r} "
            f"album={md2.get('xesam:album')!r} "
            f"artUrl={md2.get('mpris:artUrl')!r} "
            f"len={md2.get('mpris:length')!r} "
            f"trackid={md2.get('mpris:trackid')!r}"
        )
        self._emit_properties_changed(self.IFACE_PLAYER, {"Metadata": self._metadata_raw}, [])

    @staticmethod
    def _metadata_signature(md: Dict[str, Any]) -> str:
        """Stable comparable representation of metadata (lists → tuples,
        keys sorted). Used to short-circuit duplicate emits."""
        try:
            items = []
            for k in sorted(md.keys()):
                v = md[k]
                if isinstance(v, list):
                    v = tuple(v)
                items.append((k, v))
            return repr(items)
        except Exception:
            return repr(md)


class MPRISBridge:
    """
    Provide a controller object that implements subset:
      - play(), pause(), play_pause(), stop()
      - next()/next_track(), previous()/previous_track()
      - query_nowplaying() -> dict (xesam:title, xesam:artist(list), xesam:album, mpris:artUrl, mpris:length(ns))
      - query_playback_status() -> "Playing"/"Paused"/"Stopped"
      - query_position_ms() -> int ms
      - set_volume(float 0..1)
    """

    def __init__(self, controller: Any, identity: str = "LaLaDash"):
        self.controller = controller
        self.identity = identity

        self._bus: Optional[SessionBus] = None
        self._bus_obj: Optional[_MPRISObject] = None
        self._loop: Optional[GLib.MainLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._ready_evt = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return

        self._stop_evt.clear()
        self._ready_evt.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        _dbg("Bridge start()")

    def stop(self):
        self._stop_evt.set()
        try:
            if self._loop is not None:
                self._loop.quit()
        except Exception:
            pass
        _dbg("Bridge stop()")

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._ready_evt.is_set())

    def wait_ready(self, timeout: float) -> bool:
        """Block up to `timeout` seconds until the bridge owns the bus name
        and has exported the object. Returns True if ready."""
        return self._ready_evt.wait(timeout=timeout)

    def notify_changed(self) -> None:
        """Push current playback state/metadata to D-Bus immediately, without
        waiting for the next periodic tick. Safe to call from any thread —
        the work is marshalled onto the GLib loop thread via idle_add."""
        if not self._ready_evt.is_set():
            return

        def _once():
            self._tick()
            return False  # one-shot

        try:
            GLib.idle_add(_once)
        except Exception:
            pass

    def _run(self):
        """Publish on the session bus and run the GLib loop. If the loop
        exits (D-Bus disconnect, session restart, transient error), enter a
        bounded exponential-backoff retry forever, so the bridge is
        self-healing across the lifetime of the app. Honours `_stop_evt`
        for clean shutdown."""
        backoff = 1.0
        backoff_cap = 30.0
        # Short cap while we're only waiting for the bus socket to appear at
        # boot — it usually shows up within a few seconds, so don't crawl up
        # to the 30 s reconnect cap for that case.
        boot_backoff_cap = 2.0
        attempt = 0

        while not self._stop_evt.is_set():
            attempt += 1
            published = False

            # Re-resolve the session bus on EVERY attempt. At boot the XDG
            # user bus socket may not exist yet; if we publish before it does,
            # GLib auto-launches a private bus the car can never see and we'd
            # be stuck there forever. Skip publishing until a real bus exists.
            bus_addr = ensure_session_bus_env()
            if not bus_addr:
                self._ready_evt.clear()
                wait = min(boot_backoff_cap, backoff)
                if self._stop_evt.wait(timeout=wait):
                    return
                backoff = min(backoff_cap, backoff * 2.0)
                continue

            try:
                if attempt == 1 or _mpris_debug_enabled():
                    print(f"[MPRIS] session bus = {bus_addr}", flush=True)
                self._bus = SessionBus()
                self._bus_obj = _MPRISObject(self.controller, identity=self.identity)

                # publish() acquires the bus name AND exports the object in one call.
                # Do NOT call request_name() first — that grabs the name, then publish()
                # fails with "Name already registered" and the bridge silently dies.
                self._bus.publish(MPRIS_BUS_NAME, (MPRIS_OBJ_PATH, self._bus_obj))
                published = True

                self._ready_evt.set()
                print(
                    f"[MPRIS] published {MPRIS_BUS_NAME} at {MPRIS_OBJ_PATH} "
                    f"(attempt {attempt})",
                    flush=True,
                )
                # Reset backoff after a successful publish so the *next*
                # disconnect retries fast, not after a 30 s pause.
                backoff = 1.0

                # Periodic tick is just a safety net now — track changes are
                # pushed immediately via notify_changed(), so 1 s is plenty
                # and saves idle CPU on the Pi.
                GLib.timeout_add(1000, self._tick)  # ms
                self._loop = GLib.MainLoop()
                self._loop.run()

                # If we get here, the main loop exited (stop() was called,
                # OR D-Bus dropped the connection). Either path falls
                # through to the retry-or-exit check below.
                if self._stop_evt.is_set():
                    return
                print(
                    "[MPRIS] main loop exited unexpectedly — will republish",
                    flush=True,
                )
            except Exception as e:
                self._ready_evt.clear()
                msg = str(e)
                if "already" in msg.lower() and not published:
                    # Another LaLaDash (or a stale instance) owns the name.
                    # On a restart this is just the previous process still
                    # exiting, and it clears within a second — so it is not
                    # worth a console line per retry. Say something once, and
                    # only once it has gone on long enough to be a real
                    # problem rather than a handover.
                    if _mpris_debug_enabled():
                        print(f"[MPRIS] bus name busy (attempt {attempt}); retrying",
                              flush=True)
                    elif attempt == _BUSY_WARN_AFTER:
                        print("[MPRIS] bus name still held by another instance "
                              f"after {attempt} tries; still retrying", flush=True)
                    time.sleep(0.5)
                    continue
                print(
                    f"⚠️ [MPRIS] bridge error on attempt {attempt}: "
                    f"{type(e).__name__}: {e} — retrying in {backoff:.1f}s",
                    flush=True,
                )

            # Sleep with a stop-aware wait so shutdown isn't delayed.
            self._ready_evt.clear()
            self._bus_obj = None
            self._bus = None
            self._loop = None
            if self._stop_evt.wait(timeout=backoff):
                return
            backoff = min(backoff_cap, backoff * 2.0)

    def _tick(self) -> bool:
        if self._stop_evt.is_set():
            return False

        if not self._bus_obj:
            return True

        # playback status
        try:
            if hasattr(self.controller, "query_playback_status"):
                st = self.controller.query_playback_status()
                if st:
                    self._bus_obj.set_playback_status(st)
        except Exception as e:
            _dbg(f"!! query_playback_status failed: {e}")

        # metadata
        try:
            if hasattr(self.controller, "query_nowplaying"):
                md = self.controller.query_nowplaying()
                if isinstance(md, dict):
                    # It's fine if it's empty — update_metadata will handle safely
                    self._bus_obj.update_metadata(md)
        except Exception as e:
            _dbg(f"!! query_nowplaying failed: {e}")

        return True
