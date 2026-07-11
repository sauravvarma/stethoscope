"""Hermetic disk-scope tests.

Every kernel/subprocess boundary this file touches — `mount`, `lsof`, and
libproc (via core.rusage) — is faked with unittest.mock, so this suite runs
on any macOS box without root, real volumes, or live processes.

This is deliberately NOT where the disk scope's live probe contracts live:
tests/test_core.py exercises real proc_pid_rusage/libproc calls against the
running machine on purpose (struct layout, timebase conversion, wall-clock
tracking — the things that only fail on the wrong hardware). Nothing here
duplicates those; this file covers the pure data/parse layer instead —
formatting, lsof/mount text parsing, and volume-argument resolution.
"""

import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest import mock

from core import rusage
from scopes import disk


def _fake_run(stdout):
    """Stand-in for subprocess.run(...) returning a captured stdout."""
    return mock.Mock(stdout=stdout, stderr="", returncode=0)


class TestHuman(unittest.TestCase):
    def test_bytes_are_integer_no_suffix_decimal(self):
        self.assertEqual(disk.human(0), "0B")
        self.assertEqual(disk.human(512), "512B")
        self.assertEqual(disk.human(1023), "1023B")

    def test_kib_boundary(self):
        self.assertEqual(disk.human(1024), "1.0K")
        self.assertEqual(disk.human(1536), "1.5K")

    def test_larger_units(self):
        self.assertEqual(disk.human(1024 ** 2), "1.0M")
        self.assertEqual(disk.human(1024 ** 3), "1.0G")
        self.assertEqual(disk.human(1024 ** 4), "1.0T")
        self.assertEqual(disk.human(1024 ** 5), "1.0P")

    def test_rate_appends_per_second(self):
        self.assertEqual(disk.rate(2048), "2.0K/s")


class TestClassifyFd(unittest.TestCase):
    def test_named_roles(self):
        self.assertEqual(disk._classify_fd("cwd"), "working dir (cwd)")
        self.assertEqual(disk._classify_fd("rtd"), "root dir")
        self.assertEqual(disk._classify_fd("txt"), "executable/text")
        self.assertEqual(disk._classify_fd("mem"), "mmap")

    def test_numeric_fd_modes(self):
        self.assertEqual(disk._classify_fd("3r"), "open (read)")
        self.assertEqual(disk._classify_fd("4w"), "open (write)")
        self.assertEqual(disk._classify_fd("5u"), "open (read/write)")
        self.assertEqual(disk._classify_fd("12"), "open fd")   # digit-led, unknown mode

    def test_empty_is_question_mark(self):
        self.assertEqual(disk._classify_fd(""), "?")


class TestMountTable(unittest.TestCase):
    SAMPLE = (
        "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
        "devfs on /dev (devfs, local, nobrowse)\n"
        "/dev/disk6s2 on /Volumes/X9 Pro (exfat, local, nodev, nosuid)\n"
    )

    def test_parses_device_and_mountpoint_preserving_spaces(self):
        with mock.patch.object(
                disk, "_run_bounded", return_value=_fake_run(self.SAMPLE)):
            table = disk._mount_table()
        self.assertIn(("/dev/disk3s1s1", "/"), table)
        self.assertIn(("devfs", "/dev"), table)
        self.assertIn(("/dev/disk6s2", "/Volumes/X9 Pro"), table)  # space preserved


class TestResolveVolume(unittest.TestCase):
    def test_whole_disk_match_stops_at_slice_boundary(self):
        mounts = [
            ("/dev/disk1", "/Volumes/Whole"),
            ("/dev/disk1s1", "/Volumes/One"),
            ("/dev/disk10s1", "/Volumes/Ten"),
        ]
        with mock.patch.object(disk, "_mount_table", return_value=mounts):
            self.assertEqual(
                disk.resolve_volume("disk1"),
                mounts[:2],
            )


