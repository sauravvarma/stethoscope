"""Hermetic cpu-scope tests.

Every kernel boundary this file touches — libproc via core.rusage — is
faked with unittest.mock, so this suite runs on any macOS box without root
or live processes. tests/test_core.py is deliberately left as the place
where proc_pid_rusage / mach_absolute_time are exercised for real (struct
layout, timebase conversion, live snapshot/rank behavior); nothing here
duplicates those. This file covers the pure data/document/CLI layer:
sort modes, document shape, the --json contract, and the human frame.
"""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from core import cli, schema
from scopes import cpu


def _row(pid=10, start=111, cpu_pct=0.0, user_pct=0.0, system_pct=0.0,
         watts=None, total_cpu_ns=0, pkg=0.0, intr=0.0, name="proc"):
    total_wake = pkg + intr
    return cpu.CpuRow(cpu_pct, user_pct, system_pct, watts, total_cpu_ns,
                      start, pid, name, pkg, intr, total_wake)


class TestFormatting(unittest.TestCase):
    def test_cpu_time_str_bands(self):
        self.assertEqual(cpu.cpu_time_str(3.2 * 1e9), "3.2s")
        self.assertEqual(cpu.cpu_time_str(65 * 1e9), "1m05s")
        self.assertEqual(cpu.cpu_time_str((3600 + 12 * 60) * 1e9), "1h12m")

    def test_watts_str_none_is_dash(self):
        self.assertEqual(cpu.watts_str(None), "-")
        self.assertEqual(cpu.watts_str(1.5), "1.50W")


class TestDiffCounters(unittest.TestCase):
    def test_pkg_and_interrupt_diffed_independently(self):
        prev = {(10, 111): (0, 0, None, 100, 1000)}
        cur = {(10, 111): (0, 0, None, 120, 1200)}
        rows, sys_totals = cpu._diff_cpu(prev, cur, 2.0)
        self.assertAlmostEqual(rows[0].pkg_wakeups_per_s, 10.0)
        self.assertAlmostEqual(rows[0].interrupt_wakeups_per_s, 100.0)
        self.assertAlmostEqual(rows[0].total_wakeups_per_s, 110.0)
        self.assertAlmostEqual(sys_totals.pkg_wakeups_per_s, 10.0)
        self.assertAlmostEqual(sys_totals.interrupt_wakeups_per_s, 100.0)

    def test_wakeup_counters_clamp_on_negative_delta(self):
        prev = {(10, 111): (0, 0, None, 500, 500)}
        cur = {(10, 111): (0, 0, None, 400, 600)}
        rows, _ = cpu._diff_cpu(prev, cur, 1.0)
        self.assertAlmostEqual(rows[0].pkg_wakeups_per_s, 0.0)
        self.assertAlmostEqual(rows[0].interrupt_wakeups_per_s, 100.0)


class TestSortModes(unittest.TestCase):
    """rank_cpu ranks by %CPU; rank_wakeups ranks by total wakeup rate —
    but every row keeps pkg-idle and interrupt as separate fields either
    way (S8, casebook 0004): detectors baseline them separately, so the
    sum must never be the only wakeup number a row carries.
    """

    PREV = {
        (10, 111): (0, 0, None, 0, 0),      # low cpu, high wakeups
        (20, 222): (0, 0, None, 0, 0),      # high cpu, low wakeups
    }
    CUR = {
        (10, 111): (100_000_000, 0, None, 5, 995),   # 5% cpu-ish, 1000 wake/s
        (20, 222): (900_000_000, 0, None, 1, 1),     # 90%-ish cpu, 2 wake/s
    }

    def test_rank_cpu_sorts_by_cpu_pct_descending(self):
        rows, _ = cpu.rank_cpu(self.PREV, self.CUR, 1.0)
        self.assertEqual([r.pid for r in rows], [20, 10])

    def test_rank_wakeups_sorts_by_total_wakeups_descending(self):
        rows, _ = cpu.rank_wakeups(self.PREV, self.CUR, 1.0)
        self.assertEqual([r.pid for r in rows], [10, 20])
        # Both counters remain individually addressable on the winning row.
        self.assertAlmostEqual(rows[0].pkg_wakeups_per_s, 5.0)
        self.assertAlmostEqual(rows[0].interrupt_wakeups_per_s, 995.0)
        self.assertAlmostEqual(rows[0].total_wakeups_per_s, 1000.0)

    def test_same_underlying_rows_different_order_only(self):
        top_rows, _ = cpu.rank_cpu(self.PREV, self.CUR, 1.0)
        wake_rows, _ = cpu.rank_wakeups(self.PREV, self.CUR, 1.0)
        self.assertEqual(set(r.pid for r in top_rows), set(r.pid for r in wake_rows))


