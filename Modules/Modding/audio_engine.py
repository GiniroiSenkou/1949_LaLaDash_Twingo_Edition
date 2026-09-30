# =========================
# audio_engine.py  (LaLaDash)
# =========================
# PyAudio realtime mixer:
#  - Dual-layer engine with pitch (resampling) + continuous blend across RPM bands
#  - On/off throttle sequence blend
#  - Turbo loop with RPM-linked gain
#  - One-shot FX (pops, flutter, gear up/down)
#  - INI-backed settings persistence (shared with GUI)
#
# Public API (used by bridges/modding_bridge.py):
#   class AudioEngine:
#       def __init__(self, settings_path, on_log=None)
#       def load_car_folder(self, folder: str) -> None
#       def set_feature(self, name: str, enabled: bool) -> None
#       def set_threshold(self, name: str, value: int) -> None
#       def get_config_snapshot(self) -> Dict[str, Any]
#       def process_sample(self, rpm: int, gear, throttle=None) -> None
#       def set_volume(self, vol_percent: int) -> None
#       def stop_all(self) -> None
#       def close(self) -> None

from __future__ import annotations

import os
import time
import math
import wave
import random
import threading
import contextlib
import configparser
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
import numpy as np


@contextlib.contextmanager
def _silence_alsa():
    """Redirect C-level stderr to /dev/null for the duration of the block.

    ALSA probes every PCM device on PortAudio init and prints a wall of
    'unable to open slave' / 'Unknown PCM' messages to stderr via the C
    library — Python's sys.stderr redirect can't suppress them.
    Duplicating fd 2 around the call silences them completely.
    """
    devnull = os.open(os.devnull, os.O_WRONLY)
    saved   = os.dup(2)
    os.dup2(devnull, 2)
    os.close(devnull)
    try:
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)


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

try:
    import pyaudio
except Exception as e:
    raise RuntimeError(f"PyAudio is required: {e}")

_HAS_PYDUB = False
try:
    from pydub import AudioSegment
    _HAS_PYDUB = True
except Exception:
    pass

SETTINGS_SECTION = "SoundFeatures"

DEFAULTS = {
    "turbo_enabled":        True,
    "flutter_enabled":      True,
    "pops_enabled":         True,
    "gears_enabled":        True,
    "transmission_enabled": True,
}

# Internal event-detection values — not exposed in settings UI
_THRESHOLDS = {
    "pop_min_rpm":            2000,   # lowered: smoothed RPM reads lower than raw
    "shift_cooldown_ms":      600,
    "turbo_min_rpm":          1200,
}

AUDIO_SR    = 44100
AUDIO_BLOCK = 2048  # larger buffer → more slack per callback → fewer xruns on Pi

# Pre-allocated index array reused by _linear_resample_looped to avoid one
# np.arange() allocation per voice per callback.
_ARANGE_BLOCK = np.arange(AUDIO_BLOCK, dtype=np.float64)

# Scratch buffers for _linear_resample_looped. Each is consumed entirely within
# one call, so they can be shared across voices — the render callback is the
# only caller and PortAudio runs it on a single thread.
_RS_POS  = np.empty(AUDIO_BLOCK, dtype=np.float64)
_RS_FLR  = np.empty(AUDIO_BLOCK, dtype=np.float64)
_RS_I0   = np.empty(AUDIO_BLOCK, dtype=np.int64)
_RS_I1   = np.empty(AUDIO_BLOCK, dtype=np.int64)
_RS_FRAC = np.empty(AUDIO_BLOCK, dtype=np.float32)
_RS_G0   = np.empty(AUDIO_BLOCK, dtype=np.float32)
_RS_G1   = np.empty(AUDIO_BLOCK, dtype=np.float32)

# Handed out by any voice that renders silence. Read-only by contract: the mixer
# only ever passes render() results as *inputs* to numpy ops with an explicit
# `out=`, never writes through one.
_SILENCE = np.zeros(AUDIO_BLOCK, dtype=np.float32)


# ---------------------------------------------------------------------------
# Audio decode
# ---------------------------------------------------------------------------
def _load_pcm_mono(path: str) -> np.ndarray:
    ext = os.path.splitext(path.lower())[1]
    if ext == ".wav":
        with wave.open(path, "rb") as wf:
            sr    = wf.getframerate()
            ch    = wf.getnchannels()
            width = wf.getsampwidth()
            raw   = wf.readframes(wf.getnframes())
        if width == 2:
            pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        elif width == 1:
            pcm = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        else:
            pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            pcm = pcm.reshape(-1, ch).mean(axis=1).astype(np.float32)
        if sr != AUDIO_SR:
            ratio = AUDIO_SR / float(sr)
            idx   = np.arange(0, len(pcm) * ratio) / ratio
            idx   = np.clip(idx, 0, len(pcm) - 1.0001)
            i0    = np.floor(idx).astype(np.int32)
            frac  = idx - i0
            i1    = np.minimum(i0 + 1, len(pcm) - 1)
            pcm   = ((1 - frac) * pcm[i0] + frac * pcm[i1]).astype(np.float32)
        return pcm.astype(np.float32)

    if _HAS_PYDUB:
        seg = AudioSegment.from_file(path)
        seg = seg.set_channels(1).set_frame_rate(AUDIO_SR).set_sample_width(2)
        return np.frombuffer(seg.raw_data, dtype=np.int16).astype(np.float32) / 32768.0

    raise RuntimeError(f"Unsupported format (install pydub for mp3): {path}")


def _advance_pos(pos: float, step: float, n_out: int, length: int) -> float:
    """Where a voice's read head lands after n_out frames, without resampling.

    A muted voice still has to keep its phase, but it does not need any of the
    interpolation below to do it.
    """
    if length <= 0:
        return pos
    return float((pos + step * n_out) % length)


def _linear_resample_looped(src: np.ndarray, pos: float, step: float,
                             n_out: int, out: Optional[np.ndarray] = None,
                             gain: float = 1.0) -> Tuple[np.ndarray, float]:
    """Linear-interpolate `src` at `step` samples/frame into `out`.

    Writes through pre-allocated scratch throughout: the previous version
    allocated ~12 temporaries of 8-16 KB per voice per callback, and with six
    voices that is ~70 allocations every 46 ms, all of them holding the GIL
    against the Qt main thread. `out` must be caller-owned (one buffer per
    voice) because the mixer adds two voices in a single expression.
    """
    L = len(src)
    if L == 0:
        if out is not None:
            out[:n_out] = 0.0
            return out[:n_out], pos
        return _SILENCE[:n_out], pos

    if n_out > AUDIO_BLOCK or out is None:
        # Off-nominal block size (or no caller buffer): fall back to the
        # allocating path rather than growing every scratch array.
        arange = np.arange(n_out, dtype=np.float64)
        positions = pos + step * arange
        positions %= L
        i0 = positions.astype(np.int64)
        frac = (positions - i0).astype(np.float32)
        i1 = (i0 + 1) % L
        res = ((1.0 - frac) * src[i0] + frac * src[i1]).astype(np.float32)
        if gain != 1.0:
            res *= gain
        return res, _advance_pos(pos, step, n_out, L)

    p    = _RS_POS[:n_out]
    flr  = _RS_FLR[:n_out]
    i0   = _RS_I0[:n_out]
    i1   = _RS_I1[:n_out]
    frac = _RS_FRAC[:n_out]
    g0   = _RS_G0[:n_out]
    g1   = _RS_G1[:n_out]
    dst  = out[:n_out]

    np.multiply(_ARANGE_BLOCK[:n_out], step, out=p)
    np.add(p, pos, out=p)
    np.mod(p, L, out=p)              # numpy's mod takes the divisor's sign, so
    np.floor(p, out=flr)             # a negative rate (reverse play) wraps too
    np.subtract(p, flr, out=p)
    np.copyto(i0, flr, casting="unsafe")
    np.copyto(frac, p, casting="unsafe")
    np.add(i0, 1, out=i1)
    np.take(src, i0, out=g0, mode="wrap")
    np.take(src, i1, out=g1, mode="wrap")
    np.subtract(g1, g0, out=g1)
    np.multiply(g1, frac, out=g1)
    np.add(g0, g1, out=dst)
    if gain != 1.0:
        np.multiply(dst, gain, out=dst)
    return dst, _advance_pos(pos, step, n_out, L)


