#!/usr/bin/env python3
"""
stethoscope disk — the per-process disk I/O scope.

Answers three questions about disk I/O, from broad to narrow:

  top            WHO is doing disk I/O right now?   (rank processes by read/write per sec)
  inspect <pid>  WHY — what paths, reads vs writes, is it blocking?  (live syscall trace)
  holds <pid>    WHAT is a process holding open?    (open file descriptors)
  busy <volume>  WHICH pids are pinning a disk?     (reverse lookup — "why won't it eject")

Mechanism (see README for the full picture):

  * `proc_pid_rusage()` — the kernel tracks cumulative disk bytes read/written per
    process (ri_diskio_bytesread / ri_diskio_byteswritten). This is exactly what
    Activity Monitor reports. It survives SIP and needs no tracing framework. We
    poll it and diff to get rates. Reading OTHER users' processes needs root, so
    run under sudo for full-system coverage.

  * `fs_usage` — Apple's supported syscall-level tracer. Shows every filesystem
    operation with path, byte count, elapsed time, and a `W` marker when the call
    blocked (was scheduled off-CPU waiting on I/O). Needs root.

  * `lsof` — enumerates open file descriptors = the files a process is "holding".

We deliberately avoid DTrace (iosnoop/iotop): with SIP enabled on modern macOS its
io provider is unreliable, so it is not a dependable spine for this tool.

No third-party dependencies — system Python 3 + ctypes only.
"""

import os
import re
import sys
import time
import signal
import subprocess

# ---------------------------------------------------------------------------
# probe — libproc / rusage bindings live in core/rusage.py, shared by every
# scope. Re-exported here so the TUI's `d.<name>` references keep working.
# ---------------------------------------------------------------------------

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import cli, rusage, schema

list_pids = rusage.list_pids
proc_diskio = rusage.proc_diskio
proc_name = rusage.proc_name


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------

def human(n):
    """Human-readable bytes."""
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024.0:
            if unit == "B":
                return "%d%s" % (int(n), unit)
            return "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fP" % n


def rate(n):
    return human(n) + "/s"


CLEAR = "\033[2J\033[H"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def warn_if_not_root():
    if not cli.is_root():
        sys.stderr.write(
            DIM + "note: not running as root — I/O for other users' processes is "
            "hidden and fs_usage will not run. Re-run with sudo for full coverage.\n"
            + RESET)


# ---------------------------------------------------------------------------
# data layer — shared by the CLI (cmd_*) and the TUI (diskscope_tui.py)
# ---------------------------------------------------------------------------

def snapshot_diskio():
    """Return {(pid, start_abstime): (bytes_read, bytes_written)} for every
    accessible process. Keyed on process identity, not bare pid, so a reused
    pid cannot inherit a dead process's counters between snapshots.
    Callers treat the keys as opaque; rank_io unpacks the pid.
    """
    snap = {}
    for pid in list_pids():
        info = rusage._raw_rusage(pid)
        if info is not None:
            snap[(pid, info.ri_proc_start_abstime)] = (
                info.ri_diskio_bytesread, info.ri_diskio_byteswritten)
    return snap


def rank_io(prev, cur, dt):
    """Diff two snapshots over `dt` seconds into ranked activity.

    Returns (rows, sys_read_rate, sys_write_rate) where each row is
    (total_rate, read_rate, write_rate, read_total, write_total, pid, name),
    sorted by total_rate descending. Only processes with I/O this interval
    appear in rows; the system rates sum across all processes.
    """
    rows = []
    sys_dr = sys_dw = 0.0
    dt = dt or 1.0
    for key, (r, w) in cur.items():
        pid = key[0]
        pr, pw = prev.get(key, (r, w))
        dr = max(0, r - pr) / dt
        dw = max(0, w - pw) / dt
        sys_dr += dr
        sys_dw += dw
        if dr > 0 or dw > 0:
            rows.append((dr + dw, dr, dw, r, w, pid, proc_name(pid, key)))
    rows.sort(reverse=True)
    return rows, sys_dr, sys_dw


# ---------------------------------------------------------------------------
# mode: top
# ---------------------------------------------------------------------------

def _visibility():
    partial = not cli.is_root()
    return partial, ["not_root"] if partial else []


def _top_document(rows, sys_dr, sys_dw, limit):
    partial, reasons = _visibility()
    return schema.document(
        "disk", "top", partial=partial, partial_reasons=reasons,
        system={"read_per_s": sys_dr, "write_per_s": sys_dw},
        processes=[
            {
                "pid": pid,
                "name": name,
                "read_per_s": dr,
                "write_per_s": dw,
                "read_total": read_total,
                "write_total": write_total,
            }
            for _, dr, dw, read_total, write_total, pid, name in rows[:limit]
        ])