class TestResolveVolumeArgumentForms(unittest.TestCase):
    TABLE = [
        ("/dev/disk1s1", "/"),
        ("/dev/disk10s1", "/Volumes/Backup"),
        ("/dev/disk10s2", "/Volumes/Media"),
        ("/dev/disk6s2", "/Volumes/X9 Pro"),
    ]

    def setUp(self):
        self._patch = mock.patch.object(disk, "_mount_table", lambda: self.TABLE)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_mount_path(self):
        self.assertEqual(disk.resolve_volume("/Volumes/X9 Pro"),
                         [("/dev/disk6s2", "/Volumes/X9 Pro")])

    def test_volume_name(self):
        self.assertEqual(disk.resolve_volume("X9 Pro"),
                         [("/dev/disk6s2", "/Volumes/X9 Pro")])

    def test_device_node_bare_and_dev_forms(self):
        expect = [("/dev/disk6s2", "/Volumes/X9 Pro")]
        self.assertEqual(disk.resolve_volume("disk6s2"), expect)
        self.assertEqual(disk.resolve_volume("/dev/disk6s2"), expect)

    def test_whole_disk_expands_to_all_slices(self):
        self.assertEqual(
            disk.resolve_volume("disk10"),
            [("/dev/disk10s1", "/Volumes/Backup"), ("/dev/disk10s2", "/Volumes/Media")])

    def test_whole_disk_prefix_does_not_bleed(self):
        # regression for #33: disk1 must NOT swallow disk10's slices
        self.assertEqual(disk.resolve_volume("disk1"), [("/dev/disk1s1", "/")])

    def test_no_match_returns_empty(self):
        self.assertEqual(disk.resolve_volume("nope"), [])


class TestOpenFiles(unittest.TestCase):
    LSOF = (
        "COMMAND   PID  USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
        "bash    1000 kris cwd DIR 1,2 4096 10 /Users/kris\n"
        "bash    1000 kris txt REG 1,2 1000 11 /bin/bash\n"
        "bash    1000 kris 3r  REG 1,2 50   12 /tmp/data.txt\n"
        "bash    1000 kris 5u  IPv4 0x1 0t0 TCP host:1234\n"   # not REG/DIR -> dropped
    )

    def test_disk_only_keeps_regular_files_and_dirs(self):
        with mock.patch.object(
                disk, "_run_bounded", return_value=_fake_run(self.LSOF)):
            items = disk.open_files(1000)
        self.assertEqual([t for _, t, _ in items], ["DIR", "REG", "REG"])
        self.assertEqual([r for r, _, _ in items],
                         ["working dir (cwd)", "executable/text", "open (read)"])


class TestStructuredResults(unittest.TestCase):
    def test_top_result_matches_rank_and_document_contract(self):
        previous = {(7, 70): (100, 200)}
        current = {(7, 70): (300, 250)}
        with mock.patch.object(disk.cli, "is_root", return_value=True), \
                mock.patch.object(disk, "proc_name", return_value="writer"):
            result, exit_code = disk.top_result(
                previous, current, 1.0, limit=20)
            rows, read_rate, write_rate = disk.rank_io(
                previous, current, 1.0)
            expected = disk._top_document(
                rows, read_rate, write_rate, limit=20)
        self.assertEqual(result, expected)
        self.assertEqual(exit_code, disk.cli.EXIT_OK)


class TestCollectHolders(unittest.TestCase):
    LSOF = (
        "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
        "mds    1263 root cwd DIR 1,2 4096 1 /Volumes/X9 Pro\n"
        "mds    1263 root 3r  REG 1,2 50   2 /Volumes/X9 Pro/a\n"
        "Finder 2000 kris txt REG 1,2 50   3 /Volumes/X9 Pro/App\n"
    )

    def test_groups_holds_by_pid(self):
        with mock.patch.object(
                disk, "_run_bounded", return_value=_fake_run(self.LSOF)):
            procs = disk.collect_holders([("/dev/disk6s2", "/Volumes/X9 Pro")])
        self.assertEqual(set(procs), {1263, 2000})
        self.assertEqual(procs[1263]["name"], "mds")
        self.assertEqual(procs[1263]["user"], "root")
        self.assertEqual(len(procs[1263]["holds"]), 2)
        self.assertEqual(procs[2000]["holds"][0][0], "executable/text")