# ---------------------------------------------------------------------------
# Voices
# ---------------------------------------------------------------------------
class _LoopVoice:
    def __init__(self, pcm: np.ndarray):
        self.pcm  = pcm.astype(np.float32) if pcm is not None else np.zeros(1, dtype=np.float32)
        self.pos  = 0.0
        self.rate = 1.0
        self.gain = 1.0
        # One output buffer per voice: the mixer adds two voices in a single
        # np.add(a.render(n), b.render(n), out=...), so they cannot share.
        self._out = np.zeros(AUDIO_BLOCK, dtype=np.float32)

    def render(self, n: int) -> np.ndarray:
        if self.gain <= 1e-4:
            # Silent: keep the read head moving, skip the interpolation. Coast,
            # turbo and transmission voices are muted most of the time, so this
            # removes most of the mix cost in ordinary driving.
            self.pos = _advance_pos(self.pos, self.rate, n, len(self.pcm))
            return _SILENCE[:n] if n <= AUDIO_BLOCK else np.zeros(n, dtype=np.float32)
        buf = self._out if n <= AUDIO_BLOCK else None
        out, self.pos = _linear_resample_looped(
            self.pcm, self.pos, self.rate, n, out=buf, gain=self.gain
        )
        return out


class _OneShotVoice:
    def __init__(self, pcm: np.ndarray, gain: float = 1.0):
        self.pcm  = pcm.astype(np.float32)
        self.idx  = 0
        self.gain = float(gain)
        self._out = np.zeros(AUDIO_BLOCK, dtype=np.float32)

    def render(self, n: int) -> np.ndarray:
        if self.idx >= len(self.pcm):
            return _SILENCE[:n] if n <= AUDIO_BLOCK else np.zeros(n, dtype=np.float32)
        if n > AUDIO_BLOCK:
            out = np.zeros(n, dtype=np.float32)
        else:
            out = self._out[:n]
            out[:] = 0.0
        end = min(self.idx + n, len(self.pcm))
        np.multiply(self.pcm[self.idx:end], self.gain, out=out[:end - self.idx])
        self.idx = end
        return out

    def done(self) -> bool:
        return self.idx >= len(self.pcm)


# ---------------------------------------------------------------------------
# Helpers shared between AudioEngine and set_engine_off_sequence
# ---------------------------------------------------------------------------
def _classify_off_seq(seq: "List[Dict]") -> str:
    """Return strategy from a loaded engine-off sequence.

    strategy — "synthesized" / "ducked" / "replaced" based on sample count.
    Engine-off files are always RPM-keyed; the blend axis is always engine RPM.
    """
    n = len(seq)
    if n == 0:
        return "synthesized"
    if n <= 2:
        return "ducked"
    return "replaced"


