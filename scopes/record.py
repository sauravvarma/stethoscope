#!/usr/bin/env python3
"""
stethoscope record/history — persistent machine-health samples and baselines.

  record                 foreground sampler for launchd (SQLite ring buffer)
  history --since 1h     retrospective metric summary from recorded samples
  history baseline       per-hour p50/p90/p99 normals for this machine

Launchd example (save as ~/Library/LaunchAgents/com.example.stethoscope.record.plist):

<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.example.stethoscope.record</string>
  <key>ProgramArguments</key><array>
    <string>/path/to/stethoscope</string><string>record</string>
    <string>--interval</string><string>60</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>

No third-party dependencies — system Python 3 stdlib only.
"""

import datetime as _dt
import math
import os
import signal
import sqlite3
import sys
import time

try:
    from scopes import battery, core, cpu, disk, memory, output
except ImportError:   # invoked with scopes/ directly on sys.path
    import battery
    import core
    import cpu
    import disk
    import memory
    import output

DEFAULT_DB = "~/Library/Application Support/stethoscope/history.db"
DEFAULT_INTERVAL = 60.0
DEFAULT_MAX_AGE_DAYS = 14.0
DEFAULT_LIMIT = 5
_PRESSURE_VALUE = {"normal": 1.0, "warn": 2.0, "critical": 4.0, "unknown": 0.0}


class RecordOpts:
    def __init__(self):
        self.json = False
        self.once = False
        self.duration = None
        self.interval = DEFAULT_INTERVAL
        self.limit = DEFAULT_LIMIT
        self.db = default_db_path()
        self.max_age_days = DEFAULT_MAX_AGE_DAYS


class HistoryOpts:
    def __init__(self):
        self.json = False
        self.limit = 10
        self.db = default_db_path()
        self.since = None
        self.scope = None
        self.rest = []


def default_db_path():
    return os.path.expanduser(DEFAULT_DB)


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


def parse_record_opts(args):
    o = RecordOpts()
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--json":
            o.json = True
        elif a == "--once":
            o.once = True
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
        elif a == "--max-age-days":
            o.max_age_days = _float_value(args, i, a)
            i += 1
        elif a.startswith("-") and a != "-":
            raise output.OptsError("unknown option: %s" % a)
        else:
            raise output.OptsError("unexpected argument: %s" % a)
        i += 1
    if o.interval <= 0:
        raise output.OptsError("--interval must be > 0")
    if o.max_age_days <= 0:
        raise output.OptsError("--max-age-days must be > 0")
    return o


def parse_history_opts(args):
    o = HistoryOpts()
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--json":
            o.json = True
        elif a == "--limit":
            o.limit = _int_value(args, i, a)
            i += 1
        elif a == "--db":
            o.db = os.path.expanduser(_need_value(args, i, a))
            i += 1
        elif a == "--since":
            o.since = _need_value(args, i, a)
            i += 1
        elif a == "--scope":
            o.scope = _need_value(args, i, a)
            i += 1
        elif a.startswith("-") and a != "-":
            raise output.OptsError("unknown option: %s" % a)
        else:
            o.rest.append(a)
        i += 1
    return o


