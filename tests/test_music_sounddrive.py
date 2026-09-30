from __future__ import annotations

import configparser
import math
import os
import random
import tempfile
import unittest
from unittest import mock

import numpy as np

from Modules.Music.queue_utils import build_queue, dedupe_tracks, playlist_signature
from Modules.Music.realtime_analysis import AnalysisConfig, analyze_audio_samples
from Modules.Music.realtime_sounddrive import RealtimeSoundDrive
from Modules.Music.sounddrive_config import apply_normalized_music_settings, sounddrive_player_toggle_visible
from music import MusicPlayer


class SoundDriveConfigTests(unittest.TestCase):
    def test_normalizes_legacy_music_keys_to_canonical_values(self):
        cfg = configparser.ConfigParser()
        cfg["Music"] = {
            "sounddrive_enabled": "true",
            "sounddrive_method": "new_method",
            "performance": "full",
            "metadata_cache": "yes",
            "mode": "B",
            "auto_mix": "1",
        }

        values = apply_normalized_music_settings(cfg)

        self.assertTrue(values["sound_drive"])
        self.assertEqual(values["sound_drive_method"], "realtime")
        self.assertEqual(values["sound_drive_performance"], "full_power")
        self.assertTrue(values["sound_drive_metadata_cache"])
        self.assertEqual(values["mix_mode"], "Mode B")
        self.assertEqual(sorted(cfg["Music"].keys()), sorted(
            [
                "sound_drive",
                "auto_mix",
                "mix_mode",
                "sound_drive_method",
                "sound_drive_performance",
                "sound_drive_metadata_cache",
                "sound_drive_player_active",
            ]
        ))

    def test_player_toggle_visibility_requires_global_enable(self):
        self.assertFalse(
            sounddrive_player_toggle_visible(sound_drive_enabled=False, current_track_key="track.mp3")
        )
        self.assertFalse(
            sounddrive_player_toggle_visible(sound_drive_enabled=True, current_track_key="")
        )
        self.assertTrue(
            sounddrive_player_toggle_visible(sound_drive_enabled=True, current_track_key="track.mp3")
        )


