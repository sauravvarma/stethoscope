#!/usr/bin/env python3
"""One-shot baseline deviation, leak, runaway, and triage surfaces."""

import math
import os
import signal
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import baseline, cli, schema, stats
from diagnosis import rules, taxonomy
from scopes import battery, memory, record, smart

DEFAULT_INTERVAL = 1.0
DEFAULT_LIMIT = 20
DEFAULT_SINCE = "24h"
MAX_INTERVAL = 60.0
MAX_LIMIT = 256
MAX_SOURCE_REASONS = 128

MODES = ("deviation", "leaks", "runaway", "triage")
_PROCESS_BASELINE_METRICS = (
    "cpu_pct", "pkg_idle_wakeups_per_s", "interrupt_wakeups_per_s")

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def _context_key(context):
    return (
        context.get("local_hour"),
        context.get("timezone"),
        context.get("privilege"),
        context.get("power_state"),
    )


def _metric_map(sample):
    return {
        (item.get("scope"), item.get("metric")): item.get("value")
        for item in sample.get("metrics", ())
        if isinstance(item, dict)
    }


def _sampler_identity(sample):
    sampler = sample.get("context", {}).get("sampler", {})
    return sampler.get("pid"), sampler.get("start_ticks")


def _diagnostic_processes(sample, excluded_identities=()):
    excluded = set(excluded_identities)
    excluded.add(_sampler_identity(sample))
    return [
        process for process in sample.get("processes", ())
        if (process.get("pid"), process.get("start_ticks")) not in excluded
    ]


class HistoryState:
    """Bounded replay state retained only for the current diagnostic targets."""

    def __init__(self, sample):
        self.context = _context_key(sample.get("context", {}))
        processes = _diagnostic_processes(sample)
        self.sampler_identities = {_sampler_identity(sample)}
        self.identities = {
            (process.get("pid"), process.get("start_ticks"))
            for process in processes
        }
        self.names = {
            process.get("normalized_name")
            for process in processes
            if process.get("normalized_name")
        }
        self.system = {
            key: baseline.Reservoir() for key in rules.SYSTEM_METRIC_POLICIES
        }
        self.process = {
            name: {
                metric: baseline.Reservoir()
                for metric in _PROCESS_BASELINE_METRICS
            }
            for name in self.names
        }
        self.process_contributors = {
            name: set() for name in self.names
        }
        self.leaks = {
            identity: stats.OnlineTrend(recent_size=10)
            for identity in self.identities
        }
        self.matching_context_records = 0
        self.source_partial_reasons = []
        self.source_partial_reasons_omitted = 0
        self.sampler_baseline_resets = 0

    def _remember_sampler(self, identity):
        if identity in self.sampler_identities:
            return
        self.sampler_identities.add(identity)
        for name, contributors in self.process_contributors.items():
            if identity not in contributors:
                continue
            self.process[name] = {
                metric: baseline.Reservoir()
                for metric in _PROCESS_BASELINE_METRICS
            }
            contributors.clear()
            self.sampler_baseline_resets += 1

    def _add_reason(self, reason):
        if reason in self.source_partial_reasons:
            return
        if len(self.source_partial_reasons) < MAX_SOURCE_REASONS:
            self.source_partial_reasons.append(reason)
        else:
            self.source_partial_reasons_omitted += 1

    def add(self, recorded):
        for reason in recorded.get("partial_reasons", ()):
            self._add_reason(reason)
        timestamp = recorded.get("recorded_at")
        sampler_identity = _sampler_identity(recorded)
        self._remember_sampler(sampler_identity)
        for process in recorded.get("processes", ()):
            identity = (process.get("pid"), process.get("start_ticks"))
            accumulator = self.leaks.get(identity)
            footprint = process.get("footprint_bytes")
            if (identity != sampler_identity and accumulator is not None
                    and stats.finite_number(footprint)):
                accumulator.add(timestamp, footprint)

        if _context_key(recorded.get("context", {})) != self.context:
            return
        self.matching_context_records += 1
        for key, value in _metric_map(recorded).items():
            reservoir = self.system.get(key)
            if reservoir is not None and stats.finite_number(value):
                reservoir.add(value)
        for process in recorded.get("processes", ()):
            identity = (process.get("pid"), process.get("start_ticks"))
            if identity in self.sampler_identities:
                continue
            normalized = process.get("normalized_name")
            reservoirs = self.process.get(normalized)
            if reservoirs is None:
                continue
            self.process_contributors[normalized].add(identity)
            for metric, reservoir in reservoirs.items():
                value = process.get(metric)
                if stats.finite_number(value):
                    reservoir.add(value)

    def add_current_leak_endpoint(self, sample):
        timestamp = sample.get("recorded_at")
        for process in sample.get("processes", ()):
            identity = (process.get("pid"), process.get("start_ticks"))
            accumulator = self.leaks.get(identity)
            footprint = process.get("footprint_bytes")
            if accumulator is not None and stats.finite_number(footprint):
                accumulator.add(timestamp, footprint)

    @property
    def trend_invalid_count(self):
        return sum(trend.invalid_count for trend in self.leaks.values())