def _top_frame(rows, sys_dr, sys_dw, interval, limit, styled=True):
    clear = CLEAR if styled else ""
    bold = BOLD if styled else ""
    dim = DIM if styled else ""
    reset = RESET if styled else ""
    out = [clear]
    out.append(bold + "stethoscope disk · per-process disk I/O · %s · refresh %.0fs" %
               (time.strftime("%H:%M:%S"), interval) + reset)
    out.append(dim + "system: read %s  write %s   (ctrl-c to quit)" %
               (rate(sys_dr), rate(sys_dw)) + reset)
    out.append("")
    out.append(bold + "%7s  %-24s %10s %10s %10s %10s" %
               ("PID", "COMMAND", "READ/s", "WRITE/s", "RD TOTAL", "WR TOTAL")
               + reset)
    if not rows:
        out.append(dim + "  (no disk I/O this interval)" + reset)
    for _, dr, dw, read_total, write_total, pid, name in rows[:limit]:
        out.append("%7d  %-24s %10s %10s %10s %10s" %
                   (pid, name[:24], rate(dr), rate(dw),
                    human(read_total), human(write_total)))
    return "\n".join(out) + "\n"


def cmd_top(options):
    """Live per-process disk I/O, ranked by throughput."""
    if not options.json:
        warn_if_not_root()
    # Prime one sample so the first frame shows rates, not cumulative.
    prev = snapshot_diskio()
    prev_t = time.monotonic()
    started = prev_t

    while True:
        time.sleep(options.interval)
        cur = snapshot_diskio()
        now = time.monotonic()
        rows, sys_dr, sys_dw = rank_io(prev, cur, now - prev_t)
        if options.json:
            cli.emit_json(_top_document(rows, sys_dr, sys_dw, options.limit))
        else:
            sys.stdout.write(_top_frame(
                rows, sys_dr, sys_dw, options.interval, options.limit,
                styled=sys.stdout.isatty()))
            sys.stdout.flush()
        if options.once or (
                options.duration is not None and now - started >= options.duration):
            return cli.EXIT_OK
        prev, prev_t = cur, now


# ---------------------------------------------------------------------------
# mode: inspect (why — live syscall trace via fs_usage)
# ---------------------------------------------------------------------------

def cmd_inspect(pid):
    """Live syscall-level file I/O for one pid, plus cumulative totals."""
    if not cli.is_root():
        sys.stderr.write("inspect needs root (fs_usage). Re-run: sudo %s inspect %d\n"
                         % (sys.argv[0], pid))
        return cli.EXIT_PERMISSION

    name = proc_name(pid)
    io = proc_diskio(pid)
    tot = ("read %s / written %s" % (human(io[0]), human(io[1]))) if io else "n/a"
    print(BOLD + "stethoscope disk inspect · pid %d (%s)" % (pid, name) + RESET)
    print(DIM + "cumulative disk I/O: %s" % tot + RESET)
    print(DIM + "live fs_usage — op, bytes, elapsed, path.  'W' = call blocked on I/O.  "
          "ctrl-c to quit." + RESET)
    print()

    # -f filesys narrows to filesystem syscalls; -w widens columns; -e excludes self.
    cmd = ["/usr/bin/fs_usage", "-w", "-f", "filesys", "-p", str(pid)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            bufsize=1, universal_newlines=True)
    try:
        for line in proc.stdout:
            print(line.rstrip())
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
    return cli.EXIT_OK


# ---------------------------------------------------------------------------
# mode: holds (what is held open)
# ---------------------------------------------------------------------------

