"""Hermetic unit tests for the disk scope's data/parse layer.

Every kernel/subprocess boundary is faked, so these run on any macOS box
(the module binds libSystem at import, so CI must be macOS) without root,
external volumes, or live processes.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scopes"))
import disk  # noqa: E402


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


class TestRankIo(unittest.TestCase):
    def setUp(self):
        self._names = mock.patch.object(disk, "proc_name", lambda p: "p%d" % p)
        self._names.start()

    def tearDown(self):
        self._names.stop()

    def test_diff_rates_and_sort_order(self):
        prev = {1: (0, 0), 2: (100, 0)}
        cur = {1: (200, 100), 2: (100, 50)}   # pid1 +300/s total, pid2 +50/s
        rows, sr, sw = disk.rank_io(prev, cur, 1.0)
        self.assertEqual([r[5] for r in rows], [1, 2])   # highest throughput first
        self.assertAlmostEqual(sr, 200.0)
        self.assertAlmostEqual(sw, 150.0)

    def test_counter_reset_clamps_to_zero(self):
        # cur < prev (counter reset / pid reuse) must never produce a negative rate
        rows, sr, sw = disk.rank_io({1: (1000, 1000)}, {1: (10, 10)}, 1.0)
        self.assertEqual(rows, [])
        self.assertEqual((sr, sw), (0.0, 0.0))

    def test_new_pid_is_zero_on_first_interval(self):
        rows, sr, sw = disk.rank_io({}, {5: (500, 500)}, 1.0)
        self.assertEqual(rows, [])           # prev defaults to cur -> no delta
        self.assertEqual((sr, sw), (0.0, 0.0))

    def test_dt_zero_is_guarded(self):
        rows, _, _ = disk.rank_io({1: (0, 0)}, {1: (100, 0)}, 0)  # dt=0 -> 1.0
        self.assertAlmostEqual(rows[0][1], 100.0)


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
        with mock.patch.object(disk.subprocess, "run", return_value=_fake_run(self.SAMPLE)):
            table = disk._mount_table()
        self.assertIn(("/dev/disk3s1s1", "/"), table)
        self.assertIn(("devfs", "/dev"), table)
        self.assertIn(("/dev/disk6s2", "/Volumes/X9 Pro"), table)  # space preserved


class TestResolveVolume(unittest.TestCase):
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
        with mock.patch.object(disk.subprocess, "run", return_value=_fake_run(self.LSOF)):
            items = disk.open_files(1000)
        self.assertEqual([t for _, t, _ in items], ["DIR", "REG", "REG"])
        self.assertEqual([r for r, _, _ in items],
                         ["working dir (cwd)", "executable/text", "open (read)"])


class TestCollectHolders(unittest.TestCase):
    LSOF = (
        "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
        "mds    1263 root cwd DIR 1,2 4096 1 /Volumes/X9 Pro\n"
        "mds    1263 root 3r  REG 1,2 50   2 /Volumes/X9 Pro/a\n"
        "Finder 2000 kris txt REG 1,2 50   3 /Volumes/X9 Pro/App\n"
    )

    def test_groups_holds_by_pid(self):
        with mock.patch.object(disk.subprocess, "run", return_value=_fake_run(self.LSOF)):
            procs = disk.collect_holders([("/dev/disk6s2", "/Volumes/X9 Pro")])
        self.assertEqual(set(procs), {1263, 2000})
        self.assertEqual(procs[1263]["name"], "mds")
        self.assertEqual(procs[1263]["user"], "root")
        self.assertEqual(len(procs[1263]["holds"]), 2)
        self.assertEqual(procs[2000]["holds"][0][0], "executable/text")


class TestProcNameCache(unittest.TestCase):
    def tearDown(self):
        disk._name_cache.clear()

    def test_pid_reuse_invalidates_stale_name(self):
        # regression for #34: a cached entry whose start time no longer matches
        # must be dropped rather than returned.
        pid = os.getpid()
        disk._name_cache[pid] = (-1, "STALE")     # bogus start_abstime
        self.assertNotEqual(disk.proc_name(pid), "STALE")

    def test_cache_hit_when_start_time_matches(self):
        def fake_pidpath(p, buf, size):
            buf.value = b"/usr/bin/foo"
            return len(b"/usr/bin/foo")

        with mock.patch.object(disk, "_proc_start_abstime", return_value=42), \
                mock.patch.object(disk._libc, "proc_pidpath",
                                  side_effect=fake_pidpath) as pidpath:
            first = disk.proc_name(777)
            second = disk.proc_name(777)      # same start -> served from cache
        self.assertEqual((first, second), ("foo", "foo"))
        self.assertEqual(pidpath.call_count, 1)


if __name__ == "__main__":
    unittest.main()
