#!/usr/bin/env python3
"""
stethoscope gpu: the GPU scope.

Answers the question no other scope could on 2026-09-05 (casebook 0008):
the fans are pinned, CPU / memory / disk / network all read quiet, WHAT is
the GPU doing and WHO is doing it. Two probes, both root-free:

  * core.ioreport: the kernel's own GPU performance-state residency (the
    24 MHz tick clock behind powermetrics' "HW active residency") and the
    cumulative GPU energy counter. Together: active %, weighted frequency,
    watts. Degrades to "-" where the private framework is unavailable.

  * core.ioaccel: the IORegistry accelerator node (Device/Renderer/Tiler
    utilization, GPU memory) and every process holding a device client,
    with cumulative GPU time per process. Δ(accumulatedGPUTime) / interval
    is GPU%, the per-process share of the device this interval.

The one number this scope adds that neither probe prints on its own is
**unattributed**: device-active minus the sum of live-process shares. On a
healthy machine it is small (kernel/driver work). On 2026-09-05 it read
~86%: a context whose owner had exited was holding the GPU at 40 W with
no live process accruing time. The owner still appeared in the client
table (40 command queues, 79 min of GPU time, 0% this interval) with the
kernel's own status for it: exited. That row plus that line is the whole
diagnosis (casebook 0008.11).

Per-process names come from libproc, falling back to the name the kernel
recorded when the client was created, so an exited owner is still named.

No third-party dependencies: system Python 3 + ctypes + plistlib only.
"""

import os
import sys
import time
import signal

try:
    from core import rusage, ioaccel, ioreport     # via the stethoscope dispatcher
except ImportError:                                # run directly: ./scopes/gpu.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core import rusage, ioaccel, ioreport


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------