def connect_db(path):
    path = os.path.expanduser(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    init_db(conn)
    return conn


def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS samples (
            ts INTEGER NOT NULL,
            scope TEXT NOT NULL,
            metric TEXT NOT NULL,
            pid INTEGER,
            name TEXT,
            value REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_scope_metric ON samples(scope, metric)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_pid ON samples(pid)")
    conn.commit()


def insert_metric(conn, ts, scope, metric, value, pid=None, name=None):
    if value is None:
        return
    try:
        value = float(value)
    except (TypeError, ValueError):
        return
    if math.isnan(value) or math.isinf(value):
        return
    conn.execute(
        "INSERT INTO samples(ts, scope, metric, pid, name, value) VALUES (?, ?, ?, ?, ?, ?)",
        (int(ts), scope, metric, pid, name, value))


def insert_rows(conn, rows):
    for row in rows:
        insert_metric(conn, row["ts"], row["scope"], row["metric"], row.get("value"),
                      row.get("pid"), row.get("name"))


def prune_old(conn, now_ts, max_age_days):
    cutoff = int(now_ts - max_age_days * 86400)
    conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
    return cutoff


def append_rows(conn, rows, max_age_days, now_ts=None):
    now_ts = int(time.time() if now_ts is None else now_ts)
    with conn:
        insert_rows(conn, rows)
        cutoff = prune_old(conn, now_ts, max_age_days)
    return cutoff


def _usage_to_disk_map(snap):
    return {pid: (ru.read, ru.write) for pid, ru in snap.items()}


def collect_sample(interval, limit=DEFAULT_LIMIT):
    """Collect one system sample. Returns (ts, rows, summary)."""
    pid = os.getpid()
    start_self = core.proc_rusage(pid)
    prev_mach, prev = cpu.snapshot()
    prev_t = time.time()
    time.sleep(interval)
    cur_mach, cur = cpu.snapshot()
    now = time.time()
    dt = now - prev_t
    ts = int(now)

    cpu_rows, sys_cpu = cpu.rank_cpu(prev, cur, prev_mach, cur_mach, dt)
    disk_rows, sys_read, sys_write = disk.rank_io(_usage_to_disk_map(prev), _usage_to_disk_map(cur), dt)
    mem = memory.system_memory()
    batt = battery.battery_health()
    end_self = core.proc_rusage(pid)

    rows = []

    def add(scope, metric, value, pid=None, name=None):
        rows.append({"ts": ts, "scope": scope, "metric": metric,
                     "pid": pid, "name": name, "value": value})

    add("cpu", "system_cpu_pct", sys_cpu)
    add("memory", "used", mem.get("used"))
    add("memory", "total", mem.get("total"))
    add("memory", "used_pct", (mem.get("used", 0) / mem.get("total", 1) * 100.0)
        if mem.get("total") else None)
    add("memory", "pressure_level", _PRESSURE_VALUE.get(mem.get("pressure"), 0.0))
    if batt.get("present"):
        add("battery", "charge_pct", batt.get("charge_pct"))
        add("battery", "health_pct", batt.get("health_pct"))
    add("disk", "read_per_s", sys_read)
    add("disk", "write_per_s", sys_write)

    if start_self is not None and end_self is not None:
        dwall = (cur_mach - prev_mach) or 1
        d_cpu = max(0, (end_self.user_time + end_self.system_time)
                    - (start_self.user_time + start_self.system_time))
        add("record", "self_cpu_pct", d_cpu / dwall * 100.0)
        add("record", "self_footprint", end_self.footprint)
        add("record", "self_resident", end_self.resident)

    for cpu_pct, _wk, _iw, _tw, p, name in cpu_rows[:limit]:
        add("cpu", "process_cpu_pct", cpu_pct, p, name)
    mem_rows = memory.rank_mem(cur)
    for foot, _res, p, name in mem_rows[:limit]:
        add("memory", "process_footprint", foot, p, name)
    for _total, read_rate, write_rate, _r, _w, p, name in disk_rows[:limit]:
        add("disk", "process_read_per_s", read_rate, p, name)
        add("disk", "process_write_per_s", write_rate, p, name)

    summary = {
        "ts": ts,
        "interval": dt,
        "cpu": {"system_cpu_pct": sys_cpu},
        "memory": {"used": mem.get("used"), "total": mem.get("total"),
                   "pressure": mem.get("pressure")},
        "battery": {"present": bool(batt.get("present")),
                    "charge_pct": batt.get("charge_pct"),
                    "health_pct": batt.get("health_pct")},
        "disk": {"read_per_s": sys_read, "write_per_s": sys_write},
        "record": {"rows": len(rows)},
    }
    return ts, rows, summary


def parse_since(text, now=None):
    now = time.time() if now is None else float(now)
    s = text.strip()
    if not s:
        raise ValueError("empty --since")
    unit = s[-1].lower()
    if unit in ("m", "h", "d"):
        try:
            amount = float(s[:-1])
        except ValueError:
            amount = None
        if amount is not None:
            mult = {"m": 60, "h": 3600, "d": 86400}[unit]
            return int(now - amount * mult)
    if len(s) >= 3 and s[-2:].lower() == "am" and s[:-2].isdigit():
        hour = int(s[:-2]) % 12
        return _today_at(hour, now)
    if len(s) >= 3 and s[-2:].lower() == "pm" and s[:-2].isdigit():
        hour = int(s[:-2]) % 12 + 12
        return _today_at(hour, now)
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = _dt.datetime.fromisoformat(iso)
    except ValueError:
        raise ValueError("unsupported --since value: %s" % text)
    if dt.tzinfo is None:
        return int(dt.timestamp())
    return int(dt.astimezone().timestamp())


def _today_at(hour, now):
    base = _dt.datetime.fromtimestamp(now)
    dt = base.replace(hour=hour, minute=0, second=0, microsecond=0)
    if dt.timestamp() > now:
        dt -= _dt.timedelta(days=1)
    return int(dt.timestamp())


def query_history(conn, since_ts, scope=None, limit=10):
    params = [int(since_ts)]
    scope_sql = ""
    if scope:
        scope_sql = " AND scope = ?"
        params.append(scope)
    metric_rows = conn.execute("""
        SELECT scope, metric, COUNT(*) AS n, MIN(value), MAX(value), AVG(value)
        FROM samples
        WHERE ts >= ? AND pid IS NULL%s
        GROUP BY scope, metric
        ORDER BY scope, metric
    """ % scope_sql, params).fetchall()
    latest_rows = conn.execute("""
        SELECT s.scope, s.metric, s.value, s.ts
        FROM samples s
        JOIN (
            SELECT scope, metric, MAX(ts) AS ts
            FROM samples
            WHERE ts >= ? AND pid IS NULL%s
            GROUP BY scope, metric
        ) last ON last.scope = s.scope AND last.metric = s.metric AND last.ts = s.ts
        WHERE s.pid IS NULL
    """ % scope_sql, params).fetchall()
    latest = {(r[0], r[1]): {"value": r[2], "ts": r[3]} for r in latest_rows}
    metrics = []
    for sc, metric, n, lo, hi, mean in metric_rows:
        item = {"scope": sc, "metric": metric, "count": n,
                "min": lo, "max": hi, "peak": hi, "mean": mean}
        item["latest"] = latest.get((sc, metric))
        metrics.append(item)

    top_params = [int(since_ts)]
    top_scope_sql = ""
    if scope:
        top_scope_sql = " AND scope = ?"
        top_params.append(scope)
    top_params.append(int(limit))
    top = conn.execute("""
        SELECT scope, metric, pid, COALESCE(name, '?') AS name,
               COUNT(*) AS n, MAX(value) AS peak, AVG(value) AS mean
        FROM samples
        WHERE ts >= ? AND pid IS NOT NULL%s
        GROUP BY scope, metric, pid, name
        ORDER BY peak DESC
        LIMIT ?
    """ % top_scope_sql, top_params).fetchall()
    return {
        "since": int(since_ts),
        "until": int(time.time()),
        "scope_filter": scope,
        "metrics": metrics,
        "top_consumers": [
            {"scope": sc, "metric": metric, "pid": pid, "name": name,
             "count": n, "peak": peak, "mean": mean}
            for sc, metric, pid, name, n, peak, mean in top
        ],
    }


def percentile(values, pct):
    vals = sorted(float(v) for v in values)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * pct / 100.0
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def compute_baseline(conn, since_ts=None, scope=None):
    clauses = ["pid IS NULL"]
    params = []
    if since_ts is not None:
        clauses.append("ts >= ?")
        params.append(int(since_ts))
    if scope:
        clauses.append("scope = ?")
        params.append(scope)
    rows = conn.execute(
        "SELECT ts, scope, metric, value FROM samples WHERE " + " AND ".join(clauses),
        params).fetchall()
    groups = {}
    for ts, sc, metric, value in rows:
        hour = int(time.strftime("%H", time.localtime(ts)))
        groups.setdefault((hour, sc, metric), []).append(value)
    baselines = []
    for (hour, sc, metric), values in sorted(groups.items()):
        baselines.append({
            "hour": hour,
            "scope": sc,
            "metric": metric,
            "count": len(values),
            "p50": percentile(values, 50),
            "p90": percentile(values, 90),
            "p99": percentile(values, 99),
        })
    return {"since": since_ts, "scope_filter": scope, "baselines": baselines}


def _record_document(db, summary, rows, cutoff):
    return output.document("record", "record", db=db, cutoff=cutoff,
                           sample=summary, rows_written=len(rows))


def _history_document(result):
    return output.document("history", "query", **result)


def _baseline_document(result):
    return output.document("history", "baseline", **result)


def _record_frame(db, summary, rows):
    mem = summary["memory"]
    disk_s = summary["disk"]
    batt = summary["battery"]
    batt_s = "no battery" if not batt["present"] else "battery %s%% health %s%%" % (
        batt.get("charge_pct"), batt.get("health_pct"))
    return ("recorded %d metrics to %s\n"
            "cpu %.1f%% · memory %s/%s pressure %s · disk read %s write %s · %s\n" % (
                len(rows), db, summary["cpu"]["system_cpu_pct"],
                core.human(mem.get("used") or 0), core.human(mem.get("total") or 0),
                mem.get("pressure"), core.rate(disk_s["read_per_s"]),
                core.rate(disk_s["write_per_s"]), batt_s))


def _history_frame(result):
    lines = ["history since %s" % _dt.datetime.fromtimestamp(result["since"]).isoformat(timespec="seconds")]
    for m in result["metrics"]:
        lines.append("%-8s %-22s mean %.2f peak %.2f n=%d" % (
            m["scope"], m["metric"], m["mean"], m["peak"], m["count"]))
    if result["top_consumers"]:
        lines.append("top consumers:")
        for t in result["top_consumers"]:
            lines.append("  %-8s %-20s pid %-7s %-20s peak %.2f" % (
                t["scope"], t["metric"], t["pid"], t["name"][:20], t["peak"]))
    return "\n".join(lines) + "\n"


def _baseline_frame(result):
    lines = ["baseline normals"]
    for b in result["baselines"]:
        lines.append("%02d:00 %-8s %-22s p50 %.2f p90 %.2f p99 %.2f n=%d" % (
            b["hour"], b["scope"], b["metric"], b["p50"], b["p90"], b["p99"], b["count"]))
    return "\n".join(lines) + "\n"


def cmd_record(args):
    try:
        o = parse_record_opts(args)
    except output.OptsError as e:
        sys.stderr.write("%s\n%s" % (e, RECORD_USAGE))
        return output.EXIT_USAGE
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    conn = connect_db(o.db)
    deadline = None if o.duration is None else time.time() + o.duration
    try:
        while True:
            sample_interval = min(o.interval, 1.0) if o.once else o.interval
            ts, rows, summary = collect_sample(sample_interval, o.limit)
            cutoff = append_rows(conn, rows, o.max_age_days, ts)
            if o.json:
                output.emit_json(_record_document(o.db, summary, rows, cutoff))
            else:
                sys.stdout.write(_record_frame(o.db, summary, rows))
                sys.stdout.flush()
            if o.once or (deadline is not None and time.time() >= deadline):
                break
        return output.EXIT_OK
    finally:
        conn.close()


def cmd_history(args):
    mode = "query"
    if args and args[0] == "baseline":
        mode = "baseline"
        args = args[1:]
    try:
        o = parse_history_opts(args)
        since_ts = parse_since(o.since) if o.since else None
    except (output.OptsError, ValueError) as e:
        sys.stderr.write("%s\n%s" % (e, HISTORY_USAGE))
        return output.EXIT_USAGE
    if mode == "query" and since_ts is None:
        sys.stderr.write("history needs --since <when>\n%s" % HISTORY_USAGE)
        return output.EXIT_USAGE
    conn = connect_db(o.db)
    try:
        if mode == "baseline":
            result = compute_baseline(conn, since_ts, o.scope)
            if o.json:
                output.emit_json(_baseline_document(result))
            else:
                sys.stdout.write(_baseline_frame(result))
        else:
            result = query_history(conn, since_ts, o.scope, o.limit)
            if o.json:
                output.emit_json(_history_document(result))
            else:
                sys.stdout.write(_history_frame(result))
        return output.EXIT_OK
    finally:
        conn.close()


RECORD_USAGE = """stethoscope record — foreground SQLite sampler

  record [--interval N] [--db PATH] [--max-age-days N] [--once] [--json]

Default DB: ~/Library/Application Support/stethoscope/history.db
Exit codes: 0 ok · 2 usage
"""

HISTORY_USAGE = """stethoscope history — query recorded samples

  history --since <1h|30m|2d|3am|ISO> [--scope name] [--db PATH] [--json]
  history baseline [--since <when>] [--scope name] [--db PATH] [--json]

Exit codes: 0 ok · 2 usage
"""


def main(argv):
    args = argv[1:]
    prog_scope = argv[0].split()[-1] if argv else "record"
    if args and args[0] in ("-h", "--help"):
        print(HISTORY_USAGE if prog_scope == "history" else RECORD_USAGE)
        return output.EXIT_OK
    if prog_scope == "history":
        return cmd_history(args)
    return cmd_record(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