def open_files(pid, disk_only=True):
    """Return [(reason, type, name), ...] of files a pid holds open.

    reason is the decoded FD role (cwd / mmap / open (read) / ...). With
    disk_only, keep only on-disk objects (regular files & directories) —
    the actual holds — dropping pipes, sockets, and char devices.
    Shared by cmd_holds and the TUI's holds popup.
    """
    cmd = ["/usr/sbin/lsof", "-nP", "-p", str(pid)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
    items = []
    for ln in out.splitlines()[1:]:   # skip header
        parts = ln.split(None, 8)
        if len(parts) < 9:
            continue
        _cmd, _pid, _user, fd, typ, _dev, _sz, _node, name = parts
        if disk_only and typ not in ("REG", "DIR"):
            continue
        items.append((_classify_fd(fd), typ, name))
    return items


def cmd_holds(pid, options):
    """Show open file descriptors (files/dirs the process is holding)."""
    name = proc_name(pid)
    io = proc_diskio(pid)
    try:
        items = open_files(pid)
    except (OSError, subprocess.SubprocessError) as exc:
        if options.json:
            partial, reasons = _visibility()
            cli.emit_json(schema.document(
                "disk", "holds", partial=partial, partial_reasons=reasons,
                pid=pid, name=name, cumulative=None, holds=[],
                error="lsof failed: %s" % exc))
        else:
            sys.stderr.write("lsof failed: %s\n" % exc)
        return cli.EXIT_ERROR

    if options.json:
        partial, reasons = _visibility()
        cli.emit_json(schema.document(
            "disk", "holds", partial=partial, partial_reasons=reasons,
            pid=pid,
            name=name,
            cumulative={"read": io[0], "write": io[1]} if io else None,
            holds=[{"reason": reason, "type": kind, "path": path}
                   for reason, kind, path in items],
            error=None))
        return cli.EXIT_OK

    print(BOLD + "stethoscope disk holds · pid %d (%s)" % (pid, name) + RESET)
    if io:
        print(DIM + "cumulative disk I/O: read %s / written %s"
              % (human(io[0]), human(io[1])) + RESET)
    print()
    if not items:
        print(DIM + "(no on-disk files held, or permission denied — try sudo)" + RESET)
        return cli.EXIT_OK
    print(BOLD + "%-18s %-5s %s" % ("HOLD", "TYPE", "PATH") + RESET)
    for reason, kind, path in items:
        print("%-18s %-5s %s" % (reason, kind, path))
    return cli.EXIT_OK


# ---------------------------------------------------------------------------
# mode: busy (reverse lookup — which pids are pinning a disk / volume)
# ---------------------------------------------------------------------------

def _mount_table():
    """Parse `mount` into a list of (device, mountpoint)."""
    out = subprocess.run(["/sbin/mount"], capture_output=True, text=True).stdout
    pairs = []
    for ln in out.splitlines():
        # form: "/dev/disk6s2 on /Volumes/X9 Pro (exfat, ...)"
        if " on " not in ln:
            continue
        dev, rest = ln.split(" on ", 1)
        mp = rest.rsplit(" (", 1)[0]
        pairs.append((dev.strip(), mp.strip()))
    return pairs


def resolve_volume(arg):
    """Map a user argument to a list of (device, mountpoint) targets.

    Accepts a mount path (/Volumes/X9 Pro), a volume name (X9 Pro),
    a device node (/dev/disk6s2, disk6s2), or a whole disk (disk6 -> all slices).
    """
    table = _mount_table()
    # Normalize a bare device name to /dev/ form for matching.
    dev_arg = arg
    if arg.startswith("disk"):
        dev_arg = "/dev/" + arg

    targets = []
    for dev, mp in table:
        if mp == arg or mp == "/Volumes/" + arg:          # exact mount path or volume name
            targets.append((dev, mp))
        elif dev == dev_arg:                               # exact device node
            targets.append((dev, mp))
        elif arg.startswith("disk") and dev.startswith(dev_arg + "s"):  # whole disk -> slices
            targets.append((dev, mp))
    # de-dup preserving order
    seen = set()
    uniq = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _classify_fd(fd):
    """Human reason for an lsof FD column value."""
    if fd == "cwd":
        return "working dir (cwd)"
    if fd == "rtd":
        return "root dir"
    if fd == "txt":
        return "executable/text"
    if fd == "mem":
        return "mmap"
    if fd and fd[0].isdigit():
        mode = fd[-1]
        return {"r": "open (read)", "w": "open (write)",
                "u": "open (read/write)"}.get(mode, "open fd")
    return fd or "?"


def collect_holders(targets):
    """For a list of (device, mountpoint) targets, return
    {pid: {"name","user","holds":[(reason, path), ...]}} — every process
    holding an open file on those filesystems. Shared by the CLI and the TUI.
    """
    procs = {}
    for dev, mp in targets:
        # Passing the mount point makes lsof list every open file on that filesystem.
        res = subprocess.run(["/usr/sbin/lsof", "-nP", mp],
                             capture_output=True, text=True)
        for ln in res.stdout.splitlines()[1:]:   # skip header
            parts = ln.split(None, 8)
            if len(parts) < 9:
                continue
            cmd, pid, user, fd, typ, _dev, _sz, _node, name = parts
            try:
                pid = int(pid)
            except ValueError:
                continue
            p = procs.setdefault(pid, {"name": cmd, "user": user, "holds": []})
            p["holds"].append((_classify_fd(fd), name))
    return procs


def _busy_holder(pid, process):
    reasons = {}
    for reason, _ in process["holds"]:
        reasons[reason] = reasons.get(reason, 0) + 1
    io = proc_diskio(pid)
    return {
        "pid": pid,
        "name": process["name"],
        "user": process["user"],
        "reasons": reasons,
        "paths": [path for _, path in process["holds"]],
        "io": {"read": io[0], "write": io[1]} if io else None,
    }


def cmd_busy(arg, options):
    """Reverse lookup: which processes hold files open on a volume/disk."""
    targets = resolve_volume(arg)
    if not targets:
        if options.json:
            partial, reasons = _visibility()
            cli.emit_json(schema.document(
                "disk", "busy", partial=partial, partial_reasons=reasons,
                target=arg, targets=[], holders=[],
                error="no mounted volume/device matches %r" % arg))
        else:
            sys.stderr.write("no mounted volume/device matches %r.\n"
                             "mounted volumes: %s\n"
                             % (arg, ", ".join(sorted(
                                 mp for _, mp in _mount_table()
                                 if mp.startswith("/Volumes/")))))
        return cli.EXIT_USAGE

    if not options.json and not cli.is_root():
        sys.stderr.write(DIM + "note: not root — holders owned by other users / system "
                         "daemons (mds, fseventsd) are hidden. Re-run with sudo for the "
                         "full picture.\n" + RESET)

    procs = collect_holders(targets)
    if options.json:
        partial, reasons = _visibility()
        cli.emit_json(schema.document(
            "disk", "busy", partial=partial, partial_reasons=reasons,
            target=arg,
            targets=[{"device": device, "mount": mount}
                     for device, mount in targets],
            holders=[
                _busy_holder(pid, procs[pid])
                for pid in sorted(
                    procs, key=lambda value: -len(procs[value]["holds"]))
            ],
            error=None))
        return cli.EXIT_FINDINGS if procs else cli.EXIT_OK

    label = ", ".join("%s (%s)" % (mp, dev) for dev, mp in targets)
    print(BOLD + "stethoscope disk busy · %s" % label + RESET)

    if not procs:
        print(DIM + "  no processes are holding this volume — it should eject cleanly."
              + RESET)
        return cli.EXIT_OK

    print(DIM + "%d process(es) holding it open:\n" % len(procs) + RESET)
    for pid in sorted(procs, key=lambda p: -len(procs[p]["holds"])):
        p = procs[pid]
        reasons = {}
        for reason, _ in p["holds"]:
            reasons[reason] = reasons.get(reason, 0) + 1
        reason_str = ", ".join("%s×%d" % (r, c) if c > 1 else r
                               for r, c in sorted(reasons.items(), key=lambda x: -x[1]))
        io = proc_diskio(pid)
        io_str = ("  ·  live I/O: read %s / written %s" % (human(io[0]), human(io[1]))) if io else ""
        print(BOLD + "  pid %-6d %-20s" % (pid, p["name"]) + RESET
              + DIM + " user=%s%s" % (p["user"], io_str) + RESET)
        print("    holding: %s" % reason_str)
        # show up to 3 example paths (skip bare mount-point/dir noise)
        examples = [n for _, n in p["holds"]][:3]
        for ex in examples:
            print(DIM + "      %s" % ex + RESET)
        print()

    dev0 = targets[0][0].replace("/dev/", "")
    whole_disk = re.match(r"(disk\d+)", dev0)
    whole_disk = whole_disk.group(1) if whole_disk else dev0
    print(DIM + "to force-eject: diskutil unmount force '%s'   (or 'diskutil unmountDisk %s')"
          % (targets[0][1], whole_disk) + RESET)
    print(DIM + "to release a holder, quit its app or:  kill <pid>" + RESET)
    return cli.EXIT_FINDINGS


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

USAGE = """stethoscope disk — per-process disk I/O visibility for macOS

  disk [top] [--interval N] [--limit N] [--once | --duration N] [--json]
  disk inspect <pid>                      live syscall trace (human, needs sudo)
  disk holds <pid> [--json]               files the process holds open
  disk busy <volume|device> [--json]      pids pinning a disk
  disk tui                                full-screen interactive view (sudo -E)

Run under sudo to see all processes / all holders:  sudo ./stethoscope disk top
Examples:  sudo ./stethoscope disk busy "/Volumes/X9 Pro"    sudo ./stethoscope disk busy disk6

Exit codes: 0 clean · 1 findings · 2 usage · 3 permission · 4 probe error
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
        if mode in ("inspect", "holds"):
            cli.require_options(
                options, mode, set() if mode == "inspect" else {"json"})
            pid_arg = cli.require_positionals(options, mode, 1)[0]
            try:
                pid = int(pid_arg)
            except ValueError:
                raise cli.OptionsError("%s: not a pid: %r" % (mode, pid_arg))
            if pid <= 0:
                raise cli.OptionsError("%s: pid must be > 0" % mode)
            return cmd_inspect(pid) if mode == "inspect" else cmd_holds(
                pid, options)
        if mode == "busy":
            cli.require_options(options, mode, {"json"})
            target = cli.require_positionals(options, mode, 1)[0]
            return cmd_busy(target, options)
    except cli.OptionsError as exc:
        sys.stderr.write("%s\n" % exc)
        return cli.EXIT_USAGE

    sys.stderr.write("unknown mode: %s\n\n%s" % (mode, USAGE))
    return cli.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main(sys.argv))
