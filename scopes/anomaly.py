#!/usr/bin/env python3
"""
stethoscope anomaly — baseline deviation, leak, runaway, and triage detectors.

The detectors are pure data-layer functions over live readings plus the
record/history substrate. The CLI only gathers inputs and renders findings.
No third-party dependencies — system Python 3 stdlib only.
"""

import os
import signal
import sys
import time

try:
    from scopes import battery, core, cpu, disk, memory, output, record, smart
except ImportError:   # invoked with scopes/ directly on sys.path
    import battery
    import core
    import cpu
    import disk
    import memory
    import output
    import record
    import smart


DEFAULT_SINCE = "24h"
DEFAULT_DURATION = 3.0
DEFAULT_INTERVAL = 1.0
_SEV_ORDER = {"ok": 0, "info": 1, "warn": 2, "critical": 3}
_PRESSURE_VALUE = {"normal": 1.0, "warn": 2.0, "critical": 4.0, "unknown": 0.0}
# A baseline band whose p50..p99 spread is below this fraction of its center has
# not yet captured real variance (the cold-start case: a few near-identical
# samples). Judging deviation against such a razor-thin band flags noise as
# "critical", so we skip it until the history is richer.
MIN_BAND_SPREAD_FRAC = 0.05


class AnomalyOpts:
    def __init__(self):
        self.json = False
        self.duration = DEFAULT_DURATION
        self.interval = DEFAULT_INTERVAL
        self.limit = 10
        self.db = record.default_db_path()
        self.since = DEFAULT_SINCE
        self.rest = []


def _need_value(args, i, name):
    if i + 1 >= len(args):
        raise output.OptsError("%s needs a value" % name)
    return args[i + 1]


def _float_value(args, i, name):
    val = _need_value(args, i, name)
    try:
        return float(val)
    except ValueError:
        raise output.OptsError("%s wants a number, got %r" % (name, val))


def _int_value(args, i, name):
    val = _need_value(args, i, name)
    try:
        return int(val)
    except ValueError:
        raise output.OptsError("%s wants a number, got %r" % (name, val))


def parse_opts(args):
    o = AnomalyOpts()
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--json":
            o.json = True
        elif a == "--duration":
            o.duration = _float_value(args, i, a)
            i += 1
        elif a == "--interval":
            o.interval = _float_value(args, i, a)
            i += 1
        elif a == "--limit":
            o.limit = _int_value(args, i, a)
            i += 1
        elif a == "--db":
            o.db = os.path.expanduser(_need_value(args, i, a))
            i += 1
        elif a == "--since":
            o.since = _need_value(args, i, a)
            i += 1
        elif a.startswith("-") and a != "-":
            raise output.OptsError("unknown option: %s" % a)
        else:
            o.rest.append(a)
        i += 1
    if o.interval <= 0:
        raise output.OptsError("--interval must be > 0")
    if o.duration <= 0:
        raise output.OptsError("--duration must be > 0")
    return o


def _finding(severity, area, message, drill, detector, score=0.0, **fields):
    f = {"severity": severity, "area": area, "detector": detector,
         "message": message, "drill": drill, "score": round(float(score), 3)}
    f.update(fields)
    return f


def _drill(scope, metric, pid=None, name=None):
    if pid is not None:
        if scope == "memory":
            return "stethoscope memory watch %d" % pid
        if scope == "cpu":
            return "stethoscope cpu top"
    if scope == "cpu":
        return "stethoscope cpu top"
    if scope == "memory":
        return "stethoscope memory top"
    if scope == "battery":
        return "stethoscope battery health"
    if scope == "disk":
        return "stethoscope disk top"
    if scope == "smart" and name:
        return "stethoscope smart %s" % name
    return "stethoscope %s" % scope


def _sort_findings(findings):
    findings.sort(key=lambda f: (-_SEV_ORDER.get(f["severity"], 0),
                                 -float(f.get("score", 0.0)),
                                 f.get("area", ""), f.get("message", "")))
    return findings


def worst_verdict(findings):
    verdict = "ok"
    for f in findings:
        sev = f.get("severity", "ok")
        if _SEV_ORDER.get(sev, 0) > _SEV_ORDER[verdict]:
            verdict = sev
    return verdict


def exit_code_for_report(report):
    return output.EXIT_FINDINGS if report.get("overall") == "critical" else output.EXIT_OK


# ---------------------------------------------------------------------------
# live reads
# ---------------------------------------------------------------------------

