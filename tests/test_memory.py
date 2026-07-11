"""Hermetic tests for the memory scope.

Every kernel/subprocess boundary this file touches — libproc (via
core.rusage), `vm_stat`, `sysctl`, and `os.kill` — is faked with
unittest.mock, so this suite runs on any macOS box without root or a real
leaking process. Live core probe contracts (struct layout, timebase
conversion) belong in tests/test_core.py, not here; this file covers the
memory scope's own data/parse/detector layer plus its JSON/exit-code
contract, matching tests/test_contract.py's pattern for disk.
"""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from core import cli, vmstat
from scopes import memory

MB = 1024 * 1024


def _rusage_info(footprint, resident, start=100):
    """A stand-in for the ctypes struct core.rusage._raw_rusage returns —
    only the two fields this scope reads, plus the identity field."""
    info = mock.Mock()
    info.ri_phys_footprint = footprint
    info.ri_resident_size = resident
    info.ri_proc_start_abstime = start
    return info


# ---------------------------------------------------------------------------
# core/vmstat.py — vm_stat text / sysctl parsing
# ---------------------------------------------------------------------------

class TestParseVmStat(unittest.TestCase):
    def test_page_size_multiplies_every_count(self):
        text = (
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
            "Pages free:                    10.\n"
            "Pages active:                   5.\n"
        )
        counts = vmstat.parse_vm_stat(text)
        self.assertEqual(counts["pages free"], 10 * 16384)
        self.assertEqual(counts["pages active"], 5 * 16384)

    def test_default_page_size_when_header_missing(self):
        text = "Pages free:                    10.\n"
        counts = vmstat.parse_vm_stat(text)
        self.assertEqual(counts["pages free"], 10 * 4096)

    def test_malformed_lines_are_skipped_not_raised(self):
        text = (
            "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
            "garbage line with no colon\n"
            "Pages free:                    ??\n"      # non-digit value
            "Pages wired down:              7.\n"
        )
        counts = vmstat.parse_vm_stat(text)
        self.assertNotIn("pages free", counts)
        self.assertEqual(counts["pages wired down"], 7 * 4096)

    def test_empty_text_yields_empty_counts(self):
        self.assertEqual(vmstat.parse_vm_stat(""), {})


class TestPressureName(unittest.TestCase):
    def test_known_levels(self):
        self.assertEqual(vmstat.pressure_name(1), "normal")
        self.assertEqual(vmstat.pressure_name(2), "warn")
        self.assertEqual(vmstat.pressure_name(4), "critical")

    def test_unknown_level_and_missing_read(self):
        self.assertEqual(vmstat.pressure_name(3), "unknown")
        self.assertEqual(vmstat.pressure_name(99), "unknown")
        self.assertEqual(vmstat.pressure_name(None), "unknown")


class TestProbeExecution(unittest.TestCase):
    def test_missing_binary_is_explicit(self):
        with mock.patch.object(vmstat.subprocess, "run",
                               side_effect=FileNotFoundError()):
            with self.assertRaisesRegex(vmstat.ProbeError, "unavailable"):
                vmstat._run([vmstat.VM_STAT])

    def test_nonzero_exit_is_explicit(self):
        completed = mock.Mock(returncode=1, stdout="", stderr="failed")
        with mock.patch.object(vmstat.subprocess, "run",
                               return_value=completed):
            with self.assertRaisesRegex(vmstat.ProbeError, "failed"):
                vmstat._run([vmstat.VM_STAT])

    def test_invalid_sysctl_value_is_explicit(self):
        with mock.patch.object(vmstat, "_run", return_value="not-an-int"):
            with self.assertRaisesRegex(vmstat.ProbeError, "sysctl_invalid"):
                vmstat._sysctl_int("hw.memsize")


