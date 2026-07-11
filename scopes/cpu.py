#!/usr/bin/env python3
"""
stethoscope cpu — the per-process CPU and wakeup scope.

Answers the question the disk scope answers for I/O: WHO is burning CPU
right now — and, because "constantly hogging" is a claim about history as
well as the current instant, how much each process has burned over its
whole awake life (casebook 0005) — plus WHO is waking the CPU, since a
process can be scheduler-hostile at near-zero %CPU (casebook 0004).

  top       WHO is burning CPU now?       (rank by %CPU this interval)
  wakeups   WHO is waking the CPU?        (rank by total wakeup rate)

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

  * Δri_pkg_idle_wkups and Δri_interrupt_wkups / interval — two distinct
    wakeup vitals that are never summed into one alarm-facing counter
    (S8): a canonical sleep-loop storm measures zero pkg-idle wakeups
    while holding ~800 interrupt wakeups/s against a ~1/s quiet baseline
    (casebook 0004). Every row keeps both counters separate; only the
    display/ranking-facing `total_wakeups_per_s` sums them, for the human
    WAKE/s column and the `wakeups` sort order — never for a detector.

Reading other users' processes needs root, same rule as the disk scope;
the scope still runs without it and marks --json output partial.

No third-party dependencies — system Python 3 + ctypes only.
"""

import os
import sys
import time
import signal
from collections import namedtuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import cli, rusage, schema

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
    if not cli.is_root():
        sys.stderr.write(
            DIM + "note: not running as root — CPU for other users' processes is "
            "hidden. Re-run with sudo for full coverage.\n" + RESET)


# ---------------------------------------------------------------------------
# data layer — shared by the CLI and (future) TUI
# ---------------------------------------------------------------------------

def snapshot_cpu():
    """Return {(pid, start_abstime): (user_ns, system_ns, energy_nj,
    pkg_idle_wakeups, interrupt_wakeups)} for every accessible process.
    Keyed on process identity, not bare pid, so a reused pid cannot inherit
    a dead process's counters between snapshots (S10). energy_nj is None
    where rusage flavor 6 is unavailable. pkg_idle_wakeups and
    interrupt_wakeups are the cumulative ri_pkg_idle_wkups /
    ri_interrupt_wkups counters (S8, casebook 0004).
    """
    snap = {}
    for pid in list_pids():
        sample = rusage.proc_cpu_sample(pid)
        if sample is not None:
            identity, user_ns, system_ns, energy_nj, pkg_wkups, intr_wkups = sample
            snap[identity] = (user_ns, system_ns, energy_nj, pkg_wkups, intr_wkups)
    return snap


# A ranked process row. pkg_wakeups_per_s and interrupt_wakeups_per_s are
# the two S8 vitals, kept apart; total_wakeups_per_s is their sum, computed
# only for display/ranking (never fed to a detector — casebook 0004).
CpuRow = namedtuple("CpuRow", (
    "cpu_pct", "user_pct", "system_pct", "watts", "total_cpu_ns",
    "start_ticks", "pid", "name",
    "pkg_wakeups_per_s", "interrupt_wakeups_per_s", "total_wakeups_per_s",
))

# System-wide totals for one interval. watts is None if no process supplied
# an energy ledger; the two wakeup rates stay separate here too.
SysTotals = namedtuple("SysTotals", (
    "cpu_pct", "watts", "pkg_wakeups_per_s", "interrupt_wakeups_per_s",
    "total_wakeups_per_s",
))


