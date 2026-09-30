from __future__ import annotations

import os
import tempfile
import threading
import unittest

from Modules.Modding.audio_engine import AudioEngine
from Modules.Obd.engine_test_window import (
    build_rename_plan,
    canonical_target_stem,
    infer_category_from_filename,
    infer_index_order_from_filename,
    infer_value_from_filename,
    rename_audio_files,
    suggest_engine_on_rpms,
)


class _DummyVoice:
    def __init__(self):
        self.gain = 1.0


class EngineTestHelperTests(unittest.TestCase):
    def test_canonical_target_stem_for_indexed_category(self):
        self.assertEqual(canonical_target_stem("turbo", 0), "turbo_1")
        self.assertEqual(canonical_target_stem("flutter", 2), "flutter_3")

    def test_canonical_target_stem_for_value_category(self):
        self.assertEqual(canonical_target_stem("engine_on", 0, "3500"), "engine_on_3500")
        self.assertEqual(canonical_target_stem("engine_off", 0, "3500"), "engine_off_3500")
        self.assertEqual(canonical_target_stem("engine_off", 0, ""), "engine_off_???")
        self.assertEqual(canonical_target_stem("coast", 0, ""), "engine_off_???")  # legacy

    def test_canonical_target_stem_for_single_file_category(self):
        self.assertEqual(canonical_target_stem("transmission", 0), "transmission")
        self.assertEqual(canonical_target_stem("engine_start", 0), "engine_start")

    def test_suggest_engine_on_rpms_geometric_monotonic(self):
        values = suggest_engine_on_rpms(800, 7000, 7)
        self.assertEqual(len(values), 7)
        self.assertEqual(values[0], 800)
        self.assertEqual(values[-1], 7000)
        self.assertTrue(all(values[i] < values[i + 1] for i in range(len(values) - 1)))

    def test_infer_value_from_filename_for_engine_on_and_engine_off(self):
        self.assertEqual(infer_value_from_filename("engine_on", "/tmp/engine_on_3250.wav"), "3250")
        self.assertEqual(infer_value_from_filename("engine_on", "/tmp/engine_on-3250.wav"), "3250")
        self.assertEqual(infer_value_from_filename("engine_on", "/tmp/engine_on 3250.wav"), "3250")
        self.assertEqual(infer_value_from_filename("engine_off", "/tmp/engine_off_3500.wav"), "3500")
        self.assertEqual(infer_value_from_filename("engine_off", "/tmp/coast_85.mp3"), "85")  # legacy
        self.assertEqual(infer_value_from_filename("engine_on", "/tmp/random_name.wav"), "")

    def test_infer_category_from_filename(self):
        self.assertEqual(infer_category_from_filename("/tmp/engine_on_3000.wav"), "engine_on")
        self.assertEqual(infer_category_from_filename("/tmp/engine_on-3000.wav"), "engine_on")
        self.assertEqual(infer_category_from_filename("/tmp/engine_off_3500.wav"), "engine_off")
        self.assertEqual(infer_category_from_filename("/tmp/coast_75.wav"), "engine_off")  # legacy routes to engine_off
        self.assertEqual(infer_category_from_filename("/tmp/turbo_2.mp3"), "turbo")
        self.assertEqual(infer_category_from_filename("/tmp/turbo-2.mp3"), "turbo")
        self.assertEqual(infer_category_from_filename("/tmp/pop_4.wav"), "pop")
        self.assertEqual(infer_category_from_filename("/tmp/flutter_1.wav"), "flutter")
        self.assertEqual(infer_category_from_filename("/tmp/engine_start_1.wav"), "engine_start")
        self.assertEqual(infer_category_from_filename("/tmp/transmission_1.wav"), "transmission")
        self.assertEqual(infer_category_from_filename("/tmp/some_random_name.wav"), "")

    def test_infer_index_order_from_filename(self):
        self.assertEqual(infer_index_order_from_filename("turbo", "/tmp/turbo_3.wav"), 3)
        self.assertEqual(infer_index_order_from_filename("turbo", "/tmp/turbo-4.wav"), 4)
        self.assertEqual(infer_index_order_from_filename("engine_start", "/tmp/engine_start 2.wav"), 2)
        self.assertEqual(infer_index_order_from_filename("engine_start", "/tmp/engine_start.wav"), 1)
        self.assertEqual(infer_index_order_from_filename("transmission", "/tmp/transmission.wav"), 1)
        self.assertIsNone(infer_index_order_from_filename("turbo", "/tmp/turbo.wav"))
        self.assertIsNone(infer_index_order_from_filename("engine_on", "/tmp/engine_on_3000.wav"))

    def test_build_rename_plan_rejects_missing_engine_on_value(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "random.wav")
            with open(src, "wb") as handle:
                handle.write(b"stub")
            with self.assertRaises(ValueError):
                build_rename_plan([{"category": "engine_on", "source_path": src, "value": ""}])

    def test_rename_audio_files_renames_originals_in_place(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "weird_name.wav")
            with open(src, "wb") as handle:
                handle.write(b"stub")

            plan = build_rename_plan([{"category": "turbo", "source_path": src, "value": ""}])
            renamed = rename_audio_files(plan)

            self.assertFalse(os.path.exists(src))
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "turbo_1.wav")))
            self.assertEqual(renamed[0]["source_path"], os.path.join(tmpdir, "turbo_1.wav"))


