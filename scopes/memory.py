#!/usr/bin/env python3
"""
stethoscope memory — per-process footprint and leak watching.

  memory top          WHO is using memory now?   (rank by phys_footprint)
  memory watch <pid>  IS a process leaking?       (footprint slope over time)

Ranks by `ri_phys_footprint` — the honest per-process number Activity
Monitor's "Memory" column shows — read through core/rusage.py's raw struct
accessor, the same one the disk scope uses for its own snapshot (S9/S10).
`resident_size_bytes` (`ri_resident_size`) is exposed alongside it: resident
counts pages regardless of sharing, footprint is Apple's dedupe'd "what this
process is actually costing you" number, and the two can diverge a lot for
processes that share large mapped regions (issue #3).

`memory top`'s header summarises system memory from `vm_stat` plus the
kernel's own pressure verdict (`kern.memorystatus_vm_pressure_level`) —
parsed in core/vmstat.py, which is the only place in this scope that touches
subprocess text; this module only ever sees structures back from it.

`memory watch` samples one process's footprint at an interval and reports
the trend: a sustained positive slope with no recent plateau is a leak
candidate. It prints the slope (MB/min) and a sparkline — the primitive the
v0.7 leak detector will automate across every process (issue #4).

Reading other users' processes needs root; run under sudo for full coverage.
No third-party dependencies — system Python 3 + core/rusage.py + core/vmstat.py.
"""

import errno
import os
import signal
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import cli, rusage, schema, vmstat

list_pids = rusage.list_pids
proc_name = rusage.proc_name
proc_identity = rusage.proc_identity
system_memory = vmstat.system_memory

# A footprint climbing faster than this, with no plateau, is a leak candidate.
LEAK_SLOPE_MB_PER_MIN = 1.0
# Fewer samples than this cannot support any trend claim — a two-point line
# is noise, not evidence (issue #4: "require enough samples before flagging").
MIN_LEAK_SAMPLES = 5
# A recent window this wide, sloped below PLATEAU_SLOPE_MB_PER_MIN, is a
# plateau: growth that has stopped, even if the all-time average is still high.
PLATEAU_WINDOW = 10
PLATEAU_SLOPE_MB_PER_MIN = 0.1

_SPARK = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"

CLEAR = "\033[2J\033[H"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------

def human(n):
    """Human-readable bytes (same convention as the disk scope)."""
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024.0:
            if unit == "B":
                return "%d%s" % (int(n), unit)
            return "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fP" % n


def warn_if_not_root():
    if not cli.is_root():
        sys.stderr.write(
            DIM + "note: not running as root — memory for other users' processes is "
            "hidden. Re-run with sudo for full coverage.\n" + RESET)


def _visibility():
    partial = not cli.is_root()
    return partial, ["not_root"] if partial else []


# ---------------------------------------------------------------------------
# data layer — rank by footprint, shared by the CLI and (future) TUI
# ---------------------------------------------------------------------------

def snapshot_footprint():
    """Return {(pid, start_abstime): (footprint_bytes, resident_bytes)} for
    every accessible process. Keyed on identity (S10), matching the shape of
    the disk/cpu scopes' own snapshots. Uses core.rusage's raw struct
    directly (like disk.snapshot_diskio) rather than the fully-converted
    rusage() dict — this scope needs two raw counters, not the whole vitals
    set, from every pid on the machine each interval.
    """
    snap = {}
    for pid in list_pids():
        info = rusage._raw_rusage(pid)
        if info is not None:
            snap[(pid, info.ri_proc_start_abstime)] = (
                info.ri_phys_footprint, info.ri_resident_size)
    return snap


def rank_footprint(snap):
    """[(footprint, resident, pid, name)] sorted by footprint descending.

    Ranks every accessible process in `snap` — no activity filter, unlike
    the rate-based disk/cpu ranks: a footprint is a snapshot, not a delta,
    so "no I/O this interval" has no memory analogue.
    """
    rows = [(footprint, resident, pid, proc_name(pid, key))
            for key, (footprint, resident) in snap.items()
            for pid in (key[0],)]
    rows.sort(reverse=True)
    return rows


