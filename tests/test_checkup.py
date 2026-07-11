"""Hermetic full-body checkup composition and CLI tests."""

import io
import json
import math
import os
import runpy
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from core import baseline, cli
from diagnosis import taxonomy
from scopes import anomaly, checkup, record


def sample(partial_reasons=None):
    values = {
        ("cpu", "cpu_pct"): 12.0,
        ("cpu", "pkg_idle_wakeups_per_s"): 3.0,
        ("cpu", "interrupt_wakeups_per_s"): 4.0,
        ("disk", "read_bytes_per_s"): 100.0,
        ("disk", "write_bytes_per_s"): 200.0,
        ("battery", "energy_rate_watts"): 1.5,
        ("battery", "energy_score_per_s"): 2.5,
        ("memory", "used_bytes"): 60.0,
        ("memory", "free_bytes"): 40.0,
    }
    metrics = [{
        "scope": scope, "metric": metric,
        "value": values.get((scope, metric), 0.0), "unit": unit,
    } for (scope, metric), unit in baseline.REQUIRED_METRICS.items()]
    reasons = list(partial_reasons or ())
    return {
        "schema": baseline.RAW_SCHEMA,
        "recorded_at": 1000.0,
        "interval_s": 1.0,
        "requested_interval_s": 1.0,
        "context": {
            "root": True, "privilege": "root", "power_state": "ac",
            "local_hour": 1, "timezone": "UTC+00:00",
            "sampler": {
                "pid": 1, "start_ticks": 1, "name": "stethoscope",
                "normalized_name": "stethoscope",
            },
            "coverage": {
                "new_processes_zero_based": 0,
                "unmatched_current_processes": 0,
                "missing_endpoint_processes": 0,
            },
        },
        "metrics": metrics,
        "processes": [{
            "pid": 42, "start_ticks": 7, "name": "worker",
            "normalized_name": "worker",
            "cpu_pct": 12.0, "user_pct": 10.0, "system_pct": 2.0,
            "pkg_idle_wakeups_per_s": 3.0,
            "interrupt_wakeups_per_s": 4.0,
            "diskio_bytes_read_per_s": 100.0,
            "diskio_bytes_written_per_s": 200.0,
            "energy_rate_watts": 1.5, "energy_score_per_s": 2.5,
            "footprint_bytes": 60, "resident_size_bytes": 70,
        }],
        "partial": bool(reasons),
        "partial_reasons": reasons,
    }


def points(memory=None, battery=None, smart=None):
    return {
        "memory": memory or {
            "available": True, "errors": [], "pressure": "normal",
            "total": 100, "used": 60, "free": 40, "wired": 10,
            "compressed": 5,
        },
        "battery": battery or {
            "present": True, "probe_error": None, "pmset_error": None,
            "condition": "Normal", "charge_pct": 80.0, "health_pct": 90.0,
            "cycle_count": 10, "state": "charging",
            "external_connected": True, "battery_flow_watts": 4.0,
        },
        "smart": smart or {
            "available": True, "diskutil_available": True,
            "physical_drives_present": True, "smartctl_available": True,
            "drives": [{
                "device": "disk0", "name": "SSD", "smart_status": "verified",
                "smartctl_available": True, "smartctl_detail": None,
                "diskutil_detail": None, "warnings": [],
            }],
        },
    }


def diagnosis(findings=None, partial_reasons=None, point_data=None, error=None):
    raw = sample(partial_reasons)
    current = anomaly._current_document(raw, point_data or points())
    ordered = findings if findings is not None else []
    history = anomaly._empty_history("store", 0)
    return anomaly._document(
        "triage", "triage", partial_reasons or (), ordered,
        ["history note"], history, current, error)


