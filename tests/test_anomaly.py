"""Hermetic history, diagnosis, and anomaly CLI tests."""

import io
import json
import math
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from core import baseline, cli, stats
from diagnosis import rules, taxonomy
from scopes import anomaly, record

HERE = os.path.dirname(os.path.abspath(__file__))
MIB = 1024 * 1024


def raw(timestamp=1000.0, context=None, processes=None, partial_reasons=None):
    context = context or {
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
    }
    values = {
        ("cpu", "cpu_pct"): 0.0,
        ("memory", "used_bytes"): 6 * 1024 ** 3,
        ("memory", "free_bytes"): 2 * 1024 ** 3,
    }
    metrics = [{
        "scope": scope, "metric": metric,
        "value": values.get((scope, metric), 0.0), "unit": unit,
    } for (scope, metric), unit in baseline.REQUIRED_METRICS.items()]
    processes = processes if processes is not None else [{
        "pid": 42, "start_ticks": 7, "name": "Worker",
        "normalized_name": "worker", "cpu_pct": 0.0, "user_pct": 0.0,
        "system_pct": 0.0, "pkg_idle_wakeups_per_s": 0.0,
        "interrupt_wakeups_per_s": 0.0,
        "diskio_bytes_read_per_s": 0.0,
        "diskio_bytes_written_per_s": 0.0,
        "energy_rate_watts": None, "energy_score_per_s": None,
        "footprint_bytes": 100 * MIB, "resident_size_bytes": 110 * MIB,
    }]
    partial_reasons = list(partial_reasons or ())
    return {
        "schema": baseline.RAW_SCHEMA,
        "recorded_at": timestamp,
        "interval_s": 1.0,
        "requested_interval_s": 1.0,
        "context": context,
        "metrics": metrics,
        "processes": processes,
        "partial": bool(partial_reasons),
        "partial_reasons": partial_reasons,
    }


class HistoryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix=".anomaly-test-", dir=HERE)
        self.path = os.path.join(self.temp.name, "corpus")

    def tearDown(self):
        self.temp.cleanup()

    def append(self, *records):
        with baseline.Corpus(self.path) as corpus:
            for item in records:
                corpus.append(item)

    def test_empty_history_is_explicit_and_streamed(self):
        sample = raw()
        with mock.patch.object(
                baseline, "replay",
                side_effect=AssertionError("must not materialize replay")):
            state, history = anomaly.scan_history(self.path, 0, sample)
        self.assertTrue(history["cold"])
        self.assertEqual(history["record_count"], 0)
        self.assertEqual(state.leaks[(42, 7)].count, 1)

    def test_context_baselines_and_current_names_only(self):
        matching = raw(100)
        matching["metrics"][0]["value"] = 12
        other = raw(200, context=dict(matching["context"], local_hour=4))
        other["metrics"][0]["value"] = 99
        other["processes"][0]["normalized_name"] = "other"
        self.append(matching, other)
        state, history = anomaly.scan_history(self.path, 0, raw(300))
        self.assertEqual(history["matching_context_records"], 1)
        self.assertEqual(state.system[("cpu", "cpu_pct")].values, [12.0])
        self.assertEqual(set(state.process), {"worker"})

    def test_pid_start_identity_prevents_reuse_contamination(self):
        wrong = raw(100)
        wrong["processes"][0]["start_ticks"] = 6
        right = raw(200)
        right["processes"][0]["footprint_bytes"] = 130 * MIB
        self.append(wrong, right)
        state, _ = anomaly.scan_history(self.path, 0, raw(300))
        trend = state.leaks[(42, 7)]
        self.assertEqual(trend.count, 2)
        self.assertEqual(trend.first_value, 130 * MIB)
        self.assertEqual(trend.last_value, 100 * MIB)

    def test_live_endpoint_completes_leak_evidence(self):
        for index, footprint in enumerate((100, 120, 140, 160)):
            item = raw(index * 600)
            item["processes"][0]["footprint_bytes"] = footprint * MIB
            self.append(item)
        current = raw(2400)
        current["processes"][0]["footprint_bytes"] = 180 * MIB
        state, _ = anomaly.scan_history(self.path, 0, current)
        evidence = stats.leak_evidence(state.leaks[(42, 7)])
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["sample_count"], 5)
        self.assertEqual(evidence["current_footprint_bytes"], 180 * MIB)

    def test_out_of_order_trend_samples_are_counted(self):
        self.append(raw(200), raw(100))
        state, history = anomaly.scan_history(self.path, 0, raw(300))
        self.assertEqual(state.trend_invalid_count, 1)
        self.assertEqual(history["trend_invalid_count"], 1)

    def test_corruption_retains_exact_diagnostics(self):
        self.append(raw())
        filename = os.path.join(self.path, baseline.daily_name(1000))
        with open(filename, "ab") as stream:
            stream.write(b"{bad}\n")
        _, history = anomaly.scan_history(self.path, 0, raw())
        self.assertEqual(history["replay_error_count"], 1)
        self.assertEqual(history["replay_errors_omitted"], 0)
        self.assertEqual(history["replay_errors"][0]["line"], 2)

    def test_source_partial_reason_propagates(self):
        self.append(raw(partial_reasons=["not_root"]))
        state, history = anomaly.scan_history(self.path, 0, raw())
        self.assertEqual(state.source_partial_reasons, ["not_root"])
        self.assertEqual(history["source_partial_reasons"], ["not_root"])

    def test_sampler_rows_do_not_pollute_process_baselines(self):
        historical = raw(100)
        sampler = historical["processes"][0]
        sampler.update({
            "pid": 1, "start_ticks": 1, "name": "python3",
            "normalized_name": "python3", "cpu_pct": 0.0,
        })
        self.append(historical)
        current = raw(200)
        current["processes"][0]["normalized_name"] = "python3"
        state, _ = anomaly.scan_history(self.path, 0, current)
        self.assertEqual(state.process["python3"]["cpu_pct"].count, 0)

    def test_current_sampler_identity_is_excluded_from_old_records(self):
        current = raw(200)
        current["context"]["sampler"].update({
            "pid": 42, "start_ticks": 7,
            "name": "python3", "normalized_name": "python3",
        })
        current["processes"][0]["normalized_name"] = "python3"
        current["processes"].append({
            **current["processes"][0],
            "pid": 50,
            "start_ticks": 8,
        })
        historical = raw(100)
        historical["context"]["sampler"].update({
            "pid": 2, "start_ticks": 2,
        })
        historical["processes"][0].update({
            "pid": 42, "start_ticks": 7,
            "normalized_name": "python3", "cpu_pct": 99,
        })
        self.append(historical)
        state, _ = anomaly.scan_history(self.path, 0, current)
        self.assertEqual(state.process["python3"]["cpu_pct"].count, 0)

    def test_late_sampler_discovery_resets_tainted_name_bucket(self):
        current = raw(300)
        first = raw(100)
        first["processes"][0]["cpu_pct"] = 99
        second = raw(200)
        second["context"]["sampler"].update({
            "pid": 42, "start_ticks": 7,
        })
        self.append(first, second)
        state, history = anomaly.scan_history(self.path, 0, current)
        self.assertEqual(state.process["worker"]["cpu_pct"].count, 0)
        self.assertEqual(state.sampler_baseline_resets, 1)
        self.assertEqual(history["sampler_baseline_resets"], 1)

    def test_historical_sampler_identity_is_not_a_current_target(self):
        for index in range(5):
            item = raw(index * 600)
            process = item["processes"][0]
            item["context"]["sampler"].update({
                "pid": process["pid"],
                "start_ticks": process["start_ticks"],
            })
            process["cpu_pct"] = 100
            process["footprint_bytes"] = (100 + index * 100) * MIB
            self.append(item)
        current = raw(3000)
        current["processes"][0]["cpu_pct"] = 100
        state, history = anomaly.scan_history(self.path, 0, current)
        findings, _ = anomaly.analyze(
            "triage", current, state, history,
            points={
                "memory": {"pressure": "normal"},
                "battery": {"present": False},
                "smart": {"drives": []},
            })
        self.assertEqual(findings, [])