# ---------------------------------------------------------------------------
# data layer — slope, plateau, and the leak-candidate detector
# ---------------------------------------------------------------------------

def slope_mb_per_min(samples):
    """Least-squares footprint slope (MB/min) over [(t_seconds, bytes), ...].

    Fewer than 2 points, or a degenerate (all-equal) time axis, both mean
    "no slope is computable yet" — reported as a flat 0.0, not a fabricated
    trend.
    """
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


def is_plateaued(samples, window=PLATEAU_WINDOW,
                 plateau_slope_mb_per_min=PLATEAU_SLOPE_MB_PER_MIN):
    """True if the most recent `window` samples have flattened out.

    A leak candidate needs *sustained* growth (issue #4): a process that
    grew fast early and has since leveled off should stop reading as
    "actively leaking" even though its all-time average slope is still
    high. Fewer than `window` samples cannot yet prove a plateau, so this
    is conservatively False until there is enough recent history.
    """
    if len(samples) < window:
        return False
    return slope_mb_per_min(samples[-window:]) < plateau_slope_mb_per_min


def leak_state(samples, latched):
    """One transparent, pure detector step.

    Returns (slope_mb_per_min, plateaued, latched'). `latched` is the
    caller's leak_candidate flag carried in from the previous sample: once
    a run trips the candidate, it stays latched for the rest of the watch
    (PR #41 review: recomputing the flag every sample let it flip back to
    False after a later plateau, contradicting the documented "sustained
    growth ... flags" contract). A *new* trip requires MIN_LEAK_SAMPLES of
    history, an overall slope above LEAK_SLOPE_MB_PER_MIN, and a recent
    window that has not plateaued.
    """
    slope = slope_mb_per_min(samples)
    plateau = is_plateaued(samples)
    candidate_now = (len(samples) >= MIN_LEAK_SAMPLES
                      and slope > LEAK_SLOPE_MB_PER_MIN
                      and not plateau)
    return slope, plateau, latched or candidate_now


def sparkline(values):
    """A unicode sparkline of `values`, min->max mapped over 8 blocks."""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return _SPARK[0] * len(values)
    span = hi - lo
    return "".join(_SPARK[int((v - lo) / span * (len(_SPARK) - 1))] for v in values)


# ---------------------------------------------------------------------------
# process accessibility: gone (ESRCH) vs inaccessible-but-present (EPERM)
# ---------------------------------------------------------------------------