class CompositionCase(unittest.TestCase):
    def test_all_healthy_vitals_use_one_sample(self):
        source = diagnosis()
        document = checkup.compose(source)
        self.assertEqual(document["schema"], "stethoscope/1")
        self.assertEqual(document["scope"], "checkup")
        self.assertEqual(document["overall"], "ok")
        self.assertEqual(document["vitals"]["cpu"]["state"], "available")
        self.assertEqual(
            document["vitals"]["disk"]["rates"]["write_bytes_per_s"]["value"],
            200.0)
        self.assertEqual(
            document["vitals"]["cpu"]["top_consumers"][0]["pid"], 42)
        self.assertEqual(document["vitals"]["memory"]["pressure"], "normal")
        self.assertEqual(
            document["vitals"]["memory"]["top_consumers"][0]["pid"], 42)
        self.assertEqual(document["vitals"]["battery"]["state"], "available")
        self.assertEqual(document["vitals"]["smart"]["state"], "available")

    def test_findings_overall_notes_history_and_order_pass_through(self):
        critical = taxonomy.finding(
            "z", "critical", "smart", "point", "critical", 99,
            "high", ["stethoscope smart"], {})
        warning = taxonomy.finding(
            "a", "warn", "cpu", "runaway", "warning", 60,
            "moderate", ["stethoscope cpu top"], {})
        source = diagnosis([critical, warning])
        document = checkup.compose(source)
        self.assertEqual(document["findings"], [critical, warning])
        self.assertEqual(document["overall"], "critical")
        self.assertEqual(document["notes"], source["notes"])
        self.assertEqual(document["history"], source["history"])

    def test_absent_battery_and_no_drives_are_supported_states(self):
        point_data = points(
            battery={
                "present": False, "probe_error": None, "pmset_error": None,
            },
            smart={
                "available": True, "diskutil_available": True,
                "physical_drives_present": False,
                "smartctl_available": False, "drives": [],
            })
        document = checkup.compose(diagnosis(point_data=point_data))
        self.assertEqual(document["vitals"]["battery"]["state"], "absent")
        self.assertTrue(document["vitals"]["battery"]["available"])
        self.assertEqual(document["vitals"]["smart"]["state"], "absent")
        self.assertTrue(document["vitals"]["smart"]["available"])

    def test_unknown_memory_pressure_is_not_available_or_healthy(self):
        point_data = points(memory={
            "available": False, "errors": ["sysctl_failed"],
            "pressure": "unknown", "total": None, "used": None,
            "free": None, "wired": None, "compressed": None,
        })
        vital = checkup.compose(
            diagnosis(point_data=point_data))["vitals"]["memory"]
        self.assertEqual(vital["state"], "unavailable")
        self.assertFalse(vital["available"])
        self.assertEqual(vital["pressure"], "unknown")

        partial_points = points(memory={
            "available": True, "errors": [], "pressure": "unknown",
            "total": 100, "used": 60, "free": 40,
            "wired": 10, "compressed": 5,
        })
        vital = checkup.compose(
            diagnosis(point_data=partial_points))["vitals"]["memory"]
        self.assertEqual(vital["state"], "partial")
        self.assertEqual(vital["pressure"], "unknown")

    def test_missing_smartctl_is_partial_not_healthy(self):
        point_data = points(smart={
            "available": True, "diskutil_available": True,
            "physical_drives_present": True, "smartctl_available": False,
            "drives": [{
                "device": "disk0", "smart_status": "verified",
                "smartctl_available": False, "smartctl_detail": "not found",
                "diskutil_detail": None,
            }],
        })
        vital = checkup.compose(
            diagnosis(partial_reasons=["smartctl_unavailable"],
                      point_data=point_data))["vitals"]["smart"]
        self.assertEqual(vital["state"], "partial")
        self.assertFalse(vital["smartctl_available"])

    def test_diskutil_failure_is_unavailable(self):
        point_data = points(smart={
            "available": False, "diskutil_available": False,
            "physical_drives_present": None, "smartctl_available": True,
            "drives": [],
        })
        vital = checkup.compose(
            diagnosis(partial_reasons=["diskutil_unavailable"],
                      point_data=point_data))["vitals"]["smart"]
        self.assertEqual(vital["state"], "unavailable")
        self.assertFalse(vital["available"])

    def test_non_root_and_source_partial_are_preserved(self):
        source = diagnosis(partial_reasons=["not_root", "source:limited"])
        document = checkup.compose(source)
        self.assertTrue(document["partial"])
        self.assertEqual(
            document["partial_reasons"], ["not_root", "source:limited"])
        self.assertEqual(document["vitals"]["cpu"]["state"], "partial")
        self.assertEqual(document["vitals"]["disk"]["state"], "partial")

    def test_historical_partial_reason_does_not_taint_current_vitals(self):
        source = diagnosis()
        source["partial"] = True
        source["partial_reasons"] = ["not_root"]
        source["history"]["source_partial_reasons"] = ["not_root"]
        document = checkup.compose(source)
        self.assertTrue(document["partial"])
        for scope in ("cpu", "disk", "memory", "battery"):
            self.assertEqual(
                document["vitals"][scope]["state"], "available", scope)

    def test_sampler_is_excluded_from_every_process_ranking(self):
        source = diagnosis()
        source["current"]["processes"].append({
            "pid": 1, "start_ticks": 1, "name": "stethoscope",
            "normalized_name": "stethoscope",
            "cpu_pct": 1000.0, "user_pct": 500.0, "system_pct": 500.0,
            "pkg_idle_wakeups_per_s": 1000.0,
            "interrupt_wakeups_per_s": 1000.0,
            "diskio_bytes_read_per_s": 1000.0,
            "diskio_bytes_written_per_s": 1000.0,
            "energy_rate_watts": 1000.0, "energy_score_per_s": 1000.0,
            "footprint_bytes": 1000, "resident_size_bytes": 1000,
        })
        document = checkup.compose(source)
        for scope in ("cpu", "disk", "memory", "battery"):
            identities = {
                (row["pid"], row["start_ticks"])
                for row in document["vitals"][scope]["top_consumers"]
            }
            self.assertNotIn((1, 1), identities, scope)

    def test_each_process_ranking_uses_its_canonical_metric(self):
        source = diagnosis()
        source["current"]["processes"] = [
            {
                **source["current"]["processes"][0],
                "pid": 10, "start_ticks": 10, "name": "cpu",
                "cpu_pct": 90.0,
                "pkg_idle_wakeups_per_s": 0.0,
                "interrupt_wakeups_per_s": 0.0,
                "energy_score_per_s": 1.0,
                "energy_rate_watts": 100.0,
            },
            {
                **source["current"]["processes"][0],
                "pid": 20, "start_ticks": 20, "name": "wakeups",
                "cpu_pct": 10.0,
                "pkg_idle_wakeups_per_s": 1000.0,
                "interrupt_wakeups_per_s": 1000.0,
                "energy_score_per_s": 2.0,
                "energy_rate_watts": 0.0,
            },
        ]
        document = checkup.compose(source, limit=1)
        self.assertEqual(
            document["vitals"]["cpu"]["top_consumers"][0]["pid"], 10)
        self.assertEqual(
            document["vitals"]["battery"]["top_consumers"][0]["pid"], 20)

    def test_nonfinite_values_become_strict_json_null(self):
        source = diagnosis()
        source["current"]["metrics"][0]["value"] = math.nan
        document = checkup.compose(source)
        encoded = json.dumps(document, allow_nan=False)
        self.assertNotIn("NaN", encoded)


