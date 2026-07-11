#!/usr/bin/env python3
"""
stethoscope cpu — the per-process CPU scope.

Answers the question the disk scope answers for I/O: WHO is burning CPU
right now — and, because "constantly hogging" is a claim about history as
well as the current instant, how much each process has burned over its
whole awake life (casebook 0005).

Mechanism:

  * `proc_pid_rusage()` again — the same syscall the disk scope polls, read
    through `core/rusage.py`. Δ(ri_user_time + ri_system_time) / interval
    is %CPU; cumulative time over awake-age is lifetime duty. All time
    fields arrive timebase-converted from core — never raw ticks (S2).

  * `ri_energy_nj` (rusage flavor 6, where available) — the per-process
    energy ledger that actually moves at polling cadence, giving an honest
    live watts column. Where flavor 6 is absent the column renders "-",
    never a fabricated zero (casebook 0001.10/0001.11). ri_billed_energy
    is deliberately not used here: its deltas are frozen at 1 s cadence,
    which reads as "idle" for exactly the runaways this scope exists to
    catch.

Reading other users' processes needs root, same rule as the disk scope.

No third-party dependencies — system Python 3 + ctypes only.
"""

import os
import sys
import time
import signal

try:
    from core import rusage                # via the stethoscope dispatcher
except ImportError:                        # run directly: ./scopes/cpu.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core import rusage

list_pids = rusage.list_pids
proc_name = rusage.proc_name


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------

