"""Unit tests for recording/history/baseline data layer."""

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scopes"))
import record  # noqa: E402


class TempDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.path.dirname(os.path.abspath(__file__)))
        self.db = os.path.join(self.tmp.name, "history.db")
        self.conn = record.connect_db(self.db)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()


class TestSchemaRoundTrip(TempDbTest):
    def test_insert_rows_and_read_back(self):
        rows = [
            {"ts": 100, "scope": "cpu", "metric": "system_cpu_pct", "value": 12.5},
            {"ts": 100, "scope": "cpu", "metric": "process_cpu_pct", "pid": 42,
             "name": "worker", "value": 9.0},
        ]
        record.append_rows(self.conn, rows, max_age_days=1, now_ts=100)
        got = self.conn.execute(
            "SELECT ts, scope, metric, pid, name, value FROM samples ORDER BY pid IS NOT NULL"
        ).fetchall()
        self.assertEqual(got[0], (100, "cpu", "system_cpu_pct", None, None, 12.5))
        self.assertEqual(got[1], (100, "cpu", "process_cpu_pct", 42, "worker", 9.0))


class TestRotation(TempDbTest):
    def test_prunes_rows_older_than_cap(self):
        record.append_rows(self.conn, [
            {"ts": 0, "scope": "cpu", "metric": "system_cpu_pct", "value": 1},
            {"ts": 100, "scope": "cpu", "metric": "system_cpu_pct", "value": 2},
        ], max_age_days=1, now_ts=100)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 2)
        record.append_rows(self.conn, [
            {"ts": 200000, "scope": "cpu", "metric": "system_cpu_pct", "value": 3},
        ], max_age_days=1, now_ts=200000)
        remaining = self.conn.execute("SELECT ts FROM samples ORDER BY ts").fetchall()
        self.assertEqual(remaining, [(200000,)])


class TestSinceParser(unittest.TestCase):
    def test_relative_minutes_hours_days(self):
        now = 1_000_000
        self.assertEqual(record.parse_since("30m", now), now - 1800)
        self.assertEqual(record.parse_since("1h", now), now - 3600)
        self.assertEqual(record.parse_since("2d", now), now - 172800)

    def test_iso_timestamp(self):
        epoch = record.parse_since("2026-07-08T12:00:00", now=0)
        self.assertEqual(epoch, int(dt.datetime(2026, 7, 8, 12, 0, 0).timestamp()))

    def test_clock_time_uses_today_or_yesterday(self):
        noon = dt.datetime(2026, 7, 8, 12, 0, 0).timestamp()
        self.assertEqual(record.parse_since("3am", noon),
                         int(dt.datetime(2026, 7, 8, 3, 0, 0).timestamp()))
        early = dt.datetime(2026, 7, 8, 1, 0, 0).timestamp()
        self.assertEqual(record.parse_since("3am", early),
                         int(dt.datetime(2026, 7, 7, 3, 0, 0).timestamp()))


class TestHistoryQuery(TempDbTest):
    def test_summarizes_metrics_and_top_consumers(self):
        rows = [
            {"ts": 100, "scope": "cpu", "metric": "system_cpu_pct", "value": 10},
            {"ts": 200, "scope": "cpu", "metric": "system_cpu_pct", "value": 30},
            {"ts": 200, "scope": "memory", "metric": "used_pct", "value": 50},
            {"ts": 200, "scope": "cpu", "metric": "process_cpu_pct", "pid": 10,
             "name": "a", "value": 7},
            {"ts": 201, "scope": "cpu", "metric": "process_cpu_pct", "pid": 11,
             "name": "b", "value": 17},
        ]
        record.append_rows(self.conn, rows, max_age_days=1, now_ts=201)
        result = record.query_history(self.conn, since_ts=150, scope=None, limit=5)
        metrics = {(m["scope"], m["metric"]): m for m in result["metrics"]}
        self.assertEqual(metrics[("cpu", "system_cpu_pct")]["count"], 1)
        self.assertEqual(metrics[("cpu", "system_cpu_pct")]["mean"], 30)
        self.assertEqual(metrics[("memory", "used_pct")]["peak"], 50)
        self.assertEqual(result["top_consumers"][0]["pid"], 11)


class TestBaseline(TempDbTest):
    def test_percentile_math_known_distribution(self):
        base = int(dt.datetime(2026, 7, 8, 9, 0, 0).timestamp())
        rows = [
            {"ts": base + i, "scope": "cpu", "metric": "system_cpu_pct", "value": value}
            for i, value in enumerate([1, 2, 3, 4, 5])
        ]
        rows.append({"ts": base, "scope": "cpu", "metric": "process_cpu_pct",
                     "pid": 99, "name": "ignored", "value": 100})
        record.append_rows(self.conn, rows, max_age_days=1, now_ts=base + 10)
        result = record.compute_baseline(self.conn)
        self.assertEqual(len(result["baselines"]), 1)
        b = result["baselines"][0]
        self.assertEqual((b["p50"], b["p90"], b["p99"]), (3.0, 4.6, 4.96))
        self.assertEqual(b["count"], 5)


if __name__ == "__main__":
    unittest.main()