def _usage_to_disk_map(snap):
    return {pid: (ru.read, ru.write) for pid, ru in snap.items()}


def collect_live_vitals(sample_seconds=0.4, limit=10):
    """Gather live vitals once for triage and deviation/runaway detectors."""
    prev_mach, prev = cpu.snapshot()
    prev_t = time.time()
    time.sleep(sample_seconds)
    cur_mach, cur = cpu.snapshot()
    now = time.time()
    dt = now - prev_t
    cpu_rows, sys_cpu = cpu.rank_cpu(prev, cur, prev_mach, cur_mach, dt)
    disk_rows, sys_read, sys_write = disk.rank_io(_usage_to_disk_map(prev),
                                                  _usage_to_disk_map(cur), dt)
    mem = memory.system_memory()
    batt = battery.battery_health()
    drives = []
    for dev, internal in smart.list_physical_drives():
        drives.append(smart.drive_health(dev, internal))
    current = [
        {"scope": "cpu", "metric": "system_cpu_pct", "value": sys_cpu},
        {"scope": "memory", "metric": "used", "value": mem.get("used")},
        {"scope": "memory", "metric": "total", "value": mem.get("total")},
        {"scope": "memory", "metric": "used_pct",
         "value": (mem.get("used", 0) / mem.get("total", 1) * 100.0) if mem.get("total") else None},
        {"scope": "memory", "metric": "pressure_level",
         "value": _PRESSURE_VALUE.get(mem.get("pressure"), 0.0)},
        {"scope": "disk", "metric": "read_per_s", "value": sys_read},
        {"scope": "disk", "metric": "write_per_s", "value": sys_write},
    ]
    if batt.get("present"):
        current.extend([
            {"scope": "battery", "metric": "charge_pct", "value": batt.get("charge_pct")},
            {"scope": "battery", "metric": "health_pct", "value": batt.get("health_pct")},
        ])
    return {
        "ts": int(now),
        "current_metrics": [m for m in current if m.get("value") is not None],
        "process_cpu": [{"pid": pid, "name": name, "cpu_pct": c, "wakeups_per_s": wk}
                        for c, wk, _iw, _tw, pid, name in cpu_rows[:limit]],
        "memory_top": [{"pid": pid, "name": name, "footprint": foot, "resident": res}
                       for foot, res, pid, name in memory.rank_mem(cur)[:limit]],
        "disk_top": [{"pid": pid, "name": name, "read_per_s": rr, "write_per_s": wr}
                     for _tot, rr, wr, _r, _w, pid, name in disk_rows[:limit]],
        "system": {
            "cpu": {"system_cpu_pct": sys_cpu, "ncpu": cpu.NCPU},
            "memory": mem,
            "battery": batt,
            "smart": {"drives": drives},
            "disk": {"read_per_s": sys_read, "write_per_s": sys_write},
        },
    }


# ---------------------------------------------------------------------------
# detector data layers
# ---------------------------------------------------------------------------

def detect_deviation(current_metrics, baseline_result, now_ts=None, min_count=3):
    """Find current system metrics above this hour's p90/p99 baseline."""
    hour = int(time.strftime("%H", time.localtime(time.time() if now_ts is None else now_ts)))
    bands = {}
    for b in baseline_result.get("baselines", []):
        bands[(b["hour"], b["scope"], b["metric"])] = b
    findings = []
    for cur in current_metrics:
        scope, metric, value = cur["scope"], cur["metric"], cur.get("value")
        if value is None:
            continue
        b = bands.get((hour, scope, metric))
        if not b or b.get("count", 0) < min_count:
            continue
        p90 = b.get("p90")
        p99 = b.get("p99")
        if p90 is None or p99 is None or value <= p90:
            continue
        # Skip cold-start / degenerate bands: if the historical p50..p99 spread
        # is negligible relative to the band's center, the baseline hasn't seen
        # real variation yet, so being a hair above p99 is noise, not an anomaly.
        p50 = b.get("p50")
        center = max(abs(p50 or 0.0), abs(p90), 1.0)
        if (p99 - p50) < MIN_BAND_SPREAD_FRAC * center:
            continue
        sev = "critical" if value > p99 else "warn"
        threshold = p99 if sev == "critical" else p90
        score = value / threshold if threshold else value
        findings.append(_finding(
            sev, scope,
            "%s.%s %.2f is above this hour's p%s %.2f"
            % (scope, metric, value, "99" if sev == "critical" else "90", threshold),
            _drill(scope, metric), "deviation", score,
            metric=metric, current=value,
            baseline={"hour": hour, "count": b.get("count"), "p50": b.get("p50"),
                      "p90": p90, "p99": p99}))
    return _sort_findings(findings)