class TestDocument(unittest.TestCase):
    def test_process_entry_has_required_fields(self):
        row = _row(pid=7, start=100, cpu_pct=12.5, user_pct=8.0, system_pct=4.5,
                   watts=2.0, total_cpu_ns=3_000_000_000, pkg=1.0, intr=9.0,
                   name="writer")
        entry = cpu._process_entry(row, now_ticks=100 + 4_000_000_000)
        for field in ("pid", "name", "cpu_pct", "user_pct", "system_pct",
                      "watts", "total_cpu_seconds", "lifetime_duty_pct",
                      "pkg_idle_wakeups_per_s", "interrupt_wakeups_per_s",
                      "total_wakeups_per_s"):
            self.assertIn(field, entry)
        self.assertEqual(entry["pid"], 7)
        self.assertEqual(entry["name"], "writer")
        self.assertAlmostEqual(entry["total_cpu_seconds"], 3.0)
        self.assertAlmostEqual(entry["pkg_idle_wakeups_per_s"], 1.0)
        self.assertAlmostEqual(entry["interrupt_wakeups_per_s"], 9.0)
        self.assertAlmostEqual(entry["total_wakeups_per_s"], 10.0)

    def test_watts_absent_is_null_not_zero(self):
        row = _row(watts=None)
        entry = cpu._process_entry(row, now_ticks=1)
        self.assertIsNone(entry["watts"])

    def test_watts_present_is_passed_through(self):
        row = _row(watts=3.25)
        entry = cpu._process_entry(row, now_ticks=1)
        self.assertEqual(entry["watts"], 3.25)

    def test_document_shape_and_non_root_partial(self):
        rows = [_row(pid=9, name="hog", cpu_pct=50.0)]
        sys_totals = cpu.SysTotals(50.0, 1.0, 2.0, 3.0, 5.0)
        with mock.patch.object(cli, "is_root", return_value=False):
            document = cpu._document("top", rows, sys_totals, ncpu=8, limit=20,
                                     now_ticks=1_000_000)
        self.assertEqual(document["schema"], schema.SCHEMA_VERSION)
        self.assertEqual(document["scope"], "cpu")
        self.assertEqual(document["command"], "top")
        self.assertTrue(document["partial"])
        self.assertEqual(document["partial_reasons"], ["not_root"])
        self.assertEqual(document["system"], {
            "cpu_pct": 50.0, "watts": 1.0, "pkg_idle_wakeups_per_s": 2.0,
            "interrupt_wakeups_per_s": 3.0, "total_wakeups_per_s": 5.0,
            "ncpu": 8,
        })
        self.assertEqual(document["processes"][0]["pid"], 9)

    def test_document_root_is_not_partial(self):
        with mock.patch.object(cli, "is_root", return_value=True):
            document = cpu._document(
                "wakeups", [], cpu.SysTotals(0.0, None, 0.0, 0.0, 0.0),
                ncpu=4, limit=20, now_ticks=1)
        self.assertFalse(document["partial"])
        self.assertEqual(document["partial_reasons"], [])

    def test_document_respects_limit(self):
        rows = [_row(pid=i, name="p%d" % i, cpu_pct=float(i)) for i in range(5)]
        with mock.patch.object(cli, "is_root", return_value=True):
            document = cpu._document("top", rows, cpu.SysTotals(0, None, 0, 0, 0),
                                     ncpu=1, limit=2, now_ticks=1)
        self.assertEqual(len(document["processes"]), 2)


class TestFrame(unittest.TestCase):
    def test_wake_column_uses_total_not_idle_only(self):
        # Copilot #40: WAKE/s must equal the same total rate used to rank
        # the wakeups view, never the idle-only counter.
        row = _row(pid=5, name="loop", pkg=1.0, intr=799.0)
        frame = cpu._frame("wakeups", [row], cpu.SysTotals(0, None, 1.0, 799.0, 800.0),
                           ncpu=4, interval=1.0, limit=20, now_ticks=1, styled=False)
        lines = [ln for ln in frame.splitlines() if "5  " in ln]
        self.assertEqual(len(lines), 1)
        line = lines[0]
        cols = line.split()
        wake_value = float(cols[cols.index("loop") + 7])   # WAKE/s column
        self.assertAlmostEqual(wake_value, 800.0)
        self.assertNotAlmostEqual(wake_value, 1.0)   # not the pkg-idle-only value

    def test_pkg_and_interrupt_remain_visible_separately(self):
        row = _row(pid=5, name="loop", pkg=1.0, intr=799.0)
        frame = cpu._frame("wakeups", [row], cpu.SysTotals(0, None, 1.0, 799.0, 800.0),
                           ncpu=4, interval=1.0, limit=20, now_ticks=1, styled=False)
        self.assertIn("PKG/s", frame)
        self.assertIn("INTR/s", frame)
        self.assertIn("WAKE/s", frame)

    def test_unstyled_frame_has_no_terminal_control_codes(self):
        frame = cpu._frame("top", [], cpu.SysTotals(0, None, 0, 0, 0),
                           ncpu=4, interval=1.0, limit=20, now_ticks=1, styled=False)
        self.assertNotIn("\033", frame)

    def test_watts_dash_when_absent_in_frame(self):
        row = _row(pid=1, name="x", watts=None)
        frame = cpu._frame("top", [row], cpu.SysTotals(0, None, 0, 0, 0),
                           ncpu=1, interval=1.0, limit=20, now_ticks=1, styled=False)
        self.assertIn(" - ", frame + " ")


