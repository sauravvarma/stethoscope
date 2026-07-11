#!/usr/bin/env python3
"""Foreground recording, retrospective history, and hourly baselines."""

import datetime
import math
import os
import re
import signal
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import baseline, cli, power, rusage, schema, vmstat
from scopes import battery

DEFAULT_INTERVAL = 60.0
DEFAULT_LIMIT = 20
MAX_LIMIT = 256
MAX_INTERVAL = 24 * 60 * 60.0
MAX_DURATION = 365 * 24 * 60 * 60.0
MAX_HISTORY_SYSTEM_BUCKETS = 256
MAX_HISTORY_PARTIAL_REASONS = 128
ACTIVE_CPU_FLOOR = 0.1
ACTIVE_WAKEUP_FLOOR = 1.0
ACTIVE_DISK_FLOOR = 1024.0
ACTIVE_ENERGY_FLOOR = 0.01
RECORDED_SCOPES = {"cpu", "disk", "memory", "battery", "sampler"}
_PROCESS_SCOPES = {
    "cpu_pct": "cpu", "user_pct": "cpu", "system_pct": "cpu",
    "pkg_idle_wakeups_per_s": "cpu",
    "interrupt_wakeups_per_s": "cpu",
    "diskio_bytes_read_per_s": "disk",
    "diskio_bytes_written_per_s": "disk",
    "energy_rate_watts": "battery",
    "energy_score_per_s": "battery",
    "footprint_bytes": "memory",
    "resident_size_bytes": "memory",
}

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


class CollectionError(RuntimeError):
    """A completed recording interval could not be collected."""


def normalize_process_name(value):
    """Normalize an executable label for cross-PID/process baseline buckets."""
    name = os.path.basename(str(value or "?")).strip().lower()
    name = "".join(char if char.isprintable() else "?" for char in name)
    name = re.sub(r"\s+", " ", name)
    return name or "?"


def _delta(current, previous):
    return max(0, current - previous)


def _zero_counters(sample):
    return {
        "cpu_user_ns": 0,
        "cpu_system_ns": 0,
        "qos_cpu_ns": {
            name: 0 for name in battery._QOS_CLASSES
        },
        "energy_nj": 0 if sample["energy_nj"] is not None else None,
        "pkg_idle_wakeups": 0,
        "interrupt_wakeups": 0,
        "diskio_bytes_read": 0,
        "diskio_bytes_written": 0,
    }


def _energy_score_available(coeffs):
    return battery._energy_score(
        coeffs, 0.0, 0.0, 0.0, 0.0,
        qos_cpu_seconds={
            name: 0.0 for name in battery._QOS_CLASSES
        }) is not None


