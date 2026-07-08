#!/usr/bin/env python3
"""
stethoscope battery — what is draining the battery, and how healthy is it.

  battery health        charge, cycle count, capacity vs design, condition
  battery top           who is drawing power now (energy-impact score)
  battery drainers      cumulative energy impact since you unplugged

`health` reads the battery gauge from `ioreg -rc AppleSmartBattery` (+ `pmset`
for the time estimate) — no sudo, exact numbers: state of charge, cycle count,
maximum capacity relative to design (the "battery health %"), temperature and
condition.

`top` / `drainers` attribute power to processes. macOS's real Energy Impact
model is not public, so stethoscope uses a transparent proxy: an energy score
of `CPU%` plus weighted idle/interrupt wakeups (wakeups cost energy even at low
CPU%). Under sudo `powermetrics` exposes the authoritative number; this score
is the no-dependency approximation. `top` scores the current interval;
`drainers` scores cumulative impact since the last unplug, using a small
baseline file so it survives across invocations (#8).

No third-party dependencies — system Python 3 + core.py.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time

try:
    from scopes import core, output
except ImportError:   # invoked with scopes/ directly on sys.path
    import core
    import output

# Heuristic energy weights (impact units per wakeup/sec). Not Apple's model —
# a transparent proxy; powermetrics under sudo is the authoritative source.
W_IDLE = 0.10
W_INTR = 0.05

_STATE_DIR = os.path.expanduser("~/Library/Application Support/stethoscope")
_BASELINE = os.path.join(_STATE_DIR, "battery_baseline.json")

_IOREG_KEYS = {
    "CurrentCapacity", "MaxCapacity", "DesignCapacity", "AppleRawMaxCapacity",
    "AppleRawCurrentCapacity", "NominalChargeCapacity", "CycleCount",
    "IsCharging", "ExternalConnected", "FullyCharged", "Temperature",
    "PermanentFailureStatus", "Serial", "BatteryInstalled",
}


# ---------------------------------------------------------------------------
# battery health (ioreg + pmset)
# ---------------------------------------------------------------------------

def _ioreg_battery():
    """Scalar fields from `ioreg -rc AppleSmartBattery` (whitelisted keys)."""
    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-rc", "AppleSmartBattery"],
                             capture_output=True, text=True).stdout
    except OSError:
        return {}
    d = {}
    for ln in out.splitlines():
        m = re.match(r'\s*"([A-Za-z0-9_]+)"\s*=\s*(.+?)\s*$', ln)
        if not m:
            continue
        k, v = m.group(1), m.group(2)
        if k not in _IOREG_KEYS:
            continue
        if v in ("Yes", "No"):
            d[k] = (v == "Yes")
        elif v.lstrip("-").isdigit():
            d[k] = int(v)
        elif len(v) >= 2 and v[0] == '"' and v[-1] == '"':
            d[k] = v[1:-1]
    return d


def _pmset_batt():
    """(charge_pct, state, time_remaining) from `pmset -g batt`."""
    try:
        out = subprocess.run(["/usr/bin/pmset", "-g", "batt"],
                             capture_output=True, text=True).stdout
    except OSError:
        return None, None, None
    pct = re.search(r"(\d+)%", out)
    state = re.search(r"%;\s*([\w ]+?);", out)
    trem = re.search(r"(\d+:\d+)\s+remaining", out)
    return (int(pct.group(1)) if pct else None,
            state.group(1).strip() if state else None,
            trem.group(1) if trem else None)


def battery_health():
    """A structured battery health readout. `present` is False with no battery."""
    d = _ioreg_battery()
    if not d.get("BatteryInstalled") and "CurrentCapacity" not in d:
        return {"present": False}

    design = d.get("DesignCapacity") or 0
    raw_max = d.get("AppleRawMaxCapacity") or d.get("NominalChargeCapacity") or 0
    health_pct = round(raw_max / design * 100, 1) if design else None
    failed = bool(d.get("PermanentFailureStatus"))
    condition = "Service Recommended" if (
        failed or (health_pct is not None and health_pct < 80)) else "Normal"
    temp = d.get("Temperature")
    charge_pct, state, time_rem = _pmset_batt()

    return {
        "present": True,
        "charge_pct": charge_pct if charge_pct is not None else d.get("CurrentCapacity"),
        "state": state,
        "time_remaining": time_rem,
        "cycle_count": d.get("CycleCount"),
        "health_pct": health_pct,
        "condition": condition,
        "design_capacity_mah": design,
        "max_capacity_mah": raw_max,
        "temperature_c": round(temp / 100.0, 1) if temp else None,
        "charging": d.get("IsCharging"),
        "external_connected": d.get("ExternalConnected"),
        "fully_charged": d.get("FullyCharged"),
        "serial": d.get("Serial"),
    }


def cmd_health(o):
    h = battery_health()
    if o.json:
        output.emit_json(output.document("battery", "health", **h))
        return output.EXIT_OK
    if not h.get("present"):
        print(core.DIM + "no battery detected (desktop Mac?)." + core.RESET)
        return output.EXIT_OK
    print(core.BOLD + "stethoscope battery health" + core.RESET)
    line = "charge %s%%" % h["charge_pct"]
    if h["state"]:
        line += "  ·  %s" % h["state"]
    if h["time_remaining"]:
        line += "  ·  %s remaining" % h["time_remaining"]
    print(line)
    cond = h["condition"]
    cond_str = (core.BOLD + cond + core.RESET) if cond != "Normal" else cond
    print("health %s%%  ·  %d cycles  ·  condition %s"
          % (h["health_pct"], h["cycle_count"], cond_str))
    print(core.DIM + "capacity %d / %d mAh (max / design)  ·  %s°C"
          % (h["max_capacity_mah"], h["design_capacity_mah"],
             h["temperature_c"]) + core.RESET)
    return output.EXIT_OK


# ---------------------------------------------------------------------------
# per-process energy attribution
# ---------------------------------------------------------------------------

def snapshot():
    return core.mach_absolute_time(), core.snapshot_rusage()


def _energy_score(cpu_pct, idle_ps, intr_ps):
    return cpu_pct + W_IDLE * idle_ps + W_INTR * intr_ps


def rank_energy(prev, cur, prev_mach, cur_mach, dt):
    """[(score, cpu_pct, idle_ps, intr_ps, pid, name)] by score desc."""
    rows = []
    dwall = (cur_mach - prev_mach) or 1
    dt = dt or 1.0
    for pid, ru in cur.items():
        pru = prev.get(pid)
        if pru is None:
            continue
        d_cpu = max(0, (ru.user_time + ru.system_time)
                    - (pru.user_time + pru.system_time))
        cpu_pct = d_cpu / dwall * 100.0
        idle_ps = max(0, ru.idle_wkups - pru.idle_wkups) / dt
        intr_ps = max(0, ru.interrupt_wkups - pru.interrupt_wkups) / dt
        score = _energy_score(cpu_pct, idle_ps, intr_ps)
        if score > 0:
            rows.append((score, cpu_pct, idle_ps, intr_ps, pid, core.proc_name(pid)))
    rows.sort(reverse=True)
    return rows


def _top_document(rows, limit):
    return output.document(
        "battery", "top",
        processes=[{"pid": pid, "name": name, "energy_score": round(score, 2),
                    "cpu_pct": cpu, "idle_wakeups_per_s": iw,
                    "interrupt_wakeups_per_s": tw}
                   for score, cpu, iw, tw, pid, name in rows[:limit]])


def _top_frame(rows, interval, limit):
    out = [core.CLEAR]
    out.append(core.BOLD + "stethoscope battery · energy impact (proxy) · %s · refresh %.0fs"
               % (time.strftime("%H:%M:%S"), interval) + core.RESET)
    out.append(core.DIM + "score = CPU%% + weighted wakeups (higher = more drain)   (ctrl-c to quit)"
               + core.RESET)
    out.append("")
    out.append(core.BOLD + "%7s  %-26s %8s %8s %10s"
               % ("PID", "COMMAND", "ENERGY", "CPU%", "WAKE/s") + core.RESET)
    if not rows:
        out.append(core.DIM + "  (no activity this interval)" + core.RESET)
    for score, cpu, iw, tw, pid, name in rows[:limit]:
        out.append("%7d  %-26s %8.1f %7.1f%% %10.1f"
                   % (pid, name[:26], score, cpu, iw + tw))
    return "\n".join(out) + "\n"


def cmd_top(o):
    """Live per-process energy-impact score. Honors --json/--once/--duration."""
    prev_mach, prev = snapshot()
    prev_t = time.time()
    time.sleep(o.interval)
    deadline = None if o.duration is None else time.time() + o.duration
    while True:
        cur_mach, cur = snapshot()
        now = time.time()
        rows = rank_energy(prev, cur, prev_mach, cur_mach, now - prev_t)
        prev, prev_mach, prev_t = cur, cur_mach, now
        if o.json:
            output.emit_json(_top_document(rows, o.limit))
        else:
            sys.stdout.write(_top_frame(rows, o.interval, o.limit))
            sys.stdout.flush()
        if o.once or (deadline is not None and time.time() >= deadline):
            break
        time.sleep(o.interval)
    return output.EXIT_OK


# ---------------------------------------------------------------------------
# drainers since unplug (baseline persisted across invocations)
# ---------------------------------------------------------------------------

def _load_baseline():
    try:
        with open(_BASELINE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_baseline(charge, external, snap):
    os.makedirs(_STATE_DIR, exist_ok=True)
    procs = {str(pid): [ru.user_time + ru.system_time, ru.idle_wkups,
                        ru.interrupt_wkups, ru.start]
             for pid, ru in snap.items()}
    with open(_BASELINE, "w") as f:
        json.dump({"time": time.time(), "charge": charge,
                   "external": bool(external), "procs": procs}, f)


def drainers_since_unplug(now_snap, charge, external, baseline):
    """Rank cumulative energy impact per process since the baseline snapshot.

    Returns (rows, meta) where rows are (score, cpu_seconds, d_idle, d_intr,
    pid, name) by score desc, and meta carries charge drop + elapsed seconds.
    """
    rows = []
    base = baseline["procs"]
    for pid, ru in now_snap.items():
        b = base.get(str(pid))
        if not b:
            continue
        base_cpu, base_idle, base_intr, base_start = b
        if base_start != ru.start:
            continue   # pid was reused since the baseline
        cpu_s = core.abstime_to_seconds(max(0, (ru.user_time + ru.system_time) - base_cpu))
        d_idle = max(0, ru.idle_wkups - base_idle)
        d_intr = max(0, ru.interrupt_wkups - base_intr)
        score = cpu_s + W_IDLE * d_idle + W_INTR * d_intr
        if score > 0:
            rows.append((score, cpu_s, d_idle, d_intr, pid, core.proc_name(pid)))
    rows.sort(reverse=True)
    meta = {"charge_drop": (baseline.get("charge") or 0) - (charge or 0),
            "elapsed_s": time.time() - baseline.get("time", time.time())}
    return rows, meta


def cmd_drainers(o):
    h = battery_health()
    external = h.get("external_connected")
    charge = h.get("charge_pct")
    snap = core.snapshot_rusage()
    baseline = _load_baseline()

    # (Re)set the baseline whenever we're on AC or don't have one yet: the
    # "since unplug" window only makes sense while running on battery.
    if external or baseline is None or baseline.get("external"):
        _save_baseline(charge, external, snap)
        if o.json:
            output.emit_json(output.document(
                "battery", "drainers", baseline_reset=True,
                on_ac=bool(external), charge_pct=charge, drainers=[]))
        else:
            why = "on AC power" if external else "no baseline yet"
            print(core.DIM + "baseline set (%s) at charge %s%%. "
                  "Run again after some time on battery." % (why, charge) + core.RESET)
        return output.EXIT_OK

    rows, meta = drainers_since_unplug(snap, charge, external, baseline)
    if o.json:
        output.emit_json(output.document(
            "battery", "drainers", baseline_reset=False,
            charge_pct=charge, charge_drop=meta["charge_drop"],
            elapsed_s=round(meta["elapsed_s"], 1),
            drainers=[{"pid": pid, "name": name, "energy_score": round(score, 2),
                       "cpu_seconds": round(cpu_s, 2),
                       "idle_wakeups": di, "interrupt_wakeups": ti}
                      for score, cpu_s, di, ti, pid, name in rows[:o.limit]]))
        return output.EXIT_OK

    mins = meta["elapsed_s"] / 60.0
    print(core.BOLD + "stethoscope battery drainers · since unplug" + core.RESET)
    print(core.DIM + "%.0f min on battery  ·  charge dropped %d%%  (now %s%%)"
          % (mins, meta["charge_drop"], charge) + core.RESET)
    print()
    print(core.BOLD + "%7s  %-26s %8s %10s %10s"
          % ("PID", "COMMAND", "ENERGY", "CPU s", "WAKEUPS") + core.RESET)
    if not rows:
        print(core.DIM + "  (nothing notable — or processes have since exited)" + core.RESET)
    for score, cpu_s, di, ti, pid, name in rows[:o.limit]:
        print("%7d  %-26s %8.1f %10.1f %10d" % (pid, name[:26], score, cpu_s, di + ti))
    return output.EXIT_OK


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

USAGE = """stethoscope battery — power drain and battery health

  battery health     charge, cycle count, capacity vs design, condition
  battery top        who is drawing power now (energy-impact score)
  battery drainers   cumulative energy impact since you unplugged

Agent / scripting flags: --json  --once  --duration N  --interval N  --limit N
Exit codes: 0 ok · 2 usage

The energy score (CPU% + weighted wakeups) is a transparent proxy for macOS's
private Energy Impact; powermetrics under sudo is the authoritative source.
"""


def main(argv):
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    args = argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return output.EXIT_OK

    mode = "health"
    if args and not args[0].startswith("-"):
        mode = args.pop(0)

    try:
        o = output.parse_opts(args)
    except output.OptsError as e:
        sys.stderr.write("%s\n" % e)
        return output.EXIT_USAGE

    if mode == "health":
        return cmd_health(o)
    if mode == "top":
        return cmd_top(o)
    if mode == "drainers":
        return cmd_drainers(o)

    sys.stderr.write("unknown mode: %s\n\n%s" % (mode, USAGE))
    return output.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main(sys.argv))