class TestUsageText(unittest.TestCase):
    def test_help_does_not_advertise_a_permission_exit_code(self):
        # Copilot #40: cpu never returns EXIT_PERMISSION (3) — top/wakeups
        # keep running and mark --json partial instead of failing — so the
        # help text must not claim otherwise.
        self.assertNotIn("permission", cpu.USAGE.lower())
        self.assertNotIn("3 ", cpu.USAGE)
        self.assertNotIn("needs root", cpu.USAGE.lower())


class TestCLIContract(unittest.TestCase):
    def test_top_once_emits_exactly_one_document(self):
        snapshots = [
            {(7, 70): (0, 0, None, 0, 0)},
            {(7, 70): (100_000_000, 0, None, 10, 0)},
        ]
        with mock.patch.object(cpu, "snapshot_cpu", side_effect=snapshots), \
                mock.patch.object(cpu.time, "sleep"), \
                mock.patch.object(cpu.time, "monotonic", side_effect=[10.0, 11.0]), \
                mock.patch.object(cli, "is_root", return_value=True):
            options = cli.parse_options(["--json", "--once"])
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = cpu.cmd_top(options)
        self.assertEqual(result, cli.EXIT_OK)
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        document = json.loads(lines[0])
        self.assertEqual(document["command"], "top")

    def test_wakeups_once_emits_exactly_one_document(self):
        snapshots = [
            {(7, 70): (0, 0, None, 0, 0)},
            {(7, 70): (0, 0, None, 50, 5)},
        ]
        with mock.patch.object(cpu, "snapshot_cpu", side_effect=snapshots), \
                mock.patch.object(cpu.time, "sleep"), \
                mock.patch.object(cpu.time, "monotonic", side_effect=[10.0, 11.0]), \
                mock.patch.object(cli, "is_root", return_value=True):
            options = cli.parse_options(["--json", "--once"])
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = cpu.cmd_wakeups(options)
        self.assertEqual(result, cli.EXIT_OK)
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        document = json.loads(lines[0])
        self.assertEqual(document["command"], "wakeups")
        self.assertEqual(document["processes"][0]["pkg_idle_wakeups_per_s"], 50.0)
        self.assertEqual(document["processes"][0]["interrupt_wakeups_per_s"], 5.0)

    def test_duration_repeats_until_elapsed(self):
        snapshots = [
            {(7, 70): (0, 0, None, 0, 0)},
            {(7, 70): (100_000_000, 0, None, 0, 0)},
            {(7, 70): (200_000_000, 0, None, 0, 0)},
        ]
        with mock.patch.object(cpu, "snapshot_cpu", side_effect=snapshots), \
                mock.patch.object(cpu.time, "sleep") as sleep, \
                mock.patch.object(cpu.time, "monotonic",
                                  side_effect=[10.0, 10.5, 11.0]), \
                mock.patch.object(cli, "is_root", return_value=True):
            options = cli.parse_options(["--json", "--duration", "0.8",
                                         "--interval", "0.5"])
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = cpu.cmd_top(options)
        self.assertEqual(result, cli.EXIT_OK)
        self.assertEqual(len(stream.getvalue().splitlines()), 2)
        self.assertEqual(sleep.call_count, 2)

    def test_main_rejects_extra_positionals(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = cpu.main(["stethoscope cpu", "top", "bogus"])
        self.assertEqual(result, cli.EXIT_USAGE)
        self.assertIn("needs 0 argument", stderr.getvalue())

    def test_main_rejects_bad_numeric_option(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = cpu.main(["stethoscope cpu", "top", "--interval", "fast"])
        self.assertEqual(result, cli.EXIT_USAGE)
        self.assertIn("--interval", stderr.getvalue())

    def test_main_rejects_non_positive_interval(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = cpu.main(["stethoscope cpu", "wakeups", "--interval", "0"])
        self.assertEqual(result, cli.EXIT_USAGE)

    def test_main_rejects_unknown_option(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = cpu.main(["stethoscope cpu", "top", "--nope"])
        self.assertEqual(result, cli.EXIT_USAGE)

    def test_main_unknown_mode(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = cpu.main(["stethoscope cpu", "bogus"])
        self.assertEqual(result, cli.EXIT_USAGE)
        self.assertIn("unknown mode", stderr.getvalue())

    def test_main_dispatches_wakeups_mode(self):
        with mock.patch.object(cpu, "cmd_wakeups", return_value=cli.EXIT_OK) as wk, \
                mock.patch.object(cpu, "cmd_top") as top:
            result = cpu.main(["stethoscope cpu", "wakeups", "--once"])
        self.assertEqual(result, cli.EXIT_OK)
        self.assertTrue(wk.called)
        self.assertFalse(top.called)


if __name__ == "__main__":
    unittest.main()
