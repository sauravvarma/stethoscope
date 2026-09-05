"""
stethoscope core.validate — the probe-validation harness (build-order step 0).

ARCHITECTURE.md's probe table (section 3) makes empirical claims about what
macOS exposes at polling timescales. This module checks every one of them on
the machine it runs on, because the failure modes are silent: a prefix struct
corrupts the heap only at flavor 4 (S9), raw ticks are only wrong on Apple
Silicon (S2), billed_energy is nonzero but frozen (S1). Run it on any new
hardware or OS before trusting the table:

    python3 -m core.validate

Prints one PASS/FAIL/INFO/SKIP line per check with the measured values.
Exits 0 if nothing FAILed. No sudo required; checks that root would deepen
say so and carry on.

No third-party dependencies — system Python 3 + ctypes only.
"""

import ctypes
import os
import plistlib
import re
import subprocess
import sys
import threading
import time

from core import rusage

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

_results = []


def report(status, name, detail):
    _results.append(status)
    tag = {"PASS": "PASS", "FAIL": "FAIL", "INFO": "INFO", "SKIP": "SKIP"}[status]
    style = BOLD if status == "FAIL" else ""
    print("%s%-4s%s  %-28s %s" % (style, tag, RESET, name, detail))


def sdk_header_path(rel="sys/resource.h"):
    try:
        sdk = subprocess.run(["/usr/bin/xcrun", "--show-sdk-path"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return None
    path = os.path.join(sdk, "usr/include", rel)
    return path if os.path.isfile(path) else None


_C_SIZES = {"uint8_t": 1, "int32_t": 4, "uint32_t": 4, "uid_t": 4, "gid_t": 4,
            "uint64_t": 8, "char": 1}


def parse_proc_bsdinfo_header(path):
    """[(field_name, byte_size)] of struct proc_bsdinfo from sys/proc_info.h.
    MAXCOMLEN is 16 (sys/param.h); the header uses it symbolically."""
    with open(path) as f:
        text = f.read()
    m = re.search(r"struct proc_bsdinfo \{(.*?)\};", text, re.S)
    if not m:
        return None
    fields = []
    for ln in m.group(1).splitlines():
        fm = re.match(r"\s*(\w+)\s+(\w+)(?:\[([^\]]+)\])?;", ln)
        if not fm or fm.group(1) not in _C_SIZES:
            continue
        count = 1
        if fm.group(3):
            count = eval(fm.group(3).replace("MAXCOMLEN", "16"), {}, {})   # "2 * MAXCOMLEN"
        fields.append((fm.group(2), _C_SIZES[fm.group(1)] * count))
    return fields


def parse_header_struct(path, version):
    """[(field_name, byte_size)] of struct rusage_info_vN from the SDK header."""
    with open(path) as f:
        text = f.read()
    m = re.search(r"struct rusage_info_v%d \{(.*?)\};" % version, text, re.S)
    if not m:
        return None
    fields = []
    for ln in m.group(1).splitlines():
        fm = re.match(r"\s*(uint8_t|uint64_t)\s+(\w+)(?:\[(\d+)\])?;", ln)
        if fm:
            unit = 1 if fm.group(1) == "uint8_t" else 8
            count = int(fm.group(3) or 1)
            fields.append((fm.group(2), unit * count))
    return fields


def parse_header_v4(path):
    """Field-name list of struct rusage_info_v4 straight from the SDK header."""
    parsed = parse_header_struct(path, 4)
    return [name for name, _ in parsed] if parsed is not None else None


def signed64(v):
    """Decode an ioreg unsigned rendering of a signed 64-bit value."""
    return v - (1 << 64) if v >= (1 << 63) else v


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def _check_struct_against_header(header, struct_cls, version, check_name):
    parsed = parse_header_struct(header, version)
    if parsed is None:
        report("FAIL", check_name,
               "could not parse rusage_info_v%d from %s" % (version, header))
        return
    ours = [name for name, _ in struct_cls._fields_]
    theirs = [name for name, _ in parsed]
    size = ctypes.sizeof(struct_cls)
    expected = sum(nbytes for _, nbytes in parsed)
    if ours == theirs and size == expected:
        report("PASS", check_name,
               "%d fields match SDK header, sizeof %d == %d" % (len(ours), size, expected))
    else:
        report("FAIL", check_name,
               "header %d fields / %d B vs ours %d fields / %d B"
               % (len(theirs), expected, len(ours), size))


def check_struct():
    header = sdk_header_path()
    if header:
        _check_struct_against_header(header, rusage.RUsageInfoV4, 4, "rusage struct")
        if rusage.HAS_V6:
            _check_struct_against_header(header, rusage.RUsageInfoV6, 6, "rusage struct v6")
        else:
            report("SKIP", "rusage struct v6",
                   "flavor 6 unavailable on this OS — energy_nj degrades to None")
    else:
        # No CLT: settle for the syscall accepting our struct and the trailing
        # field looking like a real duration.
        info = rusage._raw_rusage(os.getpid())
        if info is None:
            report("FAIL", "rusage struct", "no SDK header and proc_pid_rusage(V4) failed")
            return
        lifetime = rusage.mach_absolute_time() - info.ri_proc_start_abstime
        ok = 0 < info.ri_runnable_time < lifetime * 2
        report("PASS" if ok else "FAIL", "rusage struct",
               "no SDK header; V4 call ok, ri_runnable_time=%d (lifetime ticks %d)"
               % (info.ri_runnable_time, lifetime))


def _burn(seconds):
    end = time.perf_counter() + seconds
    x = 0
    while time.perf_counter() < end:
        x += 1
    return x


def check_timebase():
    n, d = rusage.TIMEBASE_NUMER, rusage.TIMEBASE_DENOM
    pid = os.getpid()
    before = rusage._raw_rusage(pid)
    t0 = time.perf_counter()
    _burn(0.5)
    wall = time.perf_counter() - t0
    after = rusage._raw_rusage(pid)
    dticks = (after.ri_user_time + after.ri_system_time
              - before.ri_user_time - before.ri_system_time)
    conv = rusage.ticks_to_ns(dticks) / 1e9
    raw = dticks / 1e9
    conv_ok = abs(conv - wall) / wall < 0.20
    if n == d:
        report("PASS" if conv_ok else "FAIL", "timebase",
               "numer/denom %d/%d (Intel-style 1:1); burn %.3fs -> converted %.3fs"
               % (n, d, wall, conv))
        return
    raw_wrong = abs(raw - wall) / wall > 0.20
    report("PASS" if (conv_ok and raw_wrong) else "FAIL", "timebase",
           "numer/denom %d/%d; burn %.3fs -> converted %.3fs, unconverted %.3fs (%.1fx off)"
           % (n, d, wall, conv, raw, wall / raw if raw else 0))


def check_energy_cadence():
    # Own pid (kept genuinely busy by a burner thread) plus the 3 busiest
    # accessible pids by lifetime CPU, sampled at 1 s for 10 s. Two verdicts
    # from one window: ri_billed_energy (V4, known-lazy ledger, S1) and
    # ri_energy_nj (V6, the live candidate — casebook 0001.10/0001.11).
    own = os.getpid()
    busy = []
    for pid in rusage.list_pids():
        info = rusage._raw_rusage(pid)
        if info is not None and pid != own:
            busy.append((info.ri_user_time + info.ri_system_time, pid))
    busy.sort(reverse=True)
    pids = [own] + [p for _, p in busy[:3]]

    raw = rusage._raw_rusage_v6 if rusage.HAS_V6 else rusage._raw_rusage
    fields = ["ri_billed_energy"] + (["ri_energy_nj"] if rusage.HAS_V6 else [])
    samples = {p: {f: [] for f in fields} for p in pids}
    burner = threading.Thread(target=_burn, args=(10.0,), daemon=True)
    burner.start()
    for _ in range(11):
        for p in pids:
            info = raw(p)
            for f in fields:
                samples[p][f].append(getattr(info, f) if info else None)
        time.sleep(1.0)

    def label(p):
        return "pid %d (%s)" % (p, rusage.proc_name(p) if p != own else "self, burning")

    def moved(field):
        out = {}
        for p in pids:
            vals = [v for v in samples[p][field] if v is not None]
            deltas = [b - a for a, b in zip(vals, vals[1:])]
            out[p] = (sum(1 for x in deltas if x != 0), len(deltas))
        return out

    billed = moved("ri_billed_energy")
    detail = "  ".join("%s: %d/%d nonzero deltas" % (label(p), m[0], m[1])
                       for p, m in billed.items())
    if any(m[0] for m in billed.values()):
        report("INFO", "billed_energy cadence", "MOVES at 1 s: " + detail)
    else:
        # Frozen means UNMEASURABLE at this cadence, not idle — a zero delta
        # from this ledger must never be read as "process is quiet". The
        # blind-agent review exonerated two 60%-CPU runaways off exactly
        # that misreading (casebook 0005.2).
        report("INFO", "billed_energy cadence",
               "FROZEN at 1 s — unusable as a live activity signal; "
               "frozen != idle (casebook 0001.10): " + detail)

    if not rusage.HAS_V6:
        report("SKIP", "energy_nj cadence",
               "rusage flavor 6 unavailable — no live per-process energy on this OS")
        return
    live = moved("ri_energy_nj")
    detail = "  ".join("%s: %d/%d nonzero deltas" % (label(p), m[0], m[1])
                       for p, m in live.items())
    own_nz, own_n = live[own]
    if own_n and own_nz >= own_n - 2:
        report("PASS", "energy_nj cadence",
               "MOVES at 1 s — live per-process power source (casebook 0001.11): " + detail)
    else:
        report("INFO", "energy_nj cadence",
               "did not track a full-core burn at 1 s — treat watts as unavailable "
               "on this machine: " + detail)


def check_wakeup_split():
    pid = os.getpid()
    before = rusage._raw_rusage(pid)

    def sleeper():
        end = time.perf_counter() + 2.0
        while time.perf_counter() < end:
            time.sleep(0.001)

    t = threading.Thread(target=sleeper)
    t.start()
    t.join()
    after = rusage._raw_rusage(pid)
    d_pkg = after.ri_pkg_idle_wkups - before.ri_pkg_idle_wkups
    d_int = after.ri_interrupt_wkups - before.ri_interrupt_wkups
    report("PASS" if d_int > d_pkg else "INFO", "wakeup split",
           "2 s of 1 ms sleeps: pkg-idle +%d vs interrupt +%d (%.0fx gap)"
           % (d_pkg, d_int, d_int / d_pkg if d_pkg else float(d_int)))


def check_battery():
    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-rn", "AppleSmartBattery", "-a"],
                             capture_output=True, timeout=10).stdout
        nodes = plistlib.loads(out) if out.strip() else []
    except Exception as e:
        report("FAIL", "AppleSmartBattery", "ioreg/plist failed: %s" % e)
        return
    if not nodes:
        report("SKIP", "AppleSmartBattery", "node absent (desktop) — battery vitals degrade to 'no battery'")
        return
    b = nodes[0]
    amps = signed64(b.get("InstantAmperage", 0))
    volts = b.get("Voltage", 0)
    watts = amps * volts / 1e6
    direction = ("charging" if amps > 0 else "discharging" if amps < 0 else "idle")
    sane = abs(amps) < 20000
    report("PASS" if sane else "FAIL", "AppleSmartBattery",
           "ExternalConnected=%s IsCharging=%s Voltage=%d mV InstantAmperage=%d mA "
           "-> battery flow %.2f W (%s)"
           % (b.get("ExternalConnected"), b.get("IsCharging"), volts, amps,
              abs(watts), direction))


def _timed(cmd):
    t0 = time.perf_counter()
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout
    return time.perf_counter() - t0, len(out.splitlines())


def check_pmset_cost():
    log_s, log_lines = _timed(["/usr/bin/pmset", "-g", "log"])
    ass_s, _ = _timed(["/usr/bin/pmset", "-g", "assertions"])
    io_s, _ = _timed(["/usr/sbin/ioreg", "-rn", "AppleSmartBattery"])
    report("INFO", "pmset log cost",
           "pmset -g log: %.2f s / %d lines   (assertions %.0f ms, ioreg %.0f ms)"
           % (log_s, log_lines, ass_s * 1000, io_s * 1000))


def check_pmenergy():
    pmdir = "/usr/share/pmenergy"
    try:
        plists = sorted(os.listdir(pmdir))
    except OSError:
        report("SKIP", "pmenergy", "%s absent" % pmdir)
        return
    board = subprocess.run(["/usr/sbin/sysctl", "-n", "hw.model"],
                           capture_output=True, text=True).stdout.strip()
    # The plists are keyed by Intel board-id (Mac-<hex>); Apple Silicon
    # hw.model (e.g. Mac17,9) never matches, so expect default.plist.
    match = next((p for p in plists if board and board in p), None)
    chosen = match or ("default.plist" if "default.plist" in plists else None)
    if not chosen:
        report("FAIL", "pmenergy", "no board match and no default.plist among %d plists" % len(plists))
        return
    with open(os.path.join(pmdir, chosen), "rb") as f:
        coeffs = plistlib.load(f)
    # Coefficients live one level down, under "energy_constants".
    coeffs = coeffs.get("energy_constants", coeffs)
    keys = sorted(coeffs)
    unsupplied = [k for k in keys if k.startswith("knetwork") or k == "kgpu_time"]
    report("INFO", "pmenergy",
           "board %s -> %s (%s); %d coefficient keys; rusage cannot supply: %s"
           % (board, chosen, "board match" if match else "fallback",
              len(keys), ", ".join(unsupplied) or "none"))
    print(DIM + "      keys: %s" % ", ".join(keys) + RESET)


def check_proc_bsdinfo():
    header = sdk_header_path("sys/proc_info.h")
    if not header:
        report("SKIP", "proc_bsdinfo struct", "no SDK header; sizeof asserted at import (136)")
        return
    parsed = parse_proc_bsdinfo_header(header)
    ours = [n for n, _ in rusage.ProcBSDInfo._fields_]
    theirs = [n for n, _ in parsed] if parsed else None
    size, expected = ctypes.sizeof(rusage.ProcBSDInfo), sum(s for _, s in parsed or [])
    if parsed and ours == theirs and size == expected:
        report("PASS", "proc_bsdinfo struct",
               "%d fields match SDK header, sizeof %d == %d" % (len(ours), size, expected))
    else:
        report("FAIL", "proc_bsdinfo struct",
               "header %s fields / %d B vs ours %d fields / %d B"
               % (len(theirs) if theirs else "?", expected, len(ours), size))


def check_proc_state():
    # A real zombie: fork, let it exit, do not reap. The kernel must refuse
    # bsdinfo (ESRCH) while rusage still answers (casebook 0008.11).
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    deadline = time.time() + 2.0
    while rusage.proc_state(pid) == rusage.ALIVE and time.time() < deadline:
        time.sleep(0.01)
    status, err = rusage.proc_status(pid)
    ru = rusage._raw_rusage(pid) is not None
    zombie_state = rusage.proc_state(pid)
    os.waitpid(pid, 0)
    after = rusage.proc_state(pid)
    ok = zombie_state == rusage.EXITED and after == rusage.GONE
    report("PASS" if ok else "FAIL", "proc_state",
           "unreaped child: bsdinfo status=%s errno=%d rusage=%s -> %s; after reap -> %s; "
           "pid 1 (launchd, not ours) -> %s"
           % (status, err, "OK" if ru else "None", zombie_state, after, rusage.proc_state(1)))


def check_gpu_registry():
    from core import ioaccel
    t0 = time.perf_counter()
    snap = ioaccel.gpu_snapshot()
    ms = (time.perf_counter() - t0) * 1000
    if snap is None:
        report("SKIP", "IOAccelerator", "no IOAccelerator node: gpu scope degrades to 'no GPU'")
        return
    dev = snap["devices"][0]
    have = [k for k in ("util_pct", "renderer_pct", "tiler_pct", "mem_inuse", "mem_alloc")
            if dev[k] is not None]
    clients = snap["clients"]
    with_queues = sum(1 for c in clients.values() if c["queues"])
    exited = [(pid, c["name"], c["queues"]) for pid, c in clients.items()
              if rusage.proc_state(pid) in (rusage.EXITED, rusage.GONE)]
    ok = len(have) == 5 and clients
    report("PASS" if ok else "FAIL", "IOAccelerator",
           "%s (%s cores) %s; util %s%%; %d device clients, %d holding queues; %.0f ms per snapshot"
           % (dev["model"], dev["cores"], dev["class"], dev["util_pct"], len(clients),
              with_queues, ms))
    if exited:
        print(DIM + "      clients whose owner has exited: %s" % exited + RESET)
    t0 = time.perf_counter()
    table = ioaccel.gpu_pstate_mhz()
    ms = (time.perf_counter() - t0) * 1000
    if table:
        report("PASS" if table[0] == 0 and table[1:] == sorted(table[1:]) else "FAIL",
               "pmgr GPU P-states", "%d states, OFF first: %s MHz; %.0f ms"
               % (len(table), [int(x) for x in table], ms))
    else:
        report("INFO", "pmgr GPU P-states", "voltage-states9 absent: states labelled P1..Pn")


def check_gpu_ioreport():
    from core import ioreport
    if not ioreport.AVAILABLE:
        report("SKIP", "IOReport GPU", "unavailable (%s): residency/MHz/watts read '-'"
               % ioreport.why_unavailable())
        return
    rep = ioreport.GPUReporter()
    if not rep.available:
        report("SKIP", "IOReport GPU", "no GPU channels (%s): residency/MHz/watts read '-'" % rep.reason)
        return
    from core import ioaccel
    table = ioaccel.gpu_pstate_mhz()
    # 3 x 1 s: the residency clock must sum to 24e6 ticks/s, and the energy
    # counter must move. Saturation (100% active on every sample) is a
    # legitimate reading, never a reason to distrust the probe (casebook 0008.4).
    errs, watts, active, mhz = [], [], [], []
    prev = rep.sample()
    for _ in range(3):
        t0 = time.perf_counter()
        time.sleep(1.0)
        cur = rep.sample()
        dt = time.perf_counter() - t0
        d = rep.delta(prev, cur)
        rep.release(prev)
        prev = cur
        errs.append(d["ticks"] / ioreport.TICKS_PER_S / dt - 1.0)
        watts.append(ioreport.watts(d, dt))
        active.append(ioreport.active_fraction(d) * 100)
        mhz.append(ioreport.active_frequency_mhz(d, table))
    rep.release(prev)
    clock_ok = max(abs(e) for e in errs) < 0.02
    energy_ok = all(w is not None for w in watts)
    report("PASS" if clock_ok else "FAIL", "IOReport residency clock",
           "24 MHz tick sum vs wall: %s (|err| < 2%% required)"
           % ", ".join("%+.2f%%" % (e * 100) for e in errs))
    report("PASS" if energy_ok else "INFO", "IOReport GPU energy",
           "GPU Energy nJ counter -> %s W; active %s; %s"
           % (", ".join("%.1f" % w for w in watts) if energy_ok else "absent",
              ", ".join("%.0f%%" % a for a in active),
              ", ".join("-" if m is None else "%.0f MHz" % m for m in mhz)))


def main():
    print(BOLD + "stethoscope core.validate · probe contracts on this machine" + RESET)
    print(DIM + "  %s · %s" % (
        subprocess.run(["/usr/sbin/sysctl", "-n", "hw.model"],
                       capture_output=True, text=True).stdout.strip(),
        subprocess.run(["/usr/bin/sw_vers", "-productVersion"],
                       capture_output=True, text=True).stdout.strip()) + RESET)
    print()
    check_struct()
    check_timebase()
    check_energy_cadence()
    check_wakeup_split()
    check_battery()
    check_pmset_cost()
    check_pmenergy()
    check_proc_bsdinfo()
    check_proc_state()
    check_gpu_registry()
    check_gpu_ioreport()
    print()
    fails = _results.count("FAIL")
    print(BOLD + "%d checks · %d FAIL" % (len(_results), fails) + RESET)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