def _process_rows(previous, current, elapsed, coeffs, limit,
                  interval_start_ticks=None):
    candidates = []
    score_available = _energy_score_available(coeffs)
    coverage = {
        "new_processes_zero_based": 0,
        "unmatched_current_processes": 0,
        "missing_endpoint_processes": len(set(previous).difference(current)),
    }
    system = {
        "cpu_pct": 0.0,
        "pkg_idle_wakeups_per_s": 0.0,
        "interrupt_wakeups_per_s": 0.0,
        "diskio_bytes_read_per_s": 0.0,
        "diskio_bytes_written_per_s": 0.0,
        "energy_rate_watts": None,
        "energy_score_per_s": (0.0 if score_available else None),
    }
    for identity, sample in current.items():
        prior = previous.get(identity)
        if prior is None:
            if (interval_start_ticks is not None
                    and identity[1] >= interval_start_ticks):
                prior = _zero_counters(sample)
                coverage["new_processes_zero_based"] += 1
            else:
                prior = sample
                coverage["unmatched_current_processes"] += 1
        user_ns = _delta(sample["cpu_user_ns"], prior["cpu_user_ns"])
        system_ns = _delta(sample["cpu_system_ns"], prior["cpu_system_ns"])
        user_pct = user_ns / elapsed / 1e9 * 100.0
        system_pct = system_ns / elapsed / 1e9 * 100.0
        cpu_pct = user_pct + system_pct
        pkg = _delta(
            sample["pkg_idle_wakeups"], prior["pkg_idle_wakeups"]) / elapsed
        intr = _delta(
            sample["interrupt_wakeups"], prior["interrupt_wakeups"]) / elapsed
        read = _delta(
            sample["diskio_bytes_read"], prior["diskio_bytes_read"]) / elapsed
        write = _delta(
            sample["diskio_bytes_written"],
            prior["diskio_bytes_written"]) / elapsed
        watts = None
        if (sample["energy_nj"] is not None
                and prior["energy_nj"] is not None):
            watts = _delta(
                sample["energy_nj"], prior["energy_nj"]) / elapsed / 1e9
        qos = {
            name: _delta(
                sample.get("qos_cpu_ns", {}).get(name, 0),
                prior.get("qos_cpu_ns", {}).get(name, 0)) / elapsed / 1e9
            for name in battery._QOS_CLASSES
        }
        score = battery._energy_score(
            coeffs, (user_ns + system_ns) / elapsed / 1e9, pkg, read, write,
            qos_cpu_seconds=qos)
        pid = identity[0]
        name = rusage.proc_name(pid, identity)
        row = {
            "pid": pid,
            "start_ticks": identity[1],
            "name": name,
            "normalized_name": normalize_process_name(name),
            "cpu_pct": cpu_pct,
            "user_pct": user_pct,
            "system_pct": system_pct,
            "pkg_idle_wakeups_per_s": pkg,
            "interrupt_wakeups_per_s": intr,
            "diskio_bytes_read_per_s": read,
            "diskio_bytes_written_per_s": write,
            "energy_rate_watts": watts,
            "energy_score_per_s": score,
            "footprint_bytes": sample.get("phys_footprint_bytes"),
            "resident_size_bytes": sample.get("resident_size_bytes"),
        }
        activity = (
            cpu_pct >= ACTIVE_CPU_FLOOR
            or pkg + intr >= ACTIVE_WAKEUP_FLOOR
            or read + write >= ACTIVE_DISK_FLOOR
            or (watts is not None and watts >= ACTIVE_ENERGY_FLOOR)
            or (score is not None and score > 0))
        rank = (
            score or 0.0, watts or 0.0, cpu_pct, pkg + intr, read + write)
        candidates.append((row, activity, rank))
        system["cpu_pct"] += cpu_pct
        system["pkg_idle_wakeups_per_s"] += pkg
        system["interrupt_wakeups_per_s"] += intr
        system["diskio_bytes_read_per_s"] += read
        system["diskio_bytes_written_per_s"] += write
        if watts is not None:
            system["energy_rate_watts"] = (
                (system["energy_rate_watts"] or 0.0) + watts)
        if score is not None and system["energy_score_per_s"] is not None:
            system["energy_score_per_s"] += score

    active = sorted(
        (item for item in candidates if item[1]),
        key=lambda item: tuple(-number for number in item[2]))[:limit]
    footprint = sorted(
        candidates,
        key=lambda item: -(item[0]["footprint_bytes"] or 0))[:limit]
    selected = {}
    for row, _, _ in active + footprint:
        selected[(row["pid"], row["start_ticks"])] = row
    own_pid = os.getpid()
    for row, _, _ in candidates:
        if row["pid"] == own_pid:
            selected[(row["pid"], row["start_ticks"])] = row
            break
    rows = sorted(
        selected.values(),
        key=lambda row: (-(row["energy_score_per_s"] or 0),
                         -row["cpu_pct"],
                         -(row["footprint_bytes"] or 0), row["pid"]))
    return rows, system, coverage


def _metric(scope, name, value, unit):
    return {"scope": scope, "metric": name, "value": value, "unit": unit}


