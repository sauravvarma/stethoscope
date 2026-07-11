"""Hermetic recording, JSONL replay, history, and baseline tests."""

import datetime
import io
import json
import math
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from core import baseline, cli
from scopes import record

_HERE = os.path.dirname(os.path.abspath(__file__))


def raw(timestamp=1000.0, value=1.0, process_name="worker"):
    metrics = []
    for (scope, metric), unit in baseline.REQUIRED_METRICS.items():
        metric_value = value if (scope, metric) == ("cpu", "cpu_pct") else 0.0
        metrics.append({
            "scope": scope, "metric": metric,
            "value": metric_value, "unit": unit,
        })
    return {
        "schema": baseline.RAW_SCHEMA,
        "recorded_at": timestamp,
        "interval_s": 60.0,
        "requested_interval_s": 60.0,
        "context": {
            "root": False, "privilege": "user", "power_state": "battery",
            "local_hour": 3, "timezone": "UTC+00:00",
            "sampler": {
                "pid": 1, "start_ticks": 1, "name": "python3",
                "normalized_name": "python3",
            },
            "coverage": {
                "new_processes_zero_based": 0,
                "unmatched_current_processes": 0,
                "missing_endpoint_processes": 0,
            },
        },
        "metrics": metrics,
        "processes": [{
            "pid": 42, "start_ticks": 2, "name": process_name,
            "normalized_name": process_name.lower(),
            "cpu_pct": value, "user_pct": value, "system_pct": 0.0,
            "pkg_idle_wakeups_per_s": 0.0,
            "interrupt_wakeups_per_s": 0.0,
            "diskio_bytes_read_per_s": 0.0,
            "diskio_bytes_written_per_s": 0.0,
            "energy_rate_watts": None, "energy_score_per_s": None,
            "footprint_bytes": 100, "resident_size_bytes": 120,
        }],
        "partial": False,
        "partial_reasons": [],
    }


class TempCorpusMixin:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix=".record-test-", dir=_HERE)
        self.path = os.path.join(self.temp.name, "corpus")

    def tearDown(self):
        self.temp.cleanup()

    def append(self, *records, retention=30):
        with baseline.Corpus(self.path, retention) as corpus:
            for item in records:
                corpus.append(item)


