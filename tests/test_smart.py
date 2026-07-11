"""Hermetic tests for the smart scope: core/smart.py (probe/parse) and
scopes/smart.py (health derivation, assessment, CLI, JSON contract).

Every subprocess boundary (diskutil, smartctl) is faked with unittest.mock,
so this suite runs on any machine — with or without smartmontools, real
drives, or root — the same way tests/test_disk.py fakes mount/lsof.
"""

import io
import json
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest import mock

from core import cli, schema
from core import smart as probe
from scopes import smart


def _fake_run(stdout="", returncode=0):
    return mock.Mock(stdout=stdout, stderr="", returncode=returncode)


# ---------------------------------------------------------------------------
# core.smart — drive enumeration
# ---------------------------------------------------------------------------

class TestListPhysicalDrives(unittest.TestCase):
    SAMPLE = (
        "/dev/disk0 (internal, physical):\n"
        "   #:  TYPE NAME  SIZE  IDENTIFIER\n"
        "/dev/disk4 (external, physical):\n"
        "   #:  TYPE NAME  SIZE  IDENTIFIER\n"
        "/dev/disk3 (synthesized):\n"
    )

    def test_parses_internal_external_skips_synthesized(self):
        with mock.patch.object(probe.subprocess, "run",
                               return_value=_fake_run(self.SAMPLE)):
            drives = probe.list_physical_drives()
        self.assertEqual(drives, [("disk0", True), ("disk4", False)])

    def test_none_on_missing_diskutil(self):
        with mock.patch.object(probe.subprocess, "run", side_effect=OSError):
            self.assertIsNone(probe.list_physical_drives())

    def test_none_on_timeout(self):
        with mock.patch.object(
                probe.subprocess, "run",
                side_effect=subprocess.TimeoutExpired(["diskutil"], 15)):
            self.assertIsNone(probe.list_physical_drives())

    def test_empty_output_is_empty_list_not_none(self):
        with mock.patch.object(probe.subprocess, "run", return_value=_fake_run("")):
            self.assertEqual(probe.list_physical_drives(), [])

    def test_nonzero_diskutil_exit_is_probe_failure(self):
        with mock.patch.object(
                probe.subprocess, "run",
                return_value=_fake_run("", returncode=1)):
            self.assertIsNone(probe.list_physical_drives())


# ---------------------------------------------------------------------------
# core.smart — diskutil info parsing
# ---------------------------------------------------------------------------

class TestDiskutilInfo(unittest.TestCase):
    SAMPLE = (
        "   Device / Media Name:  APPLE SSD AP0256Q\n"
        "   SMART Status:         Verified\n"
        "   Disk Size:            251.0 GB (251000193024 Bytes) (exactly ...)\n"
        "   Solid State:          Yes\n"
    )

    def test_parse_fields(self):
        info = probe.parse_diskutil_info(self.SAMPLE)
        self.assertEqual(info["name"], "APPLE SSD AP0256Q")
        self.assertEqual(info["smart_status"], "verified")
        self.assertEqual(info["size_bytes"], 251000193024)
        self.assertTrue(info["solid_state"])

    def test_not_supported_is_lowercased(self):
        info = probe.parse_diskutil_info("   SMART Status: Not Supported\n")
        self.assertEqual(info["smart_status"], "not supported")

    def test_missing_input_is_fully_structured_unknown(self):
        info = probe.parse_diskutil_info(None)
        self.assertEqual(info, {"name": None, "size_bytes": None,
                                 "solid_state": None, "smart_status": "unknown"})
        self.assertEqual(probe.parse_diskutil_info(""), info)

    def test_diskutil_info_reports_run_failure_as_detail(self):
        with mock.patch.object(probe.subprocess, "run", side_effect=OSError("boom")):
            info, detail = probe.diskutil_info("disk0")
        self.assertEqual(info["smart_status"], "unknown")
        self.assertIsNotNone(detail)

    def test_diskutil_info_success_has_no_detail(self):
        with mock.patch.object(probe.subprocess, "run",
                               return_value=_fake_run(self.SAMPLE)):
            info, detail = probe.diskutil_info("disk0")
        self.assertIsNone(detail)
        self.assertEqual(info["name"], "APPLE SSD AP0256Q")


# ---------------------------------------------------------------------------
# core.smart — smartctl discovery across every documented search path
# ---------------------------------------------------------------------------

class TestFindSmartctl(unittest.TestCase):
    def test_path_wins_first(self):
        with mock.patch.object(probe.shutil, "which", return_value="/usr/bin/smartctl"):
            self.assertEqual(probe.find_smartctl(), "/usr/bin/smartctl")

    def test_falls_back_through_each_candidate(self):
        for candidate in probe.SMARTCTL_CANDIDATES:
            with self.subTest(candidate=candidate), \
                    mock.patch.object(probe.shutil, "which", return_value=None), \
                    mock.patch.object(probe.os.path, "exists",
                                      side_effect=lambda p, c=candidate: p == c):
                self.assertEqual(probe.find_smartctl(), candidate)

    def test_none_when_nowhere_found(self):
        with mock.patch.object(probe.shutil, "which", return_value=None), \
                mock.patch.object(probe.os.path, "exists", return_value=False):
            self.assertIsNone(probe.find_smartctl())