def scan_history(path, since, sample):
    """Replay history once with :func:`baseline.scan`, never materializing it."""
    state = HistoryState(sample)
    replay = baseline.scan(path, since, state.add)
    state.add_current_leak_endpoint(sample)
    history = {
        "available": True,
        "error": None,
        "raw_schema": baseline.RAW_SCHEMA,
        "store": path,
        "since": since,
        "record_count": replay["record_count"],
        "matching_context_records": state.matching_context_records,
        "cold": replay["record_count"] == 0,
        "replay_errors": replay["errors"],
        "replay_error_count": replay["error_count"],
        "replay_errors_omitted": replay["errors_omitted"],
        "files": replay["files"],
        "source_partial_reasons": state.source_partial_reasons,
        "source_partial_reasons_omitted":
            state.source_partial_reasons_omitted,
        "trend_invalid_count": state.trend_invalid_count,
        "sampler_baseline_resets": state.sampler_baseline_resets,
    }
    return state, history


def _empty_history(path, since):
    return {
        "available": True,
        "error": None,
        "raw_schema": baseline.RAW_SCHEMA,
        "store": path,
        "since": since,
        "record_count": 0,
        "matching_context_records": 0,
        "cold": True,
        "replay_errors": [],
        "replay_error_count": 0,
        "replay_errors_omitted": 0,
        "files": [],
        "source_partial_reasons": [],
        "source_partial_reasons_omitted": 0,
        "trend_invalid_count": 0,
        "sampler_baseline_resets": 0,
    }


def _collect_points(observations=None):
    reasons = []
    failures = []
    observations = observations or {}
    mem = observations.get("memory")
    if mem is None:
        mem = memory.system_memory()
    if mem.get("errors"):
        reasons.append("memory_probe_incomplete")
        failures.extend("memory:%s" % error for error in mem["errors"])
    if mem.get("pressure") not in ("normal", "warn", "critical"):
        reasons.append("memory_pressure_unknown")

    batt = observations.get("battery")
    if batt is None:
        batt = battery.battery_health()
    if batt.get("probe_error"):
        reasons.append("battery_probe_incomplete")
        failures.append("battery:%s" % batt["probe_error"])
    if batt.get("pmset_error"):
        reasons.append("pmset_unavailable")

    smartctl_bin = smart.probe.find_smartctl()
    physical = smart.probe.list_physical_drives()
    drives = []
    diskutil_available = physical is not None
    if physical is None:
        reasons.append("diskutil_unavailable")
        failures.append("smart:diskutil_unavailable")
    else:
        if physical and smartctl_bin is None:
            reasons.append("smartctl_unavailable")
        for device, internal in physical:
            health = smart.drive_health(device, internal, smartctl_bin)
            drives.append(health)
            if health.get("diskutil_detail"):
                reasons.append("diskutil_probe_incomplete")
            if (smartctl_bin is not None
                    and (not health.get("smartctl_available")
                         or health.get("smartctl_detail"))):
                reasons.append("smartctl_probe_incomplete")
    return {
        "memory": mem,
        "battery": batt,
        "smart": {
            "available": diskutil_available,
            "diskutil_available": diskutil_available,
            "physical_drives_present": (
                None if physical is None else bool(physical)),
            "smartctl_available": smartctl_bin is not None,
            "drives": drives,
        },
    }, _unique(reasons), failures