class TestBoundedNativeProbes(unittest.TestCase):
    def test_bounded_runner_captures_small_output(self):
        result = disk._run_bounded(
            [sys.executable, "-c", "print('ok')"],
            timeout=1, max_output=100)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "ok\n")

    def test_bounded_runner_rejects_excess_output_and_timeout(self):
        with self.assertRaises(disk.ProbeOutputError):
            disk._run_bounded(
                [sys.executable, "-c", "print('x' * 200)"],
                timeout=1, max_output=100)
        with self.assertRaises(subprocess.TimeoutExpired):
            disk._run_bounded(
                [sys.executable, "-c", "import time; time.sleep(1)"],
                timeout=0.01, max_output=100)

    def test_busy_result_maps_probe_limits_to_structured_runtime_error(self):
        with mock.patch.object(
                disk, "resolve_volume",
                side_effect=subprocess.TimeoutExpired(["mount"], 1)), \
                mock.patch.object(disk.cli, "is_root", return_value=True):
            document, exit_code = disk.busy_result("disk1")
        self.assertEqual(exit_code, disk.cli.EXIT_ERROR)
        self.assertTrue(document["partial"])
        self.assertIn("mount_probe_failed", document["partial_reasons"])
        self.assertEqual(document["targets"], [])

    def test_holds_result_maps_output_limit_to_structured_runtime_error(self):
        with mock.patch.object(
                disk, "_run_bounded",
                side_effect=disk.ProbeOutputError("too large")), \
                mock.patch.object(disk, "proc_name", return_value="proc"), \
                mock.patch.object(disk, "proc_diskio", return_value=None), \
                mock.patch.object(disk.cli, "is_root", return_value=True):
            document, exit_code = disk.holds_result(42)
        self.assertEqual(exit_code, disk.cli.EXIT_ERROR)
        self.assertEqual(document["holds"], [])
        self.assertIn("too large", document["error"])

    def test_busy_runtime_error_does_not_probe_mounts_again(self):
        document = {
            "error": "lsof failed: timed out",
            "targets": [], "holders": [],
        }
        errors = StringIO()
        with mock.patch.object(
                disk, "busy_result",
                return_value=(document, disk.cli.EXIT_ERROR)), \
                mock.patch.object(disk, "_mount_table") as mounts, \
                redirect_stderr(errors):
            exit_code = disk.cmd_busy(
                "disk1", disk.cli.parse_options([]))
        self.assertEqual(exit_code, disk.cli.EXIT_ERROR)
        mounts.assert_not_called()
        self.assertIn("lsof failed", errors.getvalue())


class TestProcNameCache(unittest.TestCase):
    def setUp(self):
        rusage._name_cache.clear()

    def tearDown(self):
        rusage._name_cache.clear()

    def test_pid_reuse_refreshes_cached_name(self):
        libc = mock.Mock()
        paths = iter((b"/usr/bin/old-process", b"/usr/bin/new-process"))

        def proc_pidpath(_pid, buf, _size):
            buf.value = next(paths)
            return len(buf.value)

        libc.proc_pidpath.side_effect = proc_pidpath
        identities = [(42, 100), (42, 100), (42, 200)]
        with mock.patch.object(rusage, "_libc", libc), \
                mock.patch.object(rusage, "proc_identity", side_effect=identities):
            self.assertEqual(rusage.proc_name(42), "old-process")
            self.assertEqual(rusage.proc_name(42), "old-process")
            self.assertEqual(rusage.proc_name(42), "new-process")

        self.assertEqual(libc.proc_pidpath.call_count, 2)
        self.assertEqual(rusage._name_cache, {(42, 200): "new-process"})


if __name__ == "__main__":
    unittest.main()
