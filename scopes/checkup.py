#!/usr/bin/env python3
"""
stethoscope checkup — a one-shot full-body exam.

Samples every scope once, rolls the results into a single health report with an
overall verdict, and lists findings worst-first with the drill-down command to
confirm each (#25). The machine's annual physical: run it, read the verdict,
follow the pointer.

  stethoscope checkup [--json]

This composes the scopes' data layers directly (never their rendered text):

    cpu.rank_cpu            → system load + the top consumer
    memory.system_memory    → pressure + headroom
    battery.battery_health  → charge, health %, condition
    smart.drive_health      → per-drive SMART verdict + pre-failure warnings

Findings carry a severity (critical / warn / info); the overall verdict is the
worst of them. Exit is 1 when anything critical is found, so `checkup` doubles
as a headless health gate. No third-party dependencies.
"""

import signal
import sys
import time

try:
    from scopes import core, output, cpu, memory, battery, smart
except ImportError:   # invoked with scopes/ directly on sys.path
    import core
    import output
    import cpu
    import memory
    import battery
    import smart

_SEV_ORDER = {"ok": 0, "info": 1, "warn": 2, "critical": 3}


def _finding(severity, area, message, drill=None):
    return {"severity": severity, "area": area, "message": message, "drill": drill}


# ---------------------------------------------------------------------------
# per-scope vitals + findings
# ---------------------------------------------------------------------------

def _check_cpu(sample_seconds=0.4):
    prev_mach, prev = cpu.snapshot()
    time.sleep(sample_seconds)
    cur_mach, cur = cpu.snapshot()
    rows, sys_cpu = cpu.rank_cpu(prev, cur, prev_mach, cur_mach, sample_seconds)
    top = rows[0] if rows else None
    vitals = {"system_cpu_pct": round(sys_cpu, 1), "ncpu": cpu.NCPU,
              "top": ({"pid": top[4], "name": top[5], "cpu_pct": round(top[0], 1)}
                      if top else None)}
    findings = []
    # A single process pegging ~a full core sustained is worth surfacing; real
    # runaway detection (vs the machine's own baseline) is the v0.7 anomaly job.
    if top and top[0] >= 90.0:
        findings.append(_finding(
            "info", "cpu", "%s (pid %d) is using %.0f%% CPU"
            % (top[5], top[4], top[0]), "stethoscope cpu top"))
    return vitals, findings


def _check_memory():
    s = memory.system_memory()
    vitals = {"pressure": s["pressure"], "used": s["used"], "total": s["total"],
              "used_pct": round(s["used"] / s["total"] * 100, 1) if s["total"] else None}
    findings = []
    if s["pressure"] == "critical":
        findings.append(_finding("critical", "memory",
                                 "memory pressure is critical", "stethoscope memory top"))
    elif s["pressure"] == "warn":
        findings.append(_finding("warn", "memory",
                                 "memory pressure is elevated", "stethoscope memory top"))
    return vitals, findings


def _check_battery():
    h = battery.battery_health()
    if not h.get("present"):
        return {"present": False}, []
    vitals = {"present": True, "charge_pct": h["charge_pct"],
              "health_pct": h["health_pct"], "cycle_count": h["cycle_count"],
              "condition": h["condition"]}
    findings = []
    if h["condition"] != "Normal":
        findings.append(_finding("warn", "battery",
                                 "battery condition: %s (health %s%%, %s cycles)"
                                 % (h["condition"], h["health_pct"], h["cycle_count"]),
                                 "stethoscope battery health"))
    return vitals, findings


def _check_smart():
    drives = []
    findings = []
    for dev, internal in smart.list_physical_drives():
        h = smart.drive_health(dev, internal)
        drives.append({"device": dev, "name": h.get("name"),
                       "smart_status": h.get("smart_status"),
                       "percentage_used": h.get("percentage_used"),
                       "worst_severity": h["worst_severity"]})
        for w in h["warnings"]:
            sev = w["severity"] if w["severity"] in _SEV_ORDER else "warn"
            findings.append(_finding(sev, "smart",
                                     "%s: %s" % (dev, w["message"]),
                                     "stethoscope smart %s" % dev))
    return {"drives": drives}, findings


