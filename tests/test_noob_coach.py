from __future__ import annotations

import unittest

from Modules.Runtime.noob_coach import ALERTS, NoobCoach

BANDS = {g: {"down_orange": 1100, "up_orange": 3000} for g in range(1, 6)}
ALL_ON = {"enabled": True, "starting": True, "rpm": True, "speeding": True,
          "uphill": True, "limit": 90.0}


def step(coach, t, cfg=ALL_ON, rpm=2000, speed=50, gear=3, uphill=False):
    return coach.update(t, rpm=rpm, speed=speed, gear=gear, uphill=uphill, cfg=cfg)


class OffTests(unittest.TestCase):
    def test_master_off_raises_nothing(self):
        c = NoobCoach(BANDS)
        out = step(c, 0.0, cfg=dict(ALL_ON, enabled=False), rpm=6000, speed=200)
        self.assertEqual(set(out), set(ALERTS))
        self.assertFalse(any(out.values()))

    def test_each_toggle_gates_its_rule(self):
        c = NoobCoach(BANDS)
        out = step(c, 0.0, cfg=dict(ALL_ON, speeding=False), speed=150)
        self.assertFalse(out["noob_speeding"])


class StartingTests(unittest.TestCase):
    def test_low_revs_pulling_away_in_first(self):
        out = step(NoobCoach(BANDS), 0.0, rpm=800, speed=5, gear=1)
        self.assertTrue(out["noob_low_revs"])

    def test_no_low_revs_warning_in_higher_gear(self):
        out = step(NoobCoach(BANDS), 0.0, rpm=800, speed=5, gear=2)
        self.assertFalse(out["noob_low_revs"])

    def test_stall_after_moving_off_is_held_then_clears(self):
        c = NoobCoach(BANDS)
        step(c, 0.0, rpm=900, speed=4, gear=1)
        out = step(c, 0.5, rpm=0, speed=2, gear=1)
        self.assertTrue(out["noob_stalled"])
        self.assertTrue(step(c, 5.0, rpm=0, speed=0, gear=0)["noob_stalled"])
        self.assertFalse(step(c, 7.0, rpm=0, speed=0, gear=0)["noob_stalled"])

    def test_a_normal_engine_off_is_not_a_stall(self):
        c = NoobCoach(BANDS)
        step(c, 0.0, rpm=900, speed=0, gear=0)
        out = step(c, 3.0, rpm=0, speed=0, gear=0)   # well past the 1 s window
        self.assertFalse(out["noob_stalled"])


class RpmTests(unittest.TestCase):
    def test_shift_up_needs_to_be_held(self):
        c = NoobCoach(BANDS)
        self.assertFalse(step(c, 0.0, rpm=3500, gear=2)["noob_shift_up"])
        self.assertFalse(step(c, 1.0, rpm=3500, gear=2)["noob_shift_up"])
        self.assertTrue(step(c, 1.6, rpm=3500, gear=2)["noob_shift_up"])

    def test_no_shift_up_in_top_gear(self):
        c = NoobCoach(BANDS)
        step(c, 0.0, rpm=3500, gear=5)
        self.assertFalse(step(c, 2.0, rpm=3500, gear=5)["noob_shift_up"])

    def test_shift_down_when_lugging(self):
        c = NoobCoach(BANDS)
        step(c, 0.0, rpm=1000, speed=40, gear=4)
        self.assertTrue(step(c, 2.0, rpm=1000, speed=40, gear=4)["noob_shift_down"])

    def test_dropping_out_of_the_band_resets_the_timer(self):
        c = NoobCoach(BANDS)
        step(c, 0.0, rpm=3500, gear=2)
        step(c, 1.0, rpm=2500, gear=2)
        self.assertFalse(step(c, 1.8, rpm=3500, gear=2)["noob_shift_up"])


class SpeedingTests(unittest.TestCase):
    def test_hysteresis(self):
        c = NoobCoach(BANDS)
        self.assertTrue(step(c, 0.0, speed=95)["noob_speeding"])
        self.assertTrue(step(c, 1.0, speed=88)["noob_speeding"])    # within 3 km/h
        self.assertFalse(step(c, 2.0, speed=86)["noob_speeding"])

    def test_custom_limit(self):
        out = step(NoobCoach(BANDS), 0.0, cfg=dict(ALL_ON, limit=50.0), speed=55)
        self.assertTrue(out["noob_speeding"])


class UphillTests(unittest.TestCase):
    def test_uphill_with_low_revs(self):
        out = step(NoobCoach(BANDS), 0.0, rpm=1300, gear=3, uphill=True)
        self.assertTrue(out["noob_uphill"])

    def test_uphill_with_enough_revs(self):
        out = step(NoobCoach(BANDS), 0.0, rpm=2500, gear=3, uphill=True)
        self.assertFalse(out["noob_uphill"])

    def test_missing_gear_band_uses_defaults(self):
        out = step(NoobCoach({}), 0.0, rpm=1300, gear=3, uphill=True)
        self.assertTrue(out["noob_uphill"])


if __name__ == "__main__":
    unittest.main()
