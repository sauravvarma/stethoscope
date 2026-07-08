"""Pure-helper tests for the multi-scope curses TUI shell."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scopes"))
import tui  # noqa: E402


class TestTabs(unittest.TestCase):
    def test_tab_registry_order(self):
        self.assertEqual(tui.TABS, ("disk", "cpu", "memory", "battery", "smart"))

    def test_tab_index_defaults_to_disk(self):
        self.assertEqual(tui.tab_index("memory"), 2)
        self.assertEqual(tui.tab_index("missing"), 0)

    def test_number_keys_map_to_tabs(self):
        self.assertEqual(tui.tab_index_for_key(ord("1")), 0)
        self.assertEqual(tui.tab_index_for_key(ord("5")), 4)
        self.assertIsNone(tui.tab_index_for_key(ord("9")))


class TestSeverity(unittest.TestCase):
    def test_severity_pair_mapping(self):
        self.assertEqual(tui.severity_pair("ok"), tui.C_READ)
        self.assertEqual(tui.severity_pair("warn"), tui.C_WRITE)
        self.assertEqual(tui.severity_pair("critical"), tui.C_CRIT)
        self.assertEqual(tui.severity_pair("unknown"), tui.C_ACCENT)

    def test_memory_pressure(self):
        self.assertEqual(tui.severity_for_memory_pressure("normal"), "ok")
        self.assertEqual(tui.severity_for_memory_pressure("warn"), "warn")
        self.assertEqual(tui.severity_for_memory_pressure("critical"), "critical")

    def test_battery_service_is_critical(self):
        self.assertEqual(tui.severity_for_battery_health({"present": False}), "ok")
        self.assertEqual(tui.severity_for_battery_health({"present": True, "condition": "Normal"}), "ok")
        self.assertEqual(
            tui.severity_for_battery_health({"present": True, "condition": "Service Recommended"}),
            "critical",
        )

    def test_smart_verdict(self):
        self.assertEqual(tui.health_verdict({"worst_severity": "critical"}), "CRITICAL")
        self.assertEqual(tui.health_verdict({"worst_severity": "warn"}), "WARN")
        self.assertEqual(tui.health_verdict({"smart_status": "verified"}), "HEALTHY")


class TestFormatting(unittest.TestCase):
    def test_cpu_row_format(self):
        row = (12.345, 7.8, 1.0, 6.8, 123, "ExampleProcess")
        self.assertIn("123", tui.format_cpu_row(row))
        self.assertIn("12.3%", tui.format_cpu_row(row))
        self.assertIn("7.8", tui.format_cpu_row(row))

    def test_smart_row_format(self):
        row = {"device": "disk0", "name": "APPLE SSD", "size_bytes": 1024**3,
               "smart_status": "verified", "percentage_used": 3, "internal": True}
        text = tui.format_smart_row(row)
        self.assertIn("disk0", text)
        self.assertIn("3%", text)
        self.assertIn("internal", text)


if __name__ == "__main__":
    unittest.main()
