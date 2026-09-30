import json
import os
import random
import hashlib
import glob
import unicodedata
from pathlib import Path

from PyQt6.QtCore import QTimer

from preferences import *
from Modules.Music.tags_list import TAG_ICONS
from Modules.Runtime import catalog_cache, settings_cache
try:
    from Modules.Music.mpris_bridge import MPRISBridge
except ImportError:
    MPRISBridge = None
from Modules.Music.sound_drive import SoundDrive
from Modules.Music.queue_utils import build_queue, dedupe_tracks, playlist_signature, track_key
from Modules.Music.realtime_sounddrive import RealtimeSoundDrive


try:
    import vlc
except ImportError:
    vlc = None

try:
    from mutagen import File as MutagenFile
except Exception:
    MutagenFile = None


# ----- Audio routing helper -----
try:
    from Modules.Music.audio_router import AudioRouter, change_system_volume, set_system_volume, force_cleanup_any_laladash_modules
except Exception:
    class AudioRouter:
        def list_devices(self): return []
        def apply_device_to_player(self, *a, **k): pass
        def default_pulse_sink(self): return None
        def ensure_bus_mirror(self, *a, **k): return False
        def move_main_to_bus(self, *a, **k): return False
        def teardown_bus(self): pass
        def set_user_delay(self, *a, **k): pass
        def set_delay_direction(self, *a, **k): pass

    def change_system_volume(*a, **k): pass
    def set_system_volume(*a, **k): pass
    def force_cleanup_any_laladash_modules(): pass

# App settings path
SETTINGS_INI = Path("Data") / "settings.ini"
SETTINGS_INI.parent.mkdir(parents=True, exist_ok=True)

# Utilities
def _retry(delay_ms, tries, fn):
    def attempt(n=1):
        ok = fn()
        if not ok and n < tries:
            QTimer.singleShot(delay_ms, lambda: attempt(n+1))
    attempt(1)

