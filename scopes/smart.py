#!/usr/bin/env python3
"""
stethoscope smart — drive health, wear, and life expectancy.

  smart [disk]   SMART status + wear + pre-failure warnings for each drive

macOS exposes drive health at two levels of detail:

  * `diskutil` / `system_profiler` give the overall SMART verdict
    (Verified / Failing) with no dependencies — always available.
  * `smartctl` (smartmontools, if installed) adds the NVMe/SATA health log:
    wear percentage, power-on hours, data written (TBW), spare capacity, media
    errors and temperature — the numbers needed to estimate remaining life.

This scope uses `smartctl` when present and falls back to the dependency-free
verdict otherwise, so it always tells you *something* and tells you more when
it can. It flags the attributes that actually predict failure — SMART failing,
wear ≥ 90%, spare below threshold, media errors — and says plainly: back this
drive up now. Life expectancy is a prognosis with honest error bars, not a
promise (#10, #11, #12).

No third-party dependencies required — system Python 3 + core.py. smartctl is
used opportunistically if it happens to be installed.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys

try:
    from scopes import core, output
except ImportError:   # invoked with scopes/ directly on sys.path
    import core
    import output

# NVMe "Data Units Written" are in 512,000-byte units (1000 * 512).
_NVME_DATA_UNIT_BYTES = 512 * 1000
_WEAR_CRITICAL = 90        # percentage_used at/above which to warn hard
_TEMP_WARN_C = 70


def _smartctl_path():
    return (shutil.which("smartctl") or
            next((p for p in ("/opt/homebrew/bin/smartctl", "/usr/local/bin/smartctl",
                              "/usr/sbin/smartctl") if os.path.exists(p)), None))


# ---------------------------------------------------------------------------
# drive enumeration + the dependency-free verdict
# ---------------------------------------------------------------------------

def list_physical_drives():
    """[(device, internal_bool)] for each physical disk (via diskutil)."""
    try:
        out = subprocess.run(["/usr/sbin/diskutil", "list", "physical"],
                             capture_output=True, text=True).stdout
    except OSError:
        return []
    drives = []
    for ln in out.splitlines():
        m = re.match(r"/dev/(disk\d+) \((internal|external|synthesized)", ln)
        if m and m.group(2) != "synthesized":
            drives.append((m.group(1), m.group(2) == "internal"))
    return drives


def _diskutil_info(dev):
    """Parse `diskutil info` for the fields we surface."""
    try:
        out = subprocess.run(["/usr/sbin/diskutil", "info", dev],
                             capture_output=True, text=True).stdout
    except OSError:
        return {}
    info = {}
    for ln in out.splitlines():
        if ":" not in ln:
            continue
        k, _, v = ln.partition(":")
        info[k.strip()] = v.strip()
    size = None
    m = re.search(r"\((\d+) Bytes\)", info.get("Disk Size", ""))
    if m:
        size = int(m.group(1))
    status = info.get("SMART Status", "").lower() or "unknown"
    return {
        "name": info.get("Device / Media Name"),
        "size_bytes": size,
        "solid_state": info.get("Solid State") == "Yes",
        "smart_status": status,   # "verified" / "failing" / "not supported" / ...
    }


# ---------------------------------------------------------------------------
# smartctl enrichment (optional)
# ---------------------------------------------------------------------------

def _smartctl_health(dev):
    """The NVMe/SATA health fields from smartctl -j, or None if unavailable."""
    sc = _smartctl_path()
    if not sc:
        return None
    try:
        res = subprocess.run([sc, "-j", "-a", "/dev/" + dev],
                             capture_output=True, text=True)
        data = json.loads(res.stdout)   # smartctl's exit code is a bitmask; ignore it
    except (OSError, ValueError):
        return None

    log = data.get("nvme_smart_health_information_log", {})
    passed = data.get("smart_status", {}).get("passed")
    duw = log.get("data_units_written")
    return {
        "model": data.get("model_name"),
        "passed": passed,
        "critical_warning": log.get("critical_warning"),
        "percentage_used": log.get("percentage_used"),
        "power_on_hours": log.get("power_on_hours"),
        "data_units_written": duw,
        "tbw_tb": round(duw * _NVME_DATA_UNIT_BYTES / 1e12, 2) if duw else None,
        "available_spare": log.get("available_spare"),
        "available_spare_threshold": log.get("available_spare_threshold"),
        "media_errors": log.get("media_errors"),
        "temperature_c": (log.get("temperature")
                          or data.get("temperature", {}).get("current")),
    }


# ---------------------------------------------------------------------------
# life estimate + pre-failure assessment
# ---------------------------------------------------------------------------

def life_estimate(percentage_used, power_on_hours):
    """Extrapolate remaining life from wear so far. Prognosis, not a promise."""
    if not percentage_used or not power_on_hours or percentage_used <= 0:
        return None            # too early (or no data) to extrapolate
    total_hours = power_on_hours / (percentage_used / 100.0)
    remaining_hours = max(0.0, total_hours - power_on_hours)
    return {
        "remaining_life_pct": max(0, 100 - percentage_used),
        "remaining_hours": round(remaining_hours),
        "remaining_years": round(remaining_hours / 24 / 365, 1),
        # early wear readings extrapolate wildly; flag low confidence.
        "confidence": "low" if percentage_used < 5 else "moderate",
    }


def assess(health):
    """Pre-failure warnings (highest severity first) for a drive-health dict."""
    warnings = []

    def add(sev, msg):
        warnings.append({"severity": sev, "message": msg})

    status = health.get("smart_status")
    if status == "failing" or health.get("passed") is False:
        add("critical", "SMART reports the drive is FAILING — back up now.")
    if health.get("critical_warning"):
        add("critical", "NVMe critical-warning flag is set — back up now.")

    spare = health.get("available_spare")
    thresh = health.get("available_spare_threshold")
    if spare is not None and thresh is not None and spare < thresh:
        add("critical", "Spare capacity %d%% is below the %d%% threshold." % (spare, thresh))

    used = health.get("percentage_used")
    if used is not None and used >= _WEAR_CRITICAL:
        add("critical", "Wear is %d%% — the drive is near end of life." % used)

    if health.get("media_errors"):
        add("warn", "%d media/data-integrity error(s) logged." % health["media_errors"])

    temp = health.get("temperature_c")
    if temp is not None and temp >= _TEMP_WARN_C:
        add("warn", "Temperature %d°C is high." % temp)

    return warnings


def drive_health(dev, internal=True):
    """Full health for one drive: verdict + (if smartctl) wear/life + warnings."""
    h = {"device": dev, "internal": internal, "source": "diskutil"}
    h.update(_diskutil_info(dev))

    sc = _smartctl_health(dev)
    if sc:
        h["source"] = "smartctl"
        if sc.get("model"):
            h["name"] = sc["model"]
        if sc.get("passed") is not None and h.get("smart_status") in (None, "", "unknown"):
            h["smart_status"] = "verified" if sc["passed"] else "failing"
        for k in ("passed", "critical_warning", "percentage_used", "power_on_hours",
                  "data_units_written", "tbw_tb", "available_spare",
                  "available_spare_threshold", "media_errors", "temperature_c"):
            h[k] = sc.get(k)
        h["life"] = life_estimate(sc.get("percentage_used"), sc.get("power_on_hours"))

    h["warnings"] = assess(h)
    h["worst_severity"] = ("critical" if any(w["severity"] == "critical" for w in h["warnings"])
                           else "warn" if h["warnings"] else "ok")
    return h


# ---------------------------------------------------------------------------
# presentation
# ---------------------------------------------------------------------------

def _render(drives):
    out = [core.BOLD + "stethoscope smart · drive health" + core.RESET]
    if not _smartctl_path():
        out.append(core.DIM + "(install smartmontools for wear/life detail: "
                   "brew install smartmontools)" + core.RESET)
    out.append("")
    for h in drives:
        verdict = h.get("smart_status", "unknown")
        head = "%s  %s  ·  %s  ·  SMART %s" % (
            h["device"], h.get("name") or "?",
            core.human(h["size_bytes"]) if h.get("size_bytes") else "?",
            verdict)
        out.append(core.BOLD + head + core.RESET)
        if h.get("percentage_used") is not None:
            life = h.get("life") or {}
            extra = ""
            if life.get("remaining_years") is not None:
                extra = "  ·  ~%.1f yr left (%s confidence)" % (
                    life["remaining_years"], life.get("confidence"))
            out.append("  wear %d%%  ·  %s written  ·  %d power-on hrs  ·  spare %s%%%s"
                       % (h["percentage_used"],
                          ("%.1f TB" % h["tbw_tb"]) if h.get("tbw_tb") else "?",
                          h.get("power_on_hours") or 0,
                          h.get("available_spare"), extra))
            if h.get("temperature_c") is not None:
                out.append(core.DIM + "  temperature %d°C" % h["temperature_c"] + core.RESET)
        for w in h["warnings"]:
            mark = "‼" if w["severity"] == "critical" else "!"
            out.append(core.BOLD + "  %s %s" % (mark, w["message"]) + core.RESET)
        if not h["warnings"] and h.get("percentage_used") is not None:
            out.append(core.DIM + "  healthy" + core.RESET)
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def cmd_status(o, only=None):
    """SMART health for each physical drive (or just `only`)."""
    drives = list_physical_drives()
    if only:
        only = only.replace("/dev/", "")
        drives = [(d, i) for d, i in drives if d == only]
        if not drives:
            sys.stderr.write("no physical drive %r (try: diskutil list physical)\n" % only)
            return output.EXIT_USAGE

    results = [drive_health(dev, internal) for dev, internal in drives]

    if o.json:
        output.emit_json(output.document("smart", "status", drives=results))
    else:
        print(_render(results))

    # Exit non-zero if any drive is in a critical state, so `smart` doubles as a
    # health check in scripts/CI.
    if any(h["worst_severity"] == "critical" for h in results):
        return output.EXIT_FINDINGS
    return output.EXIT_OK


USAGE = """stethoscope smart — drive health, wear, and life expectancy

  smart [disk]   SMART status + wear + pre-failure warnings (all drives, or one)

Uses smartctl for wear/life detail when installed; falls back to the
dependency-free SMART verdict from diskutil otherwise.

Agent / scripting flags: --json
Exit codes: 0 healthy · 1 a drive is in a critical state · 2 usage
"""


def main(argv):
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    args = argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return output.EXIT_OK

    # optional leading "status" verb, kept for symmetry with other scopes
    if args and args[0] == "status":
        args.pop(0)

    try:
        o = output.parse_opts(args)
    except output.OptsError as e:
        sys.stderr.write("%s\n" % e)
        return output.EXIT_USAGE

    only = o.rest[0] if o.rest else None
    return cmd_status(o, only=only)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