CLEAR = "\033[2J\033[H"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def dur_str(ns):
    """89h12m / 12m05s / 3.2s, from nanoseconds."""
    if ns is None:
        return "-"
    s = ns / 1e9
    if s >= 3600:
        return "%dh%02dm" % (s // 3600, s % 3600 // 60)
    if s >= 60:
        return "%dm%02ds" % (s // 60, s % 60)
    return "%.1fs" % s


def bytes_str(n):
    if n is None:
        return "-"
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return "%.1f%s" % (n, unit) if unit != "B" else "%dB" % n
        n /= 1024.0


def pct_str(x, digits=0):
    return "-" if x is None else ("%%.%df%%%%" % digits) % x


def opt_str(fmt, x):
    return "-" if x is None else fmt % x


# ---------------------------------------------------------------------------
# data layer: shared by the CLI and (future) TUI / --json
# ---------------------------------------------------------------------------

STATE_LABEL = {rusage.EXITED: "exited", rusage.GONE: "gone"}   # alive/unknown render blank


def snapshot_gpu(reporter):
    """One poll of both probes.

    {"t": wall seconds, "mach_ns": awake-time ns, "acc": ioaccel.gpu_snapshot()
     or None, "ior": opaque IOReport sample or None}. The caller releases
    "ior" via reporter.release() once it has been diffed.
    """
    acc = ioaccel.gpu_snapshot()
    ior = reporter.sample() if reporter else None
    # Clock read AFTER the registry read so no client's lastSubmittedTime
    # can postdate it (an idle age must never be negative).
    return {"t": time.time(),
            "mach_ns": rusage.ticks_to_ns(rusage.mach_absolute_time()),
            "acc": acc, "ior": ior}


def rank_clients(prev_clients, cur_clients, dt, mach_ns):
    """Diff two client tables over `dt` seconds into ranked per-process rows.

    Each row: (gpu_pct, gpu_ns_total, queues, idle_ns, pid, name, state)
    sorted by gpu_pct desc then lifetime GPU time desc. Identity is
    (pid, creator-name): a reused pid with a different image baselines
    fresh rather than inheriting the dead client's counter (S10).

    Rows are kept for any process that used the GPU this interval, holds a
    command queue, or has exited while still holding a client: the last
    being exactly the row that diagnoses a leaked context. Idle clients
    with no queues (agents that merely opened the device) are dropped.
    """
    dt = dt or 1.0
    dt_ns = dt * 1e9
    rows = []
    attributed = 0.0
    for pid, cur in cur_clients.items():
        key = (pid, cur["name"])
        prev = prev_clients.get(pid)
        if prev is None or prev["name"] != cur["name"]:
            prev_ns = cur["gpu_ns"]                   # first sight: no rate yet
        else:
            prev_ns = prev["gpu_ns"]
        d = max(0, cur["gpu_ns"] - prev_ns)
        gpu_pct = d / dt_ns * 100.0
        attributed += gpu_pct
        state = STATE_LABEL.get(rusage.proc_state(pid), "")
        if not (gpu_pct > 0 or cur["queues"] > 0 or state):
            continue
        name = rusage.proc_name(pid)
        if name == "?" or not name:
            name = cur["name"]
        idle_ns = max(0, mach_ns - cur["last_submit_ns"]) if cur["last_submit_ns"] else None
        rows.append((gpu_pct, cur["gpu_ns"], cur["queues"], idle_ns, pid, name, state))
    rows.sort(key=lambda r: (-r[0], -r[1]))
    return rows, attributed


def gpu_vitals(prev, cur, reporter, pstate_mhz):
    """Reduce two snapshots to one vitals dict. Every value may be None.

    device:       first accelerator's statistics (util/renderer/tiler %,
                  memory, restarts, model, cores) or None if no GPU node
    active_pct:   1 - OFF residency, from IOReport        (None if unavailable)
    freq_mhz:     residency-weighted active frequency     (None if unmapped)
    watts:        Δ GPU energy / interval                 (None if unavailable)
    pstates:      [(label, share_pct)] for states with nonzero residency
    rows:         rank_clients() rows
    attributed_pct:   sum of per-process GPU% this interval
    unattributed_pct: busy - attributed, clamped at 0, where busy is
                      active_pct if IOReport is available else util_pct.
                      None if neither is known.
    dt:           the interval actually measured, seconds
    """
    dt = (cur["t"] - prev["t"]) or 1.0
    acc = cur["acc"]
    device = acc["devices"][0] if acc and acc["devices"] else None
    prev_clients = prev["acc"]["clients"] if prev["acc"] else {}
    cur_clients = acc["clients"] if acc else {}
    rows, attributed = rank_clients(prev_clients, cur_clients, dt, cur["mach_ns"])

    delta = reporter.delta(prev["ior"], cur["ior"]) if reporter else None
    active = ioreport.active_fraction(delta)
    active_pct = None if active is None else active * 100.0
    pstates = []
    if delta and delta["ticks"]:
        for name, r in delta["states"]:
            if r and name != "OFF":
                mhz = ioreport.state_mhz(name, pstate_mhz)
                label = "%dMHz" % mhz if mhz else name
                pstates.append((label, r / delta["ticks"] * 100.0))

    busy = active_pct if active_pct is not None else (device["util_pct"] if device else None)
    unattributed = None if busy is None else max(0.0, busy - attributed)
    return {
        "device": device,
        "active_pct": active_pct,
        "freq_mhz": ioreport.active_frequency_mhz(delta, pstate_mhz),
        "watts": ioreport.watts(delta, dt),
        "pstates": pstates,
        "rows": rows,
        "attributed_pct": attributed,
        "unattributed_pct": unattributed,
        "dt": dt,
    }


# ---------------------------------------------------------------------------
# mode: top
# ---------------------------------------------------------------------------

def render_top(v, interval, limit, reporter_reason=None):
    """The `gpu top` frame as a list of lines (pure; no I/O)."""
    dev = v["device"]
    out = [CLEAR]
    model = "%s (%s cores)" % (dev["model"], dev["cores"]) if dev and dev["model"] else "GPU"
    out.append(BOLD + "stethoscope gpu · %s · %s · refresh %.0fs" %
               (model, time.strftime("%H:%M:%S"), interval) + RESET)
    if dev is None:
        out.append(DIM + "  (no IOAccelerator node: no GPU visible to this probe)" + RESET)
        return out
    freq = opt_str("%.0f MHz", v["freq_mhz"])
    out.append(DIM + "device: %s busy (renderer %s · tiler %s)   active %s @ %s   %s   "
               "memory %s in use / %s alloc   (ctrl-c to quit)" %
               (pct_str(dev["util_pct"]), pct_str(dev["renderer_pct"]),
                pct_str(dev["tiler_pct"]), pct_str(v["active_pct"], 1), freq,
                opt_str("%.1f W", v["watts"]), bytes_str(dev["mem_inuse"]),
                bytes_str(dev["mem_alloc"])) + RESET)
    ps = "  ".join("%s %.0f%%" % (lbl, share) for lbl, share in v["pstates"] if share >= 0.5)
    attrib = "attributed to live processes %s   unattributed %s" % (
        pct_str(v["attributed_pct"], 1), pct_str(v["unattributed_pct"], 1))
    if v["unattributed_pct"] is not None and v["unattributed_pct"] >= 50:
        attrib = BOLD + attrib + RESET + DIM + "   (device busy with no live owner: a leaked context?)"
    out.append(DIM + attrib + ("   ·   " + ps if ps else "") + RESET)
    if reporter_reason and v["active_pct"] is None:
        out.append(DIM + "  IOReport unavailable (%s): active/frequency/watts read '-'" % reporter_reason + RESET)
    out.append("")
    out.append(BOLD + "%7s  %-24s %6s %9s %6s %9s  %s" %
               ("PID", "COMMAND", "GPU%", "GPU TIME", "QUEUES", "IDLE", "STATE") + RESET)
    if not v["rows"]:
        out.append(DIM + "  (no process used the GPU this interval)" + RESET)
    for gpu_pct, gpu_ns, queues, idle_ns, pid, name, state in v["rows"][:limit]:
        line = "%7d  %-24s %6.1f %9s %6d %9s  %s" % (
            pid, name[:24], gpu_pct, dur_str(gpu_ns), queues, dur_str(idle_ns), state)
        out.append(BOLD + line + RESET if state == "exited" else line)
    return out


def cmd_top(interval=1.0, limit=20):
    """Live GPU vitals plus per-process GPU time, ranked by GPU% this interval."""
    reporter = ioreport.GPUReporter()
    pstate_mhz = ioaccel.gpu_pstate_mhz()
    prev = snapshot_gpu(reporter)
    time.sleep(interval)
    try:
        while True:
            cur = snapshot_gpu(reporter)
            v = gpu_vitals(prev, cur, reporter, pstate_mhz)
            sys.stdout.write("\n".join(render_top(v, interval, limit,
                                                  None if reporter.available else reporter.reason)) + "\n")
            sys.stdout.flush()
            reporter.release(prev["ior"])
            prev = cur
            time.sleep(interval)
    finally:
        reporter.release(prev["ior"])


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

USAGE = """stethoscope gpu: GPU visibility for macOS, no root required

  gpu [top] [--interval N] [--limit N]   what the GPU is doing, and who (default)

Header: device busy % (renderer/tiler), active residency and weighted
frequency from the kernel's performance-state clock, live watts from the
GPU energy counter, GPU memory in use / allocated. Then the line that
matters: how much of the busy time is attributed to LIVE processes and how
much is not. A large unattributed share with an 'exited' row below it is a
context whose owner died without releasing it; only a reboot reclaims it.

Columns: GPU% (share of the device this interval), GPU TIME (lifetime),
QUEUES (command queues held), IDLE (awake-time since last submit), STATE
('exited' = the kernel still holds the client for a process that is gone).

Residency / frequency / watts read '-' where the IOReport framework is
unavailable; never a fabricated zero.
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