def _history_rows(conn, since_ts, metric):
    return conn.execute("""
        SELECT ts, scope, metric, pid, COALESCE(name, '?'), value
        FROM samples
        WHERE ts >= ? AND pid IS NOT NULL AND metric = ?
        ORDER BY pid, ts
    """, (int(since_ts), metric)).fetchall()


def detect_leaks_from_rows(rows, limit=10, min_samples=3, min_slope_mb_min=1.0):
    """Rank monotonic per-pid footprint growth from rows shaped like samples."""
    by_pid = {}
    for row in rows:
        if isinstance(row, dict):
            ts, pid = row["ts"], row["pid"]
            name, value = row.get("name") or "?", row["value"]
        else:
            ts, _scope, _metric, pid, name, value = row
        if pid is None:
            continue
        by_pid.setdefault(pid, {"name": name or "?", "samples": []})["samples"].append((ts, value))
    findings = []
    for pid, item in by_pid.items():
        samples = sorted(item["samples"])
        if len(samples) < min_samples:
            continue
        values = [v for _t, v in samples]
        slope = memory.slope_mb_per_min(samples)
        drops = sum(1 for a, b in zip(values, values[1:]) if b < a)
        mostly_rising = values[-1] > values[0] and drops <= max(0, len(values) // 4)
        if slope < min_slope_mb_min or not mostly_rising:
            continue
        current = values[-1]
        score = slope * (current / (1024 * 1024))
        sev = "critical" if slope >= 10.0 and current >= 512 * 1024 * 1024 else "warn"
        findings.append(_finding(
            sev, "memory",
            "%s (pid %d) footprint is rising %.2f MB/min (now %s)"
            % (item["name"], pid, slope, core.human(current)),
            _drill("memory", "process_footprint", pid, item["name"]),
            "leak", score, pid=pid, name=item["name"],
            metric="process_footprint", slope_mb_per_min=slope,
            current_footprint=current, samples=len(samples)))
    return _sort_findings(findings)[:limit]


def sample_live_leaks(duration=DEFAULT_DURATION, interval=DEFAULT_INTERVAL, limit=10):
    """Fallback leak detector when there is no per-pid footprint history."""
    samples = {}
    deadline = time.time() + duration
    while True:
        now = time.time()
        for foot, _res, pid, name in memory.rank_mem(core.snapshot_rusage())[:max(limit * 3, limit)]:
            samples.setdefault(pid, {"name": name, "samples": []})["samples"].append((now, foot))
        if now >= deadline:
            break
        time.sleep(min(interval, max(0.0, deadline - now)))
    rows = []
    for pid, item in samples.items():
        for ts, value in item["samples"]:
            rows.append({"ts": ts, "pid": pid, "name": item["name"], "value": value})
    return detect_leaks_from_rows(rows, limit=limit)


def detect_runaways(current_processes, history_rows=None, limit=10):
    """Find processes currently far above their own recorded CPU norm."""
    history_rows = history_rows or []
    by_pid = {}
    for row in history_rows:
        if isinstance(row, dict):
            if row.get("metric") != "process_cpu_pct":
                continue
            pid, name, value = row.get("pid"), row.get("name") or "?", row.get("value")
        else:
            _ts, _scope, metric, pid, name, value = row
            if metric != "process_cpu_pct":
                continue
        if pid is not None and value is not None:
            by_pid.setdefault(pid, {"name": name or "?", "values": []})["values"].append(value)
    findings = []
    for p in current_processes:
        pid = p.get("pid")
        cur = float(p.get("cpu_pct", 0.0) or 0.0)
        wake = float(p.get("wakeups_per_s", 0.0) or 0.0)
        hist = by_pid.get(pid, {}).get("values", [])
        if len(hist) >= 3:
            p90 = record.percentile(hist, 90)
            norm = p90 if p90 is not None else 0.0
            threshold = max(30.0, norm * 2.0, norm + 20.0)
            if cur <= threshold:
                continue
            score = cur / max(norm, 1.0)
            sev = "critical" if cur >= 90.0 else "warn"
            message = "%s (pid %d) CPU %.1f%% is above its p90 norm %.1f%%" % (
                p.get("name") or "?", pid, cur, norm)
        else:
            if cur < 75.0 and wake < 500.0:
                continue
            score = max(cur / 75.0, wake / 500.0)
            sev = "critical" if cur >= 95.0 or wake >= 1000.0 else "warn"
            message = "%s (pid %d) looks runaway: CPU %.1f%%, wakeups %.1f/s" % (
                p.get("name") or "?", pid, cur, wake)
            norm = None
        findings.append(_finding(
            sev, "cpu", message, _drill("cpu", "process_cpu_pct", pid, p.get("name")),
            "runaway", score, pid=pid, name=p.get("name") or "?",
            metric="process_cpu_pct", current_cpu_pct=cur,
            current_wakeups_per_s=wake, baseline_p90=norm))
    return _sort_findings(findings)[:limit]


def point_in_time_findings(vitals):
    findings = []
    mem = vitals.get("system", {}).get("memory", {})
    if mem.get("pressure") == "critical":
        findings.append(_finding("critical", "memory", "memory pressure is critical",
                                 "stethoscope memory top", "point", 100.0,
                                 metric="pressure"))
    elif mem.get("pressure") == "warn":
        findings.append(_finding("warn", "memory", "memory pressure is elevated",
                                 "stethoscope memory top", "point", 10.0,
                                 metric="pressure"))
    batt = vitals.get("system", {}).get("battery", {})
    if batt.get("present") and batt.get("condition") and batt.get("condition") != "Normal":
        findings.append(_finding("warn", "battery",
                                 "battery condition: %s" % batt.get("condition"),
                                 "stethoscope battery health", "point", 10.0,
                                 metric="condition"))
    for d in vitals.get("system", {}).get("smart", {}).get("drives", []):
        for w in d.get("warnings", []):
            sev = w.get("severity") if w.get("severity") in _SEV_ORDER else "warn"
            findings.append(_finding(sev, "smart", "%s: %s" % (d.get("device"), w.get("message")),
                                     _drill("smart", "status", name=d.get("device")),
                                     "point", 100.0 if sev == "critical" else 10.0,
                                     device=d.get("device"), metric="smart_status"))
    return _sort_findings(findings)


def triage_report(vitals, deviation_findings, leak_findings, runaway_findings, notes=None):
    findings = []
    findings.extend(point_in_time_findings(vitals))
    findings.extend(deviation_findings)
    findings.extend(leak_findings)
    findings.extend(runaway_findings)
    _sort_findings(findings)
    return {"overall": worst_verdict(findings), "findings": findings,
            "vitals": vitals.get("system", {}), "notes": notes or []}


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def _connect_and_since(o):
    conn = record.connect_db(o.db)
    return conn, record.parse_since(o.since) if o.since else None


def _load_baseline(conn, since_ts):
    return record.compute_baseline(conn, since_ts=since_ts)


def _empty_history_note(conn):
    n = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    return None if n else "history DB has no samples yet; run `stethoscope record` to build a baseline"


def run_deviation(o, live=None):
    conn, since_ts = _connect_and_since(o)
    try:
        note = _empty_history_note(conn)
        live = live or collect_live_vitals(limit=o.limit)
        findings = [] if note else detect_deviation(live["current_metrics"], _load_baseline(conn, since_ts),
                                                    live.get("ts"))
        return {"findings": findings, "current": live["current_metrics"],
                "notes": [note] if note else [], "since": since_ts}
    finally:
        conn.close()


def run_leaks(o):
    conn, since_ts = _connect_and_since(o)
    try:
        rows = _history_rows(conn, since_ts, "process_footprint")
        if rows:
            findings = detect_leaks_from_rows(rows, limit=o.limit)
            notes = []
        else:
            findings = sample_live_leaks(o.duration, o.interval, o.limit)
            notes = ["no per-process footprint history; used short live sampling fallback"]
        return {"findings": findings, "notes": notes, "since": since_ts}
    finally:
        conn.close()


def run_runaway(o, live=None):
    conn, since_ts = _connect_and_since(o)
    try:
        rows = _history_rows(conn, since_ts, "process_cpu_pct")
        live = live or collect_live_vitals(limit=o.limit)
        findings = detect_runaways(live["process_cpu"], rows, limit=o.limit)
        notes = [] if rows else ["no per-process CPU history; used absolute CPU/wakeup thresholds"]
        return {"findings": findings, "current": live["process_cpu"],
                "notes": notes, "since": since_ts}
    finally:
        conn.close()


def run_triage(o):
    notes = []
    live = collect_live_vitals(limit=o.limit)
    conn, since_ts = _connect_and_since(o)
    try:
        empty = _empty_history_note(conn)
        if empty:
            notes.append(empty)
            deviations = []
        else:
            deviations = detect_deviation(live["current_metrics"], _load_baseline(conn, since_ts),
                                          live.get("ts"))
        footprint_rows = _history_rows(conn, since_ts, "process_footprint")
        if footprint_rows:
            leaks = detect_leaks_from_rows(footprint_rows, limit=o.limit)
        else:
            notes.append("no per-process footprint history; used short live sampling fallback")
            leaks = sample_live_leaks(o.duration, o.interval, o.limit)
        cpu_rows = _history_rows(conn, since_ts, "process_cpu_pct")
        if not cpu_rows:
            notes.append("no per-process CPU history; used absolute CPU/wakeup thresholds")
        runaways = detect_runaways(live["process_cpu"], cpu_rows, limit=o.limit)
    finally:
        conn.close()
    return triage_report(live, deviations, leaks, runaways, notes)


def _document(command, result, db=None):
    fields = dict(result)
    if db is not None:
        fields["db"] = db
    return output.document("anomaly" if command != "triage" else "triage", command, **fields)


def _render_findings(title, result):
    out = [core.BOLD + title + core.RESET]
    if result.get("overall"):
        out.append("verdict: %s" % result["overall"])
    for note in result.get("notes", []):
        out.append(core.DIM + "note: %s" % note + core.RESET)
    findings = result.get("findings", [])
    if not findings:
        out.append(core.DIM + "no findings." + core.RESET)
    for f in findings:
        mark = {"critical": "‼", "warn": "!", "info": "·"}.get(f["severity"], "·")
        out.append("  %s [%s/%s] %s" % (mark, f["area"], f.get("detector"), f["message"]))
        out.append(core.DIM + "      → %s" % f["drill"] + core.RESET)
    return "\n".join(out) + "\n"


def _emit_or_print(o, command, result):
    if o.json:
        output.emit_json(_document(command, result, o.db))
    else:
        title = "stethoscope triage · anomaly diagnosis" if command == "triage" else (
            "stethoscope anomaly · %s" % command)
        sys.stdout.write(_render_findings(title, result))


def main(argv):
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    args = argv[1:]
    prog_scope = argv[0].split()[-1] if argv else "anomaly"
    if args and args[0] in ("-h", "--help"):
        print(TRIAGE_USAGE if prog_scope == "triage" else USAGE)
        return output.EXIT_OK
    mode = "triage" if prog_scope == "triage" else "triage"
    if prog_scope != "triage" and args and not args[0].startswith("-"):
        mode = args.pop(0)
    try:
        o = parse_opts(args)
    except (output.OptsError, ValueError) as e:
        sys.stderr.write("%s\n%s" % (e, TRIAGE_USAGE if prog_scope == "triage" else USAGE))
        return output.EXIT_USAGE
    try:
        if mode == "triage":
            result = run_triage(o)
            _emit_or_print(o, "triage", result)
            return exit_code_for_report(result)
        if mode in ("deviation", "baseline"):
            result = run_deviation(o)
        elif mode in ("leaks", "leak"):
            result = run_leaks(o)
        elif mode in ("runaway", "runaways"):
            result = run_runaway(o)
        else:
            sys.stderr.write("unknown mode: %s\n\n%s" % (mode, USAGE))
            return output.EXIT_USAGE
        _emit_or_print(o, mode, result)
        return output.EXIT_FINDINGS if any(f["severity"] == "critical" for f in result["findings"]) else output.EXIT_OK
    except ValueError as e:
        sys.stderr.write("%s\n" % e)
        return output.EXIT_USAGE


TRIAGE_USAGE = """stethoscope triage — ranked anomaly diagnosis

  triage [--db PATH] [--since <1h|30m|2d|ISO>] [--json] [--duration N] [--interval N] [--limit N]

Exit codes: 0 ok/warn/info · 1 critical finding · 2 usage
"""


USAGE = """stethoscope anomaly — anomaly detectors

  anomaly triage      run every detector (same as `stethoscope triage`)
  anomaly deviation   compare live system metrics to recorded baseline
  anomaly leaks       rank sustained per-process footprint growth
  anomaly runaway     rank CPU/wakeup runaways vs history or thresholds

Flags: --db PATH --since <when> --json --duration N --interval N --limit N
Exit codes: 0 no critical findings · 1 critical finding · 2 usage
"""


if __name__ == "__main__":
    sys.exit(main(sys.argv))
