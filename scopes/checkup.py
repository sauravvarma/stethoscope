#!/usr/bin/env python3
"""One-shot full-body examination composed from canonical triage data."""

import math
import os
import signal
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import baseline, cli, schema
from scopes import anomaly

DEFAULT_INTERVAL = anomaly.DEFAULT_INTERVAL
DEFAULT_LIMIT = anomaly.DEFAULT_LIMIT
DEFAULT_SINCE = anomaly.DEFAULT_SINCE
MAX_INTERVAL = anomaly.MAX_INTERVAL
MAX_LIMIT = anomaly.MAX_LIMIT

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _metric_map(current):
    return {
        (item.get("scope"), item.get("metric")): _number(item.get("value"))
        for item in (current or {}).get("metrics", ())
        if isinstance(item, dict)
    }


def _process_rows(current, fields, ranking, limit):
    rows = []
    for process in anomaly._diagnostic_processes(current or {}):
        if not isinstance(process, dict):
            continue
        row = {
            "pid": process.get("pid"),
            "start_ticks": process.get("start_ticks"),
            "name": process.get("name"),
        }
        for field in fields:
            row[field] = _number(process.get(field))
        rows.append(row)

    def key(row):
        if ranking == "cpu":
            rank = (-(row.get("cpu_pct") or 0.0),)
        elif ranking == "disk":
            rank = (-(
                (row.get("diskio_bytes_read_per_s") or 0.0)
                + (row.get("diskio_bytes_written_per_s") or 0.0)),)
        elif ranking == "memory":
            rank = (-(row.get("footprint_bytes") or 0.0),)
        elif ranking == "battery":
            rank = (
                -(row.get("energy_score_per_s") or 0.0),
                -(row.get("energy_rate_watts") or 0.0),
                -(row.get("cpu_pct") or 0.0),
            )
        else:
            raise ValueError("unknown process ranking: %s" % ranking)
        pid = row.get("pid") if isinstance(row.get("pid"), int) else -1
        return rank + (pid, str(row.get("name") or ""))

    rows.sort(key=key)
    return rows[:limit]


def _process_visibility_partial(current):
    if current is None:
        return False
    context = current.get("context") or {}
    coverage = context.get("coverage") or {}
    reasons = set(current.get("partial_reasons") or ())
    return (
        context.get("root") is not True
        or "not_root" in reasons
        or "process_endpoint_gaps" in reasons
        or bool(coverage.get("unmatched_current_processes"))
        or bool(coverage.get("missing_endpoint_processes"))
    )


def _scope_probe_partial(current, prefix):
    return any(
        str(reason).startswith(prefix)
        for reason in (current or {}).get("partial_reasons") or ())


def _system_vital(current, scope, names, units,
                  process_fields, ranking, limit):
    metrics = _metric_map(current)
    values = {
        name: metrics.get((scope, name))
        for name in names
    }
    if current is None:
        state = "unavailable"
    elif any(value is None for value in values.values()):
        state = "partial"
    elif _process_visibility_partial(current):
        state = "partial"
    else:
        state = "available"
    return {
        "state": state,
        "available": current is not None,
        "partial": state == "partial",
        "rates": {
            name: {"value": values[name], "unit": units[name]}
            for name in names
        },
        "top_consumers": _process_rows(
            current, process_fields, ranking, limit),
    }


