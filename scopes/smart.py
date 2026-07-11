#!/usr/bin/env python3
"""
stethoscope smart — drive health, wear, and pre-failure warnings.

  smart [status] [disk] [--json]   SMART verdict + wear + warnings, per drive

macOS exposes drive health at two levels of detail (core/smart.py owns both
probes):

  * `diskutil` gives the overall SMART verdict (Verified / Failing / Not
    Supported) with no dependencies — always available, for every internal
    and external physical drive (#10).
  * `smartctl` (smartmontools, if installed) adds the NVMe health log or
    ATA/SATA attribute table: wear percentage, data written, spare capacity,
    media errors, reallocated/pending/offline-uncorrectable sectors, and
    temperature — the numbers an honest life estimate and pre-failure
    warnings need (#11, #12).

This scope uses smartctl when it can and falls back to the dependency-free
verdict otherwise, so it always says *something* and says more when it can.
A USB bridge that refuses SMART pass-through, or no smartctl install at all,
is reported as an explicit "unavailable, here's why" detail — never
fabricated as healthy, and never silently dropped from the JSON shape.

No third-party dependencies — system Python 3 + core/smart.py. smartctl is
used opportunistically if it happens to be installed.
"""

import math
import os
import signal
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import cli, schema
from core import smart as probe

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

WEAR_CRITICAL_PCT = 90     # percentage_used at/above which wear is critical
TEMP_WARN_C = 70           # temperature at/above which to warn

# Life-estimate confidence bands. Extrapolating from a handful of percent of
# wear is a much rougher prognosis than extrapolating from a drive that is
# already a fifth of the way through its rated endurance — say so honestly
# rather than pretending every reading deserves the same confidence.
LIFE_LOW_CONFIDENCE_PCT = 5
LIFE_MODERATE_CONFIDENCE_PCT = 20

# NVMe critical_warning is a bitmask (NVMe Base spec, SMART/Health log page).
_CRITICAL_WARNING_BITS = (
    (1 << 0, "available spare capacity has fallen below the threshold"),
    (1 << 1, "temperature has exceeded a critical threshold"),
    (1 << 2, "NVM subsystem reliability has been degraded"),
    (1 << 3, "media has been placed in read-only mode"),
    (1 << 4, "volatile memory backup device has failed"),
)

_SMARTCTL_FIELDS = (
    "passed", "smartctl_exit_status", "critical_warning", "percentage_used",
    "power_on_hours", "data_units_written", "tbw_tb", "available_spare",
    "available_spare_threshold", "media_errors", "temperature_c",
    "reallocated_sector_ct", "reallocated_event_count",
    "current_pending_sector", "offline_uncorrectable",
    "reported_uncorrectable", "ata_failing_attributes",
    "ata_usage_attributes_now", "ata_failed_attributes_past",
)


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------

def _size_str(n):
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024.0:
            return "%d%s" % (int(n), unit) if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0
    return "%.1fP" % n


def _pct_str(n):
    return "?" if n is None else "%d%%" % n


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


# ---------------------------------------------------------------------------
# life estimate + pre-failure assessment
# ---------------------------------------------------------------------------

def life_estimate(percentage_used, power_on_hours):
    """Extrapolate remaining life from wear so far. Prognosis, not a promise.

    None below 0% measured wear or without power-on hours: there is nothing
    yet to extrapolate from, and dividing by ~0% wear would blow up into a
    meaningless number of years rather than an honest "too early to tell".
    """
    percentage_used = _number(percentage_used)
    power_on_hours = _number(power_on_hours)
    if percentage_used is None or percentage_used <= 0:
        return None
    if power_on_hours is None or power_on_hours <= 0:
        return None
    try:
        wear_fraction = percentage_used / 100.0
        if wear_fraction == 0:
            return None
        total_hours = power_on_hours / wear_fraction
        remaining_hours = max(0.0, total_hours - power_on_hours)
        remaining_years = remaining_hours / 24 / 365
    except (OverflowError, ZeroDivisionError):
        return None
    if not math.isfinite(total_hours) or not math.isfinite(remaining_years):
        return None
    if percentage_used < LIFE_LOW_CONFIDENCE_PCT:
        confidence = "low"
    elif percentage_used < LIFE_MODERATE_CONFIDENCE_PCT:
        confidence = "moderate"
    else:
        confidence = "high"
    return {
        "remaining_life_pct": max(0, 100 - percentage_used),
        "consumed_life_pct": percentage_used,
        "remaining_hours": round(remaining_hours),
        "remaining_years": round(remaining_years, 1),
        "confidence": confidence,
    }


