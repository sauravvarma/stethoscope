"""
stethoscope core.ioaccel: the IORegistry GPU probe: the accelerator's own
statistics, every process holding a device client, and the P-state table.

Three `ioreg` reads, each 10-20 ms, no root (casebook 0008.4, 0008.11):

  * `ioreg -c IOAccelerator -r -l -d 2 -a`: one call returns the
    accelerator node (PerformanceStatistics: Device/Renderer/Tiler
    Utilization %, In use / Alloc system memory, recoveryCount) AND its
    children, the per-process device user clients. Each child carries
    `IOUserClientCreator` = "pid N, name", `CommandQueueCount`, and
    `AppUsage`: a list of {API, accumulatedGPUTime, lastSubmittedTime}
    per command queue. accumulatedGPUTime is cumulative nanoseconds of GPU
    time and moves at 1 s cadence for live clients (WindowServer measured
    at 111.7 ms/s while compositing); lastSubmittedTime is nanoseconds on
    the mach awake-time clock, so "idle for" is awake-time like every other
    age in stethoscope (casebook 0003.7).

    This is the per-process GPU attribution powermetrics needs root for.
    Its limit is that it attributes what live clients ACCRUE; a device
    context whose owner has exited keeps the device busy without accruing
    to any client. The gap between device utilization and the sum of
    per-process shares is therefore a vital in its own right (the
    `unattributed` line in `gpu top`), and it is what a leaked context
    looks like (casebook 0008.3).

  * `ioreg -rn pmgr -a`: the power manager's `voltage-states9` table:
    (frequency Hz, voltage) pairs indexed by GPU performance state, OFF
    first. On Mac17,9 that is [0, 338, 486, ..., 1578, 1620] MHz, exactly
    the bins powermetrics prints. Read once.

Everything returns structures; nothing prints. Missing nodes return None or
empty containers so the scope can render "-" rather than a fabricated zero.

No third-party dependencies: system Python 3 + plistlib only.
"""

import plistlib
import re
import struct
import subprocess

IOREG = "/usr/sbin/ioreg"
CREATOR_RE = re.compile(r"^pid (\d+), (.*)$")
GPU_VOLTAGE_STATES_KEY = "voltage-states9"


def _ioreg_plist(args):
    """Run ioreg with `-a` and return the parsed plist (a list of matched
    entries), or [] on any failure."""
    try:
        out = subprocess.run([IOREG] + args + ["-a"], capture_output=True,
                             timeout=10).stdout
        return plistlib.loads(out) if out.strip() else []
    except Exception:
        return []


def parse_creator(s):
    """'pid 5671, GTA5_Enhanced.ex' -> (5671, 'GTA5_Enhanced.ex'); None if
    the string is not in that shape."""
    m = CREATOR_RE.match(s or "")
    return (int(m.group(1)), m.group(2)) if m else None


def _device(node):
    ps = node.get("PerformanceStatistics") or {}
    return {
        "class": node.get("IOClass"),
        "model": node.get("model"),
        "cores": node.get("gpu-core-count"),
        "util_pct": ps.get("Device Utilization %"),
        "renderer_pct": ps.get("Renderer Utilization %"),
        "tiler_pct": ps.get("Tiler Utilization %"),
        "mem_inuse": ps.get("In use system memory"),
        "mem_alloc": ps.get("Alloc system memory"),
        "restarts": ps.get("recoveryCount"),
    }


def _merge_clients(children, into):
    """Fold the user-client children of one accelerator into `into`, keyed
    by pid. A process may hold several clients; sum them."""
    for child in children:
        parsed = parse_creator(child.get("IOUserClientCreator"))
        if not parsed:
            continue
        pid, name = parsed
        usage = child.get("AppUsage") or []
        entry = into.setdefault(pid, {"name": name, "clients": 0, "queues": 0,
                                      "gpu_ns": 0, "last_submit_ns": 0})
        entry["clients"] += 1
        entry["queues"] += child.get("CommandQueueCount", 0) or 0
        entry["gpu_ns"] += sum(u.get("accumulatedGPUTime", 0) or 0 for u in usage)
        entry["last_submit_ns"] = max([entry["last_submit_ns"]] +
                                      [u.get("lastSubmittedTime", 0) or 0 for u in usage])


def gpu_snapshot():
    """One ioreg call. Returns None if the machine has no IOAccelerator
    node; otherwise {"devices": [device, ...], "clients": {pid: {...}}}.
    """
    nodes = _ioreg_plist(["-c", "IOAccelerator", "-r", "-l", "-d", "2"])
    if not nodes:
        return None
    devices, clients = [], {}
    for node in nodes:
        devices.append(_device(node))
        _merge_clients(node.get("IORegistryEntryChildren") or [], clients)
    return {"devices": devices, "clients": clients}


def unpack_voltage_states(blob):
    """pmgr voltage-states blob -> [frequency_hz, ...] (little-endian u32
    pairs of (freq, voltage)). Malformed input -> []."""
    if not blob or len(blob) % 8:
        return []
    vals = struct.unpack("<%dI" % (len(blob) // 4), blob)
    return list(vals[0::2])


def gpu_pstate_mhz(key=GPU_VOLTAGE_STATES_KEY):
    """[MHz by performance-state index], OFF (0) first. [] if pmgr or the
    key is absent, in which case the scope labels states P1..Pn instead."""
    nodes = _ioreg_plist(["-rn", "pmgr"])
    if not nodes:
        return []
    hz = unpack_voltage_states(nodes[0].get(key))
    return [f / 1e6 for f in hz]
