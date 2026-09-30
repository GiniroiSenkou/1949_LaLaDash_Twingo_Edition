from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import os
import wave

import numpy as np


@dataclass(slots=True)
class AnalysisConfig:
    sample_rate: int = 22050
    frame_size: int = 2048
    hop_size: int = 1024
    min_bpm: int = 70
    max_bpm: int = 185


@dataclass(slots=True)
class TrackAnalysis:
    source_path: str
    source_mtime_ns: int
    sample_rate: int
    hop_size: int
    bpm: float
    beat_times: list[float]
    frames: list[dict[str, object]]

    def frame_at(self, time_s: float) -> dict[str, object]:
        if not self.frames:
            return {
                "time": max(0.0, float(time_s)),
                "beat": False,
                "kick": False,
                "snare": False,
                "hihat": False,
                "energy": 0.0,
                "low_energy": 0.0,
                "mid_energy": 0.0,
                "high_energy": 0.0,
                "drop": False,
                "breakdown": False,
            }
        frame_index = int(max(0, round((float(time_s) * self.sample_rate) / self.hop_size)))
        frame_index = min(frame_index, len(self.frames) - 1)
        return dict(self.frames[frame_index])

    def to_metadata(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_metadata(cls, payload: dict[str, object]) -> "TrackAnalysis":
        return cls(
            source_path=str(payload.get("source_path", "") or ""),
            source_mtime_ns=int(payload.get("source_mtime_ns", 0) or 0),
            sample_rate=int(payload.get("sample_rate", 0) or 0),
            hop_size=int(payload.get("hop_size", 0) or 0),
            bpm=float(payload.get("bpm", 0.0) or 0.0),
            beat_times=[float(v) for v in list(payload.get("beat_times", []) or [])],
            frames=[dict(item) for item in list(payload.get("frames", []) or [])],
        )


def _clamp01(values: np.ndarray) -> np.ndarray:
    return np.clip(values, 0.0, 1.0)


def _normalize_signal(samples: np.ndarray) -> np.ndarray:
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak <= 1e-9:
        return samples.astype(np.float32, copy=False)
    return (samples / peak).astype(np.float32, copy=False)


def _downsample(samples: np.ndarray, in_sr: int, out_sr: int) -> np.ndarray:
    if samples.size == 0 or in_sr <= 0 or out_sr <= 0 or in_sr == out_sr:
        return samples.astype(np.float32, copy=False)
    duration = samples.shape[0] / float(in_sr)
    target_len = max(1, int(round(duration * out_sr)))
    src_x = np.linspace(0.0, duration, num=samples.shape[0], endpoint=False)
    dst_x = np.linspace(0.0, duration, num=target_len, endpoint=False)
    return np.interp(dst_x, src_x, samples).astype(np.float32, copy=False)


def _moving_average(values: np.ndarray, size: int) -> np.ndarray:
    if values.size == 0:
        return values
    size = max(1, int(size))
    if size == 1:
        return values
    kernel = np.ones(size, dtype=np.float32) / float(size)
    return np.convolve(values, kernel, mode="same")


def _load_with_pedalboard(audio_path: str) -> tuple[np.ndarray, int]:
    from pedalboard.io import ReadableAudioFile

    with ReadableAudioFile(audio_path) as audio_file:
        samples = audio_file.read(audio_file.frames)
        sample_rate = int(audio_file.samplerate)
    data = np.asarray(samples, dtype=np.float32)
    if data.ndim == 2 and data.shape[0] <= 8 and data.shape[1] > data.shape[0]:
        data = np.transpose(data)
    return data, sample_rate


def _load_wav(audio_path: str) -> tuple[np.ndarray, int]:
    with wave.open(audio_path, "rb") as wav_file:
        sample_rate = int(wav_file.getframerate())
        channels = int(wav_file.getnchannels())
        sample_width = int(wav_file.getsampwidth())
        frames = wav_file.readframes(wav_file.getnframes())

    if sample_width == 1:
        raw = np.frombuffer(frames, dtype=np.uint8).astype(np.float32)
        raw = (raw - 128.0) / 128.0
    elif sample_width == 2:
        raw = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    else:
        raise ValueError(f"Unsupported WAV sample width: {sample_width}")
    if channels > 1:
        raw = raw.reshape(-1, channels)
    return raw.astype(np.float32, copy=False), sample_rate


def load_audio_mono(audio_path: str, *, target_sample_rate: int = 22050) -> tuple[np.ndarray, int]:
    lower = str(audio_path or "").lower()
    samples: np.ndarray
    sample_rate: int
    try:
        samples, sample_rate = _load_with_pedalboard(audio_path)
    except Exception:
        if not lower.endswith(".wav"):
            raise
        samples, sample_rate = _load_wav(audio_path)

    if samples.ndim == 2:
        samples = np.mean(samples, axis=1, dtype=np.float32)
    samples = _normalize_signal(samples.astype(np.float32, copy=False))
    return _downsample(samples, sample_rate, target_sample_rate), int(target_sample_rate)


def _estimate_tempo(onset_envelope: np.ndarray, sample_rate: int, hop_size: int, cfg: AnalysisConfig) -> float:
    if onset_envelope.size < 4:
        return 0.0
    centered = onset_envelope - float(np.mean(onset_envelope))
    autocorr = np.correlate(centered, centered, mode="full")[centered.size - 1 :]
    min_lag = max(1, int(round(sample_rate * 60.0 / (cfg.max_bpm * hop_size))))
    max_lag = max(min_lag + 1, int(round(sample_rate * 60.0 / (cfg.min_bpm * hop_size))))
    window = autocorr[min_lag:max_lag]
    if window.size == 0:
        return 0.0
    lag = int(np.argmax(window)) + min_lag
    if lag <= 0:
        return 0.0
    return float(60.0 * sample_rate / float(hop_size * lag))


def analyze_audio_samples(
    samples: np.ndarray,
    sample_rate: int,
    *,
    source_path: str = "",
    source_mtime_ns: int = 0,
    config: AnalysisConfig | None = None,
) -> TrackAnalysis:
    cfg = config or AnalysisConfig(sample_rate=sample_rate)
    mono = np.asarray(samples, dtype=np.float32).reshape(-1)
    mono = _normalize_signal(mono)
    if sample_rate != cfg.sample_rate:
        mono = _downsample(mono, int(sample_rate), cfg.sample_rate)
        sample_rate = cfg.sample_rate

    frame_size = int(cfg.frame_size)
    hop_size = int(cfg.hop_size)
    if mono.size < frame_size:
        mono = np.pad(mono, (0, frame_size - mono.size))

    starts = np.arange(0, max(1, mono.size - frame_size + 1), hop_size, dtype=np.int32)
    if starts.size == 0 or int(starts[-1]) + frame_size < mono.size:
        starts = np.append(starts, max(0, mono.size - frame_size))

    window = np.hanning(frame_size).astype(np.float32)
    freqs = np.fft.rfftfreq(frame_size, d=1.0 / sample_rate)
    low_mask = (freqs >= 20.0) & (freqs < 150.0)
    mid_mask = (freqs >= 150.0) & (freqs < 2000.0)
    high_mask = freqs >= 2000.0

    rms = np.zeros(starts.size, dtype=np.float32)
    flux = np.zeros(starts.size, dtype=np.float32)
    low_energy = np.zeros(starts.size, dtype=np.float32)
    mid_energy = np.zeros(starts.size, dtype=np.float32)
    high_energy = np.zeros(starts.size, dtype=np.float32)
    previous_mag = None

    for index, start in enumerate(starts):
        frame = mono[int(start) : int(start) + frame_size]
        if frame.size < frame_size:
            frame = np.pad(frame, (0, frame_size - frame.size))
        rms[index] = float(np.sqrt(np.mean(np.square(frame), dtype=np.float32)))
        spectrum = np.fft.rfft(frame * window)
        magnitude = np.abs(spectrum).astype(np.float32)
        power = np.square(magnitude)
        low_energy[index] = float(np.sum(power[low_mask], dtype=np.float32))
        mid_energy[index] = float(np.sum(power[mid_mask], dtype=np.float32))
        high_energy[index] = float(np.sum(power[high_mask], dtype=np.float32))
        if previous_mag is not None:
            flux[index] = float(np.sum(np.maximum(0.0, magnitude - previous_mag), dtype=np.float32))
        previous_mag = magnitude

    def _norm(values: np.ndarray) -> np.ndarray:
        peak = float(np.max(values)) if values.size else 0.0
        if peak <= 1e-9:
            return np.zeros_like(values)
        return values / peak

    rms_norm = _clamp01(_norm(rms))
    low_norm = _clamp01(_norm(low_energy))
    mid_norm = _clamp01(_norm(mid_energy))
    high_norm = _clamp01(_norm(high_energy))
    flux_norm = _clamp01(_norm(flux))
    rms_delta = np.maximum(0.0, np.diff(rms_norm, prepend=rms_norm[:1]))
    onset_envelope = _clamp01(0.65 * flux_norm + 0.35 * rms_delta)
    bpm = _estimate_tempo(onset_envelope, sample_rate, hop_size, cfg)

    short_window = max(2, int(round(0.25 * sample_rate / hop_size)))
    long_window = max(short_window + 1, int(round(1.2 * sample_rate / hop_size)))
    low_avg = _moving_average(low_norm, short_window)
    mid_avg = _moving_average(mid_norm, short_window)
    high_avg = _moving_average(high_norm, short_window)
    energy_avg = _moving_average(rms_norm, long_window)
    local_onset_avg = _moving_average(onset_envelope, short_window)

    beats: list[float] = []
    if bpm > 0.0:
        min_gap = max(1, int(round((60.0 / bpm) * sample_rate / hop_size * 0.45)))
    else:
        min_gap = max(1, short_window)

    last_beat_index = -min_gap
    frames: list[dict[str, object]] = []
    for index, start in enumerate(starts):
        time_s = float(start) / float(sample_rate)
        onset_peak = onset_envelope[index] >= max(0.32, local_onset_avg[index] * 1.08)
        local_peak = True
        if 0 < index < onset_envelope.size - 1:
            local_peak = onset_envelope[index] >= onset_envelope[index - 1] and onset_envelope[index] >= onset_envelope[index + 1]
        beat = bool(onset_peak and local_peak and (index - last_beat_index) >= min_gap)
        if beat:
            last_beat_index = index
            beats.append(round(time_s, 4))

        low_spike = bool(
            low_norm[index] >= max(0.28, low_avg[index] * 1.18)
            and (low_norm[index] - low_avg[index]) >= 0.08
        )
        mid_spike = bool(
            mid_norm[index] >= max(0.24, mid_avg[index] * 1.14)
            and (mid_norm[index] - mid_avg[index]) >= 0.06
        )
        high_spike = bool(
            high_norm[index] >= max(0.18, high_avg[index] * 1.12)
            and (high_norm[index] - high_avg[index]) >= 0.05
        )

        kick = bool(
            low_spike
            and (onset_peak or rms_delta[index] >= 0.06)
            and low_norm[index] >= mid_norm[index] * 0.75
        )
        snare = bool(
            mid_spike
            and (onset_peak or rms_delta[index] >= 0.05)
            and mid_norm[index] >= low_norm[index] * 0.55
        )
        hihat = bool(
            high_spike
            and (onset_peak or flux_norm[index] >= 0.18)
            and high_norm[index] >= mid_norm[index] * 0.38
        )

        drop = bool(
            rms_norm[index] >= max(0.55, energy_avg[index] * 1.32)
            and rms_delta[index] >= 0.12
        )
        breakdown = bool(
            energy_avg[index] > 0.08
            and rms_norm[index] <= energy_avg[index] * 0.62
        )

        frames.append(
            {
                "time": round(time_s, 4),
                "beat": beat,
                "kick": kick,
                "snare": snare,
                "hihat": hihat,
                "energy": round(float(rms_norm[index]), 4),
                "low_energy": round(float(low_norm[index]), 4),
                "mid_energy": round(float(mid_norm[index]), 4),
                "high_energy": round(float(high_norm[index]), 4),
                "drop": drop,
                "breakdown": breakdown,
            }
        )

    return TrackAnalysis(
        source_path=str(source_path or ""),
        source_mtime_ns=int(source_mtime_ns or 0),
        sample_rate=int(sample_rate),
        hop_size=int(hop_size),
        bpm=round(float(bpm), 3),
        beat_times=beats,
        frames=frames,
    )


def analyze_audio_file(audio_path: str, config: AnalysisConfig | None = None) -> TrackAnalysis:
    cfg = config or AnalysisConfig()
    samples, sample_rate = load_audio_mono(audio_path, target_sample_rate=cfg.sample_rate)
    return analyze_audio_samples(
        samples,
        sample_rate,
        source_path=audio_path,
        source_mtime_ns=os.stat(audio_path).st_mtime_ns,
        config=cfg,
    )


def dump_metadata_json(analysis: TrackAnalysis, metadata_path: str) -> None:
    payload = analysis.to_metadata()
    os.makedirs(os.path.dirname(metadata_path), exist_ok=True)
    with open(metadata_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, separators=(",", ":"))


def load_metadata_json(metadata_path: str) -> TrackAnalysis:
    with open(metadata_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return TrackAnalysis.from_metadata(payload)
