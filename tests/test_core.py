"""Unit tests for the shared sampling spine (scopes/core.py).

The formatting and cache logic are tested hermetically; the ctypes/libproc
reads are smoke-tested against the running process (macOS-only, no root).
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scopes"))
import core  # noqa: E402


class TestFormatting(unittest.TestCase):
    def test_human_units(self):
        self.assertEqual(core.human(0), "0B")
        self.assertEqual(core.human(1023), "1023B")
        self.assertEqual(core.human(1024), "1.0K")
        self.assertEqual(core.human(1536), "1.5K")
        self.assertEqual(core.human(1024 ** 3), "1.0G")

    def test_rate(self):
        self.assertEqual(core.rate(2048), "2.0K/s")


class TestRusageSpine(unittest.TestCase):
    """Smoke tests against the current process — no fakes, no root."""

    def test_list_pids_includes_self(self):
        self.assertIn(os.getpid(), core.list_pids())

    def test_proc_rusage_self_has_all_fields(self):
        ru = core.proc_rusage(os.getpid())
        self.assertIsNotNone(ru)
        self.assertGreater(ru.start, 0)
        for field in core.RUsage._fields:
            self.assertTrue(hasattr(ru, field))

    def test_proc_rusage_bogus_pid_is_none(self):
        self.assertIsNone(core.proc_rusage(2 ** 31 - 1))

    def test_snapshot_rusage_includes_self(self):
        snap = core.snapshot_rusage()
        self.assertIn(os.getpid(), snap)
        self.assertIsInstance(snap[os.getpid()], core.RUsage)


class TestProcNameCache(unittest.TestCase):
    def tearDown(self):
        core._name_cache.clear()

    def test_pid_reuse_invalidates_stale_name(self):
        # regression for #34
        pid = os.getpid()
        core._name_cache[pid] = (-1, "STALE")     # bogus start_abstime
        self.assertNotEqual(core.proc_name(pid), "STALE")

    def test_cache_hit_when_start_time_matches(self):
        def fake_pidpath(p, buf, size):
            buf.value = b"/usr/bin/foo"
            return len(b"/usr/bin/foo")

        with mock.patch.object(core, "_proc_start_abstime", return_value=42), \
                mock.patch.object(core._libc, "proc_pidpath",
                                  side_effect=fake_pidpath) as pidpath:
            first = core.proc_name(777)
            second = core.proc_name(777)      # same start -> served from cache
        self.assertEqual((first, second), ("foo", "foo"))
        self.assertEqual(pidpath.call_count, 1)


if __name__ == "__main__":
    unittest.main()