def _unique(values):
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _current_document(sample, points=None):
    return {
        "recorded_at": sample.get("recorded_at"),
        "interval_s": sample.get("interval_s"),
        "partial": sample.get("partial", False),
        "partial_reasons": sample.get("partial_reasons", []),
        "context": sample.get("context"),
        "metrics": sample.get("metrics", []),
        "processes": sample.get("processes", []),
        "points": points,
    }


def _history_notes(mode, state, history):
    notes = []
    if not history["available"]:
        notes.append(
            "history is unavailable; only live and static evidence was used")
    elif history["cold"]:
        notes.append(
            "history is cold; run `stethoscope record` to build a baseline")
    elif state.matching_context_records == 0:
        notes.append("no history matches the current context")
    if mode in ("leaks", "triage"):
        mature = any(
            trend.count >= 5 and trend.span_seconds >= 30 * 60
            for trend in state.leaks.values())
        if not mature:
            notes.append(
                "leak history is immature; no short live fallback was run")
    if mode in ("runaway", "triage"):
        mature = any(
            reservoir.count >= 5
            for metrics in state.process.values()
            for reservoir in metrics.values())
        if not mature:
            notes.append(
                "process history is immature; static runaway thresholds were used")
    if history["source_partial_reasons"]:
        notes.append("recorded source visibility is partial")
    if history["source_partial_reasons_omitted"]:
        notes.append("additional source partial reasons were omitted")
    if history["trend_invalid_count"]:
        notes.append("out-of-order trend samples were ignored")
    return notes


def analyze(mode, sample, state, history, points=None, limit=DEFAULT_LIMIT):
    """Run pure diagnosis rules over already-gathered structures."""
    current_metrics = [
        item for item in sample.get("metrics", ())
        if (item.get("scope"), item.get("metric"))
        in rules.SYSTEM_METRIC_POLICIES
    ]
    processes = _diagnostic_processes(sample, state.sampler_identities)
    findings = []
    if mode in ("deviation", "triage"):
        findings.extend(rules.system_deviation_findings(
            current_metrics, state.system))
    if mode in ("leaks", "triage"):
        findings.extend(rules.leak_findings(
            processes, state.leaks, limit=limit))
    if mode in ("runaway", "triage"):
        findings.extend(rules.runaway_findings(
            processes, state.process, limit=limit))
    if mode == "triage":
        points = points or {}
        findings.extend(rules.point_findings(
            points.get("memory"), points.get("battery"),
            points.get("smart", {}).get("drives", [])))
    findings = taxonomy.sort_findings(findings)[:limit]
    return findings, _history_notes(mode, state, history)


