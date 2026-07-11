"""Hermetic battery-scope tests.

Every kernel/subprocess boundary this file touches — `ioreg`, `pmset`,
`powermetrics`, and libproc (via core.rusage) — is faked with
unittest.mock, so this suite runs on any macOS box without root or a real
battery. Nothing here re-derives core/validate.py's or test_core.py's own
proc_pid_rusage/timebase coverage; this file owns exactly the battery
scope's own surfaces: core/power.py's ioreg/pmset/pmenergy/powermetrics
parsing, scopes/battery.py's health math and null rendering, the top/
drainers data layers (rate vs cumulative units, identity safety), the
baseline file's atomic save/load/schema validation, the --json agent
contract, and the CLI's exit-code/flag contract.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from core import cli, power, schema
from scopes import battery

_HERE = os.path.dirname(os.path.abspath(__file__))
_BOOT_ID = "00000000-0000-0000-0000-000000000001"


def _fake_run(stdout=b"", stderr=b"", returncode=0):
    return mock.Mock(stdout=stdout, stderr=stderr, returncode=returncode)


# ---------------------------------------------------------------------------
# core.power — signed decode
# ---------------------------------------------------------------------------

class TestSigned64(unittest.TestCase):
    def test_ioreg_unsigned_rendering_decodes_negative(self):
        # The exact value ARCHITECTURE.md's finding S4 verified.
        self.assertEqual(power.signed64(18446744073709540666), -10950)

    def test_already_signed_passthrough(self):
        self.assertEqual(power.signed64(-1302), -1302)
        self.assertEqual(power.signed64(0), 0)
        self.assertEqual(power.signed64(3210), 3210)

    def test_boundaries(self):
        self.assertEqual(power.signed64((1 << 63) - 1), (1 << 63) - 1)
        self.assertEqual(power.signed64(1 << 63), -(1 << 63))
        self.assertEqual(power.signed64((1 << 64) - 1), -1)

    def test_malformed_or_out_of_range_values_are_unknown(self):
        self.assertIsNone(power.signed64("5"))
        self.assertIsNone(power.signed64(True))
        self.assertIsNone(power.signed64(1 << 64))


class TestBatteryFlowWatts(unittest.TestCase):
    def test_discharging_is_negative(self):
        watts = power.battery_flow_watts(11585, -1302)
        self.assertLess(watts, 0)
        self.assertAlmostEqual(watts, 11585 * -1302 / 1e6)

    def test_charging_is_positive(self):
        watts = power.battery_flow_watts(11585, 1302)
        self.assertGreater(watts, 0)

    def test_missing_inputs_are_none(self):
        self.assertIsNone(power.battery_flow_watts(None, -100))
        self.assertIsNone(power.battery_flow_watts(11585, None))

    def test_malformed_and_nonfinite_inputs_are_none(self):
        self.assertIsNone(power.battery_flow_watts("11585", -100))
        self.assertIsNone(power.battery_flow_watts(float("inf"), -100))
        self.assertIsNone(power.battery_flow_watts(10 ** 1000, 10 ** 1000))


# ---------------------------------------------------------------------------
# core.power — ioreg (plist parsing, not text regex)
# ---------------------------------------------------------------------------

def _ioreg_plist(nodes):
    import plistlib
    return plistlib.dumps(nodes)


class TestReadIoregBattery(unittest.TestCase):
    def test_present_keeps_only_scalar_top_level_fields(self):
        node = {
            "BatteryInstalled": True,
            "CurrentCapacity": 72,
            "MaxCapacity": 100,
            "Voltage": 11585,
            "InstantAmperage": -1302,
            # A nested blob (like the real BatteryData/PortControllerInfo
            # dicts) must never surface as a "scalar" field.
            "BatteryData": {"AdapterPower": 1},
            "PortControllerInfo": [{"x": 1}],
            "ManufacturerData": b"\x00\x01",
        }
        stdout = _ioreg_plist([node])
        with mock.patch.object(power.subprocess, "run",
                               return_value=_fake_run(stdout=stdout)):
            result = power.read_ioreg_battery()
        self.assertTrue(result.ok)
        self.assertTrue(result.present)
        self.assertIsNone(result.error)
        self.assertEqual(result.fields["CurrentCapacity"], 72)
        self.assertNotIn("BatteryData", result.fields)
        self.assertNotIn("PortControllerInfo", result.fields)
        self.assertNotIn("ManufacturerData", result.fields)

    def test_absent_battery_is_ok_not_error(self):
        stdout = _ioreg_plist([])
        with mock.patch.object(power.subprocess, "run",
                               return_value=_fake_run(stdout=stdout)):
            result = power.read_ioreg_battery()
        self.assertTrue(result.ok)
        self.assertFalse(result.present)
        self.assertIsNone(result.fields)
        self.assertIsNone(result.error)

    def test_command_missing_is_probe_failure_not_absent(self):
        with mock.patch.object(power.subprocess, "run",
                               side_effect=FileNotFoundError("no ioreg")):
            result = power.read_ioreg_battery()
        self.assertFalse(result.ok)
        self.assertIsNone(result.present)
        self.assertIsNone(result.fields)
        self.assertIn("ioreg_failed", result.error)

    def test_malformed_plist_is_probe_failure(self):
        with mock.patch.object(power.subprocess, "run",
                               return_value=_fake_run(stdout=b"not a plist")):
            result = power.read_ioreg_battery()
        self.assertFalse(result.ok)
        self.assertIsNone(result.present)
        self.assertIn("ioreg_parse_failed", result.error)

    def test_truncated_xml_is_probe_failure(self):
        with mock.patch.object(
                power.subprocess, "run",
                return_value=_fake_run(
                    stdout=b'<?xml version="1.0"?><plist><array>')):
            result = power.read_ioreg_battery()
        self.assertFalse(result.ok)
        self.assertIn("ioreg_parse_failed", result.error)

    def test_nonzero_exit_is_probe_failure(self):
        with mock.patch.object(power.subprocess, "run",
                               return_value=_fake_run(returncode=1)):
            result = power.read_ioreg_battery()
        self.assertFalse(result.ok)
        self.assertIn("ioreg_failed", result.error)


class TestReadPmsetBattery(unittest.TestCase):
    def test_discharging_with_estimate(self):
        out = (" -InternalBattery-0 (id=1)\t70%; discharging; 3:07 remaining "
              "present: true\n")
        with mock.patch.object(power.subprocess, "run",
                               return_value=_fake_run(stdout=out)):
            result = power.read_pmset_battery()
        self.assertTrue(result.ok)
        self.assertEqual(result.state, "discharging")
        self.assertEqual(result.time_remaining, "3:07")

    def test_no_estimate_time_remaining_is_none(self):
        out = " -InternalBattery-0 (id=1)\t100%; charging; (no estimate) present: true\n"
        with mock.patch.object(power.subprocess, "run",
                               return_value=_fake_run(stdout=out)):
            result = power.read_pmset_battery()
        self.assertTrue(result.ok)
        self.assertEqual(result.state, "charging")
        self.assertIsNone(result.time_remaining)

    def test_never_parses_a_charge_percentage(self):
        # pmset is only ever used for state/time — battery_health() gets
        # charge_pct from ioreg. PmsetBattery has no percentage field at all.
        self.assertNotIn("charge", power.PmsetBattery.__slots__)
        self.assertNotIn("pct", power.PmsetBattery.__slots__)

    def test_command_failure_is_soft(self):
        with mock.patch.object(power.subprocess, "run",
                               side_effect=OSError("no pmset")):
            result = power.read_pmset_battery()
        self.assertFalse(result.ok)
        self.assertIsNone(result.state)
        self.assertIsNone(result.time_remaining)


class TestLastPowerTransition(unittest.TestCase):
    def test_repeated_log_lines_collapse_to_latest_state_change(self):
        out = (
            "2026-07-10 10:00:00 +0000 Assertions Using AC(Charge: 80)\n"
            "2026-07-10 10:05:00 +0000 Assertions Using AC(Charge: 81)\n"
            "2026-07-10 11:00:00 +0000 Assertions Using Batt(Charge: 80)\n"
            "2026-07-10 11:05:00 +0000 Assertions Using Batt (Charge:79%)\n"
        )
        with mock.patch.object(
                power.subprocess, "run",
                return_value=_fake_run(stdout=out)):
            state, epoch, error = power.read_last_power_transition()
        self.assertIsNone(error)
        self.assertEqual(state, "battery")
        self.assertEqual(epoch, 1783681200.0)

    def test_unparseable_log_is_named(self):
        with mock.patch.object(
                power.subprocess, "run",
                return_value=_fake_run(stdout="no power records")):
            state, epoch, error = power.read_last_power_transition()
        self.assertIsNone(state)
        self.assertIsNone(epoch)
        self.assertEqual(error, "pmset_log_unparsed")


class TestPmenergyCoefficients(unittest.TestCase):
    def test_board_match_preferred_over_default(self):
        with mock.patch.object(power.os, "listdir",
                               return_value=["default.plist", "Mac-ABC123.plist"]), \
             mock.patch.object(power, "read_board_id",
                               return_value="Mac-ABC123"), \
             mock.patch("builtins.open", mock.mock_open(read_data=b"")), \
             mock.patch.object(power.plistlib, "load",
                               return_value={"energy_constants": {"kcpu_time": 1.0}}):
            coeffs, path, err = power.pmenergy_coefficients()
        self.assertIsNone(err)
        self.assertTrue(path.endswith("Mac-ABC123.plist"))
        self.assertEqual(coeffs["kcpu_time"], 1.0)

    def test_apple_silicon_falls_back_to_default(self):
        with mock.patch.object(power.os, "listdir",
                               return_value=["default.plist", "Mac-ABC123.plist"]), \
             mock.patch.object(power, "read_board_id", return_value=None), \
             mock.patch("builtins.open", mock.mock_open(read_data=b"")), \
             mock.patch.object(power.plistlib, "load",
                               return_value={"energy_constants": {"kcpu_time": 1.0}}):
            coeffs, path, err = power.pmenergy_coefficients()
        self.assertIsNone(err)
        self.assertTrue(path.endswith("default.plist"))

    def test_missing_dir_is_named_error(self):
        with mock.patch.object(power.os, "listdir", side_effect=OSError()):
            coeffs, path, err = power.pmenergy_coefficients()
        self.assertIsNone(coeffs)
        self.assertEqual(err, "pmenergy_dir_unavailable: ")

    def test_no_plist_matches_at_all(self):
        with mock.patch.object(power.os, "listdir", return_value=["Mac-XYZ.plist"]), \
             mock.patch.object(power, "read_board_id", return_value=None):
            coeffs, path, err = power.pmenergy_coefficients()
        self.assertIsNone(coeffs)
        self.assertEqual(err, "no_matching_plist")

    def test_nonfinite_coefficient_is_rejected(self):
        with mock.patch.object(
                power.os, "listdir", return_value=["default.plist"]), \
                mock.patch.object(power, "read_board_id", return_value=None), \
                mock.patch("builtins.open", mock.mock_open(read_data=b"")), \
                mock.patch.object(
                    power.plistlib, "load",
                    return_value={"energy_constants": {
                        "kcpu_time": float("nan"),
                    }}):
            coeffs, path, err = power.pmenergy_coefficients()
        self.assertIsNone(coeffs)
        self.assertIsNone(path)
        self.assertEqual(err, "invalid_energy_constants")

    def test_truncated_xml_is_named_parse_failure(self):
        from xml.parsers.expat import ExpatError
        with mock.patch.object(
                power.os, "listdir", return_value=["default.plist"]), \
                mock.patch.object(power, "read_board_id", return_value=None), \
                mock.patch("builtins.open", mock.mock_open(read_data=b"")), \
                mock.patch.object(
                    power.plistlib, "load",
                    side_effect=ExpatError("truncated")):
            coeffs, path, err = power.pmenergy_coefficients()
        self.assertIsNone(coeffs)
        self.assertIn("pmenergy_parse_failed", err)

    def test_board_id_probe_decodes_ioreg_bytes(self):
        import plistlib
        payload = plistlib.dumps([{"board-id": b"Mac-ABC123\0"}])
        with mock.patch.object(
                power.subprocess, "run",
                return_value=_fake_run(stdout=payload)):
            self.assertEqual(power.read_board_id(), "Mac-ABC123")

    def test_boot_session_uuid_probe(self):
        with mock.patch.object(
                power.subprocess, "run",
                return_value=_fake_run(stdout=_BOOT_ID.upper())):
            self.assertEqual(power.read_boot_session_uuid(), _BOOT_ID)


# ---------------------------------------------------------------------------
# core.power — powermetrics plist parsing (synthetic fixtures; see module
# docstring — no root available in this sandbox to capture a live sample)
# ---------------------------------------------------------------------------

class TestParsePowermetricsPlist(unittest.TestCase):
    def test_well_formed_tasks(self):
        import plistlib
        data = plistlib.dumps({
            "tasks": [
                {"pid": 100, "name": "hog", "energy_impact": 42.5},
                {"pid": 200, "name": "quiet", "energy_impact": 0.1},
            ]
        })
        tasks, err = power.parse_powermetrics_plist(data)
        self.assertIsNone(err)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["pid"], 100)
        self.assertEqual(tasks[0]["energy_impact_total"], 42.5)
        self.assertIsNone(tasks[0]["energy_impact_per_s"])

    def test_missing_energy_impact_is_null_not_zero(self):
        import plistlib
        data = plistlib.dumps({"tasks": [{"pid": 1, "name": "x"}]})
        tasks, err = power.parse_powermetrics_plist(data)
        self.assertIsNone(err)
        self.assertIsNone(tasks[0]["energy_impact_total"])
        self.assertIsNone(tasks[0]["energy_impact_per_s"])

    def test_rate_and_total_are_preserved_separately(self):
        import plistlib
        data = plistlib.dumps({"tasks": [{
            "pid": 1,
            "name": "x",
            "energy_impact": 10.0,
            "energy_impact_per_s": 2.0,
        }]})
        tasks, err = power.parse_powermetrics_plist(data)
        self.assertIsNone(err)
        self.assertEqual(tasks[0]["energy_impact_per_s"], 2.0)
        self.assertEqual(tasks[0]["energy_impact_total"], 10.0)

    def test_nul_framed_single_sample_is_accepted(self):
        import plistlib
        data = plistlib.dumps({
            "tasks": [{"pid": 0, "name": "kernel_task",
                       "energy_impact_per_s": 5.0}],
        }) + b"\0"
        tasks, err = power.parse_powermetrics_plist(data)
        self.assertIsNone(err)
        self.assertEqual(tasks[0]["pid"], 0)

    def test_dead_tasks_aggregate_pid_is_preserved(self):
        import plistlib
        data = plistlib.dumps({
            "tasks": [{"pid": -1, "name": "DEAD_TASKS",
                       "energy_impact_per_s": 3.0}],
        }) + b"\0"
        tasks, err = power.parse_powermetrics_plist(data)
        self.assertIsNone(err)
        self.assertEqual(tasks[0]["pid"], -1)

    def test_multiple_samples_are_rejected_for_single_sample_probe(self):
        import plistlib
        sample = plistlib.dumps({"tasks": []})
        tasks, err = power.parse_powermetrics_plist(
            sample + b"\0" + sample + b"\0")
        self.assertIsNone(tasks)
        self.assertEqual(err, "sample_count_2")

    def test_malformed_entries_are_skipped_not_fatal(self):
        import plistlib
        data = plistlib.dumps({"tasks": [
            {"pid": 1, "name": "ok", "energy_impact": 1.0},
            {"name": "no pid"},
            "not even a dict",
        ]})
        tasks, err = power.parse_powermetrics_plist(data)
        self.assertIsNone(err)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["pid"], 1)

    def test_not_a_plist_is_parse_failed(self):
        tasks, err = power.parse_powermetrics_plist(b"garbage")
        self.assertIsNone(tasks)
        self.assertIn("parse_failed", err)

    def test_truncated_xml_is_parse_failed(self):
        tasks, err = power.parse_powermetrics_plist(
            b'<?xml version="1.0"?><plist><dict>')
        self.assertIsNone(tasks)
        self.assertIn("parse_failed", err)

    def test_malformed_numeric_tasks_are_skipped(self):
        import plistlib
        data = plistlib.dumps({"tasks": [
            {"pid": "1", "name": "bad", "energy_impact": 1.0},
            {"pid": 2, "name": "bad", "energy_impact": "high"},
            {"pid": 3, "name": "bad", "energy_impact_per_s": float("nan")},
        ]})
        tasks, err = power.parse_powermetrics_plist(data)
        self.assertIsNone(err)
        self.assertEqual(tasks, [])

    def test_top_level_not_a_dict(self):
        import plistlib
        data = plistlib.dumps([1, 2, 3])
        tasks, err = power.parse_powermetrics_plist(data)
        self.assertIsNone(tasks)
        self.assertEqual(err, "not_a_dict")

    def test_no_tasks_field(self):
        import plistlib
        data = plistlib.dumps({"nope": []})
        tasks, err = power.parse_powermetrics_plist(data)
        self.assertIsNone(tasks)
        self.assertEqual(err, "no_tasks_field")


class TestReadPowermetricsTasks(unittest.TestCase):
    def test_permission_denied_detected(self):
        with mock.patch.object(power.subprocess, "run",
                               return_value=_fake_run(
                                   stderr=b"powermetrics must be invoked as the superuser\n",
                                   returncode=1)):
            tasks, err = power.read_powermetrics_tasks()
        self.assertIsNone(tasks)
        self.assertEqual(err, "root_required")

    def test_missing_binary(self):
        with mock.patch.object(power.subprocess, "run",
                               side_effect=FileNotFoundError()):
            tasks, err = power.read_powermetrics_tasks()
        self.assertEqual(err, "powermetrics_missing")

    def test_other_os_error_is_named_probe_failure(self):
        with mock.patch.object(
                power.subprocess, "run",
                side_effect=PermissionError("denied")):
            tasks, err = power.read_powermetrics_tasks()
        self.assertIsNone(tasks)
        self.assertIn("probe_failed", err)

    def test_timeout(self):
        import subprocess as _subprocess
        with mock.patch.object(power.subprocess, "run",
                               side_effect=_subprocess.TimeoutExpired(cmd="x", timeout=1)):
            tasks, err = power.read_powermetrics_tasks()
        self.assertEqual(err, "timeout")

    def test_success_delegates_to_parser(self):
        import plistlib
        stdout = plistlib.dumps({"tasks": [{"pid": 1, "name": "a", "energy_impact": 2.0}]})
        with mock.patch.object(power.subprocess, "run",
                               return_value=_fake_run(stdout=stdout, returncode=0)):
            tasks, err = power.read_powermetrics_tasks()
        self.assertIsNone(err)
        self.assertEqual(tasks[0]["name"], "a")


# ---------------------------------------------------------------------------
# scopes.battery — health math and null-safe rendering
# ---------------------------------------------------------------------------

def _ioreg_result(present=True, fields=None, ok=True, error=None):
    return power.IoregBattery(ok, present if ok else None, fields, error)


def _pmset_result(ok=True, state="discharging", time_remaining="3:00", error=None):
    return power.PmsetBattery(ok, state if ok else None,
                              time_remaining if ok else None, error)


class TestBatteryHealth(unittest.TestCase):
    def test_probe_failure_is_distinct_from_no_battery(self):
        with mock.patch.object(power, "read_ioreg_battery",
                               return_value=_ioreg_result(ok=False, error="ioreg_failed: boom")):
            h = battery.battery_health()
        self.assertIsNone(h["present"])
        self.assertEqual(h["probe_error"], "ioreg_failed: boom")
        for key in ("charge_pct", "state", "health_pct", "battery_flow_watts"):
            self.assertIsNone(h[key])

    def test_no_battery_present_false_no_error(self):
        with mock.patch.object(power, "read_ioreg_battery",
                               return_value=_ioreg_result(present=False, fields=None)):
            h = battery.battery_health()
        self.assertFalse(h["present"])
        self.assertIsNone(h["probe_error"])
        self.assertIsNone(h["charge_pct"])

    def test_health_math_and_condition_normal(self):
        fields = {
            "CurrentCapacity": 72, "MaxCapacity": 100, "DesignCapacity": 4382,
            "AppleRawMaxCapacity": 3900, "CycleCount": 200,
            "PermanentFailureStatus": 0, "Temperature": 3102,
            "Voltage": 11585, "InstantAmperage": -1302,
            "IsCharging": False, "ExternalConnected": False, "FullyCharged": False,
        }
        with mock.patch.object(power, "read_ioreg_battery",
                               return_value=_ioreg_result(fields=fields)), \
             mock.patch.object(power, "read_pmset_battery",
                               return_value=_pmset_result()):
            h = battery.battery_health()
        self.assertTrue(h["present"])
        self.assertEqual(h["charge_pct"], 72.0)
        self.assertAlmostEqual(h["health_pct"], round(3900 / 4382 * 100, 1))
        self.assertEqual(h["condition"], "Normal")
        self.assertEqual(h["current_ma"], -1302)
        self.assertLess(h["battery_flow_watts"], 0)
        self.assertEqual(h["capacities"], {"design_mah": 4382, "max_mah": 3900})
        self.assertAlmostEqual(h["temperature_c"], 31.0)

    def test_condition_service_recommended_below_80_pct(self):
        fields = {
            "CurrentCapacity": 50, "MaxCapacity": 100, "DesignCapacity": 4000,
            "AppleRawMaxCapacity": 3000, "PermanentFailureStatus": 0,
        }
        with mock.patch.object(power, "read_ioreg_battery",
                               return_value=_ioreg_result(fields=fields)), \
             mock.patch.object(power, "read_pmset_battery",
                               return_value=_pmset_result(ok=False)):
            h = battery.battery_health()
        self.assertEqual(h["condition"], "Service Recommended")
        self.assertIsNone(h["state"])   # pmset failed -> soft-degrade to None

    def test_unrounded_health_ratio_drives_service_threshold(self):
        fields = {
            "CurrentCapacity": 50,
            "MaxCapacity": 100,
            "DesignCapacity": 100000,
            "AppleRawMaxCapacity": 79960,
            "PermanentFailureStatus": 0,
        }
        with mock.patch.object(
                power, "read_ioreg_battery",
                return_value=_ioreg_result(fields=fields)), \
                mock.patch.object(
                    power, "read_pmset_battery",
                    return_value=_pmset_result(ok=False)):
            h = battery.battery_health()
        self.assertEqual(h["health_pct"], 80.0)
        self.assertEqual(h["condition"], "Service Recommended")

    def test_permanent_failure_forces_service_recommended(self):
        fields = {
            "CurrentCapacity": 90, "MaxCapacity": 100, "DesignCapacity": 4000,
            "AppleRawMaxCapacity": 3900, "PermanentFailureStatus": 5,
        }
        with mock.patch.object(power, "read_ioreg_battery",
                               return_value=_ioreg_result(fields=fields)), \
             mock.patch.object(power, "read_pmset_battery",
                               return_value=_pmset_result(ok=False)):
            h = battery.battery_health()
        self.assertEqual(h["condition"], "Service Recommended")

    def test_missing_design_capacity_yields_null_health_not_crash(self):
        fields = {"CurrentCapacity": 50, "MaxCapacity": 100}
        with mock.patch.object(power, "read_ioreg_battery",
                               return_value=_ioreg_result(fields=fields)), \
             mock.patch.object(power, "read_pmset_battery",
                               return_value=_pmset_result(ok=False)):
            h = battery.battery_health()
        self.assertIsNone(h["health_pct"])
        self.assertIsNone(h["condition"])

    def test_missing_max_capacity_does_not_treat_mah_as_percentage(self):
        fields = {"CurrentCapacity": 3210, "DesignCapacity": 4000}
        with mock.patch.object(
                power, "read_ioreg_battery",
                return_value=_ioreg_result(fields=fields)), \
                mock.patch.object(
                    power, "read_pmset_battery",
                    return_value=_pmset_result(ok=False, error="pmset_failed")):
            h = battery.battery_health()
        self.assertIsNone(h["charge_pct"])
        self.assertIsNone(h["condition"])

    def test_reported_good_condition_is_used_without_capacity(self):
        fields = {"BatteryHealth": "Good"}
        with mock.patch.object(
                power, "read_ioreg_battery",
                return_value=_ioreg_result(fields=fields)), \
                mock.patch.object(
                    power, "read_pmset_battery",
                    return_value=_pmset_result(ok=False)):
            h = battery.battery_health()
        self.assertEqual(h["condition"], "Normal")

    def test_reported_degraded_condition_overrides_good_capacity(self):
        fields = {
            "DesignCapacity": 4000,
            "AppleRawMaxCapacity": 3600,
            "BatteryHealth": "Poor",
        }
        with mock.patch.object(
                power, "read_ioreg_battery",
                return_value=_ioreg_result(fields=fields)), \
                mock.patch.object(
                    power, "read_pmset_battery",
                    return_value=_pmset_result(ok=False)):
            h = battery.battery_health()
        self.assertEqual(h["health_pct"], 90.0)
        self.assertEqual(h["condition"], "Service Recommended")

    def test_malformed_numeric_fields_stay_unknown(self):
        fields = {
            "CurrentCapacity": "50",
            "MaxCapacity": 100,
            "DesignCapacity": {},
            "Temperature": "warm",
            "Voltage": "high",
            "InstantAmperage": [],
        }
        with mock.patch.object(
                power, "read_ioreg_battery",
                return_value=_ioreg_result(fields=fields)), \
                mock.patch.object(
                    power, "read_pmset_battery",
                    return_value=_pmset_result(ok=False)):
            h = battery.battery_health()
        self.assertIsNone(h["charge_pct"])
        self.assertIsNone(h["health_pct"])
        self.assertIsNone(h["temperature_c"])
        self.assertIsNone(h["battery_flow_watts"])

    def test_pmset_failure_is_preserved(self):
        with mock.patch.object(
                power, "read_ioreg_battery",
                return_value=_ioreg_result(fields={
                    "CurrentCapacity": 50,
                    "MaxCapacity": 100,
                })), \
                mock.patch.object(
                    power, "read_pmset_battery",
                    return_value=_pmset_result(
                        ok=False, error="pmset_failed: denied")):
            h = battery.battery_health()
        self.assertEqual(h["pmset_error"], "pmset_failed: denied")


class TestHealthRenderingNeverFormatsNoneWithD(unittest.TestCase):
    def test_all_missing_fields_render_dash(self):
        h = battery._empty_health(True, None)
        stream = __import__("io").StringIO()
        with redirect_stdout(stream):
            battery._render_health_human(h)
        out = stream.getvalue()
        self.assertIn("-", out)
        self.assertNotIn("None", out)

    def test_probe_error_writes_to_stderr_not_stdout(self):
        h = battery._empty_health(None, "ioreg_failed: boom")
        stream = __import__("io").StringIO()
        with redirect_stderr(stream):
            battery._render_health_human(h)
        self.assertIn("ioreg_failed", stream.getvalue())

    def test_no_battery_message(self):
        h = battery._empty_health(False, None)
        stream = __import__("io").StringIO()
        with redirect_stdout(stream):
            battery._render_health_human(h)
        self.assertIn("no battery", stream.getvalue())


class TestCmdHealthExitCodes(unittest.TestCase):
    def _run(self, health):
        with mock.patch.object(battery, "battery_health", return_value=health):
            stream = __import__("io").StringIO()
            with redirect_stdout(stream):
                options = cli.parse_options(["--json"])
                rc = battery.cmd_health(options)
        return rc, stream.getvalue()

    def test_probe_error_is_exit_error(self):
        h = battery._empty_health(None, "ioreg_failed: boom")
        rc, _ = self._run(h)
        self.assertEqual(rc, cli.EXIT_ERROR)

    def test_service_recommended_is_exit_findings(self):
        h = battery._empty_health(True, None)
        h["condition"] = "Service Recommended"
        rc, _ = self._run(h)
        self.assertEqual(rc, cli.EXIT_FINDINGS)

    def test_normal_condition_is_exit_ok(self):
        h = battery._empty_health(True, None)
        h["condition"] = "Normal"
        rc, _ = self._run(h)
        self.assertEqual(rc, cli.EXIT_OK)

    def test_no_battery_is_exit_ok(self):
        h = battery._empty_health(False, None)
        rc, out = self._run(h)
        self.assertEqual(rc, cli.EXIT_OK)
        doc = json.loads(out)
        self.assertFalse(doc["present"])

    def test_pmset_failure_marks_document_partial(self):
        h = battery._empty_health(True, None)
        h["pmset_error"] = "pmset_failed"
        rc, out = self._run(h)
        self.assertEqual(rc, cli.EXIT_OK)
        doc = json.loads(out)
        self.assertTrue(doc["partial"])
        self.assertIn("pmset_unavailable", doc["partial_reasons"])

    def test_public_health_result_matches_cli_exit_semantics(self):
        health = battery._empty_health(True, None)
        health["condition"] = "Service Recommended"
        with mock.patch.object(
                battery, "battery_health", return_value=health):
            document, exit_code = battery.health_result()
        self.assertEqual(document["condition"], "Service Recommended")
        self.assertEqual(exit_code, cli.EXIT_FINDINGS)


# ---------------------------------------------------------------------------
# scopes.battery — top: rate-unit rows, V6 watts present/absent, identity
# ---------------------------------------------------------------------------

def _sample(cpu_user_ns=0, cpu_system_ns=0, energy_nj=None, pkg=0, intr=0,
            read=0, write=0, identity=(1, 100)):
    return {"identity": identity, "cpu_user_ns": cpu_user_ns,
            "cpu_system_ns": cpu_system_ns, "energy_nj": energy_nj,
            "pkg_idle_wakeups": pkg, "interrupt_wakeups": intr,
            "diskio_bytes_read": read, "diskio_bytes_written": write}


_COEFFS = {"kcpu_time": 1.0, "kcpu_wakeups": 0.0002,
          "kdiskio_bytesread": 4.5e-10, "kdiskio_byteswritten": 2.4e-10}


class TestEnergyScore(unittest.TestCase):
    def test_none_coefficients_yield_none_score(self):
        self.assertIsNone(battery._energy_score(None, 1.0, 10, 0, 0))

    def test_zero_activity_is_zero_not_none(self):
        self.assertEqual(battery._energy_score(_COEFFS, 0.0, 0, 0, 0), 0.0)

    def test_cpu_time_dominates_with_weight_one(self):
        score = battery._energy_score(_COEFFS, 2.0, 0, 0, 0)
        self.assertAlmostEqual(score, 2.0)

    def test_wakeups_scaled_by_small_coefficient(self):
        score = battery._energy_score(_COEFFS, 0.0, 1000, 0, 0)
        self.assertAlmostEqual(score, 0.2)

    def test_missing_or_nonfinite_coefficient_yields_none(self):
        self.assertIsNone(battery._energy_score(
            {"kcpu_time": 1.0}, 1.0, 0, 0, 0))
        bad = dict(_COEFFS, kcpu_time=float("inf"))
        self.assertIsNone(battery._energy_score(bad, 1.0, 0, 0, 0))

    def test_qos_weights_replace_aggregate_cpu_weight(self):
        coeffs = dict(_COEFFS, kqos_background=0.2)
        score = battery._energy_score(
            coeffs, 1.0, 0, 0, 0,
            qos_cpu_seconds={"background": 1.0})
        self.assertAlmostEqual(score, 0.2)


class TestDiffPower(unittest.TestCase):
    def test_watts_present_when_v6_available(self):
        prev = {(1, 100): _sample(energy_nj=0, identity=(1, 100))}
        cur = {(1, 100): _sample(energy_nj=2_000_000_000, identity=(1, 100))}
        rows, sys_totals = battery._diff_power(prev, cur, 1.0, _COEFFS)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0].energy_rate_watts, 2.0)
        self.assertIsNotNone(sys_totals.energy_rate_watts)

    def test_watts_absent_is_none_never_fabricated_zero(self):
        prev = {(1, 100): _sample(energy_nj=None, cpu_user_ns=0)}
        cur = {(1, 100): _sample(energy_nj=None, cpu_user_ns=500_000_000)}
        rows, _ = battery._diff_power(prev, cur, 1.0, _COEFFS)
        self.assertIsNone(rows[0].energy_rate_watts)
        self.assertIsNotNone(rows[0].energy_score_per_s)

    def test_missing_coefficients_score_is_none_not_zero(self):
        prev = {(1, 100): _sample(cpu_user_ns=0)}
        cur = {(1, 100): _sample(cpu_user_ns=500_000_000)}
        rows, sys_totals = battery._diff_power(prev, cur, 1.0, None)
        self.assertIsNone(rows[0].energy_score_per_s)
        self.assertIsNone(rows[0].energy_share_pct)
        self.assertIsNone(sys_totals.energy_score_per_s)

    def test_interrupt_wakeups_never_folded_into_score(self):
        # kcpu_wakeups applies to pkg-idle wakeups only; a huge interrupt
        # count must not move the score.
        prev = {(1, 100): _sample(intr=0)}
        cur = {(1, 100): _sample(intr=100_000)}
        rows, _ = battery._diff_power(prev, cur, 1.0, _COEFFS)
        self.assertEqual(rows[0].energy_score_per_s, 0.0)
        self.assertEqual(rows[0].interrupt_wakeups_per_s, 100_000.0)

    def test_cpu_score_is_normalized_by_interval(self):
        prev = {(1, 100): _sample(cpu_user_ns=0)}
        cur = {(1, 100): _sample(cpu_user_ns=2_000_000_000)}
        rows, _ = battery._diff_power(prev, cur, 2.0, _COEFFS)
        self.assertAlmostEqual(rows[0].cpu_pct, 100.0)
        self.assertAlmostEqual(rows[0].energy_score_per_s, 1.0)

    def test_disk_activity_remains_visible_without_coefficients(self):
        prev = {(1, 100): _sample(read=0)}
        cur = {(1, 100): _sample(read=1000)}
        rows, _ = battery._diff_power(prev, cur, 1.0, None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].diskio_read_bps, 1000)
        self.assertIsNone(rows[0].energy_score_per_s)

    def test_negative_deltas_clamp_to_zero(self):
        prev = {(1, 100): _sample(cpu_user_ns=500_000_000, pkg=500)}
        cur = {(1, 100): _sample(cpu_user_ns=400_000_000, pkg=400)}
        rows, _ = battery._diff_power(prev, cur, 1.0, _COEFFS)
        self.assertEqual(rows, [])   # no positive activity -> row dropped

    def test_new_or_reused_identity_baselines_to_self_for_rate(self):
        # (S10) a key absent from prev is baselined to itself for a *rate*
        # view (top): it must contribute zero this interval, unlike
        # drainers' cumulative "since baseline" semantics.
        prev = {}
        cur = {(9, 900): _sample(cpu_user_ns=1_000_000_000, identity=(9, 900))}
        rows, _ = battery._diff_power(prev, cur, 1.0, _COEFFS)
        self.assertEqual(rows, [])

    def test_energy_share_pct_sums_appropriately(self):
        prev = {(1, 100): _sample(cpu_user_ns=0, identity=(1, 100)),
               (2, 200): _sample(cpu_user_ns=0, identity=(2, 200))}
        cur = {(1, 100): _sample(cpu_user_ns=900_000_000, identity=(1, 100)),
              (2, 200): _sample(cpu_user_ns=100_000_000, identity=(2, 200))}
        rows, _ = battery.rank_top(prev, cur, 1.0, _COEFFS)
        self.assertAlmostEqual(rows[0].energy_share_pct, 90.0)
        self.assertAlmostEqual(rows[1].energy_share_pct, 10.0)


class TestRankTop(unittest.TestCase):
    def test_sorts_by_energy_score_descending(self):
        prev = {(1, 100): _sample(identity=(1, 100)),
               (2, 200): _sample(identity=(2, 200))}
        cur = {(1, 100): _sample(cpu_user_ns=100_000_000, identity=(1, 100)),
              (2, 200): _sample(cpu_user_ns=900_000_000, identity=(2, 200))}
        rows, _ = battery.rank_top(prev, cur, 1.0, _COEFFS)
        self.assertEqual([r.pid for r in rows], [2, 1])


class TestTopDocumentAndCLI(unittest.TestCase):
    def test_public_top_result_matches_ranked_document(self):
        previous = {(7, 70): _sample(identity=(7, 70))}
        current = {
            (7, 70): _sample(
                cpu_user_ns=100_000_000, identity=(7, 70)),
        }
        model = {
            "coefficients": _COEFFS,
            "source": "test-model",
            "error": None,
            "available": True,
        }
        with mock.patch.object(
                battery, "proc_name", return_value="worker"), \
                mock.patch.object(cli, "is_root", return_value=True):
            actual, exit_code = battery.top_result(
                previous, current, 1.0, 20, model=model)
            rows, totals = battery.rank_top(
                previous, current, 1.0, _COEFFS)
            expected = battery._top_document(
                rows, totals, 20, "test-model", ())
        self.assertEqual(actual, expected)
        self.assertEqual(exit_code, cli.EXIT_OK)

    def test_no_pmenergy_marks_partial(self):
        rows, sys_totals = [], battery.BatterySysTotals(0, None, None, 0, 0)
        with mock.patch.object(cli, "is_root", return_value=True):
            doc = battery._top_document(
                rows, sys_totals, 20, None, ("no_pmenergy_coefficients",))
        self.assertTrue(doc["partial"])
        self.assertIn("no_pmenergy_coefficients", doc["partial_reasons"])

    def test_root_and_coeffs_present_is_not_partial(self):
        rows, sys_totals = [], battery.BatterySysTotals(0, None, 0.0, 0, 0)
        with mock.patch.object(cli, "is_root", return_value=True):
            doc = battery._top_document(rows, sys_totals, 20, "path", ())
        self.assertFalse(doc["partial"])

    def test_not_root_marks_partial(self):
        rows, sys_totals = [], battery.BatterySysTotals(0, None, 0.0, 0, 0)
        with mock.patch.object(cli, "is_root", return_value=False):
            doc = battery._top_document(rows, sys_totals, 20, "path", ())
        self.assertIn("not_root", doc["partial_reasons"])

    def test_top_once_emits_exactly_one_document(self):
        snapshots = [
            {(7, 70): _sample(identity=(7, 70))},
            {(7, 70): _sample(cpu_user_ns=100_000_000, identity=(7, 70))},
        ]
        with mock.patch.object(battery, "snapshot_power", side_effect=snapshots), \
             mock.patch.object(battery.time, "sleep"), \
             mock.patch.object(battery.time, "monotonic", side_effect=[10.0, 11.0]), \
             mock.patch.object(cli, "is_root", return_value=True), \
             mock.patch.object(power, "pmenergy_coefficients",
                              return_value=(_COEFFS, "path", None)):
            options = cli.parse_options(["--json", "--once"])
            stream = __import__("io").StringIO()
            with redirect_stdout(stream):
                rc = battery.cmd_top(options)
        self.assertEqual(rc, cli.EXIT_OK)
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        doc = json.loads(lines[0])
        self.assertEqual(doc["command"], "top")

    def test_duration_repeats_until_elapsed(self):
        snapshots = [
            {(7, 70): _sample(identity=(7, 70))},
            {(7, 70): _sample(cpu_user_ns=100_000_000, identity=(7, 70))},
            {(7, 70): _sample(cpu_user_ns=200_000_000, identity=(7, 70))},
        ]
        with mock.patch.object(battery, "snapshot_power", side_effect=snapshots), \
             mock.patch.object(battery.time, "sleep") as sleep, \
             mock.patch.object(battery.time, "monotonic",
                              side_effect=[10.0, 10.5, 11.0]), \
             mock.patch.object(cli, "is_root", return_value=True), \
             mock.patch.object(power, "pmenergy_coefficients",
                              return_value=(_COEFFS, "path", None)):
            options = cli.parse_options(["--json", "--duration", "0.8",
                                        "--interval", "0.5"])
            stream = __import__("io").StringIO()
            with redirect_stdout(stream):
                rc = battery.cmd_top(options)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(len(stream.getvalue().splitlines()), 2)
        self.assertEqual(sleep.call_count, 2)

    def test_unstyled_frame_has_no_terminal_control_codes(self):
        frame = battery._top_frame([], battery.BatterySysTotals(0, None, 0, 0, 0),
                                   interval=1.0, limit=20, styled=False)
        self.assertNotIn("\033", frame)

    def test_process_name_control_characters_are_replaced(self):
        row = battery.BatteryTopRow(
            1, "evil\x1b[2J\nname", 1.0, None, 1.0, 100.0,
            0.0, 0.0, 0.0, 0.0)
        frame = battery._top_frame(
            [row], battery.BatterySysTotals(1, None, 1, 0, 0),
            interval=1.0, limit=20, styled=False)
        self.assertNotIn("\x1b", frame)
        self.assertNotIn("\nname", frame)


# ---------------------------------------------------------------------------
# scopes.battery — drainers: baseline persistence + validation + identity
# ---------------------------------------------------------------------------

class BaselineTestCase(unittest.TestCase):
    """Isolates the baseline file under a scratch dir inside tests/ (never
    the system temp dir) so no test touches the real ~/.stethoscope/.
    """

    def setUp(self):
        self.scratch = tempfile.mkdtemp(dir=_HERE, prefix=".battery_scratch-")
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        self.home_patch = mock.patch.object(
            battery, "_effective_home", return_value=self.scratch)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.transition_patch = mock.patch.object(
            power, "read_last_power_transition",
            return_value=("battery", 123.0, None))
        self.transition_patch.start()
        self.addCleanup(self.transition_patch.stop)
        self.boot_patch = mock.patch.object(
            power, "read_boot_session_uuid", return_value=_BOOT_ID)
        self.boot_patch.start()
        self.addCleanup(self.boot_patch.stop)

    def path(self):
        return os.path.join(battery._state_dir(), battery.BASELINE_FILENAME)


class TestBaselineSaveLoad(BaselineTestCase):
    def test_save_then_load_round_trips(self):
        snap = {(1, 100): _sample(cpu_user_ns=1_000_000_000, pkg=5,
                                  energy_nj=123, identity=(1, 100))}
        battery._save_baseline(self.path(), False, 55.0, snap)
        obj, reason = battery._load_baseline(self.path())
        self.assertIsNone(reason)
        self.assertEqual(obj["schema"], battery.BASELINE_SCHEMA)
        self.assertEqual(obj["on_ac"], False)
        self.assertEqual(obj["charge_pct"], 55.0)
        self.assertIn("sample_abstime", obj)
        self.assertEqual(obj["boot_session_uuid"], _BOOT_ID)
        self.assertIn("root", obj)
        self.assertEqual(obj["processes"]["1"]["cpu_ns"], 1_000_000_000)
        self.assertEqual(
            set(obj["processes"]["1"]["qos_cpu_ns"]),
            set(battery._QOS_CLASSES))
        self.assertEqual(obj["processes"]["1"]["start_ticks"], 100)

    def test_save_is_atomic_no_partial_file_left_behind(self):
        snap = {(1, 100): _sample(identity=(1, 100))}
        battery._save_baseline(self.path(), True, 100.0, snap)
        directory = os.path.dirname(self.path())
        leftovers = [f for f in os.listdir(directory) if f.startswith(".battery_baseline-")]
        self.assertEqual(leftovers, [])

    def test_no_baseline_file_is_named_reason(self):
        obj, reason = battery._load_baseline(self.path())
        self.assertIsNone(obj)
        self.assertEqual(reason, "no_baseline")

    def test_malformed_json_truncated(self):
        os.makedirs(battery._state_dir(), exist_ok=True)
        with open(self.path(), "w") as fh:
            fh.write('{"schema": "battery-baseline/1", "process')  # truncated
        obj, reason = battery._load_baseline(self.path())
        self.assertIsNone(obj)
        self.assertEqual(reason, "malformed_json")

    def test_valid_json_but_a_list_resets_explicitly(self):
        os.makedirs(battery._state_dir(), exist_ok=True)
        with open(self.path(), "w") as fh:
            json.dump([1, 2, 3], fh)
        obj, reason = battery._load_baseline(self.path())
        self.assertIsNone(obj)
        self.assertEqual(reason, "not_an_object")

    def test_schema_mismatch(self):
        os.makedirs(battery._state_dir(), exist_ok=True)
        with open(self.path(), "w") as fh:
            json.dump({"schema": "some-other/7", "on_ac": False,
                      "saved_at": 1.0, "processes": {}}, fh)
        obj, reason = battery._load_baseline(self.path())
        self.assertEqual(reason, "schema_mismatch")

    def test_invalid_processes_field(self):
        os.makedirs(battery._state_dir(), exist_ok=True)
        with open(self.path(), "w") as fh:
            json.dump({"schema": battery.BASELINE_SCHEMA, "on_ac": False,
                      "saved_at": 1.0, "sample_abstime": 1000,
                      "root": False, "boot_session_uuid": _BOOT_ID,
                      "charge_pct": 50.0,
                      "processes": "not a dict"}, fh)
        obj, reason = battery._load_baseline(self.path())
        self.assertEqual(reason, "invalid_processes_field")

    def test_invalid_process_entry_wrong_types(self):
        os.makedirs(battery._state_dir(), exist_ok=True)
        with open(self.path(), "w") as fh:
            json.dump({"schema": battery.BASELINE_SCHEMA, "on_ac": False,
                      "saved_at": 1.0, "sample_abstime": 1000,
                      "root": False, "boot_session_uuid": _BOOT_ID,
                      "charge_pct": 50.0,
                      "processes": {"1": {"start_ticks": "not an int"}}}, fh)
        obj, reason = battery._load_baseline(self.path())
        self.assertEqual(reason, "invalid_process_entry")

    def test_nonfinite_saved_at_is_rejected(self):
        os.makedirs(battery._state_dir(), exist_ok=True)
        with open(self.path(), "w") as fh:
            fh.write(
                '{"schema":"battery-baseline/1","saved_at":NaN,'
                '"sample_abstime":1000,"root":false,"on_ac":false,'
                '"charge_pct":50,"processes":{}}')
        obj, reason = battery._load_baseline(self.path())
        self.assertIsNone(obj)
        self.assertEqual(reason, "malformed_json")

    def test_invalid_utf8_is_named_read_failure(self):
        os.makedirs(battery._state_dir(), exist_ok=True)
        with open(self.path(), "wb") as fh:
            fh.write(b"\xff\xfe")
        obj, reason = battery._load_baseline(self.path())
        self.assertIsNone(obj)
        self.assertEqual(reason, "baseline_read_failed")

    def test_huge_saved_at_and_counters_are_rejected(self):
        payload = {
            "schema": battery.BASELINE_SCHEMA,
            "saved_at": 10 ** 1000,
            "sample_abstime": 1000,
            "boot_session_uuid": _BOOT_ID,
            "root": False,
            "on_ac": False,
            "unplugged_at": None,
            "charge_pct": 50.0,
            "processes": {},
        }
        self.assertEqual(
            battery._validate_baseline(payload), "invalid_saved_at")
        payload["saved_at"] = 1.0
        payload["processes"] = {
            "1": {
                "start_ticks": 10,
                "cpu_ns": 1 << 80,
                "pkg_idle_wakeups": 0,
                "diskio_bytes_read": 0,
                "diskio_bytes_written": 0,
                "energy_nj": None,
            },
        }
        self.assertEqual(
            battery._validate_baseline(payload), "invalid_process_entry")

    def test_invalid_charge_percentage_is_rejected(self):
        payload = {
            "schema": battery.BASELINE_SCHEMA,
            "saved_at": 1.0,
            "sample_abstime": 1000,
            "boot_session_uuid": _BOOT_ID,
            "root": False,
            "on_ac": False,
            "charge_pct": "bad",
            "processes": {},
        }
        self.assertEqual(
            battery._validate_baseline(payload), "invalid_charge_pct")

    def test_symlinked_state_directory_is_not_followed(self):
        target = tempfile.mkdtemp(dir=self.scratch, prefix="target-")
        self.addCleanup(shutil.rmtree, target, ignore_errors=True)
        os.symlink(target, battery._state_dir())
        obj, reason = battery._load_baseline(self.path())
        self.assertIsNone(obj)
        self.assertEqual(reason, "baseline_read_failed")

    def test_permission_read_failure_is_not_no_baseline(self):
        with mock.patch.object(
                battery, "_open_state_directory",
                side_effect=PermissionError("denied")):
            obj, reason = battery._load_baseline(self.path())
        self.assertIsNone(obj)
        self.assertEqual(reason, "baseline_read_failed")


def _baseline(processes=None, sample_abstime=100, root=False):
    return {
        "processes": processes or {},
        "sample_abstime": sample_abstime,
        "root": root,
    }


class TestRankDrainers(unittest.TestCase):
    def test_identity_safe_cumulative_delta(self):
        baseline_procs = {"1": {"start_ticks": 100, "cpu_ns": 1_000_000_000,
                                "pkg_idle_wakeups": 5, "diskio_bytes_read": 0,
                                "diskio_bytes_written": 0, "energy_nj": 10}}
        snap = {(1, 100): _sample(cpu_user_ns=3_000_000_000, pkg=8,
                                  energy_nj=1_000_000_010, identity=(1, 100))}
        rows = battery.rank_drainers(
            _baseline(baseline_procs, sample_abstime=200), snap, _COEFFS)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0].cpu_seconds_since, 2.0)
        self.assertEqual(rows[0].pkg_idle_wakeups_since, 3)
        self.assertAlmostEqual(rows[0].energy_joules_since, 1.0, places=3)

    def test_reused_pid_does_not_inherit_old_baseline(self):
        # baseline had pid 1 with a DIFFERENT start_ticks (old process,
        # long since exited); current pid 1 is a new process. The entire
        # current cumulative counter must count, not a negative/garbage
        # delta against the stale baseline.
        baseline_procs = {"1": {"start_ticks": 50, "cpu_ns": 9_000_000_000,
                                "pkg_idle_wakeups": 900, "diskio_bytes_read": 0,
                                "diskio_bytes_written": 0, "energy_nj": None}}
        snap = {(1, 999): _sample(cpu_user_ns=500_000_000, pkg=2, identity=(1, 999))}
        rows = battery.rank_drainers(
            _baseline(baseline_procs, sample_abstime=100), snap, _COEFFS)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0].cpu_seconds_since, 0.5)
        self.assertEqual(rows[0].pkg_idle_wakeups_since, 2)

    def test_process_absent_from_baseline_counts_in_full(self):
        # A process that started after the baseline was taken: not present
        # in baseline_procs at all. Must appear (never silently dropped —
        # the original PR's `if not b: continue` did exactly that).
        snap = {(5, 500): _sample(cpu_user_ns=200_000_000, identity=(5, 500))}
        rows = battery.rank_drainers(
            _baseline(sample_abstime=100), snap, _COEFFS)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0].cpu_seconds_since, 0.2)

    def test_energy_score_total_is_not_a_rate(self):
        # dimensionally distinct from energy_score_per_s: no division by an
        # elapsed interval anywhere in rank_drainers.
        baseline_procs = {}
        snap = {(1, 100): _sample(cpu_user_ns=10_000_000_000, identity=(1, 100))}
        rows = battery.rank_drainers(
            _baseline(baseline_procs, sample_abstime=50), snap, _COEFFS)
        self.assertAlmostEqual(rows[0].energy_score_total, 10.0)

    def test_sorted_descending_by_score(self):
        snap = {(1, 100): _sample(cpu_user_ns=100_000_000, identity=(1, 100)),
               (2, 200): _sample(cpu_user_ns=900_000_000, identity=(2, 200))}
        rows = battery.rank_drainers(
            _baseline(sample_abstime=50), snap, _COEFFS)
        self.assertEqual([r.pid for r in rows], [2, 1])

    def test_new_process_gets_full_v6_energy_since_baseline(self):
        snap = {(5, 500): _sample(
            cpu_user_ns=1, energy_nj=2_000_000_000,
            identity=(5, 500))}
        rows = battery.rank_drainers(
            _baseline(sample_abstime=100), snap, _COEFFS)
        self.assertAlmostEqual(rows[0].energy_joules_since, 2.0)

    def test_long_running_process_newly_visible_is_not_zero_baselined(self):
        snap = {(5, 50): _sample(
            cpu_user_ns=20_000_000_000, identity=(5, 50))}
        rows = battery.rank_drainers(
            _baseline(sample_abstime=100, root=False), snap, _COEFFS)
        self.assertEqual(rows, [])


class TestCmdDrainers(BaselineTestCase):
    def _health(self, present=True, external=False, charge=50.0, probe_error=None):
        h = battery._empty_health(present if probe_error is None else None, probe_error)
        if probe_error is None and present:
            h.update({"charge_pct": charge, "external_connected": external})
        return h

    def test_no_battery_emits_on_ac_null_and_ok(self):
        with mock.patch.object(battery, "battery_health",
                              return_value=self._health(present=False)):
            options = cli.parse_options(["--json"])
            stream = __import__("io").StringIO()
            with redirect_stdout(stream):
                rc = battery.cmd_drainers(options)
        self.assertEqual(rc, cli.EXIT_OK)
        doc = json.loads(stream.getvalue())
        self.assertFalse(doc["present"])
        self.assertIn("on_ac", doc)
        self.assertIsNone(doc["on_ac"])

    def test_probe_error_is_exit_error_and_documents_on_ac_null(self):
        with mock.patch.object(battery, "battery_health",
                              return_value=self._health(probe_error="ioreg_failed: x")):
            options = cli.parse_options(["--json"])
            stream = __import__("io").StringIO()
            with redirect_stdout(stream):
                rc = battery.cmd_drainers(options)
        self.assertEqual(rc, cli.EXIT_ERROR)
        doc = json.loads(stream.getvalue())
        self.assertIn("on_ac", doc)
        self.assertIsNone(doc["on_ac"])

    def test_unknown_ac_state_is_error_and_does_not_mutate_baseline(self):
        with mock.patch.object(
                battery, "battery_health",
                return_value=self._health(external=None)), \
                mock.patch.object(battery, "snapshot_power") as snapshot, \
                mock.patch.object(battery, "_save_baseline") as save:
            options = cli.parse_options(["--json"])
            stream = __import__("io").StringIO()
            with redirect_stdout(stream):
                rc = battery.cmd_drainers(options)
        self.assertEqual(rc, cli.EXIT_ERROR)
        self.assertFalse(snapshot.called)
        self.assertFalse(save.called)
        doc = json.loads(stream.getvalue())
        self.assertIsNone(doc["on_ac"])
        self.assertEqual(doc["error"], "power_state_unknown")

    def test_first_run_sets_baseline_and_always_emits_on_ac(self):
        with mock.patch.object(battery, "battery_health",
                              return_value=self._health(external=False)), \
             mock.patch.object(battery, "snapshot_power", return_value={}):
            options = cli.parse_options(["--json"])
            stream = __import__("io").StringIO()
            with redirect_stdout(stream):
                rc = battery.cmd_drainers(options)
        self.assertEqual(rc, cli.EXIT_OK)
        doc = json.loads(stream.getvalue())
        self.assertTrue(doc["baseline_reset"])
        self.assertEqual(doc["reset_reason"], "no_baseline")
        self.assertFalse(doc["on_ac"])
        self.assertIn("on_ac", doc)

    def test_on_ac_always_resets(self):
        with mock.patch.object(battery, "battery_health",
                              return_value=self._health(external=True)), \
             mock.patch.object(battery, "snapshot_power", return_value={}):
            options = cli.parse_options(["--json"])
            stream = __import__("io").StringIO()
            with redirect_stdout(stream):
                rc = battery.cmd_drainers(options)
        doc = json.loads(stream.getvalue())
        self.assertTrue(doc["baseline_reset"])
        self.assertEqual(doc["reset_reason"], "on_ac")
        self.assertTrue(doc["on_ac"])

    def test_unplug_transition_resets_once_then_diffs(self):
        with mock.patch.object(battery, "battery_health",
                              return_value=self._health(external=True)), \
             mock.patch.object(battery, "snapshot_power", return_value={}):
            options = cli.parse_options(["--json"])
            with redirect_stdout(__import__("io").StringIO()):
                battery.cmd_drainers(options)   # baseline saved while on_ac

        # Now unplugged: the stored baseline says on_ac=True -> "unplugged"
        # reset (the baseline captured at unplug), not a diff yet.
        with mock.patch.object(battery, "battery_health",
                              return_value=self._health(external=False)), \
             mock.patch.object(battery, "snapshot_power", return_value={}):
            options = cli.parse_options(["--json"])
            stream = __import__("io").StringIO()
            with redirect_stdout(stream):
                rc = battery.cmd_drainers(options)
        doc = json.loads(stream.getvalue())
        self.assertTrue(doc["baseline_reset"])
        self.assertEqual(doc["reset_reason"], "unplugged")
        self.assertFalse(doc["on_ac"])

        # Third call, still on battery: now diffs against the unplug baseline.
        with mock.patch.object(battery, "battery_health",
                              return_value=self._health(external=False)), \
             mock.patch.object(battery, "snapshot_power", return_value={}), \
             mock.patch.object(power, "pmenergy_coefficients",
                              return_value=(_COEFFS, "path", None)):
            options = cli.parse_options(["--json"])
            stream = __import__("io").StringIO()
            with redirect_stdout(stream):
                rc = battery.cmd_drainers(options)
        doc = json.loads(stream.getvalue())
        self.assertFalse(doc["baseline_reset"])
        self.assertEqual(rc, cli.EXIT_OK)

    def test_malformed_baseline_resets_with_machine_readable_reason(self):
        os.makedirs(battery._state_dir(), exist_ok=True)
        with open(self.path(), "w") as fh:
            fh.write("[1, 2")   # truncated list
        with mock.patch.object(battery, "battery_health",
                              return_value=self._health(external=False)), \
             mock.patch.object(battery, "snapshot_power", return_value={}):
            options = cli.parse_options(["--json"])
            stream = __import__("io").StringIO()
            with redirect_stdout(stream):
                rc = battery.cmd_drainers(options)
        self.assertEqual(rc, cli.EXIT_OK)
        doc = json.loads(stream.getvalue())
        self.assertTrue(doc["baseline_reset"])
        self.assertEqual(doc["reset_reason"], "malformed_json")

    def test_baseline_read_failure_is_exit_error_not_first_run(self):
        with mock.patch.object(
                battery, "battery_health",
                return_value=self._health(external=False)), \
                mock.patch.object(battery, "snapshot_power", return_value={}), \
                mock.patch.object(
                    battery, "_load_baseline",
                    return_value=(None, "baseline_read_failed")), \
                mock.patch.object(battery, "_save_baseline") as save:
            options = cli.parse_options(["--json"])
            stream = __import__("io").StringIO()
            with redirect_stdout(stream):
                rc = battery.cmd_drainers(options)
        self.assertEqual(rc, cli.EXIT_ERROR)
        self.assertFalse(save.called)
        self.assertEqual(
            json.loads(stream.getvalue())["error"],
            "baseline_read_failed")

    def test_baseline_write_failure_is_structured_exit_error(self):
        with mock.patch.object(
                battery, "battery_health",
                return_value=self._health(external=False)), \
                mock.patch.object(battery, "snapshot_power", return_value={}), \
                mock.patch.object(
                    battery, "_load_baseline",
                    return_value=(None, "no_baseline")), \
                mock.patch.object(
                    battery, "_save_baseline",
                    side_effect=PermissionError("denied")):
            options = cli.parse_options(["--json"])
            stream = __import__("io").StringIO()
            with redirect_stdout(stream):
                rc = battery.cmd_drainers(options)
        self.assertEqual(rc, cli.EXIT_ERROR)
        self.assertEqual(
            json.loads(stream.getvalue())["error"],
            "baseline_write_failed")

    def test_system_restart_resets_baseline(self):
        baseline = {
            "schema": battery.BASELINE_SCHEMA,
            "saved_at": 1.0,
            "sample_abstime": 1000,
            "boot_session_uuid": "previous-boot",
            "root": False,
            "on_ac": False,
            "unplugged_at": 123.0,
            "charge_pct": 60.0,
            "processes": {},
        }
        with mock.patch.object(
                battery, "battery_health",
                return_value=self._health(external=False)), \
                mock.patch.object(battery, "snapshot_power", return_value={}), \
                mock.patch.object(
                    battery, "_load_baseline",
                    return_value=(baseline, None)), \
                mock.patch.object(
                    battery.rusage, "mach_absolute_time",
                    return_value=500), \
                mock.patch.object(battery, "_save_baseline"):
            options = cli.parse_options(["--json"])
            stream = __import__("io").StringIO()
            with redirect_stdout(stream):
                rc = battery.cmd_drainers(options)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(
            json.loads(stream.getvalue())["reset_reason"],
            "system_restarted")

    def test_new_unplug_transition_resets_stale_battery_baseline(self):
        baseline = {
            "schema": battery.BASELINE_SCHEMA,
            "saved_at": 1.0,
            "sample_abstime": 100,
            "boot_session_uuid": _BOOT_ID,
            "root": False,
            "on_ac": False,
            "unplugged_at": 100.0,
            "charge_pct": 60.0,
            "processes": {},
        }
        with mock.patch.object(
                battery, "battery_health",
                return_value=self._health(external=False)), \
                mock.patch.object(
                    power, "read_last_power_transition",
                    return_value=("battery", 200.0, None)), \
                mock.patch.object(battery, "snapshot_power", return_value={}), \
                mock.patch.object(
                    battery, "_load_baseline",
                    return_value=(baseline, None)), \
                mock.patch.object(
                    battery.rusage, "mach_absolute_time",
                    return_value=300), \
                mock.patch.object(battery, "_save_baseline") as save:
            options = cli.parse_options(["--json"])
            stream = __import__("io").StringIO()
            with redirect_stdout(stream):
                rc = battery.cmd_drainers(options)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertTrue(save.called)
        self.assertEqual(
            json.loads(stream.getvalue())["reset_reason"],
            "power_session_changed")

    def test_missing_pmenergy_marks_drainers_partial(self):
        baseline = {
            "schema": battery.BASELINE_SCHEMA,
            "saved_at": 1.0,
            "sample_abstime": 100,
            "boot_session_uuid": _BOOT_ID,
            "root": False,
            "on_ac": False,
            "unplugged_at": 123.0,
            "charge_pct": 60.0,
            "processes": {},
        }
        with mock.patch.object(
                battery, "battery_health",
                return_value=self._health(external=False)), \
                mock.patch.object(battery, "snapshot_power", return_value={}), \
                mock.patch.object(
                    battery, "_load_baseline",
                    return_value=(baseline, None)), \
                mock.patch.object(
                    battery.rusage, "mach_absolute_time",
                    return_value=200), \
                mock.patch.object(
                    power, "pmenergy_coefficients",
                    return_value=(None, None, "unavailable")), \
                mock.patch.object(cli, "is_root", return_value=True):
            options = cli.parse_options(["--json"])
            stream = __import__("io").StringIO()
            with redirect_stdout(stream):
                rc = battery.cmd_drainers(options)
        self.assertEqual(rc, cli.EXIT_OK)
        doc = json.loads(stream.getvalue())
        self.assertTrue(doc["partial"])
        self.assertIn(
            "no_pmenergy_coefficients", doc["partial_reasons"])

    def test_root_query_marks_nonroot_baseline_visibility_partial(self):
        baseline = {
            "schema": battery.BASELINE_SCHEMA,
            "saved_at": 1.0,
            "sample_abstime": 100,
            "boot_session_uuid": _BOOT_ID,
            "root": False,
            "on_ac": False,
            "unplugged_at": 123.0,
            "charge_pct": 60.0,
            "processes": {},
        }
        with mock.patch.object(
                battery, "battery_health",
                return_value=self._health(external=False)), \
                mock.patch.object(battery, "snapshot_power", return_value={}), \
                mock.patch.object(
                    battery, "_load_baseline",
                    return_value=(baseline, None)), \
                mock.patch.object(
                    battery.rusage, "mach_absolute_time",
                    return_value=200), \
                mock.patch.object(
                    power, "pmenergy_coefficients",
                    return_value=(_COEFFS, "path", None)), \
                mock.patch.object(cli, "is_root", return_value=True):
            options = cli.parse_options(["--json"])
            stream = __import__("io").StringIO()
            with redirect_stdout(stream):
                rc = battery.cmd_drainers(options)
        self.assertEqual(rc, cli.EXIT_OK)
        doc = json.loads(stream.getvalue())
        self.assertTrue(doc["partial"])
        self.assertIn(
            "baseline_visibility_changed", doc["partial_reasons"])


class TestDrainersFlagRejection(unittest.TestCase):
    def test_rejects_interval(self):
        stderr = __import__("io").StringIO()
        with redirect_stderr(stderr):
            rc = battery.main(["stethoscope battery", "drainers", "--interval", "2"])
        self.assertEqual(rc, cli.EXIT_USAGE)
        self.assertIn("drainers", stderr.getvalue())

    def test_rejects_once(self):
        stderr = __import__("io").StringIO()
        with redirect_stderr(stderr):
            rc = battery.main(["stethoscope battery", "drainers", "--once"])
        self.assertEqual(rc, cli.EXIT_USAGE)

    def test_rejects_duration(self):
        stderr = __import__("io").StringIO()
        with redirect_stderr(stderr):
            rc = battery.main(["stethoscope battery", "drainers", "--duration", "5"])
        self.assertEqual(rc, cli.EXIT_USAGE)

    def test_rejects_extra_positional(self):
        stderr = __import__("io").StringIO()
        with redirect_stderr(stderr):
            rc = battery.main(["stethoscope battery", "drainers", "bogus"])
        self.assertEqual(rc, cli.EXIT_USAGE)

    def test_accepts_limit_and_json(self):
        with mock.patch.object(battery, "cmd_drainers", return_value=cli.EXIT_OK) as cmd:
            rc = battery.main(["stethoscope battery", "drainers", "--limit", "5", "--json"])
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertTrue(cmd.called)


class TestHealthFlagRejection(unittest.TestCase):
    def test_rejects_limit(self):
        stderr = __import__("io").StringIO()
        with redirect_stderr(stderr):
            rc = battery.main(["stethoscope battery", "health", "--limit", "5"])
        self.assertEqual(rc, cli.EXIT_USAGE)

    def test_rejects_extra_positional(self):
        stderr = __import__("io").StringIO()
        with redirect_stderr(stderr):
            rc = battery.main(["stethoscope battery", "health", "bogus"])
        self.assertEqual(rc, cli.EXIT_USAGE)


# ---------------------------------------------------------------------------
# scopes.battery — inspect: root gate + explicit unavailability
# ---------------------------------------------------------------------------

class TestCmdInspect(unittest.TestCase):
    def test_not_root_is_exit_permission(self):
        with mock.patch.object(cli, "is_root", return_value=False):
            options = cli.parse_options(["--json"])
            stream = __import__("io").StringIO()
            with redirect_stdout(stream):
                rc = battery.cmd_inspect(options)
        self.assertEqual(rc, cli.EXIT_PERMISSION)
        doc = json.loads(stream.getvalue())
        self.assertFalse(doc["available"])
        self.assertEqual(doc["reason"], "root_required")

    def test_root_but_powermetrics_unavailable_is_exit_error_no_fabrication(self):
        with mock.patch.object(cli, "is_root", return_value=True), \
             mock.patch.object(battery, "battery_health",
                              return_value=battery._empty_health(True, None)), \
             mock.patch.object(power, "read_powermetrics_tasks",
                              return_value=(None, "timeout")):
            options = cli.parse_options(["--json"])
            stream = __import__("io").StringIO()
            with redirect_stdout(stream):
                rc = battery.cmd_inspect(options)
        self.assertEqual(rc, cli.EXIT_ERROR)
        doc = json.loads(stream.getvalue())
        self.assertFalse(doc["available"])
        self.assertEqual(doc["reason"], "timeout")
        self.assertEqual(doc["tasks"], [])

    def test_root_and_available_ranks_by_energy_impact(self):
        tasks = [
            {
                "pid": 1,
                "name": "high total",
                "energy_impact_per_s": 1.0,
                "energy_impact_total": 100.0,
            },
            {
                "pid": 2,
                "name": "high rate",
                "energy_impact_per_s": 2.0,
                "energy_impact_total": 10.0,
            },
        ]
        with mock.patch.object(cli, "is_root", return_value=True), \
             mock.patch.object(battery, "battery_health",
                              return_value=battery._empty_health(True, None)), \
             mock.patch.object(power, "read_powermetrics_tasks",
                              return_value=(tasks, None)):
            options = cli.parse_options(["--json"])
            stream = __import__("io").StringIO()
            with redirect_stdout(stream):
                rc = battery.cmd_inspect(options)
        self.assertEqual(rc, cli.EXIT_OK)
        doc = json.loads(stream.getvalue())
        self.assertTrue(doc["available"])
        self.assertEqual(doc["tasks"][0]["pid"], 2)
        self.assertIn("not watts", doc["reconciliation_note"])
        self.assertEqual(doc["tasks"][0]["energy_impact_per_s"], 2.0)
        self.assertEqual(doc["tasks"][0]["energy_impact_total"], 10.0)

    def test_rejects_interval_flag(self):
        stderr = __import__("io").StringIO()
        with redirect_stderr(stderr):
            rc = battery.main(["stethoscope battery", "inspect", "--interval", "1"])
        self.assertEqual(rc, cli.EXIT_USAGE)


# ---------------------------------------------------------------------------
# CLI dispatch / usage text
# ---------------------------------------------------------------------------

class TestMainDispatch(unittest.TestCase):
    def test_default_mode_is_health(self):
        with mock.patch.object(battery, "cmd_health", return_value=cli.EXIT_OK) as cmd:
            rc = battery.main(["stethoscope battery"])
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertTrue(cmd.called)

    def test_unknown_mode(self):
        stderr = __import__("io").StringIO()
        with redirect_stderr(stderr):
            rc = battery.main(["stethoscope battery", "bogus"])
        self.assertEqual(rc, cli.EXIT_USAGE)
        self.assertIn("unknown mode", stderr.getvalue())

    def test_help_does_not_error(self):
        stream = __import__("io").StringIO()
        with redirect_stdout(stream):
            rc = battery.main(["stethoscope battery", "--help"])
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("battery", stream.getvalue())

    def test_usage_documents_all_exit_codes(self):
        for code in ("0 ", "1 ", "2 ", "3 ", "4"):
            self.assertIn(code, battery.USAGE)


if __name__ == "__main__":
    unittest.main()