def pid_status(pid):
    """Classify a pid libproc could not read: 'gone', 'denied', or 'present'.

    os.kill(pid, 0) sends no signal; it only probes existence/permission,
    which is exactly how to tell "the process exited" (ESRCH) apart from
    "the process exists but I lack permission to see it" (EPERM) — PR #41
    review: proc_pid_rusage returning None conflated the two into one
    "not accessible" outcome, which made a plain nonexistent pid look like
    a permission error. 'present' covers the rare case where the kill(2)
    permission model allows the probe but libproc's still refuses (e.g. a
    protected system process) — treated the same as 'denied' by callers.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        return "denied"
    except OSError as exc:
        # Defensive: any other errno (e.g. EINVAL on a bad pid) is neither
        # "gone" nor "denied" in a way callers should paper over.
        if exc.errno == errno.ESRCH:
            return "gone"
        if exc.errno == errno.EPERM:
            return "denied"
        raise
    return "present"


# ---------------------------------------------------------------------------
# presentation — top
# ---------------------------------------------------------------------------

def _top_document(rows, sysmem, limit):
    partial, reasons = _visibility()
    if not sysmem.get("available", True):
        partial = True
        reasons = reasons + ["system_memory_probe"]
    return schema.document(
        "memory", "top", partial=partial, partial_reasons=reasons,
        system=sysmem,
        processes=[
            {
                "pid": pid,
                "name": name,
                "footprint_bytes": footprint,
                "resident_size_bytes": resident,
            }
            for footprint, resident, pid, name in rows[:limit]
        ])


def top_result(limit):
    """Collect one memory snapshot and return its document and exit code."""
    rows = rank_footprint(snapshot_footprint())
    return _top_document(rows, system_memory(), limit), cli.EXIT_OK


def _top_frame(rows, sysmem, interval, limit, styled=True):
    clear = CLEAR if styled else ""
    bold = BOLD if styled else ""
    dim = DIM if styled else ""
    reset = RESET if styled else ""
    out = [clear]
    out.append(bold + "stethoscope memory · per-process footprint · %s · refresh %.0fs" %
               (time.strftime("%H:%M:%S"), interval) + reset)
    out.append(dim + "system: used %s / %s  ·  wired %s  ·  compressed %s  ·  pressure %s"
               % (human(sysmem.get("used")), human(sysmem.get("total")),
                  human(sysmem.get("wired")), human(sysmem.get("compressed")),
                  sysmem.get("pressure", "unknown")) + reset)
    out.append("")
    out.append(bold + "%7s  %-30s %12s %12s" %
               ("PID", "COMMAND", "FOOTPRINT", "RESIDENT") + reset)
    if not rows:
        out.append(dim + "  (no accessible processes — try sudo)" + reset)
    for footprint, resident, pid, name in rows[:limit]:
        out.append("%7d  %-30s %12s %12s" %
                   (pid, name[:30], human(footprint), human(resident)))
    return "\n".join(out) + "\n"


def cmd_top(options):
    """Per-process memory footprint, ranked. Honors --json/--once/--duration."""
    if not options.json:
        warn_if_not_root()
    started = time.monotonic()

    while True:
        document, exit_code = top_result(options.limit)
        if options.json:
            cli.emit_json(document)
        else:
            rows = [
                (item["footprint_bytes"], item["resident_size_bytes"],
                 item["pid"], item["name"])
                for item in document["processes"]
            ]
            sys.stdout.write(_top_frame(
                rows, document["system"], options.interval, options.limit,
                styled=sys.stdout.isatty()))
            sys.stdout.flush()
        now = time.monotonic()
        if options.once or (
                options.duration is not None and now - started >= options.duration):
            return exit_code
        time.sleep(options.interval)


# ---------------------------------------------------------------------------
# presentation — watch
# ---------------------------------------------------------------------------

def _watch_document(pid, name, footprint, resident, slope, plateau, leak,
                     samples, running):
    partial, reasons = _visibility()
    return schema.document(
        "memory", "watch", partial=partial, partial_reasons=reasons,
        pid=pid,
        name=name,
        running=running,
        footprint_bytes=footprint,
        resident_size_bytes=resident,
        slope_mb_per_min=slope,
        plateaued=plateau,
        leak_candidate=leak,
        samples=samples)


def _watch_frame(pid, name, footprint, resident, slope, leak, window,
                  interval, styled=True):
    clear = CLEAR if styled else ""
    bold = BOLD if styled else ""
    dim = DIM if styled else ""
    reset = RESET if styled else ""
    out = [clear]
    out.append(bold + "stethoscope memory watch · pid %d (%s) · %s · every %.0fs" %
               (pid, name, time.strftime("%H:%M:%S"), interval) + reset)
    sign = "+" if slope >= 0 else ""
    verdict = (bold + "LEAK CANDIDATE" + reset) if leak else "steady"
    out.append("footprint %s   resident %s   slope %s%.2f MB/min   %s" %
               (human(footprint), human(resident), sign, slope, verdict))
    out.append(dim + "trend " + sparkline(window) + reset)
    out.append("")
    out.append(dim + "(sustained positive slope with no plateau = leak; ctrl-c to quit)"
               + reset)
    return "\n".join(out) + "\n"


def _exited_frame(pid, name, leak):
    verdict = " (was flagged as a leak candidate)" if leak else ""
    return DIM + "process %d (%s) exited%s.\n" % (pid, name, verdict) + RESET


def cmd_watch(pid, options):
    """Sample one process's footprint over time; report slope + leak candidacy."""
    if not options.json:
        warn_if_not_root()

    identity = proc_identity(pid)
    if identity is None:
        status = pid_status(pid)
        if status == "gone":
            sys.stderr.write("memory watch: no such process: %d\n" % pid)
            return cli.EXIT_USAGE
        sys.stderr.write(
            "memory watch: pid %d exists but is not accessible (try sudo)\n" % pid)
        return cli.EXIT_PERMISSION

    name = proc_name(pid, identity)
    samples = []          # [(t_seconds, footprint_bytes), ...]
    leak = False
    t0 = time.monotonic()
    started = t0

    while True:
        info = rusage._raw_rusage(pid)
        now = time.monotonic()
        current = (pid, info.ri_proc_start_abstime) if info is not None else None
        if current != identity:
            # Gone: either the process exited, or the pid was reused by an
            # unrelated process between samples (S10) — both mean "this
            # watch target no longer exists" and must not silently adopt
            # the new identity's counters.
            if options.json:
                cli.emit_json(_watch_document(
                    pid, name, None, None, None, None, leak,
                    len(samples), running=False))
            else:
                sys.stdout.write(_exited_frame(pid, name, leak))
                sys.stdout.flush()
            return cli.EXIT_FINDINGS if leak else cli.EXIT_OK

        footprint = info.ri_phys_footprint
        resident = info.ri_resident_size
        samples.append((now - t0, footprint))
        slope, plateau, leak = leak_state(samples, leak)

        if options.json:
            cli.emit_json(_watch_document(
                pid, name, footprint, resident, slope, plateau, leak,
                len(samples), running=True))
        else:
            window = [y for _, y in samples[-60:]]
            sys.stdout.write(_watch_frame(
                pid, name, footprint, resident, slope, leak, window,
                options.interval, styled=sys.stdout.isatty()))
            sys.stdout.flush()

        if options.once or (
                options.duration is not None and now - started >= options.duration):
            return cli.EXIT_FINDINGS if leak else cli.EXIT_OK
        time.sleep(options.interval)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