class RulesCase(unittest.TestCase):
    def test_process_runaway_uses_normalized_name_not_pid(self):
        current = raw()["processes"]
        current[0]["cpu_pct"] = 80
        baselines = {"worker": {"cpu_pct": [0] * 8}}
        findings = rules.runaway_findings(current, baselines)
        self.assertEqual(findings[0]["code"], "process_cpu_pct_runaway")
        self.assertEqual(
            findings[0]["evidence"]["baseline_source"],
            "history_and_static_threshold")

    def test_pkg_and_interrupt_findings_are_separate(self):
        process = raw()["processes"][0]
        process["pkg_idle_wakeups_per_s"] = 1200
        process["interrupt_wakeups_per_s"] = 3000
        findings = rules.runaway_findings([process], {})
        self.assertEqual({item["code"] for item in findings}, {
            "process_pkg_idle_wakeups_per_s_runaway",
            "process_interrupt_wakeups_per_s_runaway",
        })

    def test_leak_requires_current_identity(self):
        process = raw()["processes"][0]
        trend = stats.trend([
            (i * 600, (100 + i * 20) * MIB) for i in range(6)])
        self.assertEqual(rules.leak_findings(
            [process], {(42, 7): trend})[0]["detector"], "leak")
        self.assertEqual(rules.leak_findings(
            [process], {(42, 8): trend}), [])

    def test_point_findings_and_supported_absence(self):
        drives = [{
            "device": "disk0", "smart_status": "failing",
            "warnings": [{
                "code": "smart_failing", "severity": "critical",
                "message": "back up",
            }],
        }]
        findings = rules.point_findings(
            {"pressure": "warn"},
            {"present": True, "condition": "Service Recommended",
             "health_pct": 70},
            drives)
        self.assertEqual(taxonomy.overall(findings), "critical")
        self.assertEqual(rules.point_findings(
            {"pressure": "normal"}, {"present": False}, []), [])

    def test_unknown_memory_pressure_is_not_healthy(self):
        finding = rules.point_findings(
            {"pressure": "unknown"}, {"present": False}, [])[0]
        self.assertEqual(finding["code"], "memory_pressure_unknown")
        self.assertEqual(finding["severity"], "info")

    def test_sort_and_overall_are_deterministic(self):
        first = taxonomy.finding(
            "b", "warn", "cpu", "x", "b", 4, "low", [], {})
        second = taxonomy.finding(
            "a", "critical", "memory", "x", "a", 1, "high", [], {})
        ordered = taxonomy.sort_findings([first, second])
        self.assertEqual([item["code"] for item in ordered], ["a", "b"])
        self.assertEqual(taxonomy.overall(ordered), "critical")

    def test_current_sampler_is_not_a_runaway_target(self):
        sample = raw()
        process = sample["processes"][0]
        sample["context"]["sampler"].update({
            "pid": process["pid"],
            "start_ticks": process["start_ticks"],
        })
        process["cpu_pct"] = 100
        state = anomaly.HistoryState(sample)
        history = anomaly._empty_history("store", 0)
        findings, _ = anomaly.analyze(
            "runaway", sample, state, history)
        self.assertEqual(findings, [])


def clean_document(scope, mode):
    return anomaly._document(
        scope, mode, [], [], [], anomaly._empty_history("store", 0),
        {"external": "ok"}, None)