# ---------------------------------------------------------------------------
# core.smart — smartctl JSON probing: malformed / unavailable / USB bridge
# ---------------------------------------------------------------------------

class TestProbeSmartctl(unittest.TestCase):
    def test_no_binary_is_explicit_detail_not_none_silently(self):
        data, detail = probe.probe_smartctl("disk4", None)
        self.assertIsNone(data)
        self.assertIn("not found", detail)

    def test_subprocess_failure_is_explicit_detail(self):
        with mock.patch.object(probe.subprocess, "run", side_effect=OSError("nope")):
            data, detail = probe.probe_smartctl("disk4", "/usr/local/bin/smartctl")
        self.assertIsNone(data)
        self.assertIn("smartctl failed to run", detail)

    def test_malformed_json_is_explicit_detail(self):
        with mock.patch.object(probe.subprocess, "run",
                               return_value=_fake_run("not json{{{")):
            data, detail = probe.probe_smartctl("disk4", "/usr/local/bin/smartctl")
        self.assertIsNone(data)
        self.assertIn("not valid JSON", detail)

    def test_non_object_json_is_explicit_detail(self):
        for payload in ("null", "[]", "true"):
            with self.subTest(payload=payload), \
                    mock.patch.object(
                        probe.subprocess, "run",
                        return_value=_fake_run(payload)):
                data, detail = probe.probe_smartctl(
                    "disk4", "/usr/local/bin/smartctl")
            self.assertIsNone(data)
            self.assertIn("not a JSON object", detail)

    def test_invalid_exit_status_is_explicit_detail(self):
        payload = json.dumps({
            "smartctl": {"exit_status": None},
            "smart_status": {"passed": True},
        })
        with mock.patch.object(
                probe.subprocess, "run",
                return_value=_fake_run(payload)):
            data, detail = probe.probe_smartctl(
                "disk4", "/usr/local/bin/smartctl")
        self.assertIsNone(data)
        self.assertIn("invalid exit_status", detail)

    def test_usb_bridge_no_passthrough_is_explicit_unavailable(self):
        payload = json.dumps({
            "smartctl": {"exit_status": 2, "messages": [
                {"string": "Smartctl open device: /dev/disk4 failed: "
                           "Unknown USB bridge [0x0781:0x5583]",
                 "severity": "error"}]}})
        with mock.patch.object(probe.subprocess, "run",
                               return_value=_fake_run(payload, returncode=2)):
            data, detail = probe.probe_smartctl("disk4", "/usr/local/bin/smartctl")
        self.assertIsNone(data)
        self.assertIn("USB bridge", detail)

    def test_open_failure_without_message_text_still_unavailable(self):
        payload = json.dumps({"smartctl": {"exit_status": 2}})
        with mock.patch.object(probe.subprocess, "run",
                               return_value=_fake_run(payload, returncode=2)):
            data, detail = probe.probe_smartctl("disk4", "/usr/local/bin/smartctl")
        self.assertIsNone(data)
        self.assertIn("could not open", detail)

    def test_incidental_error_message_does_not_discard_usable_data(self):
        # An error-severity side-query message must not discard a usable
        # current health log when smartctl's bitmask reports no failure.
        payload = json.dumps({
            "smartctl": {"exit_status": 0, "messages": [
                {"string": "Read 1 entries from Error Information Log failed",
                 "severity": "error"}]},
            "smart_status": {"passed": True},
            "nvme_smart_health_information_log": {"percentage_used": 3},
        })
        with mock.patch.object(probe.subprocess, "run",
                               return_value=_fake_run(payload)):
            data, detail = probe.probe_smartctl("disk0", "/usr/local/bin/smartctl")
        self.assertIsNone(detail)
        self.assertIsNotNone(data)
        self.assertTrue(data["smart_status"]["passed"])

    def test_command_failure_retains_usable_data_but_reports_incomplete(self):
        payload = json.dumps({
            "smartctl": {"exit_status": 0x04},
            "smart_status": {"passed": True},
        })
        with mock.patch.object(
                probe.subprocess, "run",
                return_value=_fake_run(payload, returncode=4)):
            data, detail = probe.probe_smartctl(
                "disk0", "/usr/local/bin/smartctl")
        self.assertIsNotNone(data)
        self.assertIn("incomplete", detail)

    def test_no_usable_fields_and_no_messages_is_unavailable(self):
        payload = json.dumps({"device": {"protocol": "SCSI"}})
        with mock.patch.object(probe.subprocess, "run",
                               return_value=_fake_run(payload)):
            data, detail = probe.probe_smartctl("disk4", "/usr/local/bin/smartctl")
        self.assertIsNone(data)
        self.assertIn("no usable SMART data", detail)

    def test_smart_not_supported_is_explicit_unavailable(self):
        payload = json.dumps({"smart_support": {"available": False}})
        with mock.patch.object(probe.subprocess, "run",
                               return_value=_fake_run(payload)):
            data, detail = probe.probe_smartctl("disk4", "/usr/local/bin/smartctl")
        self.assertIsNone(data)
        self.assertIn("does not support SMART", detail)

    def test_success_returns_data_and_no_detail(self):
        payload = json.dumps({
            "smartctl": {"exit_status": 0},
            "model_name": "X",
            "smart_status": {"passed": True},
        })
        with mock.patch.object(probe.subprocess, "run",
                               return_value=_fake_run(payload)):
            data, detail = probe.probe_smartctl("disk0", "/usr/local/bin/smartctl")
        self.assertIsNone(detail)
        self.assertEqual(data["model_name"], "X")

    def test_missing_json_status_uses_process_bits_and_marks_incomplete(self):
        payload = json.dumps({"smart_status": {"passed": True}})
        with mock.patch.object(
                probe.subprocess, "run",
                return_value=_fake_run(payload, returncode=8)):
            data, detail = probe.probe_smartctl(
                "disk0", "/usr/local/bin/smartctl")
        self.assertIsNotNone(data)
        self.assertIn("omitted", detail)
        self.assertEqual(data["smartctl"]["exit_status"], 8)
        self.assertFalse(probe.extract_smartctl(data)["passed"])

    def test_disagreeing_statuses_preserve_every_reported_bit(self):
        payload = json.dumps({
            "smartctl": {"exit_status": 0x10},
            "smart_status": {"passed": True},
        })
        with mock.patch.object(
                probe.subprocess, "run",
                return_value=_fake_run(payload, returncode=0x40)):
            data, detail = probe.probe_smartctl(
                "disk0", "/usr/local/bin/smartctl")
        self.assertIn("disagreed", detail)
        self.assertEqual(data["smartctl"]["exit_status"], 0x50)