def _describe_critical_warning(bits):
    described = [msg for bit, msg in _CRITICAL_WARNING_BITS if bits & bit]
    return described or ["unspecified critical-warning bit(s) set (0x%x)" % bits]


def assess(health):
    """Pre-failure warnings (severity-tagged) for a drive-health dict.

    Flags exactly the attributes that predict failure (#12): SMART FAILING,
    an NVMe critical-warning flag, spare capacity under threshold, wear at
    or above WEAR_CRITICAL_PCT, pending/offline-uncorrectable sectors,
    media/data-integrity errors, already-reallocated sectors, and high
    temperature. Anything that predicts imminent, unrecoverable data loss is
    `critical`; anything that is a real but softer signal is `warn`.
    """
    warnings = []

    def add(code, severity, message):
        warnings.append({
            "code": code,
            "severity": severity,
            "message": message,
        })

    status = health.get("smart_status")
    if status == "failing" or health.get("passed") is False:
        add("smart_failing", "critical",
            "SMART reports the drive is FAILING — back up now.")

    exit_status = health.get("smartctl_exit_status")
    exit_status = (exit_status
                   if isinstance(exit_status, int)
                   and not isinstance(exit_status, bool) else 0)
    failing_attributes = health.get("ata_failing_attributes")
    failing_attributes = (failing_attributes
                          if isinstance(failing_attributes, list) else [])
    failing_attributes = [name for name in failing_attributes
                          if isinstance(name, str)]
    failed_past = health.get("ata_failed_attributes_past")
    failed_past = failed_past if isinstance(failed_past, list) else []
    failed_past = [name for name in failed_past if isinstance(name, str)]
    usage_now = health.get("ata_usage_attributes_now")
    usage_now = usage_now if isinstance(usage_now, list) else []
    usage_now = [name for name in usage_now if isinstance(name, str)]
    if failing_attributes:
        add("ata_attribute_failing", "critical",
            "ATA attribute(s) failing now: %s — back up now."
            % ", ".join(failing_attributes))
    elif exit_status & 0x10:
        add("ata_prefail_threshold", "critical",
            "An ATA pre-failure attribute is at or below its threshold — "
            "back up now.")
    if usage_now:
        add("ata_usage_attribute_threshold", "warn",
            "ATA usage attribute(s) at threshold now: %s."
            % ", ".join(usage_now))
    elif exit_status & 0x20:
        add("ata_usage_threshold", "warn",
            "An ATA usage attribute has reached or crossed its threshold.")
    if failed_past:
        add("ata_attribute_failed_past", "warn",
            "ATA attribute(s) previously crossed a threshold: %s."
            % ", ".join(failed_past))
    if exit_status & 0x40:
        add("smart_error_log", "warn",
            "The SMART error log contains device errors.")
    if exit_status & 0x80:
        add("smart_self_test_log", "warn",
            "The SMART self-test log contains failed tests.")

    critical_warning = health.get("critical_warning")
    critical_warning = (critical_warning
                        if isinstance(critical_warning, int)
                        and not isinstance(critical_warning, bool) else None)
    if critical_warning:
        for desc in _describe_critical_warning(critical_warning):
            add("nvme_critical_warning", "critical",
                "NVMe critical warning: %s — back up now." % desc)

    spare = _number(health.get("available_spare"))
    threshold = _number(health.get("available_spare_threshold"))
    if spare is not None and threshold is not None and spare < threshold:
        add("spare_below_threshold", "critical",
            "Spare capacity %d%% is below the %d%% threshold — "
            "back up now." % (spare, threshold))

    used = _number(health.get("percentage_used"))
    if used is not None and used >= WEAR_CRITICAL_PCT:
        add("wear_critical", "critical",
            "Wear is %d%% — the drive is near end of life. "
            "Back up now." % used)

    pending = _number(health.get("current_pending_sector"))
    if pending:
        add("pending_sectors", "critical",
            "%d sector(s) pending reallocation — back up now." % pending)

    offline = _number(health.get("offline_uncorrectable"))
    if offline:
        add("offline_uncorrectable", "critical",
            "%d offline-uncorrectable sector(s) — back up now." % offline)

    media = _number(health.get("media_errors"))
    if media:
        add("media_errors", "warn",
            "%d media/data-integrity error(s) logged; back this "
            "drive up soon." % media)

    reported = _number(health.get("reported_uncorrectable"))
    if reported:
        add("reported_uncorrectable", "warn",
            "%d uncorrectable command error(s) reported; keep a current "
            "backup." % reported)

    realloc = _number(health.get("reallocated_sector_ct"))
    if realloc:
        add("reallocated_sectors", "warn",
            "%d sector(s) already reallocated; keep a current "
            "backup." % realloc)

    realloc_events = _number(health.get("reallocated_event_count"))
    if realloc_events:
        add("reallocation_events", "warn",
            "%d reallocation event(s) logged." % realloc_events)

    temp = _number(health.get("temperature_c"))
    if temp is not None and temp >= TEMP_WARN_C:
        add("temperature_high", "warn",
            "Temperature %s\u00b0C is high." % temp)

    return warnings