class StoreCase(TempCorpusMixin, unittest.TestCase):
    def test_secure_append_is_one_strict_json_line(self):
        self.append(raw())
        filename = os.path.join(
            self.path, baseline.daily_name(raw()["recorded_at"]))
        with open(filename, "rb") as stream:
            payload = stream.read()
        self.assertTrue(payload.endswith(b"\n"))
        self.assertEqual(payload.count(b"\n"), 1)
        self.assertEqual(json.loads(payload)["schema"], baseline.RAW_SCHEMA)

    def test_maximum_retained_union_fits_raw_line_bound(self):
        item = raw()
        template = item["processes"][0]
        item["processes"] = [
            {
                **template,
                "pid": 10000 + index,
                "start_ticks": index + 1,
                "name": "x" * 255,
                "normalized_name": "x" * 255,
            }
            for index in range(5 * record.MAX_LIMIT + 1)
        ]
        self.append(item)
        filename = os.path.join(
            self.path, baseline.daily_name(item["recorded_at"]))
        self.assertLessEqual(
            os.path.getsize(filename), baseline.MAX_RAW_LINE_BYTES)

    def test_append_never_rewrites_existing_lines(self):
        self.append(raw(1000, 1), raw(1001, 2))
        replay = baseline.replay(self.path, 0)
        self.assertEqual(
            [item["metrics"][0]["value"] for item in replay["records"]],
            [1, 2])

    def test_append_rejects_existing_incomplete_final_line(self):
        self.append(raw())
        filename = os.path.join(
            self.path, baseline.daily_name(raw()["recorded_at"]))
        with open(filename, "rb+") as stream:
            stream.seek(-1, os.SEEK_END)
            stream.truncate()
        with baseline.Corpus(self.path) as corpus:
            with self.assertRaisesRegex(
                    baseline.StoreError, "final line is incomplete"):
                corpus.append(raw(1001))
        result = baseline.replay(self.path, 0)
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(
            result["errors"][0]["reason"], "partial_final_line")

    def test_nonblocking_writer_lock_reports_contention(self):
        first = baseline.Corpus(self.path).acquire()
        try:
            with self.assertRaises(baseline.LockError):
                baseline.Corpus(self.path).acquire()
        finally:
            first.close()

    def test_append_failure_is_explicit(self):
        with baseline.Corpus(self.path) as corpus, \
             mock.patch.object(
                 baseline.os, "write", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(baseline.StoreError, "append"):
                corpus.append(raw())

    def test_retention_failure_is_explicit(self):
        old = datetime.datetime(2026, 1, 1, 12).timestamp()
        self.append(raw(old))
        now = datetime.datetime(2026, 2, 1, 12).timestamp()
        with baseline.Corpus(self.path, 2) as corpus, \
             mock.patch.object(
                 baseline.os, "unlink", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(baseline.StoreError, "retention failed"):
                corpus.retain(now)

    def test_rotation_uses_deterministic_local_daily_names(self):
        first = datetime.datetime(2026, 1, 1, 23, 59).timestamp()
        second = datetime.datetime(2026, 1, 2, 0, 1).timestamp()
        self.append(raw(first), raw(second))
        names = sorted(name for name in os.listdir(self.path)
                       if name.endswith(".jsonl"))
        self.assertEqual(names, ["2026-01-01.jsonl", "2026-01-02.jsonl"])

    def test_retention_removes_only_expired_daily_files(self):
        now = datetime.datetime(2026, 2, 1, 12).timestamp()
        old = datetime.datetime(2026, 1, 1, 12).timestamp()
        recent = datetime.datetime(2026, 1, 31, 12).timestamp()
        self.append(raw(old), raw(recent))
        with baseline.Corpus(self.path, 2) as corpus:
            corpus.retain(now)
        self.assertFalse(os.path.exists(os.path.join(
            self.path, "2026-01-01.jsonl")))
        self.assertTrue(os.path.exists(os.path.join(
            self.path, "2026-01-31.jsonl")))

    def test_missing_store_is_clean_and_empty(self):
        result = baseline.replay(self.path, 0)
        self.assertEqual(result, {
            "records": [], "errors": [], "error_count": 0,
            "errors_omitted": 0, "files": [],
        })

    def test_partial_final_line_is_parsed_but_reported(self):
        self.append(raw())
        name = baseline.daily_name(raw()["recorded_at"])
        with open(os.path.join(self.path, name), "ab") as stream:
            stream.write(json.dumps(raw(1001)).encode())
        result = baseline.replay(self.path, 0)
        self.assertEqual(len(result["records"]), 2)
        self.assertEqual(result["errors"][0]["reason"], "partial_final_line")

    def test_truncated_final_line_reports_partial_and_malformed(self):
        os.mkdir(self.path)
        with open(os.path.join(self.path, "2026-01-01.jsonl"), "wb") as stream:
            stream.write(b'{"schema":"baseline-raw/1"')
        result = baseline.replay(self.path, 0)
        self.assertEqual(
            [error["reason"] for error in result["errors"][:1]],
            ["partial_final_line"])
        self.assertEqual(len(result["errors"]), 2)

    def test_every_malformed_nonobject_and_nonfinite_line_is_reported(self):
        os.mkdir(self.path)
        name = os.path.join(self.path, "2026-01-01.jsonl")
        with open(name, "w") as stream:
            stream.write("{bad}\n")
            stream.write("[]\n")
            stream.write('{"schema":"baseline-raw/1","recorded_at":NaN}\n')
        result = baseline.replay(self.path, 0)
        self.assertEqual(len(result["errors"]), 3)
        self.assertEqual([error["line"] for error in result["errors"]],
                         [1, 2, 3])

    def test_huge_integer_timestamp_is_corrupt_not_a_replay_crash(self):
        os.mkdir(self.path)
        name = os.path.join(self.path, "2026-01-01.jsonl")
        with open(name, "w") as stream:
            stream.write(json.dumps({
                "schema": baseline.RAW_SCHEMA,
                "recorded_at": int("9" * 400),
            }) + "\n")
        result = baseline.replay(self.path, 0)
        self.assertEqual(result["records"], [])
        self.assertEqual(
            result["errors"][0]["reason"], "integer_too_long")

    def test_deeply_nested_json_is_reported_not_raised(self):
        os.mkdir(self.path)
        name = os.path.join(self.path, "2026-01-01.jsonl")
        with open(name, "w") as stream:
            stream.write("[" * 2000 + "0" + "]" * 2000 + "\n")
        result = baseline.replay(self.path, 0)
        self.assertEqual(result["records"], [])
        self.assertEqual(len(result["errors"]), 1)

    def test_long_json_integer_is_rejected_before_conversion(self):
        os.mkdir(self.path)
        name = os.path.join(self.path, "2026-01-01.jsonl")
        with open(name, "w") as stream:
            stream.write(
                '{"schema":"baseline-raw/1","recorded_at":%s}\n'
                % ("9" * 1000))
        result = baseline.replay(self.path, 0)
        self.assertEqual(
            result["errors"][0]["reason"], "integer_too_long")

    def test_overlong_line_is_reported_without_becoming_a_record(self):
        os.mkdir(self.path)
        name = os.path.join(self.path, "2026-01-01.jsonl")
        with open(name, "wb") as stream:
            stream.write(b"{" + b"x" * baseline.MAX_RAW_LINE_BYTES + b"}\n")
        result = baseline.replay(self.path, 0)
        self.assertEqual(result["records"], [])
        self.assertEqual(result["errors"][0]["reason"], "line_too_long")

    def test_replay_error_details_are_bounded_with_exact_total(self):
        os.mkdir(self.path)
        name = os.path.join(self.path, "2026-01-01.jsonl")
        count = baseline.MAX_REPLAY_ERRORS + 10
        with open(name, "w") as stream:
            stream.write("{bad}\n" * count)
        result = baseline.replay(self.path, 0)
        self.assertEqual(len(result["errors"]), baseline.MAX_REPLAY_ERRORS)
        self.assertEqual(result["error_count"], count)
        self.assertEqual(result["errors_omitted"], 10)

    def test_required_metric_and_process_metric_types_are_validated(self):
        missing = raw()
        missing["metrics"] = []
        self.assertRegex(
            baseline.validate_record(missing), r"^missing_metric:")
        invalid = raw()
        invalid["processes"][0]["cpu_pct"] = "fast"
        self.assertEqual(
            baseline.validate_record(invalid),
            "invalid_process_metric:cpu_pct")
        negative = raw()
        negative["metrics"][0]["value"] = -1.0
        self.assertEqual(
            baseline.validate_record(negative), "invalid_metric_domain")
        signed = raw()
        flow = next(
            metric for metric in signed["metrics"]
            if (metric["scope"], metric["metric"])
            == ("battery", "flow_watts"))
        flow["value"] = -10.0
        self.assertIsNone(baseline.validate_record(signed))
        inconsistent = raw()
        inconsistent["context"]["coverage"]["missing_endpoint_processes"] = 1
        self.assertEqual(
            baseline.validate_record(inconsistent),
            "inconsistent_process_coverage")
        impossible = raw()
        impossible["recorded_at"] = 1e20
        self.assertEqual(
            baseline.validate_record(impossible), "invalid_recorded_at")

    def test_symlink_daily_file_is_not_followed(self):
        os.mkdir(self.path)
        target = os.path.join(self.temp.name, "target")
        with open(target, "w") as stream:
            stream.write("{}")
        os.symlink(target, os.path.join(self.path, "2026-01-01.jsonl"))
        with self.assertRaises(baseline.StoreError):
            baseline.replay(self.path, 0)

    def test_file_removed_after_listing_is_tolerated(self):
        self.append(raw())
        real_open = baseline.os.open

        def disappear(path, *args, **kwargs):
            if isinstance(path, str) and path.endswith(".jsonl"):
                raise FileNotFoundError(path)
            return real_open(path, *args, **kwargs)

        with mock.patch.object(baseline.os, "open", side_effect=disappear):
            result = baseline.replay(self.path, 0)
        self.assertEqual(result["records"], [])
        self.assertEqual(result["files"], [])

    def test_effective_sudo_user_home_and_ids(self):
        entry = mock.Mock(pw_dir="/Users/alice", pw_uid=501, pw_gid=20)
        with mock.patch.object(baseline.os, "geteuid", return_value=0), \
             mock.patch.dict(baseline.os.environ, {"SUDO_USER": "alice"}), \
             mock.patch.object(baseline.pwd, "getpwnam", return_value=entry):
            self.assertEqual(
                baseline.effective_user(), ("/Users/alice", 501, 20))
            self.assertEqual(
                baseline.default_store(),
                "/Users/alice/.stethoscope/baseline-raw")

    def test_new_files_are_fchowned_for_effective_sudo_user(self):
        entry = mock.Mock(
            pw_dir=self.temp.name, pw_uid=os.getuid(), pw_gid=os.getgid())
        real_fchown = os.fchown
        calls = []

        def capture(fd, uid, gid):
            calls.append((uid, gid))
            return real_fchown(fd, uid, gid)

        with mock.patch.object(baseline.os, "geteuid", return_value=0), \
             mock.patch.dict(baseline.os.environ, {"SUDO_USER": "alice"}), \
             mock.patch.object(baseline.pwd, "getpwnam", return_value=entry), \
             mock.patch.object(baseline.os, "fchown", side_effect=capture):
            self.append(raw())
        self.assertIn((entry.pw_uid, entry.pw_gid), calls)

    def test_sudo_rejects_user_home_components_owned_by_another_uid(self):
        entry = mock.Mock(
            pw_dir=self.temp.name, pw_uid=os.getuid() + 10000,
            pw_gid=os.getgid() + 10000)
        with mock.patch.object(baseline.os, "geteuid", return_value=0), \
             mock.patch.dict(baseline.os.environ, {"SUDO_USER": "alice"}), \
             mock.patch.object(baseline.pwd, "getpwnam", return_value=entry):
            with self.assertRaisesRegex(
                    baseline.StoreError,
                    "not traversable|unexpected owner"):
                baseline.Corpus(self.path).acquire()


class TimeAndReservoirCase(unittest.TestCase):
    def test_relative_iso_and_local_clock_since_parsing(self):
        now_dt = datetime.datetime(2026, 7, 11, 10, 0).astimezone()
        now = now_dt.timestamp()
        self.assertEqual(baseline.parse_since("3h", now), now - 10800)
        iso = "2026-07-11T01:02:03+00:00"
        self.assertEqual(
            baseline.parse_since(iso, now),
            datetime.datetime.fromisoformat(iso).timestamp())
        expected = now_dt.replace(hour=3, minute=0, second=0,
                                  microsecond=0).timestamp()
        self.assertEqual(baseline.parse_since("3am", now), expected)

    def test_future_clock_means_previous_day(self):
        now_dt = datetime.datetime(2026, 7, 11, 2, 0).astimezone()
        expected = (now_dt - datetime.timedelta(days=1)).replace(
            hour=3, minute=0, second=0, microsecond=0).timestamp()
        self.assertEqual(
            baseline.parse_since("3am", now_dt.timestamp()), expected)

    @unittest.skipUnless(hasattr(time, "tzset"), "requires POSIX timezone rules")
    def test_local_clock_since_uses_dst_rules(self):
        previous_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/New_York"
            time.tzset()
            now = time.mktime((2026, 3, 8, 4, 0, 0, 0, 0, -1))
            expected = time.mktime((2026, 3, 8, 1, 0, 0, 0, 0, -1))
            self.assertEqual(baseline.parse_since("1am", now), expected)
        finally:
            if previous_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous_tz
            time.tzset()

    def test_nonfinite_relative_since_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "finite"):
            baseline.parse_since("9" * 400 + "h", 1000)

    @unittest.skipUnless(hasattr(time, "tzset"), "requires POSIX timezone rules")
    def test_extreme_naive_iso_is_a_value_error(self):
        previous_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/New_York"
            time.tzset()
            with self.assertRaises(ValueError):
                baseline.parse_since(
                    "9999-12-31T23:59:59", time.time())
        finally:
            if previous_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous_tz
            time.tzset()

    def test_reservoir_is_bounded_deterministic_and_counts_all(self):
        first = baseline.Reservoir(10, seed=7)
        second = baseline.Reservoir(10, seed=7)
        for value in range(1000):
            first.add(value)
            second.add(value)
        self.assertEqual(first.values, second.values)
        self.assertEqual(first.count, 1000)
        self.assertEqual(len(first.values), 10)
        self.assertEqual(first.summary()["sample_count"], 10)

    def test_candidate_reservoirs_bound_name_cardinality(self):
        candidates = baseline.CandidateReservoirs(10)
        for value in range(1000):
            candidates.add("process-%d" % value, value)
        self.assertEqual(len(candidates.entries), 10)
        self.assertGreater(candidates.dropped_values, 0)
        self.assertIn("process-999", candidates.entries)

    def test_percentiles_do_not_overflow_opposite_extremes(self):
        reservoir = baseline.Reservoir()
        reservoir.add(-1e308)
        reservoir.add(1e308)
        self.assertTrue(math.isfinite(reservoir.percentile(50)))
        self.assertTrue(math.isfinite(reservoir.percentile(90)))

    def test_hour_context_and_process_baseline_grouping(self):
        other = raw(1001, 9, "Worker")
        other["context"]["local_hour"] = 4
        rows = baseline.percentile_baselines([raw(), other])
        self.assertTrue(any(
            row["local_hour"] == 3 and row["scope"] == "cpu"
            and row["normalized_process_name"] == "worker"
            for row in rows))
        self.assertEqual({row["local_hour"] for row in rows}, {3, 4})

    def test_empty_baselines_preserve_cold_state(self):
        self.assertEqual(baseline.percentile_baselines([]), [])


class CollectionCase(unittest.TestCase):
    @staticmethod
    def sample(identity, user=0, footprint=100):
        return {
            "identity": identity, "cpu_user_ns": user, "cpu_system_ns": 0,
            "qos_cpu_ns": {name: 0 for name in record.battery._QOS_CLASSES},
            "energy_nj": None, "pkg_idle_wakeups": 0,
            "interrupt_wakeups": 0, "diskio_bytes_read": 0,
            "diskio_bytes_written": 0,
            "phys_footprint_bytes": footprint,
            "resident_size_bytes": footprint + 10,
        }

    def test_once_collection_sleeps_exact_requested_interval(self):
        pid = os.getpid()
        snapshots = [
            {(pid, 10): self.sample((pid, 10), 0)},
            {(pid, 10): self.sample((pid, 10), 500_000_000)},
        ]
        sleeper = mock.Mock()
        memory = {
            "available": True, "errors": [], "total": 1000, "used": 500,
            "free": 500, "active": 0, "inactive": 0, "wired": 0,
            "compressed": 0, "pressure": "normal",
        }
        health = {
            "state": "discharging", "external_connected": False,
            "charge_pct": 50.0, "health_pct": 90.0,
            "battery_flow_watts": -5.0, "probe_error": None,
            "pmset_error": None,
        }
        with mock.patch.object(
                record.battery, "snapshot_power", side_effect=snapshots), \
             mock.patch.object(record.power, "pmenergy_coefficients",
                               return_value=(None, None, "missing")), \
             mock.patch.object(record.vmstat, "system_memory",
                               return_value=memory), \
             mock.patch.object(record.battery, "battery_health",
                               return_value=health), \
             mock.patch.object(record.rusage, "proc_name",
                               return_value="Python Helper (Renderer)"), \
             mock.patch.object(record.cli, "is_root", return_value=False):
            sample, observations = record.collect_interval_observed(
                17.25, sleeper=sleeper,
                monotonic=mock.Mock(
                    side_effect=[10.0, 10.0, 27.25, 27.25]),
                wall_time=mock.Mock(return_value=1000.0))
        sleeper.assert_called_once_with(17.25)
        self.assertEqual(sample["requested_interval_s"], 17.25)
        self.assertEqual(sample["interval_s"], 17.25)
        self.assertEqual(
            sample["context"]["sampler"]["normalized_name"],
            "python helper (renderer)")
        self.assertEqual(sample["context"]["power_state"], "battery")
        self.assertIsNone(baseline.validate_record(sample))
        footprint = next(
            metric for metric in sample["metrics"]
            if metric["scope"] == "sampler"
            and metric["metric"] == "footprint_bytes")
        self.assertEqual(footprint["value"], 100)
        self.assertIs(observations["memory"], memory)
        self.assertIs(observations["battery"], health)
        self.assertNotIn("observations", sample)

    def test_observed_collection_exposes_reused_points_only(self):
        sample = raw()
        observations = {
            "memory": {"pressure": "normal"},
            "battery": {"present": False},
        }
        with mock.patch.object(
                record, "collect_interval_observed",
                return_value=(sample, observations)) as observed:
            result = record.collect_interval(4, 7)
        self.assertIs(result, sample)
        observed.assert_called_once_with(
            4, 7, sleeper=record.time.sleep, wall_time=record.time.time,
            monotonic=record.time.monotonic)
        self.assertNotIn("observations", result)

    def test_per_domain_and_footprint_union_is_bounded(self):
        previous = {}
        current = {}
        for pid in range(1, 101):
            identity = (pid, pid)
            previous[identity] = self.sample(identity, 0, pid)
            current[identity] = self.sample(identity, pid * 1_000_000, pid)
        with mock.patch.object(record.rusage, "proc_name",
                               side_effect=lambda pid, identity: "p%d" % pid):
            rows, _, _ = record._process_rows(
                previous, current, 1.0, None, 5)
        self.assertLessEqual(len(rows), 26)

    def test_per_domain_leaders_survive_raw_limit(self):
        identities = {
            "cpu": (10001, 1),
            "wakeups": (10002, 1),
            "disk": (10003, 1),
            "energy": (10004, 1),
            "memory": (10005, 1),
        }
        previous = {
            identity: self.sample(identity)
            for identity in identities.values()
        }
        current = {
            identity: self.sample(identity)
            for identity in identities.values()
        }
        current[identities["cpu"]]["cpu_user_ns"] = 900_000_000
        current[identities["wakeups"]]["pkg_idle_wakeups"] = 900
        current[identities["disk"]]["diskio_bytes_written"] = 1_000_000_000
        previous[identities["energy"]]["energy_nj"] = 0
        current[identities["energy"]]["energy_nj"] = 100_000_000_000
        current[identities["memory"]]["phys_footprint_bytes"] = 10_000
        with mock.patch.object(
                record.rusage, "proc_name",
                side_effect=lambda pid, identity: "p%d" % pid):
            rows, _, _ = record._process_rows(
                previous, current, 1.0, None, 1)
        self.assertEqual(
            {(row["pid"], row["start_ticks"]) for row in rows},
            set(identities.values()))

    def test_sampler_does_not_consume_footprint_limit(self):
        sampler = (os.getpid(), 1)
        worker = (10001, 1)
        previous = {
            sampler: self.sample(sampler, footprint=1000),
            worker: self.sample(worker, footprint=500),
        }
        current = {
            sampler: self.sample(sampler, footprint=1000),
            worker: self.sample(worker, footprint=500),
        }
        with mock.patch.object(
                record.rusage, "proc_name",
                side_effect=lambda pid, identity: "p%d" % pid):
            rows, _, _ = record._process_rows(
                previous, current, 1.0, None, 1)
        self.assertEqual(
            {(row["pid"], row["start_ticks"]) for row in rows},
            {sampler, worker})

    def test_process_started_during_interval_is_zero_based(self):
        identity = (42, 100)
        current = {identity: self.sample(
            identity, user=1_000_000_000)}
        with mock.patch.object(
                record.rusage, "proc_name", return_value="short-job"):
            rows, totals, coverage = record._process_rows(
                {}, current, 2.0, None, 5, interval_start_ticks=50)
        self.assertEqual(rows[0]["cpu_pct"], 50.0)
        self.assertEqual(totals["cpu_pct"], 50.0)
        self.assertEqual(coverage["new_processes_zero_based"], 1)

    def test_missing_process_endpoint_is_explicit(self):
        identity = (42, 100)
        _, _, coverage = record._process_rows(
            {identity: self.sample(identity)}, {}, 2.0, None, 5,
            interval_start_ticks=50)
        self.assertEqual(coverage["missing_endpoint_processes"], 1)

    def test_incomplete_coefficients_do_not_fabricate_zero_score(self):
        identity = (42, 100)
        previous = {identity: self.sample(identity, 0)}
        current = {identity: self.sample(identity, 1_000_000_000)}
        with mock.patch.object(
                record.rusage, "proc_name", return_value="worker"):
            rows, totals, _ = record._process_rows(
                previous, current, 1.0, {"kcpu_time": 1.0}, 5)
        self.assertIsNone(rows[0]["energy_score_per_s"])
        self.assertIsNone(totals["energy_score_per_s"])

    def test_elapsed_uses_matching_snapshot_midpoints(self):
        pid = os.getpid()
        identity = (pid, 10)
        snapshots = [
            {identity: self.sample(identity, 0)},
            {identity: self.sample(identity, 12_000_000_000)},
        ]
        memory = {
            "available": True, "errors": [], "total": 1000, "used": 500,
            "free": 500, "active": 0, "inactive": 0, "wired": 0,
            "compressed": 0, "pressure": "normal",
        }
        health = {
            "state": None, "external_connected": None,
            "charge_pct": None, "health_pct": None,
            "battery_flow_watts": None, "probe_error": None,
            "pmset_error": None,
        }
        with mock.patch.object(
                record.battery, "snapshot_power", side_effect=snapshots), \
             mock.patch.object(
                 record.power, "pmenergy_coefficients",
                 return_value=(None, None, "missing")), \
             mock.patch.object(
                 record.vmstat, "system_memory", return_value=memory), \
             mock.patch.object(
                 record.battery, "battery_health", return_value=health), \
             mock.patch.object(
                 record.rusage, "proc_name", return_value="python3"), \
             mock.patch.object(record.cli, "is_root", return_value=True):
            sample = record.collect_interval(
                10, sleeper=mock.Mock(),
                monotonic=mock.Mock(side_effect=[0.0, 2.0, 12.0, 14.0]),
                wall_time=mock.Mock(return_value=1000.0))
        self.assertEqual(sample["interval_s"], 12.0)
        cpu = next(
            metric["value"] for metric in sample["metrics"]
            if metric["scope"] == "cpu" and metric["metric"] == "cpu_pct")
        self.assertEqual(cpu, 100.0)


class HistoryAndCliCase(TempCorpusMixin, unittest.TestCase):
    def test_history_summaries_and_top_consumers_share_result(self):
        self.append(raw(1000, 2, "alpha"), raw(1001, 8, "beta"))
        result = record.history_result(self.path, 0, scope="cpu", limit=1)
        self.assertEqual(result["record_count"], 2)
        self.assertEqual(result["summaries"][0]["count"], 2)
        cpu_rows = [
            row for row in result["top_consumers"]
            if row["metric"] == "cpu_pct"
        ]
        self.assertEqual(len(cpu_rows), 1)
        self.assertEqual(
            cpu_rows[0]["normalized_process_name"], "beta")

    def test_top_consumer_limit_is_applied_per_metric(self):
        item = raw(1000, 90, "cpu-hog")
        item["processes"].append({
            **item["processes"][0],
            "pid": 43,
            "name": "memory-hog",
            "normalized_name": "memory-hog",
            "cpu_pct": 1.0,
            "footprint_bytes": 1024 ** 3,
        })
        self.append(item)
        result = record.history_result(self.path, 0, limit=1)
        metrics = {
            row["metric"] for row in result["top_consumers"]
        }
        self.assertIn("cpu_pct", metrics)
        self.assertIn("footprint_bytes", metrics)

    def test_history_streams_without_materializing_replay(self):
        self.append(raw())
        with mock.patch.object(
                baseline, "replay",
                side_effect=AssertionError("history must stream")):
            result = record.history_result(self.path, 0)
        self.assertEqual(result["record_count"], 1)

    def test_history_baseline_has_context_buckets(self):
        self.append(raw())
        result = record.history_result(
            self.path, 0, scope="cpu", baseline_mode=True)
        self.assertFalse(result["cold"])
        self.assertTrue(result["buckets"])
        self.assertEqual(result["buckets"][0]["sample_count"], 1)

    def test_history_preserves_source_partial_visibility_without_exit_four(self):
        item = raw()
        item["partial"] = True
        item["partial_reasons"] = ["not_root"]
        self.append(item)
        options = cli.parse_options([
            "--json", "--since", "100d", "--store", self.path],
            extras={"since", "store"})
        output = io.StringIO()
        with redirect_stdout(output):
            status = record.cmd_history(options, since=0)
        document = json.loads(output.getvalue())
        self.assertEqual(status, cli.EXIT_OK)
        self.assertTrue(document["partial"])
        self.assertEqual(document["partial_reasons"], ["not_root"])

    def test_corrupt_history_is_partial_and_exit_four(self):
        self.append(raw())
        name = baseline.daily_name(raw()["recorded_at"])
        with open(os.path.join(self.path, name), "a") as stream:
            stream.write("{bad}\n")
        options = cli.parse_options([
            "--json", "--since", "100d", "--store", self.path],
            extras={"since", "store"})
        output = io.StringIO()
        with redirect_stdout(output):
            status = record.cmd_history(options)
        document = json.loads(output.getvalue())
        self.assertEqual(status, cli.EXIT_ERROR)
        self.assertTrue(document["partial"])
        self.assertEqual(document["partial_reasons"], ["corrupt_store"])

    def test_empty_history_is_clean_cold_and_exit_zero(self):
        options = cli.parse_options([
            "--json", "--since", "1h", "--store", self.path],
            extras={"since", "store"})
        output = io.StringIO()
        with redirect_stdout(output):
            status = record.cmd_history(options)
        self.assertEqual(status, cli.EXIT_OK)
        self.assertTrue(json.loads(output.getvalue())["cold"])

    def test_history_accepts_zero_or_one_scope_and_uses_scope(self):
        for args in (
                ["stethoscope history", "--json", "--store", self.path],
                ["stethoscope history", "cpu", "--json", "--store", self.path],
                ["stethoscope history", "baseline", "cpu", "--json",
                 "--store", self.path]):
            with self.subTest(args=args):
                output = io.StringIO()
                with redirect_stdout(output):
                    status = record.main(args)
                self.assertEqual(status, cli.EXIT_OK)
        self.assertEqual(json.loads(output.getvalue())["requested_scope"], "cpu")

    def test_rejects_extra_unknown_positionals_and_flags(self):
        cases = [
            ["stethoscope record", "cpu"],
            ["stethoscope history", "cpu", "disk"],
            ["stethoscope history", "unknown"],
            ["stethoscope history", "--once"],
            ["stethoscope record", "--since", "3am"],
            ["stethoscope record", "--bogus"],
            ["stethoscope record", "--store", "--once"],
            ["stethoscope history", "--since", "--json"],
        ]
        for args in cases:
            with self.subTest(args=args):
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(record.main(args), cli.EXIT_USAGE)

    def test_invalid_since_is_usage_error(self):
        with redirect_stderr(io.StringIO()):
            status = record.main([
                "stethoscope history", "--since", "definitely-not-a-time",
                "--json", "--store", self.path,
            ])
        self.assertEqual(status, cli.EXIT_USAGE)

    def test_extreme_since_and_limit_are_usage_errors(self):
        cases = [
            ["stethoscope history", "--since", "1" + "0" * 305 + "h"],
            ["stethoscope history", "--since", "1000000000000d"],
            ["stethoscope history", "--limit", str(record.MAX_LIMIT + 1)],
            ["stethoscope record", "--interval",
             str(record.MAX_INTERVAL + 1)],
            ["stethoscope record", "--duration",
             str(record.MAX_DURATION + 1)],
            ["stethoscope record", "--retention-days",
             str(baseline.MAX_RETENTION_DAYS + 1)],
        ]
        for args in cases:
            with self.subTest(args=args), redirect_stderr(io.StringIO()):
                self.assertEqual(record.main(args), cli.EXIT_USAGE)

    def test_record_once_appends_one_sample_and_strict_json(self):
        options = cli.parse_options([
            "--once", "--interval", "7", "--store", self.path, "--json"],
            extras={"store"})
        sample = raw(time.time())
        output = io.StringIO()
        with mock.patch.object(record, "collect_interval",
                               return_value=sample) as collect, \
             redirect_stdout(output):
            status = record.cmd_record(options)
        self.assertEqual(status, cli.EXIT_OK)
        collect.assert_called_once_with(7.0, 20)
        document = json.loads(output.getvalue())
        self.assertEqual(document["schema"], "stethoscope/1")
        self.assertTrue(document["stored"])
        self.assertIsNone(document["error"])
        self.assertEqual(len(baseline.replay(self.path, 0)["records"]), 1)

    def test_record_error_json_keeps_stable_fields(self):
        options = cli.parse_options([
            "--once", "--interval", "7", "--store", self.path, "--json"],
            extras={"store"})
        output = io.StringIO()
        with mock.patch.object(
                record, "collect_interval",
                side_effect=record.CollectionError("clock failed")), \
             redirect_stdout(output):
            status = record.cmd_record(options)
        document = json.loads(output.getvalue())
        self.assertEqual(status, cli.EXIT_ERROR)
        self.assertFalse(document["stored"])
        self.assertIsNone(document["recorded_at"])
        self.assertEqual(document["metrics"], [])
        self.assertEqual(document["processes"], [])
        self.assertEqual(document["requested_interval_s"], 7.0)

    def test_invalid_sample_does_not_poison_error_json(self):
        options = cli.parse_options([
            "--once", "--store", self.path, "--json"], extras={"store"})
        sample = raw()
        sample["recorded_at"] = float("inf")
        output = io.StringIO()
        with mock.patch.object(
                record, "collect_interval", return_value=sample), \
             redirect_stdout(output):
            status = record.cmd_record(options)
        document = json.loads(output.getvalue())
        self.assertEqual(status, cli.EXIT_ERROR)
        self.assertIsNone(document["recorded_at"])
        self.assertEqual(document["metrics"], [])

    def test_retention_failure_reports_that_sample_was_stored(self):
        options = cli.parse_options([
            "--once", "--store", self.path, "--json"], extras={"store"})
        sample = raw(time.time())
        output = io.StringIO()
        with mock.patch.object(
                record, "collect_interval", return_value=sample), \
             mock.patch.object(
                 baseline.Corpus, "retain",
                 side_effect=baseline.StoreError("retention failed")), \
             redirect_stdout(output):
            status = record.cmd_record(options)
        document = json.loads(output.getvalue())
        self.assertEqual(status, cli.EXIT_ERROR)
        self.assertTrue(document["stored"])
        self.assertEqual(
            len(baseline.replay(self.path, 0)["records"]), 1)

    def test_history_error_json_keeps_mode_fields(self):
        options = cli.parse_options([
            "--json", "--store", self.path], extras={"store"})
        output = io.StringIO()
        with mock.patch.object(
                record, "history_result",
                side_effect=baseline.StoreError("unsafe store")), \
             redirect_stdout(output):
            status = record.cmd_history(
                options, baseline_mode=True, since=0)
        document = json.loads(output.getvalue())
        self.assertEqual(status, cli.EXIT_ERROR)
        self.assertEqual(document["buckets"], [])
        self.assertEqual(document["replay_errors"], [])
        self.assertEqual(document["record_count"], 0)

    def test_default_store_failures_keep_stable_error_documents(self):
        record_options = cli.parse_options(["--json", "--once"])
        history_options = cli.parse_options(["--json"])
        for command, options in (
                (record.cmd_record, record_options),
                (record.cmd_history, history_options)):
            with self.subTest(command=command.__name__), \
                 mock.patch.object(
                     baseline, "default_store",
                     side_effect=baseline.StoreError("bad sudo user")), \
                 redirect_stdout(io.StringIO()) as output:
                status = command(options)
            document = json.loads(output.getvalue())
            self.assertEqual(status, cli.EXIT_ERROR)
            self.assertIsNone(document["store"])
            self.assertIsNotNone(document["error"])

    def test_help_documents_every_accepted_flag(self):
        for flag in (
                "--json", "--once", "--duration", "--interval", "--limit",
                "--store", "--retention-days", "--since"):
            self.assertIn(flag, record.USAGE)


if __name__ == "__main__":
    unittest.main()
