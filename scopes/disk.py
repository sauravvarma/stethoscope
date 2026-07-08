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

try:
    from scopes import core
except ImportError:   # invoked with scopes/ directly on sys.path
    import core

# ---------------------------------------------------------------------------
# shared spine — the sampling machinery lives in core.py (see #1) so cpu,
# memory and battery reuse it. Re-export the names the CLI, the TUI (disk_tui)
# and the tests reach for as disk.<name>.
# ---------------------------------------------------------------------------

list_pids = core.list_pids
proc_name = core.proc_name
proc_rusage = core.proc_rusage
human = core.human
rate = core.rate
CLEAR, BOLD, DIM, RESET = core.CLEAR, core.BOLD, core.DIM, core.RESET


def proc_diskio(pid):
    """(bytes_read, bytes_written) cumulative for pid, or None if inaccessible."""
    ru = core.proc_rusage(pid)
    return (ru.read, ru.write) if ru else None


def warn_if_not_root():
    if not core.is_root():
        sys.stderr.write(
            DIM + "note: not running as root — I/O for other users' processes is "
            "hidden and fs_usage will not run. Re-run with sudo for full coverage.\n"
            + RESET)


# ---------------------------------------------------------------------------
# data layer — shared by the CLI (cmd_*) and the TUI (disk_tui.py)
# ---------------------------------------------------------------------------

def snapshot_diskio():
    """{pid: (bytes_read, bytes_written)} for every accessible process."""
    return {pid: (ru.read, ru.write)
            for pid, ru in core.snapshot_rusage().items()}


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
    for pid, (r, w) in cur.items():
        pr, pw = prev.get(pid, (r, w))
        dr = max(0, r - pr) / dt
        dw = max(0, w - pw) / dt
        sys_dr += dr
        sys_dw += dw
        if dr > 0 or dw > 0:
            rows.append((dr + dw, dr, dw, r, w, pid, proc_name(pid)))
    rows.sort(reverse=True)
    return rows, sys_dr, sys_dw


# ---------------------------------------------------------------------------
# mode: top
# ---------------------------------------------------------------------------

def cmd_top(interval=1.0, limit=20):
    """Live per-process disk I/O, ranked by throughput."""
    warn_if_not_root()
    # Prime one sample so the first frame shows rates, not cumulative.
    prev = snapshot_diskio()
    prev_t = time.time()
    time.sleep(interval)

    while True:
        cur = snapshot_diskio()
        now = time.time()
        rows, sys_dr, sys_dw = rank_io(prev, cur, now - prev_t)
        out = [CLEAR]
        out.append(BOLD + "stethoscope disk · per-process disk I/O · %s · refresh %.0fs" %
                   (time.strftime("%H:%M:%S"), interval) + RESET)
        out.append(DIM + "system: read %s  write %s   (ctrl-c to quit)" %
                   (rate(sys_dr), rate(sys_dw)) + RESET)
        out.append("")
        out.append(BOLD + "%7s  %-24s %10s %10s %10s %10s" %
                   ("PID", "COMMAND", "READ/s", "WRITE/s", "RD TOTAL", "WR TOTAL")
                   + RESET)
        if not rows:
            out.append(DIM + "  (no disk I/O this interval)" + RESET)
        for _, dr, dw, r, w, pid, name in rows[:limit]:
            out.append("%7d  %-24s %10s %10s %10s %10s" %
                       (pid, name[:24], rate(dr), rate(dw), human(r), human(w)))
        sys.stdout.write("\n".join(out) + "\n")
        sys.stdout.flush()

        prev = cur
        prev_t = now
        time.sleep(interval)


# ---------------------------------------------------------------------------
# mode: inspect (why — live syscall trace via fs_usage)
# ---------------------------------------------------------------------------

def cmd_inspect(pid):
    """Live syscall-level file I/O for one pid, plus cumulative totals."""
    if os.geteuid() != 0:
        sys.stderr.write("inspect needs root (fs_usage). Re-run: sudo %s inspect %d\n"
                         % (sys.argv[0], pid))
        return 1

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
    return 0


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


def cmd_holds(pid):
    """Show open file descriptors (files/dirs the process is holding)."""
    name = proc_name(pid)
    print(BOLD + "stethoscope disk holds · pid %d (%s)" % (pid, name) + RESET)
    io = proc_diskio(pid)
    if io:
        print(DIM + "cumulative disk I/O: read %s / written %s"
              % (human(io[0]), human(io[1])) + RESET)
    print()
    try:
        items = open_files(pid)
    except Exception as e:
        print("lsof failed: %s" % e)
        return 1
    if not items:
        print(DIM + "(no on-disk files held, or permission denied — try sudo)" + RESET)
        return 0
    print(BOLD + "%-18s %-5s %s" % ("HOLD", "TYPE", "PATH") + RESET)
    for reason, typ, name in items:
        print("%-18s %-5s %s" % (reason, typ, name))
    return 0


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
        elif arg.startswith("disk") and (dev == dev_arg or dev.startswith(dev_arg + "s")):  # whole disk -> all slices
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


def cmd_busy(arg):
    """Reverse lookup: which processes hold files open on a volume/disk."""
    targets = resolve_volume(arg)
    if not targets:
        sys.stderr.write("no mounted volume/device matches %r.\n"
                         "mounted volumes: %s\n"
                         % (arg, ", ".join(sorted(mp for _, mp in _mount_table()
                                                  if mp.startswith("/Volumes/")))))
        return 2

    if os.geteuid() != 0:
        sys.stderr.write(DIM + "note: not root — holders owned by other users / system "
                         "daemons (mds, fseventsd) are hidden. Re-run with sudo for the "
                         "full picture.\n" + RESET)

    label = ", ".join("%s (%s)" % (mp, dev) for dev, mp in targets)
    print(BOLD + "stethoscope disk busy · %s" % label + RESET)

    procs = collect_holders(targets)
    if not procs:
        print(DIM + "  no processes are holding this volume — it should eject cleanly."
              + RESET)
        return 0

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
    return 0


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

USAGE = """stethoscope disk — per-process disk I/O visibility for macOS

  disk [top] [--interval N] [--limit N]   who is doing disk I/O now (default)
  disk inspect <pid>                       why — live syscall trace (needs sudo)
  disk holds <pid>                         what files a process holds open
  disk busy <volume|device>                which pids pin a disk (reverse lookup)
  disk tui                                 full-screen interactive view (sudo -E)

Run under sudo to see all processes / all holders:  sudo ./stethoscope disk top
Examples:  sudo ./stethoscope disk busy "/Volumes/X9 Pro"    sudo ./stethoscope disk busy disk6
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
    if mode in ("inspect", "holds"):
        if not args:
            sys.stderr.write("%s needs a pid\n" % mode)
            return 2
        pid = int(args[0])
        return cmd_inspect(pid) if mode == "inspect" else cmd_holds(pid)
    if mode == "busy":
        if not args:
            sys.stderr.write("busy needs a volume path, name, or device "
                             "(e.g. '/Volumes/X9 Pro', 'X9 Pro', disk6)\n")
            return 2
        return cmd_busy(args[0])

    sys.stderr.write("unknown mode: %s\n\n%s" % (mode, USAGE))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
