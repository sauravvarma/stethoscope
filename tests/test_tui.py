"""Hermetic tests for the unified curses shell and shared widgets."""

import curses
import errno
import io
import os
import pty
import runpy
import select
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from core import cli
from core import tui as widgets
from scopes import disk_tui
from scopes import smart
from scopes import tui


class FakeWindow:
    def __init__(self, height=24, width=120, keys=None, fail_draw=False):
        self.height = height
        self.width = width
        self.keys = list(keys or [])
        self.fail_draw = fail_draw
        self.draws = []
        self.timeouts = []

    def getmaxyx(self):
        return self.height, self.width

    def addnstr(self, y, x, text, count, attr=0):
        if self.fail_draw:
            raise curses.error("resize")
        self.draws.append((y, x, text[:count], attr))

    def erase(self):
        return None

    def clear(self):
        return None

    def refresh(self):
        return None

    def timeout(self, value):
        self.timeouts.append(value)

    def getch(self):
        return self.keys.pop(0) if self.keys else -1

    def text(self):
        return "\n".join(item[2] for item in self.draws)


class FakeClock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value


def disk_row(pid=7, name="proc"):
    return (30.0, 10.0, 20.0, 100, 200, pid, name)


class AppTestCase(unittest.TestCase):
    def make_app(self, initial="disk", window=None, clock=None):
        window = window or FakeWindow()
        clock = clock or FakeClock()
        patches = (
            mock.patch.object(tui.cli, "is_root", return_value=True),
            mock.patch.object(tui.curses, "curs_set", return_value=None),
            mock.patch.object(tui.disk, "snapshot_diskio", return_value={}),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return tui.App(window, initial_tab=initial, clock=clock), window, clock


class TestCoreWidgets(unittest.TestCase):
    def test_sanitize_removes_external_controls_and_bounds(self):
        self.assertEqual(widgets.sanitize("bad\nname\x1b", 8), "bad?name")

    def test_ring_history_and_sparkline_are_bounded(self):
        history = widgets.RingHistory(3)
        for value in range(5):
            history.append(value)
        self.assertEqual(history.values(), [2, 3, 4])
        self.assertLessEqual(len(history.sparkline(2)), 2)
        self.assertEqual(widgets.sparkline([float("nan")]), "")

    def test_safe_draw_and_fill_survive_tiny_resize(self):
        window = FakeWindow(1, 1)
        self.assertFalse(widgets.safe_addstr(window, 0, 0, "x"))
        self.assertFalse(widgets.safe_fill(window, 0))
        window.width = 4
        self.assertTrue(widgets.safe_addstr(window, 0, 0, "abcdef"))
        self.assertEqual(window.draws[-1][2], "abc")

    def test_safe_draw_absorbs_curses_errors(self):
        window = FakeWindow(fail_draw=True)
        self.assertFalse(widgets.safe_addstr(window, 0, 0, "x"))
        self.assertFalse(widgets.safe_fill(window, 0))

    def test_popup_declines_impossibly_small_screen(self):
        palette = mock.Mock()
        self.assertFalse(widgets.popup(FakeWindow(3, 8), palette, "x", ["y"]))

    def test_color_independent_labels_cover_degraded_states(self):
        for state in ("ok", "info", "healthy", "warn", "critical", "unknown",
                      "absent", "error", "partial"):
            self.assertTrue(widgets.severity_label(state).startswith("["))


class TestRegistryAndKeys(AppTestCase):
    def test_tab_registry_and_visible_drives_label(self):
        self.assertEqual(
            tui.TABS, ("disk", "cpu", "memory", "battery", "smart"))
        self.assertEqual(tui.TAB_LABELS["smart"], "drives")
        self.assertEqual(tui.tab_index_for_key(ord("1")), 0)
        self.assertEqual(tui.tab_index_for_key(ord("5")), 4)
        self.assertIsNone(tui.tab_index_for_key(ord("9")))

    def test_tab_cycles_globally_and_v_changes_disk_subview(self):
        app, _window, _clock = self.make_app()
        with mock.patch.object(app, "enter_tab", wraps=app.enter_tab):
            app.handle_key(ord("\t"))
        self.assertEqual(tui.TABS[app.tab], "cpu")
        app.handle_key(ord("1"))
        self.assertEqual(tui.TABS[app.tab], "disk")
        with mock.patch.object(app, "refresh_volumes"):
            app.handle_key(ord("v"))
        self.assertEqual(app.disk_view, tui.V_VOL)

    def test_numeric_keys_never_change_disk_subview(self):
        app, _window, _clock = self.make_app()
        app.disk_view = tui.V_VOL
        app.handle_key(ord("2"))
        app.handle_key(ord("1"))
        self.assertEqual(app.disk_view, tui.V_VOL)

    def test_selection_clamps_and_moves(self):
        app, _window, _clock = self.make_app()
        app.disk_rows = [disk_row(1), disk_row(2)]
        app.handle_key(curses.KEY_DOWN)
        app.handle_key(curses.KEY_DOWN)
        self.assertEqual(app.selection["disk"], 1)
        app.handle_key(ord("k"))
        self.assertEqual(app.selection["disk"], 0)
        self.assertEqual(app.selected_pid()[0], 1)


class TestLazyRefresh(AppTestCase):
    def test_startup_primes_only_active_rate_tab(self):
        with mock.patch.object(tui.cpu, "snapshot_cpu") as cpu_snapshot, \
                mock.patch.object(tui.memory, "snapshot_footprint") as mem, \
                mock.patch.object(tui.battery, "snapshot_power") as power, \
                mock.patch.object(tui.smart, "collect_health") as drives, \
                mock.patch.object(tui.anomaly, "run") as diagnose, \
                mock.patch.object(tui.disk, "_mount_table") as mounts:
            self.make_app()
        cpu_snapshot.assert_not_called()
        mem.assert_not_called()
        power.assert_not_called()
        drives.assert_not_called()
        diagnose.assert_not_called()
        mounts.assert_not_called()

    def test_diagnosis_is_explicit_and_uses_canonical_triage(self):
        app, _window, _clock = self.make_app()
        finding = {
            "code": "cpu_hot", "severity": "warn", "area": "cpu",
            "detector": "runaway", "message": "CPU is hot", "score": 70,
            "confidence": "high", "drill_down": ["stethoscope cpu top"],
            "evidence": {"cpu_pct": 180.0},
        }
        document = {
            "findings": [finding], "partial": False,
            "partial_reasons": [], "error": None,
        }
        with mock.patch.object(
                tui.anomaly, "run", return_value=(document, 0)) as run:
            app.handle_key(ord("d"))
        run.assert_called_once_with(
            "triage", interval=1.0, limit=20, scope="triage")
        self.assertTrue(app.findings_focused)
        self.assertEqual(app.diagnosis_document, document)

    def test_findings_strip_cycles_and_enter_opens_evidence(self):
        app, window, _clock = self.make_app()
        app.diagnosis_document = {
            "findings": [
                {"code": "one", "severity": "warn", "area": "cpu",
                 "detector": "runaway", "message": "first", "score": 60,
                 "confidence": "moderate", "evidence": {"cpu": 90},
                 "drill_down": ["stethoscope cpu top"]},
                {"code": "two", "severity": "critical", "area": "memory",
                 "detector": "point", "message": "second", "score": 100,
                 "confidence": "high", "evidence": {"pressure": "critical"},
                 "drill_down": ["stethoscope memory top"]},
            ],
            "partial": False, "partial_reasons": [], "error": None,
        }
        app.draw()
        self.assertIn("first", window.text())
        app.handle_key(ord("]"))
        with mock.patch.object(app, "popup") as popup:
            app.handle_key(10)
        self.assertIn("second", popup.call_args[0][1][1])
        self.assertTrue(any(
            "pressure" in line for line in popup.call_args[0][1]))

    def test_partial_diagnosis_is_not_rendered_healthy(self):
        app, window, _clock = self.make_app()
        app.diagnosis_document = {
            "findings": [], "partial": True,
            "partial_reasons": ["history_unavailable"], "error": None,
        }
        app.draw()
        self.assertIn("[PARTIAL]", window.text())
        self.assertNotIn("no active diagnosis findings", window.text())

    def test_finding_does_not_hide_partial_diagnosis_coverage(self):
        app, window, _clock = self.make_app()
        app.diagnosis_document = {
            "findings": [
                {"code": "one", "severity": "warn", "area": "cpu",
                 "detector": "runaway", "message": "first", "score": 60,
                 "confidence": "low", "evidence": {}, "drill_down": []},
            ],
            "partial": True, "partial_reasons": ["not_root"], "error": None,
        }
        app.draw()
        self.assertIn("first", window.text())
        self.assertIn("[PARTIAL] not_root", window.text())

    def test_cpu_is_primed_on_entry_and_ranked_only_after_cadence(self):
        app, _window, clock = self.make_app()
        with mock.patch.object(
                tui.cpu, "snapshot_cpu", side_effect=[{"a": 1}, {"b": 2}]
                ) as snapshot, \
                mock.patch.object(
                    tui.cpu, "rank_cpu", return_value=([], mock.Mock(
                        cpu_pct=0.0, watts=None, pkg_wakeups_per_s=0.0,
                        interrupt_wakeups_per_s=0.0))):
            app.handle_key(ord("2"))
            self.assertEqual(snapshot.call_count, 1)
            app.maybe_refresh()
            self.assertEqual(snapshot.call_count, 1)
            clock.value += 1.0
            app.maybe_refresh()
            self.assertEqual(snapshot.call_count, 2)

    def test_reentering_cpu_replaces_inactive_rate_baseline(self):
        app, _window, clock = self.make_app()
        totals = mock.Mock(
            cpu_pct=0.0, watts=None, pkg_wakeups_per_s=0.0,
            interrupt_wakeups_per_s=0.0)
        with mock.patch.object(
                tui.cpu, "snapshot_cpu",
                side_effect=[{"old": 1}, {"new": 2}, {"current": 3}]
                ) as snapshot, \
                mock.patch.object(
                    tui.cpu, "rank_cpu", return_value=([], totals)) as rank:
            app.handle_key(ord("2"))
            app.handle_key(ord("1"))
            clock.value += 10
            app.handle_key(ord("2"))
            clock.value += 1
            app.maybe_refresh()
        self.assertEqual(snapshot.call_count, 3)
        rank.assert_called_once_with(
            {"new": 2}, {"current": 3}, 1.0)

    def test_failed_disk_rate_sample_discards_baseline_before_retry(self):
        app, _window, _clock = self.make_app()
        app.disk_prev = {"old": 1}
        app.disk_prev_t = 100.0
        with mock.patch.object(
                tui.disk, "snapshot_diskio",
                side_effect=[OSError("failed"), {"new": 2}]), \
                mock.patch.object(tui.disk, "rank_io") as rank:
            app.refresh_disk(101.0)
            self.assertIsNone(app.disk_prev)
            app.refresh_disk(102.0)
        rank.assert_not_called()
        self.assertEqual(app.disk_prev, {"new": 2})
        self.assertEqual(app.disk_prev_t, 102.0)

    def test_failed_cpu_rate_sample_discards_baseline_before_retry(self):
        app, _window, _clock = self.make_app()
        app.cpu_prev = {"old": 1}
        app.cpu_prev_t = 100.0
        with mock.patch.object(
                tui.cpu, "snapshot_cpu",
                side_effect=[OSError("failed"), {"new": 2}]), \
                mock.patch.object(tui.cpu, "rank_cpu") as rank:
            app.refresh_cpu(101.0)
            self.assertIsNone(app.cpu_prev)
            app.refresh_cpu(102.0)
        rank.assert_not_called()
        self.assertEqual(app.cpu_prev, {"new": 2})
        self.assertEqual(app.cpu_prev_t, 102.0)

    def test_pause_stops_refresh_and_resume_honors_cadence(self):
        app, _window, clock = self.make_app()
        clock.value += 2
        app.paused = True
        with mock.patch.object(app, "refresh_disk") as refresh:
            app.maybe_refresh()
            refresh.assert_not_called()
            app.paused = False
            app.maybe_refresh()
            refresh.assert_called_once()

    def test_memory_point_snapshot_uses_canonical_functions(self):
        app, _window, _clock = self.make_app()
        with mock.patch.object(
                tui.memory, "snapshot_footprint", return_value={"x": (1, 2)}
                ) as snapshot, \
                mock.patch.object(
                    tui.memory, "rank_footprint", return_value=[]
                ) as rank, \
                mock.patch.object(
                    tui.memory, "system_memory",
                    return_value={"available": True, "used": 1,
                                  "pressure": "normal"}
                ) as system:
            app.handle_key(ord("3"))
        snapshot.assert_called_once()
        rank.assert_called_once()
        system.assert_called_once()

    def test_battery_uses_public_model_snapshot_and_rank(self):
        app, _window, clock = self.make_app()
        total = mock.Mock(
            energy_rate_watts=1.5, energy_score_per_s=2.5)
        health = {"present": False, "probe_error": None}
        model = {"coefficients": {"x": 1}, "source": "p",
                 "error": None, "available": True}
        with mock.patch.object(
                tui.battery, "battery_health", return_value=health), \
                mock.patch.object(
                    tui.battery, "power_model", return_value=model) as pm, \
                mock.patch.object(
                    tui.battery, "snapshot_power",
                    side_effect=[{"a": 1}, {"b": 2}]) as snapshot, \
                mock.patch.object(
                    tui.battery, "rank_top",
                    return_value=([], total)) as rank:
            app.handle_key(ord("4"))
            clock.value += 1
            app.maybe_refresh()
        pm.assert_called()
        self.assertEqual(snapshot.call_count, 2)
        rank.assert_called_once_with(
            {"a": 1}, {"b": 2}, 1.0, {"x": 1})

    def test_failed_battery_rate_sample_discards_baseline_before_retry(self):
        app, _window, _clock = self.make_app()
        app.battery_prev = {"old": 1}
        app.battery_prev_t = 100.0
        health = {"present": True, "probe_error": None, "pmset_error": None}
        model = {"coefficients": {}, "source": "p",
                 "error": None, "available": True}
        with mock.patch.object(
                tui.battery, "battery_health", return_value=health), \
                mock.patch.object(
                    tui.battery, "power_model", return_value=model), \
                mock.patch.object(
                    tui.battery, "snapshot_power",
                    side_effect=[OSError("failed"), {"new": 2}]), \
                mock.patch.object(tui.battery, "rank_top") as rank:
            app.refresh_battery(101.0)
            self.assertIsNone(app.battery_prev)
            app.refresh_battery(102.0)
        rank.assert_not_called()
        self.assertEqual(app.battery_prev, {"new": 2})
        self.assertEqual(app.battery_prev_t, 102.0)

    def test_drives_refresh_on_entry_but_not_more_often_than_five_seconds(self):
        app, _window, clock = self.make_app()
        result = {"drives": [], "partial": False, "partial_reasons": [],
                  "enumeration_error": None}
        with mock.patch.object(
                tui.smart, "collect_health", return_value=result) as collect:
            app.handle_key(ord("5"))
            self.assertEqual(collect.call_count, 1)
            app.handle_key(ord("1"))
            clock.value += 4.9
            app.handle_key(ord("5"))
            self.assertEqual(collect.call_count, 1)
            clock.value += 0.1
            app.handle_key(ord("1"))
            app.handle_key(ord("5"))
            self.assertEqual(collect.call_count, 2)


class TestDegradedStates(AppTestCase):
    def test_unknown_memory_pressure_is_never_healthy(self):
        self.assertEqual(
            tui.severity_for_memory_pressure("unknown"), "unknown")
        self.assertNotEqual(
            tui.severity_for_memory_pressure("unknown"), "healthy")
        app, window, _clock = self.make_app()
        app.tab = tui.tab_index("memory")
        app.system_memory = {
            "available": False, "errors": ["sysctl failed"],
            "used": None, "total": None, "wired": None,
            "compressed": None, "pressure": "unknown",
        }
        app.draw()
        self.assertIn("[UNKNOWN]", window.text())
        self.assertNotIn("[HEALTHY]", window.text())

    def test_partial_memory_probe_overrides_normal_pressure(self):
        app, window, _clock = self.make_app()
        app.tab = tui.tab_index("memory")
        app.system_memory = {
            "available": False, "errors": ["vm_stat failed"],
            "used": None, "total": 10, "wired": None,
            "compressed": None, "pressure": "normal",
        }
        app.draw()
        self.assertIn("[PARTIAL]", window.text())
        self.assertNotIn("[HEALTHY]", window.text())

    def test_no_battery_and_failed_probe_are_distinct(self):
        self.assertEqual(tui.severity_for_battery_health(
            {"present": False, "probe_error": None}), "absent")
        self.assertEqual(tui.severity_for_battery_health(
            {"present": None, "probe_error": "ioreg failed"}), "error")

    def test_battery_pmset_and_model_gaps_are_partial(self):
        app, window, _clock = self.make_app()
        app.tab = tui.tab_index("battery")
        app.battery_health = {
            "present": True, "probe_error": None,
            "pmset_error": "pmset failed", "condition": "Normal",
            "charge_pct": 80,
        }
        app.power_model = {
            "coefficients": None, "source": None,
            "error": "missing", "available": False,
        }
        app.draw()
        self.assertIn("[PARTIAL]", window.text())
        self.assertIn("energy model unavailable", window.text())

    def test_failed_model_refresh_cannot_reuse_stale_coefficients(self):
        app, _window, _clock = self.make_app()
        app.power_model = {
            "coefficients": {"stale": 1}, "source": "old",
            "error": None, "available": True,
        }
        health = {
            "present": True, "probe_error": None,
            "pmset_error": None, "condition": "Normal",
        }
        with mock.patch.object(
                tui.battery, "battery_health", return_value=health), \
                mock.patch.object(
                    tui.battery, "power_model",
                    side_effect=OSError("model failed")):
            app._read_battery_points()
        self.assertIsNone(app.power_model["coefficients"])
        self.assertIn("model failed", app.errors["battery"])

    def test_drive_collection_distinguishes_enumeration_failure_and_absence(self):
        with mock.patch.object(smart.probe, "find_smartctl", return_value=None), \
                mock.patch.object(
                    smart.probe, "list_physical_drives", return_value=None):
            failed = smart.collect_health()
        with mock.patch.object(smart.probe, "find_smartctl", return_value=None), \
                mock.patch.object(
                    smart.probe, "list_physical_drives", return_value=[]):
            absent = smart.collect_health()
        self.assertIsNotNone(failed["enumeration_error"])
        self.assertIsNone(absent["enumeration_error"])
        self.assertEqual(absent["drives"], [])

    def test_missing_smartctl_is_partial_not_healthy_or_failure(self):
        health = {
            "device": "disk0", "internal": True,
            "diskutil_detail": None, "smartctl_available": False,
            "smartctl_detail": "not found", "warnings": [],
            "worst_severity": "ok", "smart_status": "unknown",
        }
        with mock.patch.object(smart.probe, "find_smartctl", return_value=None), \
                mock.patch.object(
                    smart.probe, "list_physical_drives",
                    return_value=[("disk0", True)]), \
                mock.patch.object(
                    smart, "drive_health", return_value=health):
            result = smart.collect_health()
        self.assertTrue(result["partial"])
        self.assertIn("smartctl_unavailable", result["partial_reasons"])
        self.assertEqual(tui.severity_for_drive(health), "partial")
        self.assertEqual(tui.drive_verdict(health), "PARTIAL")


class TestSmartAlignment(unittest.TestCase):
    def test_header_and_row_fields_align_location_before_verdict(self):
        health = {
            "device": "disk0", "name": "Model", "internal": True,
            "smart_status": "verified", "worst_severity": "ok",
            "percentage_used": 3, "temperature_c": 41,
        }
        fields = tui.smart_row_fields(health)
        self.assertEqual(tui.SMART_HEADERS, (
            "DEVICE", "MODEL", "LOCATION", "VERDICT", "WEAR", "TEMP"))
        self.assertEqual(fields[2], "internal")
        self.assertEqual(fields[3], "HEALTHY")
        header_positions = [
            tui.format_smart_header().index(value)
            for value in tui.SMART_HEADERS
        ]
        row = tui.format_smart_row(health)
        self.assertEqual(row[header_positions[2]:].split()[0], "internal")
        self.assertEqual(row[header_positions[3]:].split()[0], "HEALTHY")

    def test_explicit_smartctl_unavailability_is_partial(self):
        health = {
            "smart_status": "verified", "worst_severity": "ok",
            "smartctl_available": False,
        }
        self.assertEqual(tui.drive_verdict(health), "PARTIAL")


class TestDrawingAndActions(AppTestCase):
    def test_external_process_text_is_sanitized_before_output(self):
        app, window, _clock = self.make_app()
        app.disk_rows = [disk_row(name="evil\nname\x1b")]
        app.draw()
        self.assertNotIn("\x1b", window.text())
        self.assertIn("evil?name?", window.text())

    def test_every_tab_draws_on_very_narrow_short_terminal(self):
        app, window, _clock = self.make_app(window=FakeWindow(2, 3))
        app.memory_rows = [(1, 2, 3, "x")]
        app.battery_rows = []
        app.drive_collection = {"drives": [], "enumeration_error": None}
        for index in range(len(tui.TABS)):
            app.tab = index
            app.draw()
        window.height, window.width = 40, 180
        app.draw()

    def test_title_layout_never_overlaps_or_exceeds_terminal(self):
        for width in range(1, 121):
            title = tui.format_title(
                width, "memory", True, "12:34:56")
            self.assertLessEqual(len(title), max(0, width - 1))
        wide = tui.format_title(120, "memory", True, "12:34:56")
        self.assertIn("[3]memory", wide)
        self.assertTrue(wide.endswith("root 12:34:56"))

    def test_drive_detail_row_does_not_overwrite_selected_drive(self):
        app, window, _clock = self.make_app(window=FakeWindow(8, 100))
        app.tab = tui.tab_index("smart")
        app.selection["smart"] = 1
        drive = {
            "name": "Model", "internal": True, "smart_status": "verified",
            "worst_severity": "ok", "smartctl_available": True,
            "smartctl_detail": None, "diskutil_detail": "partial detail",
            "warnings": [], "percentage_used": 1, "temperature_c": 30,
        }
        app.drive_collection = {
            "drives": [
                dict(drive, device="disk0"),
                dict(drive, device="disk1"),
            ],
            "partial": True, "partial_reasons": ["diskutil_probe_incomplete"],
            "enumeration_error": None,
        }
        app.draw()
        selected_rows = [
            text for y, _x, text, _attr in window.draws if y == 5]
        detail_rows = [
            text for y, _x, text, _attr in window.draws if y == 6]
        self.assertTrue(any("disk1" in text for text in selected_rows))
        self.assertTrue(any("partial detail" in text for text in detail_rows))

    def test_drive_detail_prefers_critical_warning(self):
        app, window, _clock = self.make_app(window=FakeWindow(8, 120))
        app.tab = tui.tab_index("smart")
        app.drive_collection = {
            "drives": [{
                "device": "disk0", "name": "Model", "internal": True,
                "smart_status": "failing", "worst_severity": "critical",
                "smartctl_available": True, "smartctl_detail": None,
                "diskutil_detail": None, "percentage_used": 1,
                "temperature_c": 30,
                "warnings": [
                    {"severity": "warn", "code": "wear",
                     "message": "wear warning"},
                    {"severity": "critical", "code": "media",
                     "message": "media failure"},
                ],
            }],
            "partial": False, "partial_reasons": [],
            "enumeration_error": None,
        }
        app.draw()
        self.assertIn("media failure", window.text())
        self.assertNotIn("wear warning", window.text())

    def test_files_action_uses_scope_helper_for_selected_process(self):
        app, _window, _clock = self.make_app()
        app.disk_rows = [disk_row(42)]
        with mock.patch.object(
                tui.disk, "open_files",
                return_value=[("open", "REG", "/bad\npath")]) as files, \
                mock.patch.object(app, "popup") as popup:
            app.act_files()
        files.assert_called_once_with(42)
        self.assertIn("/bad\npath", popup.call_args[0][1][0])

    def test_inspect_nonzero_exit_is_retained_in_footer(self):
        app, _window, _clock = self.make_app()
        app.disk_rows = [disk_row(42)]
        with redirect_stdout(io.StringIO()), \
                mock.patch.object(tui.curses, "def_prog_mode"), \
                mock.patch.object(tui.curses, "endwin"), \
                mock.patch.object(tui.curses, "reset_prog_mode"), \
                mock.patch.object(
                    tui.disk, "cmd_inspect",
                    return_value=cli.EXIT_PERMISSION), \
                mock.patch("builtins.input", return_value=""):
            app.act_inspect()
        self.assertEqual(app.msg, "inspect exited with status 3")

    def test_disk_subview_only_scans_volumes_when_requested(self):
        app, _window, _clock = self.make_app()
        with mock.patch.object(
                tui.disk, "_mount_table",
                return_value=[("/dev/disk2", "/Volumes/X")]) as mounts:
            app.handle_key(ord("v"))
        mounts.assert_called_once()
        self.assertEqual(app.volumes, [("/dev/disk2", "/Volumes/X")])


class TTYBuffer(io.StringIO):
    def isatty(self):
        return True


class TestEntryPoints(unittest.TestCase):
    def test_help_usage_and_non_tty_exit_contract(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(tui.main(["stethoscope tui", "--help"]), 0)
        self.assertIn("1-5", output.getvalue())

        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(tui.main(["stethoscope tui", "extra"]), 2)
        self.assertEqual(tui.main(["stethoscope tui"]), cli.EXIT_ERROR)

    def test_curses_startup_failure_is_exit_error_and_term_fallback(self):
        output = TTYBuffer()
        with redirect_stdout(output), \
                mock.patch.object(
                    tui.curses, "wrapper",
                    side_effect=curses.error("setupterm")), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(tui.main(["stethoscope tui"]), cli.EXIT_ERROR)
            self.assertEqual(os.environ.get("TERM"), "xterm-256color")

    def test_disk_compatibility_wrapper_focuses_disk_and_forwards_args(self):
        with mock.patch.object(
                disk_tui.tui, "main", return_value=7) as unified:
            result = disk_tui.main(["stethoscope disk tui", "--help"])
        self.assertEqual(result, 7)
        unified.assert_called_once_with(
            ["stethoscope disk tui", "--help"], initial_tab="disk")

    def test_dispatcher_registers_and_routes_top_level_and_disk_tui(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        namespace = runpy.run_path(os.path.join(root, "stethoscope"))
        self.assertEqual(namespace["SCOPES"]["tui"]["module"], "scopes.tui")
        module = mock.Mock()
        module.main.return_value = 9
        with mock.patch.dict(
                namespace["main"].__globals__,
                {"_load": mock.Mock(return_value=module)}):
            result = namespace["main"](["stethoscope", "tui"])
        self.assertEqual(result, 9)
        module.main.assert_called_once_with(["stethoscope tui"])

        module.reset_mock()
        with mock.patch.dict(
                namespace["main"].__globals__,
                {"_load": mock.Mock(return_value=module)}):
            result = namespace["main"](
                ["stethoscope", "disk", "tui", "--help"])
        self.assertEqual(result, 9)
        module.main.assert_called_once_with(
            ["stethoscope disk tui", "--help"])

    def test_real_pty_startup_and_quit(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        master, slave = pty.openpty()
        script = (
            "from scopes import tui\n"
            "tui.disk.snapshot_diskio = lambda: {}\n"
            "def smoke(app):\n"
            "    app.draw()\n"
            "    assert app.handle_key(ord('q')) is False\n"
            "tui.App.run = smoke\n"
            "raise SystemExit(tui.main(['stethoscope tui']))\n"
        )
        environment = dict(os.environ)
        environment["TERM"] = "xterm-256color"
        process = subprocess.Popen(
            [sys.executable, "-c", script], cwd=root,
            stdin=slave, stdout=slave, stderr=slave,
            close_fds=True, env=environment)
        os.close(slave)
        transcript = []
        try:
            deadline = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                ready, _writable, _exceptional = select.select(
                    [master], [], [], 0.1)
                if not ready:
                    continue
                try:
                    transcript.append(os.read(master, 4096))
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    break
            if process.poll() is None:
                self.fail(
                    "TUI PTY smoke timed out: %s" %
                    b"".join(transcript).decode("utf-8", "replace"))
            return_code = process.wait(timeout=1)
        finally:
            os.close(master)
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        self.assertEqual(return_code, 0)


if __name__ == "__main__":
    unittest.main()