class ProbeCompositionCase(unittest.TestCase):
    def test_triage_collects_memory_battery_and_smart_once(self):
        raw = sample()
        mem = points()["memory"]
        batt = points()["battery"]
        smart_point = points()["smart"]

        def observed(interval, limit):
            return raw, {
                "memory": anomaly.memory.system_memory(),
                "battery": anomaly.battery.battery_health(),
            }

        state = anomaly.HistoryState(raw)
        state.add_current_leak_endpoint(raw)
        history = anomaly._empty_history("store", 0)
        with mock.patch.object(
                record, "collect_interval_observed",
                side_effect=observed) as collect, \
             mock.patch.object(
                 anomaly.memory, "system_memory", return_value=mem) as memory, \
             mock.patch.object(
                 anomaly.battery, "battery_health", return_value=batt) as battery, \
             mock.patch.object(
                 anomaly.smart.probe, "find_smartctl",
                 return_value="/smartctl") as find_smartctl, \
             mock.patch.object(
                 anomaly.smart.probe, "list_physical_drives",
                 return_value=[("disk0", True)]) as list_drives, \
             mock.patch.object(
                 anomaly.smart, "drive_health",
                 return_value=smart_point["drives"][0]) as drive_health, \
             mock.patch.object(
                 anomaly, "scan_history", return_value=(state, history)):
            document, code = anomaly.run(
                "triage", interval=1, limit=20, since=0, store="store",
                scope="triage")
        self.assertEqual(code, cli.EXIT_OK)
        collect.assert_called_once_with(1, 20)
        memory.assert_called_once_with()
        battery.assert_called_once_with()
        find_smartctl.assert_called_once_with()
        list_drives.assert_called_once_with()
        drive_health.assert_called_once_with("disk0", True, "/smartctl")
        self.assertEqual(document["current"]["points"]["memory"], mem)
        self.assertEqual(document["current"]["points"]["battery"], batt)

    def test_checkup_invokes_canonical_diagnosis_once(self):
        source = diagnosis()
        with mock.patch.object(
                checkup.anomaly, "run",
                return_value=(source, cli.EXIT_OK)) as run:
            document, code = checkup.run(
                interval=2, limit=3, since=4, store="store")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(document["scope"], "checkup")
        run.assert_called_once_with(
            "triage", interval=2, limit=3, since=4,
            store="store", scope="triage")

    def test_point_metadata_distinguishes_probe_states(self):
        with mock.patch.object(
                anomaly.smart.probe, "find_smartctl", return_value=None), \
             mock.patch.object(
                 anomaly.smart.probe, "list_physical_drives",
                 return_value=[]):
            point_data, reasons, failures = anomaly._collect_points({
                "memory": points()["memory"],
                "battery": points()["battery"],
            })
        self.assertTrue(point_data["smart"]["diskutil_available"])
        self.assertFalse(point_data["smart"]["physical_drives_present"])
        self.assertFalse(point_data["smart"]["smartctl_available"])
        self.assertEqual(reasons, [])
        self.assertEqual(failures, [])

        with mock.patch.object(
                anomaly.smart.probe, "find_smartctl",
                return_value="/smartctl"), \
             mock.patch.object(
                 anomaly.smart.probe, "list_physical_drives",
                 return_value=None):
            point_data, reasons, failures = anomaly._collect_points({
                "memory": points()["memory"],
                "battery": points()["battery"],
            })
        self.assertFalse(point_data["smart"]["diskutil_available"])
        self.assertIsNone(point_data["smart"]["physical_drives_present"])
        self.assertTrue(point_data["smart"]["smartctl_available"])
        self.assertIn("diskutil_unavailable", reasons)
        self.assertIn("smart:diskutil_unavailable", failures)