class MusicPlayer:
    TAG_ICONS = TAG_ICONS

    def __init__(self, gui):
        # Keep a reference to the GUI
        self.gui = gui
        

        self.vlc_instance = vlc.Instance()
        self.player = self.vlc_instance.media_player_new()
        try:
            self.player.audio_set_volume(100)
        except Exception:
            pass
        self.sounddrive = SoundDrive(self) 
        self.realtime_sounddrive = RealtimeSoundDrive(
            self,
            metadata_dir=os.path.join(BASE_DIR, "Data", "sounddrive_cache"),
        )
        self.current_track = None
        self.current_track_key = ""
        self.current_track_path = ""
        from Modules.Settings.settings_audio import VLC_REGISTRY
        VLC_REGISTRY.register("Music: Main", self.player, group="Music")
        self._volume_before_sounddrive = 100
        # SoundDrive state
        self._sd_enabled = False
        self._stem_mixer = None   # StemMixer instance (pedalboard + sounddevice)
        self._sd_paths = {}       # store the stem paths
        self._sd_last_position = 0
        self.sounddrive_active = False

        # ---- output routing state ----
        self.router = AudioRouter()
        self.mirror_player = None
        # Last EQ curve pushed to libvlc, quantised. See
        # apply_main_player_reactive_mix().
        self._last_eq_bands: "tuple | None" = None
        self.output_route = "Default"       # "Default" | "Mirror to Device" | "Bass to Device"
        self.selected_device = None
        self.default_output_sink = self._read_default_output_sink()

        # ---- playlists / playback ----
        self.active_playlist = []
        self.preview_playlist = []
        self.current_index = -1
        self.visible_playlist = []
        # ---- shuffle queue / anti-repeat ----
        from collections import deque
        self._queue = []              # list of indices into active_playlist
        self._queue_pos = -1          # position inside _queue
        self._recent = deque(maxlen=8)  # last played keys (anti-repeat window)
        # bucket signature so filtering doesn't reshuffle every refresh
        self._bucket_sig = None

        # ---- crossfade controls ----
        self.fade_skip_enabled = False    # manual Next/Prev crossfade
        self.fade_tracks_enabled = False  # auto crossfade near track end
        self.crossfade_enabled = False
        self.crossfade_ms = 3000  # ~3s typical

        self._auto_fade_timer = QTimer()
        self._auto_fade_timer.setInterval(250)
        self._auto_fade_timer.timeout.connect(self._auto_fade_tick)
        self._auto_fade_armed_for = None  # track-key we already armed for (prevents double trigger)
        self._anti_repeat_n = 5

        # ---- fixed VLC volume ----
        # Keep VLC at a stable level; control final loudness on the car/output side.
        self.FIXED_VLC_VOLUME = 100
        self._user_volume = self.FIXED_VLC_VOLUME
        self._xfade_timer = None
        self._xfade_old_player = None
        self._xfade_new_player = None

        # Progress timer (owned by MusicPlayer)
        self.timer = QTimer()
        self.timer.timeout.connect(gui.update_progress)  # single heartbeat for progress
        self.timer.setInterval(100)
        self.suppress_autoplay = False
        self._art_cache: dict[str, str] = {}
        # Load metadata / playlists
        self.load_music_data()
        self.set_volume(self.FIXED_VLC_VOLUME)

    def _read_default_output_sink(self) -> str:
        cfg = settings_cache.load_copy(str(SETTINGS_INI))
        return cfg.get("Music", "default_output_sink", fallback="").strip()

    def refresh_default_output_sink(self) -> str:
        self.default_output_sink = self._read_default_output_sink()
        return self.default_output_sink

    def get_stem_paths_for_current_track(self):
        """Return dict with paths to stems, or None if unavailable."""
        if not self.current_track_key:
            return None

        # Pull stem folder from preferences
        try:
            import preferences
            STEMS_BASE = preferences.STEMS_FOLDER
        except Exception:
            # safety fallback
            STEMS_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Stems")

        audio_path = self.current_track_path or self.current_track
        folder = self._resolve_stem_folder(audio_path, stems_base=STEMS_BASE)
        if not folder:
            return None

        # Stem file paths
        stems = {
            "drums": os.path.join(folder, "drums.flac"),
            "bass": os.path.join(folder, "bass.flac"),
            "other": os.path.join(folder, "other.flac"),
            "vocals": os.path.join(folder, "vocals.flac"),
        }

        # Verify all 4 exist
        if all(os.path.isfile(p) for p in stems.values()):
            return stems
        return None

    @staticmethod
    def _normalize_stem_folder_key(value: str) -> str:
        raw = unicodedata.normalize("NFKD", str(value or ""))
        raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
        return "".join(ch.lower() for ch in raw if ch.isalnum())

    def _resolve_stem_folder(self, source: str, *, stems_base: str | None = None) -> str:
        base_dir = str(stems_base or STEMS_FOLDER or "").strip()
        if not base_dir or not os.path.isdir(base_dir):
            return ""

        stem_name = os.path.splitext(os.path.basename(str(source or "").strip()))[0].strip()
        if not stem_name:
            return ""

        exact = os.path.join(base_dir, stem_name)
        if os.path.isdir(exact):
            return exact

        target_key = self._normalize_stem_folder_key(stem_name)
        if not target_key:
            return ""

        try:
            with os.scandir(base_dir) as entries:
                for entry in entries:
                    if not entry.is_dir():
                        continue
                    if self._normalize_stem_folder_key(entry.name) == target_key:
                        return entry.path
        except Exception:
            return ""
        return ""


    def enable_sounddrive(self, stems: dict, start_ms: int):
        """Switch normal player -> StemMixer (pedalboard + sounddevice)."""
        # Store actual system volume for restoration later
        try:
            from Modules.Music.audio_router import get_default_sink_volume
            self._volume_before_sounddrive = get_default_sink_volume()
        except Exception:
            self._volume_before_sounddrive = 70
        if self._sd_enabled:
            return

        # 1) Kill main player FIRST — mute then stop then release media
        try:
            if self.player:
                self.player.audio_set_volume(0)
                self.player.stop()
                self.player.set_media(None)
        except Exception:
            pass

        pulse_sink = ""
        try:
            if self.output_route == "Mirror to Device":
                # Split is active; write to the bus null-sink so both loopbacks receive the audio.
                bus = str(getattr(self.router, "_bus_sink_name", "") or "").strip()
                pulse_sink = bus or ""
            elif self.output_route != "Default":
                pulse_sink = str((self.selected_device or {}).get("id", "") or "").strip()
            if not pulse_sink:
                self.refresh_default_output_sink()
                pulse_sink = str(self.default_output_sink or self.router.default_pulse_sink() or "").strip()
        except Exception:
            pulse_sink = ""

        # 2) Create StemMixer — import inside try so ImportError is caught and printed
        try:
            from Modules.Music.stem_mixer import StemMixer
            self._sd_enabled = True
            self._sd_paths = stems
            self._sd_last_position = start_ms
            self._stem_mixer = StemMixer(stems, start_ms=start_ms, pulse_sink=pulse_sink)
            self._stem_mixer.set_on_end(self._on_stem_mixer_ended)
            self._stem_mixer.play()
        except Exception as e:
            import traceback
            print(f"[MusicPlayer] StemMixer failed: {e}")
            traceback.print_exc()
            self._sd_enabled = False
            self._stem_mixer = None
            # Restore VLC — media was cleared above, reload and play
            try:
                if self.current_track_path:
                    media = self.vlc_instance.media_new(self.current_track_path)
                    self.player.stop()
                    self.player.set_media(media)
                    QTimer.singleShot(100, self.player.play)
            except Exception:
                pass
            return

        # StemMixer running


    
    def disable_sounddrive(self, *, resume_main_player: bool = True):
        """Stop StemMixer and optionally restore normal track playback."""
        if not self._sd_enabled:
            return

        # 1) Capture last position from mixer
        if self._stem_mixer:
            try:
                self._sd_last_position = self._stem_mixer.get_time_ms()
            except Exception:
                pass
            try:
                self._stem_mixer.close()
            except Exception:
                pass
            self._stem_mixer = None

        self._sd_enabled = False

        if not resume_main_player:
            try:
                self.player.set_equalizer(None)
            except Exception:
                pass
            try:
                if hasattr(self, "_volume_before_sounddrive"):
                    try:
                        from Modules.Music.audio_router import set_default_volume
                        set_default_volume(self._volume_before_sounddrive)
                    except Exception:
                        pass
            except Exception:
                pass
            return

        # 2) Restore the original track on the main VLC player
        try:
            if not self.current_track_path:
                return

            media = self.vlc_instance.media_new(self.current_track_path)
            self.player.stop()
            self.player.set_media(media)

            def _resume():
                if self._sd_enabled:
                    return
                try:
                    self.player.audio_set_volume(getattr(self, "_user_volume", self.FIXED_VLC_VOLUME))
                except Exception:
                    pass
                self.player.play()
                pos = max(0, int(self._sd_last_position))
                QTimer.singleShot(200, lambda: self.player.set_time(pos))
                try:
                    self.player.set_equalizer(None)
                except Exception:
                    pass
                try:
                    if hasattr(self, "_volume_before_sounddrive"):
                        try:
                            from Modules.Music.audio_router import set_default_volume
                            set_default_volume(self._volume_before_sounddrive)
                        except Exception:
                            pass
                    # Always restore to the user's chosen VLC volume, not the system sink level.
                    restore_vol = int(getattr(self, '_user_volume', getattr(self, 'FIXED_VLC_VOLUME', 100)))
                    self.player.audio_set_volume(restore_vol)
                except Exception as e:
                    print("[MusicPlayer] Volume restore failed:", e)

            QTimer.singleShot(150, _resume)

        except Exception as e:
            print("Error restoring normal playback:", e)

    def _set_current_track_state(self, track: dict | None) -> None:
        if not isinstance(track, dict):
            self.current_track = None
            self.current_track_key = ""
            self.current_track_path = ""
            return
        self.current_track = str(track.get("title", "") or "")
        self.current_track_key = track_key(track)
        self.current_track_path = self._track_audio_path(track)

    def clear_main_player_eq(self) -> None:
        try:
            self.player.set_equalizer(None)
        except Exception:
            pass

    def disable_all_sounddrive(self) -> None:
        try:
            self.sounddrive.stop()
        except Exception:
            pass
        try:
            self.realtime_sounddrive.stop()
        except Exception:
            pass
        try:
            self.disable_sounddrive()
        except Exception:
            pass
        self.sounddrive_active = False
        self.clear_main_player_eq()

    def enable_damuci_sounddrive(self, *, mix_mode: str = "Mode A") -> None:
        try:
            self.realtime_sounddrive.stop()
        except Exception:
            pass
        stems = self.get_stem_paths_for_current_track()
        if stems and not self._sd_enabled:
            self.enable_sounddrive(stems, self.get_time_ms())
        mixer = getattr(self, "_stem_mixer", None)
        if mixer:
            try:
                mixer.set_mix_mode(mix_mode)
            except Exception:
                pass
        self.sounddrive.set_mix_mode(mix_mode)
        self.sounddrive.start()
        self.sounddrive_active = True

    def enable_realtime_sounddrive(
        self,
        *,
        performance_mode: str = "rpi4_safe",
        metadata_cache_enabled: bool = False,
    ) -> None:
        try:
            self.sounddrive.stop()
        except Exception:
            pass
        if self._sd_enabled:
            self.disable_sounddrive()
        self.realtime_sounddrive.set_performance_mode(performance_mode)
        self.realtime_sounddrive.set_metadata_cache_enabled(metadata_cache_enabled)
        self.realtime_sounddrive.start()
        self.sounddrive_active = True

    def is_damuci_sounddrive_active(self) -> bool:
        return bool(self.sounddrive.is_enabled())

    def is_realtime_sounddrive_active(self) -> bool:
        return bool(self.realtime_sounddrive.is_enabled())

    def _on_stem_mixer_ended(self):
        """Called from StemMixer when all stems reach end-of-file."""
        # Guard: if SoundDrive was already disabled (e.g. by _poll_progress auto-next or
        # play_song), another code path already advanced the track — don't double-next.
        if not self._sd_enabled:
            return
        try:
            QTimer.singleShot(0, self._stem_track_finished)
        except Exception:
            pass

    def _stem_track_finished(self):
        """Handle stem track end — disable mixer and play next."""
        if not self._sd_enabled:
            return
        self.disable_sounddrive(resume_main_player=False)
        # Deferred next song to ensure disable_sounddrive completes
        try:
            QTimer.singleShot(250, self.next_song)
        except Exception as e:
            print(f"[MusicPlayer] Auto-advance deferred call failed: {e}")

    def set_stem_volumes(self, drums, bass, other, vocals):
        """
        Per-stem openness (0 = muffled, 1 = clear).
        Delegates to StemMixer's LowpassFilter + Gain sweep.
        """
        if not self._sd_enabled or not self._stem_mixer:
            return
        self._stem_mixer.set_stem_openness(drums, bass, other, vocals)

    def set_stem_dynamic_params(self, stereo_width=1.0, vocal_pan=0.0,
                                brake_duck=0.0, drive=0.0):
        """Reactive per-tick params (SoundDrive → StemMixer)."""
        if not self._sd_enabled or not self._stem_mixer:
            return
        try:
            self._stem_mixer.set_dynamic_params(
                stereo_width=stereo_width,
                vocal_pan=vocal_pan,
                brake_duck=brake_duck,
                drive=drive,
            )
        except Exception:
            pass

    def _make_distance_eq(self, distance: float):
        """
        distance: 0.0 = close/clear, 1.0 = far/muffled (more bass, less highs)
        """
        try:
            import vlc
        except Exception:
            return None

        try:
            d = max(0.0, min(1.0, float(distance)))
            eq = vlc.AudioEqualizer()

            # Stronger shaping so SoundDrive is clearly audible even without stems.
            low_boost  = 6.0 * d
            low_mid    = 3.0 * d
            mid_cut    = -8.0 * d
            high_cut   = -20.0 * d
            air_cut    = -22.0 * d

            bands = [
                low_boost,   # 0
                low_boost,   # 1
                low_mid,     # 2
                -1.5 * d,    # 3
                mid_cut,     # 4
                mid_cut,     # 5
                high_cut,    # 6
                high_cut,    # 7
                air_cut,     # 8
                air_cut,     # 9
            ]

            eq.set_preamp(0.0)
            for i, amp in enumerate(bands):
                eq.set_amp_at_index(float(amp), int(i))

            return eq
        except Exception:
            return None

    def set_sounddrive_distance(self, distance: float) -> None:
        """No longer used — per-stem EQ is handled inside set_stem_volumes."""
        pass

    def set_main_player_openness(self, openness: float) -> None:
        """
        Apply EQ shaping to the main VLC player (for tracks without stems).
        openness: 0.0 = muffled/distant, 1.0 = clear/natural.
        """
        if self._sd_enabled:
            return  # stems active, don't touch main player
        try:
            import vlc
        except Exception:
            return
        try:
            o = max(0.0, min(1.0, float(openness)))
            if o >= 0.98:
                # Fully clear — remove EQ entirely so normal volume is preserved
                self.player.set_equalizer(None)
                return
            eq = self._make_distance_eq(1.0 - o)
            if eq is None:
                return
            self.player.set_equalizer(eq)
        except Exception:
            pass

    def apply_main_player_reactive_mix(self, profile: dict[str, float]) -> None:
        """Apply a richer reactive SoundDrive profile to the main player."""
        if self._sd_enabled:
            return
        try:
            import vlc
        except Exception:
            return
        try:
            drive = max(0.0, min(1.0, float(profile.get("drive", 0.0))))
            bass = max(0.0, min(1.0, float(profile.get("bass", drive))))
            presence = max(0.0, min(1.0, float(profile.get("presence", drive))))
            sparkle = max(0.0, min(1.0, float(profile.get("sparkle", drive))))
            punch = max(0.0, min(1.0, float(profile.get("punch", 0.0))))
            flash = max(0.0, min(1.0, float(profile.get("flash", 0.0))))
            air = max(0.0, min(1.0, float(profile.get("air", sparkle))))

            if drive >= 0.94 and presence >= 0.78 and air >= 0.72:
                if self._last_eq_bands is not None:
                    self._last_eq_bands = None
                    self.player.set_equalizer(None)
                return

            closed = max(0.0, min(1.0, 1.0 - drive))
            eq = vlc.AudioEqualizer()
            eq.set_preamp(0.0)

            bands = [
                (1.8 + (5.2 * bass) + (3.0 * closed) + (1.4 * punch)),
                (1.4 + (4.6 * bass) + (2.6 * closed) + (1.0 * punch)),
                (0.5 + (2.6 * bass) + (1.5 * presence) + (1.0 * closed)),
                (0.1 + (1.6 * presence)) - (2.2 * closed),
                ((3.6 * presence) + (0.8 * punch)) - (9.0 * closed),
                ((4.4 * presence) + (1.1 * punch)) - (10.5 * closed),
                ((4.8 * sparkle) + (0.8 * punch)) - (13.0 * closed),
                ((5.4 * sparkle) + (1.2 * punch)) - (14.5 * closed),
                ((5.2 * air) + (2.0 * flash)) - (16.5 * closed),
                ((5.8 * air) + (2.4 * flash)) - (18.0 * closed),
            ]

            # Quantise to 0.5 dB and skip the reconfigure when nothing moved.
            # SoundDrive ticks at 10-25 Hz and libvlc's set_equalizer() rebuilds
            # the audio filter chain on every call, which is both a main-thread
            # cost and a source of audible seams. Below 0.5 dB the change is
            # inaudible anyway.
            shaped = tuple(
                round(max(-20.0, min(20.0, amp)) * 2.0) / 2.0 for amp in bands
            )
            if shaped == self._last_eq_bands:
                return
            self._last_eq_bands = shaped
            for i, amp in enumerate(shaped):
                eq.set_amp_at_index(float(amp), int(i))
            self.player.set_equalizer(eq)
        except Exception:
            pass


    def get_current_title(self):
        return self.current_track

    def get_current_track_key(self):
        return self.current_track_key

    def get_current_track_path(self):
        return self.current_track_path

    def has_stems_for(self, title: str) -> bool:
        # Check by audio path first — stems are filed by filename, not track title
        if self.current_track_path and self._has_stems_for_title(self.current_track_path):
            return True
        return self._has_stems_for_title(title) if title else False

    def ensure_stems_playback(self, title: str):
        # If already in stem mode → do nothing
        if self._sd_enabled:
            return

        stems = self.get_stem_paths_for_current_track()
        if stems:
            pos = self.get_time_ms()
            self.enable_sounddrive(stems, pos)

    def ensure_normal_playback(self):
        if self._sd_enabled:
            self.disable_sounddrive()

   
    def get_time_ms(self):
        """Current playback time in ms (StemMixer if SoundDrive is active)."""
        try:
            if self._sd_enabled and self._stem_mixer:
                return self._stem_mixer.get_time_ms()
            return max(0, int(self.player.get_time()))
        except Exception:
            return 0

    def get_length_ms(self):
        """Track length in ms (StemMixer if SoundDrive is active)."""
        try:
            if self._sd_enabled and self._stem_mixer:
                return self._stem_mixer.get_duration_ms()
            return max(0, int(self.player.get_length()))
        except Exception:
            return 0

    def set_time_ms(self, ms):
        """Seek normal player or StemMixer when in SoundDrive."""
        try:
            ms = int(ms)
        except Exception:
            return

        if self._sd_enabled and self._stem_mixer:
            self._stem_mixer.seek(ms)
            self._sd_last_position = ms
        else:
            try:
                self.player.set_time(ms)
            except Exception:
                pass


    # ---- routing API ----
    def set_selected_device(self, device: dict | None):
        self.selected_device = device

    def set_output_route(self, route: str):
        self.output_route = route or "Default"

        # Stop bass sidecar when leaving Bass mode
        if self.output_route != "Bass to Device" and self.mirror_player:
            try: self.mirror_player.stop()
            except Exception: pass
            self.mirror_player = None

        # IMPORTANT: do NOT auto-build mirror here; only after user presses Apply.
        if self.output_route != "Mirror to Device":
            try: self.router.teardown_bus()
            except Exception: pass

    def _start_bass_sidecar(self, media_path: str):
        if not self.selected_device:
            return

        # NEW: kill any leftover bass sidecars system-wide (race-safe)
        try:
            # available after step 1 above
            from Modules.audio_router import kill_sink_inputs_by_media
            kill_sink_inputs_by_media("GTR_BASS_SIDECAR")
        except Exception:
            pass

        # Existing local stop (kept)
        if self.mirror_player:
            try: self.mirror_player.stop()
            except Exception: pass

        self.mirror_player = self.vlc_instance.media_player_new()
        media = self.vlc_instance.media_new(media_path)
        self.mirror_player.set_media(media)
        self.mirror_player.audio_set_volume(100)  # optional
        self.mirror_player.play()
        self.router.apply_device_to_player(self.mirror_player, self.selected_device)


        sink = (self.selected_device or {}).get('id')
        if sink:
            def _move_bass():
                moved = self.router.move_bass_sidecar(sink)
                return moved > 0
            QTimer.singleShot(900, lambda: _retry(600, 4, _move_bass))

    # ---- library ----
    def load_music_data(self):
        self.music_data = self.load_json()

        # Normalize tags
        for s in self.music_data:
            tags = s.get("tags", [])
            if isinstance(tags, bool):
                tags = []
            elif not isinstance(tags, list):
                tags = [tags]
            clean = []
            for t in tags:
                try:
                    st = t if isinstance(t, str) else str(t)
                    st = st.strip()
                    if st:
                        clean.append(st)
                except Exception:
                    pass
            s["tags"] = clean

        self.update_json_with_new_songs()
        sorted_all = sorted(self.music_data, key=lambda x: x['title'].lower())
        self.active_playlist = sorted_all[:]
        self.preview_playlist = sorted_all[:]

    def load_json(self):
        if os.path.exists(MUSIC_JSON):
            try:
                with open(MUSIC_JSON, "r") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
            except Exception as e:
                print("⚠️ Couldn’t load JSON:", e)
        return []

    def update_json_with_new_songs(self):
        # Case-folded so a file that was merely re-cased on disk is recognised
        # as already-known, not appended as a fresh untagged entry beside the
        # tagged original. Also basename-folded, because a title may legitimately
        # carry a sub-folder prefix the scan returns and vice versa.
        existing = set()
        for s in self.music_data:
            t = str(s.get('title', '') or '')
            if not t:
                continue
            existing.add(t.casefold())
            existing.add(os.path.basename(t).casefold())
        new_ones = []
        for filename in catalog_cache.music_filenames(MUSIC_FOLDER, exts=AUDIO_EXTENSIONS):
            if filename.casefold() in existing or os.path.basename(filename).casefold() in existing:
                continue
            new_ones.append({'title': filename, 'tags': []})
            existing.add(filename.casefold())
            existing.add(os.path.basename(filename).casefold())
            print(f"Adding New Songs: {filename}")
        if not new_ones:
            # Nothing genuinely new on disk -> leave music.json exactly as the
            # user wrote it. The old code re-serialised (and re-sorted) it on
            # every single boot, so a hand-curated file could never settle;
            # only an actual new file earns a rewrite now.
            self.music_data.sort(key=lambda x: str(x.get('title', '')).lower())
            return

        self.music_data.extend(new_ones)
        self.music_data.sort(key=lambda x: str(x.get('title', '')).lower())

        try:
            with open(MUSIC_JSON, "w") as f:
                json.dump(self.music_data, f, indent=4)
        except IOError as e:
            print("⚠️ Couldn’t write JSON:", e)


    def get_track_art_path(self, track: dict | None) -> str | None:
        if not isinstance(track, dict):
            return None
        cache_key = self._track_art_cache_key(track)
        if not cache_key:
            return None
        cached = self._art_cache.get(cache_key)
        if cached is not None:
            return cached or None

        audio_path = self._track_audio_path(track)
        if not audio_path:
            return None
        audio_dir = os.path.dirname(audio_path)
        stem = os.path.splitext(os.path.basename(audio_path))[0]
        album = str(track.get("album", "") or "").strip()
        stem_variants = {stem}
        if " - " in stem:
            try:
                stem_variants.add(stem.split(" - ", 1)[1].strip())
            except Exception:
                pass

        candidates: list[str] = []
        for k in ("cover", "cover_art", "art", "artwork", "image", "thumbnail", "art_path"):
            raw = str(track.get(k, "") or "").strip()
            if not raw:
                continue
            if os.path.isabs(raw):
                candidates.append(raw)
            else:
                candidates.append(os.path.join(BASE_DIR, raw))
                candidates.append(os.path.join(MUSIC_FOLDER, raw))
                candidates.append(os.path.join(audio_dir, raw))

        # Song-specific filenames (always safe to use).
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            for s in stem_variants:
                if s:
                    candidates.append(os.path.join(audio_dir, f"{s}{ext}"))
            candidates.append(os.path.join(audio_dir, "covers", f"{stem}{ext}"))
            candidates.append(os.path.join(audio_dir, "artwork", f"{stem}{ext}"))

        # Generic / shared art names — only use when the song lives in an
        # album sub-directory, NOT when it sits in the root Music folder.
        # Otherwise a single cover.jpg / AlbumArtSmall.jpg matches every song.
        is_album_subdir = os.path.normpath(audio_dir) != os.path.normpath(MUSIC_FOLDER)
        if is_album_subdir:
            for ext in (".jpg", ".jpeg", ".png", ".webp"):
                candidates.append(os.path.join(audio_dir, f"cover{ext}"))
                candidates.append(os.path.join(audio_dir, f"folder{ext}"))
                candidates.append(os.path.join(audio_dir, f"album{ext}"))
                candidates.append(os.path.join(audio_dir, f"front{ext}"))
                if album:
                    candidates.append(os.path.join(audio_dir, f"{album}{ext}"))
            candidates.append(os.path.join(audio_dir, "AlbumArtSmall.jpg"))

        seen = set()
        for p in candidates:
            if not p or p in seen:
                continue
            seen.add(p)
            if os.path.isfile(p):
                self._art_cache[cache_key] = p
                return p

        cached_art = self._existing_cached_art_path(audio_path, cache_key)
        if cached_art:
            self._art_cache[cache_key] = cached_art
            return cached_art

        embedded = self._extract_embedded_art(audio_path, cache_key)
        if embedded and os.path.isfile(embedded):
            self._art_cache[cache_key] = embedded
            return embedded

        # VLC ArtworkURL fallback: only safe when this track IS the currently
        # playing one (VLC may return stale art for any other track).
        # If this IS the current track but VLC hasn't parsed art yet, skip
        # caching "" so the next _sync_from_player call can retry.
        try:
            current_mrl = ""
            if self.player:
                m = self.player.get_media()
                if m:
                    current_mrl = m.get_mrl() or ""
            cur_path = ""
            if current_mrl.startswith("file://"):
                from urllib.parse import unquote
                cur_path = os.path.normpath(unquote(current_mrl[7:]))
            if cur_path and os.path.normpath(audio_path) == cur_path:
                # This is the currently playing track.
                if self.player:
                    m = self.player.get_media()
                    if m:
                        try:
                            import vlc as _vlc
                            _art_key = _vlc.Meta.ArtworkURL
                        except Exception:
                            _art_key = 15  # libvlc_meta_ArtworkURL
                        vlc_art = m.get_meta(_art_key)
                        if vlc_art and vlc_art.startswith("file://"):
                            from urllib.parse import unquote as _uq
                            vlc_path = os.path.normpath(_uq(vlc_art[7:]))
                            if os.path.isfile(vlc_path):
                                self._art_cache[cache_key] = vlc_path
                                return vlc_path
                # VLC hasn't parsed art yet — don't cache "" so the
                # next call (from delayed re-sync) can retry.
                return None
        except Exception:
            pass

        self._art_cache[cache_key] = ""
        return None

    def resolve_track_art_deep(self, track: dict | None, timeout_ms: int = 3000) -> str | None:
        """Album art for ANY track, not just the playing one. Never call this
        from the Qt main thread.

        ``get_track_art_path`` can only reach the ArtworkURL of whatever the
        player currently holds, so every other row in a queue fell back to
        embedded-tag extraction — which needs mutagen and writes a PNG. Here a
        throwaway ``Media`` is parsed instead, which asks libvlc for exactly
        the same metadata it would give the playing track and drops the result
        in VLC's own art cache. No new dependency, and it hits tracks whose art
        is embedded rather than sitting beside the file.

        It blocks for up to ``timeout_ms`` waiting on the parser, hence the
        restriction to worker threads.
        """
        if not isinstance(track, dict):
            return None
        cache_key = self._track_art_cache_key(track)
        if not cache_key:
            return None
        cached = self._art_cache.get(cache_key)
        if cached:
            return cached

        audio_path = self._track_audio_path(track)
        if not audio_path or not os.path.isfile(audio_path):
            return None

        instance = getattr(self, "vlc_instance", None)
        if instance is None or vlc is None:
            return None
        import time
        try:
            media = instance.media_new_path(audio_path)
            if media is None:
                return None
            media.parse_with_options(vlc.MediaParseFlag.local, int(timeout_ms))
            deadline = time.time() + (timeout_ms / 1000.0)
            terminal = (vlc.MediaParsedStatus.done,
                        vlc.MediaParsedStatus.failed,
                        vlc.MediaParsedStatus.timeout)
            while time.time() < deadline:
                if media.get_parsed_status() in terminal:
                    break
                time.sleep(0.02)
            art = media.get_meta(vlc.Meta.ArtworkURL) or ""
            try:
                media.release()
            except Exception:
                pass
        except Exception:
            return None

        if not art.startswith("file://"):
            return None
        from urllib.parse import unquote
        art_path = os.path.normpath(unquote(art[7:]))
        if not os.path.isfile(art_path):
            return None
        self._art_cache[cache_key] = art_path
        return art_path

    def _track_audio_path(self, track: dict | None) -> str:
        if not isinstance(track, dict):
            return ""
        raw = (
            str(track.get("path", "") or "").strip()
            or str(track.get("file", "") or "").strip()
            or str(track.get("source_path", "") or "").strip()
            or str(track.get("title", "") or "").strip()
        )
        if not raw:
            return ""
        if os.path.isabs(raw):
            return os.path.normpath(raw)

        direct = os.path.normpath(os.path.join(MUSIC_FOLDER, raw))
        if os.path.isfile(direct):
            return direct

        # The literal join missed. The filesystem here is case-sensitive, so a
        # music.json title whose casing has drifted from the file on disk
        # ("ACdc - TNT.MP3" vs "ACDC - TNT.MP3") resolves to nothing — no path,
        # no artwork — even though the track is right there. music_index maps
        # case-folded relative path, then case-folded bare filename, back to the
        # real name, which also recovers a title that lost its sub-folder
        # prefix. Falls through to `direct` when there is genuinely no file, so
        # callers' own isfile() guards and log messages are unchanged.
        try:
            rel = raw.replace("\\", "/").strip("/")
            index = catalog_cache.music_index(MUSIC_FOLDER, exts=AUDIO_EXTENSIONS)
            hit = index.get(rel.casefold()) or index.get(os.path.basename(rel).casefold())
            if hit:
                return os.path.normpath(os.path.join(MUSIC_FOLDER, hit))
        except Exception:
            pass
        return direct

    def _track_art_cache_key(self, track: dict | None) -> str:
        if not isinstance(track, dict):
            return ""
        audio_path = self._track_audio_path(track)
        if audio_path:
            return audio_path
        return str(track.get("title", "") or "").strip()

    def _extract_embedded_art(self, audio_path: str, cache_key: str) -> str | None:
        if MutagenFile is None or not audio_path or not os.path.isfile(audio_path):
            return None
        try:
            mf = MutagenFile(audio_path)
        except Exception:
            return None
        if mf is None:
            return None

        def _blob_ext(raw: bytes, mime_hint: str = "") -> str:
            lm = str(mime_hint or "").lower()
            if "png" in lm:
                return ".png"
            if "webp" in lm:
                return ".webp"
            if "jpeg" in lm or "jpg" in lm:
                return ".jpg"
            if "gif" in lm:
                return ".gif"
            if "bmp" in lm:
                return ".bmp"
            if raw.startswith(b"\x89PNG\r\n\x1a\n"):
                return ".png"
            if raw.startswith(b"\xff\xd8\xff"):
                return ".jpg"
            if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
                return ".webp"
            if raw.startswith((b"GIF87a", b"GIF89a")):
                return ".gif"
            if raw.startswith(b"BM"):
                return ".bmp"
            return ""

        def _accept_blob(raw: bytes | bytearray | None, mime_hint: str = "") -> tuple[bytes, str] | None:
            if not isinstance(raw, (bytes, bytearray)):
                return None
            blob = bytes(raw)
            if len(blob) < 256:
                return None
            ext = _blob_ext(blob, mime_hint)
            if not ext:
                return None
            return blob, ext

        best_blob: bytes | None = None
        best_ext = ".jpg"
        best_rank = 999

        try:
            pics = list(getattr(mf, "pictures", []) or [])
            for pic in pics:
                raw = getattr(pic, "data", None)
                accepted = _accept_blob(raw, str(getattr(pic, "mime", "") or ""))
                if accepted:
                    best_blob, best_ext = accepted
                    best_rank = 0
                    break
        except Exception:
            pass

        if best_blob is None:
            tags = getattr(mf, "tags", None)
            if tags is not None:
                try:
                    items = list(tags.items())
                except Exception:
                    try:
                        items = [(str(i), v) for i, v in enumerate(tags.values())]
                    except Exception:
                        items = []

                for key, val in items:
                    k = str(key or "").upper()
                    rank = 20
                    if k.startswith("APIC"):
                        rank = 1
                    elif "COVR" in k or "PICTURE" in k:
                        rank = 2

                    blobs: list[tuple[bytes, str]] = []
                    if isinstance(val, (list, tuple)):
                        for item in val:
                            accepted = _accept_blob(
                                getattr(item, "data", item),
                                str(getattr(item, "mime", "") or ""),
                            )
                            if accepted:
                                blobs.append(accepted)
                    else:
                        accepted = _accept_blob(
                            getattr(val, "data", val),
                            str(getattr(val, "mime", "") or ""),
                        )
                        if accepted:
                            blobs.append(accepted)

                    if not blobs:
                        continue
                    blob, ext = blobs[0]
                    if rank < best_rank:
                        best_blob = blob
                        best_ext = ext
                        best_rank = rank

        if not best_blob:
            return None

        try:
            digest = self._art_cache_digest(audio_path, cache_key)
            out_dir = self._art_cache_dir()
        except Exception:
            return None
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception:
            return None
        out_path = os.path.join(out_dir, f"{digest}{best_ext}")
        try:
            with open(out_path, "wb") as f:
                f.write(best_blob)
        except Exception:
            return None
        return out_path if os.path.isfile(out_path) else None

    def _art_cache_dir(self) -> str:
        return os.path.join(BASE_DIR, "Data", "art_cache")

    def _art_cache_digest(self, audio_path: str, cache_key: str) -> str:
        try:
            st = os.stat(audio_path)
            sig = f"{cache_key}|{int(st.st_mtime)}|{st.st_size}"
        except Exception:
            sig = cache_key
        return hashlib.sha1(sig.encode("utf-8")).hexdigest()

    def _existing_cached_art_path(self, audio_path: str, cache_key: str) -> str | None:
        if not audio_path or not cache_key or not os.path.isfile(audio_path):
            return None
        digest = self._art_cache_digest(audio_path, cache_key)
        patterns = [os.path.join(self._art_cache_dir(), f"{digest}.{ext}") for ext in ("jpg", "jpeg", "png", "webp", "gif", "bmp")]
        for pattern in patterns:
            matches = sorted(glob.glob(pattern))
            for match in matches:
                if os.path.isfile(match):
                    return match
        return None
    # ---- playback ----
    def play_song(self, index=None, *, use_fade: bool | None = None):
        if index is None:
            return ""   # 👈 prevents autoplay

        if not self.active_playlist:
            print("⚠️ Active playlist empty.")
            return ""
        self._hard_cancel_any_fade()

        if index is not None and 0 <= index < len(self.active_playlist):
            # If stem players are active, stop them so the normal play path can
            # load the new track.  SoundDrive's tick will re-enable stems if the
            # new track has them.
            if self._sd_enabled:
                self.disable_sounddrive(resume_main_player=False)
            self.current_index = index
        if not isinstance(self.current_index, int) or self.current_index < 0 or self.current_index >= len(self.active_playlist):
            # never random-pick here; follow the session queue if available
            if getattr(self, "_queue", None):
                self._queue_pos = 0
                self.current_index = int(self._queue[self._queue_pos])
            else:
                self.current_index = 0
        if 0 <= self.current_index < len(self.active_playlist):
            track = self.active_playlist[self.current_index]
            path = self._track_audio_path(track)
            if not path or not os.path.isfile(path):
                print(f"⚠️ Missing audio file: {path or track.get('title', '')}")
                return ""
            self._set_current_track_state(track)
            self._auto_fade_armed_for = None

            # Crossfade (if enabled) instead of hard cut
            do_fade = use_fade if use_fade is not None else self.crossfade_enabled
            if do_fade:
                if self._crossfade_to(path):
                    # routing sidecar (Bass) is handled only in non-fade path; fade is Default-only by design
                    self.timer.start()
                    QTimer.singleShot(1000, self.gui.update_current_song)
                    self.gui.cached_duration_ms = 0
                    self.gui._last_progress_value = -1
                    return os.path.splitext(track['title'])[0]

            # Normal (non-fade) path: load media on the main player and start playback.
            try:
                media = self.vlc_instance.media_new(path)
                self.player.stop()
                self.player.set_media(media)
                # After sounddrive ends, use the restored system output volume level.
                # Fallback to _user_volume if not recovering from sounddrive.
                play_volume = getattr(self, "_user_volume", self.FIXED_VLC_VOLUME)
                if not getattr(self, "_sd_enabled", False):
                    # Not in sounddrive; try to read current system output level
                    try:
                        from Modules.Music.audio_router import get_default_sink_volume
                        system_vol = get_default_sink_volume()
                        if system_vol is not None and system_vol > 0:
                            play_volume = int(system_vol)
                    except Exception:
                        pass
                self.player.audio_set_volume(play_volume)
                self.player.play()
                self.set_volume(play_volume)
            except Exception as e:
                print(f"⚠️ play_song: unable to start playback: {e}")
                return ""

            def _route_after_stream_is_alive():
                sel = (self.selected_device or {}).get('id')
                self.refresh_default_output_sink()

                if self.output_route == "Bass to Device" and sel:
                    self._start_bass_sidecar(path)
                    return True

                if self.output_route == "Mirror to Device":
                    try:
                        ok = self.router.move_main_to_bus("GTR_MAIN")
                        return ok
                    except Exception:
                        return False

                if self.output_route == "Default":
                    target_sink = self.default_output_sink
                    if target_sink:
                        try:
                            return bool(self.router.move_main_to_sink(target_sink))
                        except Exception:
                            return False

                return True

            QTimer.singleShot(900, lambda: _retry(600, 4, _route_after_stream_is_alive))

            # single progress heartbeat (owned by MusicPlayer)
            self.timer.start()
            QTimer.singleShot(1000, self.gui.update_current_song)

            self.gui.cached_duration_ms = 0
            self.gui._last_progress_value = -1

            return os.path.splitext(track['title'])[0]

        print("⚠️ Invalid index in active playlist.")
        return ""

    def toggle_play_pause(self):
        # SoundDrive: pause/resume StemMixer
        if self._sd_enabled and self._stem_mixer:
            if self._stem_mixer.is_playing():
                self._stem_mixer.pause()
            else:
                self._stem_mixer.resume()
            return

        if self.player.is_playing():
            self.player.pause()
        else:
            # Check if there's media loaded before trying to resume
            if self.player.get_media() is None:
                if self.active_playlist and isinstance(self.current_index, int) and 0 <= self.current_index < len(self.active_playlist):
                    self.play_song(self.current_index)
                    return
                self.next_song()
                return
            try:
                self.player.audio_set_volume(getattr(self, "_user_volume", getattr(self, "FIXED_VLC_VOLUME", 100)))
            except Exception:
                pass
            self.player.play()
            # Ensure the progress timer is running after resume
            if not self.timer.isActive():
                self.timer.start()


    def stop_song(self):
        self.player.stop()
        self.timer.stop()
        mp = getattr(self, "mirror_player", None)
        if mp:
            try: mp.stop()
            except Exception: pass
            self.mirror_player = None

    # ------------------------------
    # Shuffle queue + anti-repeat
    # ------------------------------

    def set_active_playlist(self, playlist, *, keep_current=True, reshuffle=True):
        """Set the current bucket (tag/search filtered list) for deterministic Next.

        The UI can refresh the visible list frequently; we only rebuild/shuffle the session queue
        when the bucket content *actually* changes.
        """
        new_list = dedupe_tracks(list(playlist) if playlist else [])
        new_sig = playlist_signature(new_list)

        # Always update the reference
        self.active_playlist = new_list

        # No real change -> keep queue + recent as-is (avoids glitchy reshuffles)
        if new_sig == getattr(self, "_bucket_sig", None):
            return

        self._bucket_sig = new_sig

        if not keep_current:
            self.current_index = -1

        self._rebuild_queue(keep_current=keep_current, reshuffle=reshuffle)


    def _rebuild_queue(self, *, keep_current=True, reshuffle=True):
        """Build a shuffled queue of indices into active_playlist."""
        current_key = self.current_track_key if keep_current else ""
        self._queue, self._queue_pos = build_queue(
            self.active_playlist,
            shuffle_enabled=bool(reshuffle),
            current_track_key=current_key,
            rng=random,
        )
        if keep_current and current_key:
            try:
                cur_idx = next(
                    (
                        i
                        for i, track in enumerate(self.active_playlist)
                        if track_key(track) == current_key
                    ),
                    None,
                )
                if cur_idx is not None:
                    self.current_index = int(cur_idx)
            except Exception:
                pass

        # reset recent list so switching tags doesn't feel "blocked"
        try:
            self._recent.clear()
        except Exception:
            pass


    def _select_next_index(self):
        """Return the next track index respecting anti-repeat where possible."""
        if not self.active_playlist:
            return None
        if not getattr(self, "_queue", None):
            self._rebuild_queue(keep_current=False, reshuffle=True)

        def _key_for_idx(idx: int) -> str:
            try:
                return track_key(self.active_playlist[idx])
            except Exception:
                return ""

        max_steps = len(self._queue) + 2
        steps = 0
        while steps < max_steps:
            steps += 1
            self._queue_pos = (self._queue_pos + 1) % len(self._queue)
            cand_idx = self._queue[self._queue_pos]
            k = _key_for_idx(cand_idx)
            if not k:
                continue
            if k in self._recent and len(self.active_playlist) > len(self._recent):
                # try another one
                continue
            return cand_idx

        # fallback: give whatever we have
        self._queue_pos = (self._queue_pos + 1) % len(self._queue)
        return self._queue[self._queue_pos]

    def _track_key(self, idx: int) -> str:
        """Stable identifier for anti-repeat & bucket signature."""
        try:
            return track_key(self.active_playlist[idx])
        except Exception:
            return ""

    def _register_recent(self, idx):
        try:
            k = self._track_key(idx)
            if k:
                self._recent.append(k)
        except Exception:
            pass


    # ------------------------------
    
    def _crossfade_start_in(self, target_vol: int) -> int:
        """Start volume for the incoming track so it is audible immediately."""
        try:
            target_vol = int(target_vol)
        except Exception:
            target_vol = int(getattr(self, "FIXED_VLC_VOLUME", 100))
        if target_vol <= 0:
            return 0
        floor = int(getattr(self, "crossfade_start_floor", 30))
        ratio = float(getattr(self, "crossfade_start_ratio", 0.30))
        start_in = max(floor, int(round(target_vol * ratio)))
        return min(target_vol, max(0, start_in))

    # Crossfade (2-player volume ramp)
    # ------------------------------
    def set_crossfade(self, enabled: bool, duration_ms: int | None = None):
        self.fade_skip_enabled = bool(enabled)
        # backward-compat flag (some parts may still read it)
        self.crossfade_enabled = self.fade_skip_enabled
        if duration_ms is not None:
            try:
                self.crossfade_ms = max(500, int(duration_ms))
            except Exception:
                pass

    def _cancel_crossfade(self):
        try:
            if self._xfade_timer:
                self._xfade_timer.stop()
        except Exception:
            pass
        self._xfade_timer = None
        # If we were mid-crossfade, stop and drop the extra players safely
        try:
            if self._xfade_old_player and self._xfade_old_player is not self.player:
                self._xfade_old_player.stop()
        except Exception:
            pass
        try:
            if self._xfade_new_player and self._xfade_new_player is not self.player:
                self._xfade_new_player.stop()
        except Exception:
            pass
        self._xfade_old_player = None
        self._xfade_new_player = None

    def _hard_cancel_any_fade(self):
        """Kill any crossfade players/timers immediately (for rapid Next/Prev spam)."""
        # stop timer
        try:
            if getattr(self, "_xfade_timer", None):
                try: self._xfade_timer.stop()
                except Exception: pass
        except Exception:
            pass

        # stop BOTH players, even if one is self.player
        for p in (getattr(self, "_xfade_old_player", None), getattr(self, "_xfade_new_player", None)):
            if p:
                try: p.stop()
                except Exception: pass

        self._xfade_timer = None
        self._xfade_old_player = None
        self._xfade_new_player = None


    # ------------------------------
    # Auto crossfade near end-of-track
    # ------------------------------
    def set_fade_tracks(self, enabled: bool, duration_ms: int | None = None):
        """Enable/disable automatic crossfade to next track as the song ends."""
        self.fade_tracks_enabled = bool(enabled)
        if duration_ms is not None:
            try:
                self.crossfade_ms = max(500, int(duration_ms))
            except Exception:
                pass
        # arm timer only when enabled; harmless to keep running but we keep it tidy
        try:
            if self.fade_tracks_enabled:
                if not self._auto_fade_timer.isActive():
                    self._auto_fade_timer.start()
            else:
                self._auto_fade_timer.stop()
                self._auto_fade_armed_for = None
        except Exception:
            pass

    def _auto_fade_tick(self):
        """Timer tick: when remaining time <= crossfade window, fade into the next queued track."""
        if not getattr(self, "fade_tracks_enabled", False):
            return
        if not self.active_playlist:
            return

        # don't interfere with SoundDrive (stems) or mid-crossfade
        if getattr(self, "_sd_enabled", False) or getattr(self, "sounddrive_active", False):
            return
        if getattr(self, "_xfade_timer", None):
            # already crossfading
            return

        try:
            if not self.player or not self.player.is_playing():
                return
            length_ms = int(self.player.get_length() or 0)
            time_ms = int(self.player.get_time() or 0)
        except Exception:
            return

        # VLC can report 0/negative while buffering; ignore
        if length_ms <= 0 or time_ms < 0:
            return

        remaining = length_ms - time_ms
        if remaining > int(getattr(self, "crossfade_ms", 3000)):
            return

        # prevent retriggering multiple times for the same track
        cur_key = self._track_key(self.current_index) if getattr(self, "current_index", -1) >= 0 else ""
        if cur_key and self._auto_fade_armed_for == cur_key:
            return
        self._auto_fade_armed_for = cur_key

        # pick next index from the session queue (respects anti-repeat)
        next_idx = self._select_next_index()
        if next_idx is None:
            return

        self.current_index = int(next_idx)
        self._register_recent(self.current_index)

        # start crossfade straight to the file path
        try:
            track = self.active_playlist[self.current_index]
            next_path = self._track_audio_path(track)
        except Exception:
            return
        if not next_path or not os.path.isfile(next_path):
            print(f"⚠️ Missing audio file (advance): "
                  f"{next_path or track.get('title', '')}")
            return
        self._set_current_track_state(track)

        started = self._crossfade_to(next_path)
        if started:
            try:
                QTimer.singleShot(250, self.gui.update_current_song)
            except Exception:
                pass


    def _crossfade_to(self, next_path: str) -> bool:
        """Start a ~3s crossfade into next_path. Returns True if crossfade started."""
        # Don’t crossfade when SoundDrive (stems) is active
        if getattr(self, "_sd_enabled", False) or getattr(self, "sounddrive_active", False):
            return False

        # Routing modes rely on sink-input moves; crossfade is safest in Default route.
        if getattr(self, "output_route", "Default") != "Default":
            return False

        try:
            if not self.player or not self.player.is_playing():
                return False
        except Exception:
            return False

        # Cancel any existing crossfade (prevents stacking players)
        self._cancel_crossfade()

        # Crossfade ramps target the user's current volume (fallback to FIXED_VLC_VOLUME).
        target_vol = int(getattr(self, '_user_volume', getattr(self, 'FIXED_VLC_VOLUME', 100)))
        target_vol = max(0, min(100, target_vol))
        start_vol = target_vol
        old_player = self.player
        # Ensure the outgoing track starts from the fixed target volume.
        try:
            old_player.audio_set_volume(target_vol)
        except Exception:
            pass
        new_player = self.vlc_instance.media_player_new()
        media = self.vlc_instance.media_new(next_path)
        new_player.set_media(media)

        start_in = None
        try:
            start_in = int(self._crossfade_start_in(target_vol))
            new_player.audio_set_volume(start_in)
        except Exception:
            try:
                new_player.audio_set_volume(0)
            except Exception:
                pass

        new_player.play()

        # keep references so we can stop them
        self._xfade_old_player = old_player
        self._xfade_new_player = new_player

        duration = max(500, int(getattr(self, "crossfade_ms", 3000)))
        step_ms = 50
        steps = max(1, duration // step_ms)
        state = {"i": 0}

        timer = QTimer()
        self._xfade_timer = timer

        def _tick():
            # A queued QTimer tick can still fire after this crossfade was
            # cancelled by rapid Next/Prev — _hard_cancel_any_fade() stops the
            # timer, but a timeout already sitting in Qt's event queue is still
            # delivered. If _xfade_timer no longer points at our timer, this
            # crossfade is dead: bail before touching any volume, otherwise we
            # ramp the volume of a player already repurposed for the new track
            # (symptom: volume stuck at the crossfade start level, ~32%).
            if self._xfade_timer is not timer:
                return
            i = state["i"]
            t = i / float(steps)
            # linear ramp
            out_v = int(round(start_vol * (1.0 - t)))
            in_v  = int(round((start_in if start_in is not None else 0) + (start_vol - (start_in if start_in is not None else 0)) * t))

            try:
                old_player.audio_set_volume(max(0, out_v))
            except Exception:
                pass
            try:
                new_player.audio_set_volume(max(0, in_v))
            except Exception:
                pass

            state["i"] += 1
            if state["i"] > steps:
                try:
                    old_player.stop()
                except Exception:
                    pass

                # swap main player to the new one
                self.player = new_player


                # force a clean final volume (prevents getting stuck low)
                try:
                    self.set_volume(target_vol)
                except Exception:
                    pass
                # clear xfade state
                try:
                    timer.stop()
                except Exception:
                    pass
                self._xfade_timer = None
                self._xfade_old_player = None
                self._xfade_new_player = None

        timer.timeout.connect(_tick)
        timer.start(step_ms)
        return True

    def next_song(self):
        if not self.active_playlist:
            print("⚠️ Active playlist is empty. No next song.")
            return ""
        idx = self._select_next_index()
        if idx is None:
            return ""
        self.current_index = int(idx)
        self._register_recent(self.current_index)
        return self.play_song(self.current_index, use_fade=getattr(self,'fade_skip_enabled', False))

    def set_volume(self, v):
        try:
            vol = int(v)
        except Exception:
            vol = int(getattr(self, "_user_volume", getattr(self, "FIXED_VLC_VOLUME", 100)))
        vol = max(0, min(100, vol))
        self._user_volume = vol

        # MAIN PLAYER — skip entirely when SoundDrive is active.
        # The main player is stopped during SoundDrive; touching its volume here
        # can cause it to bleed back through if VLC resumes.
        if not getattr(self, "_sd_enabled", False):
            try:
                self.player.audio_set_volume(vol)
            except Exception as e:
                print(f"[MusicPlayer] main volume set failed: {e}")

        # STEM PLAYERS — do NOT touch when SoundDrive is active.
        # SoundDrive sets each stem's volume independently every tick;
        # overwriting them here with a flat value destroys the per-stem mix.

        # MIRROR/BASS PLAYER
        try:
            if self.mirror_player:
                self.mirror_player.audio_set_volume(vol)
        except Exception as e:
            print(f"[MusicPlayer] mirror volume set failed: {e}")


    def _has_stems_for_title(self, title: str) -> bool:
        folder = self._resolve_stem_folder(title)
        if not folder:
            return False
        return (
            os.path.isfile(os.path.join(folder, "drums.flac")) and
            os.path.isfile(os.path.join(folder, "bass.flac")) and
            os.path.isfile(os.path.join(folder, "other.flac")) and
            os.path.isfile(os.path.join(folder, "vocals.flac"))
        )


    def get_position(self):
        return self.player.get_position() * self.player.get_length()


    def set_position(self, val):
        if self.player.is_playing():
            self.player.set_position(val / 100.0)