def _memory_vital(points, current, limit):
    memory = (points or {}).get("memory")
    if not isinstance(memory, dict):
        return {
            "state": "unavailable", "available": False, "partial": False,
            "pressure": "unknown", "total_bytes": None, "used_bytes": None,
            "free_bytes": None, "wired_bytes": None,
            "compressed_bytes": None, "errors": [], "top_consumers": [],
        }
    values = [
        memory.get("total"), memory.get("used"), memory.get("free"),
        memory.get("wired"), memory.get("compressed"),
    ]
    errors = list(memory.get("errors") or ())
    unavailable = (
        not any(_number(value) is not None for value in values)
        and memory.get("pressure") not in ("normal", "warn", "critical"))
    partial = (
        bool(errors)
        or memory.get("pressure") not in ("normal", "warn", "critical")
        or not memory.get("available", not unavailable))
    if (not unavailable
            and (_process_visibility_partial(current)
                 or _scope_probe_partial(current, "memory:"))):
        partial = True
    return {
        "state": "unavailable" if unavailable else (
            "partial" if partial else "available"),
        "available": not unavailable,
        "partial": partial and not unavailable,
        "pressure": (
            memory.get("pressure")
            if memory.get("pressure") in ("normal", "warn", "critical")
            else "unknown"),
        "total_bytes": _number(memory.get("total")),
        "used_bytes": _number(memory.get("used")),
        "free_bytes": _number(memory.get("free")),
        "wired_bytes": _number(memory.get("wired")),
        "compressed_bytes": _number(memory.get("compressed")),
        "errors": errors,
        "top_consumers": _process_rows(
            current,
            ("footprint_bytes", "resident_size_bytes"),
            "memory", limit),
    }


def _battery_vital(points, current, limit):
    health = (points or {}).get("battery")
    metrics = _metric_map(current)
    rates = {
        "energy_rate_watts": {
            "value": metrics.get(("battery", "energy_rate_watts")),
            "unit": "watts",
        },
        "energy_score_per_s": {
            "value": metrics.get(("battery", "energy_score_per_s")),
            "unit": "unitless_per_second",
        },
    }
    consumers = _process_rows(
        current,
        ("cpu_pct", "energy_rate_watts", "energy_score_per_s"),
        "battery", limit)
    if not isinstance(health, dict):
        return {
            "state": "unavailable", "available": False, "partial": False,
            "present": None, "condition": None, "charge_pct": None,
            "health_pct": None, "cycle_count": None, "state_detail": None,
            "external_connected": None, "battery_flow_watts": None,
            "probe_error": None, "pmset_error": None,
            "rates": rates, "top_consumers": consumers,
        }
    present = health.get("present")
    if health.get("probe_error") or present is None:
        state = "unavailable"
    elif present is False:
        state = "absent"
    elif (health.get("pmset_error")
          or _process_visibility_partial(current)
          or _scope_probe_partial(current, "battery:")):
        state = "partial"
    else:
        state = "available"
    return {
        "state": state,
        "available": state not in ("unavailable",),
        "partial": state == "partial",
        "present": present if isinstance(present, bool) else None,
        "condition": health.get("condition"),
        "charge_pct": _number(health.get("charge_pct")),
        "health_pct": _number(health.get("health_pct")),
        "cycle_count": _number(health.get("cycle_count")),
        "state_detail": health.get("state"),
        "external_connected": (
            health.get("external_connected")
            if isinstance(health.get("external_connected"), bool) else None),
        "battery_flow_watts": _number(health.get("battery_flow_watts")),
        "probe_error": health.get("probe_error"),
        "pmset_error": health.get("pmset_error"),
        "rates": rates,
        "top_consumers": consumers,
    }


def _smart_vital(points):
    point = (points or {}).get("smart")
    if not isinstance(point, dict):
        return {
            "state": "unavailable", "available": False, "partial": False,
            "diskutil_available": False, "physical_drives_present": None,
            "smartctl_available": None, "drives": [],
        }
    diskutil_available = point.get("diskutil_available")
    if diskutil_available is None:
        diskutil_available = point.get("available")
    physical_present = point.get("physical_drives_present")
    drives = list(point.get("drives") or ())
    if physical_present is None and diskutil_available:
        physical_present = bool(drives)
    smartctl_available = point.get("smartctl_available")
    if diskutil_available is not True:
        state = "unavailable"
    elif physical_present is False:
        state = "absent"
    elif not drives or smartctl_available is not True or any(
            drive.get("diskutil_detail")
            or not drive.get("smartctl_available")
            or drive.get("smartctl_detail")
            for drive in drives if isinstance(drive, dict)):
        state = "partial"
    else:
        state = "available"
    return {
        "state": state,
        "available": state != "unavailable",
        "partial": state == "partial",
        "diskutil_available": diskutil_available is True,
        "physical_drives_present": (
            physical_present if isinstance(physical_present, bool) else None),
        "smartctl_available": (
            smartctl_available
            if isinstance(smartctl_available, bool) else None),
        "drives": drives,
    }