def _json_safe(value):
    """Replace unexpected non-finite probe values before strict JSON encoding."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _document(scope, mode, partial_reasons, findings, notes, history,
              current, error):
    ordered = taxonomy.sort_findings(findings)
    return schema.document(
        scope, mode, partial=bool(partial_reasons),
        partial_reasons=_unique(partial_reasons),
        mode=mode,
        overall=taxonomy.overall(ordered),
        findings=ordered,
        notes=list(notes),
        history=history,
        current=current,
        error=error)


def _sample_failure_reason(reason):
    if reason.startswith("memory:"):
        return True
    if not reason.startswith("battery:"):
        return False
    detail = reason.split(":", 1)[1]
    return not (
        detail == "no_pmenergy_coefficients"
        or detail.startswith("pmset_"))


def run(mode, interval=DEFAULT_INTERVAL, limit=DEFAULT_LIMIT, since=None,
        store=None, scope="anomaly"):
    """Gather once, stream history, classify, and return ``(document, exit)``."""
    path = store
    parsed_since = since
    sample = None
    sample_valid = False
    history = _empty_history(path, parsed_since)
    partial_reasons = []
    failures = []
    history_access_error = None
    if parsed_since is None:
        parsed_since = baseline.parse_since(DEFAULT_SINCE)
        history["since"] = parsed_since
    elif isinstance(parsed_since, str):
        parsed_since = baseline.parse_since(parsed_since)
        history["since"] = parsed_since
    if path is None:
        try:
           path = baseline.default_store()
           history["store"] = path
        except baseline.StoreError as exc:
           history_access_error = exc
    try:
        if mode == "triage":
            sample, observations = record.collect_interval_observed(
                interval, limit)
        else:
            sample = record.collect_interval(interval, limit)
            observations = None
        problem = baseline.validate_record(sample)
        if problem is not None:
           raise record.CollectionError("invalid live sample: %s" % problem)
        sample_valid = True
    except (record.CollectionError, OSError) as exc:
        partial_reasons.append("runtime_failure")
        document = _document(
           scope, mode, partial_reasons, [], [], history,
           _current_document(sample) if sample_valid else None, str(exc))
        return _json_safe(document), cli.EXIT_ERROR

    sample_reasons = sample.get("partial_reasons", ())
    partial_reasons.extend(sample_reasons)
    failures.extend(
        reason for reason in sample_reasons if _sample_failure_reason(reason))

    if history_access_error is None:
        try:
           state, history = scan_history(path, parsed_since, sample)
        except (baseline.StoreError, OSError) as exc:
           history_access_error = exc
    if history_access_error is not None:
        state = HistoryState(sample)
        state.add_current_leak_endpoint(sample)
        history = _empty_history(path, parsed_since)
        history["available"] = False
        history["error"] = str(history_access_error)
        history["trend_invalid_count"] = state.trend_invalid_count
        partial_reasons.append("history_unavailable")
        failures.append("history:%s" % history_access_error)

    partial_reasons.extend(history["source_partial_reasons"])
    if history["source_partial_reasons_omitted"]:
        partial_reasons.append("source_partial_reasons_omitted")
    if history["trend_invalid_count"]:
        partial_reasons.append("history_ordering_gaps")
    if history["replay_error_count"]:
        partial_reasons.append("corrupt_store")
        failures.append(
           "history:%d replay error(s)" % history["replay_error_count"])

    points = None
    if mode == "triage":
        points, point_reasons, point_failures = _collect_points(observations)
        partial_reasons.extend(point_reasons)
        failures.extend(point_failures)
    findings, notes = analyze(
        mode, sample, state, history, points=points, limit=limit)
    failures = _unique(failures)
    error = "; ".join(str(item) for item in failures) or None
    if any(not str(item).startswith("history:") for item in failures):
        partial_reasons.append("probe_failure")
    document = _document(
        scope, mode, partial_reasons, findings, notes, history,
        _current_document(sample, points), error)
    if failures:
        return _json_safe(document), cli.EXIT_ERROR
    if document["overall"] == "critical":
        return _json_safe(document), cli.EXIT_FINDINGS
    return _json_safe(document), cli.EXIT_OK


def _render(document):
    title = ("stethoscope triage · anomaly diagnosis"
             if document["scope"] == "triage"
             else "stethoscope anomaly · %s" % document["mode"])
    lines = [BOLD + title + RESET, "overall: %s" % document["overall"]]
    for note in document["notes"]:
        lines.append(DIM + "note: %s" % cli.safe_text(note) + RESET)
    if document["partial_reasons"]:
        lines.append(DIM + "partial: %s" % ", ".join(
            cli.safe_text(reason)
            for reason in document["partial_reasons"]) + RESET)
    if document["error"]:
        lines.append("error: %s" % cli.safe_text(document["error"]))
    history = document.get("history") or {}
    replay_errors = history.get("replay_errors") or []
    for replay_error in replay_errors[:20]:
        lines.append(DIM + "replay: %s:%s: %s" % (
            cli.safe_text(replay_error.get("file", "?")),
            replay_error.get("line", "?"),
            cli.safe_text(replay_error.get("reason", "unknown"))) + RESET)
    replay_error_count = history.get("replay_error_count") or 0
    if replay_error_count > min(20, len(replay_errors)):
        lines.append(DIM + "replay: %d additional error(s) omitted" % (
            replay_error_count - min(20, len(replay_errors))) + RESET)
    if not document["findings"]:
        lines.append(DIM + "no findings." + RESET)
    for item in document["findings"]:
        mark = {"critical": "!!", "warn": "!", "info": "."}.get(
            item["severity"], ".")
        lines.append("  %s [%s/%s] %s" % (
            mark, cli.safe_text(item["area"]),
            cli.safe_text(item["detector"]),
            cli.safe_text(item["message"])))
        for command in item["drill_down"]:
            lines.append(DIM + "      -> %s" %
                         cli.safe_text(command) + RESET)
    return "\n".join(lines) + "\n"


def _usage_document(scope, mode, store, error):
    return _document(
        scope, mode, ["usage_error"], [], [],
        _empty_history(store, None), None, str(error))


TRIAGE_USAGE = """stethoscope triage — ranked one-shot anomaly diagnosis

  triage [--json] [--interval N] [--limit N] [--since WHEN] [--store DIR]