def _diff_cpu(prev, cur, dt):
    """Diff two snapshots over `dt` seconds into unsorted CPU/wakeup rows.

    Returns (rows, sys_totals): rows is a list of CpuRow, sys_totals is one
    SysTotals summed across all processes. Only processes with CPU, watts,
    or wakeup activity this interval appear in rows; the system totals sum
    across every accessible process regardless. A key absent from `prev`
    (new pid, or a reused pid — S10) is baselined to itself, so it
    contributes no rate its first interval.
    """
    rows = []
    sys_cpu = 0.0
    sys_watts = None
    sys_pkg = 0.0
    sys_intr = 0.0
    dt = dt or 1.0
    dt_ns = dt * 1e9
    for key, (u, s, e, pkg, intr) in cur.items():
        pu, ps, pe, ppkg, pintr = prev.get(key, (u, s, e, pkg, intr))
        du = max(0, u - pu)
        ds = max(0, s - ps)
        dpkg = max(0, pkg - ppkg) / dt
        dintr = max(0, intr - pintr) / dt
        watts = None
        if e is not None and pe is not None:
            watts = max(0, e - pe) / dt / 1e9        # nJ/s -> W
            sys_watts = (sys_watts or 0.0) + watts
        cpu_pct = (du + ds) / dt_ns * 100.0
        sys_cpu += cpu_pct
        sys_pkg += dpkg
        sys_intr += dintr
        total_wake = dpkg + dintr
        if du + ds > 0 or (watts or 0) > 0 or total_wake > 0:
            rows.append(CpuRow(
                cpu_pct, du / dt_ns * 100.0, ds / dt_ns * 100.0, watts,
                u + s, key[1], key[0], proc_name(key[0], key),
                dpkg, dintr, total_wake))
    sys_totals = SysTotals(sys_cpu, sys_watts, sys_pkg, sys_intr, sys_pkg + sys_intr)
    return rows, sys_totals


def rank_cpu(prev, cur, dt):
    """Diff two snapshots, ranked by %CPU descending. Backs `top`."""
    rows, sys_totals = _diff_cpu(prev, cur, dt)
    rows.sort(key=lambda r: -r.cpu_pct)
    return rows, sys_totals


def rank_wakeups(prev, cur, dt):
    """Diff two snapshots, ranked by total wakeup rate descending. Backs
    `wakeups`. total_wakeups_per_s is display/ranking-only — pkg-idle and
    interrupt stay separate fields on every row (S8, casebook 0004);
    detectors baseline each counter against its own history, never the sum.
    """
    rows, sys_totals = _diff_cpu(prev, cur, dt)
    rows.sort(key=lambda r: -r.total_wakeups_per_s)
    return rows, sys_totals


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
# presentation — human table and the --json contract
# ---------------------------------------------------------------------------

def _visibility():
    partial = not cli.is_root()
    return partial, ["not_root"] if partial else []


def _process_entry(row, now_ticks):
    return {
        "pid": row.pid,
        "name": row.name,
        "cpu_pct": row.cpu_pct,
        "user_pct": row.user_pct,
        "system_pct": row.system_pct,
        "watts": row.watts,
        "total_cpu_seconds": row.total_cpu_ns / 1e9,
        "lifetime_duty_pct": lifetime_duty_pct(
            row.total_cpu_ns, row.start_ticks, now_ticks),
        "pkg_idle_wakeups_per_s": row.pkg_wakeups_per_s,
        "interrupt_wakeups_per_s": row.interrupt_wakeups_per_s,
        "total_wakeups_per_s": row.total_wakeups_per_s,
    }


def _document(command, rows, sys_totals, ncpu, limit, now_ticks):
    partial, reasons = _visibility()
    return schema.document(
        "cpu", command, partial=partial, partial_reasons=reasons,
        system={
            "cpu_pct": sys_totals.cpu_pct,
            "watts": sys_totals.watts,
            "pkg_idle_wakeups_per_s": sys_totals.pkg_wakeups_per_s,
            "interrupt_wakeups_per_s": sys_totals.interrupt_wakeups_per_s,
            "total_wakeups_per_s": sys_totals.total_wakeups_per_s,
            "ncpu": ncpu,
        },
        processes=[_process_entry(row, now_ticks) for row in rows[:limit]])


