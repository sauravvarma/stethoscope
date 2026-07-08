#!/usr/bin/env python3
"""
stethoscope memory — per-process footprint and leak watching.

  memory top          WHO is using memory now?   (rank by phys_footprint)
  memory watch <pid>  IS a process leaking?       (footprint slope over time)

Ranks by `ri_phys_footprint` — the honest per-process number Activity Monitor's
"Memory" column shows — read from the shared rusage snapshot in core.py.
`memory top`'s header summarises system memory from `vm_stat` plus the kernel's
own pressure level (`kern.memorystatus_vm_pressure_level`).

`memory watch` samples one process's footprint at an interval and reports the
trend: a sustained positive slope with no plateau is a leak candidate. It prints
the slope (MB/min) and a sparkline — the primitive the v0.7 leak detector will
automate across every process (#4).

Reading other users' processes needs root; run under sudo for full coverage.
No third-party dependencies — system Python 3 + core.py.
"""

import os
import re
import signal
import subprocess
import sys
import time

try:
    from scopes import core, output
except ImportError:   # invoked with scopes/ directly on sys.path
    import core
    import output

# A footprint climbing faster than this, with no plateau, is a leak candidate.
LEAK_SLOPE_MB_PER_MIN = 1.0

_PRESSURE = {1: "normal", 2: "warn", 4: "critical"}
_SPARK = "▁▂▃▄▅▆▇█"


# ---------------------------------------------------------------------------
# system memory summary (vm_stat + sysctl)
# ---------------------------------------------------------------------------

def _sysctl_int(name):
    try:
        out = subprocess.run(["/usr/sbin/sysctl", "-n", name],
                             capture_output=True, text=True).stdout.strip()
        return int(out)
    except (ValueError, OSError):
        return None


def _vm_stat():
    """Parse `vm_stat` into {'<lowercased label>': bytes}."""
    out = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True).stdout
    pagesize = 4096
    m = re.search(r"page size of (\d+) bytes", out)
    if m:
        pagesize = int(m.group(1))
    counts = {}
    for ln in out.splitlines():
        if ":" not in ln:
            continue
        label, _, val = ln.partition(":")
        val = val.strip().rstrip(".")
        if val.isdigit():
            counts[label.strip().lower()] = int(val) * pagesize
    return counts


def system_memory():
    """A system memory summary in bytes, plus the kernel pressure level."""
    c = _vm_stat()
    active = c.get("pages active", 0)
    wired = c.get("pages wired down", 0)
    compressed = c.get("pages occupied by compressor", 0)
    level = _sysctl_int("kern.memorystatus_vm_pressure_level")
    return {
        "total": _sysctl_int("hw.memsize") or 0,
        "used": active + wired + compressed,   # active + wired + compressed
        "free": c.get("pages free", 0) + c.get("pages speculative", 0),
        "active": active,
        "inactive": c.get("pages inactive", 0),
        "wired": wired,
        "compressed": compressed,
        "pressure": _PRESSURE.get(level, "unknown"),
    }


# ---------------------------------------------------------------------------
# data layer
# ---------------------------------------------------------------------------

def rank_mem(cur):
    """[(footprint, resident, pid, name)] sorted by footprint descending."""
    rows = [(ru.footprint, ru.resident, pid, core.proc_name(pid))
            for pid, ru in cur.items() if ru.footprint > 0]
    rows.sort(reverse=True)
    return rows


def slope_mb_per_min(samples):
    """Least-squares footprint slope (MB/min) over [(t_seconds, bytes), ...]."""
    n = len(samples)
    if n < 2:
        return 0.0
    tbar = sum(t for t, _ in samples) / n
    ybar = sum(y for _, y in samples) / n
    denom = sum((t - tbar) ** 2 for t, _ in samples)
    if denom == 0:
        return 0.0
    slope_bytes_per_s = sum((t - tbar) * (y - ybar) for t, y in samples) / denom
    return slope_bytes_per_s / (1024 * 1024) * 60.0


def sparkline(values):
    """A unicode sparkline of `values` (min→max mapped over 8 blocks)."""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return _SPARK[0] * len(values)
    span = hi - lo
    return "".join(_SPARK[int((v - lo) / span * (len(_SPARK) - 1))] for v in values)


# ---------------------------------------------------------------------------
# presentation
# ---------------------------------------------------------------------------

def _top_document(rows, sysmem, limit):
    return output.document(
        "memory", "top",
        system=sysmem,
        processes=[{"pid": pid, "name": name,
                    "footprint": foot, "resident": res}
                   for foot, res, pid, name in rows[:limit]])