def _worst_severity(warnings):
    if any(w["severity"] == "critical" for w in warnings):
        return "critical"
    if warnings:
        return "warn"
    return "ok"


# ---------------------------------------------------------------------------
# per-drive health assembly
# ---------------------------------------------------------------------------

def drive_health(dev, internal, smartctl_bin):
    """Full health for one drive: diskutil verdict, optional smartctl
    enrichment, life estimate, and pre-failure warnings.
    """
    h = {"device": dev, "internal": internal}
    info, diskutil_detail = probe.diskutil_info(dev)
    h.update(info)
    h["diskutil_detail"] = diskutil_detail

    raw, smartctl_detail = probe.probe_smartctl(dev, smartctl_bin)
    smartctl_available = raw is not None
    h["smartctl_available"] = smartctl_available
    h["smartctl_detail"] = smartctl_detail
    for field in _SMARTCTL_FIELDS:
        h[field] = None

    if smartctl_available:
        h["source"] = "smartctl"
        extracted = probe.extract_smartctl(raw)
        if not h.get("name") and extracted.get("model"):
            h["name"] = extracted["model"]
        passed = extracted.get("passed")
        status = h.get("smart_status")
        if passed is False:
            # A failing verdict always wins: never let a nicer-looking
            # source suppress the one that says the drive is dying.
            h["smart_status"] = "failing"
        elif passed is True and status in (None, "", "unknown", "not supported"):
            # A passing verdict only replaces an unknown/unsupported
            # diskutil status — it never downgrades an existing "failing".
            h["smart_status"] = "verified"
        for field in _SMARTCTL_FIELDS:
            h[field] = extracted.get(field)
    else:
        h["source"] = "diskutil"

    h["life"] = life_estimate(h.get("percentage_used"), h.get("power_on_hours"))
    h["warnings"] = assess(h)
    h["worst_severity"] = _worst_severity(h["warnings"])
    return h


def collect_health(only=None):
    """Collect structured SMART health once for any presentation surface.

    ``enumeration_error`` distinguishes a failed diskutil probe from the
    supported empty ``drives`` result. Optional smartctl absence and incomplete
    per-drive probes are represented as partial reasons, never as health.
    """
    smartctl_bin = probe.find_smartctl()
    physical = probe.list_physical_drives()
    if physical is None:
        message = "diskutil is unavailable or failed; cannot enumerate drives"
        return {
            "drives": [],
            "smartctl_path": smartctl_bin,
            "smartctl_available": smartctl_bin is not None,
            "partial": True,
            "partial_reasons": ["diskutil_unavailable"],
            "enumeration_error": message,
            "selection_error": None,
        }

    if only:
        normalized = only.replace("/dev/", "")
        matched = [(dev, internal) for dev, internal in physical
                   if dev == normalized]
        if not matched:
            return {
                "drives": [],
                "smartctl_path": smartctl_bin,
                "smartctl_available": smartctl_bin is not None,
                "partial": False,
                "partial_reasons": [],
                "enumeration_error": None,
                "selection_error":
                    "no physical drive %r (try: diskutil list physical)"
                    % normalized,
            }
        physical = matched

    results = [
        drive_health(device, internal, smartctl_bin)
        for device, internal in physical
    ]
    reasons = []
    if any(health.get("diskutil_detail") for health in results):
        reasons.append("diskutil_probe_incomplete")
    if smartctl_bin is None and results:
        reasons.append("smartctl_unavailable")
    elif any(
            not health.get("smartctl_available")
            or health.get("smartctl_detail")
            for health in results):
        reasons.append("smartctl_probe_incomplete")
    return {
        "drives": results,
        "smartctl_path": smartctl_bin,
        "smartctl_available": smartctl_bin is not None,
        "partial": bool(reasons),
        "partial_reasons": reasons,
        "enumeration_error": None,
        "selection_error": None,
    }


# ---------------------------------------------------------------------------
# presentation
# ---------------------------------------------------------------------------

