"""Hermetic tests for finite-safe diagnosis statistics."""

import math
import unittest

from core import stats

MIB = 1024 * 1024


class RobustBandCase(unittest.TestCase):
    def test_robust_outlier_does_not_move_center(self):
        band = stats.robust_band([10, 10, 11, 11, 1000], 2, 0)
        self.assertEqual(band["center"], 11)
        self.assertIsNone(stats.classify_deviation(12, band))
        self.assertEqual(
            stats.classify_deviation(20, band)["severity"], "critical")

    def test_degenerate_memory_band_uses_floor(self):
        values = [6.15e9, 6.1502e9, 6.1499e9, 6.1501e9, 6.15e9]
        band = stats.robust_band(values, 256 * MIB, 0.05)
        self.assertIsNone(stats.classify_deviation(6.151e9, band))

    def test_zero_band_still_flags_material_spike(self):
        band = stats.robust_band([0] * 8, 10, 0.5)
        self.assertEqual(
            stats.classify_deviation(40, band)["severity"], "critical")

    def test_low_direction(self):
        band = stats.robust_band([100] * 8, 10, 0)
        self.assertEqual(
            stats.classify_deviation(70, band, "low")["severity"], "critical")

    def test_nonfinite_values_are_ignored(self):
        band = stats.robust_band([1, float("nan"), float("inf"), 2], 1, 0)
        self.assertEqual(band["count"], 2)
        self.assertIsNone(stats.classify_deviation(float("nan"), band))

    def test_opposite_max_floats_do_not_overflow(self):
        band = stats.robust_band([-1e308, 0, 1e308], 1, 0)
        self.assertTrue(math.isfinite(band["center"]))
        self.assertIsNone(stats.classify_deviation(1e308, band))

    def test_extreme_finite_score_is_clamped(self):
        band = stats.robust_band([0] * 5, 1, 0)
        evidence = stats.classify_deviation(1e308, band)
        self.assertEqual(evidence["score"], 99)


class OnlineTrendCase(unittest.TestCase):
    def test_least_squares_slope_and_bounded_recent(self):
        trend = stats.OnlineTrend(recent_size=5)
        for minute in range(20):
            trend.add(minute * 60, minute * 2 * MIB)
        self.assertAlmostEqual(
            trend.slope_per_second * 60 / MIB, 2.0)
        self.assertEqual(len(trend.recent), 5)

    def test_rejects_nonfinite_and_out_of_order(self):
        trend = stats.OnlineTrend()
        self.assertTrue(trend.add(1, 1))
        self.assertFalse(trend.add(1, 2))
        self.assertFalse(trend.add(2, float("inf")))
        self.assertEqual(trend.invalid_count, 2)

    def test_overflow_is_unavailable_not_infinite(self):
        trend = stats.OnlineTrend()
        trend.add(0, -1e308)
        trend.add(1, 1e308)
        self.assertIsNone(trend.slope_per_second)


class LeakCase(unittest.TestCase):
    def test_requires_count_and_span(self):
        samples = [(i * 60, (100 + i * 2) * MIB) for i in range(5)]
        self.assertIsNone(stats.leak_evidence(samples))
        samples = [(i * 600, (100 + i * 20) * MIB) for i in range(4)]
        self.assertIsNone(stats.leak_evidence(samples))

    def test_sustained_growth_is_evidence(self):
        samples = [(i * 600, (100 + i * 20) * MIB) for i in range(6)]
        evidence = stats.leak_evidence(samples)
        self.assertEqual(evidence["severity"], "warn")
        self.assertAlmostEqual(evidence["slope_mib_per_min"], 2)

    def test_recent_plateau_blocks_old_growth(self):
        values = [100, 200, 300, 400, 500, 500, 500, 500, 500, 500]
        samples = [(i * 300, value * MIB) for i, value in enumerate(values)]
        self.assertIsNone(stats.leak_evidence(samples))

    def test_frequent_drops_block_growth(self):
        values = [100, 160, 120, 180, 140, 220, 180, 260]
        samples = [(i * 300, value * MIB) for i, value in enumerate(values)]
        self.assertIsNone(stats.leak_evidence(samples))


class RunawayCase(unittest.TestCase):
    def test_mature_zero_cpu_baseline_flags_spike(self):
        evidence = stats.runaway_evidence("cpu_pct", 80, [0] * 10)
        self.assertEqual(
            evidence["baseline_source"], "history_and_static_threshold")
        self.assertEqual(evidence["severity"], "critical")

    def test_static_guardrail_survives_poisoned_history(self):
        evidence = stats.runaway_evidence("cpu_pct", 100, [100] * 20)
        self.assertEqual(evidence["severity"], "critical")
        self.assertEqual(
            evidence["baseline_source"], "history_and_static_threshold")

    def test_static_cpu_threshold(self):
        self.assertIsNone(stats.runaway_evidence("cpu_pct", 70, []))
        self.assertEqual(
            stats.runaway_evidence("cpu_pct", 75, [])["severity"], "warn")
        self.assertEqual(
            stats.runaway_evidence("cpu_pct", 95, [])["severity"], "critical")

    def test_wakeup_counters_are_independent(self):
        self.assertIsNone(stats.runaway_evidence(
            "pkg_idle_wakeups_per_s", 400, []))
        self.assertEqual(stats.runaway_evidence(
            "interrupt_wakeups_per_s", 1200, [])["severity"], "warn")
        self.assertIsNone(stats.runaway_evidence(
            "interrupt_wakeups_per_s", 400, []))


if __name__ == "__main__":
    unittest.main()
