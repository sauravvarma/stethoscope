#!/usr/bin/env python3
"""
stethoscope cpu — per-process CPU utilization and wakeups.

Ranks processes by CPU% using the same poll-and-diff spine `disk` uses, over
the shared rusage snapshot in core.py. `proc_pid_rusage()` carries cumulative
user + system CPU time (mach-absolute units) and idle/interrupt wakeup counts,
so cpu reads the very struct disk already samples — no new tracing framework.

  cpu top        WHO is using the CPU right now?      (rank by CPU%)
  cpu wakeups    WHO is waking the CPU?               (rank by wakeups/sec)

CPU% is delta(user+system CPU ticks) / delta(wall ticks) in the same mach
units — a timebase-free ratio. It is per-machine, not per-core: a process using
two cores fully reads ~200%, and the system line sums across all processes
(so it can approach ncpu × 100%).

Wakeups (idle + interrupt per second) surface battery- and scheduler-hostile
processes even at low CPU% — the number Activity Monitor's Energy tab shows,
and groundwork for the battery scope (#2).

Reading other users' processes needs root, so run under sudo for full-system
coverage. No third-party dependencies — system Python 3 + core.py.
"""

import os
import signal
import sys
import time

try:
    from scopes import core, output
except ImportError:   # invoked with scopes/ directly on sys.path
    import core
    import output

NCPU = os.cpu_count() or 1


# ---------------------------------------------------------------------------
# data layer — a snapshot is (mach clock, {pid: RUsage}); rank_cpu diffs two.
# ---------------------------------------------------------------------------

def snapshot():
    """(_mach_now, {pid: RUsage}) — one sampling step for cpu diffing."""
    now = core.mach_absolute_time()
    return now, core.snapshot_rusage()


def rank_cpu(prev, cur, prev_mach, cur_mach, dt):
    """Diff two snapshots into ranked CPU activity.

    Returns (rows, sys_cpu_pct) where each row is
    (cpu_pct, wake_per_s, idle_per_s, intr_per_s, pid, name), sorted by CPU%
    descending. Only processes active this interval appear; sys_cpu_pct sums
    every process's CPU% (can exceed 100% across cores). `dt` is wall seconds
    for the wakeup rates; CPU% uses the mach-tick wall delta.
    """
    rows = []
    sys_cpu = 0.0
    dwall_ticks = (cur_mach - prev_mach) or 1
    dt = dt or 1.0
    for pid, ru in cur.items():
        pru = prev.get(pid)
        if pru is None:
            continue   # new pid: no baseline this interval
        d_cpu = max(0, (ru.user_time + ru.system_time)
                    - (pru.user_time + pru.system_time))
        cpu_pct = d_cpu / dwall_ticks * 100.0
        idle_ps = max(0, ru.idle_wkups - pru.idle_wkups) / dt
        intr_ps = max(0, ru.interrupt_wkups - pru.interrupt_wkups) / dt
        sys_cpu += cpu_pct
        if cpu_pct > 0 or idle_ps or intr_ps:
            rows.append((cpu_pct, idle_ps + intr_ps, idle_ps, intr_ps,
                         pid, core.proc_name(pid)))
    rows.sort(reverse=True)
    return rows, sys_cpu


# ---------------------------------------------------------------------------
# presentation — human table and the --json contract (see SCHEMA.md)
# ---------------------------------------------------------------------------

def _document(rows, sys_cpu, command, limit):
    return output.document(
        "cpu", command,
        system={"cpu_pct": sys_cpu, "ncpu": NCPU},
        processes=[{"pid": pid, "name": name, "cpu_pct": cpu,
                    "wakeups_per_s": wk,
                    "idle_wakeups_per_s": iw, "interrupt_wakeups_per_s": tw}
                   for cpu, wk, iw, tw, pid, name in rows[:limit]])


def _frame(rows, sys_cpu, command, interval, limit):
    title = "per-process CPU" if command == "top" else "per-process wakeups"
    out = [core.CLEAR]
    out.append(core.BOLD + "stethoscope cpu · %s · %s · refresh %.0fs"
               % (title, time.strftime("%H:%M:%S"), interval) + core.RESET)
    out.append(core.DIM + "system: CPU %.1f%% of %d cores   (ctrl-c to quit)"
               % (sys_cpu, NCPU) + core.RESET)
    out.append("")
    out.append(core.BOLD + "%7s  %-26s %8s %10s %10s"
               % ("PID", "COMMAND", "CPU%", "WAKE/s", "INTR/s") + core.RESET)
    if not rows:
        out.append(core.DIM + "  (no CPU activity this interval)" + core.RESET)
    for cpu, wk, iw, tw, pid, name in rows[:limit]:
        out.append("%7d  %-26s %7.1f%% %10.1f %10.1f"
                   % (pid, name[:26], cpu, iw, tw))
    return "\n".join(out) + "\n"


def _warn_if_not_root():
    if not core.is_root():
        sys.stderr.write(core.DIM + "note: not root — CPU for other users' processes "
                         "is hidden. Re-run with sudo for full coverage.\n" + core.RESET)


def cmd_top(o, command="top"):
    """Live per-process CPU, ranked by CPU% (top) or wakeups/s (wakeups).

    Honors the agent contract: --json (one document per sample), --once,
    --duration N.
    """
    if not o.json:
        _warn_if_not_root()
    prev_mach, prev = snapshot()
    prev_t = time.time()
    time.sleep(o.interval)
    deadline = None if o.duration is None else time.time() + o.duration

    while True:
        cur_mach, cur = snapshot()
        now = time.time()
        rows, sys_cpu = rank_cpu(prev, cur, prev_mach, cur_mach, now - prev_t)
        if command == "wakeups":
            rows.sort(key=lambda r: r[1], reverse=True)   # by total wakeups/s
        prev, prev_mach, prev_t = cur, cur_mach, now
        if o.json:
            output.emit_json(_document(rows, sys_cpu, command, o.limit))
        else:
            sys.stdout.write(_frame(rows, sys_cpu, command, o.interval, o.limit))
            sys.stdout.flush()
        if o.once or (deadline is not None and time.time() >= deadline):
            break
        time.sleep(o.interval)
    return output.EXIT_OK


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

USAGE = """stethoscope cpu — per-process CPU utilization and wakeups

  cpu [top]      who is using the CPU now, ranked by CPU%   (default)
  cpu wakeups    who is waking the CPU, ranked by wakeups/sec

Agent / scripting flags: --json  --once  --duration N  --interval N  --limit N
Exit codes: 0 ok · 2 usage · 3 needs root

Run under sudo to see all processes:  sudo ./stethoscope cpu top
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

    if mode in ("top", "wakeups"):
        return cmd_top(o, command=mode)

    sys.stderr.write("unknown mode: %s\n\n%s" % (mode, USAGE))
    return output.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main(sys.argv))