# ---------------------------------------------------------------------------
# core.smart — extraction: NVMe zero counters, ATA attributes
# ---------------------------------------------------------------------------

class TestExtractSmartctl(unittest.TestCase):
    def test_malformed_nested_containers_are_total(self):
        extracted = probe.extract_smartctl({
            "smart_status": [],
            "nvme_smart_health_information_log": {
                "data_units_written": {},
                "percentage_used": "bad",
                "critical_warning": 1.5,
            },
            "ata_smart_attributes": {"table": [None, "bad", {"raw": []}]},
            "power_on_time": [],
            "temperature": "bad",
        })
        self.assertIsNone(extracted["passed"])
        self.assertIsNone(extracted["data_units_written"])
        self.assertIsNone(extracted["tbw_tb"])
        self.assertIsNone(extracted["percentage_used"])
        self.assertIsNone(extracted["critical_warning"])
        self.assertIsNone(extracted["temperature_c"])

    def test_extreme_counter_cannot_derive_infinite_tbw(self):
        extracted = probe.extract_smartctl({
            "nvme_smart_health_information_log": {
                "data_units_written": 1e308,
            },
        })
        self.assertIsNone(extracted["data_units_written"])
        self.assertIsNone(extracted["tbw_tb"])

    def test_smartctl_failing_status_bit_overrides_passed_true(self):
        extracted = probe.extract_smartctl({
            "smartctl": {"exit_status": 0x08},
            "smart_status": {"passed": True},
        })
        self.assertFalse(extracted["passed"])
        self.assertEqual(extracted["smartctl_exit_status"], 0x08)

    def test_zero_data_units_written_is_a_valid_zero_not_missing(self):
        data = {"nvme_smart_health_information_log": {"data_units_written": 0}}
        extracted = probe.extract_smartctl(data)
        self.assertEqual(extracted["data_units_written"], 0)
        self.assertEqual(extracted["tbw_tb"], 0.0)
        self.assertIsNotNone(extracted["tbw_tb"])

    def test_missing_data_units_written_is_none(self):
        extracted = probe.extract_smartctl({})
        self.assertIsNone(extracted["data_units_written"])
        self.assertIsNone(extracted["tbw_tb"])

    def test_nvme_full_log_extracted(self):
        data = {
            "model_name": "NVMe Model",
            "smart_status": {"passed": True},
            "nvme_smart_health_information_log": {
                "critical_warning": 0, "percentage_used": 12,
                "power_on_hours": 5000, "data_units_written": 123456,
                "available_spare": 100, "available_spare_threshold": 10,
                "media_errors": 0, "temperature": 42,
            },
        }
        extracted = probe.extract_smartctl(data)
        self.assertEqual(extracted["model"], "NVMe Model")
        self.assertTrue(extracted["passed"])
        self.assertEqual(extracted["percentage_used"], 12)
        self.assertEqual(extracted["power_on_hours"], 5000)
        self.assertEqual(extracted["temperature_c"], 42)
        self.assertAlmostEqual(extracted["tbw_tb"], 123456 * 512000 / 1e12, places=2)

    def test_ata_attributes_extracted_by_name(self):
        data = {
            "smart_status": {"passed": True},
            "ata_smart_attributes": {"table": [
                {"name": "Reallocated_Sector_Ct", "raw": {"value": 3}},
                {"name": "Reallocated_Event_Count", "raw": {"value": 1}},
                {"name": "Current_Pending_Sector", "raw": {"value": 2}},
                {"name": "Offline_Uncorrectable", "raw": {"value": 0}},
                {"name": "Temperature_Celsius", "raw": {"value": 38}},
            ]},
        }
        extracted = probe.extract_smartctl(data)
        self.assertEqual(extracted["reallocated_sector_ct"], 3)
        self.assertEqual(extracted["reallocated_event_count"], 1)
        self.assertEqual(extracted["current_pending_sector"], 2)
        self.assertEqual(extracted["offline_uncorrectable"], 0)
        self.assertEqual(extracted["temperature_c"], 38)

    def test_common_sata_wear_errors_and_writes_are_extracted(self):
        data = {
            "ata_smart_attributes": {"table": [
                {
                    "name": "Media_Wearout_Indicator",
                    "value": 6,
                    "raw": {"value": 1234},
                },
                {
                    "name": "Reported_Uncorrect",
                    "value": 91,
                    "raw": {"value": 9},
                },
                {
                    "name": "Total_LBAs_Written",
                    "value": 99,
                    "raw": {"value": 2000000000},
                },
            ]},
        }
        extracted = probe.extract_smartctl(data)
        self.assertEqual(extracted["percentage_used"], 94)
        self.assertEqual(extracted["reported_uncorrectable"], 9)
        self.assertEqual(extracted["tbw_tb"], 1.02)

    def test_ata_when_failed_attributes_are_preserved(self):
        data = {
            "ata_smart_attributes": {"table": [
                {
                    "name": "Current_Pending_Sector",
                    "when_failed": "FAILING_NOW",
                    "flags": {"prefailure": True},
                    "raw": {"value": 1},
                },
                {
                    "name": "Airflow_Temperature_Cel",
                    "when_failed": "FAILING_NOW",
                    "flags": {"prefailure": False},
                    "raw": {"value": 80},
                },
                {
                    "name": "Reallocated_Sector_Ct",
                    "when_failed": "In_the_past",
                    "raw": {"value": 2},
                },
            ]},
        }
        extracted = probe.extract_smartctl(data)
        self.assertEqual(
            extracted["ata_failing_attributes"],
            ["Current_Pending_Sector"])
        self.assertEqual(
            extracted["ata_usage_attributes_now"],
            ["Airflow_Temperature_Cel"])
        self.assertEqual(
            extracted["ata_failed_attributes_past"],
            ["Reallocated_Sector_Ct"])

    def test_power_on_hours_falls_back_to_ata_power_on_time(self):
        data = {"power_on_time": {"hours": 777}}
        self.assertEqual(probe.extract_smartctl(data)["power_on_hours"], 777)

    def test_temperature_falls_back_to_top_level_then_ata(self):
        self.assertEqual(
            probe.extract_smartctl({"temperature": {"current": 50}})["temperature_c"], 50)