def _vitals(current, limit):
    points = (current or {}).get("points")
    cpu = _system_vital(
        current, "cpu",
        ("cpu_pct", "pkg_idle_wakeups_per_s", "interrupt_wakeups_per_s"),
        {
            "cpu_pct": "percent_of_one_core",
            "pkg_idle_wakeups_per_s": "per_second",
            "interrupt_wakeups_per_s": "per_second",
        },
        ("cpu_pct", "user_pct", "system_pct",
         "pkg_idle_wakeups_per_s", "interrupt_wakeups_per_s"),
        "cpu", limit)
    disk = _system_vital(
        current, "disk",
        ("read_bytes_per_s", "write_bytes_per_s"),
        {
            "read_bytes_per_s": "bytes_per_second",
            "write_bytes_per_s": "bytes_per_second",
        },
        ("diskio_bytes_read_per_s", "diskio_bytes_written_per_s"),
        "disk", limit)
    return {
        "cpu": cpu,
        "disk": disk,
        "memory": _memory_vital(points, current, limit),
        "battery": _battery_vital(points, current, limit),
        "smart": _smart_vital(points),
    }


def _sample_descriptor(current):
    if current is None:
        return None
    context = current.get("context") or {}
    return {
        "recorded_at": current.get("recorded_at"),
        "interval_s": current.get("interval_s"),
        "privilege": context.get("privilege"),
        "power_state": context.get("power_state"),
        "process_count": len(current.get("processes") or ()),
    }


def compose(diagnosis, limit=DEFAULT_LIMIT):
    """Compose checkup presentation data without reclassifying diagnosis."""
    current = diagnosis.get("current")
    reasons = list(diagnosis.get("partial_reasons") or ())
    document = schema.document(
        "checkup", "checkup",
        partial=diagnosis.get("partial", False),
        partial_reasons=reasons,
        overall=diagnosis.get("overall", "ok"),
        findings=diagnosis.get("findings") or [],
        notes=diagnosis.get("notes") or [],
        history=diagnosis.get("history"),
        sample=_sample_descriptor(current),
        vitals=_vitals(current, limit),
        error=diagnosis.get("error"))
    return anomaly._json_safe(document)


def run(interval=DEFAULT_INTERVAL, limit=DEFAULT_LIMIT, since=None, store=None):
    """Invoke canonical triage exactly once and compose its structured result."""
    diagnosis, exit_code = anomaly.run(
        "triage", interval=interval, limit=limit, since=since,
        store=store, scope="triage")
    return compose(diagnosis, limit=limit), exit_code


def _display(value, suffix=""):
    value = _number(value)
    return "unknown" if value is None else "%s%s" % (value, suffix)


def _state_label(vital, absent):
    if vital["state"] == "absent":
        return absent
    if vital["state"] == "unavailable":
        return "unknown"
    return vital["state"]