WHEN is a relative duration (3h), ISO timestamp, or local clock (3am).
Defaults: interval 1s, limit 20 (maximum 256), since 24h, canonical JSONL store.
Exit codes: 0 clean/warn · 1 critical finding · 2 usage · 4 probe/replay/runtime
"""

USAGE = """stethoscope anomaly — one-shot anomaly detectors

  anomaly deviation [--json] [--interval N] [--limit N] [--since WHEN] [--store DIR]
  anomaly leaks     [--json] [--interval N] [--limit N] [--since WHEN] [--store DIR]
  anomaly runaway   [--json] [--interval N] [--limit N] [--since WHEN] [--store DIR]
  anomaly triage    [--json] [--interval N] [--limit N] [--since WHEN] [--store DIR]

Modes are exact; aliases are not accepted. Triage/anomaly are one-shot and
reject --once and --duration. Maximum interval is 60s; maximum limit is 256.
Exit codes: 0 clean/warn · 1 critical finding · 2 usage · 4 probe/replay/runtime
"""


def main(argv):
    signal.signal(signal.SIGINT, lambda *args: sys.exit(0))
    invoked = argv[0].split()[-1] if argv else "anomaly"
    args = list(argv[1:])
    direct_triage = invoked == "triage"
    usage = TRIAGE_USAGE if direct_triage else USAGE
    if args and args[0] in ("-h", "--help"):
        print(usage)
        return cli.EXIT_OK

    json_requested = "--json" in args
    scope = "triage" if direct_triage else "anomaly"
    mode = "triage" if direct_triage else "invalid"
    if not direct_triage and args and not args[0].startswith("-"):
        mode = args.pop(0)
    options = None
    try:
        options = cli.parse_options(
            args, interval=DEFAULT_INTERVAL, limit=DEFAULT_LIMIT,
            extras={"store", "since"})
        cli.require_options(
            options, invoked,
            {"json", "interval", "limit", "store", "since"})
        if direct_triage:
            cli.require_positionals(options, "triage", 0)
        else:
            cli.require_positionals(options, "anomaly", 0)
            if mode not in MODES:
                raise cli.OptionsError("unknown anomaly mode: %s" % mode)
        if options.interval > MAX_INTERVAL:
            raise cli.OptionsError("--interval must be <= %.0f" % MAX_INTERVAL)
        if options.limit > MAX_LIMIT:
            raise cli.OptionsError("--limit must be <= %d" % MAX_LIMIT)
        try:
            since = baseline.parse_since(options.since or DEFAULT_SINCE)
        except ValueError as exc:
            raise cli.OptionsError(str(exc)) from exc
    except cli.OptionsError as exc:
        if json_requested:
            cli.emit_json(_json_safe(_usage_document(
                scope, mode, options.store if options else None, exc)))
        else:
            sys.stderr.write("%s\n\n%s" % (cli.safe_text(exc), usage))
        return cli.EXIT_USAGE

    document, exit_code = run(
        mode, interval=options.interval, limit=options.limit,
        since=since, store=options.store, scope=scope)
    if options.json:
        cli.emit_json(document)
    else:
        stream = sys.stderr if exit_code == cli.EXIT_ERROR else sys.stdout
        stream.write(_render(document))
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