class TestSystemMemory(unittest.TestCase):
    VM_STAT_TEXT = (
        "Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
        "Pages free:                    100.\n"
        "Pages active:                  200.\n"
        "Pages inactive:                 50.\n"
        "Pages wired down:               75.\n"
        "Pages occupied by compressor:   25.\n"
    )

    def test_structure_and_values(self):
        def fake_run(cmd):
            if cmd == [vmstat.VM_STAT]:
                return self.VM_STAT_TEXT
            if cmd == [vmstat.SYSCTL, "-n", "hw.memsize"]:
                return "1000000\n"
            if cmd == [vmstat.SYSCTL, "-n", "kern.memorystatus_vm_pressure_level"]:
                return "2\n"
            self.fail("unexpected command: %r" % (cmd,))

        with mock.patch.object(vmstat, "_run", side_effect=fake_run):
            summary = vmstat.system_memory()

        self.assertEqual(summary, {
            "available": True,
            "errors": [],
            "total": 1000000,
            "used": (200 + 75 + 25) * 4096,
            "free": 100 * 4096,
            "active": 200 * 4096,
            "inactive": 50 * 4096,
            "wired": 75 * 4096,
            "compressed": 25 * 4096,
            "pressure": "warn",
        })

    def test_missing_probes_are_explicit_and_nullable(self):
        with mock.patch.object(
                vmstat, "_run", side_effect=vmstat.ProbeError("probe_failed")):
            summary = vmstat.system_memory()
        self.assertFalse(summary["available"])
        self.assertIsNone(summary["total"])
        self.assertIsNone(summary["used"])
        self.assertEqual(summary["pressure"], "unknown")
        self.assertEqual(summary["errors"],
                         ["probe_failed", "probe_failed", "probe_failed"])

    def test_every_key_present_even_on_total_failure(self):
        with mock.patch.object(
                vmstat, "_run", side_effect=vmstat.ProbeError("probe_failed")):
            summary = vmstat.system_memory()
        self.assertEqual(
            set(summary),
            {"available", "errors", "total", "used", "free", "active",
             "inactive", "wired", "compressed", "pressure"})


# ---------------------------------------------------------------------------
# ranking and identity
# ---------------------------------------------------------------------------

class TestRankFootprint(unittest.TestCase):
    def test_sorted_descending_and_carries_identity(self):
        snap = {(10, 111): (2 * MB, 1 * MB), (20, 222): (5 * MB, 4 * MB)}
        names = {(10, 111): "small", (20, 222): "big"}
        with mock.patch.object(memory, "proc_name", side_effect=lambda pid, key: names[key]):
            rows = memory.rank_footprint(snap)
        self.assertEqual(rows, [
            (5 * MB, 4 * MB, 20, "big"),
            (2 * MB, 1 * MB, 10, "small"),
        ])

    def test_empty_snapshot_yields_no_rows(self):
        self.assertEqual(memory.rank_footprint({}), [])

    def test_pid_reuse_keeps_both_identities_distinct(self):
        # Same bare pid, two different start_abstimes: both rows are kept,
        # not merged (S10) — rank_footprint has no notion of "the same pid".
        snap = {(10, 111): (1 * MB, 1 * MB), (10, 222): (3 * MB, 3 * MB)}
        with mock.patch.object(memory, "proc_name", return_value="p"):
            rows = memory.rank_footprint(snap)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], 3 * MB)


class TestSnapshotFootprint(unittest.TestCase):
    def test_uses_raw_rusage_keyed_by_identity(self):
        info = _rusage_info(7 * MB, 6 * MB, start=555)
        with mock.patch.object(memory, "list_pids", return_value=[42]), \
                mock.patch.object(memory.rusage, "_raw_rusage", return_value=info):
            snap = memory.snapshot_footprint()
        self.assertEqual(snap, {(42, 555): (7 * MB, 6 * MB)})

    def test_inaccessible_pid_excluded(self):
        with mock.patch.object(memory, "list_pids", return_value=[1, 2]), \
                mock.patch.object(memory.rusage, "_raw_rusage",
                                   side_effect=[None, _rusage_info(MB, MB)]):
            snap = memory.snapshot_footprint()
        self.assertEqual(len(snap), 1)


# ---------------------------------------------------------------------------
# slope / plateau / leak-latch detector
# ---------------------------------------------------------------------------