def _render(document):
    vitals = document["vitals"]
    lines = [
        BOLD + "stethoscope checkup · full-body exam" + RESET,
        "overall: %s" % cli.safe_text(document["overall"]),
    ]
    sample = document.get("sample")
    if sample:
        lines.append("sample: %s · %s · %s" % (
            _display(sample.get("interval_s"), "s"),
            cli.safe_text(sample.get("privilege") or "unknown"),
            cli.safe_text(sample.get("power_state") or "unknown")))
    else:
        lines.append("sample: unknown")

    cpu = vitals["cpu"]
    lines.append("cpu [%s]: %s · wakeups %s pkg / %s interrupt" % (
        _state_label(cpu, "absent"),
        _display(cpu["rates"]["cpu_pct"]["value"], "%"),
        _display(cpu["rates"]["pkg_idle_wakeups_per_s"]["value"]),
        _display(cpu["rates"]["interrupt_wakeups_per_s"]["value"])))
    disk = vitals["disk"]
    lines.append("disk [%s]: read %s B/s · write %s B/s" % (
        _state_label(disk, "absent"),
        _display(disk["rates"]["read_bytes_per_s"]["value"]),
        _display(disk["rates"]["write_bytes_per_s"]["value"])))
    memory = vitals["memory"]
    lines.append("memory [%s]: pressure %s · used %s / %s bytes" % (
        _state_label(memory, "absent"),
        cli.safe_text(memory.get("pressure") or "unknown"),
        _display(memory.get("used_bytes")),
        _display(memory.get("total_bytes"))))
    battery = vitals["battery"]
    lines.append("battery [%s]: charge %s · health %s · condition %s" % (
        _state_label(battery, "absent (no battery)"),
        _display(battery.get("charge_pct"), "%"),
        _display(battery.get("health_pct"), "%"),
        cli.safe_text(battery.get("condition") or "unknown")))
    smart = vitals["smart"]
    lines.append("smart [%s]: diskutil %s · smartctl %s" % (
        _state_label(smart, "absent (no physical drives)"),
        "available" if smart.get("diskutil_available") else "unknown",
        ("available" if smart.get("smartctl_available") is True else
         "unknown")))
    for drive in smart.get("drives") or ():
        lines.append("  drive %s · %s · SMART %s" % (
            cli.safe_text(drive.get("device") or "unknown"),
            cli.safe_text(drive.get("name") or "unknown"),
            cli.safe_text(drive.get("smart_status") or "unknown")))

    for note in document["notes"]:
        lines.append(DIM + "note: %s" % cli.safe_text(note) + RESET)
    if document["partial_reasons"]:
        lines.append(DIM + "partial: %s" % ", ".join(
            cli.safe_text(reason)
            for reason in document["partial_reasons"]) + RESET)
    if document["error"]:
        lines.append("error: %s" % cli.safe_text(document["error"]))
    history = document.get("history") or {}
    for replay_error in (history.get("replay_errors") or ())[:20]:
        lines.append(DIM + "replay: %s:%s: %s" % (
            cli.safe_text(replay_error.get("file", "unknown")),
            cli.safe_text(replay_error.get("line", "unknown")),
            cli.safe_text(replay_error.get("reason", "unknown"))) + RESET)
    if not document["findings"]:
        lines.append(DIM + "no findings." + RESET)
    for item in document["findings"]:
        mark = {"critical": "!!", "warn": "!", "info": "."}.get(
            item.get("severity"), ".")
        lines.append("  %s [%s/%s] %s" % (
            mark, cli.safe_text(item.get("area", "unknown")),
            cli.safe_text(item.get("detector", "unknown")),
            cli.safe_text(item.get("message", "unknown"))))
        for command in item.get("drill_down") or ():
            lines.append(DIM + "      -> %s" %
                         cli.safe_text(command) + RESET)
    return "\n".join(lines) + "\n"


def _empty_document(store, error):
    diagnosis = anomaly._usage_document(
        "checkup", "checkup", store, error)
    return compose(diagnosis)


USAGE = """stethoscope checkup — one-shot full-body examination

  checkup [--json] [--interval N] [--limit N] [--since WHEN] [--store DIR]

WHEN is a relative duration (3h), ISO timestamp, or local clock (3am).
Defaults: interval 1s, limit 20 (maximum 256), since 24h, canonical JSONL store.
Exit codes: 0 clean/warn · 1 critical finding · 2 usage · 4 probe/replay/runtime
"""


def main(argv):
    signal.signal(signal.SIGINT, lambda *args: sys.exit(0))
    args = list(argv[1:])
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return cli.EXIT_OK
    json_requested = "--json" in args
    options = None
    try:
        options = cli.parse_options(
            args, interval=DEFAULT_INTERVAL, limit=DEFAULT_LIMIT,
            extras={"store", "since"})
        cli.require_options(
            options, "checkup",
            {"json", "interval", "limit", "store", "since"})
        cli.require_positionals(options, "checkup", 0)
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
            cli.emit_json(_empty_document(
                options.store if options else None, exc))
        else:
            sys.stderr.write("%s\n\n%s" % (cli.safe_text(exc), USAGE))
        return cli.EXIT_USAGE

    document, exit_code = run(
        interval=options.interval, limit=options.limit,
        since=since, store=options.store)
    if options.json:
        cli.emit_json(document)
    else:
        stream = sys.stderr if exit_code == cli.EXIT_ERROR else sys.stdout
        stream.write(_render(document))
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
