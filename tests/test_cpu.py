"""Unit tests for the cpu scope (scopes/cpu.py).

rank_cpu and the JSON shape are tested hermetically with synthetic RUsage
snapshots; the mach clock is smoke-tested against the live process.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scopes"))
import core  # noqa: E402
import cpu   # noqa: E402


def _ru(user=0, system=0, idle=0, intr=0, foot=0, res=0, start=1):
    """An RUsage with just the CPU/wakeup fields set."""
    return core.RUsage(read=0, write=0, user_time=user, system_time=system,
                       idle_wkups=idle, interrupt_wkups=intr,
                       footprint=foot, resident=res, start=start)


class TestRankCpu(unittest.TestCase):
    def setUp(self):
        # rank_cpu resolves names via core.proc_name — stub it out.
        self._names = mock.patch.object(cpu.core, "proc_name", lambda p: "p%d" % p)
        self._names.start()

    def tearDown(self):
        self._names.stop()

    def test_cpu_percent_is_ratio_of_ticks(self):
        # pid1 burned 500 CPU ticks over 1000 wall ticks -> 50%
        prev = {1: _ru(user=0, system=0)}
        cur = {1: _ru(user=300, system=200)}
        rows, sys_cpu = cpu.rank_cpu(prev, cur, 0, 1000, 1.0)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0][0], 50.0)
        self.assertAlmostEqual(sys_cpu, 50.0)

    def test_multicore_exceeds_100(self):
        # 2000 CPU ticks over 1000 wall ticks -> 200% (two cores)
        prev = {1: _ru()}
        cur = {1: _ru(user=2000)}
        rows, _ = cpu.rank_cpu(prev, cur, 0, 1000, 1.0)
        self.assertAlmostEqual(rows[0][0], 200.0)

    def test_wakeup_rates_use_wall_seconds(self):
        prev = {1: _ru(idle=0, intr=0)}
        cur = {1: _ru(idle=100, intr=20)}
        rows, _ = cpu.rank_cpu(prev, cur, 0, 1000, 2.0)   # dt = 2s
        _cpu, wake, idle_ps, intr_ps, _pid, _name = rows[0]
        self.assertAlmostEqual(idle_ps, 50.0)             # 100 / 2s
        self.assertAlmostEqual(intr_ps, 10.0)             # 20 / 2s
        self.assertAlmostEqual(wake, 60.0)

    def test_new_pid_skipped(self):
        rows, sys_cpu = cpu.rank_cpu({}, {9: _ru(user=500)}, 0, 1000, 1.0)
        self.assertEqual(rows, [])
        self.assertEqual(sys_cpu, 0.0)

    def test_counter_clamp_no_negative(self):
        rows, _ = cpu.rank_cpu({1: _ru(user=1000)}, {1: _ru(user=10)}, 0, 1000, 1.0)
        self.assertEqual(rows, [])

    def test_sorted_by_cpu_descending(self):
        prev = {1: _ru(), 2: _ru()}
        cur = {1: _ru(user=100), 2: _ru(user=900)}
        rows, _ = cpu.rank_cpu(prev, cur, 0, 1000, 1.0)
        self.assertEqual([r[4] for r in rows], [2, 1])


class TestCpuDocument(unittest.TestCase):
    def test_shape(self):
        rows = [(93.9, 412.0, 400.0, 12.0, 29641, "copilot")]
        doc = cpu._document(rows, 143.9, "top", 20)
        self.assertEqual((doc["scope"], doc["command"]), ("cpu", "top"))
        self.assertEqual(doc["system"]["ncpu"], cpu.NCPU)
        p = doc["processes"][0]
        self.assertEqual(p["pid"], 29641)
        self.assertAlmostEqual(p["cpu_pct"], 93.9)
        self.assertAlmostEqual(p["interrupt_wakeups_per_s"], 12.0)


class TestMachClock(unittest.TestCase):
    def test_monotonic(self):
        a = core.mach_absolute_time()
        b = core.mach_absolute_time()
        self.assertGreaterEqual(b, a)

    def test_abstime_to_seconds_positive(self):
        self.assertGreater(core.abstime_to_seconds(24_000_000), 0)


if __name__ == "__main__":
    unittest.main()
