"""
stethoscope core.rusage — the libproc / proc_pid_rusage probe.

The kernel keeps a per-process ledger of cumulative counters — CPU time,
wakeups, disk bytes, memory footprint, billed energy — readable via
`proc_pid_rusage()`. This module owns that binding for every scope
(disk and cpu today, battery next), read at flavor RUSAGE_INFO_V4, plus
RUSAGE_INFO_V6 where available for ri_energy_nj — the energy ledger that
moves at polling cadence, unlike ri_billed_energy (casebook 0001.10).

Two contracts this module exists to enforce (Appendix A of ARCHITECTURE.md,
findings S9 and S2):

  * The struct is declared IN FULL. proc_pid_rusage copies
    sizeof(rusage_info_v4) bytes for flavor 4 regardless of what the caller
    allocated — a prefix struct is heap corruption, not an optimization.
    35 uint64 fields + a 16-byte uuid = 296 bytes; sizeof-asserted below and
    verified against the live SDK header by `python3 -m core.validate`.

  * Time fields (ri_user_time, ri_system_time, the QoS times,
    ri_runnable_time, ri_proc_start_abstime) are mach-abstime TICKS, not
    nanoseconds. On Apple Silicon the timebase is 125/3 — a 41.7x error if
    read raw (Intel is 1/1, which is how the bug hides). Conversion happens
    HERE, once; callers of the public API never see raw ticks.

No third-party dependencies — system Python 3 + ctypes only.
"""

import ctypes
import os
import time

_libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)

PROC_ALL_PIDS = 1
RUSAGE_INFO_V4 = 4
RUSAGE_INFO_V6 = 6
PROC_PIDPATHINFO_MAXSIZE = 4 * 1024

# Declared signatures: without argtypes/restype, ctypes defaults every
# argument to int-sized — pointers truncate on 64-bit and mistakes pass
# silently instead of raising ArgumentError.
_libc.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_void_p, ctypes.c_int]
_libc.proc_listpids.restype = ctypes.c_int
_libc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
_libc.proc_pid_rusage.restype = ctypes.c_int
_libc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
_libc.proc_pidpath.restype = ctypes.c_int


class RUsageInfoV4(ctypes.Structure):
    """struct rusage_info_v4 — complete, matching the SDK's sys/resource.h.

    Never truncate this: the kernel writes sizeof(rusage_info_v4) bytes
    for flavor 4 (S9). Field list derived from the header on this machine.
    """
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
        ("ri_cpu_time_qos_default", ctypes.c_uint64),
        ("ri_cpu_time_qos_maintenance", ctypes.c_uint64),
        ("ri_cpu_time_qos_background", ctypes.c_uint64),
        ("ri_cpu_time_qos_utility", ctypes.c_uint64),
        ("ri_cpu_time_qos_legacy", ctypes.c_uint64),
        ("ri_cpu_time_qos_user_initiated", ctypes.c_uint64),
        ("ri_cpu_time_qos_user_interactive", ctypes.c_uint64),
        ("ri_billed_system_time", ctypes.c_uint64),
        ("ri_serviced_system_time", ctypes.c_uint64),
        ("ri_logical_writes", ctypes.c_uint64),
        ("ri_lifetime_max_phys_footprint", ctypes.c_uint64),
        ("ri_instructions", ctypes.c_uint64),
        ("ri_cycles", ctypes.c_uint64),
        ("ri_billed_energy", ctypes.c_uint64),
        ("ri_serviced_energy", ctypes.c_uint64),
        ("ri_interval_max_phys_footprint", ctypes.c_uint64),
        ("ri_runnable_time", ctypes.c_uint64),
    ]


# 16-byte uuid + 35 x uint64 = 296. If this trips, the field list above has
# drifted from the header — fix the list, never the assert.
assert ctypes.sizeof(RUsageInfoV4) == 296