USAGE = """stethoscope memory — per-process footprint and leak watching

  memory [top] [--interval N] [--limit N] [--once | --duration N] [--json]
  memory watch <pid> [--interval N] [--once | --duration N] [--json]

top ranks accessible processes by ri_phys_footprint (Activity Monitor's
"Memory" column) and shows resident size alongside it, plus a system
summary (used/wired/compressed vs total, kernel pressure level).

watch samples one process's footprint, reports a least-squares slope
(MB/min) and a sparkline, and latches leak_candidate once sustained
positive growth with no plateau is observed.

Exit codes: 0 ok · 1 leak candidate (watch) · 2 usage · 3 needs root/permission

Run under sudo to see all processes:  sudo ./stethoscope memory top
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
        if mode == "top":
            cli.require_positionals(options, mode, 0)
            return cmd_top(options)
        if mode == "watch":
            cli.require_options(
                options, mode, {"json", "once", "duration", "interval"})
            pid_arg = cli.require_positionals(options, mode, 1)[0]
            try:
                pid = int(pid_arg)
            except ValueError:
                raise cli.OptionsError("watch: not a pid: %r" % pid_arg)
            if pid <= 0:
                raise cli.OptionsError("watch: pid must be > 0")
            return cmd_watch(pid, options)
    except cli.OptionsError as exc:
        sys.stderr.write("%s\n" % exc)
        return cli.EXIT_USAGE

    sys.stderr.write("unknown mode: %s\n\n%s" % (mode, USAGE))
    return cli.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main(sys.argv))
