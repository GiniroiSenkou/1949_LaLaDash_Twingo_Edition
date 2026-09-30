# Modules/Music/stem_mixer.py
"""
StemMixer: real-time 4-stem mixer using pedalboard + sounddevice.

Each stem has its own LowpassFilter sweep. Vocals are handled differently
depending on the active mix mode:

  Mix A — Amplitude fade only (simple on/off)
  Mix B — LowpassFilter sweep + amplitude fade (muffled→clear + quiet→loud)
  Mix C — LPF sweep + reverb wet→dry (far away → present); drums=speed, vocals=throttle
  Mix D — Clone of C with stronger distant feel near standstill + speed slowdown
  Mix E — Clone of D without speed slowdown
"""
from __future__ import annotations

import contextlib
import math
import os
import threading
from typing import Callable, Dict, Optional

import numpy as np

# Suppress ALSA probe noise during import
_devnull = os.open(os.devnull, os.O_WRONLY)
_saved_fd = os.dup(2)
os.dup2(_devnull, 2)
os.close(_devnull)
try:
    import sounddevice as sd
finally:
    os.dup2(_saved_fd, 2)
    os.close(_saved_fd)

from pedalboard import Pedalboard, LowpassFilter, Reverb
from pedalboard.io import ReadableAudioFile


# ── Per-stem filter config ────────────────────────────────────────────────

# (cutoff_min, cutoff_max) in Hz — exponential sweep
STEM_CUTOFF = {
    "drums": (300.0,  20000.0),
    "bass":  (200.0,  20000.0),
    "other": (400.0,  20000.0),
}

VOCAL_CUTOFF = (300.0, 20000.0)

STEM_NAMES = ("drums", "bass", "other", "vocals")

BLOCK_SIZE = 2048  # samples per callback (~46 ms at 44100; larger budget reduces xruns on Pi)
MASTER_GAIN = 1.22


@contextlib.contextmanager
def _temporary_env(updates: Dict[str, Optional[str]]):
    previous: Dict[str, Optional[str]] = {}
    for key, value in updates.items():
        previous[key] = os.environ.get(key)
        if value in (None, ""):
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _exp_sweep(o: float, lo: float, hi: float) -> float:
    """Exponential sweep from lo to hi (perceptually linear)."""
    o = max(0.0, min(1.0, o))
    if lo >= hi:
        return hi
    return lo * ((hi / lo) ** o)