class RUsageInfoV6(ctypes.Structure):
    """struct rusage_info_v6 — complete, a strict superset of v4 (verified
    against the SDK header by `python3 -m core.validate`).

    Read for ri_energy_nj / ri_penergy_nj: the per-process energy ledger
    that actually moves at polling cadence — measured 10/10 nonzero deltas
    at 1 s where ri_billed_energy (V4) stays lazily folded and frozen
    (casebook 0001.10). Never truncate this either (S9): the kernel writes
    sizeof(rusage_info_v6) bytes for flavor 6, reserved tail included.
    """
    _fields_ = list(RUsageInfoV4._fields_) + [
        ("ri_flags", ctypes.c_uint64),
        ("ri_user_ptime", ctypes.c_uint64),
        ("ri_system_ptime", ctypes.c_uint64),
        ("ri_pinstructions", ctypes.c_uint64),
        ("ri_pcycles", ctypes.c_uint64),
        ("ri_energy_nj", ctypes.c_uint64),
        ("ri_penergy_nj", ctypes.c_uint64),
        ("ri_secure_time_in_system", ctypes.c_uint64),
        ("ri_secure_ptime_in_system", ctypes.c_uint64),
        ("ri_neural_footprint", ctypes.c_uint64),
        ("ri_lifetime_max_neural_footprint", ctypes.c_uint64),
        ("ri_interval_max_neural_footprint", ctypes.c_uint64),
        ("ri_reserved", ctypes.c_uint64 * 9),
    ]


# 16-byte uuid + (35 + 12) x uint64 + 9 reserved uint64 = 464.
assert ctypes.sizeof(RUsageInfoV6) == 464


# ---------------------------------------------------------------------------
# mach timebase — ticks -> nanoseconds (S2)
# ---------------------------------------------------------------------------