def run_checkup():
    """Gather vitals + findings from every scope; return one report dict."""
    vitals = {}
    findings = []
    for name, fn in (("cpu", _check_cpu), ("memory", _check_memory),
                     ("battery", _check_battery), ("smart", _check_smart)):
        try:
            v, f = fn()
        except Exception as e:   # a broken probe must not sink the whole exam
            v, f = {"error": str(e)}, [_finding("warn", name,
                                                "%s probe failed: %s" % (name, e))]
        vitals[name] = v
        findings.extend(f)

    findings.sort(key=lambda f: -_SEV_ORDER.get(f["severity"], 0))
    overall = "ok"
    for f in findings:
        if _SEV_ORDER.get(f["severity"], 0) > _SEV_ORDER[overall]:
            overall = f["severity"]
    if overall == "info":
        overall = "ok"   # info-level notes don't make the machine "unwell"
    return {"overall": overall, "findings": findings, "vitals": vitals}


# ---------------------------------------------------------------------------
# presentation
# ---------------------------------------------------------------------------

def _render(report):
    v = report["vitals"]
    out = [core.BOLD + "stethoscope checkup · full-body exam · %s"
           % time.strftime("%H:%M:%S") + core.RESET]

    verdict = report["overall"]
    banner = {"ok": "✓ all clear", "warn": "! needs attention",
              "critical": "‼ critical — act now"}.get(verdict, verdict)
    out.append((core.BOLD + banner + core.RESET) if verdict != "ok"
               else core.DIM + banner + core.RESET)
    out.append("")

    cpu_v = v.get("cpu", {})
    out.append("cpu      %s%% of %d cores%s"
               % (cpu_v.get("system_cpu_pct", "?"), cpu_v.get("ncpu", 0),
                  ("   top: %s %.0f%%" % (cpu_v["top"]["name"], cpu_v["top"]["cpu_pct"])
                   if cpu_v.get("top") else "")))
    mem_v = v.get("memory", {})
    out.append("memory   %s / %s used  ·  pressure %s"
               % (core.human(mem_v.get("used", 0)), core.human(mem_v.get("total", 0)),
                  mem_v.get("pressure", "?")))
    bat_v = v.get("battery", {})
    if bat_v.get("present"):
        out.append("battery  %s%%  ·  health %s%%  ·  %s cycles  ·  %s"
                   % (bat_v["charge_pct"], bat_v["health_pct"],
                      bat_v["cycle_count"], bat_v["condition"]))
    for d in v.get("smart", {}).get("drives", []):
        wear = ("wear %d%%" % d["percentage_used"]) if d.get("percentage_used") is not None else "no detail"
        out.append("drive    %s %s  ·  SMART %s  ·  %s"
                   % (d["device"], d.get("name") or "?", d.get("smart_status"), wear))

    out.append("")
    if report["findings"]:
        out.append(core.BOLD + "findings:" + core.RESET)
        for f in report["findings"]:
            mark = {"critical": "‼", "warn": "!", "info": "·"}.get(f["severity"], "·")
            line = "  %s [%s] %s" % (mark, f["area"], f["message"])
            out.append(line)
            if f.get("drill"):
                out.append(core.DIM + "      → %s" % f["drill"] + core.RESET)
    else:
        out.append(core.DIM + "no findings — everything looks healthy." + core.RESET)
    return "\n".join(out)


def cmd_checkup(o):
    report = run_checkup()
    if o.json:
        output.emit_json(output.document("checkup", "checkup", **report))
    else:
        print(_render(report))
    return output.EXIT_FINDINGS if report["overall"] == "critical" else output.EXIT_OK


USAGE = """stethoscope checkup — a one-shot full-body exam

  checkup [--json]   sample every scope once, print a health report + findings

Exit codes: 0 healthy (or only advisory findings) · 1 something critical
"""


def main(argv):
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    args = argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return output.EXIT_OK
    try:
        o = output.parse_opts(args)
    except output.OptsError as e:
        sys.stderr.write("%s\n" % e)
        return output.EXIT_USAGE
    return cmd_checkup(o)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