# ---------------------------------------------------------------------------
# scopes.smart — life estimate + confidence
# ---------------------------------------------------------------------------

class TestLifeEstimate(unittest.TestCase):
    def test_extrapolates(self):
        life = smart.life_estimate(12, 5000)
        self.assertEqual(life["remaining_life_pct"], 88)
        self.assertEqual(life["consumed_life_pct"], 12)
        self.assertGreater(life["remaining_hours"], 0)

    def test_none_when_no_wear_yet(self):
        self.assertIsNone(smart.life_estimate(0, 1000))
        self.assertIsNone(smart.life_estimate(None, 1000))

    def test_none_without_power_on_hours(self):
        self.assertIsNone(smart.life_estimate(10, None))
        self.assertIsNone(smart.life_estimate(10, 0))

    def test_malformed_scalars_do_not_raise(self):
        self.assertIsNone(smart.life_estimate("10", 1000))
        self.assertIsNone(smart.life_estimate(10, {}))
        self.assertIsNone(smart.life_estimate(float("nan"), 1000))
        self.assertIsNone(smart.life_estimate(5e-324, 1))
        self.assertIsNone(smart.life_estimate(10, 1e308))

    def test_low_confidence_under_five_percent(self):
        self.assertEqual(smart.life_estimate(3, 1393)["confidence"], "low")
        self.assertEqual(smart.life_estimate(4.9, 1393)["confidence"], "low")

    def test_moderate_confidence_band(self):
        self.assertEqual(smart.life_estimate(5, 1393)["confidence"], "moderate")
        self.assertEqual(smart.life_estimate(19, 1393)["confidence"], "moderate")

    def test_high_confidence_band(self):
        self.assertEqual(smart.life_estimate(20, 1393)["confidence"], "high")
        self.assertEqual(smart.life_estimate(80, 50000)["confidence"], "high")


# ---------------------------------------------------------------------------
# scopes.smart — assess(): each pre-failure warning class
# ---------------------------------------------------------------------------