class _MachTimebaseInfo(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


_tb = _MachTimebaseInfo()
_libc.mach_timebase_info(ctypes.byref(_tb))
TIMEBASE_NUMER = _tb.numer
TIMEBASE_DENOM = _tb.denom

_libc.mach_absolute_time.restype = ctypes.c_uint64


def mach_absolute_time():
    """Current mach-abstime tick count (raw; convert via ticks_to_ns)."""
    return _libc.mach_absolute_time()


def ticks_to_ns(t):
    """mach-abstime ticks -> nanoseconds. 125/3 on Apple Silicon, 1/1 Intel."""
    return t * TIMEBASE_NUMER // TIMEBASE_DENOM


def _ticks_to_s(t):
    return ticks_to_ns(t) / 1e9


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def list_pids():
    """Return a list of all pids via proc_listpids(PROC_ALL_PIDS)."""
    # First call with NULL buffer to learn the required size.
    needed = _libc.proc_listpids(PROC_ALL_PIDS, 0, None, 0)
    if needed <= 0:
        return []
    count = needed // ctypes.sizeof(ctypes.c_int32)
    # Over-allocate a little; the pid table can grow between the two calls.
    count += 32
    buf = (ctypes.c_int32 * count)()
    got = _libc.proc_listpids(PROC_ALL_PIDS, 0, buf, ctypes.sizeof(buf))
    if got <= 0:
        return []
    n = got // ctypes.sizeof(ctypes.c_int32)
    return [buf[i] for i in range(n) if buf[i] != 0]


def _raw_rusage(pid):
    """Raw RUsageInfoV4 for pid, or None on EPERM/ESRCH."""
    info = RUsageInfoV4()
    rc = _libc.proc_pid_rusage(ctypes.c_int(pid), ctypes.c_int(RUSAGE_INFO_V4),
                               ctypes.byref(info))
    if rc != 0:
        return None
    return info


def _raw_rusage_v6(pid):
    """Raw RUsageInfoV6 for pid, or None on EPERM/ESRCH/flavor-unsupported."""
    info = RUsageInfoV6()
    rc = _libc.proc_pid_rusage(ctypes.c_int(pid), ctypes.c_int(RUSAGE_INFO_V6),
                               ctypes.byref(info))
    if rc != 0:
        return None
    return info


# Flavor 6 availability, probed once against our own pid. Where absent
# (older macOS), energy reads degrade to None and callers must render the
# absence, never a zero (casebook 0001.11).
HAS_V6 = _raw_rusage_v6(os.getpid()) is not None


def rusage(pid):
    """Converted vitals-ready counters for pid, or None if inaccessible.

    All time fields are seconds (timebase-converted); energy is nanojoules;
    sizes are bytes. start_time_epoch is Unix time derived from
    ri_proc_start_abstime against mach_absolute_time now. energy_nj is the
    live flavor-6 ledger, None where flavor 6 is unavailable;
    billed_energy_nj is kept for the slow cross-check tier only — its
    deltas are frozen at polling cadence (casebook 0001.10).
    """
    info = _raw_rusage_v6(pid) if HAS_V6 else _raw_rusage(pid)
    if info is None:
        return None
    now_ticks = mach_absolute_time()
    return {
        "cpu_user_s": _ticks_to_s(info.ri_user_time),
        "cpu_system_s": _ticks_to_s(info.ri_system_time),
        "pkg_idle_wakeups": info.ri_pkg_idle_wkups,
        "interrupt_wakeups": info.ri_interrupt_wkups,
        "diskio_bytes_read": info.ri_diskio_bytesread,
        "diskio_bytes_written": info.ri_diskio_byteswritten,
        "phys_footprint_bytes": info.ri_phys_footprint,
        "billed_energy_nj": info.ri_billed_energy,
        "energy_nj": info.ri_energy_nj if HAS_V6 else None,
        "qos_time_s": {
            "default": _ticks_to_s(info.ri_cpu_time_qos_default),
            "maintenance": _ticks_to_s(info.ri_cpu_time_qos_maintenance),
            "background": _ticks_to_s(info.ri_cpu_time_qos_background),
            "utility": _ticks_to_s(info.ri_cpu_time_qos_utility),
            "legacy": _ticks_to_s(info.ri_cpu_time_qos_legacy),
            "user_initiated": _ticks_to_s(info.ri_cpu_time_qos_user_initiated),
            "user_interactive": _ticks_to_s(info.ri_cpu_time_qos_user_interactive),
        },
        "billed_system_s": _ticks_to_s(info.ri_billed_system_time),
        "serviced_system_s": _ticks_to_s(info.ri_serviced_system_time),
        "runnable_s": _ticks_to_s(info.ri_runnable_time),
        "start_time_epoch": time.time()
                            - _ticks_to_s(now_ticks - info.ri_proc_start_abstime),
    }


def proc_cpu_sample(pid):
    """One cpu-scope poll: (identity, user_ns, system_ns, energy_nj,
    pkg_idle_wakeups, interrupt_wakeups) or None.

    identity is (pid, ri_proc_start_abstime raw ticks) — the opaque
    pid-reuse key (S10). user_ns / system_ns are timebase-converted
    cumulative CPU time. energy_nj is the lifetime ri_energy_nj ledger
    from flavor 6 — the energy field whose deltas move at polling cadence,
    unlike ri_billed_energy (casebook 0001.10) — or None where flavor 6
    is unavailable. pkg_idle_wakeups / interrupt_wakeups are the cumulative
    ri_pkg_idle_wkups / ri_interrupt_wkups counters — two distinct vitals
    that are never summed here (S8, casebook 0004): a canonical sleep-loop
    storm measures zero pkg-idle wakeups while holding ~800 interrupt
    wakeups/s, so callers must diff and keep them apart, not fold them into
    one number before a baseline-relative detector ever sees them.
    """
    if HAS_V6:
        info = _raw_rusage_v6(pid)
        if info is None:
            return None
        return ((pid, info.ri_proc_start_abstime),
                ticks_to_ns(info.ri_user_time),
                ticks_to_ns(info.ri_system_time),
                info.ri_energy_nj,
                info.ri_pkg_idle_wkups,
                info.ri_interrupt_wkups)
    info = _raw_rusage(pid)
    if info is None:
        return None
    return ((pid, info.ri_proc_start_abstime),
            ticks_to_ns(info.ri_user_time),
            ticks_to_ns(info.ri_system_time),
            None,
            info.ri_pkg_idle_wkups,
            info.ri_interrupt_wkups)


def proc_diskio(pid):
    """Return (bytes_read, bytes_written) cumulative for pid, or None if inaccessible."""
    info = _raw_rusage(pid)
    if info is None:
        return None
    return (info.ri_diskio_bytesread, info.ri_diskio_byteswritten)


def proc_power_sample(pid):
    """One battery-scope poll: identity + every cumulative counter its
    energy_score needs (CPU time, pkg-idle/interrupt wakeups, the flavor-6
    energy ledger, disk bytes), from a single struct read.

    A narrow addition for the battery scope (case 0009): `proc_cpu_sample`
    already reads this same struct but does not surface
    ri_diskio_bytesread/byteswritten, and pmenergy's Energy Impact formula
    needs them alongside CPU time and pkg-idle wakeups. Re-reading via a
    second `_raw_rusage` call per pid per poll (as a naive `proc_diskio`
    call alongside `proc_cpu_sample` would) doubles the syscall count for
    every accessible process every interval; this reads the struct once.

    Returns None if inaccessible. Otherwise a dict: "identity" is the
    (pid, ri_proc_start_abstime) pid-reuse-safe key (S10); "cpu_user_ns" /
    "cpu_system_ns" are timebase-converted; "energy_nj" is the flavor-6
    ledger (case 0001.10/0001.11) or None where flavor 6 is unavailable —
    never a fabricated zero; "pkg_idle_wakeups" / "interrupt_wakeups" are
    the two counters kept apart (S8, casebook 0004); "diskio_bytes_read" /
    "diskio_bytes_written" are cumulative bytes; footprint/resident are the
    current byte gauges from the same struct; "qos_cpu_ns" retains the
    seven cumulative CPU ledgers needed by pmenergy's QoS-specific weights.
    """
    info = _raw_rusage_v6(pid) if HAS_V6 else _raw_rusage(pid)
    if info is None:
        return None
    return {
        "identity": (pid, info.ri_proc_start_abstime),
        "cpu_user_ns": ticks_to_ns(info.ri_user_time),
        "cpu_system_ns": ticks_to_ns(info.ri_system_time),
        "qos_cpu_ns": {
            "default": ticks_to_ns(info.ri_cpu_time_qos_default),
            "maintenance": ticks_to_ns(info.ri_cpu_time_qos_maintenance),
            "background": ticks_to_ns(info.ri_cpu_time_qos_background),
            "utility": ticks_to_ns(info.ri_cpu_time_qos_utility),
            "legacy": ticks_to_ns(info.ri_cpu_time_qos_legacy),
            "user_initiated": ticks_to_ns(
                info.ri_cpu_time_qos_user_initiated),
            "user_interactive": ticks_to_ns(
                info.ri_cpu_time_qos_user_interactive),
        },
        "energy_nj": info.ri_energy_nj if HAS_V6 else None,
        "pkg_idle_wakeups": info.ri_pkg_idle_wkups,
        "interrupt_wakeups": info.ri_interrupt_wkups,
        "diskio_bytes_read": info.ri_diskio_bytesread,
        "diskio_bytes_written": info.ri_diskio_byteswritten,
        "phys_footprint_bytes": info.ri_phys_footprint,
        "resident_size_bytes": info.ri_resident_size,
    }


def proc_identity(pid):
    """(pid, start_abstime_ticks) — the sample identity that survives pid reuse.

    Raw ticks on purpose: this is an opaque key, not a duration. None if
    inaccessible. (S10)
    """
    info = _raw_rusage(pid)
    if info is None:
        return None
    return (pid, info.ri_proc_start_abstime)


_name_cache = {}


def proc_name(pid, identity=None):
    """Best-effort short command name, cached by process identity.

    A bare pid is not stable: macOS reuses it after a process exits. Callers
    that already sampled ``(pid, start_abstime)`` can pass that identity and
    avoid another rusage read.
    """
    identity = identity if identity is not None else proc_identity(pid)
    cache_key = identity if identity is not None else (pid, None)
    cached = _name_cache.get(cache_key)
    if cached is not None:
        return cached
    buf = ctypes.create_string_buffer(PROC_PIDPATHINFO_MAXSIZE)
    n = _libc.proc_pidpath(ctypes.c_int(pid), buf, PROC_PIDPATHINFO_MAXSIZE)
    if n > 0:
        name = os.path.basename(buf.value.decode("utf-8", "replace"))
    else:
        name = "?"
    for key in [key for key in _name_cache if key[0] == pid and key != cache_key]:
        del _name_cache[key]
    _name_cache[cache_key] = name
    return name