class TestSlope(unittest.TestCase):
    def test_fewer_than_two_samples_is_zero(self):
        self.assertEqual(memory.slope_mb_per_min([]), 0.0)
        self.assertEqual(memory.slope_mb_per_min([(0, 0)]), 0.0)

    def test_linear_growth_matches_expected_rate(self):
        # 2 MB/s == 120 MB/min, over a plain two-point line.
        samples = [(0, 0), (60, 120 * MB)]
        self.assertAlmostEqual(memory.slope_mb_per_min(samples), 120.0)

    def test_degenerate_time_axis_is_zero(self):
        # Every sample at the same timestamp: no time axis to regress on.
        samples = [(5, 0), (5, MB), (5, 2 * MB)]
        self.assertEqual(memory.slope_mb_per_min(samples), 0.0)

    def test_flat_footprint_is_zero_slope(self):
        samples = [(t, 10 * MB) for t in range(5)]
        self.assertEqual(memory.slope_mb_per_min(samples), 0.0)

    def test_negative_slope_for_shrinking_footprint(self):
        samples = [(0, 60 * MB), (60, 0)]
        self.assertAlmostEqual(memory.slope_mb_per_min(samples), -60.0)


class TestPlateau(unittest.TestCase):
    def test_false_before_window_is_filled(self):
        samples = [(t, t * MB) for t in range(memory.PLATEAU_WINDOW - 1)]
        self.assertFalse(memory.is_plateaued(samples))

    def test_true_when_recent_window_is_flat(self):
        samples = [(t, 40 * MB) for t in range(memory.PLATEAU_WINDOW)]
        self.assertTrue(memory.is_plateaued(samples))

    def test_false_when_recent_window_still_climbing(self):
        samples = [(t, t * 2 * MB) for t in range(memory.PLATEAU_WINDOW)]
        self.assertFalse(memory.is_plateaued(samples))

    def test_overall_growth_with_flat_tail_is_plateaued(self):
        # Grew fast early, then flattened for the whole recent window: the
        # all-time average slope stays high, but the trend itself stopped.
        head = [(0, 0), (1, 10 * MB), (2, 20 * MB), (3, 30 * MB), (4, 40 * MB)]
        tail = [(t, 40 * MB) for t in range(5, 5 + memory.PLATEAU_WINDOW)]
        samples = head + tail
        self.assertGreater(memory.slope_mb_per_min(samples),
                            memory.LEAK_SLOPE_MB_PER_MIN)
        self.assertTrue(memory.is_plateaued(samples))


class TestLeakState(unittest.TestCase):
    def test_too_few_samples_never_trips(self):
        samples = [(t, t * 10 * MB) for t in range(memory.MIN_LEAK_SAMPLES - 1)]
        slope, plateau, latched = memory.leak_state(samples, False)
        self.assertGreater(slope, memory.LEAK_SLOPE_MB_PER_MIN)
        self.assertFalse(latched)

    def test_sustained_growth_trips_candidate(self):
        samples = [(t, t * 10 * MB) for t in range(memory.MIN_LEAK_SAMPLES)]
        slope, plateau, latched = memory.leak_state(samples, False)
        self.assertFalse(plateau)
        self.assertTrue(latched)

    def test_plateau_blocks_a_new_trip(self):
        head = [(0, 0), (1, 10 * MB), (2, 20 * MB), (3, 30 * MB), (4, 40 * MB)]
        tail = [(t, 40 * MB) for t in range(5, 5 + memory.PLATEAU_WINDOW)]
        samples = head + tail
        slope, plateau, latched = memory.leak_state(samples, False)
        self.assertTrue(plateau)
        self.assertFalse(latched)

    def test_latch_stays_true_once_tripped_even_after_plateau(self):
        # PR #41 review: a per-sample flip-flop is not an acceptable
        # "sustained ... flags" contract. Once latched=True is fed back in,
        # a later plateaued/flat sample must not clear it.
        flat_samples = [(t, 40 * MB) for t in range(memory.PLATEAU_WINDOW)]
        slope, plateau, latched = memory.leak_state(flat_samples, True)
        self.assertTrue(plateau)
        self.assertTrue(latched)

    def test_steady_process_never_trips(self):
        samples = [(t, 10 * MB) for t in range(20)]
        latched = False
        for i in range(2, len(samples) + 1):
            _, _, latched = memory.leak_state(samples[:i], latched)
        self.assertFalse(latched)


class TestSparkline(unittest.TestCase):
    def test_empty_is_empty(self):
        self.assertEqual(memory.sparkline([]), "")

    def test_constant_values_use_lowest_block(self):
        self.assertEqual(memory.sparkline([5, 5, 5]), memory._SPARK[0] * 3)

    def test_spans_full_range_low_to_high(self):
        spark = memory.sparkline([0, 50, 100])
        self.assertEqual(spark[0], memory._SPARK[0])
        self.assertEqual(spark[-1], memory._SPARK[-1])