class TestAssess(unittest.TestCase):
    def test_healthy_has_no_warnings(self):
        h = {"smart_status": "verified", "passed": True, "percentage_used": 3,
             "available_spare": 100, "available_spare_threshold": 99,
             "media_errors": 0, "temperature_c": 45, "critical_warning": 0,
             "current_pending_sector": 0, "offline_uncorrectable": 0,
             "reallocated_sector_ct": 0, "reallocated_event_count": 0}
        self.assertEqual(smart.assess(h), [])

    def test_failing_smart_status_is_critical(self):
        w = smart.assess({"smart_status": "failing"})
        self.assertEqual(w[0]["severity"], "critical")

    def test_passed_false_is_critical_even_if_status_unknown(self):
        w = smart.assess({"smart_status": "unknown", "passed": False})
        self.assertTrue(any(x["severity"] == "critical" for x in w))

    def test_nvme_critical_warning_bit_is_critical_and_decoded(self):
        w = smart.assess({"critical_warning": 0b1})
        self.assertEqual(w[0]["severity"], "critical")
        self.assertIn("spare capacity", w[0]["message"])

    def test_smartctl_prefail_exit_bit_is_critical(self):
        warnings = smart.assess({"smartctl_exit_status": 0x10})
        self.assertEqual(warnings[0]["code"], "ata_prefail_threshold")
        self.assertEqual(warnings[0]["severity"], "critical")

    def test_smartctl_old_age_exit_bit_is_warning(self):
        warnings = smart.assess({"smartctl_exit_status": 0x20})
        self.assertEqual(warnings[0]["code"], "ata_usage_threshold")
        self.assertEqual(warnings[0]["severity"], "warn")

    def test_smart_error_and_self_test_log_bits_are_warnings(self):
        warnings = smart.assess({"smartctl_exit_status": 0xc0})
        self.assertEqual(
            [warning["code"] for warning in warnings],
            ["smart_error_log", "smart_self_test_log"])

    def test_named_ata_failure_is_more_specific_than_generic_bit(self):
        warnings = smart.assess({
            "smartctl_exit_status": 0x10,
            "ata_failing_attributes": ["Current_Pending_Sector"],
        })
        self.assertEqual(warnings[0]["code"], "ata_attribute_failing")
        self.assertNotIn(
            "ata_prefail_threshold",
            [warning["code"] for warning in warnings])

    def test_current_old_age_attribute_is_warning_not_critical(self):
        warnings = smart.assess({
            "ata_usage_attributes_now": ["Airflow_Temperature_Cel"],
        })
        self.assertEqual(
            [warning["severity"] for warning in warnings], ["warn"])
        self.assertEqual(
            warnings[0]["code"], "ata_usage_attribute_threshold")

    def test_reported_uncorrectable_is_warning(self):
        warnings = smart.assess({"reported_uncorrectable": 9})
        self.assertEqual(warnings[0]["code"], "reported_uncorrectable")
        self.assertEqual(warnings[0]["severity"], "warn")

    def test_spare_below_threshold_is_critical(self):
        w = smart.assess({"available_spare": 5, "available_spare_threshold": 10})
        self.assertTrue(any(x["severity"] == "critical" for x in w))

    def test_high_wear_is_critical(self):
        w = smart.assess({"percentage_used": 95})
        self.assertTrue(any("Wear" in x["message"] and x["severity"] == "critical"
                            for x in w))

    def test_wear_just_under_threshold_is_not_flagged(self):
        w = smart.assess({"percentage_used": smart.WEAR_CRITICAL_PCT - 1})
        self.assertEqual(w, [])

    def test_pending_sector_is_critical(self):
        w = smart.assess({"current_pending_sector": 4})
        self.assertTrue(any(x["severity"] == "critical" for x in w))

    def test_offline_uncorrectable_is_critical(self):
        w = smart.assess({"offline_uncorrectable": 1})
        self.assertTrue(any(x["severity"] == "critical" for x in w))

    def test_media_errors_is_warn(self):
        w = smart.assess({"media_errors": 3})
        self.assertEqual(w, [{"code": "media_errors", "severity": "warn",
                              "message": "3 media/data-integrity error(s) logged; "
                                         "back this drive up soon."}])

    def test_reallocated_sectors_is_warn(self):
        w = smart.assess({"reallocated_sector_ct": 2})
        self.assertTrue(any(x["severity"] == "warn" for x in w))

    def test_high_temperature_is_warn(self):
        w = smart.assess({"temperature_c": 80})
        self.assertEqual(w, [{"code": "temperature_high", "severity": "warn",
                              "message": "Temperature 80\u00b0C is high."}])

    def test_temperature_just_under_threshold_is_not_flagged(self):
        self.assertEqual(smart.assess({"temperature_c": smart.TEMP_WARN_C - 1}), [])

    def test_none_values_never_raise_or_warn(self):
        self.assertEqual(smart.assess({}), [])

    def test_every_warning_has_stable_fields(self):
        warnings = smart.assess({
            "smart_status": "failing",
            "critical_warning": 1,
            "available_spare": 1,
            "available_spare_threshold": 10,
            "percentage_used": 99,
            "current_pending_sector": 1,
            "offline_uncorrectable": 1,
            "media_errors": 1,
            "reallocated_sector_ct": 1,
            "reallocated_event_count": 1,
            "reported_uncorrectable": 1,
            "temperature_c": 80,
        })
        self.assertTrue(warnings)
        for warning in warnings:
            self.assertEqual(
                set(warning), {"code", "severity", "message"})

    def test_malformed_scalars_do_not_raise_or_warn(self):
        self.assertEqual(smart.assess({
            "critical_warning": "1",
            "available_spare": {},
            "available_spare_threshold": [],
            "percentage_used": "99",
            "temperature_c": float("inf"),
        }), [])