# ---------------------------------------------------------------------------
# AudioEngine
# ---------------------------------------------------------------------------
class AudioEngine:
    _THROTTLE_IGNORE = 0.10   # ignore <10%
    _THROTTLE_CURVE_GAMMA = 1.35

    def __init__(self, settings_path=None, on_log=None, output_sink: Optional[str] = None, **kwargs):
        self._log          = on_log or (lambda m: None)
        self.settings_path = Path(settings_path) if settings_path else Path("Data/settings.ini")
        # Explicit PulseAudio/PipeWire sink for the engine stream. None = read
        # [SoundFeatures] engine_default_output; "" = system default. The
        # bridge resolves "auto" to the wired USB card here so the engine
        # never silently lands on a Bluetooth sink and its ~300 ms.
        self._output_sink = output_sink

        self._cfg = configparser.ConfigParser()
        self._ensure_settings()

        self._features = {
            "turbo":        self._get_bool("turbo_enabled"),
            "flutter":      self._get_bool("flutter_enabled"),
            "pops":         self._get_bool("pops_enabled"),
            "gears":        self._get_bool("gears_enabled"),
            "transmission": self._get_bool("transmission_enabled"),
        }
        self._thresholds = dict(_THRESHOLDS)

        # car data
        self._car_folder      = ""
        self._engine_seq: List[Dict]      = []
        self._engine_seq_off: List[Dict]  = []
        self._pop_names: List[str]        = []
        self._flutter_names: List[str]    = []
        self._gear_up: Optional[str]      = None
        self._gear_down: Optional[str]    = None
        self._turbo_name: Optional[str]          = None
        self._transmission_name: Optional[str]  = None
        self._engine_start_name: Optional[str]  = None
        self._gear_up_names: List[str] = []
        self._gear_down_names: List[str] = []
        self._turbo_names: List[str] = []
        self._transmission_names: List[str] = []
        self._engine_start_names: List[str] = []
        self._engine_was_off: bool             = False
        self._engine_start_fade: float         = 1.0   # 0→1 ramp after engine start
        self._engine_start_delay: float        = 0.0   # seconds remaining before loops start
        self._engine_start_voice: Optional[_OneShotVoice] = None  # crossfades into loops
        self._pcm_cache: Dict[str, np.ndarray] = {}

        # continuous-blend band tracking (index of lower bracket, -1 = uninitialised)
        self._blend_lower_on:  int = -1
        self._blend_lower_off: int = -1

        # history
        self._last_rpm:    Optional[int] = None
        self._last_gear:   Optional[int] = None
        self._smooth_rpm:   float = 0.0
        self._rpm_alpha:    float = 0.55   # legacy fixed-cadence alpha (unused; kept for reference)
        # RPM smoothing is now framed as a time constant so it behaves the same
        # whether process_sample() is called every 50 ms (old GUI timer) or once
        # per OBD poll (event-driven). tau=0.063 s reproduces the old alpha=0.55
        # at the previous 50 ms cadence; lower = snappier (less audible lag).
        self._rpm_smooth_tau: float = 0.063
        self._last_sample_ts: Optional[float] = None
        self._prev_throttle_input: Optional[float] = None
        self._prev_throttle_val:   float           = 0.0   # computed (fallback for sim)
        self._last_speed:          Optional[float] = None

        # Shift detection (RPM-divergence heuristic + optional CAN clutch)
        self._clutch_pressed:  bool  = False   # True while RPM diverges from speed
        self._clutch_press_ts: float = 0.0     # monotonic time of last clutch-press
        self._last_shift_ts:   float = 0.0     # cooldown timer for shift events
        self._last_pop_ts:     float = 0.0     # cooldown timer for pop events
        self._throttle_on_since:      Optional[float] = None  # monotonic time when throttle was pressed
        self._last_throttle_hold_dur: float           = 0.0   # saved hold duration at last release
        self._prev_clutch_input: Optional[bool] = None  # CAN clutch from previous tick
        self._gear_at_press: Optional[int] = None       # gear engaged before the clutch went in

        # throttle
        self._target_throttle: float = 0.0
        self._smooth_throttle: float = 0.0
        self._master_gain:     float = 1.0

        # engine shutdown wind-down (pitch drop + fade before switching to electric)
        self._shutdown_active:  bool  = False
        self._shutdown_t:       float = 0.0
        self._shutdown_rate_a:  float = 1.0
        self._shutdown_rate_b:  float = 1.0
        self._shutdown_rate_c:  float = 1.0
        self._shutdown_rate_d:  float = 1.0

        # audio
        self._sample_rate = 44100
        self._buffer      = max(1024, AUDIO_BLOCK)
        self._channels    = 2
        with _silence_alsa():
            self._pa = pyaudio.PyAudio()
        self._stream      = None
        self._stream_lock = threading.Lock()
        self._state_lock  = threading.RLock()

        # Pre-allocated mix accumulator buffers — reused every callback to avoid
        # repeated np.zeros() / + operator allocations in _render_block.
        _n = self._buffer
        self._mix_mono_buf = np.zeros(_n, dtype=np.float32)
        self._on_mix_buf   = np.zeros(_n, dtype=np.float32)
        self._off_mix_buf  = np.zeros(_n, dtype=np.float32)
        self._tmp_mix_buf  = np.zeros(_n, dtype=np.float32)
        # Pre-allocated stereo and int16 output buffers for _pa_callback.
        self._stereo_buf   = np.zeros((_n, 2), dtype=np.float32)
        self._int16_buf    = np.zeros((_n, 2), dtype=np.int16)

        # voices — on-sequence (A=current band, B=next band)
        self._voice_a = _LoopVoice(np.zeros(1, dtype=np.float32))
        self._voice_b = _LoopVoice(np.zeros(1, dtype=np.float32))
        # voices — off-sequence / engine_off (C=current, D=next) — no pitch shift
        self._voice_c = _LoopVoice(np.zeros(1, dtype=np.float32))
        self._voice_d = _LoopVoice(np.zeros(1, dtype=np.float32))
        self._turbo_voice        = _LoopVoice(np.zeros(1, dtype=np.float32))
        self._transmission_voice = _LoopVoice(np.zeros(1, dtype=np.float32))
        self._fx_oneshots: List[_OneShotVoice] = []
        self._fx_lock = threading.Lock()

        # Async FX decode: cold-cache decode would stall the state thread; offload
        # to a daemon worker so the first trigger of a never-seen FX sample skips
        # cleanly and the next trigger hits the warmed cache.
        from collections import deque as _deque
        self._fx_decode_queue: "_deque[str]" = _deque()
        self._fx_decode_pending: set = set()
        self._fx_decode_lock = threading.Lock()
        self._fx_decode_event = threading.Event()
        self._fx_decode_thread: Optional[threading.Thread] = None
        self._fx_decode_stop = False

        # targets
        self._target_rate_a = self._target_rate_b = 1.0
        self._target_gain_a = 1.0
        self._target_gain_b = 0.0
        self._target_rate_c = self._target_rate_d = 1.0
        self._target_gain_c = 1.0
        self._target_gain_d = 0.0
        self._target_turbo_gain        = 0.0
        self._target_turbo_rate        = 0.8
        self._target_transmission_gain = 0.0

        # Smoothed coast crossfade state (0=engine_on side, 1=coast side).
        self._coast_blend: float = 0.0

        # Coast rendering strategy (set at car-load time from off-sample count):
        #   "synthesized" — 0 engine_off files; on-layer reversed at 35% fills the gap
        #   "ducked"      — 1-2 files; on-layer at 35% + coast layer at 100% (default)
        #   "replaced"    — 3+ files; full AC-style replacement (on goes to 0)
        self._coast_strategy: str = "ducked"

        # Turbo boost level integrated with spool lag (0.0–1.0).
        # Driven by throttle+RPM but lags behind with asymmetric time constants.
        self._smooth_boost: float = 0.0

        # transmission hold-off: keep sound alive for ~1.5 s after throttle drops
        self._transmission_hold_t:    float = 0.0
        self._transmission_last_gain: float = 0.0

        self._open_stream()

    # ------------------------------------------------------------------
    # Stream
    # ------------------------------------------------------------------
    def _open_stream(self):
        last_error = None
        if self._output_sink is not None:
            pulse_sink = str(self._output_sink).strip()
        else:
            pulse_sink = self._cfg.get(SETTINGS_SECTION, "engine_default_output", fallback="").strip()
        self._log(f"Engine sink: {pulse_sink or 'system default'}")
        pulse_env = {
            "PULSE_PROP_application.name": "LaLaDash Engine",
            "PULSE_PROP_media.name": "GTR_ENGINE",
            "PULSE_SINK": pulse_sink or None,
        }
        for buf in (int(self._buffer), 2048):
            try:
                self._buffer = int(buf)
                with _temporary_env(pulse_env):
                    self._stream = self._pa.open(
                        format=pyaudio.paInt16,
                        channels=self._channels,
                        rate=self._sample_rate,
                        output=True,
                        frames_per_buffer=self._buffer,
                        stream_callback=self._pa_callback,
                    )
                self._stream.start_stream()
                self._log(f"Audio stream opened @ {self._sample_rate} Hz buffer={self._buffer}")
                return
            except Exception as e:
                last_error = e
                self._stream = None
        self._log(f"Audio stream failed: {last_error}")

    def _pa_callback(self, in_data, frame_count, time_info, status):
        try:
            block = self._render_block(frame_count)  # float32, pre-allocated mono buf
            if block.ndim == 1:
                # Expand mono → stereo into pre-allocated buffer (avoids np.stack alloc)
                stereo = self._stereo_buf[:frame_count]
                stereo[:, 0] = block
                stereo[:, 1] = block
                block = stereo
            # Clip and convert to int16 using pre-allocated buffers
            np.clip(block, -1.0, 1.0, out=block)
            np.multiply(block, 32767.0, out=block)
            out_i16 = self._int16_buf[:frame_count]
            np.copyto(out_i16, block, casting='unsafe')
            out = out_i16.tobytes()
        except Exception as e:
            self._log(f"Audio callback error: {e}")
            out = b"\x00" * frame_count * self._channels * 2
        return (out, pyaudio.paContinue)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def _ensure_settings(self):
        # Read through settings_cache (shared, mtime-checked) rather than a
        # private parser of the whole file.
        try:
            from Modules.Runtime import settings_cache
            shared = settings_cache.shared(str(self.settings_path))
            self._cfg = configparser.ConfigParser()
            self._cfg[SETTINGS_SECTION] = {}
            if shared.has_section(SETTINGS_SECTION):
                for k, v in shared.items(SETTINGS_SECTION):
                    self._cfg[SETTINGS_SECTION][k] = v
        except Exception:
            self._cfg = configparser.ConfigParser()
            self._cfg[SETTINGS_SECTION] = {}
        # Keys this engine has actually changed since the last save. _save()
        # writes only these: the engine's copy of the section is a snapshot
        # from construction, and writing it back whole clobbered keys other
        # code owns (output_volume, set by the Modding bridge, kept snapping
        # back to the value the engine had read at start-up).
        self._dirty_keys: set = set()
        for k, v in DEFAULTS.items():
            if k not in self._cfg[SETTINGS_SECTION]:
                self._cfg[SETTINGS_SECTION][k] = "true" if (isinstance(v, bool) and v) else str(v)
                self._dirty_keys.add(k)
        if self._dirty_keys:
            self._save()

    def _save(self):
        """Merge only the [SoundFeatures] keys this engine changed into the
        current file via settings_cache.

        This used to `cfg.write()` the engine's private copy of the entire INI
        with open("w"): non-atomic, and it wrote back whatever the rest of the
        file looked like when the engine was constructed — a second writer
        racing MusicBridge/SettingsBridge, and on the car (2026-09-15) very
        likely how a brownout-emptied settings.ini came back as three sections.
        """
        try:
            from Modules.Runtime import settings_cache
            keys = set(getattr(self, "_dirty_keys", ()))
            if not keys:
                return
            cfg = settings_cache.load_copy(str(self.settings_path))
            if not cfg.has_section(SETTINGS_SECTION):
                cfg.add_section(SETTINGS_SECTION)
            for k in keys:
                cfg.set(SETTINGS_SECTION, k, str(self._cfg[SETTINGS_SECTION].get(k, "")))
            if settings_cache.save(cfg, str(self.settings_path)):
                self._dirty_keys.clear()
        except Exception as exc:
            self._log(f"settings save failed: {exc}")

    def _get_bool(self, key):
        return self._cfg.getboolean(SETTINGS_SECTION, key, fallback=bool(DEFAULTS.get(key, False)))


    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_config_snapshot(self) -> Dict[str, Any]:
        return {"turbo_enabled":   self._features["turbo"],
                "flutter_enabled": self._features["flutter"],
                "pops_enabled":    self._features["pops"],
                "gears_enabled":   self._features["gears"],
                **self._thresholds}

    def set_feature(self, name: str, enabled: bool):
        name = str(name).lower()
        if name not in self._features: return
        self._features[name] = bool(enabled)
        self._cfg.set(SETTINGS_SECTION, f"{name}_enabled", "true" if enabled else "false")
        self._dirty_keys.add(f"{name}_enabled")
        self._save()

    def set_runtime_feature(self, name: str, enabled: bool):
        name = str(name).lower()
        if name not in self._features:
            return
        self._features[name] = bool(enabled)

    def set_threshold(self, name: str, value: int):
        if name not in self._thresholds: return
        self._thresholds[name] = int(value)
        self._cfg.set(SETTINGS_SECTION, name, str(int(value)))
        self._dirty_keys.add(name)
        self._save()

    def set_volume(self, vol_percent: int):
        with self._state_lock:
            self._master_gain = max(0.0, min(1.0, int(vol_percent) / 100.0))


    def _auto_detect_sequences(self) -> Tuple[List[Dict], List[Dict]]:
        """Scan car folder for engine_on_<rpm>, engine_off_<rpm>, and coast_<rpm> files.
        All engine-off files are RPM-indexed and pitch-shifted to match engine RPM.
        coast_<rpm> and engine_off_<rpm> are treated identically.
        Returns (on_seq, off_seq).
        """
        import re
        on_seq:  List[Dict] = []
        off_seq: List[Dict] = []
        pat_on  = re.compile(r'^engine_on_(\d+)',  re.IGNORECASE)
        pat_off = re.compile(r'^engine_off_(\d+)', re.IGNORECASE)
        pat_cst = re.compile(r'^coast_(\d+)',      re.IGNORECASE)
        try:
            for fname in os.listdir(self._car_folder):
                if not fname.lower().endswith(('.wav', '.mp3')):
                    continue
                stem = os.path.splitext(fname)[0]
                m = pat_on.match(stem)
                if m:
                    on_seq.append({"file": stem, "rpm": int(m.group(1))})
                    continue
                m = pat_off.match(stem) or pat_cst.match(stem)
                if m:
                    off_seq.append({"file": stem, "rpm": int(m.group(1))})
        except Exception:
            pass
        on_seq.sort(key=lambda x: x["rpm"])
        off_seq.sort(key=lambda x: x["rpm"])
        return on_seq, off_seq

    def _auto_detect_fx(self) -> Dict[str, Any]:
        """Scan car folder for FX sounds by standard filename prefixes.
        Detected: pop_* → pops list, flutter* → flutter list,
        turbo* → turbo (first), transmission* → transmission (first),
        gear_up* → gear_up (first), gear_down* → gear_down (first).
        """
        pops: List[str] = []
        flutters: List[str] = []
        turbo = transmission = gear_up = gear_down = engine_start = None
        try:
            fnames = sorted(os.listdir(self._car_folder))
        except Exception:
            fnames = []
        for fname in fnames:
            if not fname.lower().endswith(('.wav', '.mp3')):
                continue
            stem = os.path.splitext(fname)[0]
            sl   = stem.lower()
            if sl.startswith('pop_'):
                pops.append(stem)
            elif sl.startswith('flutter'):
                flutters.append(stem)
            elif sl.startswith('engine_start') and engine_start is None:
                engine_start = stem
            elif sl.startswith('turbo') and turbo is None:
                turbo = stem
            elif sl.startswith('transmission') and transmission is None:
                transmission = stem
            elif sl.startswith('gear_up') and gear_up is None:
                gear_up = stem
            elif sl.startswith('gear_down') and gear_down is None:
                gear_down = stem
        return {"pop_sounds": pops, "flutter": flutters,
                "engine_start": engine_start,
                "turbo": turbo, "transmission": transmission,
                "gear_up": gear_up, "gear_down": gear_down}

    def _prime_audio_cache(self):
        names: list[str] = []
        for seq in (self._engine_seq, self._engine_seq_off):
            for entry in seq:
                name = str(entry.get("file", "") or "").strip()
                if name:
                    names.append(name)
        for name in (
            self._turbo_name,
            self._transmission_name,
            self._gear_up,
            self._gear_down,
            self._engine_start_name,
        ):
            if name:
                names.append(str(name))
        names.extend(self._pop_names)
        names.extend(self._flutter_names)
        names.extend(self._turbo_names)
        names.extend(self._transmission_names)
        names.extend(self._engine_start_names)
        names.extend(self._gear_up_names)
        names.extend(self._gear_down_names)

        loaded = 0
        for name in names:
            try:
                if self._decode_name(name) is not None:
                    loaded += 1
            except Exception:
                pass
        self._log(f"Primed audio cache: {loaded}/{len(names)} assets")

    def load_car_folder(self, folder: str):
        self._car_folder = os.path.abspath(folder)
        import json
        cfg_path = os.path.join(self._car_folder, "config.json")
        car_cfg: Dict[str, Any] = {}
        try:
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    car_cfg = json.load(f)
        except Exception as e:
            self._log(f"config.json error: {e}")

        seq    = car_cfg.get("engine_sequence_in", [])
        # engine_sequence_off is the primary key; coast_sequence kept for legacy configs
        raw_off = car_cfg.get("engine_sequence_off", []) or car_cfg.get("coast_sequence", [])

        # Auto-detect from filenames: engine_off_<rpm> and coast_<rpm> both accepted.
        auto_on, auto_off = self._auto_detect_sequences()
        if not seq:
            seq = auto_on
            if seq:
                self._log(f"Auto-detected {len(seq)} on-bands from filenames")
        if not raw_off and auto_off:
            raw_off = auto_off
            self._log(f"Auto-detected {len(raw_off)} RPM-keyed coast-bands from filenames")

        with self._state_lock:
            self._engine_seq = sorted(seq, key=lambda x: int(x.get("rpm", 0)))

            if raw_off and isinstance(raw_off[0], dict):
                self._engine_seq_off = sorted(raw_off, key=lambda x: int(x.get("rpm", 0)))
            else:
                self._engine_seq_off = []

            # Determine rendering strategy from off-sample count.
            self._coast_strategy = _classify_off_seq(self._engine_seq_off)

            fx = self._auto_detect_fx()
            self._pop_names     = list(car_cfg.get("pop_sounds") or fx["pop_sounds"])
            self._flutter_names = list(car_cfg.get("flutter")    or fx["flutter"])
            self._gear_up       = car_cfg.get("gear_up")         or fx["gear_up"]
            self._gear_down     = car_cfg.get("gear_down")       or fx["gear_down"]
            # "turbo" / "transmission" keys in config (even null) override auto-detect
            self._turbo_name        = car_cfg["turbo"]        if "turbo"        in car_cfg else fx["turbo"]
            self._transmission_name = car_cfg["transmission"] if "transmission" in car_cfg else fx.get("transmission")
            self._engine_start_name = car_cfg.get("engine_start") or fx["engine_start"]
            self._gear_up_names = [self._gear_up] if self._gear_up else []
            self._gear_down_names = [self._gear_down] if self._gear_down else []
            self._turbo_names = [self._turbo_name] if self._turbo_name else []
            self._transmission_names = [self._transmission_name] if self._transmission_name else []
            self._engine_start_names = [self._engine_start_name] if self._engine_start_name else []
            self._engine_was_off     = False  # reset on car change
            self._target_transmission_gain = 0.0
            self._engine_start_fade  = 1.0   # no fade-in needed when switching cars
            self._engine_start_delay = 0.0
            with self._fx_lock:
                self._engine_start_voice = None

            # reset blend state
            self._blend_lower_on  = -1
            self._blend_lower_off = -1
            self._coast_blend = 0.0
            self._pcm_cache.clear()
            self._prime_audio_cache()

            self._log(f"Loaded: {os.path.basename(folder)}  "
                      f"on={len(self._engine_seq)} engine_off={len(self._engine_seq_off)} bands  "
                      f"coast={self._coast_strategy}(rpm)")

    def stop_all(self):
        with self._stream_lock:
            with self._state_lock:
                for v in (self._voice_a, self._voice_b, self._voice_c,
                          self._voice_d, self._turbo_voice, self._transmission_voice):
                    v.gain = 0.0
                self._target_throttle = self._smooth_throttle = 0.0
                self._coast_blend = 0.0
                with self._fx_lock:
                    self._fx_oneshots.clear()
        try:
            if self._stream:
                self._stream.stop_stream()
                self._stream.close()
                self._stream = None
        except Exception:
            pass

    def close(self):
        try:
            self.stop_all()
        finally:
            try:
                if self._pa:
                    self._pa.terminate()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # process_sample — called every telemetry tick
    # ------------------------------------------------------------------
    @classmethod
    def _normalize_driver_throttle(cls, throttle: float, *, progressive: bool = True) -> float:
        """Normalize throttle to 0..1 with deadzone.

        Accepts either 0..1 or 0..100 input values.
        """
        value = max(0.0, float(throttle))
        if value > 1.0:
            value *= 0.01
        value = min(1.0, value)
        if value < cls._THROTTLE_IGNORE:
            return 0.0
        linear = max(0.0, min(1.0, (value - cls._THROTTLE_IGNORE) / (1.0 - cls._THROTTLE_IGNORE)))
        if not progressive:
            return linear
        return max(0.0, min(1.0, linear ** cls._THROTTLE_CURVE_GAMMA))

    def process_sample(self, rpm: int, gear: Optional[int], throttle: Optional[float] = None,
                       speed: Optional[float] = None, clutch: Optional[bool] = None):
        with self._state_lock:
            # Elapsed time since the last sample. Clamp it so an unusually long
            # gap (caller stalled) or a burst can't destabilise the time-based
            # filters below. First call assumes the nominal cadence.
            _now = time.monotonic()
            if self._last_sample_ts is None:
                dt = 0.05
            else:
                dt = max(0.02, min(0.30, _now - self._last_sample_ts))
            self._last_sample_ts = _now

            if self._last_rpm is None:
                self._smooth_rpm = float(rpm)
            else:
                alpha = 1.0 - math.exp(-dt / self._rpm_smooth_tau)
                self._smooth_rpm = (1 - alpha) * self._smooth_rpm + alpha * float(rpm)

            # Engine off: raw RPM must be 0 (engine not running / telemetry shows ???)
            if rpm < 1:
                self._engine_was_off = True

            # Engine start: first tick where raw RPM is above idle threshold after being off
            elif rpm >= 300 and self._engine_was_off:
                self._engine_was_off    = False
                self._engine_start_fade = 0.0   # engine loops start fully muted
                self._engine_start_delay = 1.0  # hold loops silent for 1 s while start plays
                start_name = random.choice(self._engine_start_names) if self._engine_start_names else self._engine_start_name
                if start_name:
                    pcm = self._decode_name(start_name)
                    if pcm is not None:
                        with self._fx_lock:
                            self._engine_start_voice = _OneShotVoice(pcm, gain=1.0)

            # Count down the start-sound hold, then fade loops in over ~1 s.
            # Stepped by real elapsed time so the 1 s hold/fade is honoured at
            # any call cadence.
            if self._engine_start_delay > 0.0:
                self._engine_start_delay = max(0.0, self._engine_start_delay - dt)
            elif self._engine_start_fade < 1.0:
                self._engine_start_fade = min(1.0, self._engine_start_fade + dt)

            if throttle is not None:
                throttle_signal = self._normalize_driver_throttle(throttle, progressive=False)
                throttle_val = self._normalize_driver_throttle(throttle, progressive=True)
            else:
                if self._last_rpm is not None:
                    delta = float(rpm) - float(self._last_rpm)
                    self._target_throttle = 1.0 if delta >= 0 else 0.0
                throttle_val = self._target_throttle
                throttle_signal = None

            # Track how long throttle was held; used by _trigger_flutter to scale volume.
            _tnow = time.monotonic()
            if throttle_val > 0.01:
                if self._throttle_on_since is None:
                    self._throttle_on_since = _tnow
            elif self._throttle_on_since is not None:
                self._last_throttle_hold_dur = _tnow - self._throttle_on_since
                self._throttle_on_since = None

            self._update_turbo_level(int(self._smooth_rpm), throttle_val, dt)
            self._handle_events(int(self._smooth_rpm), gear, throttle_signal, throttle_val, speed, clutch)
            self._update_engine_targets(int(self._smooth_rpm), throttle_val, speed=speed)
            self._update_transmission_level(int(self._smooth_rpm), throttle_val, speed=speed)

            self._last_rpm   = int(self._smooth_rpm)
            self._last_gear  = gear if isinstance(gear, int) else self._last_gear
            self._last_speed = speed
            self._prev_throttle_input = throttle_signal
            self._prev_throttle_val   = throttle_val

    # ------------------------------------------------------------------
    # Event detection
    # ------------------------------------------------------------------
    def _handle_events(self, rpm: int, gear: Optional[int],
                       throttle_raw: Optional[float], throttle_val: float,
                       speed: Optional[float] = None,
                       clutch: Optional[bool] = None):
        prev = self._prev_throttle_input  # raw OBD throttle from previous tick

        # ---------- Pops (throttle lift-off only) ----------
        # Use real OBD ThrottlePos when available; fall back to the computed
        # throttle_val (binary 0/1 from RPM delta) for sim / no-ThrottlePos mode.
        _pop_cur  = throttle_raw if throttle_raw is not None else throttle_val
        _pop_prev = prev         if prev is not None         else self._prev_throttle_val
        if (self._features["pops"] and self._last_rpm is not None
                and _pop_prev >= 0.35 and _pop_cur < 0.15
                and rpm >= self._thresholds["pop_min_rpm"]):
            self._trigger_pop(0.6 + 0.4 * _pop_prev)

        # ---------- Flutter + Gear FX (lift-under-boost + shift detection) ----------
        # Lift-under-boost: fires whenever throttle drops hard while boost is built,
        # independent of gear shifts (mid-corner lift, engine braking with built boost).
        if (self._features["flutter"] and _pop_prev >= 0.50 and _pop_cur < 0.15
                and rpm >= self._thresholds["pop_min_rpm"]
                and self._smooth_boost >= 0.4):
            self._trigger_flutter(0.6 + 0.4 * self._smooth_boost)

        if self._features["flutter"] or self._features["gears"]:
            if self._detect_shift(rpm, speed, clutch):
                if self._features["flutter"]:
                    self._trigger_flutter(1.0)
                    if self._features["pops"]:
                        self._trigger_pop(0.4)
                # The gear read while the clutch is in is meaningless, and the
                # previous tick's gear usually already equals the new one — compare
                # against the gear held before the press, and stay quiet if unknown.
                before = self._gear_at_press
                if (self._features["gears"] and isinstance(gear, int) and gear > 0
                        and isinstance(before, int) and before > 0 and gear != before):
                    self._trigger_gear_fx(gear > before)

    def _detect_shift(self, rpm: int, speed: Optional[float],
                       clutch: Optional[bool]) -> bool:
        """Detect a gear shift and return True on the release edge.

        CAN clutch (primary): uses the raw clutch switch — shift is confirmed
        on the falling edge (clutch released after being pressed).

        RPM-divergence (fallback): when RPM drops > 400 in one tick while
        vehicle speed stays within 2 km/h, that's a clutch-press.  When RPM
        recovers (rises > 200), that's the release → shift confirmed.
        """
        now = time.monotonic()
        cooldown = self._thresholds["shift_cooldown_ms"] / 1000.0
        if now - self._last_shift_ts < cooldown:
            return False

        # --- CAN clutch (primary) ---
        if clutch is not None:
            prev_clutch = self._prev_clutch_input
            self._prev_clutch_input = clutch
            if clutch and not prev_clutch:
                self._gear_at_press = self._last_gear
            if prev_clutch is True and clutch is False:
                # Clutch released → shift complete
                self._last_shift_ts = now
                return True
            return False

        # --- RPM-divergence heuristic (fallback) ---
        if self._last_rpm is None:
            return False

        rpm_delta = rpm - self._last_rpm
        # When speed data is available confirm it stays stable during the RPM drop.
        # Without speed data (sim / no OBD speed PID) skip the constraint so
        # flutters still fire on RPM pattern alone.
        speed_stable = (
            speed is None or self._last_speed is None
            or abs(speed - self._last_speed) < 2.0
        )

        if not self._clutch_pressed:
            # Detect clutch press: RPM drops sharply (and speed stable if known).
            # Per RPM update, not per second: RPM is rate-limited to 5 Hz upstream.
            if rpm_delta < -100 and speed_stable:
                self._clutch_pressed = True
                self._clutch_press_ts = now
                self._gear_at_press = self._last_gear
        else:
            # Timeout: if clutch "pressed" for > 2s, it's not a shift
            if now - self._clutch_press_ts > 2.0:
                self._clutch_pressed = False
                return False
            # Detect clutch release: RPM recovers upward.
            if rpm_delta > 50:
                self._clutch_pressed = False
                self._last_shift_ts = now
                return True

        return False

    def _trigger_pop(self, strength: float):
        if not self._pop_names: return
        now = time.monotonic()
        if now - self._last_pop_ts < 0.10:   # 100 ms cooldown — prevents double-fire
            return
        self._last_pop_ts = now
        pcm = self._decode_name_nonblocking(random.choice(self._pop_names))
        if pcm is None: return
        with self._fx_lock:
            self._fx_oneshots.append(_OneShotVoice(pcm, gain=max(0.1, min(1.0, strength))))

    def _trigger_flutter(self, strength: float):
        if not self._flutter_names: return
        pcm = self._decode_name_nonblocking(random.choice(self._flutter_names))
        if pcm is None: return
        with self._fx_lock:
            self._fx_oneshots.append(_OneShotVoice(pcm, gain=max(0.1, min(1.0, strength))))

    def _trigger_gear_fx(self, up: bool):
        choices = self._gear_up_names if up else self._gear_down_names
        name = random.choice(choices) if choices else (self._gear_up if up else self._gear_down)
        if not name: return
        pcm = self._decode_name_nonblocking(name)
        if pcm is None: return
        with self._fx_lock:
            self._fx_oneshots.append(_OneShotVoice(pcm, gain=0.9))

    # ------------------------------------------------------------------
    # Continuous-blend voice pair update
    # ------------------------------------------------------------------
    def _update_voice_pair(self, rpm: float, seq: List[Dict],
                            va: '_LoopVoice', vb: '_LoopVoice',
                            idx_attr: str,
                            rate_a_attr: str, rate_b_attr: str,
                            gain_a_attr: str, gain_b_attr: str):
        """Always blend between the two files that bracket current RPM.
        No hysteresis — blend tracks RPM continuously.
        On a forward advance (lower == prev+1), inherit vb's playback
        position into va so there is no click.
        """
        n = len(seq)
        if n == 0:
            return

        thr = [max(1, int(x.get("rpm", 1))) for x in seq]

        if n == 1:
            pcm = self._decode_name(seq[0].get("file"))
            if pcm is not None and va.pcm is not pcm:
                va.pcm = pcm; va.pos = 0.0
            setattr(self, rate_a_attr, max(0.25, min(3.0, max(1.0, rpm) / thr[0])))
            setattr(self, gain_a_attr, 1.0)
            setattr(self, rate_b_attr, 1.0)
            setattr(self, gain_b_attr, 0.0)
            return

        # Find lower bracket: highest index where thr[i] <= rpm
        lower = 0
        for i in range(n - 1):
            if rpm >= thr[i]:
                lower = i
        lower = min(lower, n - 2)
        upper = lower + 1

        prev = getattr(self, idx_attr)
        if prev != lower:
            pcm_a = self._decode_name(seq[lower].get("file"))
            pcm_b = self._decode_name(seq[upper].get("file"))
            if pcm_a is not None:
                if lower == prev + 1 and vb.pcm is pcm_a:
                    # RPM rose one step: vb was playing pcm_a at full gain.
                    # Hand its entire live state to va — no gap, no gain jump.
                    va.pcm  = pcm_a
                    va.pos  = vb.pos
                    va.gain = vb.gain
                    va.rate = vb.rate
                elif lower == prev - 1 and va.pcm is pcm_b:
                    # RPM dropped one step: va was playing pcm_b (now the upper band)
                    # at full gain.  Hand its live state to vb so volume is continuous,
                    # then let va fade in the new lower-band content from silence.
                    vb.pcm  = pcm_b
                    vb.pos  = va.pos
                    vb.gain = va.gain
                    vb.rate = va.rate
                    va.pcm  = pcm_a
                    va.pos  = 0.0
                    va.gain = 0.0
                elif va.pcm is not pcm_a:
                    va.pcm  = pcm_a
                    va.pos  = 0.0
                    va.gain = 0.0      # non-sequential jump: fade in from silence
            if pcm_b is not None and vb.pcm is not pcm_b:
                vb.pcm  = pcm_b
                vb.pos  = 0.0
                vb.gain = 0.0  # new upper-band content always starts silent
            setattr(self, idx_attr, lower)

        rec_a = float(thr[lower])
        rec_b = float(thr[upper])
        blend = max(0.0, min(1.0, (rpm - rec_a) / max(1.0, rec_b - rec_a)))
        setattr(self, rate_a_attr, max(0.25, min(3.0, max(1.0, rpm) / rec_a)))
        setattr(self, rate_b_attr, max(0.25, min(3.0, max(1.0, rpm) / rec_b)))
        setattr(self, gain_a_attr, 1.0 - blend)
        setattr(self, gain_b_attr, blend)

    # RPM below which the engine is considered idle → always use engine_on samples
    _IDLE_RPM = 1000
    # Minimum RPM for engine_off (coasting) to be active
    _COAST_MIN_RPM = 1500

    def _update_engine_targets(self, rpm: int, throttle: float, speed: Optional[float] = None):
        self._target_throttle = max(0.0, min(1.0, throttle))

        # Below _COAST_MIN_RPM there is no coasting: without this a car with no
        # coast files played its idle backwards at 35 % whenever idle sat >150 rpm
        # above the idle sample (a cold engine). Coast voices fade via the lerp.
        if rpm < self._COAST_MIN_RPM:
            self._target_throttle = 1.0

        if self._engine_seq:
            self._update_voice_pair(
                float(rpm), self._engine_seq,
                self._voice_a, self._voice_b, '_blend_lower_on',
                '_target_rate_a', '_target_rate_b',
                '_target_gain_a', '_target_gain_b')

        if self._engine_seq_off:
            # Blend axis is always engine RPM — pitch-shifted to match current revs.
            if rpm >= self._IDLE_RPM:
                # Keep voices primed at all valid RPM so there is no gap when
                # RPM crosses _COAST_MIN_RPM — the throttle lerp handles the fade.
                self._update_voice_pair(
                    float(rpm), self._engine_seq_off,
                    self._voice_c, self._voice_d, '_blend_lower_off',
                    '_target_rate_c', '_target_rate_d',
                    '_target_gain_c', '_target_gain_d')
            else:
                # Below idle RPM: zero gains hard (engine is off / stalled).
                self._target_gain_c = 0.0
                self._target_gain_d = 0.0

        # After engine start, fade loops in over ~1.5 s so the engine_start
        # one-shot can be heard cleanly before the loops take over.
        if self._engine_start_fade < 1.0:
            self._target_gain_a *= self._engine_start_fade
            self._target_gain_b *= self._engine_start_fade
            self._target_gain_c *= self._engine_start_fade
            self._target_gain_d *= self._engine_start_fade

        # While engine is completely off, silence all loop voices.
        # _update_voice_pair still runs above so voices are primed (right files +
        # positions) and will fade in the instant _engine_was_off clears.
        if self._engine_was_off:
            self._target_gain_a = 0.0
            self._target_gain_b = 0.0
            self._target_gain_c = 0.0
            self._target_gain_d = 0.0

    # Spool / dump time constants (s). Equal to the old per-tick 0.12 / 0.33
    # at the 50 ms cadence they were tuned on: ~0.65 s to 80 % boost.
    _BOOST_TAU_UP = 0.39
    _BOOST_TAU_DOWN = 0.125

    def _update_turbo_level(self, rpm: int, throttle: float, dt: float = 0.05):
        turbo_name = self._turbo_names[0] if self._turbo_names else self._turbo_name
        if not self._features["turbo"] or not turbo_name:
            self._smooth_boost = 0.0
            self._target_turbo_gain = 0.0; return
        thr = max(0.0, min(1.0, throttle))
        if thr <= 0.20:
            thr_norm = 0.0
        else:
            thr_norm = min(1.0, (thr - 0.20) / 0.80)
        # RPM gate: no boost below idle RPM.
        rpm_gate = max(0.0, min(1.0, (rpm - 800) / 400.0))  # 0 at 800, 1.0 at 1200+
        boost_target = thr_norm * rpm_gate
        tau = self._BOOST_TAU_UP if boost_target >= self._smooth_boost else self._BOOST_TAU_DOWN
        self._smooth_boost += (1.0 - math.exp(-dt / tau)) * (boost_target - self._smooth_boost)
        self._smooth_boost = max(0.0, min(1.0, self._smooth_boost))
        self._target_turbo_gain = self._smooth_boost * 0.64
        self._target_turbo_rate = 0.8 + 0.4 * self._smooth_boost
        pcm = self._decode_name(turbo_name)
        if pcm is not None and self._turbo_voice.pcm is not pcm:
            self._turbo_voice.pcm = pcm

    def _update_transmission_level(self, rpm: int, throttle: float = 0.0,
                                    speed: Optional[float] = None):
        """Transmission whine: purely speed-driven volume and pitch.

        The gearbox spins whenever the car is moving, independent of throttle.
        Audible from 40 km/h; pitch and volume both rise with speed.

        Pitch reference: 80 km/h → rate 1.0 (natural playback speed).
          40 km/h ≈ 0.50 (low rumble)   120 km/h ≈ 1.50   240 km/h → 3.0 (clamped)
        Volume: silent below 40 km/h, full at 60 km/h (20 km/h ramp).
        """
        transmission_name = self._transmission_names[0] if self._transmission_names else self._transmission_name
        if not self._features.get("transmission", True) or not transmission_name:
            self._target_transmission_gain = 0.0
            return

        pcm = self._decode_name(transmission_name)
        if pcm is not None and self._transmission_voice.pcm is not pcm:
            self._transmission_voice.pcm = pcm

        spd = float(speed) if speed is not None else 0.0

        # Pitch: linear with speed, reference 80 km/h = rate 1.0
        self._transmission_voice.rate = max(0.3, min(3.0, spd / 80.0))

        # Volume: 0 below 40 km/h, ramps to 1.0 at 60 km/h, scaled by load.
        # Gear teeth mesh harder under load → transmission whine is louder on power.
        load_factor = 0.35 + 0.65 * max(0.0, min(1.0, throttle))
        if spd < 40.0:
            self._target_transmission_gain = 0.0
        else:
            self._target_transmission_gain = min(1.0, (spd - 40.0) / 20.0) * load_factor

    # ------------------------------------------------------------------
    # Render block (audio callback)
    # ------------------------------------------------------------------
    def _render_block(self, n: int) -> np.ndarray:
        with self._state_lock:
            # --- Engine shutdown: drop pitch and prepare to fade out ---
            if self._shutdown_active:
                self._shutdown_t = min(1.0, self._shutdown_t + n / self._sample_rate)
                f  = self._shutdown_t
                ps = max(0.3, 1.0 - f * 0.7)
                self._target_rate_a = self._shutdown_rate_a * ps
                self._target_rate_b = self._shutdown_rate_b * ps
                self._target_rate_c = self._shutdown_rate_c * ps
                self._target_rate_d = self._shutdown_rate_d * ps

            lerp = 0.28
            self._voice_a.rate  = (1-lerp)*self._voice_a.rate  + lerp*self._target_rate_a
            self._voice_b.rate  = (1-lerp)*self._voice_b.rate  + lerp*self._target_rate_b
            self._voice_a.gain  = (1-lerp)*self._voice_a.gain  + lerp*self._target_gain_a
            self._voice_b.gain  = (1-lerp)*self._voice_b.gain  + lerp*self._target_gain_b

            has_off = bool(self._engine_seq_off)
            if has_off:
                self._voice_c.rate = (1-lerp)*self._voice_c.rate + lerp*self._target_rate_c
                self._voice_d.rate = (1-lerp)*self._voice_d.rate + lerp*self._target_rate_d
                self._voice_c.gain = (1-lerp)*self._voice_c.gain + lerp*self._target_gain_c
                self._voice_d.gain = (1-lerp)*self._voice_d.gain + lerp*self._target_gain_d

            _turbo_lerp = 0.08 if self._target_turbo_gain > self._turbo_voice.gain else 0.05
            self._turbo_voice.gain = (1-_turbo_lerp)*self._turbo_voice.gain + _turbo_lerp*self._target_turbo_gain
            self._turbo_voice.rate = (1-_turbo_lerp)*self._turbo_voice.rate + _turbo_lerp*self._target_turbo_rate
            self._transmission_voice.gain = (1-lerp)*self._transmission_voice.gain + lerp*self._target_transmission_gain

            # Rising: smooth attack.
            # Falling: high-throttle decay is slower, but full lift (target=0)
            # snaps immediately so coast can engage right away.
            if self._target_throttle <= 0.0:
                self._smooth_throttle = 0.0
            elif self._target_throttle >= self._smooth_throttle:
                _t_lerp = 0.30
                self._smooth_throttle = (1 - _t_lerp) * self._smooth_throttle + _t_lerp * self._target_throttle
            else:
                _t_release = max(0.01, min(1.0, 0.12 * (1.0 - 0.75 * self._smooth_throttle)))
                self._smooth_throttle = (
                    (1 - _t_release) * self._smooth_throttle
                    + _t_release * self._target_throttle
                )
            t = max(0.0, min(1.0, self._smooth_throttle))

            # Idle-band guard: first entry in engine_seq is the idle loop; keep it
            # at stable forward volume so the engine sounds right at standstill.
            _idle_rpm = int(self._engine_seq[0].get('rpm', 0)) if self._engine_seq else 0
            _at_idle_band = int(self._smooth_rpm) <= _idle_rpm + 150

            # When there is no driver demand and RPM is above idle: play engine-on
            # backwards at low ambient volume to simulate decel drag.
            # Rate stored positive for the lerp; flip only at render time so
            # _linear_resample_looped advances pos in reverse.
            _has_driver_demand = self._target_throttle > 1e-4
            _throttle_releasing = (not _has_driver_demand) and not _at_idle_band
            if _throttle_releasing:
                self._voice_a.rate = -abs(self._voice_a.rate)
                self._voice_b.rate = -abs(self._voice_b.rate)

            # Accumulate voice_a + voice_b into pre-allocated on_mix buffer.
            on_mix = self._on_mix_buf[:n]
            np.add(self._voice_a.render(n), self._voice_b.render(n), out=on_mix)

            if _throttle_releasing:
                self._voice_a.rate = abs(self._voice_a.rate)
                self._voice_b.rate = abs(self._voice_b.rate)

            # Coast must only exist while truly coasting (zero driver demand),
            # and should fade out smoothly (not abruptly) when engine returns.
            _coast_target = 1.0 if (has_off and _throttle_releasing) else 0.0
            _coast_lerp = 0.08 if _coast_target > self._coast_blend else 0.14
            self._coast_blend = (1.0 - _coast_lerp) * self._coast_blend + _coast_lerp * _coast_target
            self._coast_blend = max(0.0, min(1.0, self._coast_blend))

            off_weight = self._coast_blend if has_off else 0.0
            on_weight = max(0.0, min(1.0, 1.0 - off_weight))
            # Duck level during coast depends on off-sample richness.
            # "replaced" (3+ files) → silence on-layer once coast is audible.
            # "ducked"   (1-2 files) → 35% on-layer fills thin coverage.
            # "synthesized" (0 files) → on-layer reversed above already covers it.
            if _throttle_releasing and has_off and self._coast_strategy == "replaced" and off_weight > 0.05:
                _on_release_duck = 0.0
            elif _throttle_releasing:
                _on_release_duck = 0.35
            else:
                _on_release_duck = 1.0

            # mix = on_mix * weight — write into pre-allocated mix buffer.
            mix = self._mix_mono_buf[:n]
            np.multiply(on_weight * _on_release_duck, on_mix, out=mix)

            # Reusable temp buffer for scaled contributions.
            tmp = self._tmp_mix_buf[:n]

            if has_off:
                off_mix = self._off_mix_buf[:n]
                np.add(self._voice_c.render(n), self._voice_d.render(n), out=off_mix)
                np.multiply(off_weight, off_mix, out=tmp)
                np.add(mix, tmp, out=mix)

            np.multiply(0.25, self._turbo_voice.render(n), out=tmp)
            np.add(mix, tmp, out=mix)
            np.multiply(0.2, self._transmission_voice.render(n), out=tmp)
            np.add(mix, tmp, out=mix)

            with self._fx_lock:
                alive = []
                for fx in self._fx_oneshots:
                    np.add(mix, fx.render(n), out=mix)
                    if not fx.done():
                        alive.append(fx)
                self._fx_oneshots = alive

                if self._engine_start_voice is not None:
                    sv_gain = 1.0 - self._engine_start_fade
                    if sv_gain > 1e-4 and not self._engine_start_voice.done():
                        np.multiply(sv_gain, self._engine_start_voice.render(n), out=tmp)
                        np.add(mix, tmp, out=mix)
                    else:
                        self._engine_start_voice = None

            peak = np.max(np.abs(mix))
            if peak > 1.0:
                mix /= peak

            if self._shutdown_active:
                mix *= max(0.0, 1.0 - self._shutdown_t)

            mix *= self._master_gain
            return mix  # float32 pre-allocated; caller must not cache past next callback

    # ------------------------------------------------------------------
    # Debug / logging
    # ------------------------------------------------------------------
    def get_debug_state(self) -> str:
        """Return a one-line summary of the current mix state for the sound log.

        Gains shown are *effective* (voice.gain × on/off factor) so they
        directly reflect what the listener hears — not the raw blend ratios.
        """
        with self._state_lock:
            rpm = int(self._smooth_rpm)
            t   = max(0.0, min(1.0, self._smooth_throttle))   # on-throttle weight

            def _fname(pcm: Optional[np.ndarray]) -> str:
                if pcm is None or (hasattr(pcm, '__len__') and len(pcm) <= 1):
                    return "---"
                for path, cached in self._pcm_cache.items():
                    if cached is pcm:
                        return os.path.splitext(os.path.basename(path))[0]
                return "?"

            parts = [f"RPM:{rpm}  on/off:{t:.2f}  fade:{self._engine_start_fade:.2f}"]
            _off_w = 0.0

            if self._engine_seq:
                fa = _fname(self._voice_a.pcm)
                fb = _fname(self._voice_b.pcm)
                _idle_rpm = int(self._engine_seq[0].get('rpm', 0)) if self._engine_seq else 0
                _at_idle_band = int(self._smooth_rpm) <= _idle_rpm + 150
                _has_driver_demand = self._target_throttle > 1e-4
                _coast_target = 1.0 if (self._engine_seq_off and (not _has_driver_demand) and (not _at_idle_band)) else 0.0
                _coast_lerp = 0.08 if _coast_target > self._coast_blend else 0.14
                _coast_blend_dbg = (1.0 - _coast_lerp) * self._coast_blend + _coast_lerp * _coast_target
                _coast_blend_dbg = max(0.0, min(1.0, _coast_blend_dbg))
                _off_w = _coast_blend_dbg if self._engine_seq_off else 0.0
                _on_w = max(0.0, min(1.0, 1.0 - _off_w))
                _releasing_dbg = (not _has_driver_demand) and (not _at_idle_band)
                if _releasing_dbg and self._engine_seq_off and self._coast_strategy == "replaced" and _off_w > 0.05:
                    _on_release_duck = 0.0
                elif _releasing_dbg:
                    _on_release_duck = 0.35
                else:
                    _on_release_duck = 1.0
                _on_eff = _on_w * _on_release_duck
                ga = self._voice_a.gain * _on_eff
                gb = self._voice_b.gain * _on_eff
                parts.append(f"ON(×{_on_eff:.2f}): {fa}[{ga:.2f}] + {fb}[{gb:.2f}]")

            if self._engine_seq_off:
                fc = _fname(self._voice_c.pcm)
                fd = _fname(self._voice_d.pcm)
                ot = _off_w
                gc = self._voice_c.gain * ot
                gd = self._voice_d.gain * ot
                parts.append(f"OFF(×{ot:.2f}): {fc}[{gc:.2f}] + {fd}[{gd:.2f}]")

            with self._fx_lock:
                n_fx = len(self._fx_oneshots)
            if n_fx:
                parts.append(f"FX:{n_fx}")

            return "  |  ".join(parts)

    # ------------------------------------------------------------------
    # Decode helper
    # ------------------------------------------------------------------
    def _decode_name_nonblocking(self, name: Optional[str]) -> Optional[np.ndarray]:
        """Cache hit → return pcm. Miss → enqueue for async decode, return None.
        The next trigger of the same sample will hit the warmed cache. Prevents
        state-thread stalls on cold FX decode (especially MP3 via pydub)."""
        if not name:
            return None
        if os.path.isabs(name):
            cached = self._pcm_cache.get(name)
            if cached is not None:
                return cached
        elif self._car_folder:
            for ext in (".wav", ".mp3"):
                fp = os.path.join(self._car_folder, name + ext)
                cached = self._pcm_cache.get(fp)
                if cached is not None:
                    return cached
        else:
            return None
        with self._fx_decode_lock:
            if name not in self._fx_decode_pending:
                self._fx_decode_pending.add(name)
                self._fx_decode_queue.append(name)
                if self._fx_decode_thread is None or not self._fx_decode_thread.is_alive():
                    self._fx_decode_stop = False
                    t = threading.Thread(
                        target=self._fx_decode_worker,
                        name="EngineFxDecoder",
                        daemon=True,
                    )
                    self._fx_decode_thread = t
                    t.start()
        self._fx_decode_event.set()
        return None

    def _fx_decode_worker(self) -> None:
        while not self._fx_decode_stop:
            with self._fx_decode_lock:
                name = self._fx_decode_queue.popleft() if self._fx_decode_queue else None
            if name is None:
                self._fx_decode_event.wait(timeout=1.0)
                self._fx_decode_event.clear()
                continue
            try:
                self._decode_name(name)
            except Exception:
                pass
            finally:
                with self._fx_decode_lock:
                    self._fx_decode_pending.discard(name)

    def _decode_name(self, name: Optional[str]) -> Optional[np.ndarray]:
        if not name: return None
        # Absolute path (electric / external sequences supply full paths)
        if os.path.isabs(name):
            if name in self._pcm_cache: return self._pcm_cache[name]
            if os.path.exists(name):
                try:
                    pcm = _load_pcm_mono(name)
                    self._pcm_cache[name] = pcm
                    return pcm
                except Exception as e:
                    self._log(f"Decode failed {name}: {e}")
            return None
        if not self._car_folder: return None
        for ext in (".wav", ".mp3"):
            fp = os.path.join(self._car_folder, name + ext)
            if os.path.exists(fp):
                if fp in self._pcm_cache: return self._pcm_cache[fp]
                try:
                    pcm = _load_pcm_mono(fp)
                    self._pcm_cache[fp] = pcm
                    return pcm
                except Exception as e:
                    self._log(f"Decode failed {fp}: {e}")
        return None

    # ------------------------------------------------------------------
    # Hybrid / external sequence control
    # ------------------------------------------------------------------
    def set_engine_sequence(self, seq: List[Dict]):
        """Replace the engine-on sequence (e.g. swap in electric loops).
        seq entries: {"file": "/abs/path/file.wav", "rpm": NNN}
        """
        with self._state_lock:
            self._engine_seq     = sorted(seq, key=lambda x: int(x.get("rpm", 0)))
            self._blend_lower_on = -1   # reset blend so voices reprime cleanly
            if not self._engine_seq:
                self._target_gain_a = 0.0
                self._target_gain_b = 0.0
                self._voice_a.gain = 0.0
                self._voice_b.gain = 0.0
            # Clear only the sequence PCM from the cache to free RAM
            for entry in seq:
                path = entry.get("file", "")
                self._pcm_cache.pop(path, None)

    def set_engine_off_sequence(self, seq: List[Dict]):
        """Replace the coast / engine-off sequence using absolute file paths."""
        with self._state_lock:
            self._engine_seq_off = sorted(seq, key=lambda x: int(x.get("rpm", 0)))
            self._blend_lower_off = -1
            self._coast_strategy = _classify_off_seq(self._engine_seq_off)
            if not self._engine_seq_off:
                self._target_gain_c = 0.0
                self._target_gain_d = 0.0
                self._voice_c.gain = 0.0
                self._voice_d.gain = 0.0
            for entry in seq:
                path = entry.get("file", "")
                self._pcm_cache.pop(path, None)

    def set_asset_overrides(self, overrides: Dict[str, List[str]]):
        with self._state_lock:
            self._pop_names = list(overrides.get("pop", []))
            self._flutter_names = list(overrides.get("flutter", []))
            self._gear_up_names = list(overrides.get("gear_up", []))
            self._gear_down_names = list(overrides.get("gear_down", []))
            self._turbo_names = list(overrides.get("turbo", []))
            self._transmission_names = list(overrides.get("transmission", []))
            self._engine_start_names = list(overrides.get("engine_start", []))
            self._gear_up = self._gear_up_names[0] if self._gear_up_names else None
            self._gear_down = self._gear_down_names[0] if self._gear_down_names else None
            self._turbo_name = self._turbo_names[0] if self._turbo_names else None
            self._transmission_name = self._transmission_names[0] if self._transmission_names else None
            self._engine_start_name = self._engine_start_names[0] if self._engine_start_names else None
            if not self._turbo_name:
                self._target_turbo_gain = 0.0
                self._turbo_voice.gain = 0.0
            if not self._transmission_name:
                self._target_transmission_gain = 0.0
                self._transmission_voice.gain = 0.0
            for paths in overrides.values():
                for path in paths:
                    self._pcm_cache.pop(path, None)


    def __del__(self):
        try: self.close()
        except Exception: pass