class CliCase(unittest.TestCase):
    def invoke(self, argv, source=None, code=cli.EXIT_OK):
        out, err = io.StringIO(), io.StringIO()
        source = source or diagnosis()
        with mock.patch.object(
                checkup.anomaly, "run", return_value=(source, code)), \
             redirect_stdout(out), redirect_stderr(err):
            status = checkup.main(argv)
        return status, out.getvalue(), err.getvalue()

    def test_human_and_strict_json(self):
        status, output, _ = self.invoke(["stethoscope checkup"])
        self.assertEqual(status, cli.EXIT_OK)
        self.assertIn("full-body exam", output)
        self.assertNotIn("healthy", output.lower())
        status, output, _ = self.invoke(["stethoscope checkup", "--json"])
        self.assertEqual(status, cli.EXIT_OK)
        self.assertEqual(output.count("\n"), 1)
        self.assertEqual(json.loads(output)["scope"], "checkup")

    def test_dispatcher_registers_checkup(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        namespace = runpy.run_path(os.path.join(root, "stethoscope"))
        self.assertEqual(
            namespace["SCOPES"]["checkup"]["module"], "scopes.checkup")

    def test_critical_history_and_probe_failures_preserve_exits(self):
        critical = taxonomy.finding(
            "critical", "critical", "smart", "point", "bad", 100,
            "high", [], {})
        cases = [
            (diagnosis([critical]), cli.EXIT_FINDINGS),
            (diagnosis(partial_reasons=["corrupt_store"],
                       error="history:corrupt"), cli.EXIT_ERROR),
            (diagnosis(partial_reasons=["probe_failure"],
                       error="smart:diskutil_unavailable"), cli.EXIT_ERROR),
        ]
        for source, expected in cases:
            with self.subTest(expected=expected):
                status, output, _ = self.invoke(
                    ["stethoscope checkup", "--json"], source, expected)
                document = json.loads(output)
                self.assertEqual(status, expected)
                self.assertEqual(document["error"], source["error"])
                self.assertEqual(document["history"], source["history"])

    def test_usage_json_has_stable_fields_and_rejects_invalid_forms(self):
        cases = [
            ["--once"], ["--duration", "2"], ["extra"], ["--bogus"],
            ["--interval", "61"], ["--limit", "257"],
            ["--since", "not-a-time"],
        ]
        stable = {
            "schema", "scope", "command", "partial", "partial_reasons",
            "overall", "findings", "notes", "history", "sample", "vitals",
            "error",
        }
        for args in cases:
            with self.subTest(args=args):
                status, output, _ = self.invoke(
                    ["stethoscope checkup"] + args + ["--json"])
                document = json.loads(output)
                self.assertEqual(status, cli.EXIT_USAGE)
                self.assertTrue(stable.issubset(document))
                self.assertEqual(document["scope"], "checkup")
                self.assertIsNotNone(document["error"])
                self.assertIsNone(document["sample"])

    def test_runtime_json_has_same_stable_fields(self):
        source = anomaly._document(
            "triage", "triage", ["runtime_failure"], [], [],
            anomaly._empty_history("store", 0), None, "probe failed")
        status, output, _ = self.invoke(
            ["stethoscope checkup", "--json"], source, cli.EXIT_ERROR)
        document = json.loads(output)
        self.assertEqual(status, cli.EXIT_ERROR)
        self.assertIsNone(document["sample"])
        self.assertEqual(
            set(document["vitals"]),
            {"cpu", "disk", "memory", "battery", "smart"})
        self.assertEqual(document["vitals"]["cpu"]["state"], "unavailable")
        self.assertEqual(document["error"], "probe failed")

    def test_human_sanitizes_every_external_string_surface(self):
        bad = "evil\x1b[2J\ntext"
        finding = taxonomy.finding(
            "bad", "warn", bad, bad, bad, 50, "low", [bad], {})
        source = diagnosis([finding], partial_reasons=[bad])
        source["notes"] = [bad]
        source["error"] = bad
        source["history"]["replay_errors"] = [{
            "file": bad, "line": bad, "reason": bad,
        }]
        source["current"]["context"]["privilege"] = bad
        source["current"]["context"]["power_state"] = bad
        source["current"]["points"]["memory"]["pressure"] = bad
        source["current"]["points"]["battery"]["condition"] = bad
        source["current"]["points"]["smart"]["drives"][0].update({
            "device": bad, "name": bad, "smart_status": bad,
        })
        _, output, _ = self.invoke(["stethoscope checkup"], source)
        self.assertNotIn(bad, output)
        self.assertIn("evil?[2J?text", output)


if __name__ == "__main__":
    unittest.main()