class TestWorstSeverity(unittest.TestCase):
    def test_rollup(self):
        self.assertEqual(smart._worst_severity([]), "ok")
        self.assertEqual(smart._worst_severity([{"severity": "warn"}]), "warn")
        self.assertEqual(
            smart._worst_severity([{"severity": "warn"}, {"severity": "critical"}]),
            "critical")


# ---------------------------------------------------------------------------
# scopes.smart — drive_health(): status precedence, spare/TBW rendering
# ---------------------------------------------------------------------------

class TestDriveHealthStatusPrecedence(unittest.TestCase):
    def _health(self, diskutil_status, smartctl_passed):
        info = {"name": "X", "size_bytes": 1, "solid_state": True,
                "smart_status": diskutil_status}
        raw = {"smart_status": {"passed": smartctl_passed}} if smartctl_passed is not None else None
        with mock.patch.object(probe, "diskutil_info", return_value=(info, None)), \
                mock.patch.object(
                    probe, "probe_smartctl",
                    return_value=(raw, None if raw else "unavailable")):
            return smart.drive_health("disk9", False, "/usr/local/bin/smartctl")

    def test_smartctl_fail_overrides_diskutil_verified(self):
        h = self._health("verified", False)
        self.assertEqual(h["smart_status"], "failing")

    def test_smartctl_pass_replaces_unknown(self):
        h = self._health("unknown", True)
        self.assertEqual(h["smart_status"], "verified")

    def test_smartctl_pass_replaces_not_supported(self):
        h = self._health("not supported", True)
        self.assertEqual(h["smart_status"], "verified")

    def test_smartctl_pass_does_not_downgrade_existing_failing(self):
        h = self._health("failing", True)
        self.assertEqual(h["smart_status"], "failing")

    def test_no_smartctl_keeps_diskutil_verdict(self):
        h = self._health("not supported", None)
        self.assertEqual(h["smart_status"], "not supported")
        self.assertFalse(h["smartctl_available"])
        self.assertIsNotNone(h["smartctl_detail"])

    def test_worst_severity_rollup_from_diskutil_only(self):
        h = self._health("failing", None)
        self.assertEqual(h["worst_severity"], "critical")
        self.assertEqual(h["source"], "diskutil")


class TestDriveHealthUsbBridge(unittest.TestCase):
    def test_usb_bridge_detail_is_structured_not_fabricated_healthy(self):
        info = {"name": "External", "size_bytes": 1, "solid_state": False,
                "smart_status": "not supported"}
        with mock.patch.object(probe, "diskutil_info", return_value=(info, None)), \
                mock.patch.object(
                    probe, "probe_smartctl",
                    return_value=(None, "Unknown USB bridge [0x0781:0x5583]")):
            h = smart.drive_health("disk4", False, "/usr/local/bin/smartctl")
        self.assertFalse(h["smartctl_available"])
        self.assertIn("USB bridge", h["smartctl_detail"])
        self.assertEqual(h["smart_status"], "not supported")
        self.assertEqual(h["worst_severity"], "ok")   # not fabricated as failing either


# ---------------------------------------------------------------------------
# scopes.smart — human rendering: missing fields stay safe
# ---------------------------------------------------------------------------

class TestRenderHumanMissingFields(unittest.TestCase):
    def test_missing_spare_renders_question_mark_not_none(self):
        h = {"device": "disk0", "internal": True, "name": "X", "size_bytes": None,
             "smart_status": "verified", "smartctl_available": True,
             "smartctl_detail": None, "percentage_used": 10,
             "power_on_hours": 100, "tbw_tb": None, "available_spare": None,
             "temperature_c": None, "life": None, "warnings": [],
             "worst_severity": "ok"}
        text = smart._render([h], "/usr/local/bin/smartctl")
        self.assertIn("spare ?", text)
        self.assertNotIn("spare None", text)
        self.assertNotIn("None%", text)

    def test_missing_size_and_name_render_question_marks(self):
        h = {"device": "disk1", "internal": False, "name": None, "size_bytes": None,
             "smart_status": "unknown", "smartctl_available": False,
             "smartctl_detail": "smartctl not found on PATH or common install locations",
             "warnings": [], "worst_severity": "ok"}
        text = smart._render([h], None)
        self.assertIn("disk1  ?  \u00b7  ?  \u00b7  SMART unknown", text)
        self.assertIn("smartctl not found", text)

    def test_unsupported_drive_is_not_labeled_healthy(self):
        h = {
            "device": "disk4",
            "internal": False,
            "name": "USB",
            "size_bytes": 1000,
            "smart_status": "not supported",
            "smartctl_available": False,
            "smartctl_detail": "device does not support SMART",
            "warnings": [],
            "worst_severity": "ok",
        }
        text = smart._render([h], "/usr/local/bin/smartctl")
        self.assertNotIn("healthy", text)

    def test_ata_temperature_renders_without_nvme_wear(self):
        h = {
            "device": "disk2",
            "internal": False,
            "name": "SATA SSD",
            "size_bytes": 1000,
            "smart_status": "verified",
            "smartctl_available": True,
            "smartctl_detail": None,
            "percentage_used": None,
            "temperature_c": 42,
            "life": None,
            "warnings": [],
            "worst_severity": "ok",
        }
        text = smart._render([h], "/usr/local/bin/smartctl")
        self.assertIn("temperature 42\u00b0C", text)

    def test_unknown_power_on_hours_render_as_unknown_not_zero(self):
        h = {
            "device": "disk0",
            "internal": True,
            "name": "NVMe",
            "size_bytes": 1000,
            "smart_status": "verified",
            "smartctl_available": True,
            "smartctl_detail": None,
            "percentage_used": 5,
            "power_on_hours": None,
            "tbw_tb": None,
            "available_spare": None,
            "temperature_c": None,
            "life": None,
            "warnings": [],
            "worst_severity": "ok",
        }
        text = smart._render([h], "/usr/local/bin/smartctl")
        self.assertIn("? power-on hrs", text)
        self.assertNotIn("0 power-on hrs", text)

    def test_no_drives_renders_safely(self):
        text = smart._render([], "/usr/local/bin/smartctl")
        self.assertIn("no physical drives found", text)

    def test_no_control_codes_are_required_for_readability(self):
        # sanity: rendering does not raise when styling constants are present
        h = {"device": "disk0", "internal": True, "name": "X", "size_bytes": 100,
             "smart_status": "verified", "smartctl_available": False,
             "smartctl_detail": "no smartctl", "warnings": [], "worst_severity": "ok"}
        text = smart._render([h], None)
        self.assertIsInstance(text, str)


