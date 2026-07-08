"""Unit tests for the smart scope (scopes/smart.py)."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scopes"))
import smart  # noqa: E402


class TestListPhysical(unittest.TestCase):
    SAMPLE = (
        "/dev/disk0 (internal, physical):\n"
        "   #:  TYPE NAME  SIZE  IDENTIFIER\n"
        "/dev/disk4 (external, physical):\n"
        "   #:  TYPE NAME  SIZE  IDENTIFIER\n"
        "/dev/disk3 (synthesized):\n"
    )

    def test_parses_internal_external_skips_synthesized(self):
        with mock.patch.object(smart.subprocess, "run",
                               return_value=mock.Mock(stdout=self.SAMPLE)):
            drives = smart.list_physical_drives()
        self.assertEqual(drives, [("disk0", True), ("disk4", False)])


class TestDiskutilInfo(unittest.TestCase):
    SAMPLE = (
        "   Device / Media Name:  APPLE SSD AP0256Q\n"
        "   SMART Status:         Verified\n"
        "   Disk Size:            251.0 GB (251000193024 Bytes) (exactly ...)\n"
        "   Solid State:          Yes\n"
    )

    def test_parse(self):
        with mock.patch.object(smart.subprocess, "run",
                               return_value=mock.Mock(stdout=self.SAMPLE)):
            info = smart._diskutil_info("disk0")
        self.assertEqual(info["name"], "APPLE SSD AP0256Q")
        self.assertEqual(info["smart_status"], "verified")
        self.assertEqual(info["size_bytes"], 251000193024)
        self.assertTrue(info["solid_state"])


class TestLifeEstimate(unittest.TestCase):
    def test_extrapolates(self):
        # 3% used over 1393h -> total ~46433h, remaining ~45040h ~5.1yr
        life = smart.life_estimate(3, 1393)
        self.assertEqual(life["remaining_life_pct"], 97)
        self.assertAlmostEqual(life["remaining_years"], 5.1, places=1)
        self.assertEqual(life["confidence"], "low")   # <5% wear = low confidence

    def test_none_when_no_wear_yet(self):
        self.assertIsNone(smart.life_estimate(0, 1000))
        self.assertIsNone(smart.life_estimate(None, 1000))


class TestAssess(unittest.TestCase):
    def test_healthy_has_no_warnings(self):
        h = {"smart_status": "verified", "passed": True, "percentage_used": 3,
             "available_spare": 100, "available_spare_threshold": 99,
             "media_errors": 0, "temperature_c": 45}
        self.assertEqual(smart.assess(h), [])

    def test_failing_is_critical(self):
        w = smart.assess({"smart_status": "failing"})
        self.assertEqual(w[0]["severity"], "critical")

    def test_spare_below_threshold_critical(self):
        w = smart.assess({"available_spare": 5, "available_spare_threshold": 10})
        self.assertTrue(any(x["severity"] == "critical" for x in w))

    def test_high_wear_critical(self):
        w = smart.assess({"percentage_used": 95})
        self.assertTrue(any("Wear" in x["message"] and x["severity"] == "critical"
                            for x in w))

    def test_media_errors_and_temp_are_warn(self):
        w = smart.assess({"media_errors": 3, "temperature_c": 80})
        sevs = {x["severity"] for x in w}
        self.assertEqual(sevs, {"warn"})
        self.assertEqual(len(w), 2)


class TestDriveHealthWorstSeverity(unittest.TestCase):
    def test_worst_severity_rollup(self):
        with mock.patch.object(smart, "_diskutil_info",
                               return_value={"smart_status": "failing", "name": "X",
                                             "size_bytes": 1, "solid_state": True}), \
                mock.patch.object(smart, "_smartctl_health", return_value=None):
            h = smart.drive_health("disk9", internal=False)
        self.assertEqual(h["worst_severity"], "critical")
        self.assertEqual(h["source"], "diskutil")


if __name__ == "__main__":
    unittest.main()