class CliCase(unittest.TestCase):
    def invoke(self, argv, document=None, code=0):
        out, err = io.StringIO(), io.StringIO()
        if document is None:
            scope = "triage" if argv[0].endswith("triage") else "anomaly"
            mode = "triage" if scope == "triage" else argv[1]
            document = clean_document(scope, mode)
        with mock.patch.object(anomaly, "run", return_value=(document, code)), \
             redirect_stdout(out), redirect_stderr(err):
            result = anomaly.main(argv)
        return result, out.getvalue(), err.getvalue()

    def test_every_mode_human_and_json(self):
        commands = [
            ["stethoscope anomaly", "deviation"],
            ["stethoscope anomaly", "leaks"],
            ["stethoscope anomaly", "runaway"],
            ["stethoscope anomaly", "triage"],
            ["stethoscope triage"],
        ]
        for command in commands:
            with self.subTest(command=command):
                rc, out, _ = self.invoke(command)
                self.assertEqual(rc, 0)
                self.assertIn("overall: ok", out)
                rc, out, _ = self.invoke(command + ["--json"])
                self.assertEqual(rc, 0)
                parsed = json.loads(out)
                self.assertIn("overall", parsed)
                self.assertIn("history", parsed)
                self.assertIn("current", parsed)
                self.assertIn("error", parsed)

    def test_critical_and_runtime_exit_codes(self):
        finding = taxonomy.finding(
            "critical", "critical", "memory", "point", "bad", 100,
            "high", [], {})
        document = anomaly._document(
            "triage", "triage", [], [finding], [], {}, {}, None)
        self.assertEqual(self.invoke(
            ["stethoscope triage"], document, cli.EXIT_FINDINGS)[0], 1)
        document["error"] = "probe"
        self.assertEqual(self.invoke(
            ["stethoscope triage"], document, cli.EXIT_ERROR)[0], 4)

    def test_rejects_missing_mode_flags_positionals_and_maxima(self):
        cases = [
            ["stethoscope anomaly", "--json"],
            ["stethoscope anomaly", "leaks", "extra"],
            ["stethoscope anomaly", "leak"],
            ["stethoscope anomaly", "leaks", "--once"],
            ["stethoscope triage", "--duration", "2"],
            ["stethoscope anomaly", "runaway", "--limit", "257"],
            ["stethoscope triage", "--interval", "61"],
            ["stethoscope triage", "--since", "nonsense"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                rc, out, err = self.invoke(argv)
                self.assertEqual(rc, 2)
                if "--json" in argv:
                    self.assertEqual(json.loads(out)["error"] is None, False)
                else:
                    self.assertTrue(err)

    def test_usage_json_preserves_selected_mode(self):
        rc, out, _ = self.invoke([
            "stethoscope anomaly", "deviation", "--limit", "nope", "--json"])
        document = json.loads(out)
        self.assertEqual(rc, cli.EXIT_USAGE)
        self.assertEqual(document["command"], "deviation")
        self.assertEqual(document["mode"], "deviation")

    def test_strict_json_replaces_nonfinite_probe_value(self):
        document = clean_document("triage", "triage")
        document["current"] = {"value": float("nan")}
        safe = anomaly._json_safe(document)
        encoded = json.dumps(safe, allow_nan=False)
        self.assertNotIn("NaN", encoded)


class RunCase(unittest.TestCase):
    def test_empty_history_has_no_expensive_fallback(self):
        sample = raw()
        replay = {
            "record_count": 0, "errors": [], "error_count": 0,
            "errors_omitted": 0, "files": [],
        }
        with mock.patch.object(record, "collect_interval", return_value=sample), \
             mock.patch.object(baseline, "scan", return_value=replay), \
             mock.patch.object(stats, "leak_evidence",
                               wraps=stats.leak_evidence) as detector:
            document, rc = anomaly.run("leaks", since=0, store="missing")
        self.assertEqual(rc, 0)
        self.assertTrue(document["history"]["cold"])
        self.assertIn("no short live fallback", " ".join(document["notes"]))
        detector.assert_called()

    def test_corrupt_history_returns_four_even_with_finding(self):
        sample = raw()
        state = anomaly.HistoryState(sample)
        history = anomaly._empty_history("store", 0)
        history["replay_error_count"] = 1
        history["replay_errors"] = [
            {"file": "x", "line": 1, "reason": "bad"}]
        with mock.patch.object(record, "collect_interval", return_value=sample), \
             mock.patch.object(anomaly, "scan_history",
                               return_value=(state, history)):
            document, rc = anomaly.run("deviation", since=0, store="store")
        self.assertEqual(rc, 4)
        self.assertIn("corrupt_store", document["partial_reasons"])
        self.assertIsNotNone(document["error"])

    def test_history_failure_retains_static_and_point_findings(self):
        sample = raw()
        sample["processes"][0]["cpu_pct"] = 100
        points = {
            "memory": {"pressure": "normal"},
            "battery": {"present": False},
            "smart": {"drives": []},
        }
        with mock.patch.object(
                record, "collect_interval_observed",
                return_value=(sample, {
                    "memory": points["memory"],
                    "battery": points["battery"],
                })), \
             mock.patch.object(
                 anomaly, "scan_history",
                 side_effect=baseline.StoreError("unreadable")), \
             mock.patch.object(
                 anomaly, "_collect_points",
                 return_value=(points, [], [])):
            document, rc = anomaly.run(
                "triage", since=0, store="store", scope="triage")
        self.assertEqual(rc, cli.EXIT_ERROR)
        self.assertEqual(document["overall"], "critical")
        self.assertTrue(document["findings"])
        self.assertIsNotNone(document["current"]["points"])
        self.assertFalse(document["history"]["available"])

    def test_reused_probe_failure_is_reported_once(self):
        sample = raw(partial_reasons=["memory:vm_stat_failed"])
        state = anomaly.HistoryState(sample)
        history = anomaly._empty_history("store", 0)
        points = {
            "memory": {"available": False, "errors": ["vm_stat_failed"]},
            "battery": {"present": False},
            "smart": {"drives": []},
        }
        with mock.patch.object(
                record, "collect_interval_observed",
                return_value=(sample, {})), \
             mock.patch.object(
                 anomaly, "scan_history", return_value=(state, history)), \
             mock.patch.object(
                 anomaly, "_collect_points",
                 return_value=(
                     points, ["memory:vm_stat_failed"],
                     ["memory:vm_stat_failed"])):
            document, rc = anomaly.run(
                "triage", since=0, store="store", scope="triage")
        self.assertEqual(rc, cli.EXIT_ERROR)
        self.assertEqual(document["error"], "memory:vm_stat_failed")

    def test_optional_pmset_partial_reason_is_not_fatal(self):
        sample = raw(partial_reasons=["battery:pmset_failed"])
        state = anomaly.HistoryState(sample)
        state.add_current_leak_endpoint(sample)
        history = anomaly._empty_history("store", 0)
        with mock.patch.object(
                record, "collect_interval", return_value=sample), \
             mock.patch.object(
                 anomaly, "scan_history", return_value=(state, history)):
            document, rc = anomaly.run("runaway", since=0, store="store")
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertTrue(document["partial"])

    def test_not_root_partial_alone_is_not_runtime_failure(self):
        sample = raw(partial_reasons=["not_root"])
        state = anomaly.HistoryState(sample)
        history = anomaly._empty_history("store", 0)
        with mock.patch.object(record, "collect_interval", return_value=sample), \
             mock.patch.object(anomaly, "scan_history",
                               return_value=(state, history)):
            document, rc = anomaly.run("runaway", since=0, store="store")
        self.assertEqual(rc, 0)
        self.assertTrue(document["partial"])

    def test_rejected_live_sample_is_not_exposed_as_current(self):
        sample = raw()
        sample["schema"] = "bad"
        with mock.patch.object(
                record, "collect_interval", return_value=sample):
            document, rc = anomaly.run(
                "runaway", since=0, store="store")
        self.assertEqual(rc, cli.EXIT_ERROR)
        self.assertIsNone(document["current"])


if __name__ == "__main__":
    unittest.main()