CLEAR = "\033[2J\033[H"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def cpu_time_str(ns):
    """Human cumulative CPU time: 89h12m / 12m05s / 3.2s."""
    s = ns / 1e9
    if s >= 3600:
        return "%dh%02dm" % (s // 3600, s % 3600 // 60)
    if s >= 60:
        return "%dm%02ds" % (s // 60, s % 60)
    return "%.1fs" % s


def watts_str(w):
    return "-" if w is None else "%.2fW" % w


def warn_if_not_root():
    if os.geteuid() != 0:
        sys.stderr.write(
            DIM + "note: not running as root — CPU for other users' processes is "
            "hidden. Re-run with sudo for full coverage.\n" + RESET)


# ---------------------------------------------------------------------------
# data layer — shared by the CLI and (future) TUI
# ---------------------------------------------------------------------------

def snapshot_cpu():
    """Return {(pid, start_abstime): (user_ns, system_ns, energy_nj)} for
    every accessible process. Keyed on process identity, not bare pid, so a
    reused pid cannot inherit a dead process's counters between snapshots
    (S10). energy_nj is None where rusage flavor 6 is unavailable.
    """
    snap = {}
    for pid in list_pids():
        sample = rusage.proc_cpu_sample(pid)
        if sample is not None:
            identity, user_ns, system_ns, energy_nj = sample
            snap[identity] = (user_ns, system_ns, energy_nj)
    return snap


def rank_cpu(prev, cur, dt):
    """Diff two snapshots over `dt` seconds into ranked CPU activity.

    Returns (rows, sys_cpu_pct, sys_watts) where each row is
    (cpu_pct, user_pct, system_pct, watts, total_cpu_ns, start_ticks,
    pid, name), sorted by cpu_pct descending. watts is None without a
    flavor-6 energy ledger; sys_watts is None if no process supplied one.
    Only processes active this interval appear in rows; the system totals
    sum across all processes.
    """
    rows = []
    sys_cpu = 0.0
    sys_watts = None
    dt = dt or 1.0
    dt_ns = dt * 1e9
    for key, (u, s, e) in cur.items():
        pu, ps, pe = prev.get(key, (u, s, e))
        du = max(0, u - pu)
        ds = max(0, s - ps)
        watts = None
        if e is not None and pe is not None:
            watts = max(0, e - pe) / dt / 1e9        # nJ/s -> W
            sys_watts = (sys_watts or 0.0) + watts
        cpu_pct = (du + ds) / dt_ns * 100.0
        sys_cpu += cpu_pct
        if du + ds > 0 or (watts or 0) > 0:
            rows.append((cpu_pct, du / dt_ns * 100.0, ds / dt_ns * 100.0,
                         watts, u + s, key[1], key[0], proc_name(key[0], key)))
    rows.sort(key=lambda r: -r[0])
    return rows, sys_cpu, sys_watts


def lifetime_duty_pct(total_cpu_ns, start_ticks, now_ticks):
    """Cumulative CPU over process awake-age, as a percentage.

    Awake-age deliberately: mach_absolute_time does not advance during
    machine sleep, so age from ri_proc_start_abstime excludes sleep — the
    honest denominator for a duty claim (casebook 0003.7).
    """
    awake_ns = rusage.ticks_to_ns(max(0, now_ticks - start_ticks))
    if awake_ns <= 0:
        return 0.0
    return total_cpu_ns / awake_ns * 100.0


# ---------------------------------------------------------------------------
# mode: top
# ---------------------------------------------------------------------------

def cmd_top(interval=1.0, limit=20):
    """Live per-process CPU, ranked by %CPU this interval."""
    warn_if_not_root()
    ncpu = os.cpu_count() or 1
    # Prime one sample so the first frame shows rates, not cumulative.
    prev = snapshot_cpu()
    prev_t = time.time()
    time.sleep(interval)

    while True:
        cur = snapshot_cpu()
        now = time.time()
        rows, sys_cpu, sys_watts = rank_cpu(prev, cur, now - prev_t)
        now_ticks = rusage.mach_absolute_time()
        out = [CLEAR]
        out.append(BOLD + "stethoscope cpu · per-process CPU · %s · refresh %.0fs" %
                   (time.strftime("%H:%M:%S"), interval) + RESET)
        power = "" if sys_watts is None else "   attributed power %.1f W" % sys_watts
        out.append(DIM + "system: %.0f%% of %d00%% (%d cores)%s   (ctrl-c to quit)" %
                   (sys_cpu, ncpu, ncpu, power) + RESET)
        out.append("")
        out.append(BOLD + "%7s  %-24s %7s %7s %7s %8s %9s %6s" %
                   ("PID", "COMMAND", "%CPU", "USER%", "SYS%", "POWER",
                    "CPU TIME", "DUTY%") + RESET)
        if not rows:
            out.append(DIM + "  (no CPU activity this interval)" + RESET)
        for cpu_pct, u_pct, s_pct, watts, total_ns, start_ticks, pid, name in rows[:limit]:
            duty = lifetime_duty_pct(total_ns, start_ticks, now_ticks)
            out.append("%7d  %-24s %7.1f %7.1f %7.1f %8s %9s %6.1f" %
                       (pid, name[:24], cpu_pct, u_pct, s_pct,
                        watts_str(watts), cpu_time_str(total_ns), duty))
        sys.stdout.write("\n".join(out) + "\n")
        sys.stdout.flush()

        prev = cur
        prev_t = now
        time.sleep(interval)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

USAGE = """stethoscope cpu — per-process CPU visibility for macOS

  cpu [top] [--interval N] [--limit N]   who is burning CPU now (default)

Columns: %CPU this interval (USER/SYS split), POWER (live watts from the
rusage flavor-6 energy ledger; '-' where the OS lacks it), lifetime CPU
TIME, and DUTY% — lifetime CPU over the process's awake-age, the number
that separates "hogging since boot" from "busy this second".

Run under sudo to see all processes:  sudo ./stethoscope cpu top
"""


def main(argv):
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    args = argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    mode = "top"
    if args and not args[0].startswith("-"):
        mode = args.pop(0)

    if mode == "top":
        interval, limit = 1.0, 20
        while args:
            a = args.pop(0)
            if a == "--interval":
                interval = float(args.pop(0))
            elif a == "--limit":
                limit = int(args.pop(0))
            else:
                sys.stderr.write("unknown option: %s\n" % a)
                return 2
        cmd_top(interval, limit)
        return 0

    sys.stderr.write("unknown mode: %s\n\n%s" % (mode, USAGE))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