def collect_interval(interval, limit=DEFAULT_LIMIT, sleeper=time.sleep,
                     wall_time=time.time, monotonic=time.monotonic):
    """Collect one completed interval using one libproc struct read per PID/poll."""
    interval_start_ticks = rusage.mach_absolute_time()
    first_started = monotonic()
    previous = battery.snapshot_power()
    first_ended = monotonic()
    try:
        sleeper(interval)
    except OverflowError as exc:
        raise CollectionError("sampling interval is outside the clock range") from exc
    second_started = monotonic()
    current = battery.snapshot_power()
    second_ended = monotonic()
    elapsed = (
        (second_started + second_ended) / 2.0
        - (first_started + first_ended) / 2.0)
    if not math.isfinite(elapsed) or elapsed <= 0:
        raise CollectionError("sampling clock did not advance")

    coeffs, _, coefficient_error = power.pmenergy_coefficients()
    score_available = _energy_score_available(coeffs)
    processes, totals, coverage = _process_rows(
        previous, current, elapsed, coeffs, limit, interval_start_ticks)
    memory = vmstat.system_memory()
    health = battery.battery_health()
    recorded_at = wall_time()
    local = datetime.datetime.fromtimestamp(recorded_at).astimezone()
    offset = local.utcoffset()
    offset_seconds = int(offset.total_seconds()) if offset else 0
    timezone = "%s%s%02d:%02d" % (
        local.tzname() or "local", "+" if offset_seconds >= 0 else "-",
        abs(offset_seconds) // 3600, abs(offset_seconds) // 60 % 60)
    external_connected = health.get("external_connected")
    if isinstance(external_connected, bool):
        power_state = "ac" if external_connected else "battery"
    else:
        state = str(health.get("state") or "").lower()
        if "discharg" in state:
            power_state = "battery"
        elif "charg" in state or state == "ac":
            power_state = "ac"
        else:
            power_state = "unknown"

    own_pid = os.getpid()
    own = next((row for row in processes if row["pid"] == own_pid), None)
    if own is None:
        own_sample = next(
            (sample for identity, sample in current.items()
             if identity[0] == own_pid), None)
        own_identity = (own_sample or {}).get("identity")
        own_name = rusage.proc_name(own_pid, own_identity)
        own = {
            "pid": own_pid,
            "start_ticks": own_identity[1] if own_identity else None,
            "name": own_name,
            "normalized_name": normalize_process_name(own_name),
            "cpu_pct": None,
            "footprint_bytes": (
                own_sample.get("phys_footprint_bytes")
                if own_sample is not None else None),
            "resident_size_bytes": (
                own_sample.get("resident_size_bytes")
                if own_sample is not None else None),
        }

    metrics = [
        _metric("cpu", "cpu_pct", totals["cpu_pct"], "percent_of_one_core"),
        _metric("cpu", "pkg_idle_wakeups_per_s",
                totals["pkg_idle_wakeups_per_s"], "per_second"),
        _metric("cpu", "interrupt_wakeups_per_s",
                totals["interrupt_wakeups_per_s"], "per_second"),
        _metric("disk", "read_bytes_per_s",
                totals["diskio_bytes_read_per_s"], "bytes_per_second"),
        _metric("disk", "write_bytes_per_s",
                totals["diskio_bytes_written_per_s"], "bytes_per_second"),
        _metric("battery", "energy_rate_watts",
                totals["energy_rate_watts"], "watts"),
        _metric("battery", "energy_score_per_s",
                totals["energy_score_per_s"], "unitless_per_second"),
        _metric("memory", "used_bytes", memory.get("used"), "bytes"),
        _metric("memory", "free_bytes", memory.get("free"), "bytes"),
        _metric("memory", "wired_bytes", memory.get("wired"), "bytes"),
        _metric("memory", "compressed_bytes", memory.get("compressed"), "bytes"),
        _metric("battery", "charge_pct", health.get("charge_pct"), "percent"),
        _metric("battery", "health_pct", health.get("health_pct"), "percent"),
        _metric("battery", "flow_watts",
                health.get("battery_flow_watts"), "watts"),
        _metric("sampler", "cpu_pct", own.get("cpu_pct"),
                "percent_of_one_core"),
        _metric("sampler", "footprint_bytes",
                own.get("footprint_bytes"), "bytes"),
        _metric("sampler", "resident_size_bytes",
                own.get("resident_size_bytes"), "bytes"),
    ]
    reasons = []
    if not cli.is_root():
        reasons.append("not_root")
    if memory.get("errors"):
        reasons.extend("memory:%s" % error for error in memory["errors"])
    if health.get("probe_error"):
        reasons.append("battery:%s" % health["probe_error"])
    if health.get("pmset_error"):
        reasons.append("battery:%s" % health["pmset_error"])
    if coefficient_error is not None or not score_available:
        reasons.append("battery:no_pmenergy_coefficients")
    if (coverage["unmatched_current_processes"]
            or coverage["missing_endpoint_processes"]):
        reasons.append("process_endpoint_gaps")
    context = {
        "root": cli.is_root(),
        "privilege": "root" if cli.is_root() else "user",
        "power_state": power_state,
        "local_hour": local.hour,
        "timezone": timezone,
        "sampler": {
            "pid": own["pid"],
            "start_ticks": own["start_ticks"],
            "name": own["name"],
            "normalized_name": own["normalized_name"],
        },
        "coverage": coverage,
    }
    return {
        "schema": baseline.RAW_SCHEMA,
        "recorded_at": recorded_at,
        "interval_s": elapsed,
        "requested_interval_s": interval,
        "context": context,
        "metrics": metrics,
        "processes": processes,
        "partial": bool(reasons),
        "partial_reasons": reasons,
    }


def _record_document(sample, store, stored=True, error=None,
                     requested_interval_s=None):
    if (error is not None and sample is not None
            and baseline.validate_record(sample) is not None):
        sample = None
    partial_reasons = list(sample["partial_reasons"]) if sample else []
    if error is not None and "record_runtime_error" not in partial_reasons:
        partial_reasons.append("record_runtime_error")
    return schema.document(
        "record", "sample",
        partial=(sample["partial"] if sample else False) or error is not None,
        partial_reasons=partial_reasons,
        raw_schema=baseline.RAW_SCHEMA,
        store=store,
        stored=stored,
        recorded_at=sample["recorded_at"] if sample else None,
        interval_s=sample["interval_s"] if sample else None,
        requested_interval_s=(
            sample["requested_interval_s"] if sample
            else requested_interval_s),
        context=sample["context"] if sample else None,
        metrics=sample["metrics"] if sample else [],
        processes=sample["processes"] if sample else [],
        error=error)


def _emit_record_human(sample, store):
    print(BOLD + "stethoscope record" + RESET)
    print("stored %s interval at %s" % (
        "%.2fs" % sample["interval_s"],
        time.strftime("%Y-%m-%d %H:%M:%S",
                      time.localtime(sample["recorded_at"]))))
    print(DIM + "%d processes · sampler %.1f MiB · %s" % (
        len(sample["processes"]),
        next((metric["value"] or 0 for metric in sample["metrics"]
              if metric["scope"] == "sampler"
              and metric["metric"] == "footprint_bytes"), 0)
        / 1024 / 1024,
        cli.safe_text(store)) + RESET)
    if sample["partial_reasons"]:
        print(DIM + "partial: %s" % ", ".join(
            cli.safe_text(reason)
            for reason in sample["partial_reasons"]) + RESET)


def cmd_record(options):
    path = options.store
    sample = None
    stored = False
    try:
        if path is None:
            path = baseline.default_store()
        with baseline.Corpus(path, options.retention_days) as corpus:
            started = time.monotonic()
            while True:
                sample = None
                stored = False
                sample = collect_interval(options.interval, options.limit)
                corpus.append(sample)
                stored = True
                corpus.retain()
                if options.json:
                    cli.emit_json(_record_document(sample, path))
                else:
                    _emit_record_human(sample, path)
                if options.once:
                    return cli.EXIT_OK
                if (options.duration is not None
                        and time.monotonic() - started >= options.duration):
                    return cli.EXIT_OK
    except (baseline.StoreError, CollectionError, OSError) as exc:
        if options.json:
            cli.emit_json(_record_document(
                sample, path, stored=stored, error=str(exc),
                requested_interval_s=options.interval))
        else:
            sys.stderr.write("record: %s\n" % cli.safe_text(exc))
        return cli.EXIT_ERROR


def _add_history_record(state, recorded, scope, candidate_capacity):
    for metric_scope, metric, process_name, value in baseline._metric_values(
            recorded):
        if scope is not None and metric_scope != scope:
            continue
        key = (metric_scope, metric, process_name)
        if process_name is None:
            if key not in state["system"]:
                if len(state["system"]) >= MAX_HISTORY_SYSTEM_BUCKETS:
                    state["dropped_values"] += 1
                    continue
                state["system"][key] = baseline.Reservoir(
                    baseline.DEFAULT_RESERVOIR_SIZE, seed=0)
            state["system"][key].add(value)
            continue
        group_key = (metric_scope, metric)
        candidates = state["process"].get(group_key)
        if candidates is None:
            candidates = baseline.CandidateReservoirs(candidate_capacity)
            state["process"][group_key] = candidates
        candidates.add(process_name, value)


def _history_rows(state, limit):
    summaries = []
    for (metric_scope, metric, _), reservoir in state["system"].items():
        row = {
            "scope": metric_scope, "metric": metric,
            **reservoir.summary(),
        }
        summaries.append(row)
    consumers = []
    for (metric_scope, metric), candidates in state["process"].items():
        state["dropped_values"] += candidates.dropped_values
        candidates.dropped_values = 0
        rows = []
        for process_name, reservoir in candidates.entries.items():
            row = {
                "scope": metric_scope, "metric": metric,
                **reservoir.summary(),
            }
            row["normalized_process_name"] = process_name
            rows.append(row)
        rows.sort(key=lambda row: (
            -(row["p90"] or 0), row["normalized_process_name"]))
        consumers.extend(rows[:limit])
    summaries.sort(key=lambda row: (row["scope"], row["metric"]))
    consumers.sort(key=lambda row: (
        row["scope"], row["metric"], -(row["p90"] or 0),
        row["normalized_process_name"]))
    return summaries, consumers


def _limit_baseline_rows(rows, limit):
    system_rows = []
    process_groups = {}
    for row in rows:
        if row["normalized_process_name"] is None:
            system_rows.append(row)
            continue
        key = (
            row["local_hour"], row["timezone"], row["privilege"],
            row["power_state"], row["scope"], row["metric"])
        process_groups.setdefault(key, []).append(row)
    selected = list(system_rows)
    for key in sorted(process_groups, key=lambda item: tuple(map(str, item))):
        group = process_groups[key]
        group.sort(key=lambda row: (
            -(row["p90"] or 0), row["normalized_process_name"]))
        selected.extend(group[:limit])
    selected.sort(key=lambda row: (
        row["local_hour"], row["timezone"], row["privilege"],
        row["power_state"], row["scope"], row["metric"],
        row["normalized_process_name"] or ""))
    return selected


def history_result(path, since, scope=None, baseline_mode=False, limit=DEFAULT_LIMIT):
    state = {
        "system": {},
        "process": {},
        "source_partial_reasons": [],
        "dropped_values": 0,
    }
    candidate_capacity = max(64, min(MAX_LIMIT * 2, limit * 2))
    baseline_accumulator = (
        baseline.BaselineAccumulator(
            scope=scope, process_candidates=candidate_capacity)
        if baseline_mode else None)

    def consume(recorded):
        for reason in recorded["partial_reasons"]:
            if reason not in state["source_partial_reasons"]:
                if (len(state["source_partial_reasons"])
                        < MAX_HISTORY_PARTIAL_REASONS):
                    state["source_partial_reasons"].append(reason)
                else:
                    state["dropped_values"] += 1
        if baseline_accumulator is not None:
            baseline_accumulator.add(recorded)
        else:
            _add_history_record(state, recorded, scope, candidate_capacity)

    replay = baseline.scan(path, since, consume)
    record_count = replay["record_count"]
    errors = replay["errors"]
    replay_error_count = replay["error_count"]
    partial_reasons = list(state["source_partial_reasons"])
    if replay_error_count and "corrupt_store" not in partial_reasons:
        partial_reasons.append("corrupt_store")
    if baseline_mode:
        buckets = _limit_baseline_rows(baseline_accumulator.rows(), limit)
        dropped_values = (
            baseline_accumulator.dropped_values + state["dropped_values"])
        if dropped_values and "history_bucket_limit" not in partial_reasons:
            partial_reasons.append("history_bucket_limit")
        return schema.document(
            "history", "baseline",
            partial=bool(partial_reasons), partial_reasons=partial_reasons,
            raw_schema=baseline.RAW_SCHEMA, store=path, since=since,
            requested_scope=scope, record_count=record_count,
            cold=not buckets, buckets=buckets, replay_errors=errors,
            replay_error_count=replay_error_count,
            replay_errors_omitted=replay["errors_omitted"],
            dropped_values=dropped_values, error=None)
    summaries, consumers = _history_rows(state, limit)
    if (state["dropped_values"]
            and "history_bucket_limit" not in partial_reasons):
        partial_reasons.append("history_bucket_limit")
    return schema.document(
        "history", "summary",
        partial=bool(partial_reasons), partial_reasons=partial_reasons,
        raw_schema=baseline.RAW_SCHEMA, store=path, since=since,
        requested_scope=scope, record_count=record_count,
        cold=record_count == 0, summaries=summaries,
        top_consumers=consumers, replay_errors=errors,
        replay_error_count=replay_error_count,
        replay_errors_omitted=replay["errors_omitted"],
        dropped_values=state["dropped_values"], error=None)


def _emit_history_human(result):
    title = "stethoscope history"
    if result["command"] == "baseline":
        title += " baseline"
    print(BOLD + title + RESET)
    print("%d records since %s" % (
        result["record_count"],
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(result["since"]))))
    if result["cold"]:
        print(DIM + "(no samples; baseline is cold)" + RESET)
    if result["partial_reasons"]:
        print(DIM + "partial: %s" % ", ".join(
            cli.safe_text(reason)
            for reason in result["partial_reasons"]) + RESET)
    for row in result.get("summaries", []):
        print("%-8s %-32s p50 %-10s p90 %-10s p99 %-10s n=%d" % (
            cli.safe_text(row["scope"]), cli.safe_text(row["metric"]),
            _format_number(row["p50"]),
            _format_number(row["p90"]), _format_number(row["p99"]),
            row["count"]))
    for row in result.get("top_consumers", []):
        print("%-8s %-24s %-28s p90 %s" % (
            cli.safe_text(row["scope"]),
            cli.safe_text(row["normalized_process_name"])[:24],
            cli.safe_text(row["metric"]), _format_number(row["p90"])))
    if result.get("buckets"):
        for row in result["buckets"]:
            process = cli.safe_text(
                row["normalized_process_name"] or "system")
            print("%02s %-8s %-20s %-24s p90 %s n=%d" % (
                row["local_hour"], cli.safe_text(row["scope"]), process,
                cli.safe_text(row["metric"]),
                _format_number(row["p90"]), row["count"]))
    if result["replay_errors"]:
        for error in result["replay_errors"]:
            sys.stderr.write("%s:%s: %s\n" % (
                cli.safe_text(error["file"]), error["line"],
                cli.safe_text(error["reason"])))
    if result["replay_errors_omitted"]:
        sys.stderr.write("%d additional replay errors omitted\n"
                         % result["replay_errors_omitted"])