def _frame(command, rows, sys_totals, ncpu, interval, limit, now_ticks, styled=True):
    clear = CLEAR if styled else ""
    bold = BOLD if styled else ""
    dim = DIM if styled else ""
    reset = RESET if styled else ""
    title = "per-process CPU" if command == "top" else "per-process wakeups"
    out = [clear]
    out.append(bold + "stethoscope cpu · %s · %s · refresh %.0fs" %
               (title, time.strftime("%H:%M:%S"), interval) + reset)
    power = "" if sys_totals.watts is None else "   attributed power %.1f W" % sys_totals.watts
    out.append(
        dim + "system: %.0f%% of %d00%% (%d cores)   wake %.0f/s (pkg %.0f/s · "
        "intr %.0f/s)%s   (ctrl-c to quit)" %
        (sys_totals.cpu_pct, ncpu, ncpu, sys_totals.total_wakeups_per_s,
         sys_totals.pkg_wakeups_per_s, sys_totals.interrupt_wakeups_per_s, power)
        + reset)
    out.append("")
    # WAKE/s is the same total rate `wakeups` ranks by, never idle-only
    # (Copilot #40); PKG/s and INTR/s stay alongside so the two vitals that
    # are never summed for detection (S8/0004) remain visible separately.
    out.append(bold + "%7s  %-24s %7s %7s %7s %8s %9s %6s %8s %8s %8s" %
               ("PID", "COMMAND", "%CPU", "USER%", "SYS%", "POWER",
                "CPU TIME", "DUTY%", "WAKE/s", "PKG/s", "INTR/s") + reset)
    if not rows:
        out.append(dim + "  (no CPU activity this interval)" + reset)
    for row in rows[:limit]:
        duty = lifetime_duty_pct(row.total_cpu_ns, row.start_ticks, now_ticks)
        out.append(
            "%7d  %-24s %7.1f %7.1f %7.1f %8s %9s %6.1f %8.1f %8.1f %8.1f" %
            (row.pid, row.name[:24], row.cpu_pct, row.user_pct, row.system_pct,
             watts_str(row.watts), cpu_time_str(row.total_cpu_ns), duty,
             row.total_wakeups_per_s, row.pkg_wakeups_per_s,
             row.interrupt_wakeups_per_s))
    return "\n".join(out) + "\n"


def _run(command, rank_fn, options):
    """Shared live loop for `top` and `wakeups`: sample, diff, render, repeat.

    Honors the agent contract: --json (one document per sample), --once,
    --duration N, --interval N, --limit N — parsed and validated by
    core.cli. Non-TTY human output skips clear-screen/color codes.
    """
    if not options.json:
        warn_if_not_root()
    ncpu = os.cpu_count() or 1
    # Prime one sample so the first frame shows rates, not cumulative.
    prev = snapshot_cpu()
    prev_t = time.monotonic()
    started = prev_t

    while True:
        time.sleep(options.interval)
        cur = snapshot_cpu()
        now = time.monotonic()
        rows, sys_totals = rank_fn(prev, cur, now - prev_t)
        now_ticks = rusage.mach_absolute_time()
        if options.json:
            cli.emit_json(
                _document(command, rows, sys_totals, ncpu, options.limit, now_ticks))
        else:
            sys.stdout.write(_frame(
                command, rows, sys_totals, ncpu, options.interval, options.limit,
                now_ticks, styled=sys.stdout.isatty()))
            sys.stdout.flush()
        if options.once or (
                options.duration is not None and now - started >= options.duration):
            return cli.EXIT_OK
        prev, prev_t = cur, now


def cmd_top(options):
    """Live per-process CPU, ranked by %CPU this interval."""
    return _run("top", rank_cpu, options)


def cmd_wakeups(options):
    """Live per-process wakeups, ranked by total wakeup rate this interval."""
    return _run("wakeups", rank_wakeups, options)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

USAGE = """stethoscope cpu — per-process CPU and wakeup visibility for macOS

  cpu [top] [--interval N] [--limit N] [--once | --duration N] [--json]
                                          who is burning CPU now (default)
  cpu wakeups [--interval N] [--limit N] [--once | --duration N] [--json]
                                          who is waking the CPU

Columns: %CPU this interval (USER/SYS split), POWER (live watts from the
rusage flavor-6 energy ledger; '-' where the OS lacks it), lifetime CPU
TIME, DUTY% (lifetime CPU over the process's awake-age), and WAKE/s — the
total wakeup rate `wakeups` ranks by — alongside PKG/s and INTR/s so the two
wakeup vitals (never summed for detection, casebook 0004) stay unambiguous.

Not running as root hides other users' processes; the scope still runs and
marks --json output partial (reason "not_root") rather than failing.

Run under sudo to see all processes:  sudo ./stethoscope cpu top
Exit codes: 0 ok · 2 usage
"""


def main(argv):
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    args = argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return cli.EXIT_OK

    mode = "top"
    if args and not args[0].startswith("-"):
        mode = args.pop(0)

    try:
        options = cli.parse_options(args)
        if mode in ("top", "wakeups"):
            cli.require_positionals(options, mode, 0)
            return cmd_top(options) if mode == "top" else cmd_wakeups(options)
    except cli.OptionsError as exc:
        sys.stderr.write("%s\n" % exc)
        return cli.EXIT_USAGE

    sys.stderr.write("unknown mode: %s\n\n%s" % (mode, USAGE))
    return cli.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main(sys.argv))