# ---------------------------------------------------------------------------
# pid accessibility: gone (ESRCH) vs denied (EPERM)
# ---------------------------------------------------------------------------

class TestPidStatus(unittest.TestCase):
    def test_esrch_is_gone(self):
        with mock.patch.object(memory.os, "kill",
                                side_effect=ProcessLookupError()):
            self.assertEqual(memory.pid_status(999999), "gone")

    def test_eperm_is_denied(self):
        with mock.patch.object(memory.os, "kill",
                                side_effect=PermissionError()):
            self.assertEqual(memory.pid_status(1), "denied")

    def test_success_is_present(self):
        with mock.patch.object(memory.os, "kill", return_value=None):
            self.assertEqual(memory.pid_status(42), "present")


# ---------------------------------------------------------------------------
# top: JSON contract, partial visibility, once/duration
# ---------------------------------------------------------------------------

class TestTopContract(unittest.TestCase):
    def test_document_marks_non_root_visibility_partial(self):
        rows = [(5 * MB, 4 * MB, 7, "writer")]
        sysmem = {"total": 1, "used": 1, "free": 0, "active": 0,
                   "inactive": 0, "wired": 0, "compressed": 0,
                   "pressure": "normal"}
        with mock.patch.object(cli, "is_root", return_value=False):
            document = memory._top_document(rows, sysmem, 20)
        self.assertTrue(document["partial"])
        self.assertEqual(document["partial_reasons"], ["not_root"])
        self.assertEqual(document["processes"][0]["pid"], 7)
        self.assertEqual(document["processes"][0]["footprint_bytes"], 5 * MB)
        self.assertEqual(document["processes"][0]["resident_size_bytes"], 4 * MB)

    def test_document_marks_system_probe_failure_partial(self):
        sysmem = {
            "available": False,
            "errors": ["vm_stat_failed"],
            "total": None,
            "used": None,
            "free": None,
            "active": None,
            "inactive": None,
            "wired": None,
            "compressed": None,
            "pressure": "unknown",
        }
        with mock.patch.object(cli, "is_root", return_value=True):
            document = memory._top_document([], sysmem, 20)
        self.assertTrue(document["partial"])
        self.assertEqual(document["partial_reasons"],
                         ["system_memory_probe"])

    def test_limit_truncates_processes(self):
        rows = [(i * MB, i * MB, i, "p%d" % i) for i in range(5, 0, -1)]
        sysmem = {"total": 0, "used": 0, "free": 0, "active": 0,
                   "inactive": 0, "wired": 0, "compressed": 0,
                   "pressure": "unknown"}
        document = memory._top_document(rows, sysmem, 2)
        self.assertEqual(len(document["processes"]), 2)

    def test_once_emits_exactly_one_document(self):
        options = cli.parse_options(["--json", "--once"])
        with mock.patch.object(memory, "snapshot_footprint", return_value={}), \
                mock.patch.object(memory, "system_memory", return_value={
                    "total": 0, "used": 0, "free": 0, "active": 0,
                    "inactive": 0, "wired": 0, "compressed": 0,
                    "pressure": "unknown"}), \
                mock.patch.object(memory.time, "sleep"), \
                mock.patch.object(memory.time, "monotonic",
                                  side_effect=[10.0, 10.0]), \
                mock.patch.object(cli, "is_root", return_value=True):
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = memory.cmd_top(options)
        self.assertEqual(result, cli.EXIT_OK)
        self.assertEqual(len(stream.getvalue().splitlines()), 1)

    def test_duration_repeats_until_elapsed(self):
        options = cli.parse_options(["--json", "--duration", "0.8", "--interval", "0.5"])
        sysmem = {"total": 0, "used": 0, "free": 0, "active": 0,
                   "inactive": 0, "wired": 0, "compressed": 0,
                   "pressure": "unknown"}
        with mock.patch.object(memory, "snapshot_footprint", return_value={}), \
                mock.patch.object(memory, "system_memory", return_value=sysmem), \
                mock.patch.object(memory.time, "sleep") as sleep, \
                mock.patch.object(memory.time, "monotonic",
                                  side_effect=[10.0, 10.0, 10.5, 11.0]), \
                mock.patch.object(cli, "is_root", return_value=True):
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = memory.cmd_top(options)
        self.assertEqual(result, cli.EXIT_OK)
        self.assertEqual(len(stream.getvalue().splitlines()), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_unstyled_frame_has_no_terminal_control_codes(self):
        sysmem = {"total": 0, "used": 0, "free": 0, "active": 0,
                   "inactive": 0, "wired": 0, "compressed": 0,
                   "pressure": "unknown"}
        frame = memory._top_frame([], sysmem, 1.0, 20, styled=False)
        self.assertNotIn("\033", frame)

    def test_frame_handles_nullable_system_values(self):
        sysmem = {
            "available": False,
            "errors": ["vm_stat_failed"],
            "total": None,
            "used": None,
            "free": None,
            "active": None,
            "inactive": None,
            "wired": None,
            "compressed": None,
            "pressure": "unknown",
        }
        frame = memory._top_frame([], sysmem, 1.0, 20, styled=False)
        self.assertIn("used ? / ?", frame)


# ---------------------------------------------------------------------------
# watch: JSON contract, exit codes, process gone / permission, once/duration
# ---------------------------------------------------------------------------

class TestWatchAccessibility(unittest.TestCase):
    def test_nonexistent_pid_is_usage_not_permission(self):
        with mock.patch.object(memory, "proc_identity", return_value=None), \
                mock.patch.object(memory, "pid_status", return_value="gone"):
            options = cli.parse_options(["--json"])
            result = memory.cmd_watch(999999, options)
        self.assertEqual(result, cli.EXIT_USAGE)

    def test_existing_inaccessible_pid_is_permission(self):
        with mock.patch.object(memory, "proc_identity", return_value=None), \
                mock.patch.object(memory, "pid_status", return_value="denied"):
            options = cli.parse_options(["--json"])
            result = memory.cmd_watch(1, options)
        self.assertEqual(result, cli.EXIT_PERMISSION)


class TestWatchContract(unittest.TestCase):
    def _run_watch(self, options, infos):
        """Drive cmd_watch with a scripted sequence of raw-rusage samples."""
        with mock.patch.object(memory, "proc_identity", return_value=(42, 100)), \
                mock.patch.object(memory, "proc_name", return_value="leaky"), \
                mock.patch.object(memory.rusage, "_raw_rusage", side_effect=infos), \
                mock.patch.object(memory.time, "sleep"), \
                mock.patch.object(memory.time, "monotonic",
                                  side_effect=[float(i) for i in range(100)]):
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = memory.cmd_watch(42, options)
        return result, stream.getvalue()

    def test_sustained_growth_reports_leak_and_exit_findings(self):
        n = memory.MIN_LEAK_SAMPLES
        options = cli.parse_options(["--json", "--duration", "%d" % n])
        infos = [_rusage_info(i * 10 * MB, i * 10 * MB) for i in range(n)]
        result, out = self._run_watch(options, infos)
        docs = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(len(docs), n)
        self.assertFalse(docs[0]["leak_candidate"])
        self.assertTrue(docs[-1]["leak_candidate"])
        self.assertEqual(result, cli.EXIT_FINDINGS)

    def test_steady_process_stays_ok(self):
        n = 8
        options = cli.parse_options(["--json", "--duration", "%d" % n])
        infos = [_rusage_info(10 * MB, 10 * MB) for _ in range(n)]
        result, out = self._run_watch(options, infos)
        docs = [json.loads(line) for line in out.splitlines()]
        self.assertTrue(all(not d["leak_candidate"] for d in docs))
        self.assertEqual(result, cli.EXIT_OK)

    def test_process_exit_mid_watch_sets_running_false(self):
        options = cli.parse_options(["--json", "--duration", "10"])
        infos = [_rusage_info(10 * MB, 10 * MB), None]
        result, out = self._run_watch(options, infos)
        docs = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(len(docs), 2)
        self.assertTrue(docs[0]["running"])
        self.assertFalse(docs[1]["running"])
        self.assertIsNone(docs[1]["footprint_bytes"])
        self.assertIsNone(docs[1]["resident_size_bytes"])
        self.assertIsNone(docs[1]["slope_mb_per_min"])
        self.assertEqual(result, cli.EXIT_OK)

    def test_pid_reuse_mid_watch_treated_as_exit(self):
        # Same bare pid, a different start_abstime: the watched process is
        # gone even though _raw_rusage(pid) still succeeds (S10).
        options = cli.parse_options(["--json", "--duration", "10"])
        infos = [_rusage_info(10 * MB, 10 * MB, start=100),
                 _rusage_info(1 * MB, 1 * MB, start=999)]
        result, out = self._run_watch(options, infos)
        docs = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(len(docs), 2)
        self.assertFalse(docs[1]["running"])

    def test_latched_leak_survives_process_exit_in_exit_code(self):
        n = memory.MIN_LEAK_SAMPLES
        options = cli.parse_options(["--json", "--duration", "20"])
        infos = [_rusage_info(i * 10 * MB, i * 10 * MB) for i in range(n)] + [None]
        result, out = self._run_watch(options, infos)
        docs = [json.loads(line) for line in out.splitlines()]
        self.assertTrue(docs[-2]["leak_candidate"])
        self.assertFalse(docs[-1]["running"])
        self.assertTrue(docs[-1]["leak_candidate"])
        self.assertEqual(result, cli.EXIT_FINDINGS)

    def test_once_emits_exactly_one_document(self):
        options = cli.parse_options(["--json", "--once"])
        result, out = self._run_watch(options, [_rusage_info(MB, MB)])
        self.assertEqual(result, cli.EXIT_OK)
        self.assertEqual(len(out.splitlines()), 1)

    def test_unstyled_frame_has_no_terminal_control_codes(self):
        frame = memory._watch_frame(
            42, "leaky", 10 * MB, 9 * MB, 0.5, False, [1, 2, 3], 1.0,
            styled=False)
        self.assertNotIn("\033", frame)


# ---------------------------------------------------------------------------
# main(): option/positional validation
# ---------------------------------------------------------------------------

class TestMainDispatch(unittest.TestCase):
    def test_watch_rejects_limit_flag(self):
        with mock.patch.object(memory, "cmd_watch") as cmd_watch:
            result = memory.main(["stethoscope memory", "watch", "42", "--limit", "5"])
        self.assertEqual(result, cli.EXIT_USAGE)
        self.assertFalse(cmd_watch.called)

    def test_watch_requires_a_pid_positional(self):
        with mock.patch.object(memory, "cmd_watch") as cmd_watch:
            result = memory.main(["stethoscope memory", "watch"])
        self.assertEqual(result, cli.EXIT_USAGE)
        self.assertFalse(cmd_watch.called)

    def test_watch_rejects_extra_positionals(self):
        with mock.patch.object(memory, "cmd_watch") as cmd_watch:
            result = memory.main(["stethoscope memory", "watch", "42", "43"])
        self.assertEqual(result, cli.EXIT_USAGE)
        self.assertFalse(cmd_watch.called)

    def test_watch_rejects_non_integer_pid(self):
        with mock.patch.object(memory, "cmd_watch") as cmd_watch:
            result = memory.main(["stethoscope memory", "watch", "abc"])
        self.assertEqual(result, cli.EXIT_USAGE)
        self.assertFalse(cmd_watch.called)

    def test_watch_rejects_non_positive_pid(self):
        with mock.patch.object(memory, "cmd_watch") as cmd_watch:
            result = memory.main(["stethoscope memory", "watch", "0"])
        self.assertEqual(result, cli.EXIT_USAGE)
        self.assertFalse(cmd_watch.called)

    def test_top_rejects_extra_positional(self):
        with mock.patch.object(memory, "cmd_top") as cmd_top:
            result = memory.main(["stethoscope memory", "top", "stray"])
        self.assertEqual(result, cli.EXIT_USAGE)
        self.assertFalse(cmd_top.called)

    def test_unknown_option_is_usage(self):
        with mock.patch.object(memory, "cmd_top") as cmd_top:
            result = memory.main(["stethoscope memory", "--bogus"])
        self.assertEqual(result, cli.EXIT_USAGE)
        self.assertFalse(cmd_top.called)

    def test_unknown_mode_is_usage(self):
        result = memory.main(["stethoscope memory", "nonsense"])
        self.assertEqual(result, cli.EXIT_USAGE)

    def test_help_flag_short_circuits(self):
        with mock.patch("builtins.print") as fake_print:
            result = memory.main(["stethoscope memory", "--help"])
        self.assertEqual(result, cli.EXIT_OK)
        self.assertTrue(fake_print.called)


if __name__ == "__main__":
    unittest.main()