class StemMixer:
    """
    Reads 4 FLAC stem files and outputs a single mixed audio stream.
    Supports 5 mix modes for how vocals are processed.
    """

    def __init__(self, stem_paths: Dict[str, str], start_ms: int = 0, pulse_sink: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._playing = False
        self._paused = False
        self._on_end: Optional[Callable] = None
        self._ended = False
        self._pulse_sink = str(pulse_sink or "").strip()

        # Mix mode: "A", "B", "C", "D", or "E"
        self._mix_mode: str = "A"

        # Read audio properties from file metadata only — files are not kept open.
        self._sample_rate: int = 44100
        self._num_channels: int = 2
        self._duration_frames: int = 0

        for name in STEM_NAMES:
            path = stem_paths.get(name)
            if not path or not os.path.isfile(path):
                raise FileNotFoundError(f"Stem file missing: {name} -> {path}")
            f = ReadableAudioFile(path)
            try:
                self._sample_rate = int(f.samplerate)
                self._num_channels = max(self._num_channels, int(f.num_channels))
                self._duration_frames = max(self._duration_frames, int(f.frames))
            finally:
                f.close()

        # Position tracking.
        # _read_pos is the absolute frame index into the decoded PCM arrays (same
        # coordinate space as _duration_frames). It is also used as the display
        # position so get_time_ms() and seek() work identically before/after decode.
        start_frame = max(0, min(int(start_ms * self._sample_rate / 1000.0), self._duration_frames))
        self._position = start_frame
        self._read_pos = start_frame

        # Pre-allocated mix scratch buffer — reused every callback to avoid per-call
        # np.zeros() allocation (the dominant allocation in the original callback).
        self._mix_buf = np.zeros((BLOCK_SIZE, self._num_channels), dtype=np.float32)

        # Pre-decoded stem data (shape: channels × total_frames, float32).
        # Filled by background threads; callback outputs silence until _decode_done.
        self._pcm_data: Dict[str, np.ndarray] = {}
        self._decode_done = False
        self._decode_cancelled = False
        self._stem_paths = dict(stem_paths)

        # ── Instrument boards (drums/bass/other) — LowpassFilter only ────
        self._boards: Dict[str, Pedalboard] = {}
        self._filters: Dict[str, LowpassFilter] = {}

        for name in ("drums", "bass", "other"):
            lo, _hi = STEM_CUTOFF[name]
            lpf = LowpassFilter(cutoff_frequency_hz=lo)
            self._filters[name] = lpf
            self._boards[name] = Pedalboard([lpf])

        # ── Vocals board — LPF + Reverb (effects toggled by mix mode) ────
        self._vocal_lpf = LowpassFilter(cutoff_frequency_hz=20000.0)
        self._vocal_reverb = Reverb(
            room_size=0.0, wet_level=0.0, dry_level=1.0,
        )
        self._boards["vocals"] = Pedalboard([self._vocal_lpf, self._vocal_reverb])

        # Per-stem amplitude multiplier (vocals only in Mix A/B)
        self._amplitude: Dict[str, float] = {
            "drums": 1.0, "bass": 1.0, "other": 1.0, "vocals": 0.0,
        }

        # Openness values (updated by SoundDrive at 10 Hz)
        self._openness: Dict[str, float] = {
            "drums": 0.1, "bass": 0.35, "other": 0.0, "vocals": 0.0,
        }

        # Dynamic params (updated by SoundDrive at 10 Hz)
        self._stereo_width: float = 1.0   # 0.0 = mono, 1.0 = normal, >1 = extra-wide
        self._vocal_pan: float = 0.0      # -1 = full left, 0 = center, +1 = full right
        self._brake_duck: float = 0.0     # 0 = no duck, 1 = full duck
        self._playback_rate: float = 1.0  # 1.0 = normal speed

        # sounddevice stream (created on play())
        self._stream: Optional[sd.OutputStream] = None

        # Kick off background decode — 4 parallel threads (one per stem).
        # pedalboard/libsndfile releases the GIL during FLAC decode, so all four
        # threads run on separate cores simultaneously on Pi 4B.
        _t = threading.Thread(target=self._decode_all_stems, daemon=True, name="StemDecoder")
        _t.start()

    # ── Background pre-decode ─────────────────────────────────────────────

    def _decode_all_stems(self) -> None:
        """Decode all four stems to float32 RAM in parallel background threads.

        Each stem is decoded on its own thread; pedalboard/libsndfile releases the
        Python GIL during FLAC I/O so all four run concurrently on separate cores.
        The callback outputs silence until _decode_done is set.  The decoded arrays
        always start at frame 0 so _read_pos == display position at all times.
        """
        results: Dict[str, np.ndarray] = {}
        results_lock = threading.Lock()

        def _decode_one(name: str) -> None:
            if self._decode_cancelled:
                return
            path = self._stem_paths.get(name, "")
            if not path or not os.path.isfile(path):
                return
            try:
                f = ReadableAudioFile(path)
                try:
                    pcm = f.read(int(f.frames)).astype(np.float32)
                finally:
                    f.close()
                if not self._decode_cancelled:
                    with results_lock:
                        results[name] = pcm  # shape: (channels, total_frames)
            except Exception:
                pass

        threads = [
            threading.Thread(target=_decode_one, args=(n,), daemon=True)
            for n in STEM_NAMES
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if self._decode_cancelled or len(results) < len(STEM_NAMES):
            return  # aborted or a stem failed — stay in silence mode

        with self._lock:
            self._pcm_data = results
            # _read_pos already holds the correct start position (set in __init__
            # or updated by any seek() called during decode).
            self._decode_done = True

    # ── Mix mode ──────────────────────────────────────────────────────────

    def set_mix_mode(self, mode: str) -> None:
        """Set vocal processing mode: 'A', 'B', 'C', 'D', or 'E'."""
        # Accept "Mode A", "Mix A", or just "A"
        mode = mode.replace("Mode ", "").replace("Mix ", "").strip().upper()
        if mode in ("A", "B", "C", "D", "E"):
            with self._lock:
                self._mix_mode = mode
                self._rebuild_instrument_boards()
            # Re-apply current openness with new mode
            o = self._openness["vocals"]
            self._apply_vocal_openness(o)

    def _rebuild_instrument_boards(self) -> None:
        """Rebuild instrument boards on mode change (all modes use LPF-only)."""
        self._boards["drums"] = Pedalboard([self._filters["drums"]])
        self._boards["bass"] = Pedalboard([self._filters["bass"]])
        self._boards["other"] = Pedalboard([self._filters["other"]])

    # ── Playback control ──────────────────────────────────────────────────

    def play(self) -> None:
        if self._stream is not None:
            return
        self._playing = True
        self._paused = False
        self._ended = False
        pulse_env = {
            "PULSE_PROP_application.name": "LaLaDash Music StemMixer",
            "PULSE_PROP_media.name": "GTR_MUSIC_STEMS",
            "PULSE_SINK": self._pulse_sink or None,
        }
        with _temporary_env(pulse_env):
            self._stream = sd.OutputStream(
                samplerate=self._sample_rate,
                channels=self._num_channels,
                dtype="float32",
                blocksize=BLOCK_SIZE,
                callback=self._audio_callback,
                finished_callback=self._on_stream_finished,
            )
        self._stream.start()

    def pause(self) -> None:
        if self._stream and self._playing:
            self._paused = True

    def resume(self) -> None:
        if self._stream and self._paused:
            self._paused = False

    def stop(self) -> None:
        self._playing = False
        self._paused = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def close(self) -> None:
        self._decode_cancelled = True
        self.stop()
        self._pcm_data.clear()

    def seek(self, ms: int) -> None:
        with self._lock:
            frame = max(0, int(ms * self._sample_rate / 1000.0))
            frame = min(frame, self._duration_frames)
            self._position = frame
            self._read_pos = frame  # array index == absolute frame (decoded from start)

    # ── Queries ────────────────────────────────────────────────────────────

    def get_time_ms(self) -> int:
        with self._lock:
            return int(self._read_pos * 1000.0 / self._sample_rate)

    def get_duration_ms(self) -> int:
        return int(self._duration_frames * 1000.0 / self._sample_rate)

    def is_playing(self) -> bool:
        return self._playing and not self._paused

    # ── Openness control (called by SoundDrive at 10 Hz) ──────────────────

    def set_stem_openness(self, drums: float, bass: float,
                          other: float, vocals: float) -> None:
        self._openness["drums"] = drums
        self._openness["bass"] = bass
        self._openness["other"] = other
        self._openness["vocals"] = vocals
        mode = self._mix_mode

        # Update instrument filters (no amplitude changes — filter only)
        for name in ("drums", "bass", "other"):
            o = max(0.0, min(1.0, self._openness[name]))
            lo, hi = STEM_CUTOFF[name]
            self._filters[name].cutoff_frequency_hz = _exp_sweep(o, lo, hi)
            self._amplitude[name] = 1.0

        # Update vocals based on mix mode
        self._apply_vocal_openness(max(0.0, min(1.0, vocals)))

    def _apply_vocal_openness(self, o: float) -> None:
        """Apply vocal effects based on current mix mode."""
        mode = self._mix_mode

        if mode == "A":
            # Speed Wave — amplitude fade only, no tonal change
            self._vocal_lpf.cutoff_frequency_hz = 20000.0
            self._vocal_reverb.room_size = 0.0
            self._vocal_reverb.wet_level = 0.0
            self._vocal_reverb.dry_level = 1.0
            self._amplitude["vocals"] = 0.14 + (0.86 * o)

        elif mode == "B":
            # Throttle Pulse — LPF sweep + amplitude: vocals shift from muffled to clear
            lo, hi = 400.0, 20000.0
            self._vocal_lpf.cutoff_frequency_hz = _exp_sweep(o, lo, hi)
            self._vocal_reverb.room_size = 0.0
            self._vocal_reverb.wet_level = 0.0
            self._vocal_reverb.dry_level = 1.0
            self._amplitude["vocals"] = o

        elif mode in ("C", "D", "E"):
            # Spatial / Spatial+Distance — vocals rise from distant reverb room
            lo, hi = VOCAL_CUTOFF
            self._vocal_lpf.cutoff_frequency_hz = _exp_sweep(o, lo, hi)
            rev_amount = max(0.0, 1.0 - o * 2.0)  # gone at o >= 0.5
            self._vocal_reverb.room_size = 0.65 * rev_amount
            self._vocal_reverb.wet_level = 0.50 * rev_amount
            self._vocal_reverb.dry_level = 1.0 - 0.4 * rev_amount
            self._amplitude["vocals"] = o

    def set_dynamic_params(self, stereo_width: float = 1.0,
                           vocal_pan: float = 0.0,
                           brake_duck: float = 0.0,
                           drive: float = 0.0) -> None:
        """Per-tick reactive parameters driven by SoundDrive."""
        self._stereo_width = max(0.0, min(1.8, float(stereo_width)))
        self._vocal_pan = max(-1.0, min(1.0, float(vocal_pan)))
        self._brake_duck = max(0.0, min(1.0, float(brake_duck)))
        self._playback_rate = max(0.35, min(1.5, float(drive if drive else 1.0)))

    @staticmethod
    def _resample_block(block: np.ndarray, out_frames: int) -> np.ndarray:
        """Resample (frames, channels) to out_frames using linear interpolation."""
        in_frames = int(block.shape[0])
        if in_frames <= 0:
            return np.zeros((out_frames, block.shape[1]), dtype=np.float32)
        if in_frames == out_frames:
            return block
        if in_frames == 1:
            return np.repeat(block, out_frames, axis=0).astype(np.float32)
        x_src = np.linspace(0.0, 1.0, in_frames, dtype=np.float32)
        x_dst = np.linspace(0.0, 1.0, out_frames, dtype=np.float32)
        out = np.empty((out_frames, block.shape[1]), dtype=np.float32)
        for ch in range(block.shape[1]):
            out[:, ch] = np.interp(x_dst, x_src, block[:, ch]).astype(np.float32)
        return out

    def set_on_end(self, callback: Optional[Callable]) -> None:
        self._on_end = callback

    # ── Audio callback ────────────────────────────────────────────────────

    def _audio_callback(self, outdata: np.ndarray, frames: int,
                        time_info, status) -> None:
        if not self._playing or self._paused:
            outdata[:] = 0
            return

        if not self._decode_done:
            # Silence while background decode is still running.
            outdata[:] = 0
            return

        # Reuse the pre-allocated mix buffer when the frame count matches (always
        # true for fixed-blocksize streams; fall back to fresh allocation otherwise).
        if frames == BLOCK_SIZE:
            mix = self._mix_buf
            mix.fill(0)
        else:
            mix = np.zeros((frames, self._num_channels), dtype=np.float32)

        any_read = False

        with self._lock:
            rate = float(self._playback_rate)
            read_frames = max(1, int(round(frames * rate)))
            pos = self._read_pos

            for name in STEM_NAMES:
                pcm = self._pcm_data.get(name)
                if pcm is None:
                    continue

                stem_total = pcm.shape[1]
                if pos >= stem_total:
                    continue  # this stem is exhausted (EOF)

                # Slice pre-decoded array — no I/O or FLAC decode in callback.
                raw = pcm[:, pos:pos + read_frames]  # shape: (channels, actual_frames)
                if raw.shape[1] == 0:
                    continue

                any_read = True

                # Apply per-stem effects (LPF, and for vocals: LPF + reverb)
                processed = self._boards[name](raw, self._sample_rate, reset=False)

                # Transpose to (samples, channels) for mixing
                processed_t = processed.T

                # Handle mono stems → stereo output
                if processed_t.shape[1] < self._num_channels:
                    processed_t = np.repeat(processed_t, self._num_channels, axis=1)

                if processed_t.shape[0] != frames:
                    processed_t = self._resample_block(processed_t, frames)

                # Amplitude multiplier (vocals in Mix A/B)
                amp = self._amplitude.get(name, 1.0)

                # Brake duck — dip drums + bass when braking (Mode C). Per-stem
                # selectivity rules out a global backend-volume duck; we keep the
                # scalar multiply but do it in-place so each block only writes the
                # already-allocated pedalboard output buffer (no per-stem alloc).
                if name in ("drums", "bass") and self._brake_duck > 0.001:
                    amp *= 1.0 - self._brake_duck * 0.55

                if amp < 0.999:
                    processed_t *= amp

                # Vocal pan — constant-power L/R distribution (Mode C)
                if (name == "vocals" and self._num_channels >= 2
                        and abs(self._vocal_pan) > 0.001
                        and processed_t.shape[1] >= 2):
                    angle = (self._vocal_pan + 1.0) * math.pi * 0.25
                    l_gain = math.cos(angle) * math.sqrt(2.0)
                    r_gain = math.sin(angle) * math.sqrt(2.0)
                    processed_t[:, 0] *= l_gain
                    processed_t[:, 1] *= r_gain

                mix[:frames] += processed_t[:frames]

            self._read_pos += read_frames
            self._position = self._read_pos  # keep _position in sync for external readers

        # Stereo width — narrow→wide mid/side scaling (set by SoundDrive)
        if self._num_channels >= 2 and abs(self._stereo_width - 1.0) > 0.001:
            w = self._stereo_width
            mid = 0.5 * (mix[:, 0] + mix[:, 1])
            side = 0.5 * (mix[:, 0] - mix[:, 1]) * w
            mix[:, 0] = mid + side
            mix[:, 1] = mid - side

        mix *= MASTER_GAIN
        # Soft clip to prevent harsh distortion when all stems sum > 1.0
        np.tanh(mix, out=mix)

        outdata[:] = mix

        if not any_read:
            self._playing = False
            self._ended = True

    def _on_stream_finished(self) -> None:
        if self._ended and self._on_end:
            try:
                self._on_end()
            except Exception:
                pass
