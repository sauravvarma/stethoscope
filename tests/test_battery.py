"""Unit tests for the battery scope (scopes/battery.py)."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scopes"))
import core     # noqa: E402
import battery  # noqa: E402


def _ru(user=0, system=0, idle=0, intr=0, start=1):
    return core.RUsage(read=0, write=0, user_time=user, system_time=system,
                       idle_wkups=idle, interrupt_wkups=intr,
                       footprint=0, resident=0, start=start)


IOREG_SAMPLE = (
    '      "CurrentCapacity" = 62\n'
    '      "MaxCapacity" = 100\n'
    '      "DesignCapacity" = 4382\n'
    '      "AppleRawMaxCapacity" = 3555\n'
    '      "CycleCount" = 371\n'
    '      "IsCharging" = No\n'
    '      "ExternalConnected" = No\n'
    '      "Temperature" = 3095\n'
    '      "PermanentFailureStatus" = 0\n'
    '      "BatteryInstalled" = Yes\n'
    '      "Serial" = "ABC123"\n'
    '      "BatteryData" = {"CycleCount"=999,"DesignCapacity"=1}\n'   # must be ignored
)


class TestIoregParse(unittest.TestCase):
    def test_scalar_whitelist_only(self):
        with mock.patch.object(battery.subprocess, "run",
                               return_value=mock.Mock(stdout=IOREG_SAMPLE)):
            d = battery._ioreg_battery()
        self.assertEqual(d["CycleCount"], 371)          # not the 999 inside BatteryData
        self.assertEqual(d["DesignCapacity"], 4382)     # not the 1 inside BatteryData
        self.assertIs(d["IsCharging"], False)
        self.assertEqual(d["Serial"], "ABC123")
        self.assertNotIn("BatteryData", d)


class TestPmset(unittest.TestCase):
    def test_discharging(self):
        text = " -InternalBattery-0 (id=1)\t62%; discharging; 2:31 remaining present: true"
        with mock.patch.object(battery.subprocess, "run",
                               return_value=mock.Mock(stdout=text)):
            pct, state, trem = battery._pmset_batt()
        self.assertEqual((pct, state, trem), (62, "discharging", "2:31"))

    def test_charged_no_time(self):
        text = " -InternalBattery-0 (id=1)\t100%; charged; 0:00 remaining present: true"
        with mock.patch.object(battery.subprocess, "run",
                               return_value=mock.Mock(stdout=text)):
            pct, state, _ = battery._pmset_batt()
        self.assertEqual((pct, state), (100, "charged"))


class TestBatteryHealth(unittest.TestCase):
    def test_health_and_condition(self):
        with mock.patch.object(battery, "_ioreg_battery", return_value={
                "BatteryInstalled": True, "CurrentCapacity": 62, "DesignCapacity": 4382,
                "AppleRawMaxCapacity": 3555, "CycleCount": 371,
                "PermanentFailureStatus": 0, "Temperature": 3095,
                "IsCharging": False, "ExternalConnected": False}), \
                mock.patch.object(battery, "_pmset_batt", return_value=(62, "discharging", "2:31")):
            h = battery.battery_health()
        self.assertTrue(h["present"])
        self.assertEqual(h["charge_pct"], 62)
        self.assertAlmostEqual(h["health_pct"], 81.1, places=1)
        self.assertEqual(h["condition"], "Normal")
        self.assertAlmostEqual(h["temperature_c"], 30.9, places=1)

    def test_service_recommended_below_80(self):
        with mock.patch.object(battery, "_ioreg_battery", return_value={
                "BatteryInstalled": True, "DesignCapacity": 5000,
                "AppleRawMaxCapacity": 3500, "CycleCount": 900,
                "PermanentFailureStatus": 0}), \
                mock.patch.object(battery, "_pmset_batt", return_value=(50, "discharging", None)):
            h = battery.battery_health()
        self.assertLess(h["health_pct"], 80)
        self.assertEqual(h["condition"], "Service Recommended")

    def test_no_battery(self):
        with mock.patch.object(battery, "_ioreg_battery", return_value={}):
            self.assertEqual(battery.battery_health(), {"present": False})


class TestRankEnergy(unittest.TestCase):
    def setUp(self):
        self._n = mock.patch.object(battery.core, "proc_name", lambda p: "p%d" % p)
        self._n.start()

    def tearDown(self):
        self._n.stop()

    def test_score_combines_cpu_and_wakeups(self):
        # pid1: 100% CPU (500/500 ticks). pid2: 0% cpu but 1000 idle wakeups/s.
        prev = {1: _ru(), 2: _ru()}
        cur = {1: _ru(user=500), 2: _ru(idle=1000)}
        rows = battery.rank_energy(prev, cur, 0, 500, 1.0)
        by_pid = {r[4]: r for r in rows}
        self.assertAlmostEqual(by_pid[1][0], 100.0)                 # cpu only
        self.assertAlmostEqual(by_pid[2][0], battery.W_IDLE * 1000) # wakeups only

    def test_new_pid_skipped(self):
        self.assertEqual(battery.rank_energy({}, {9: _ru(user=99)}, 0, 500, 1.0), [])


class TestDrainers(unittest.TestCase):
    def setUp(self):
        self._n = mock.patch.object(battery.core, "proc_name", lambda p: "p%d" % p)
        self._n.start()

    def tearDown(self):
        self._n.stop()

    def test_diffs_against_baseline_and_skips_reused_pid(self):
        now = {1: _ru(user=24_000_000, idle=100, start=111),
               2: _ru(user=0, start=999)}          # pid2 start differs from baseline
        baseline = {"time": battery.time.time() - 60, "charge": 80,
                    "procs": {"1": [0, 0, 0, 111], "2": [0, 0, 0, 222]}}
        rows, meta = battery.drainers_since_unplug(now, 70, False, baseline)
        pids = [r[4] for r in rows]
        self.assertIn(1, pids)
        self.assertNotIn(2, pids)                   # reused pid excluded
        self.assertEqual(meta["charge_drop"], 10)   # 80 -> 70


if __name__ == "__main__":
    unittest.main()