class AlbumArtTests(unittest.TestCase):
    def test_reuses_existing_art_cache_when_embedded_art_loader_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = os.path.join(tmpdir, "song.mp3")
            with open(audio_path, "wb") as handle:
                handle.write(b"fake-audio")

            art_dir = os.path.join(tmpdir, "Data", "art_cache")
            os.makedirs(art_dir, exist_ok=True)

            dummy = type("DummyPlayer", (), {})()
            dummy._art_cache = {}
            dummy._track_audio_path = lambda track: audio_path
            dummy._track_art_cache_key = lambda track: audio_path
            dummy._extract_embedded_art = lambda audio, cache_key: None
            dummy._art_cache_digest = lambda audio, cache_key: MusicPlayer._art_cache_digest(dummy, audio, cache_key)
            dummy._art_cache_dir = lambda: art_dir
            dummy._existing_cached_art_path = lambda audio, cache_key: MusicPlayer._existing_cached_art_path(dummy, audio, cache_key)

            digest = MusicPlayer._art_cache_digest(dummy, audio_path, audio_path)
            cached_art = os.path.join(art_dir, f"{digest}.jpg")
            with open(cached_art, "wb") as handle:
                handle.write(b"fake-image")

            with mock.patch("music.MUSIC_FOLDER", tmpdir), mock.patch("music.BASE_DIR", tmpdir):
                resolved = MusicPlayer.get_track_art_path(dummy, {"title": "song.mp3", "tags": []})

            self.assertEqual(resolved, cached_art)

    def test_stem_folder_resolution_handles_unicode_normalization(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stem_dir = os.path.join(tmpdir, "RÜFÜS DU SOL - Alive")
            os.makedirs(stem_dir, exist_ok=True)
            dummy = type("DummyPlayer", (), {})()
            dummy._normalize_stem_folder_key = MusicPlayer._normalize_stem_folder_key

            resolved = MusicPlayer._resolve_stem_folder(dummy, "RÜFÜS DU SOL - Alive.mp3", stems_base=tmpdir)

            self.assertEqual(resolved, stem_dir)


class QueueTests(unittest.TestCase):
    def test_queue_is_full_permutation_and_signature_stable(self):
        playlist = [
            {"title": "b.mp3"},
            {"title": "a.mp3"},
            {"title": "c.mp3"},
            {"title": "a.mp3"},
        ]

        deduped = dedupe_tracks(playlist)
        signature_a = playlist_signature(playlist)
        signature_b = playlist_signature(list(playlist))
        queue, queue_pos = build_queue(
            deduped,
            shuffle_enabled=True,
            current_track_key="a.mp3",
            rng=random.Random(7),
        )

        self.assertEqual(signature_a, signature_b)
        self.assertEqual(len(deduped), 3)
        self.assertEqual(sorted(queue), [0, 1, 2])
        self.assertEqual(deduped[queue[queue_pos]]["title"], "a.mp3")


class RealtimeAnalysisTests(unittest.TestCase):
    def test_realtime_analysis_detects_band_events(self):
        sample_rate = 22050
        duration_s = 2.0
        samples = np.zeros(int(sample_rate * duration_s), dtype=np.float32)

        def add_tone(start_s: float, freq: float, length_s: float, amplitude: float) -> None:
            start = int(start_s * sample_rate)
            length = int(length_s * sample_rate)
            t = np.arange(length, dtype=np.float32) / float(sample_rate)
            envelope = np.exp(-8.0 * t).astype(np.float32)
            wave = amplitude * np.sin(2.0 * math.pi * freq * t).astype(np.float32)
            end = min(samples.size, start + length)
            samples[start:end] += (wave[: end - start] * envelope[: end - start]).astype(np.float32)

        def add_noise(start_s: float, length_s: float, amplitude: float) -> None:
            start = int(start_s * sample_rate)
            length = int(length_s * sample_rate)
            rng = np.random.default_rng(int(start_s * 1000) + 3)
            noise = rng.standard_normal(length).astype(np.float32) * amplitude
            end = min(samples.size, start + length)
            samples[start:end] += noise[: end - start]

        for beat_time in (0.2, 0.6, 1.0, 1.4, 1.8):
            add_tone(beat_time, 80.0, 0.09, 1.0)
        for snare_time in (0.4, 1.2, 1.6):
            add_tone(snare_time, 420.0, 0.08, 0.9)
        for hat_time in (0.3, 0.5, 0.7, 0.9, 1.1, 1.3, 1.5, 1.7):
            add_noise(hat_time, 0.025, 0.45)

        analysis = analyze_audio_samples(
            samples,
            sample_rate,
            source_path="synthetic.wav",
            config=AnalysisConfig(sample_rate=22050, frame_size=1024, hop_size=256),
        )

        self.assertGreater(analysis.bpm, 70.0)
        self.assertTrue(any(frame["beat"] for frame in analysis.frames))
        self.assertTrue(any(frame["kick"] for frame in analysis.frames))
        self.assertTrue(any(frame["snare"] for frame in analysis.frames))
        self.assertTrue(any(frame["hihat"] for frame in analysis.frames))

    def test_realtime_sounddrive_builds_stable_telemetry_profile(self):
        drive = object.__new__(RealtimeSoundDrive)
        drive._prev_speed = 0.0
        drive._prev_rpm = 0.0
        telemetry = RealtimeSoundDrive._telemetry_features(
            drive,
            {
                "RPM": {"value": 2800.0},
                "Speed": {"value": 42.0},
                "Throttle Pos": {"value": 38.0},
            }
        )
        self.assertGreater(telemetry["drive"], 0.9)
        self.assertGreater(telemetry["bass"], 0.5)
        self.assertLess(telemetry["brake"], 0.05)

        drive._drive_state = 0.04
        drive._bass_state = 0.50
        drive._presence_state = 0.02
        drive._air_state = 0.0
        drive._energy_state = 0.10
        drive._kick_env = 0.0
        drive._snare_env = 0.0
        drive._hat_env = 0.0
        drive._drop_env = 0.0
        drive._rpm_state = 0.0
        drive._speed_state = 0.0
        drive._throttle_state = 0.0
        drive._brake_state = 0.0

        profile = RealtimeSoundDrive._telemetry_only_profile(drive, telemetry)
        self.assertGreater(profile["drive"], 0.10)
        self.assertGreater(profile["bass"], 0.22)
        self.assertGreaterEqual(profile["presence"], 0.0)

        idle = RealtimeSoundDrive._telemetry_features(
            drive,
            {
                "RPM": {"value": 0.0},
                "Speed": {"value": 0.0},
                "Throttle Pos": {"value": 0.0},
            }
        )
        self.assertLess(idle["drive"], 0.05)
        self.assertGreaterEqual(idle["bass"], 0.10)

        drive._prev_speed = 36.0
        drive._prev_rpm = 2800.0
        braking = RealtimeSoundDrive._telemetry_features(
            drive,
            {
                "RPM": {"value": 1200.0},
                "Speed": {"value": 8.0},
                "Throttle Pos": {"value": 0.0},
            }
        )
        self.assertGreater(braking["brake"], 0.30)


if __name__ == "__main__":
    unittest.main()