def _format_number(value):
    return "-" if value is None else "%.3g" % value


def _history_error_document(path, since, scope, baseline_mode, error):
    fields = {
        "raw_schema": baseline.RAW_SCHEMA,
        "store": path,
        "since": since,
        "requested_scope": scope,
        "record_count": 0,
        "cold": True,
        "replay_errors": [],
        "replay_error_count": 0,
        "replay_errors_omitted": 0,
        "dropped_values": 0,
        "error": str(error),
    }
    if baseline_mode:
        fields["buckets"] = []
    else:
        fields["summaries"] = []
        fields["top_consumers"] = []
    return schema.document(
        "history", "baseline" if baseline_mode else "summary",
        partial=True, partial_reasons=["history_runtime_error"], **fields)


def cmd_history(options, scope=None, baseline_mode=False, since=None):
    path = options.store
    try:
        if path is None:
            path = baseline.default_store()
        if since is None:
            since = baseline.parse_since(options.since)
        result = history_result(
            path, since, scope=scope, baseline_mode=baseline_mode,
            limit=options.limit)
    except (baseline.StoreError, OSError) as exc:
        result = _history_error_document(
            path, since, scope, baseline_mode, exc)
        if options.json:
            cli.emit_json(result)
        else:
            sys.stderr.write("history: %s\n" % cli.safe_text(exc))
        return cli.EXIT_ERROR
    if options.json:
        cli.emit_json(result)
    else:
        _emit_history_human(result)
    return cli.EXIT_ERROR if result["replay_error_count"] else cli.EXIT_OK


