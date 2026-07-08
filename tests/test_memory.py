"""Unit tests for the memory scope (scopes/memory.py)."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scopes"))
import core    # noqa: E402
import memory  # noqa: E402


def _ru(footprint=0, resident=0):
    return core.RUsage(read=0, write=0, user_time=0, system_time=0,
                       idle_wkups=0, interrupt_wkups=0,
                       footprint=footprint, resident=resident, start=1)


class TestRankMem(unittest.TestCase):
    def test_sorted_by_footprint_and_skips_zero(self):
        with mock.patch.object(memory.core, "proc_name", lambda p: "p%d" % p):
            rows = memory.rank_mem({1: _ru(100, 200), 2: _ru(900, 950), 3: _ru(0, 0)})
        self.assertEqual([r[2] for r in rows], [2, 1])   # 3 (zero footprint) dropped
        self.assertEqual(rows[0][0], 900)


class TestVmStat(unittest.TestCase):
    SAMPLE = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                        10.\n"
        "Pages active:                     100.\n"
        "Pages wired down:                  50.\n"
        "Pages occupied by compressor:      20.\n"
    )

    def test_parse_multiplies_by_pagesize(self):
        with mock.patch.object(memory.subprocess, "run",
                               return_value=mock.Mock(stdout=self.SAMPLE)):
            c = memory._vm_stat()
        self.assertEqual(c["pages free"], 10 * 16384)
        self.assertEqual(c["pages occupied by compressor"], 20 * 16384)

    def test_system_memory_summary(self):
        with mock.patch.object(memory, "_vm_stat", return_value={
                "pages active": 100, "pages wired down": 50,
                "pages occupied by compressor": 20, "pages free": 10,
                "pages inactive": 5, "pages speculative": 2}), \
                mock.patch.object(memory, "_sysctl_int",
                                  side_effect=lambda n: {"hw.memsize": 1000,
                                                         "kern.memorystatus_vm_pressure_level": 2}.get(n)):
            s = memory.system_memory()
        self.assertEqual(s["total"], 1000)
        self.assertEqual(s["used"], 170)          # active + wired + compressed
        self.assertEqual(s["free"], 12)           # free + speculative
        self.assertEqual(s["pressure"], "warn")


class TestSlope(unittest.TestCase):
    def test_linear_growth_slope(self):
        mb = 1024 * 1024
        # +1 MB per second over 4 samples -> 60 MB/min
        samples = [(0, 0), (1, mb), (2, 2 * mb), (3, 3 * mb)]
        self.assertAlmostEqual(memory.slope_mb_per_min(samples), 60.0, places=3)

    def test_flat_is_zero(self):
        self.assertEqual(memory.slope_mb_per_min([(0, 5), (1, 5), (2, 5)]), 0.0)

    def test_single_sample_is_zero(self):
        self.assertEqual(memory.slope_mb_per_min([(0, 5)]), 0.0)


class TestSparkline(unittest.TestCase):
    def test_monotonic_spans_low_to_high(self):
        s = memory.sparkline([0, 1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(s[0], memory._SPARK[0])
        self.assertEqual(s[-1], memory._SPARK[-1])

    def test_flat_all_lowest(self):
        self.assertEqual(memory.sparkline([3, 3, 3]), memory._SPARK[0] * 3)

    def test_empty(self):
        self.assertEqual(memory.sparkline([]), "")


class TestDocuments(unittest.TestCase):
    def test_top_shape(self):
        rows = [(734003200, 812345344, 1234, "WindowServer")]
        sysmem = {"total": 1, "used": 1, "free": 0, "active": 0, "inactive": 0,
                  "wired": 0, "compressed": 0, "pressure": "warn"}
        doc = memory._top_document(rows, sysmem, 20)
        self.assertEqual((doc["scope"], doc["command"]), ("memory", "top"))
        self.assertEqual(doc["system"]["pressure"], "warn")
        self.assertEqual(doc["processes"][0]["footprint"], 734003200)


if __name__ == "__main__":
    unittest.main()