def _render(drives, smartctl_bin):
    out = [BOLD + "stethoscope smart \u00b7 drive health" + RESET]
    if not smartctl_bin:
        out.append(DIM + "(smartctl not found — install smartmontools for "
                   "wear/life detail: brew install smartmontools)" + RESET)
    out.append("")
    if not drives:
        out.append(DIM + "  (no physical drives found)" + RESET)
        return "\n".join(out) + "\n"

    for h in drives:
        verdict = h.get("smart_status") or "unknown"
        head = "%s  %s  \u00b7  %s  \u00b7  SMART %s" % (
            h["device"], h.get("name") or "?", _size_str(h.get("size_bytes")),
            verdict)
        out.append(BOLD + head + RESET)

        if h["smartctl_available"]:
            if h.get("percentage_used") is not None:
                life = h.get("life") or {}
                extra = ""
                if life.get("remaining_years") is not None:
                    extra = "  \u00b7  ~%.1f yr left (%s confidence)" % (
                        life["remaining_years"], life.get("confidence"))
                tbw = ("%.1f TB" % h["tbw_tb"]
                       if h.get("tbw_tb") is not None else "?")
                hours = ("?" if h.get("power_on_hours") is None
                         else "%d" % h["power_on_hours"])
                out.append("  wear %s  \u00b7  %s written  "
                           "\u00b7  %s power-on hrs  \u00b7  spare %s%s"
                           % (_pct_str(h["percentage_used"]), tbw,
                              hours,
                              _pct_str(h.get("available_spare")), extra))
            if h.get("temperature_c") is not None:
                out.append(DIM + "  temperature %s\u00b0C" % h["temperature_c"] + RESET)
            if h.get("smartctl_detail"):
                out.append(DIM + "  (smartctl: %s)"
                           % h["smartctl_detail"] + RESET)
        else:
            out.append(DIM + "  (smartctl: %s)" % h["smartctl_detail"] + RESET)

        for w in h["warnings"]:
            mark = "\u2021" if w["severity"] == "critical" else "!"
            out.append(BOLD + "  %s %s" % (mark, w["message"]) + RESET)
        if not h["warnings"] and verdict == "verified":
            out.append(DIM + "  healthy" + RESET)
        out.append("")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def cmd_status(options):
    """SMART health for each physical drive, or just the one named."""
    only = options.rest[0] if options.rest else None
    collection = collect_health(only)
    message = collection["enumeration_error"]
    if message:
        if options.json:
            cli.emit_json(schema.document(
                "smart", "status", partial=True,
                partial_reasons=["diskutil_unavailable"], drives=[],
                error=message))
        else:
            sys.stderr.write(message + "\n")
        return cli.EXIT_ERROR

    message = collection["selection_error"]
    if message:
        if options.json:
            cli.emit_json(schema.document(
                "smart", "status", drives=[], error=message))
        else:
            sys.stderr.write(message + "\n")
        return cli.EXIT_USAGE

    results = collection["drives"]
    reasons = collection["partial_reasons"]
    partial = collection["partial"]

    if options.json:
        cli.emit_json(schema.document(
            "smart", "status", partial=partial, partial_reasons=reasons,
            drives=results, error=None))
    else:
        sys.stdout.write(_render(results, collection["smartctl_path"]))

    if any(h["worst_severity"] != "ok" for h in results):
        return cli.EXIT_FINDINGS
    return cli.EXIT_OK


USAGE = """stethoscope smart — drive health, wear, and pre-failure warnings

  smart [status] [disk] [--json]   SMART verdict + wear + warnings
                                    (every physical drive, or just one)

Uses smartctl for wear/life/attribute detail when installed (PATH, or
/opt/homebrew/{bin,sbin}, /usr/local/{bin,sbin}, or /usr/sbin); always falls
back to the dependency-free SMART verdict from diskutil.

Agent / scripting flags: --json
Exit codes: 0 no findings \u00b7 1 drive warning/critical \u00b7 2 usage \u00b7 4 probe error
"""


def main(argv):
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    args = argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return cli.EXIT_OK

    mode = "status"
    if args and args[0] == "status":
        args.pop(0)
        mode = "status"

    try:
        options = cli.parse_options(args)
        cli.require_options(options, mode, {"json"})
        if len(options.rest) > 1:
            raise cli.OptionsError(
                "%s accepts at most one disk argument" % mode)
        return cmd_status(options)
    except cli.OptionsError as exc:
        sys.stderr.write("%s\n" % exc)
        return cli.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main(sys.argv))