def _top_frame(rows, sysmem, interval, limit):
    out = [core.CLEAR]
    out.append(core.BOLD + "stethoscope memory · per-process footprint · %s · refresh %.0fs"
               % (time.strftime("%H:%M:%S"), interval) + core.RESET)
    out.append(core.DIM + "system: used %s / %s  ·  wired %s  ·  compressed %s  ·  pressure %s"
               % (core.human(sysmem["used"]), core.human(sysmem["total"]),
                  core.human(sysmem["wired"]), core.human(sysmem["compressed"]),
                  sysmem["pressure"]) + core.RESET)
    out.append("")
    out.append(core.BOLD + "%7s  %-30s %12s %12s"
               % ("PID", "COMMAND", "FOOTPRINT", "RESIDENT") + core.RESET)
    if not rows:
        out.append(core.DIM + "  (no accessible processes — try sudo)" + core.RESET)
    for foot, res, pid, name in rows[:limit]:
        out.append("%7d  %-30s %12s %12s"
                   % (pid, name[:30], core.human(foot), core.human(res)))
    return "\n".join(out) + "\n"


def _warn_if_not_root():
    if not core.is_root():
        sys.stderr.write(core.DIM + "note: not root — memory for other users' processes "
                         "is hidden. Re-run with sudo for full coverage.\n" + core.RESET)


def cmd_top(o):
    """Per-process memory footprint, ranked. Honors --json/--once/--duration."""
    if not o.json:
        _warn_if_not_root()
    deadline = None if o.duration is None else time.time() + o.duration
    while True:
        rows = rank_mem(core.snapshot_rusage())
        sysmem = system_memory()
        if o.json:
            output.emit_json(_top_document(rows, sysmem, o.limit))
        else:
            sys.stdout.write(_top_frame(rows, sysmem, o.interval, o.limit))
            sys.stdout.flush()
        if o.once or (deadline is not None and time.time() >= deadline):
            break
        time.sleep(o.interval)
    return output.EXIT_OK


def cmd_watch(pid, o):
    """Sample one process's footprint over time; report slope + leak candidacy."""
    name = core.proc_name(pid)
    if core.proc_rusage(pid) is None:
        sys.stderr.write("memory watch: pid %d not accessible (gone, or try sudo)\n" % pid)
        return output.EXIT_PERM if not core.is_root() else output.EXIT_USAGE

    samples = []          # (t_seconds, footprint_bytes)
    t0 = time.time()
    deadline = None if o.duration is None else t0 + o.duration
    rc = output.EXIT_OK
    while True:
        ru = core.proc_rusage(pid)
        if ru is None:
            if not o.json:
                print(core.DIM + "process %d exited." % pid + core.RESET)
            break
        samples.append((time.time() - t0, ru.footprint))
        window = [y for _, y in samples[-60:]]
        slope = slope_mb_per_min(samples)
        leak = slope > LEAK_SLOPE_MB_PER_MIN and len(samples) >= 5
        if leak:
            rc = output.EXIT_FINDINGS
        if o.json:
            output.emit_json(output.document(
                "memory", "watch", pid=pid, name=name,
                footprint=ru.footprint, resident=ru.resident,
                slope_mb_per_min=slope, samples=len(samples),
                leak_candidate=leak))
        else:
            sys.stdout.write(_watch_frame(pid, name, ru, slope, leak, window, o.interval))
            sys.stdout.flush()
        if o.once or (deadline is not None and time.time() >= deadline):
            break
        time.sleep(o.interval)
    return rc


def _watch_frame(pid, name, ru, slope, leak, window, interval):
    out = [core.CLEAR]
    out.append(core.BOLD + "stethoscope memory watch · pid %d (%s) · %s · every %.0fs"
               % (pid, name, time.strftime("%H:%M:%S"), interval) + core.RESET)
    sign = "+" if slope >= 0 else ""
    verdict = (core.BOLD + "LEAK CANDIDATE" + core.RESET) if leak else "steady"
    out.append("footprint %s   resident %s   slope %s%.2f MB/min   %s"
               % (core.human(ru.footprint), core.human(ru.resident),
                  sign, slope, verdict))
    out.append(core.DIM + "trend " + sparkline(window) + core.RESET)
    out.append("")
    out.append(core.DIM + "(sustained positive slope with no plateau = leak; ctrl-c to quit)"
               + core.RESET)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

USAGE = """stethoscope memory — per-process footprint and leak watching

  memory [top]         who is using memory now, ranked by footprint (default)
  memory watch <pid>   sample one process's footprint over time (leak trend)

Agent / scripting flags: --json  --once  --duration N  --interval N  --limit N
Exit codes: 0 ok · 1 leak candidate (watch) · 2 usage · 3 needs root

Run under sudo to see all processes:  sudo ./stethoscope memory top
"""


def main(argv):
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    args = argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return output.EXIT_OK

    mode = "top"
    if args and not args[0].startswith("-"):
        mode = args.pop(0)

    try:
        o = output.parse_opts(args)
    except output.OptsError as e:
        sys.stderr.write("%s\n" % e)
        return output.EXIT_USAGE

    if mode == "top":
        return cmd_top(o)
    if mode == "watch":
        if not o.rest:
            sys.stderr.write("watch needs a pid\n")
            return output.EXIT_USAGE
        try:
            pid = int(o.rest[0])
        except ValueError:
            sys.stderr.write("watch: not a pid: %r\n" % o.rest[0])
            return output.EXIT_USAGE
        return cmd_watch(pid, o)

    sys.stderr.write("unknown mode: %s\n\n%s" % (mode, USAGE))
    return output.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main(sys.argv))