# ---------------------------------------------------------------------------
# scopes.smart — JSON contract shape
# ---------------------------------------------------------------------------

class TestStatusJsonShape(unittest.TestCase):
    def test_document_has_stable_envelope_and_drive_fields(self):
        options = cli.parse_options(["--json"])
        drives = [("disk0", True)]
        health = {"device": "disk0", "internal": True, "name": "X",
                  "smart_status": "verified", "smartctl_available": True,
                  "smartctl_detail": None, "warnings": [], "worst_severity": "ok"}
        with mock.patch.object(smart.probe, "find_smartctl",
                               return_value="/usr/local/bin/smartctl"), \
                mock.patch.object(smart.probe, "list_physical_drives",
                                  return_value=drives), \
                mock.patch.object(smart, "drive_health", return_value=health):
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = smart.cmd_status(options)
        document = json.loads(stream.getvalue())
        self.assertEqual(result, cli.EXIT_OK)
        self.assertEqual(document["schema"], schema.SCHEMA_VERSION)
        self.assertEqual(document["scope"], "smart")
        self.assertEqual(document["command"], "status")
        self.assertIn("drives", document)
        self.assertEqual(document["drives"][0]["device"], "disk0")
        self.assertEqual(stream.getvalue().count("\n"), 1)

    def test_smartctl_missing_marks_partial(self):
        options = cli.parse_options(["--json"])
        health = {"device": "disk0", "internal": True, "smartctl_available": False,
                  "warnings": [], "worst_severity": "ok"}
        with mock.patch.object(smart.probe, "find_smartctl", return_value=None), \
                mock.patch.object(smart.probe, "list_physical_drives",
                                  return_value=[("disk0", True)]), \
                mock.patch.object(smart, "drive_health", return_value=health):
            stream = io.StringIO()
            with redirect_stdout(stream):
                smart.cmd_status(options)
        document = json.loads(stream.getvalue())
        self.assertTrue(document["partial"])
        self.assertIn("smartctl_unavailable", document["partial_reasons"])

    def test_diskutil_info_failure_marks_partial(self):
        options = cli.parse_options(["--json"])
        health = {
            "device": "disk0",
            "internal": True,
            "diskutil_detail": "diskutil info failed to run",
            "smartctl_available": True,
            "warnings": [],
            "worst_severity": "ok",
        }
        with mock.patch.object(
                smart.probe, "find_smartctl",
                return_value="/usr/local/bin/smartctl"), \
                mock.patch.object(
                    smart.probe, "list_physical_drives",
                    return_value=[("disk0", True)]), \
                mock.patch.object(
                    smart, "drive_health", return_value=health):
            stream = io.StringIO()
            with redirect_stdout(stream):
                smart.cmd_status(options)
        document = json.loads(stream.getvalue())
        self.assertTrue(document["partial"])
        self.assertIn(
            "diskutil_probe_incomplete", document["partial_reasons"])

    def test_smartctl_incomplete_detail_marks_partial(self):
        options = cli.parse_options(["--json"])
        health = {
            "device": "disk0",
            "internal": True,
            "diskutil_detail": None,
            "smartctl_available": True,
            "smartctl_detail": "smartctl reported an incomplete command",
            "warnings": [],
            "worst_severity": "ok",
        }
        with mock.patch.object(
                smart.probe, "find_smartctl",
                return_value="/usr/local/bin/smartctl"), \
                mock.patch.object(
                    smart.probe, "list_physical_drives",
                    return_value=[("disk0", True)]), \
                mock.patch.object(
                    smart, "drive_health", return_value=health):
            stream = io.StringIO()
            with redirect_stdout(stream):
                smart.cmd_status(options)
        document = json.loads(stream.getvalue())
        self.assertTrue(document["partial"])
        self.assertIn(
            "smartctl_probe_incomplete", document["partial_reasons"])

    def test_probe_error_shape_and_exit(self):
        options = cli.parse_options(["--json"])
        with mock.patch.object(smart.probe, "list_physical_drives", return_value=None):
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = smart.cmd_status(options)
        document = json.loads(stream.getvalue())
        self.assertEqual(result, cli.EXIT_ERROR)
        self.assertEqual(document["drives"], [])
        self.assertIsNotNone(document["error"])
        self.assertTrue(document["partial"])

    def test_unknown_disk_argument_is_usage_error(self):
        options = cli.parse_options(["--json", "disk99"])
        with mock.patch.object(smart.probe, "find_smartctl", return_value=None), \
                mock.patch.object(smart.probe, "list_physical_drives",
                                  return_value=[("disk0", True)]):
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = smart.cmd_status(options)
        document = json.loads(stream.getvalue())
        self.assertEqual(result, cli.EXIT_USAGE)
        self.assertEqual(document["drives"], [])
        self.assertIsNotNone(document["error"])

    def test_critical_drive_findings_exit_code(self):
        options = cli.parse_options(["--json"])
        health = {"device": "disk0", "internal": True, "smartctl_available": True,
                  "warnings": [{"severity": "critical", "message": "x"}],
                  "worst_severity": "critical"}
        with mock.patch.object(smart.probe, "find_smartctl",
                               return_value="/usr/local/bin/smartctl"), \
                mock.patch.object(smart.probe, "list_physical_drives",
                                  return_value=[("disk0", True)]), \
                mock.patch.object(smart, "drive_health", return_value=health):
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = smart.cmd_status(options)
        self.assertEqual(result, cli.EXIT_FINDINGS)

    def test_warning_drive_findings_exit_code(self):
        options = cli.parse_options(["--json"])
        health = {
            "device": "disk0",
            "internal": True,
            "diskutil_detail": None,
            "smartctl_available": True,
            "smartctl_detail": None,
            "warnings": [{
                "code": "smart_error_log",
                "severity": "warn",
                "message": "device errors",
            }],
            "worst_severity": "warn",
        }
        with mock.patch.object(
                smart.probe, "find_smartctl",
                return_value="/usr/local/bin/smartctl"), \
                mock.patch.object(
                    smart.probe, "list_physical_drives",
                    return_value=[("disk0", True)]), \
                mock.patch.object(
                    smart, "drive_health", return_value=health):
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = smart.cmd_status(options)
        self.assertEqual(result, cli.EXIT_FINDINGS)