USAGE = """stethoscope recording and history

  record [--once | --duration N] [--interval N] [--limit N] [--json]
         [--store DIR] [--retention-days N]
      Append completed live intervals to daily baseline-raw/1 JSONL.

  history [scope] [--since WHEN] [--limit N] [--store DIR] [--json]
      Summarize history and top process consumers. Scope is optional.

  history baseline [scope] [--since WHEN] [--limit N] [--store DIR] [--json]
      Per-local-hour/context p50/p90/p99 baseline buckets.

WHEN is a relative duration (3h), ISO timestamp, or local clock (3am).
Defaults: interval 60s, limit 20 (maximum 256), retention 30 days,
history since 24h. Maximum interval is 1 day and explicit duration is 365 days.
Exit codes: 0 clean/empty · 2 usage · 4 store/replay/corruption/runtime
"""


def main(argv):
    signal.signal(signal.SIGINT, lambda *args: sys.exit(0))
    invoked = argv[0].split()[-1]
    args = list(argv[1:])
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return cli.EXIT_OK
    try:
        options = cli.parse_options(
            args, interval=DEFAULT_INTERVAL, limit=DEFAULT_LIMIT,
            extras={"store", "since", "retention_days"})
        if options.limit > MAX_LIMIT:
            raise cli.OptionsError("--limit must be <= %d" % MAX_LIMIT)
        if invoked == "record":
            cli.require_options(
                options, "record",
                {"json", "once", "duration", "interval", "limit", "store",
                 "retention_days"})
            cli.require_positionals(options, "record", 0)
            if options.interval > MAX_INTERVAL:
                raise cli.OptionsError(
                    "--interval must be <= %.0f" % MAX_INTERVAL)
            if (options.duration is not None
                    and options.duration > MAX_DURATION):
                raise cli.OptionsError(
                    "--duration must be <= %.0f" % MAX_DURATION)
            if options.retention_days > baseline.MAX_RETENTION_DAYS:
                raise cli.OptionsError(
                    "--retention-days must be <= %d"
                    % baseline.MAX_RETENTION_DAYS)
            return cmd_record(options)
        if invoked == "history":
            cli.require_options(
                options, "history", {"json", "since", "limit", "store"})
            rest = options.rest
            baseline_mode = bool(rest and rest[0] == "baseline")
            if baseline_mode:
                rest = rest[1:]
            if len(rest) > 1:
                raise cli.OptionsError(
                    "history%s accepts at most one scope"
                    % (" baseline" if baseline_mode else ""))
            scope = rest[0] if rest else None
            if scope is not None and scope not in RECORDED_SCOPES:
                raise cli.OptionsError("unknown history scope: %s" % scope)
            try:
                since = baseline.parse_since(options.since)
            except ValueError as exc:
                raise cli.OptionsError(str(exc)) from exc
            return cmd_history(options, scope, baseline_mode, since=since)
    except cli.OptionsError as exc:
        sys.stderr.write("%s\n\n%s" % (cli.safe_text(exc), USAGE))
        return cli.EXIT_USAGE
    sys.stderr.write("record/history dispatcher error\n")
    return cli.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main(sys.argv))
