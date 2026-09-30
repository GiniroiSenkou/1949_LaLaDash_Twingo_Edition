from __future__ import annotations

import json
import os
import random
import shutil
import threading
from collections import deque
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QTimer, pyqtProperty, pyqtSignal, pyqtSlot

from preferences import BASE_DIR, DATA_SETTINGS, MUSIC_FOLDER, STEMS_FOLDER

from Modules.Music import lyrics as _lyrics

_SESSION_FILE = os.path.join(BASE_DIR, "Data", "music_session.json")
from Modules.Music.queue_utils import track_key
from Modules.Music.sounddrive_config import (
    DEFAULT_MIX_MODE,
    DEFAULT_SOUND_DRIVE_METHOD,
    DEFAULT_SOUND_DRIVE_PERFORMANCE,
    apply_normalized_music_settings,
    sounddrive_player_toggle_visible,
)
from Modules.Runtime import settings_cache
from Modules.Settings.settings_audio import SystemAudio, VLC_REGISTRY
from Modules.Music.tags_list import TAG_ICONS, canonical_tag_key, tag_display_label
from Modules.Music.audio_router import AudioRouter


class MusicBridge(QObject):
    """Expose the music player and library state to QML."""

    # Accepted values for the lyrics display-mode setting. Anything else
    # found in settings.ini falls back to the default at the front.
    _LYRICS_DISPLAY_MODES = ("smooth_wave", "single_focus", "solo_spotlight", "solo_preview")

    trackChanged = pyqtSignal()
    progressChanged = pyqtSignal()
    playlistChanged = pyqtSignal()
    artResolved = pyqtSignal()
    volumeChanged = pyqtSignal()
    routeChanged = pyqtSignal()
    audioPlayersChanged = pyqtSignal()
    sessionAvailableChanged = pyqtSignal()
    splitAudioChanged = pyqtSignal()
    mediaKeysChanged = pyqtSignal()
    btAudioChanged = pyqtSignal()
    lyricsChanged = pyqtSignal()
    lyricsCurrentLineChanged = pyqtSignal()
    lyricsDisplayModeChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._player = None
        self._music_gui = None

        self._title = ""
        self._artist = ""
        self._album_art = ""
        self._art_url_cache = {}
        self._art_pending = set()
        self._art_queue = deque()
        self._art_worker = None
        self._art_lock = threading.Lock()
        self._art_revision = 0
        self._position_ms = 0
        self._duration_ms = 0
        self._playing = False
        self._volume = 100
        self._shuffle = True

        self._sound_drive_enabled = False
        self._sound_drive_active = False
        self._sound_drive_method = DEFAULT_SOUND_DRIVE_METHOD
        self._sound_drive_performance = DEFAULT_SOUND_DRIVE_PERFORMANCE
        self._sound_drive_metadata_cache = False
        self._player_sd_session: bool = False  # session-only, never persisted
        self._auto_mix_enabled = False
        self._mix_mode = DEFAULT_MIX_MODE
        self._current_track_key = ""
        self._sounddrive_restore_attempt_key = ""

        self._router = AudioRouter()
        self._split_active: bool = False
        self._split_device_list: list = []
        self._fb_split_active: bool = False
        self._split_auto_delay: bool = False
        self._invert_lr: bool = False
        self._split_engine_sound: bool = False
        self._saved_engine_sink: str = ""
        self._modding_bridge = None
        # Every pactl call is a subprocess. Run them on one serialised worker
        # instead of the Qt main thread: setVolume() alone used to spawn up to
        # five (two volume writes plus a full `pactl list sink-inputs`) per
        # volume step, which is what made the sound system stall the UI.
        self._audio_ops: "deque" = deque()
        self._audio_ops_event = threading.Event()
        self._audio_ops_stop = False
        self._audio_ops_thread: Optional[threading.Thread] = None
        # splitAudioVolume is bound in QML; it must never shell out. Seeded
        # from settings and kept current by _refresh_split_volume() on the
        # worker.
        self._split_volume: int = 100
        # Front/rear level bias, -100 (front only) .. +100 (rear only).
        self._split_balance: int = 0
        # Volume to hand back when an AirPlay duck releases; None = not ducked.
        self._duck_saved_volume: "int | None" = None
        # What the Auto delay probe last concluded, shown next to the button so
        # it is not a control that silently does nothing.
        self._split_delay_status: str = ""
        # Watches for the Bluetooth leg disappearing mid-playback.
        self._sink_watcher = None
        self._split_guard_pending = False
        # Keeps the head unit's A2DP link up (boot race, link loss).
        self._bt_auto = None

        # Steering-wheel / head-unit buttons that MPRIS does not carry.
        self._nav_bridge = None
        self._media_keys = None
        self._mpris_hooked = False
        self._bt_connected_at: float = 0.0
        self._bt_was_connected: bool = False
        self._last_key_code: int = 0
        self._last_key_name: str = ""
        self._last_key_device: str = ""
        self._last_key_action: str = ""
        self._vol_before_sd_session: int = 100

        self._current_folder = ""
        self._selected_tag = "All"
        self._search_query = ""
        self._library: list[dict] = []
        self._visible_tracks: list[dict] = []
        # Materialised form of _visible_tracks, rebuilt only when the filter
        # changes. See the visibleTracks property for why this must be cached.
        self._visible_items: list[dict] | None = None
        # track key -> position in _visible_tracks, same lifetime as above.
        self._visible_index: dict[str, int] | None = None
        self._file_list: list[str] = []
        self._tag_list: list[dict] = []
        self._audio_players: list[dict] = []

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(250)
        self._poll_timer.timeout.connect(self._poll_progress)
        self._poll_active = False  # True when Music page is visible

        # Single-shot timer: retries art resolution after VLC has had time
        # to parse the embedded cover of the currently playing track.
        self._art_retry_timer = QTimer(self)
        self._art_retry_timer.setSingleShot(True)
        self._art_retry_timer.setInterval(2500)
        self._art_retry_timer.timeout.connect(self._retry_art)
        self._art_retry_count = 0

        # Lyrics state. Populated synchronously from local sources
        # (Lyrics folder / sidecar / embedded tag) on each track change.
        self._lyrics_lines: list[dict] = []
        self._lyrics_synced: bool = False
        self._lyrics_partial: bool = False
        self._lyrics_available: bool = False
        self._lyrics_source: str = ""
        self._lyrics_current_idx: int = -1
        # User-selected lyrics rendering mode. Persisted in [Music] of
        # settings.ini. Unknown / corrupt values fall back to the default.
        try:
            persisted_mode = str(settings_cache.get(
                "Music", "lyrics_display_mode",
                fallback=self._LYRICS_DISPLAY_MODES[0],
                path=DATA_SETTINGS,
            ) or "").strip().lower()
        except Exception:
            persisted_mode = ""
        if persisted_mode not in self._LYRICS_DISPLAY_MODES:
            persisted_mode = self._LYRICS_DISPLAY_MODES[0]
        self._lyrics_display_mode: str = persisted_mode

    # ── wiring ────────────────────────────────────────────────────────────
    @staticmethod
    def _mpris_proxy_running() -> bool:
        """True if an mpris-proxy process is already running."""
        import subprocess
        try:
            return subprocess.run(
                ["pgrep", "-x", "mpris-proxy"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
            ).returncode == 0
        except Exception:
            return False

    def _ensure_mpris_proxy(self):
        """Make sure mpris-proxy is running so the car head unit can detect
        and control LaLaDash over Bluetooth AVRCP.

        If the binary is installed but no process is running, LaLaDash
        launches it as a daemon child sharing this process's session bus
        (pinned by mpris_bridge.ensure_session_bus_env) — removing the
        fragile dependency on a correctly-enabled systemd user unit. A small
        supervisor thread relaunches it if it dies; stop_mpris_proxy()
        terminates it on app exit."""
        if shutil.which("mpris-proxy") is None:
            print(
                "WARNING [MusicBridge]: mpris-proxy not found on PATH. "
                "Bluetooth AVRCP (car head-unit controls / track display) will NOT work. "
                "Run Installation/install.sh option 5 to install it.",
                flush=True,
            )
            return

        if self._mpris_proxy_running():
            print("[MusicBridge] mpris-proxy already running — AVRCP bridge is live.", flush=True)
            return

        # Pin the session bus so the spawned proxy shares it with the bridge.
        try:
            from Modules.Music.mpris_bridge import ensure_session_bus_env
            ensure_session_bus_env()
        except Exception:
            pass

        self._mpris_proxy_stop_evt = threading.Event()
        self._mpris_proxy_proc = None
        self._mpris_proxy_thread = threading.Thread(
            target=self._supervise_mpris_proxy, name="MprisProxySup", daemon=True,
        )
        self._mpris_proxy_thread.start()

    def attach_mpris_bridge(self, bridge):
        """Register the MPRISBridge so the proxy supervisor can wait for the
        bridge to own org.mpris.MediaPlayer2.LaLaDash before launching
        mpris-proxy (avoids the proxy starting against an empty player), and
        push metadata to D-Bus the moment a track changes instead of waiting
        for the bridge's periodic tick."""
        self._mpris_bridge = bridge
        try:
            self.trackChanged.connect(self._notify_mpris_changed)
        except Exception:
            pass

    def _notify_mpris_changed(self):
        bridge = getattr(self, "_mpris_bridge", None)
        if bridge is not None:
            try:
                bridge.notify_changed()
            except Exception:
                pass

    def _supervise_mpris_proxy(self):
        """Daemon loop: keep an mpris-proxy child alive until app exit."""
        import subprocess
        stop = self._mpris_proxy_stop_evt
        backoff = 1.0

        # Wait (stop-aware) for the MPRIS bridge to actually own the bus name
        # before the first launch, so mpris-proxy doesn't attach to a player
        # that hasn't published yet. After a timeout, launch anyway —
        # mpris-proxy tolerates the name appearing later.
        bridge = getattr(self, "_mpris_bridge", None)
        if bridge is not None:
            deadline = time.monotonic() + 15.0
            while not stop.is_set() and time.monotonic() < deadline:
                try:
                    if bridge.wait_ready(0.5):
                        break
                except Exception:
                    break

        while not stop.is_set():
            # A systemd-managed instance may have appeared — don't double-run.
            if self._mpris_proxy_running():
                if stop.wait(10.0):
                    return
                continue
            try:
                proc = subprocess.Popen(
                    ["mpris-proxy"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._mpris_proxy_proc = proc
                print(
                    f"[MusicBridge] mpris-proxy started (pid {proc.pid}) — AVRCP bridge is live.",
                    flush=True,
                )
            except Exception as e:
                print(f"WARNING [MusicBridge]: failed to launch mpris-proxy: {e}", flush=True)
                if stop.wait(backoff):
                    return
                backoff = min(30.0, backoff * 2.0)
                continue
            backoff = 1.0
            # Block until the child exits or we're asked to stop.
            while not stop.is_set():
                try:
                    proc.wait(timeout=1.0)
                    break  # child exited — fall through to relaunch
                except subprocess.TimeoutExpired:
                    continue
            if stop.is_set():
                return
            print("WARNING [MusicBridge]: mpris-proxy exited — relaunching.", flush=True)

    def stop_mpris_proxy(self):
        """Terminate the self-spawned mpris-proxy child. No-op if we never
        spawned one (e.g. a systemd unit already owned it)."""
        evt = getattr(self, "_mpris_proxy_stop_evt", None)
        if evt is not None:
            evt.set()
        proc = getattr(self, "_mpris_proxy_proc", None)
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._mpris_proxy_proc = None

    def set_player(self, music_gui):
        self._ensure_mpris_proxy()
        self._music_gui = music_gui
        self._player = getattr(music_gui, "music_player", None)
        if self._player is not None:
            player_router = getattr(self._player, "router", None)
            if player_router is not None:
                self._router = player_router
            else:
                try:
                    self._player.router = self._router
                except Exception:
                    pass
        self._volume = int(getattr(self._player, "_user_volume", 100) or 100) if self._player else 100
        self._load_music_settings()
        self._apply_runtime_sounddrive_settings()
        self._reload_library()
        self.refreshAudioPlayers()
        self._sync_from_player()
        self._load_split_audio_state()

    def start_polling(self):
        # Background heartbeat: 1 Hz. Detects track changes for MPRIS/AVRCP
        # without burning CPU at 10 Hz when the Music page isn't visible.
        if not self._poll_active:
            self._poll_timer.setInterval(1000)
            if not self._poll_timer.isActive():
                self._poll_timer.start()

    def stop_polling(self):
        self._poll_timer.stop()
        self._poll_active = False
        self._stop_sink_watch()
        self._stop_media_keys_safe()
        self.stop_bt_autoconnect()
        self._stop_audio_ops()

    def _stop_media_keys_safe(self):
        try:
            self.stop_media_keys()
        except Exception:
            pass

    @pyqtSlot()
    def activatePage(self):
        # Music page visible — 250 ms is fast enough for skip detection and
        # keeps Pi 4B headroom for the engine sim audio thread.
        self._poll_active = True
        self._poll_timer.setInterval(250)
        if not self._poll_timer.isActive():
            self._poll_timer.start()

    @pyqtSlot()
    def deactivatePage(self):
        # Music page hidden — fall back to 1Hz heartbeat (keeps MPRIS alive).
        self._poll_active = False
        self._poll_timer.setInterval(1000)
        if not self._poll_timer.isActive():
            self._poll_timer.start()

    def create_mpris_controller(self):
        """Return a small controller object for the D-Bus MPRIS bridge."""
        mb = self

        class _Ctl:
            def play(self):
                # Only the MPRIS/AVRCP path reaches here; the UI calls
                # MusicBridge.play() directly, so pressing Play on the
                # dashboard is never suppressed.
                if mb._avrcp_play_suppressed():
                    return
                mb.play()
            def pause(self):             mb.pause()
            def play_pause(self):        mb.togglePlayPause()
            def stop(self):
                try:
                    mb._player.stop_song()
                except Exception:
                    pass
            def next_track(self):        mb.next()
            def previous_track(self):    mb.prev()
            def set_volume(self, v):
                mb.setVolume(int(round(v * 100)) if v <= 1.0 else int(v))
            def query_playing(self):     return mb.playing
            def query_playback_status(self):
                return "Playing" if mb.playing else "Paused"
            def query_position_ms(self): return mb.positionMs
            def query_length_ms(self):   return mb.durationMs
            def query_nowplaying(self):
                title   = mb.title
                artist  = mb.artist
                art_url = mb.albumArtUrl
                dur_ns  = max(0, mb.durationMs) * 1_000_000
                key     = f"{artist}|{title}"
                trackid = f"/org/mpris/MediaPlayer2/track/{abs(hash(key)) % 0xFFFFFF}"
                return {
                    "xesam:title":  title,
                    "xesam:artist": [artist] if artist else [],
                    "xesam:album":  mb._now_playing_tag_text(),
                    "mpris:artUrl": art_url,
                    "mpris:length": dur_ns,
                    "mpris:trackid": trackid,
                }

        return _Ctl()

    # ── track info properties ─────────────────────────────────────────────
    @pyqtProperty(str, notify=trackChanged)
    def title(self):
        return self._title

    @pyqtProperty(str, notify=trackChanged)
    def artist(self):
        return self._artist

    @pyqtProperty(str, notify=trackChanged)
    def albumArt(self):
        return self._album_art

    @pyqtProperty(str, notify=trackChanged)
    def albumArtUrl(self):
        art = str(self._album_art or "").strip()
        if not art:
            return ""
        try:
            return Path(art).resolve().as_uri()
        except Exception:
            return ""

    @pyqtProperty(bool, notify=trackChanged)
    def playing(self):
        return self._playing

    @pyqtProperty(bool, notify=trackChanged)
    def soundDriveEnabled(self):
        return self._sound_drive_enabled

    @pyqtProperty(bool, notify=trackChanged)
    def soundDriveActive(self):
        return self._player_sd_session

    @pyqtProperty(bool, notify=trackChanged)
    def soundDriveToggleVisible(self):
        return sounddrive_player_toggle_visible(
            sound_drive_enabled=self._sound_drive_enabled,
            current_track_key=self._current_track_key,
        )

    @pyqtProperty(str, notify=trackChanged)
    def soundDriveMethod(self):
        return self._sound_drive_method

    @pyqtProperty(str, notify=trackChanged)
    def soundDrivePerformance(self):
        return self._sound_drive_performance

    @pyqtProperty(bool, notify=trackChanged)
    def soundDriveMetadataCache(self):
        return self._sound_drive_metadata_cache

    @pyqtProperty(str, notify=trackChanged)
    def mixMode(self):
        return self._mix_mode

    @pyqtProperty(bool, notify=trackChanged)
    def stemsAvailable(self):
        track = self._current_track_dict()
        return bool(track and self._has_stems(track))


    # ── Lyrics properties ────────────────────────────────────────────────
    @pyqtProperty(list, notify=lyricsChanged)
    def lyricsLines(self):
        return list(self._lyrics_lines)

    @pyqtProperty(bool, notify=lyricsChanged)
    def lyricsSynced(self):
        return self._lyrics_synced

    @pyqtProperty(bool, notify=lyricsChanged)
    def lyricsPartial(self):
        """Synced, but some lines had no stamp of their own (carried) — the
        overlay flags them so a half-timed file is not read as fully synced."""
        return self._lyrics_partial

    @pyqtProperty(bool, notify=lyricsChanged)
    def lyricsAvailable(self):
        return self._lyrics_available


    @pyqtProperty(int, notify=lyricsCurrentLineChanged)
    def lyricsCurrentLine(self):
        return self._lyrics_current_idx

    @pyqtProperty(str, notify=lyricsChanged)
    def lyricsLabelText(self):
        if not self._lyrics_available:
            return ""
        if self._lyrics_synced and self._lyrics_partial:
            return "Partly synced"
        if self._lyrics_synced:
            return "Synced Lyrics"
        return "Not synced — won't scroll"

    @pyqtProperty(str, notify=lyricsChanged)
    def lyricsLabelColor(self):
        if not self._lyrics_available:
            return "transparent"
        if self._lyrics_synced and not self._lyrics_partial:
            return "#2ecc40"
        return "#f1c40f"

    @pyqtProperty(str, notify=lyricsDisplayModeChanged)
    def lyricsDisplayMode(self):
        return self._lyrics_display_mode

    @pyqtSlot(str)
    def setLyricsDisplayMode(self, mode):
        """Validate, persist, and broadcast the new lyrics display mode.
        Silently ignores unknown values so QML can't put the bridge in a
        weird state."""
        clean = str(mode or "").strip().lower()
        if clean not in self._LYRICS_DISPLAY_MODES:
            return
        if clean == self._lyrics_display_mode:
            return
        self._lyrics_display_mode = clean
        try:
            settings_cache.set_value(
                "Music", "lyrics_display_mode", clean, path=DATA_SETTINGS,
            )
        except Exception:
            pass
        self.lyricsDisplayModeChanged.emit()

    # ── Split Audio properties ────────────────────────────────────────────
    @pyqtProperty(bool, notify=splitAudioChanged)
    def splitAudioConfigured(self):
        return bool(settings_cache.get("audio", "device_id", fallback="", path=DATA_SETTINGS))

    @pyqtProperty(bool, notify=splitAudioChanged)
    def splitAudioEnabled(self):
        return settings_cache.getboolean("audio", "split_enabled", fallback=False, path=DATA_SETTINGS)

    @pyqtProperty(bool, notify=splitAudioChanged)
    def splitAudioActive(self):
        return self._split_active

    @pyqtProperty(list, notify=splitAudioChanged)
    def splitAudioDeviceList(self):
        return list(self._split_device_list)

    @pyqtProperty(str, notify=splitAudioChanged)
    def splitAudioDeviceId(self):
        return settings_cache.get("audio", "device_id", fallback="", path=DATA_SETTINGS) or ""

    @pyqtProperty(int, notify=splitAudioChanged)
    def splitAudioDelayMs(self):
        return settings_cache.getint("audio", "delay_ms", fallback=200, path=DATA_SETTINGS)

    @pyqtProperty(int, notify=splitAudioChanged)
    def splitAudioVolume(self):
        """Cached — this is bound in QML and must not shell out.

        It used to run `pactl list sinks` on every binding evaluation, so each
        splitAudioChanged emit spawned a subprocess on the Qt main thread.
        _refresh_split_volume() keeps this current from the audio worker.
        """
        return int(max(0, min(150, self._split_volume)))

    @pyqtProperty(bool, notify=splitAudioChanged)
    def splitAudioInvertDelay(self):
        return settings_cache.getboolean("audio", "invert_delay", fallback=False, path=DATA_SETTINGS)

    @pyqtProperty(bool, notify=splitAudioChanged)
    def fbSplitActive(self):
        return self._fb_split_active

    @pyqtProperty(bool, notify=splitAudioChanged)
    def splitAutoDelay(self):
        return self._split_auto_delay

    @pyqtProperty(bool, notify=splitAudioChanged)
    def splitAudioInvertLR(self):
        return self._invert_lr

    @pyqtProperty(bool, notify=splitAudioChanged)
    def splitEngineSound(self):
        return self._split_engine_sound

    @pyqtProperty(bool, notify=splitAudioChanged)
    def splitAudio4chActive(self):
        """True when the router is in single-device 4-channel (surround40) mode."""
        try:
            def_sink, sel_sink = self._router._last_pair
            return bool(def_sink and sel_sink is None and self._split_active)
        except Exception:
            return False

    @pyqtProperty(int, notify=splitAudioChanged)
    def splitBalance(self):
        """-100 = front only, 0 = even, +100 = rear only."""
        return self._split_balance

    @pyqtProperty(bool, notify=splitAudioChanged)
    def splitBalanceAvailable(self):
        """False in single-device 4-channel mode, where both pairs ride one
        loopback and there is nothing to bias between."""
        return bool(self._split_active and not self.splitAudio4chActive)

    @pyqtProperty(str, notify=splitAudioChanged)
    def splitDelayStatus(self):
        return self._split_delay_status

    @pyqtProperty(bool, notify=splitAudioChanged)
    def splitDropGuard(self):
        """Auto-disable split when one of its two outputs disappears."""
        return settings_cache.getboolean(
            "audio", "drop_guard", fallback=True, path=DATA_SETTINGS
        )

    @pyqtSlot(bool)
    def setSplitDropGuard(self, on):
        settings_cache.set_value(
            "audio", "drop_guard", "1" if bool(on) else "0", path=DATA_SETTINGS
        )
        self.splitAudioChanged.emit()

    @pyqtSlot(int)
    def setSplitBalance(self, balance):
        b = int(max(-100, min(100, balance)))
        self._split_balance = b
        settings_cache.set_value("audio", "balance", str(b), path=DATA_SETTINGS)
        if self._split_active:
            self._audio_op(lambda: self._router.set_front_rear_balance(b))
        self.splitAudioChanged.emit()

    @pyqtSlot()
    def setModdingBridge(self, bridge):
        self._modding_bridge = bridge

    # ── steering-wheel buttons ───────────────────────────────────────────
    def setNavigationBridge(self, bridge):
        self._nav_bridge = bridge

    def start_media_keys(self):
        """Begin listening for the car's non-MPRIS buttons.

        Silent no-op when there is nothing to read — a dev PC with no paired
        device, or a Pi whose user is not in the `input` group. The feature
        simply does not fire; nothing else is affected.
        """
        if not self.mediaKeysEnabled:
            return
        # The AVRCP path does not go through /dev/input and works even when
        # no Bluetooth input node exists at all, so hook it first and
        # independently of whether the evdev listener can start.
        self._hook_mpris_commands()
        if self._media_keys is not None:
            return
        try:
            from Modules.Music.media_keys import MediaKeyListener
            listener = MediaKeyListener(self._on_media_key)
            if listener.start():
                self._media_keys = listener
        except Exception:
            self._media_keys = None

    def _hook_mpris_commands(self):
        """Report AVRCP transport commands into the Steering Keys display.

        Without this the page is permanently empty on any car that hands its
        wheel buttons to the MPRIS player rather than to a uinput node — the
        buttons work (MPRIS carries them) while "Last button" shows nothing,
        which reads as a broken feature.
        """
        if getattr(self, "_mpris_hooked", False):
            return
        try:
            from Modules.Music import mpris_bridge as _mb
            _mb.set_command_observer(self._on_mpris_command)
            self._mpris_hooked = True
        except Exception:
            pass

    def _on_mpris_command(self, name: str):
        """A transport command arrived from the head unit over AVRCP.

        Runs on the D-Bus thread. No key code exists for these — BlueZ never
        made one — so they are recorded by name and marked as already
        handled, to distinguish them from a raw, bindable key event.
        """
        self._last_key_code = 0
        self._last_key_name = str(name)
        self._last_key_device = "Head unit (AVRCP → MPRIS)"
        self._last_key_action = ""
        self.mediaKeysChanged.emit()

    def stop_media_keys(self):
        listener = self._media_keys
        self._media_keys = None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass

    @pyqtProperty(bool, notify=mediaKeysChanged)
    def mediaKeysEnabled(self):
        # ANDed with the Settings → Services master switch.
        from Modules.Runtime.services import service_on
        return service_on("steering_keys") and settings_cache.getboolean(
            "SteeringKeys", "enabled", fallback=True, path=DATA_SETTINGS
        )

    @pyqtSlot()
    def refreshMediaKeysService(self):
        """Settings → Services flipped: stop or (re)start the listener."""
        if self.mediaKeysEnabled:
            self.start_media_keys()
        else:
            self.stop_media_keys()
        self.mediaKeysChanged.emit()

    @pyqtSlot(bool)
    def setMediaKeysEnabled(self, on):
        settings_cache.set_value(
            "SteeringKeys", "enabled", "1" if bool(on) else "0", path=DATA_SETTINGS
        )
        if on:
            self.start_media_keys()
        else:
            self.stop_media_keys()
        self.mediaKeysChanged.emit()

    @pyqtProperty(bool, notify=mediaKeysChanged)
    def mediaKeysListening(self):
        """True when buttons can actually reach us — by either route.

        An AVRCP-only head unit produces no input node, but its commands do
        arrive; treating that as "not listening" made a working setup read as
        broken.
        """
        if self._media_keys is not None and self._media_keys.device_count > 0:
            return True
        return bool(self._last_key_name and not self._last_key_code)

    @pyqtProperty(str, notify=mediaKeysChanged)
    def mediaKeyDevices(self):
        names = self._media_keys.device_names if self._media_keys is not None else []
        if names:
            return ", ".join(names)
        # No input node, but transport commands are arriving: the head unit
        # forwards only AVRCP and BlueZ gave it to the MPRIS player. Say so,
        # rather than "No Bluetooth input device", which looks like a fault.
        if self._last_key_name and not self._last_key_code:
            return f"{self._last_key_device or 'Head unit'} — AVRCP only, no input node"
        if self._media_keys is None:
            return "Not listening"
        return "No Bluetooth input device"

    # ── head-unit Bluetooth link ─────────────────────────────────────────
    def start_bt_autoconnect(self):
        """Start keeping the paired head unit connected. Silent no-op on a
        machine without BlueZ (dev PC) — the thread reports "unavailable"
        and idles at the slow cadence. See Modules/Runtime/bt_autoconnect.py."""
        if self._bt_auto is not None:
            return
        if not self.btAudioEnabled:
            return
        try:
            from Modules.Runtime.bt_autoconnect import BtAudioAutoConnect
            pin = settings_cache.get("Bluetooth", "device", fallback="", path=DATA_SETTINGS) or ""
            auto = BtAudioAutoConnect(self._on_bt_auto_change, device=pin.strip())
            auto.start()
            self._bt_auto = auto
        except Exception:
            self._bt_auto = None

    def stop_bt_autoconnect(self):
        auto = self._bt_auto
        self._bt_auto = None
        if auto is not None:
            try:
                auto.stop()
            except Exception:
                pass
            self.btAudioChanged.emit()

    def _on_bt_auto_change(self):
        """Runs on the supervisor's thread; Qt queues the delivery."""
        try:
            now_conn = (self._bt_auto is not None
                        and self._bt_auto.status == "connected")
        except Exception:
            now_conn = False
        if now_conn and not self._bt_was_connected:
            self._bt_connected_at = time.monotonic()
        self._bt_was_connected = now_conn
        self.btAudioChanged.emit()

    # ── AVRCP play-on-connect guard ──────────────────────────────────────
    _CONNECT_PLAY_GRACE_S = 6.0

    def _avrcp_play_suppressed(self) -> bool:
        """True for a Play arriving just after the head unit connected.

        Car head units routinely fire a transport command the moment
        A2DP/AVRCP comes up, to sync state. That is not the driver asking for
        music, so obeying it starts playback on every single connect. Set
        [Bluetooth] play_on_connect = true to allow it through.
        """
        try:
            if settings_cache.getboolean("Bluetooth", "play_on_connect",
                                         fallback=False, path=DATA_SETTINGS):
                return False
        except Exception:
            pass
        at = self._bt_connected_at
        if not at:
            return False
        if (time.monotonic() - at) <= self._CONNECT_PLAY_GRACE_S:
            print(f"[music] ignoring AVRCP Play within "
                  f"{self._CONNECT_PLAY_GRACE_S:g}s of the head unit "
                  f"connecting ([Bluetooth] play_on_connect=true to allow)",
                  flush=True)
            return True
        return False

    @pyqtProperty(bool, notify=btAudioChanged)
    def btAudioEnabled(self):
        return settings_cache.getboolean("Bluetooth", "auto_connect", fallback=True, path=DATA_SETTINGS)

    @pyqtSlot(bool)
    def setBtAudioEnabled(self, on):
        settings_cache.set_value("Bluetooth", "auto_connect", "true" if bool(on) else "false", path=DATA_SETTINGS)
        if on:
            self.start_bt_autoconnect()
        else:
            self.stop_bt_autoconnect()
        self.btAudioChanged.emit()

    @pyqtProperty(bool, notify=btAudioChanged)
    def btAudioConnected(self):
        return self._bt_auto is not None and self._bt_auto.status == "connected"

    @pyqtProperty(str, notify=btAudioChanged)
    def btAudioStatus(self):
        auto = self._bt_auto
        if auto is None:
            return "Auto-connect off"
        st = auto.status
        if st == "connected":
            return f"Connected to {auto.connected_name}"
        if st == "connecting":
            n = auto.attempts
            err = auto.last_error
            tail = f" — {err}" if err and n > 1 else ""
            return f"Connecting to {auto.connected_name} (attempt {n}){tail}"
        if st == "no-device":
            return "No paired audio device — pair the head unit once with bluetoothctl"
        if st == "unavailable":
            return "Bluetooth not available"
        return "Waiting…"

    @pyqtSlot()
    def btAudioConnectNow(self):
        if self._bt_auto is None:
            self.start_bt_autoconnect()
        elif self._bt_auto is not None:
            self._bt_auto.kick()

    @pyqtProperty(str, notify=mediaKeysChanged)
    def lastMediaKey(self):
        """Most recent button press, for binding a button you can press but
        cannot look up. Empty until something arrives.

        Two sources reach here. A raw Bluetooth key event has a code and can
        be bound. A transport command the head unit sent over AVRCP has no
        code — BlueZ routed it to the MPRIS player, never to /dev/input — so
        it is shown as already handled rather than offered for binding.
        """
        if self._last_key_code:
            action = self._last_key_action or "unbound"
            return f"{self._last_key_name} ({self._last_key_code}) → {action}"
        if self._last_key_name:
            return f"{self._last_key_name} — over AVRCP, already handled"
        return ""


    @pyqtProperty(list, constant=True)
    def mediaKeyActions(self):
        from Modules.Music.media_keys import ACTIONS
        return [
            {"value": a, "label": a.replace("_", " ").title() if a else "Unbound"}
            for a in ACTIONS
        ]

    @pyqtProperty(list, notify=mediaKeysChanged)
    def mediaKeyBindings(self):
        """Every bound code, defaults merged with the user's overrides."""
        from Modules.Music.media_keys import key_name
        rows = []
        for code, action in sorted(self._key_bindings().items()):
            rows.append({
                "code": int(code),
                "name": key_name(code),
                "action": action,
                "label": action.replace("_", " ").title() if action else "Unbound",
            })
        return rows

    @pyqtProperty(int, notify=mediaKeysChanged)
    def callDuckPercent(self):
        return max(0, min(100, settings_cache.getint(
            "SteeringKeys", "call_duck_percent", fallback=80, path=DATA_SETTINGS
        )))

    @pyqtSlot(int)
    def setCallDuckPercent(self, value):
        pct = max(0, min(100, int(value)))
        settings_cache.set_value(
            "SteeringKeys", "call_duck_percent", str(pct), path=DATA_SETTINGS
        )
        self.mediaKeysChanged.emit()

    @pyqtSlot(int, str)
    def setMediaKeyBinding(self, code, action):
        from Modules.Music.media_keys import ACTIONS
        act = str(action or "")
        if act not in ACTIONS:
            return
        settings_cache.set_value(
            "SteeringKeys", f"key_{int(code)}", act, path=DATA_SETTINGS
        )
        self.mediaKeysChanged.emit()


    def _key_bindings(self) -> dict:
        """DEFAULT_BINDINGS with any `[SteeringKeys] key_<code>` overrides.

        An override of "" unbinds a default, which is how a button that the car
        sends for something else can be silenced.
        """
        from Modules.Music.media_keys import DEFAULT_BINDINGS
        merged = dict(DEFAULT_BINDINGS)
        try:
            cfg = settings_cache.shared(DATA_SETTINGS)
            if cfg.has_section("SteeringKeys"):
                for key, value in cfg.items("SteeringKeys"):
                    if not key.startswith("key_"):
                        continue
                    try:
                        code = int(key[4:])
                    except ValueError:
                        continue
                    merged[code] = str(value or "").strip()
        except Exception:
            pass
        return merged

    def _on_media_key(self, code: int, device: str):
        """Runs on the listener thread."""
        from Modules.Music.media_keys import key_name
        self._last_key_code = int(code)
        self._last_key_name = key_name(code)
        self._last_key_device = device
        action = self._key_bindings().get(int(code), "")
        self._last_key_action = action
        self.mediaKeysChanged.emit()
        if not action:
            return
        # Hop to the GUI thread: the actions below touch VLC, navigation and
        # QML-visible state.
        QTimer.singleShot(0, lambda: self._run_key_action(action))

    def _run_key_action(self, action: str):
        nav = self._nav_bridge
        try:
            if action == "play_pause":
                self.togglePlayPause()
            elif action == "next_track":
                self.next()
            elif action == "prev_track":
                self.prev()
            elif action == "mute_music":
                self.setMute(True)
            elif action == "toggle_side_panel":
                if nav is not None:
                    nav.toggleSidePanel()
            elif action == "next_page":
                if nav is not None:
                    nav.navigateTo((nav.currentPage + 1) % len(nav.pageKeys))
            elif action == "prev_page":
                if nav is not None:
                    nav.navigateTo((nav.currentPage - 1) % len(nav.pageKeys))
            elif action == "mode_ok":
                self._action_mode_ok()
            elif action == "answer":
                self._action_answer()
            elif action == "hangup":
                self._action_hangup()
        except Exception:
            pass

    def _action_mode_ok(self):
        """"Mode/OK" means "change what I'm looking at" on a head unit.

        Cycles the dash page, which is the closest equivalent this app has to
        the head unit's source button. Rebindable like every other key.
        """
        nav = self._nav_bridge
        if nav is None:
            return
        nav.navigateTo((nav.currentPage + 1) % len(nav.pageKeys))

    def _action_answer(self):
        """The phone owns the call audio; the app's job is to get out of the way."""
        self.duckForExternal(self.callDuckPercent)

    def _action_hangup(self):
        """Undo the call duck, or pause if there was none to undo."""
        if self._duck_saved_volume is not None:
            self.releaseExternalDuck()
        elif self._playing:
            self.togglePlayPause()

    @pyqtSlot()
    def toggleBackChannel(self):
        if not self.splitAudioEnabled:
            return
        try:
            self._fb_split_active = not self._fb_split_active
            self._router.set_channel_split(self._fb_split_active, self._invert_lr)
        except Exception:
            self._fb_split_active = False
        self.splitAudioChanged.emit()

    @pyqtSlot(bool)
    def setSplitAudioInvertLR(self, invert):
        self._invert_lr = bool(invert)
        settings_cache.set_value("audio", "invert_lr", "1" if self._invert_lr else "0", path=DATA_SETTINGS)
        try:
            if self.splitAudio4chActive:
                self._router.set_invert_lr(self._invert_lr)
            elif self._fb_split_active:
                self._router.set_channel_split(True, self._invert_lr)
        except Exception:
            pass
        self.splitAudioChanged.emit()

    @pyqtSlot(bool)
    def setSplitEngineSound(self, enabled):
        enabled = bool(enabled)
        self._split_engine_sound = enabled
        settings_cache.set_value("audio", "split_engine_sound", "1" if enabled else "0", path=DATA_SETTINGS)
        if enabled and self.splitAudio4chActive:
            self._enable_engine_rear()
        else:
            self._disable_engine_rear()
        self.splitAudioChanged.emit()

    def _enable_engine_rear(self):
        if self._modding_bridge is None:
            return
        try:
            sink_name = self._router.ensure_rear_remap_sink()
            if sink_name:
                self._saved_engine_sink = getattr(self._modding_bridge, "engineOutputChannel", "") or ""
                self._modding_bridge.setEngineOutputChannel(sink_name)
        except Exception:
            pass

    def _disable_engine_rear(self):
        if self._modding_bridge is None:
            return
        try:
            self._router.teardown_rear_remap_sink()
            restored = self._saved_engine_sink
            self._modding_bridge.setEngineOutputChannel(restored)
            self._saved_engine_sink = ""
        except Exception:
            pass

    @pyqtProperty(list, notify=trackChanged)
    def currentTrackTags(self):
        track = self._current_track_dict()
        if not track:
            return []
        tags = track.get("tags", [])
        if not isinstance(tags, list):
            return []
        return [self._tag_icon_name(tag) for tag in tags[:10] if self._tag_icon_name(tag)]

    def _now_playing_tag_text(self) -> str:
        """Comma-joined Title-Case tag labels for the current track.

        Used for the MPRIS `xesam:album` field — over Bluetooth/AVRCP the
        car can render album *text* but not the tag icon image (BlueZ has
        no AVRCP cover-art OBEX server), so the tags ride in the album line.
        """
        track = self._current_track_dict()
        if not track:
            return ""
        tags = track.get("tags", [])
        if not isinstance(tags, list):
            return ""
        seen, labels = set(), []
        for tag in tags:
            key = canonical_tag_key(tag)
            if not key or key in seen:
                continue
            seen.add(key)
            label = tag_display_label(key)
            if label:
                labels.append(label)
            if len(labels) >= 6:        # guard against a pathologically long line
                break
        return ", ".join(labels)

    @pyqtProperty(bool, notify=trackChanged)
    def fadeSkipEnabled(self):
        return bool(getattr(self._player, "fade_skip_enabled", False)) if self._player else False

    @pyqtProperty(bool, notify=trackChanged)
    def fadeTracksEnabled(self):
        return bool(getattr(self._player, "fade_tracks_enabled", False)) if self._player else False


    # ── progress properties ───────────────────────────────────────────────
    @pyqtProperty(int, notify=progressChanged)
    def positionMs(self):
        return self._position_ms

    @pyqtProperty(int, notify=progressChanged)
    def durationMs(self):
        return self._duration_ms

    @pyqtProperty(str, notify=progressChanged)
    def positionText(self):
        return self._format_time(self._position_ms)

    @pyqtProperty(str, notify=progressChanged)
    def durationText(self):
        return self._format_time(self._duration_ms)

    @pyqtProperty(str, notify=progressChanged)
    def remainingText(self):
        remaining = max(0, self._duration_ms - self._position_ms)
        return f"-{self._format_time(remaining)}"

    @pyqtProperty(float, notify=progressChanged)
    def progress(self):
        if self._duration_ms <= 0:
            return 0.0
        return max(0.0, min(1.0, self._position_ms / self._duration_ms))

    # ── volume ────────────────────────────────────────────────────────────
    @pyqtProperty(int, notify=volumeChanged)
    def volume(self):
        return self._volume

    # ── playlist ──────────────────────────────────────────────────────────
    @pyqtProperty(str, notify=playlistChanged)
    def currentFolder(self):
        return self._current_folder

    @pyqtProperty(list, notify=playlistChanged)
    def fileList(self):
        return self._file_list

    @pyqtProperty(list, notify=playlistChanged)
    def visibleTracks(self):
        """The filtered library as QML-shaped items.

        Cached, and that is not an optimisation — it is a correctness fix.
        A QML binding elsewhere re-reads this on every ``trackChanged``, and
        the poll timer emits that signal up to 4x a second. Rebuilding 3470
        dicts per read (each one a title split plus a ``Path`` for the track
        key) put the main thread at ~12 ms per read and, measured with
        SIGUSR1 stack sampling, wedged the event loop for 33 s on a single
        track change. The list only changes when the tag/search filter does,
        which is exactly where the cache is dropped.
        """
        if self._visible_items is None:
            self._visible_items = [self._track_to_item(t) for t in self._visible_tracks]
        return self._visible_items

    @pyqtProperty(int, notify=artResolved)
    def artRevision(self):
        """Bumped whenever a background art lookup lands.

        QML cannot re-evaluate a binding on a slot call alone, so delegates fold
        this property into the expression to get re-queried when art arrives.
        """
        return self._art_revision

    @pyqtSlot(int, result=list)
    def upNext(self, count):
        """The `count` tracks that will actually play next, in playback order.

        This reads the player's own session queue rather than the visible
        library. Playback follows ``MusicPlayer._queue`` — a shuffled list of
        indices into ``active_playlist``, walked forward from ``_queue_pos``
        by ``_select_next_index()`` and skipping the anti-repeat window. A rail
        that instead showed the library from the playing track onwards was not
        a preview of anything: pressing Next jumped somewhere else, so the list
        looked like it reshuffled on every skip.

        The walk below mirrors ``_select_next_index`` exactly, including the
        ``_recent`` skip, with each pick added to a local copy of that window
        the way playing it would.

        The window is computed here, in Python, on purpose: a QML
        `property var` assigned from a list property does not snapshot it — it
        holds a sequence wrapper that re-reads the property on every index
        access, so scanning it re-marshalled 3470 dicts per row.

        Each item carries its own `index` into the visible list, because that
        is what `selectFile()` takes.
        """
        count = max(0, int(count))
        if count == 0 or self._player is None:
            return []

        playlist = getattr(self._player, "active_playlist", None) or []
        queue = getattr(self._player, "_queue", None) or []
        if not playlist or not queue:
            return self._up_next_by_library(count)

        pos = getattr(self._player, "_queue_pos", -1)
        try:
            pos = int(pos)
        except Exception:
            pos = -1

        # deque(maxlen=8); copy it so the preview cannot disturb playback.
        recent = list(getattr(self._player, "_recent", None) or [])
        recent_max = getattr(getattr(self._player, "_recent", None), "maxlen", 8) or 8
        anti_repeat = len(playlist) > len(recent)

        out = []
        seen = set()
        # One full lap is enough to fill any window; the +2 mirrors the
        # step budget _select_next_index gives itself.
        for _ in range(len(queue) + 2):
            if len(out) >= count:
                break
            pos = (pos + 1) % len(queue)
            idx = queue[pos]
            if not (0 <= idx < len(playlist)):
                continue
            track = playlist[idx]
            key = track_key(track)
            if not key or key in seen:
                continue
            if anti_repeat and key in recent:
                continue
            seen.add(key)
            recent.append(key)
            del recent[:-recent_max]
            item = dict(self._track_to_item(track))
            # selectFile() indexes the *visible* list, which is the playlist
            # before de-duplication, so the two can drift apart.
            item["index"] = self._visible_index_of_key(key)
            out.append(item)
        return out

    def _up_next_by_library(self, count):
        """Fallback for when the player has no session queue yet.

        Only reachable before the first playlist is handed to the player (the
        Music page can be opened before anything plays), so library order is
        the only order there is.
        """
        tracks = self._visible_tracks
        if not tracks:
            return []
        start = self._visible_index_of_current() + 1
        out = []
        for i in range(start, min(start + count, len(tracks))):
            item = dict(self._track_to_item(tracks[i]))
            item["index"] = i
            out.append(item)
        return out

    def _visible_index_of_current(self):
        """Position of the playing track in the visible list, or -1.

        Matched on the track key rather than the displayed title/artist pair,
        so a retitled or duplicate-named track cannot land the rail on the
        wrong song. The key->index map is built once per filter change and
        thrown away with the item cache; ``track_key`` walks a ``Path``, and
        doing that 3470 times on every track change is a visible hitch.
        """
        key = self._current_track_key or self._current_track_key_for_player()
        return self._visible_index_of_key(key)

    def _visible_index_of_key(self, key):
        """Position of a track key in the visible list, or -1.

        Keyed rather than matched on the displayed title/artist pair, so a
        retitled or duplicate-named track cannot land the rail on the wrong
        song. First occurrence wins, which is the one `dedupe_tracks` keeps
        when the player builds its playlist.
        """
        if not key:
            return -1
        if self._visible_index is None:
            index = {}
            for i, track in enumerate(self._visible_tracks):
                index.setdefault(track_key(track), i)
            self._visible_index = index
        return self._visible_index.get(key, -1)

    @pyqtSlot(int, result=str)
    def artForIndex(self, index):
        """Album-art file URL for one visible track, "" if unknown *yet*.

        Deliberately not a field on ``visibleTracks``: that list is the whole
        library (3400 tracks) and resolving art touches the file — a sidecar
        probe, and for embedded art a full tag parse plus a PNG write.

        Equally deliberately non-blocking. Doing that work inline stalled the
        event loop for up to 3.2 s the first time the rail was populated, which
        is exactly the kind of hitch this skin exists to remove. A miss queues
        the track for a worker thread and returns "" now; the row shows its
        placeholder and swaps to the cover when `artResolved` fires.
        """
        tracks = self._visible_tracks
        if not (0 <= index < len(tracks)):
            return ""
        track = tracks[index]
        key = track_key(track)
        cached = self._art_url_cache.get(key)
        if cached is not None:
            return cached
        self._queue_art_lookup(key, track)
        return ""

    def _queue_art_lookup(self, key, track):
        with self._art_lock:
            if key in self._art_pending:
                return
            self._art_pending.add(key)
            self._art_queue.append((key, track))
            if self._art_worker is None or not self._art_worker.is_alive():
                self._art_worker = threading.Thread(
                    target=self._art_worker_loop, name="art-resolver", daemon=True)
                self._art_worker.start()

    def _art_worker_loop(self):
        while True:
            with self._art_lock:
                if not self._art_queue:
                    self._art_worker = None
                    return
                key, track = self._art_queue.popleft()
            url = ""
            try:
                # Cheap paths first: sidecar image, art cache, and (only when
                # this happens to be the playing track) VLC's live ArtworkURL.
                art = self._resolve_album_art(track)
                if not art and self._player is not None:
                    # Nothing beside the file. Ask libvlc to parse this track
                    # in its own right — the only way to reach art embedded in
                    # a tag without mutagen, and safe here because this is a
                    # worker thread.
                    art = self._player.resolve_track_art_deep(track)
                if art:
                    url = Path(art).resolve().as_uri()
            except Exception:
                url = ""
            with self._art_lock:
                if len(self._art_url_cache) > 512:
                    self._art_url_cache.clear()
                self._art_url_cache[key] = url
                self._art_pending.discard(key)
            if url:
                self._art_revision += 1
                self.artResolved.emit()

    @pyqtProperty(list, notify=playlistChanged)
    def tagList(self):
        return list(self._tag_list)

    @pyqtProperty(str, notify=playlistChanged)
    def currentTag(self):
        return self._selected_tag

    # ── audio players ─────────────────────────────────────────────────────
    @pyqtProperty(list, notify=audioPlayersChanged)
    def audioPlayers(self):
        return list(self._audio_players)

    # ── progress polling ──────────────────────────────────────────────────
    def _poll_progress(self):
        if self._player is None:
            return
        try:
            pos = max(0, int(self._player.get_time_ms()))
            dur = max(0, int(self._player.get_length_ms()))
            playing = self._query_playing()
            sounddrive_active = self._query_sounddrive_active()
        except Exception:
            return

        track_key_now = self._current_track_key_for_player()
        track_changed = track_key_now != self._current_track_key
        progress_changed = (
            pos != self._position_ms
            or dur != self._duration_ms
            or playing != self._playing
        )
        sounddrive_changed = sounddrive_active != self._sound_drive_active

        self._position_ms = pos
        self._duration_ms = dur

        if track_changed:
            self._sync_from_player()
            if self._sound_drive_active and self._auto_mix_enabled and self._sound_drive_method == "damuci":
                self._apply_auto_mix_mode()
            return

        if playing != self._playing:
            self._playing = playing
            self.trackChanged.emit()

        if sounddrive_changed:
            self._sound_drive_active = sounddrive_active
            self.trackChanged.emit()

        if progress_changed:
            self.progressChanged.emit()
            self._update_lyrics_current_line(pos)

        if (
            dur > 0
            and pos > 0
            and pos >= max(0, dur - 800)
            and not getattr(self._player, "fade_tracks_enabled", False)
            and not getattr(self._player, "_xfade_timer", None)
            and not getattr(self._player, "_sd_enabled", False)
        ):
            try:
                title = self._player.next_song()
            except Exception:
                title = ""
            self._update_track_info(title)

    # ── playback controls ─────────────────────────────────────────────────
    def _query_playing(self) -> bool:
        try:
            mixer = getattr(self._player, "_stem_mixer", None)
            if getattr(self._player, "_sd_enabled", False) and mixer:
                return bool(mixer.is_playing())
            vlc_player = getattr(self._player, "player", None)
            if vlc_player:
                return bool(vlc_player.is_playing())
        except Exception:
            pass
        return self._playing

    def _query_sounddrive_active(self) -> bool:
        if self._player is None:
            return False
        try:
            if getattr(self._player, "is_damuci_sounddrive_active", None):
                if self._player.is_damuci_sounddrive_active():
                    return True
            if getattr(self._player, "is_realtime_sounddrive_active", None):
                if self._player.is_realtime_sounddrive_active():
                    return True
        except Exception:
            pass
        return False

    @pyqtSlot()
    def play(self):
        """Play — directional, not a toggle.

        Play and Pause both used to call togglePlayPause(), so a head unit
        sending a state-sync Pause when it connects *started* playback. Worse,
        with nothing loaded `toggle_play_pause()` falls through to
        `next_song()`, so it began a track outright. Play is now a no-op when
        already playing.
        """
        if not self._player or self._playing:
            return
        self.togglePlayPause()

    @pyqtSlot()
    def pause(self):
        """Pause — a no-op when already paused (see play())."""
        if not self._player or not self._playing:
            return
        self.togglePlayPause()

    @pyqtSlot()
    def togglePlayPause(self):
        if not self._player:
            return
        self._player.toggle_play_pause()
        self._sync_from_player()

    @pyqtSlot()
    def next(self):
        if not self._player:
            return
        title = self._player.next_song()
        self._update_track_info(title)

    @pyqtSlot()
    def prev(self):
        if self._player is None:
            return
        try:
            queue = getattr(self._player, "_queue", None)
            if queue:
                qpos = getattr(self._player, "_queue_pos", 0) or 0
                qpos = (qpos - 1) % len(queue)
                self._player._queue_pos = qpos
                prev_idx = queue[qpos]
            else:
                playlist = getattr(self._player, "active_playlist", []) or []
                idx = getattr(self._player, "current_index", 0) or 0
                if not playlist:
                    return
                prev_idx = (idx - 1) % len(playlist)
            title = self._player.play_song(index=prev_idx, use_fade=self.fadeSkipEnabled)
        except Exception:
            return
        self._update_track_info(title)

    @pyqtSlot(int)
    def seek(self, ms):
        if not self._player:
            return
        self._player.set_time_ms(max(0, int(ms)))
        self._poll_progress()
        QTimer.singleShot(60, self._poll_progress)

    @pyqtSlot(float)
    def seekFraction(self, fraction):
        if self._duration_ms <= 0:
            return
        self.seek(int(max(0.0, min(1.0, fraction)) * self._duration_ms))

    @pyqtSlot(int)
    def setVolume(self, volume):
        self._volume = max(0, min(100, int(volume)))
        if self._duck_saved_volume is not None:
            # Deliberate change while ducked: that becomes the level to return
            # to, otherwise releasing the duck would undo the user's own move.
            self._duck_saved_volume = self._volume
        if self._player:
            self._player.set_volume(self._volume)
        self._sync_split_output_volume(self._volume, include_default=True)
        self.volumeChanged.emit()
        self.refreshAudioPlayers()

    # ── external ducking (AirPlay) ───────────────────────────────────────
    @pyqtSlot(int)
    def duckForExternal(self, percent):
        """Lower music by `percent` while something else is playing over it.

        Applied to the player's own volume rather than to the PulseAudio stream,
        so the exact pre-duck level can be handed back afterwards even if the
        user changes it mid-duck (the delta is re-applied on release).
        """
        pct = max(0, min(100, int(percent)))
        if self._player is None or pct <= 0:
            return
        if self._duck_saved_volume is None:
            self._duck_saved_volume = self._volume
        target = int(round(self._duck_saved_volume * (1.0 - pct / 100.0)))
        target = max(0, min(100, target))
        if target == self._volume:
            return
        self._volume = target
        try:
            self._player.set_volume(target)
        except Exception:
            pass
        self._sync_split_output_volume(target, include_default=True)
        self.volumeChanged.emit()

    @pyqtSlot()
    def releaseExternalDuck(self):
        saved = self._duck_saved_volume
        self._duck_saved_volume = None
        if saved is None or self._player is None:
            return
        self._volume = max(0, min(100, int(saved)))
        try:
            self._player.set_volume(self._volume)
        except Exception:
            pass
        self._sync_split_output_volume(self._volume, include_default=True)
        self.volumeChanged.emit()


    @pyqtSlot(bool)
    def setMute(self, muted):
        if self._player is None:
            return
        try:
            vlc_player = getattr(self._player, "player", None)
            if vlc_player:
                vlc_player.audio_set_mute(bool(muted))
        except Exception:
            pass
        self.refreshAudioPlayers()

    # ── playlist browsing / filtering ────────────────────────────────────
    @pyqtSlot(str)
    def browseFolder(self, folder_path):
        self._current_folder = folder_path or MUSIC_FOLDER
        self._reload_library()

    @pyqtSlot(str)
    def setSearchQuery(self, query):
        normalized = (query or "").strip().lower()
        if normalized == self._search_query:
            return
        self._search_query = normalized
        self._apply_filters()

    @pyqtSlot()
    def clearSearch(self):
        self.setSearchQuery("")

    @pyqtSlot(str)
    def filterByTag(self, tag):
        normalized = self._normalize_tag(tag)
        if normalized == self._selected_tag:
            return
        self._selected_tag = normalized
        self._apply_filters()

    @pyqtSlot(int)
    def selectFile(self, index):
        if self._player is None or index < 0 or index >= len(self._visible_tracks):
            return
        track = self._visible_tracks[index]
        self._activate_track(track)


    @pyqtSlot(bool)
    def setSoundDriveEnabled(self, enabled):
        enabled = bool(enabled)
        if enabled == self._sound_drive_enabled:
            return
        self._sound_drive_enabled = enabled
        if not enabled:
            self._player_sd_session = False
        self._save_music_settings()

        if enabled and self._player_sd_session and self._current_track_key_for_player():
            self._enable_player_sounddrive()
        else:
            self._disable_player_sounddrive()

        self._sync_from_player()

    @pyqtSlot(bool)
    def setPlayerSoundDrive(self, enabled):
        enabled = bool(enabled)
        if not self._sound_drive_enabled:
            enabled = False
        if enabled == self._player_sd_session:
            return
        self._player_sd_session = enabled
        if enabled:
            self._vol_before_sd_session = self._volume
            self._enable_player_sounddrive()
        else:
            self._disable_player_sounddrive()
        self._save_music_settings()
        self._sync_from_player()
        if not enabled:
            self._volume = self._vol_before_sd_session
            self.volumeChanged.emit()

    @pyqtSlot(str)
    def setSoundDriveMethod(self, method):
        normalized = self._normalize_method(method)
        if normalized == self._sound_drive_method:
            return
        self._sound_drive_method = normalized
        if normalized != "damuci" and self._auto_mix_enabled:
            self._auto_mix_enabled = False
        self._save_music_settings()
        self._apply_runtime_sounddrive_settings()
        if (
            self._sound_drive_enabled
            and self._player_sd_session
            and self._current_track_key_for_player()
        ):
            self._enable_player_sounddrive()
        else:
            self._disable_player_sounddrive()
        self._sync_from_player()

    @pyqtSlot(str)
    def setSoundDrivePerformance(self, mode):
        normalized = self._normalize_performance(mode)
        if normalized == self._sound_drive_performance:
            return
        self._sound_drive_performance = normalized
        self._save_music_settings()
        self._apply_runtime_sounddrive_settings()
        if self._player_sd_session and self._sound_drive_method == "realtime":
            self._enable_player_sounddrive()
        self.trackChanged.emit()

    @pyqtSlot(bool)
    def setSoundDriveMetadataCache(self, enabled):
        enabled = bool(enabled)
        if enabled == self._sound_drive_metadata_cache:
            return
        self._sound_drive_metadata_cache = enabled
        self._save_music_settings()
        self._apply_runtime_sounddrive_settings()
        if self._player_sd_session and self._sound_drive_method == "realtime":
            self._enable_player_sounddrive()
        self.trackChanged.emit()


    @pyqtSlot(str)
    def setMixMode(self, mode):
        normalized = self._normalize_mix_mode(mode)
        if normalized == self._mix_mode:
            return
        self._mix_mode = normalized
        if self._player:
            try:
                mixer = getattr(self._player, "_stem_mixer", None)
                if mixer:
                    mixer.set_mix_mode(normalized)
                sounddrive = getattr(self._player, "sounddrive", None)
                if sounddrive:
                    sounddrive.set_mix_mode(normalized)
            except Exception:
                pass
        self._save_music_settings()
        self.trackChanged.emit()

    @pyqtSlot(bool)
    def setFadeSkip(self, enabled):
        if self._player is None:
            return
        try:
            self._player.set_crossfade(bool(enabled), duration_ms=3000)
        except Exception:
            self._player.fade_skip_enabled = bool(enabled)
            self._player.crossfade_enabled = bool(enabled)
        self._save_music_flag("fade_skip", enabled)
        self.trackChanged.emit()

    @pyqtSlot(bool)
    def setFadeTracks(self, enabled):
        if self._player is None:
            return
        try:
            self._player.set_fade_tracks(bool(enabled), duration_ms=3000)
        except Exception:
            self._player.fade_tracks_enabled = bool(enabled)
        self._save_music_flag("fade_tracks", enabled)
        self.trackChanged.emit()


    def _enable_player_sounddrive(self):
        if self._player is None or not self._sound_drive_enabled:
            self._sound_drive_active = False
            return
        if not self._sound_drive_method:
            self._sound_drive_active = False
            return
        try:
            if self._sound_drive_method == "realtime":
                self._player.enable_realtime_sounddrive(
                    performance_mode=self._sound_drive_performance,
                    metadata_cache_enabled=self._sound_drive_metadata_cache,
                )
            else:
                if self._auto_mix_enabled:
                    self._apply_auto_mix_mode()
                self._player.enable_damuci_sounddrive(mix_mode=self._mix_mode)
            self._sound_drive_active = self._query_sounddrive_active()
        except Exception as e:
            print(f"[Bridge] _enable_player_sounddrive failed: {e}")
            self._sound_drive_active = False

    def _disable_player_sounddrive(self):
        if self._player is None:
            self._sound_drive_active = False
            return
        try:
            self._player.disable_all_sounddrive()
        except Exception:
            pass
        self._sound_drive_active = False

    # ── Split Audio ───────────────────────────────────────────────────────
    def _split_device_entry(self, device_id: str) -> dict:
        for device in self._split_device_list:
            if isinstance(device, dict) and device.get("id") == device_id:
                return dict(device)
        return {"id": device_id, "name": device_id, "displayName": device_id}

    def _sync_player_split_route(self, enabled: bool, device_id: str = ""):
        if self._player is None:
            return
        try:
            self._player.router = self._router
        except Exception:
            pass
        try:
            self._player.set_selected_device(self._split_device_entry(device_id) if enabled and device_id else None)
        except Exception:
            pass
        try:
            self._player.set_output_route("Mirror to Device" if enabled and device_id else "Default")
        except Exception:
            pass

    def _apply_split_audio_enabled(self, enabled: bool) -> bool:
        enabled = bool(enabled)
        device_id = self.splitAudioDeviceId
        if not enabled:
            self._disable_engine_rear()
            self._stop_sink_watch()
            try:
                self._router.teardown_bus()
            except Exception:
                pass
            self._sync_player_split_route(False)
            self._split_active = False
            self._fb_split_active = False
            return True

        if not device_id:
            self.refreshSplitAudioDevices()
            self._sync_player_split_route(False)
            self._split_active = False
            self._fb_split_active = False
            return False

        self._fb_split_active = False
        try:
            self._router.set_user_delay(self.splitAudioDelayMs)
            self._router.set_delay_direction(self.splitAudioInvertDelay)
            ok = bool(self._router.ensure_bus_mirror(device_id))
            self._sync_player_split_route(ok, device_id)
            moved = bool(self._router.move_main_to_bus("GTR_MAIN"))
            if not moved:
                QTimer.singleShot(300, lambda: self._router.move_main_to_bus("GTR_MAIN"))
                QTimer.singleShot(900, lambda: self._router.move_main_to_bus("GTR_MAIN"))
            try:
                split_vol = settings_cache.getint("audio", "split_volume", fallback=self._volume, path=DATA_SETTINGS)
                self._sync_split_output_volume(split_vol, include_default=False)
            except Exception:
                pass
            self._split_active = ok
            if ok:
                self._refresh_split_volume()
                self._start_sink_watch()
                if self._split_balance:
                    balance = self._split_balance
                    self._audio_op(lambda: self._router.set_front_rear_balance(balance))
                if self._split_auto_delay:
                    # Bluetooth latency differs between connections, so a
                    # sticky Auto re-measures whenever the route is rebuilt.
                    QTimer.singleShot(1200, self.probeSplitDelay)
            else:
                self._stop_sink_watch()
            # Restore engine-rear routing if it was active before
            if ok and self._split_engine_sound and self.splitAudio4chActive:
                self._enable_engine_rear()
            return ok
        except Exception as e:
            print(f"[MusicBridge] split audio apply failed: {e}")
            self._sync_player_split_route(False)
            self._split_active = False
            return False

    @pyqtSlot(bool)
    def toggleSplitAudio(self, on):
        settings_cache.set_value("audio", "split_enabled", "1" if bool(on) else "0", path=DATA_SETTINGS)
        self._apply_split_audio_enabled(bool(on))
        self.splitAudioChanged.emit()

    @pyqtSlot(str)
    def setSplitAudioDevice(self, device_id):
        settings_cache.set_value("audio", "device_id", str(device_id or ""), path=DATA_SETTINGS)
        if self.splitAudioEnabled:
            self._apply_split_audio_enabled(True)
        else:
            self._sync_player_split_route(False)
        self.splitAudioChanged.emit()

    @pyqtSlot(int)
    def setSplitAudioDelay(self, ms):
        ms = max(0, min(2000, int(ms)))
        settings_cache.set_value("audio", "delay_ms", str(ms), path=DATA_SETTINGS)
        self._split_auto_delay = False  # manual override clears auto flag
        self._split_delay_status = "Set by hand"
        try:
            self._router.set_user_delay(ms)
        except Exception:
            pass
        if self.splitAudioEnabled:
            self._apply_split_audio_enabled(True)
        self.splitAudioChanged.emit()

    @pyqtSlot(bool)
    def setSplitAudioInvertDelay(self, invert):
        invert = bool(invert)
        settings_cache.set_value("audio", "invert_delay", "1" if invert else "0", path=DATA_SETTINGS)
        try:
            self._router.set_delay_direction(invert)
        except Exception:
            pass
        if self.splitAudioEnabled:
            self._apply_split_audio_enabled(True)
        self.splitAudioChanged.emit()

    @pyqtSlot(int)
    def setSplitAudioVolume(self, volume):
        vol = int(max(0, min(150, volume)))
        self._split_volume = vol
        settings_cache.set_value("audio", "split_volume", str(vol), path=DATA_SETTINGS)
        self._sync_split_output_volume(vol, include_default=False)
        self.splitAudioChanged.emit()

    @pyqtSlot()
    def probeSplitDelay(self):
        """Measure the offset between the two outputs and apply it.

        Runs on the audio worker: it reads `pactl list sinks` and then rebuilds
        the mirror, both of which are subprocess work that used to happen on the
        Qt main thread. It also no longer requires one leg to be Bluetooth — the
        old version simply returned, with no feedback, for any other pair, which
        is why the Auto button looked dead.
        """
        if not self.splitAudioEnabled or not self._split_active:
            self._split_delay_status = "Turn split audio on first"
            self.splitAudioChanged.emit()
            return

        self._split_delay_status = "Measuring…"
        self.splitAudioChanged.emit()

        def _work():
            try:
                ms, note = self._router.probe_pair_latency_ms()
            except Exception as e:
                ms, note = None, f"Probe failed: {e}"
            if ms is None:
                self._split_auto_delay = False
                self._split_delay_status = note
                self.splitAudioChanged.emit()
                return
            settings_cache.set_value("audio", "delay_ms", str(int(ms)), path=DATA_SETTINGS)
            self._split_auto_delay = True
            self._split_delay_status = note
            try:
                self._router.set_user_delay(int(ms))
                self._router.move_main_to_bus("GTR_MAIN")
            except Exception:
                pass
            self.splitAudioChanged.emit()

        self._audio_op(_work)

    # ── output-drop guard ────────────────────────────────────────────────
    def _start_sink_watch(self):
        """Watch for either split leg disappearing.

        Bluetooth is the one that actually goes: the Renault's head unit drops
        the A2DP link and PulseAudio removes `bluez_output.*`. Every stream
        pointed at it — including our own front-leg loopback — is then moved to
        the new default sink, which is the USB device the rear leg already
        feeds. Both legs land on the same speakers, the rear one still carrying
        the delay meant to compensate for Bluetooth, and the result is the
        doubled, echoing playback the user hears as "two traces at the same
        time". Nothing in PulseAudio undoes that on its own, so the mirror has
        to be torn down when a leg vanishes.
        """
        if self._sink_watcher is not None:
            return
        try:
            from Modules.Music.audio_router import SinkWatcher
            watcher = SinkWatcher(self._on_sink_event)
            if watcher.start():
                self._sink_watcher = watcher
        except Exception:
            self._sink_watcher = None

    def _stop_sink_watch(self):
        watcher = self._sink_watcher
        self._sink_watcher = None
        if watcher is not None:
            try:
                watcher.stop()
            except Exception:
                pass

    def _on_sink_event(self):
        """Called from the SinkWatcher thread on any sink add/remove/change.

        Coalesced: a Bluetooth teardown emits a burst of events, and each check
        costs a `pactl list short sinks`.
        """
        # A sink coming or going is also the earliest sign the head unit's
        # link changed — let the auto-connect loop look now instead of at its
        # next 30 s tick (it rate-limits itself).
        if self._bt_auto is not None:
            self._bt_auto.kick()
        if self._split_guard_pending or not self._split_active:
            return
        self._split_guard_pending = True
        self._audio_op(self._check_split_legs)

    def _check_split_legs(self):
        self._split_guard_pending = False
        if not self._split_active:
            return
        try:
            def_sink, sel_sink = self._router._last_pair
        except Exception:
            return
        if not def_sink:
            return

        try:
            from Modules.Music.audio_router import list_sinks_short
            present = set(list_sinks_short().keys())
        except Exception:
            return
        if not present:
            return

        missing = [s for s in (def_sink, sel_sink) if s and s not in present]
        if not missing:
            return

        gone = missing[0]
        label = "Bluetooth output" if "bluez_output." in gone else "second output"
        if not self.splitDropGuard:
            self._split_delay_status = f"{label} disappeared — split may be doubled"
            self.splitAudioChanged.emit()
            return

        # Drop back to a single output. teardown_bus() unloads both loopbacks,
        # so whatever sink is left becomes the only one playing.
        settings_cache.set_value("audio", "split_enabled", "0", path=DATA_SETTINGS)
        self._apply_split_audio_enabled(False)
        self._split_delay_status = f"Split off — {label} disconnected"
        self.splitAudioChanged.emit()

    @pyqtSlot()
    def refreshSplitAudioDevices(self):
        try:
            self._split_device_list = self._router.list_devices()
        except Exception:
            self._split_device_list = []
        self.splitAudioChanged.emit()

    def _load_split_audio_state(self):
        try:
            from Modules.Music.audio_router import force_cleanup_any_laladash_modules
            force_cleanup_any_laladash_modules()
        except Exception:
            pass
        try:
            self._split_device_list = self._router.list_devices()
        except Exception:
            self._split_device_list = []
        try:
            self._router.set_user_delay(self.splitAudioDelayMs)
            self._router.set_delay_direction(self.splitAudioInvertDelay)
        except Exception:
            pass
        # Always start with split off — user must enable explicitly each session.
        self._split_auto_delay = False
        self._split_volume = settings_cache.getint(
            "audio", "split_volume", fallback=self._volume, path=DATA_SETTINGS
        )
        self._split_balance = max(-100, min(100, settings_cache.getint(
            "audio", "balance", fallback=0, path=DATA_SETTINGS
        )))
        self._invert_lr = settings_cache.getboolean("audio", "invert_lr", fallback=False, path=DATA_SETTINGS)
        self._split_engine_sound = settings_cache.getboolean("audio", "split_engine_sound", fallback=False, path=DATA_SETTINGS)
        self._saved_engine_sink = ""
        settings_cache.set_value("audio", "split_enabled", "0", path=DATA_SETTINGS)
        self._split_active = False
        self._apply_split_audio_enabled(False)
        self.splitAudioChanged.emit()

    # ── audio mixer surface ───────────────────────────────────────────────
    @pyqtSlot()
    def refreshAudioPlayers(self):
        """Enumerate players on the audio worker — `pactl list sink-inputs`
        is a subprocess and this is reached from setVolume(), i.e. from every
        volume step."""
        self._audio_op(self._collect_audio_players)

    def _collect_audio_players(self):
        players: list[dict] = []

        try:
            alive = VLC_REGISTRY.list_alive()
        except Exception:
            alive = {}
        for name, entry in alive.items():
            players.append(
                {
                    "id": f"app:{name}",
                    "name": str(entry.name),
                    "group": str(entry.group or "App"),
                    "kind": "app",
                    "volume": int(max(0, min(150, entry.volume_hint))),
                }
            )

        try:
            streams = SystemAudio.list_vlc_streams()
        except Exception:
            streams = []
        for stream in streams:
            players.append(
                {
                    "id": f"stream:{stream.stream_id}",
                    "name": str(stream.name),
                    "group": str(stream.app or "System"),
                    "kind": "system",
                    "volume": int(max(0, min(150, stream.vol_percent))),
                }
            )

        self._audio_players = players
        self.audioPlayersChanged.emit()

    @pyqtSlot(str, int)
    def setAudioPlayerVolume(self, player_id, volume):
        if not player_id:
            return
        vol = int(max(0, min(150, volume)))
        if player_id.startswith("app:"):
            name = player_id[4:]
            self._audio_op(lambda: VLC_REGISTRY.set_volume(name, vol))
            if self._player and name == "Music: Main":
                self._volume = max(0, min(100, vol))
                self._sync_split_output_volume(self._volume, include_default=True)
                self.volumeChanged.emit()
        elif player_id.startswith("stream:"):
            sid = player_id[7:]
            self._audio_op(lambda: SystemAudio.set_stream_volume(sid, vol))
        self.refreshAudioPlayers()

    # ── pactl worker ──────────────────────────────────────────────────────
    def _audio_op(self, fn):
        """Queue a pactl-touching callable onto the serialised audio worker.

        Ordering matters (a volume write must not overtake a route change), so
        this is one worker rather than a thread per call.
        """
        self._audio_ops.append(fn)
        if self._audio_ops_thread is None or not self._audio_ops_thread.is_alive():
            self._audio_ops_stop = False
            self._audio_ops_thread = threading.Thread(
                target=self._audio_ops_loop, daemon=True, name="MusicAudioOps"
            )
            self._audio_ops_thread.start()
        self._audio_ops_event.set()

    def _audio_ops_loop(self):
        while not self._audio_ops_stop:
            if not self._audio_ops:
                self._audio_ops_event.wait(1.0)
                self._audio_ops_event.clear()
                continue
            try:
                fn = self._audio_ops.popleft()
            except IndexError:
                continue
            try:
                fn()
            except Exception:
                pass

    def _stop_audio_ops(self):
        self._audio_ops_stop = True
        self._audio_ops_event.set()
        t = self._audio_ops_thread
        if t is not None and t.is_alive():
            t.join(timeout=0.5)
        self._audio_ops_thread = None

    def _refresh_split_volume(self):
        """Read the split device's real volume on the worker, then publish."""
        device_id = self.splitAudioDeviceId
        if not device_id:
            return

        def _work():
            try:
                from Modules.Music.audio_router import get_device_volume
                vol = get_device_volume(device_id)
            except Exception:
                vol = None
            if vol is None:
                return
            clamped = int(max(0, min(150, vol)))
            if clamped != self._split_volume:
                self._split_volume = clamped
                # Cross-thread emit; Qt queues it to the GUI thread.
                self.splitAudioChanged.emit()

        self._audio_op(_work)

    def _sync_split_output_volume(self, volume: int, include_default: bool = False):
        if not self.splitAudioEnabled:
            return
        device_id = self.splitAudioDeviceId
        if not device_id:
            return
        vol = int(max(0, min(150, volume)))
        self._split_volume = vol

        def _work():
            try:
                from Modules.Music.audio_router import set_default_volume, set_device_volume
                if include_default:
                    set_default_volume(vol)
                set_device_volume(device_id, vol)
            except Exception:
                pass

        self._audio_op(_work)

    # ── helpers ───────────────────────────────────────────────────────────
    def _sync_from_player(self):
        if self._player is None:
            return

        track = self._current_track_dict()
        if track:
            artist, title = self._split_artist_title(track.get("title", ""))
            self._album_art = self._resolve_album_art(track)
            self._current_track_key = track_key(track)
        else:
            raw_title = getattr(self._player, "current_track", "") or ""
            artist, title = self._split_artist_title(raw_title)
            self._album_art = ""
            self._current_track_key = self._current_track_key_for_player()

        self._title = title
        self._artist = artist
        self._duration_ms = max(0, int(self._player.get_length_ms()))
        self._position_ms = max(0, int(self._player.get_time_ms()))
        self._playing = self._query_playing()
        self._sound_drive_active = self._query_sounddrive_active()
        self._volume = int(getattr(self._player, "_user_volume", self._volume) or self._volume)
        if (
            self._player_sd_session
            and self._sound_drive_enabled
            and self._current_track_key
            and not self._sound_drive_active
            and self._sounddrive_restore_attempt_key != self._current_track_key
        ):
            self._sounddrive_restore_attempt_key = self._current_track_key
            self._enable_player_sounddrive()
            self._sound_drive_active = self._query_sounddrive_active()

        self.trackChanged.emit()
        self.progressChanged.emit()
        self.volumeChanged.emit()

        # If art is still missing for the current track, VLC may not have
        # finished parsing the embedded art yet. Schedule a retry (only if
        # not already in a retry loop).
        if not self._album_art and track and self._art_retry_count == 0:
            self._schedule_art_retry()

        # Resolve lyrics for the new track. _load_lyrics_for_current()
        # handles local lookup synchronously and dispatches an LRCLIB fetch
        # on a daemon thread if needed.
        self._load_lyrics_for_current()

    def _update_track_info(self, raw_title):
        if raw_title or self._current_track_dict():
            self._sync_from_player()
            if (
                self._sound_drive_enabled
                and self._player_sd_session
                and self._current_track_key_for_player()
            ):
                self._enable_player_sounddrive()
                self._sync_from_player()

    def _schedule_art_retry(self):
        """Schedule first art retry 2.5 s from now (resets counter)."""
        self._art_retry_count = 1
        self._art_retry_timer.start()

    def _retry_art(self):
        """Called 2.5 s after a track starts when art was initially missing.
        Retries up to 3 times (7.5 s total) to handle slow VLC parsing."""
        if self._player is None or self._album_art:
            self._art_retry_count = 0
            return
        self._sync_from_player()
        # _sync_from_player will not re-schedule (count > 0) so manage here.
        if not self._album_art and self._art_retry_count < 3:
            self._art_retry_count += 1
            self._art_retry_timer.start()
        else:
            self._art_retry_count = 0

    # ── Lyrics ───────────────────────────────────────────────────────────
    def _reset_lyrics_state(self):
        self._lyrics_lines = []
        self._lyrics_synced = False
        self._lyrics_partial = False
        self._lyrics_available = False
        self._lyrics_source = ""
        self._lyrics_current_idx = -1

    def _load_lyrics_for_current(self):
        """Invoked when the playing track changes. Consults *local* sources
        only — the Lyrics folder, a sidecar .lrc, or an embedded USLT/LYRICS
        tag. There is no network fetch: lyrics are read whenever available."""
        track = self._current_track_dict()
        if not track:
            self._reset_lyrics_state()
            self.lyricsChanged.emit()
            return

        title = self._title or ""
        artist = self._artist or ""
        audio_path = self._track_audio_path(track) or ""

        result = None
        try:
            result = _lyrics.load_lyrics_for_audio(audio_path, title, artist)
        except Exception:
            result = None

        if result is not None:
            self._apply_lyrics_result_obj(result)
            return

        # No local lyrics for this track — show the empty state.
        self._reset_lyrics_state()
        self.lyricsChanged.emit()

    def _apply_lyrics_result_obj(self, result):
        """Apply a LyricsResult dataclass (local source) to bridge state."""
        lines = result.lines or []
        self._lyrics_lines = [
            {"time": int(l.time_ms or 0), "text": l.text,
             "estimated": bool(getattr(l, "estimated", False))}
            for l in lines
        ]
        self._lyrics_synced = bool(result.synced)
        self._lyrics_partial = bool(getattr(result, "partial", False))
        self._lyrics_available = bool(lines)
        self._lyrics_source = str(result.source or "")
        # Default the current line to 0 for synced lyrics so the overlay
        # immediately highlights line 1 during the song's intro instead of
        # showing nothing until the first stamp matches.
        self._lyrics_current_idx = 0 if (lines and result.synced) else -1
        self.lyricsChanged.emit()

    def _update_lyrics_current_line(self, position_ms):
        if not self._lyrics_synced or not self._lyrics_lines:
            return
        pos = int(position_ms or 0)
        cur = self._lyrics_current_idx
        # Forward scan from current index; jump back if we've rewound.
        if cur >= 0 and cur < len(self._lyrics_lines):
            if pos < self._lyrics_lines[cur]["time"]:
                cur = -1   # rewound past current line; rescan from start
        next_idx = cur
        n = len(self._lyrics_lines)
        i = cur + 1 if cur >= 0 else 0
        while i < n and self._lyrics_lines[i]["time"] <= pos:
            next_idx = i
            i += 1
        # During the song intro (position is earlier than the first lyric's
        # timestamp), no stamp has matched yet so `next_idx` is still -1.
        # Default to line 0 instead so the overlay always shows the first
        # line — visually "the song hasn't reached the lyrics yet, but
        # here's what's coming first" — rather than going blank.
        if next_idx < 0:
            next_idx = 0
        if next_idx != self._lyrics_current_idx:
            self._lyrics_current_idx = next_idx
            self.lyricsCurrentLineChanged.emit()

    def _reload_library(self):
        music_data = list(getattr(self._player, "music_data", []) or []) if self._player else []
        self._library = sorted(music_data, key=lambda item: str(item.get("title", "")).lower())
        self._current_folder = MUSIC_FOLDER
        self._build_tag_list()
        self._apply_filters()

    def _apply_filters(self):
        tracks = list(self._library)
        selected_tag = self._selected_tag

        if selected_tag == "Stems":
            tracks = [track for track in tracks if self._has_stems(track)]
        elif selected_tag != "All":
            # Compare canonical keys on both sides so mixed-case JSON tags
            # ("Nostalgia") match the canonical chip name ("nostalgia").
            wanted = canonical_tag_key(selected_tag)
            tracks = [
                track for track in tracks
                if wanted in {canonical_tag_key(t) for t in (track.get("tags") or [])}
            ]

        query = self._search_query
        if query:
            filtered = []
            for track in tracks:
                haystacks = [
                    self._display_title(track).lower(),
                    str(track.get("title", "")).lower(),
                ]
                if any(query in text for text in haystacks):
                    filtered.append(track)
            tracks = filtered

        self._visible_tracks = tracks
        self._visible_items = None          # filter moved — drop the caches
        self._visible_index = None
        self._file_list = [self._display_title(track) for track in tracks]
        self._sync_player_bucket()
        self.playlistChanged.emit()

    def _sync_player_bucket(self):
        if self._player is None:
            return
        bucket = list(self._visible_tracks)
        try:
            self._player.set_active_playlist(bucket, keep_current=True, reshuffle=self._shuffle)
        except Exception:
            self._player.active_playlist = bucket
            self._player.current_index = 0 if bucket else -1

    def _activate_track(self, track):
        if self._player is None:
            return
        self._sync_player_bucket()
        idx = self._match_track_index(track, getattr(self._player, "active_playlist", []) or [])
        if idx < 0:
            return
        try:
            queue = getattr(self._player, "_queue", None)
            if queue and idx in queue:
                self._player._queue_pos = queue.index(idx)
        except Exception:
            pass
        try:
            title = self._player.play_song(index=idx, use_fade=False)
        except Exception:
            title = ""
        self._update_track_info(title)

    def _current_track_dict(self):
        if self._player is None:
            return None

        player_key = str(getattr(self._player, "get_current_track_key", lambda: "")() or "").strip()
        if player_key:
            for collection in (
                getattr(self._player, "active_playlist", None) or [],
                self._library,
            ):
                for track in collection:
                    if track_key(track) == player_key:
                        return track

        player_path = str(getattr(self._player, "get_current_track_path", lambda: "")() or "").strip()
        if player_path:
            normalized = os.path.normpath(player_path)
            for collection in (
                getattr(self._player, "active_playlist", None) or [],
                self._library,
            ):
                for track in collection:
                    if os.path.normpath(self._track_audio_path(track)) == normalized:
                        return track

        playlist = getattr(self._player, "active_playlist", None) or []
        idx = getattr(self._player, "current_index", -1)
        if isinstance(idx, int) and 0 <= idx < len(playlist):
            track = playlist[idx]
            if isinstance(track, dict):
                return track

        current_title = str(getattr(self._player, "current_track", "") or "")
        for track in self._library:
            if str(track.get("title", "") or "") == current_title:
                return track
        return None

    def _current_track_key_for_player(self):
        if self._player is None:
            return ""
        player_key = str(getattr(self._player, "get_current_track_key", lambda: "")() or "").strip()
        if player_key:
            return player_key
        track = self._current_track_dict()
        if track:
            return track_key(track)
        return str(getattr(self._player, "current_track", "") or "")

    def _resolve_album_art(self, track):
        if self._player is None or not track:
            return ""
        try:
            art = self._player.get_track_art_path(track)
        except Exception:
            art = None
        return art if art and os.path.exists(art) else ""

    def _apply_auto_mix_mode(self):
        if self._sound_drive_method != "damuci":
            return
        self.setMixMode(random.choice(("Mode A", "Mode B", "Mode C")))

    def _apply_runtime_sounddrive_settings(self):
        if self._player is None:
            return
        try:
            sounddrive = getattr(self._player, "sounddrive", None)
            if sounddrive:
                sounddrive.set_mix_mode(self._mix_mode)
        except Exception:
            pass
        try:
            realtime = getattr(self._player, "realtime_sounddrive", None)
            if realtime:
                realtime.set_performance_mode(self._sound_drive_performance)
                realtime.set_metadata_cache_enabled(self._sound_drive_metadata_cache)
        except Exception:
            pass
        try:
            self._player.set_volume(self._volume)
        except Exception:
            pass
        self.setFadeSkip(self._music_flag("fade_skip", False))
        self.setFadeTracks(self._music_flag("fade_tracks", False))

    def _load_music_settings(self):
        cfg = settings_cache.load_copy(DATA_SETTINGS)
        normalized = apply_normalized_music_settings(cfg)
        settings_cache.save(cfg, DATA_SETTINGS)

        self._sound_drive_enabled = bool(normalized["sound_drive"])
        self._auto_mix_enabled = bool(normalized["auto_mix"])
        self._mix_mode = str(normalized["mix_mode"])
        self._sound_drive_method = str(normalized["sound_drive_method"])
        self._sound_drive_performance = str(normalized["sound_drive_performance"])
        self._sound_drive_metadata_cache = bool(normalized["sound_drive_metadata_cache"])
        self._player_sd_session = bool(normalized["sound_drive_player_active"]) and self._sound_drive_enabled
        self._sound_drive_active = False
        self._shuffle = True

    def _save_music_settings(self):
        cfg = settings_cache.load_copy(DATA_SETTINGS)
        apply_normalized_music_settings(
            cfg,
            {
                "sound_drive": self._sound_drive_enabled,
                "auto_mix": self._auto_mix_enabled,
                "mix_mode": self._mix_mode,
                "sound_drive_method": self._sound_drive_method,
                "sound_drive_performance": self._sound_drive_performance,
                "sound_drive_metadata_cache": self._sound_drive_metadata_cache,
                "sound_drive_player_active": bool(self._player_sd_session),
            },
        )
        settings_cache.save(cfg, DATA_SETTINGS)

    def _music_flag(self, key, default=False):
        return settings_cache.getboolean("Music", key, fallback=default, path=DATA_SETTINGS)

    def _save_music_flag(self, key, enabled):
        settings_cache.set_value("Music", key, "1" if enabled else "0", path=DATA_SETTINGS)

    # ── Session save / restore ────────────────────────────────────────────

    @pyqtProperty(bool, notify=sessionAvailableChanged)
    def sessionAvailable(self):
        try:
            if not os.path.isfile(_SESSION_FILE):
                return False
            with open(_SESSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return bool(data.get("track_path") or data.get("title"))
        except Exception:
            return False

    @pyqtProperty(str, notify=sessionAvailableChanged)
    def sessionTitle(self):
        try:
            if os.path.isfile(_SESSION_FILE):
                with open(_SESSION_FILE, "r", encoding="utf-8") as f:
                    return str(json.load(f).get("title", "") or "")
        except Exception:
            pass
        return ""

    @pyqtProperty(str, notify=sessionAvailableChanged)
    def sessionArtist(self):
        try:
            if os.path.isfile(_SESSION_FILE):
                with open(_SESSION_FILE, "r", encoding="utf-8") as f:
                    return str(json.load(f).get("artist", "") or "")
        except Exception:
            pass
        return ""

    @pyqtProperty(str, notify=sessionAvailableChanged)
    def sessionTag(self):
        try:
            if os.path.isfile(_SESSION_FILE):
                with open(_SESSION_FILE, "r", encoding="utf-8") as f:
                    raw = str(json.load(f).get("tag", "All") or "All")
                return tag_display_label(canonical_tag_key(raw)) or raw
        except Exception:
            pass
        return ""

    @pyqtProperty(str, notify=sessionAvailableChanged)
    def sessionTagIcon(self):
        """Returns the icon filename (e.g. 'rock.png') for the saved tag."""
        try:
            if os.path.isfile(_SESSION_FILE):
                with open(_SESSION_FILE, "r", encoding="utf-8") as f:
                    tag = str(json.load(f).get("tag", "All") or "All")
                return self._tag_icon_name(tag)
        except Exception:
            pass
        return "default.png"

    @pyqtSlot()
    def saveSession(self):
        """Snapshot current tag, track path, and position to Data/music_session.json."""
        try:
            track = self._current_track_dict()
            path = self._track_audio_path(track) if track else ""
            if not path and not self._title:
                return
            pos_ms = max(0, int(self._player.get_time_ms())) if self._player else 0
            session = {
                "tag": self._selected_tag or "All",
                "track_path": path,
                "position_ms": pos_ms,
                "title": self._title,
                "artist": self._artist,
            }
            os.makedirs(os.path.dirname(_SESSION_FILE), exist_ok=True)
            with open(_SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(session, f, indent=2)
            self.sessionAvailableChanged.emit()
        except Exception as e:
            print(f"MusicBridge.saveSession error: {e}")

    @pyqtSlot()
    def restoreSession(self):
        """Load the saved session: filter by tag, seek to track and position, then play."""
        try:
            if not os.path.isfile(_SESSION_FILE):
                return
            with open(_SESSION_FILE, "r", encoding="utf-8") as f:
                session = json.load(f)

            tag = str(session.get("tag", "All") or "All")
            track_path = str(session.get("track_path", "") or "")
            pos_ms = int(session.get("position_ms", 0) or 0)

            # Apply tag filter
            if tag and tag != self._selected_tag:
                self._selected_tag = tag
                self._apply_filters()

            # Find the track in the library and activate it
            restored = False
            if track_path and self._player:
                norm = os.path.normpath(track_path)
                for track in self._library:
                    if os.path.normpath(self._track_audio_path(track)) == norm:
                        self._activate_track(track)
                        restored = True
                        break

            # Seek to saved position
            if restored and pos_ms > 0 and self._player:
                try:
                    self._player.seek_ms(pos_ms)
                except Exception:
                    pass

            # Auto-play
            if self._player:
                try:
                    if not self._player.is_playing():
                        self._player.player.play()
                        if not self._player.timer.isActive():
                            self._player.timer.start()
                except Exception:
                    pass
        except Exception as e:
            print(f"MusicBridge.restoreSession error: {e}")


    def _build_tag_list(self):
        # Aggregate by canonical key so mixed-case duplicates in music.json
        # (e.g. "Nostalgia" + "nostalgia") collapse into a single chip.
        counts: dict[str, int] = {}
        for track in self._library:
            tags = track.get("tags") or []
            if not isinstance(tags, list):
                continue
            for tag in tags:
                raw = str(tag or "").strip()
                if not raw or raw.lower() == "false":
                    continue
                key = canonical_tag_key(raw)
                if not key:
                    continue
                counts[key] = counts.get(key, 0) + 1

        stems_count = sum(1 for t in self._library if self._has_stems(t))

        items = [
            {
                "name": "All",
                "label": "All",
                "count": len(self._library),
                "icon": "default.png",
            }
        ]
        if stems_count:
            items.append({
                "name": "Stems",
                "label": "Stems",
                "count": stems_count,
                "icon": "stems.png",
            })
        for key in sorted(counts):
            items.append(
                {
                    "name": key,
                    "label": tag_display_label(key),
                    "count": counts[key],
                    "icon": self._tag_icon_name(key),
                }
            )
        self._tag_list = items

    @staticmethod
    def _display_title(track):
        return Path(str((track or {}).get("title", "") or "")).stem

    @staticmethod
    def _split_artist_title(text):
        stem = Path(str(text or "")).stem
        if " - " in stem:
            artist, title = stem.split(" - ", 1)
            return artist.strip(), title.strip()
        return "", stem

    @staticmethod
    def _format_time(ms):
        if ms <= 0:
            return "0:00"
        total_sec = ms // 1000
        minutes, seconds = divmod(total_sec, 60)
        return f"{minutes}:{seconds:02d}"

    @staticmethod
    def _tag_icon_name(tag):
        clean = str(tag or "").strip()
        if not clean or clean.lower() == "false":
            return "untagged.png"
        if clean.startswith("All"):
            return "default.png"
        if clean == "Stems":
            return "stems.png"
        key = canonical_tag_key(clean)
        path = TAG_ICONS.get(key)
        if path:
            return os.path.basename(path)
        return "untagged.png"

    @staticmethod
    def _match_track_index(track, playlist):
        target = track_key(track)
        for idx, candidate in enumerate(playlist):
            if track_key(candidate) == target:
                return idx
        return -1

    @staticmethod
    def _normalize_tag(tag):
        raw = str(tag or "").strip()
        if not raw:
            return "All"
        if raw.startswith("All"):
            return "All"
        return raw.split(" (", 1)[0].strip() or "All"

    @staticmethod
    def _normalize_method(method: str) -> str:
        clean = str(method or "").strip().lower()
        if clean in {"realtime", "new_method", "new method"}:
            return "realtime"
        if clean in {"damuci", "damuci_sound", "damuci sound"}:
            return "damuci"
        return ""

    @staticmethod
    def _normalize_performance(mode: str) -> str:
        clean = str(mode or "").strip().lower()
        if clean in {"full_power", "full power"}:
            return "full_power"
        return "rpi4_safe"

    @staticmethod
    def _normalize_mix_mode(mode: str) -> str:
        clean = str(mode or "").strip().upper()
        if clean in {"MODE B", "B", "MIX B"}:
            return "Mode B"
        if clean in {"MODE C", "C", "MIX C"}:
            return "Mode C"
        if clean in {"MODE D", "D", "MIX D"}:
            return "Mode D"
        if clean in {"MODE E", "E", "MIX E"}:
            return "Mode E"
        return "Mode A"

    def _has_stems(self, track):
        if self._player is None or not track:
            return False
        try:
            audio_path = self._player._track_audio_path(track)
        except Exception:
            audio_path = ""
        base = Path(audio_path or str(track.get("title", "") or "")).stem
        if not base:
            return False
        stems_path = Path(STEMS_FOLDER)
        return all(
            (stems_path / base / stem).is_file()
            for stem in ("drums.flac", "bass.flac", "other.flac", "vocals.flac")
        )

    def _track_audio_path(self, track):
        if self._player is None:
            return ""
        try:
            return self._player._track_audio_path(track)
        except Exception:
            return ""

    def _track_to_item(self, track):
        artist, title = self._split_artist_title(track.get("title", ""))
        return {
            "title": title or self._display_title(track),
            "artist": artist,
            "fullTitle": str(track.get("title", "") or ""),
            "trackKey": track_key(track),
        }
