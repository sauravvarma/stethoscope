"""Unit tests for anomaly detector data layers."""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scopes"))
import anomaly  # noqa: E402


MB = 1024 * 1024


class TestDeviation(unittest.TestCase):
    def test_flags_above_p90_and_p99(self):
        baseline = {"baselines": [
            {"hour": 9, "scope": "cpu", "metric": "system_cpu_pct",
             "count": 10, "p50": 10, "p90": 50, "p99": 90}
        ]}
        current = [{"scope": "cpu", "metric": "system_cpu_pct", "value": 95}]
        got = anomaly.detect_deviation(current, baseline, now_ts=time.mktime((2026, 1, 1, 9, 0, 0, 0, 1, -1)))
        self.assertEqual(got[0]["severity"], "critical")
        self.assertEqual(got[0]["baseline"]["p99"], 90)

    def test_within_band_is_clean(self):
        baseline = {"baselines": [
            {"hour": 9, "scope": "memory", "metric": "used_pct",
             "count": 10, "p50": 50, "p90": 80, "p99": 95}
        ]}
        current = [{"scope": "memory", "metric": "used_pct", "value": 70}]
        self.assertEqual(anomaly.detect_deviation(
            current, baseline, now_ts=time.mktime((2026, 1, 1, 9, 0, 0, 0, 1, -1))), [])

    def test_degenerate_band_is_ignored(self):
        # cold start: p50 ~= p90 ~= p99 (few near-identical samples). A value a
        # hair above p99 must NOT be flagged (regression: it read "critical").
        baseline = {"baselines": [
            {"hour": 9, "scope": "memory", "metric": "used",
             "count": 4, "p50": 6151700000.0, "p90": 6151730000.0,
             "p99": 6151730000.0}
        ]}
        current = [{"scope": "memory", "metric": "used", "value": 6152683520.0}]
        self.assertEqual(anomaly.detect_deviation(
            current, baseline, now_ts=time.mktime((2026, 1, 1, 9, 0, 0, 0, 1, -1))), [])


class TestLeaks(unittest.TestCase):
    def test_rising_series_ranks_above_flat(self):
        rows = []
        for i, val in enumerate([100, 120, 150, 190]):
            rows.append({"ts": i * 60, "pid": 10, "name": "leaky", "value": val * MB})
        for i in range(4):
            rows.append({"ts": i * 60, "pid": 20, "name": "flat", "value": 300 * MB})
        got = anomaly.detect_leaks_from_rows(rows, min_slope_mb_min=0.1)
        self.assertEqual([f["pid"] for f in got], [10])
        self.assertGreater(got[0]["slope_mb_per_min"], 0)


class TestRunaways(unittest.TestCase):
    def test_sustained_high_vs_own_norm(self):
        hist = [
            {"ts": i, "metric": "process_cpu_pct", "pid": 42, "name": "helper", "value": v}
            for i, v in enumerate([8, 10, 12, 11, 9])
        ]
        current = [{"pid": 42, "name": "helper", "cpu_pct": 85, "wakeups_per_s": 10}]
        got = anomaly.detect_runaways(current, hist)
        self.assertEqual(got[0]["pid"], 42)
        self.assertEqual(got[0]["severity"], "warn")

    def test_absolute_threshold_without_history(self):
        got = anomaly.detect_runaways([
            {"pid": 7, "name": "spin", "cpu_pct": 98, "wakeups_per_s": 20}
        ], [])
        self.assertEqual(got[0]["severity"], "critical")


class TestTriage(unittest.TestCase):
    def test_severity_sort_verdict_and_exit(self):
        vitals = {"system": {"memory": {"pressure": "normal"},
                             "battery": {"present": False},
                             "smart": {"drives": []}}}
        report = anomaly.triage_report(
            vitals,
            [anomaly._finding("warn", "cpu", "hot", "stethoscope cpu top", "deviation", 2)],
            [anomaly._finding("critical", "memory", "leak", "stethoscope memory watch 1", "leak", 9)],
            [anomaly._finding("info", "cpu", "busy", "stethoscope cpu top", "runaway", 1)])
        self.assertEqual(report["overall"], "critical")
        self.assertEqual(report["findings"][0]["severity"], "critical")
        self.assertEqual(anomaly.exit_code_for_report(report), 1)


if __name__ == "__main__":
    unittest.main()
