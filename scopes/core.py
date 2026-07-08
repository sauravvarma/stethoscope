#!/usr/bin/env python3
"""
stethoscope core — the shared sampling spine every scope is built on.

macOS exposes per-process accounting through libproc's `proc_pid_rusage()`:
one cheap syscall returns a pid's cumulative CPU time, idle/interrupt wakeups,
memory footprint and disk bytes — the same numbers Activity Monitor shows, and
they survive SIP. The disk scope polls-and-diffs these; cpu, memory and battery
will do the same over different fields. This module owns that machinery — the
ctypes bindings, the rusage snapshot, pid/name resolution and shared
formatting — so each scope in scopes/<name>.py stays a thin data layer over one
common spine (see issue #1).

No third-party dependencies — system Python 3 + ctypes only.
"""

import ctypes
import os
from collections import namedtuple

# ---------------------------------------------------------------------------
# libproc bindings
# ---------------------------------------------------------------------------

_libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)

PROC_ALL_PIDS = 1
RUSAGE_INFO_V2 = 2
PROC_PIDPATHINFO_MAXSIZE = 4 * 1024

# Declare the ABI of every symbol we bind. Without argtypes ctypes assumes C
# ints and silently truncates any 64-bit pointer passed as a Python int to 32
# bits — a latent footgun as more scopes reuse these bindings.
_libc.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_void_p, ctypes.c_int]
_libc.proc_listpids.restype = ctypes.c_int
_libc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
_libc.proc_pid_rusage.restype = ctypes.c_int
_libc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
_libc.proc_pidpath.restype = ctypes.c_int


class RUsageInfo(ctypes.Structure):
    """Prefix of rusage_info_v2 — every field cpu/memory/battery/disk read."""
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64),
        ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64),
        ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
    ]


# A scope-agnostic view of the fields we actually consume. Times are in
# mach-absolute ticks, wakeups are counts, sizes/bytes are bytes.
RUsage = namedtuple("RUsage", [
    "read", "write",                  # cumulative disk bytes  (disk)
    "user_time", "system_time",       # cumulative CPU time    (cpu, battery)
    "idle_wkups", "interrupt_wkups",  # wakeups                (cpu, battery)
    "footprint", "resident",          # memory                 (memory)
    "start",                          # start abstime (identity / uptime)
])


def list_pids():
    """Every pid on the system, via proc_listpids(PROC_ALL_PIDS)."""
    # First call with a NULL buffer to learn the required size.
    needed = _libc.proc_listpids(PROC_ALL_PIDS, 0, None, 0)
    if needed <= 0:
        return []
    # Over-allocate a little; the pid table can grow between the two calls.
    count = needed // ctypes.sizeof(ctypes.c_int32) + 32
    buf = (ctypes.c_int32 * count)()
    got = _libc.proc_listpids(PROC_ALL_PIDS, 0, buf, ctypes.sizeof(buf))
    if got <= 0:
        return []
    n = got // ctypes.sizeof(ctypes.c_int32)
    return [buf[i] for i in range(n) if buf[i] != 0]


def proc_rusage(pid):
    """An RUsage for pid, or None if inaccessible (not owner / gone).

    One proc_pid_rusage() syscall — the shared read every scope diffs.
    """
    info = RUsageInfo()
    rc = _libc.proc_pid_rusage(pid, RUSAGE_INFO_V2, ctypes.byref(info))
    if rc != 0:
        return None
    return RUsage(
        read=info.ri_diskio_bytesread, write=info.ri_diskio_byteswritten,
        user_time=info.ri_user_time, system_time=info.ri_system_time,
        idle_wkups=info.ri_pkg_idle_wkups,
        interrupt_wkups=info.ri_interrupt_wkups,
        footprint=info.ri_phys_footprint, resident=info.ri_resident_size,
        start=info.ri_proc_start_abstime,
    )


def snapshot_rusage(pids=None):
    """{pid: RUsage} for every accessible process — the shared sampling step.

    Scopes call this once per interval and diff the fields they care about
    (disk bytes, CPU time, wakeups, footprint) between snapshots.
    """
    snap = {}
    for pid in (list_pids() if pids is None else pids):
        ru = proc_rusage(pid)
        if ru is not None:
            snap[pid] = ru
    return snap


_name_cache = {}   # pid -> (start_abstime, name)


def _proc_start_abstime(pid):
    """Start time of pid (mach-abs), 0 if inaccessible.

    (pid, start_abstime) uniquely identifies a process, so it is what lets the
    name cache notice PID reuse.
    """
    ru = proc_rusage(pid)
    return ru.start if ru else 0


def proc_name(pid):
    """Best-effort short command name for pid.

    Cached on (pid, start_time) so that when the kernel reuses a pid for a new
    process the stale name is dropped and re-resolved — otherwise long-running
    `top`/TUI sessions mislabel reused pids.
    """
    start = _proc_start_abstime(pid)
    cached = _name_cache.get(pid)
    if cached is not None and cached[0] == start:
        return cached[1]
    buf = ctypes.create_string_buffer(PROC_PIDPATHINFO_MAXSIZE)
    n = _libc.proc_pidpath(pid, buf, PROC_PIDPATHINFO_MAXSIZE)
    name = os.path.basename(buf.value.decode("utf-8", "replace")) if n > 0 else "?"
    _name_cache[pid] = (start, name)
    return name


# ---------------------------------------------------------------------------
# formatting + chrome shared by every scope's presentation layer
# ---------------------------------------------------------------------------

def human(n):
    """Human-readable bytes."""
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024.0:
            return "%d%s" % (int(n), unit) if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fP" % n


def rate(n):
    return human(n) + "/s"


CLEAR = "\033[2J\033[H"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def is_root():
    return os.geteuid() == 0
