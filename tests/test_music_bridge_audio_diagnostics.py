from __future__ import annotations

import configparser
import os
import tempfile
import unittest
from unittest import mock

from Modules.Music.audio_router import AudioRouter
from Modules.Runtime import settings_cache
from bridges.music_bridge import MusicBridge
from bridges.obd_bridge import OBDBridge
from bridges.settings_bridge import SettingsBridge
from Modules.Obd.telemetry import FuelCalibrator, TelemetryHub


class DummyRouter:
    def __init__(self):
        self.delay = None
        self.invert = None
        self.ensured = []
        self.moved = 0
        self.torn_down = 0

    def list_devices(self):
        return [{"id": "sink.a", "name": "Speaker A", "displayName": "Speaker A"}]

    def set_user_delay(self, ms):
        self.delay = ms

    def set_delay_direction(self, invert):
        self.invert = invert

    def ensure_bus_mirror(self, sink):
        self.ensured.append(sink)
        return True

    def move_main_to_bus(self, media_name="GTR_MAIN"):
        self.moved += 1
        return True

    def teardown_bus(self):
        self.torn_down += 1


class DummyPlayer:
    def __init__(self):
        self.router = DummyRouter()
        self.selected_device = None
        self.output_route = "Default"
        self._user_volume = 100

    def set_selected_device(self, device):
        self.selected_device = device

    def set_output_route(self, route):
        self.output_route = route


class MusicBridgeSplitAudioTests(unittest.TestCase):
    def test_split_audio_persists_and_applies_immediately(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings_path = os.path.join(tmpdir, "settings.ini")
            cfg = configparser.ConfigParser()
            cfg["audio"] = {"device_id": "sink.a", "delay_ms": "1750", "invert_delay": "true"}
            with open(settings_path, "w", encoding="utf-8") as handle:
                cfg.write(handle)
            settings_cache.invalidate(settings_path)

            bridge = MusicBridge()
            player = DummyPlayer()
            bridge._player = player
            bridge._router = player.router

            with mock.patch("bridges.music_bridge.DATA_SETTINGS", settings_path):
                bridge._load_split_audio_state()
                bridge.toggleSplitAudio(True)

                self.assertTrue(bridge.splitAudioEnabled)
                self.assertTrue(bridge.splitAudioActive)
                self.assertEqual(player.router.delay, 1750)
                self.assertTrue(player.router.invert)
                self.assertEqual(player.router.ensured[-1], "sink.a")
                self.assertGreaterEqual(player.router.moved, 1)
                self.assertEqual(player.output_route, "Mirror to Device")
                self.assertEqual(player.selected_device["id"], "sink.a")

    def test_split_audio_device_change_reapplies_active_bus(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings_path = os.path.join(tmpdir, "settings.ini")
            cfg = configparser.ConfigParser()
            cfg["audio"] = {"split_enabled": "1", "device_id": "sink.a", "delay_ms": "250"}
            with open(settings_path, "w", encoding="utf-8") as handle:
                cfg.write(handle)
            settings_cache.invalidate(settings_path)

            bridge = MusicBridge()
            player = DummyPlayer()
            bridge._player = player
            bridge._router = player.router
            bridge._split_device_list = [
                {"id": "sink.b", "name": "Speaker B", "displayName": "Speaker B"}
            ]

            with mock.patch("bridges.music_bridge.DATA_SETTINGS", settings_path):
                bridge.setSplitAudioDevice("sink.b")

                self.assertEqual(player.router.ensured[-1], "sink.b")
                self.assertEqual(player.selected_device["displayName"], "Speaker B")

    def test_split_audio_volume_targets_selected_sink(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings_path = os.path.join(tmpdir, "settings.ini")
            cfg = configparser.ConfigParser()
            cfg["audio"] = {"split_enabled": "1", "device_id": "sink.a"}
            with open(settings_path, "w", encoding="utf-8") as handle:
                cfg.write(handle)
            settings_cache.invalidate(settings_path)

            bridge = MusicBridge()
            with mock.patch("bridges.music_bridge.DATA_SETTINGS", settings_path), \
                 mock.patch("Modules.Music.audio_router.set_device_volume", return_value=True) as set_device:
                bridge.setSplitAudioVolume(73)

            set_device.assert_called_with("sink.a", 73)


class AudioRouterDeviceNameTests(unittest.TestCase):
    def test_list_devices_uses_friendly_display_names(self):
        sinks_json = [
            {
                "index": 3,
                "name": "alsa_output.usb-Foo.analog-stereo",
                "properties": {"device.description": "USB DAC"},
            }
        ]
        short = "3\talsa_output.usb-Foo.analog-stereo\tPipeWire\trunning\n"

        with mock.patch("Modules.Music.audio_router._pactl_json", return_value=sinks_json), \
             mock.patch("Modules.Music.audio_router._run_cmd", return_value=(0, short, "")):
            devices = AudioRouter().list_devices()

        self.assertEqual(devices[0]["id"], "alsa_output.usb-Foo.analog-stereo")
        self.assertEqual(devices[0]["displayName"], "USB DAC")


class OBDBridgeDiagnosticTests(unittest.TestCase):
    def test_manual_pid_rejects_non_hex(self):
        with mock.patch.object(OBDBridge, "_refresh_state", lambda self: None):
            bridge = OBDBridge()
        self.assertEqual(bridge.runManualPid("01 ZZ"), "Manual PID: HEX only.")

    def test_can_mode_blocks_obd2_diagnostics(self):
        with mock.patch.object(OBDBridge, "_refresh_state", lambda self: None):
            bridge = OBDBridge()
        bridge._protocol = "can"
        self.assertIn("CAN-BUS mode", bridge.runDiagnostic("Read VIN"))


class TelemetryFormulaTests(unittest.TestCase):
    def test_fuel_range_formula_maps_raw_sender_endpoints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings_path = os.path.join(tmpdir, "settings.ini")
            cfg = configparser.ConfigParser()
            cfg["Calibration"] = {
                "fuel_formula": "range",
                "fuel_raw_empty": "8",
                "fuel_raw_full": "82",
                "fuel_offset": "0",
                "fuel_scale": "1",
                "fuel_reserve_percent": "0",
                "fuel_use_lut": "false",
            }
            with open(settings_path, "w", encoding="utf-8") as handle:
                cfg.write(handle)

            with mock.patch("Modules.Obd.telemetry.DATA_SETTINGS", settings_path):
                cal = FuelCalibrator()

            self.assertAlmostEqual(cal.apply(45), 50.0, places=1)

    def test_throttle_fallback_promotes_available_driver_demand(self):
        hub = object.__new__(TelemetryHub)
        hub.values = {
            "Throttle Pos": None,
            "Rel. Throttle": None,
            "Accel Pedal D": 42.0,
            "Absolute Load": 60.0,
        }
        hub.snapshot_cache = {"Throttle Pos": {"value": None, "text": "N/A"}}
        hub.value_scale = 1.0

        hub._apply_throttle_fallback_snapshot(engine_running=True)

        self.assertEqual(hub.snapshot_cache["Throttle Pos"]["value"], 42.0)
        self.assertEqual(hub.snapshot_cache["Throttle Pos"]["source"], "Accel Pedal D")
        self.assertTrue(hub.snapshot_cache["Throttle Pos"]["fallback"])


class MprisRestartTests(unittest.TestCase):
    def test_restart_targets_application_bridge_reference(self):
        import inspect

        source = inspect.getsource(SettingsBridge.restartMprisBridge)
        self.assertIn('"_mpris"', source)
        self.assertNotIn("player._mpris", source)


if __name__ == "__main__":
    unittest.main()