# ---------------------------------------------------------------------------
# scopes.smart — CLI surface: static flag rejection, exit codes
# ---------------------------------------------------------------------------

class TestMainCliSurface(unittest.TestCase):
    def test_rejects_sampling_flags_it_cannot_honor(self):
        for flag_args in (["--once"], ["--duration", "3"], ["--interval", "2"],
                          ["--limit", "5"]):
            with self.subTest(flag_args=flag_args):
                stream = io.StringIO()
                with mock.patch("sys.stderr", stream):
                    result = smart.main(["stethoscope smart"] + flag_args)
                self.assertEqual(result, cli.EXIT_USAGE)
                self.assertIn("does not support", stream.getvalue())

    def test_rejects_extra_positionals(self):
        stream = io.StringIO()
        with mock.patch("sys.stderr", stream):
            result = smart.main(["stethoscope smart", "disk0", "disk1"])
        self.assertEqual(result, cli.EXIT_USAGE)
        self.assertIn("at most one disk argument", stream.getvalue())

    def test_status_verb_is_optional_and_equivalent(self):
        with mock.patch.object(smart, "cmd_status", return_value=cli.EXIT_OK) as cmd:
            smart.main(["stethoscope smart", "status", "disk0"])
            self.assertEqual(cmd.call_args[0][0].rest, ["disk0"])
        with mock.patch.object(smart, "cmd_status", return_value=cli.EXIT_OK) as cmd:
            smart.main(["stethoscope smart", "disk0"])
            self.assertEqual(cmd.call_args[0][0].rest, ["disk0"])

    def test_help_exits_ok_without_probing(self):
        stream = io.StringIO()
        with redirect_stdout(stream), \
                mock.patch.object(smart.probe, "list_physical_drives") as enumerate_:
            result = smart.main(["stethoscope smart", "--help"])
        self.assertEqual(result, cli.EXIT_OK)
        enumerate_.assert_not_called()
        self.assertIn("smart", stream.getvalue())

    def test_end_to_end_exit_ok_with_no_drives(self):
        with mock.patch.object(smart.probe, "find_smartctl", return_value=None), \
                mock.patch.object(smart.probe, "list_physical_drives", return_value=[]):
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = smart.main(["stethoscope smart"])
        self.assertEqual(result, cli.EXIT_OK)


if __name__ == "__main__":
    unittest.main()