class AudioEngineRuntimeSetterTests(unittest.TestCase):
    def test_set_engine_off_sequence_clears_off_voices_when_empty(self):
        engine = object.__new__(AudioEngine)
        engine._state_lock = threading.RLock()
        engine._engine_seq_off = [{"file": "/tmp/a.wav", "rpm": 30}]
        engine._blend_lower_off = 2
        engine._target_gain_c = 1.0
        engine._target_gain_d = 1.0
        engine._voice_c = _DummyVoice()
        engine._voice_d = _DummyVoice()
        engine._pcm_cache = {}

        AudioEngine.set_engine_off_sequence(engine, [])

        self.assertEqual(engine._engine_seq_off, [])
        self.assertEqual(engine._blend_lower_off, -1)
        self.assertEqual(engine._target_gain_c, 0.0)
        self.assertEqual(engine._target_gain_d, 0.0)
        self.assertEqual(engine._voice_c.gain, 0.0)
        self.assertEqual(engine._voice_d.gain, 0.0)

    def test_set_asset_overrides_sets_lists_and_primary_names(self):
        engine = object.__new__(AudioEngine)
        engine._state_lock = threading.RLock()
        engine._pcm_cache = {}
        engine._turbo_voice = _DummyVoice()
        engine._transmission_voice = _DummyVoice()
        engine._target_turbo_gain = 1.0
        engine._target_transmission_gain = 1.0

        AudioEngine.set_asset_overrides(
            engine,
            {
                "turbo": ["/tmp/turbo_a.wav", "/tmp/turbo_b.wav"],
                "transmission": ["/tmp/transmission.wav"],
                "engine_start": ["/tmp/start.wav"],
                "gear_up": ["/tmp/up.wav"],
                "gear_down": ["/tmp/down.wav"],
                "pop": ["/tmp/pop.wav"],
                "flutter": ["/tmp/flutter.wav"],
            },
        )

        self.assertEqual(engine._turbo_names, ["/tmp/turbo_a.wav", "/tmp/turbo_b.wav"])
        self.assertEqual(engine._turbo_name, "/tmp/turbo_a.wav")
        self.assertEqual(engine._transmission_name, "/tmp/transmission.wav")
        self.assertEqual(engine._engine_start_name, "/tmp/start.wav")
        self.assertEqual(engine._gear_up, "/tmp/up.wav")
        self.assertEqual(engine._gear_down, "/tmp/down.wav")
        self.assertEqual(engine._pop_names, ["/tmp/pop.wav"])
        self.assertEqual(engine._flutter_names, ["/tmp/flutter.wav"])


if __name__ == "__main__":
    unittest.main()
