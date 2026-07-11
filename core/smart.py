"""
stethoscope core.smart — raw diskutil / smartctl probes and parsers.

Drive health on macOS is layered:

  * `diskutil list physical` / `diskutil info <disk>` — dependency-free
    enumeration and the overall SMART verdict (Verified / Failing / Not
    Supported). Always available; this is the fallback that never fails
    for lack of a third-party tool.

  * `smartctl -j -a <disk>` (smartmontools, if installed) — the NVMe health
    log or ATA/SATA attribute table: wear percentage, data written, spare
    capacity, media errors, reallocated/pending/offline-uncorrectable
    sectors, temperature. Opportunistic: used when present, never required.

This module owns only the probe (subprocess invocation) and the parse (text
and JSON -> plain dicts). It has no opinion on what counts as a warning or
how confident a life estimate is — that judgment lives in scopes/smart.py,
which is also where the CLI and JSON envelope live (#10, #11, #12).

Every parser here is total: given absent, truncated, or malformed input it
returns a structure with `None` fields rather than raising, so a missing
tool or an unplugged drive is representable as an explicit, honest "unknown"
in JSON output rather than a fabricated healthy value or a crash.

No third-party dependencies — system Python 3 only. smartctl itself is
optional; its absence is a normal, supported state.
"""

import json
import math
import os
import re
import shutil
import subprocess

# NVMe "Data Units Written" is reported in units of 1000 * 512 bytes,
# per the NVMe spec (and as emitted by smartctl's JSON log).
NVME_DATA_UNIT_BYTES = 512 * 1000
_ATA_REMAINING_LIFE_NAMES = (
    "Media_Wearout_Indicator",
    "Percent_Lifetime_Remain",
    "SSD_Life_Left",
    "Remaining_Lifetime_Perc",
    "Wear_Leveling_Count",
)
_ATA_USED_LIFE_NAMES = (
    "Percent_Lifetime_Used",
    "Percentage_Used",
)
_ATA_WRITE_UNITS = (
    ("Total_LBAs_Written", 512),
    ("Host_Writes_32MiB", 32 * 1024 * 1024),
    ("Host_Writes_GiB", 1024 * 1024 * 1024),
    ("Lifetime_Writes_GiB", 1024 * 1024 * 1024),
)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _integer(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _terabytes(value, bytes_per_unit):
    value = _number(value)
    if value is None:
        return None
    try:
        terabytes = value * bytes_per_unit / 1e12
    except OverflowError:
        return None
    if not math.isfinite(terabytes):
        return None
    return round(terabytes, 2)

DISKUTIL = "/usr/sbin/diskutil"

# smartctl (smartmontools) is not shipped by Apple. It commonly lands under
# Homebrew's bin *or* sbin (Apple Silicon and Intel prefixes both occur), or
# a manual /usr/local install — none of which are guaranteed to be on PATH.
SMARTCTL_CANDIDATES = (
    "/opt/homebrew/bin/smartctl",
    "/opt/homebrew/sbin/smartctl",
    "/usr/local/bin/smartctl",
    "/usr/local/sbin/smartctl",
    "/usr/sbin/smartctl",
)

_PHYSICAL_RE = re.compile(r"^/dev/(disk\d+) \((internal|external|synthesized)")


def find_smartctl():
    """Locate smartctl on PATH, else in the common install locations."""
    found = shutil.which("smartctl")
    if found:
        return found
    for candidate in SMARTCTL_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# diskutil — dependency-free enumeration + verdict
# ---------------------------------------------------------------------------

def run_diskutil(*args, timeout=15):
    """Run diskutil with args, returning stdout, or None if it could not run.

    None is reserved for an actual probe failure (missing binary, timeout,
    OS-level error) — never for a well-formed "nothing found" result, so
    callers can tell "diskutil is broken" from "diskutil says there is
    nothing here".
    """
    try:
        completed = subprocess.run(
            [DISKUTIL] + list(args), capture_output=True, text=True,
            timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def parse_physical_drives(text):
    """Parse `diskutil list physical` into [(device, internal_bool), ...].

    Synthesized (APFS container) entries are skipped — they are not
    physical drives and have no SMART data of their own.
    """
    drives = []
    for line in (text or "").splitlines():
        m = _PHYSICAL_RE.match(line)
        if m and m.group(2) != "synthesized":
            drives.append((m.group(1), m.group(2) == "internal"))
    return drives


def list_physical_drives():
    """[(device, internal_bool), ...], or None if diskutil could not run."""
    text = run_diskutil("list", "physical")
    if text is None:
        return None
    return parse_physical_drives(text)


def parse_diskutil_info(text):
    """Parse `diskutil info <disk>` into the fields this scope surfaces.

    Total: absent/unparseable input yields a fully-`None`/"unknown"
    structure rather than a partial dict or an exception.
    """
    info = {"name": None, "size_bytes": None, "solid_state": None,
            "smart_status": "unknown"}
    if not text:
        return info
    fields = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    info["name"] = fields.get("Device / Media Name") or None
    m = re.search(r"\((\d+) Bytes\)", fields.get("Disk Size", ""))
    if m:
        info["size_bytes"] = int(m.group(1))
    if "Solid State" in fields:
        info["solid_state"] = fields["Solid State"] == "Yes"
    status = fields.get("SMART Status", "").strip().lower()
    info["smart_status"] = status or "unknown"
    return info


def diskutil_info(dev):
    """Return (info_dict, detail). detail is None on success, else a string
    explaining why diskutil produced no data for this drive.
    """
    text = run_diskutil("info", dev)
    if text is None:
        return parse_diskutil_info(None), "diskutil info failed to run"
    if not text.strip():
        return parse_diskutil_info(None), "diskutil info returned no data"
    return parse_diskutil_info(text), None


# ---------------------------------------------------------------------------
# smartctl — opportunistic NVMe / ATA enrichment
# ---------------------------------------------------------------------------

def probe_smartctl(dev, smartctl_bin):
    """Run `smartctl -j -a` on dev. Return (data, detail).

    `data` is the parsed JSON document when usable, else None. `detail` is a
    short human-readable reason when data is unavailable or incomplete,
    including the common "USB bridge has no SMART pass-through" case.

    smartctl's exit status is a bitmask. Bit 0x02 means the device could not
    be opened and makes the data unavailable. Command/checksum failures
    (0x01/0x04) mark otherwise-usable data incomplete. Health bits remain in
    the document for extract_smartctl and assessment; historical error/self
    test bits do not discard usable current measurements.
    """
    if not smartctl_bin:
        return None, "smartctl not found on PATH or common install locations"
    try:
        result = subprocess.run(
            [smartctl_bin, "-j", "-a", "/dev/" + dev],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "smartctl failed to run: %s" % exc

    try:
        data = json.loads(result.stdout or "")
    except ValueError:
        return None, "smartctl output was not valid JSON"
    if not isinstance(data, dict):
        return None, "smartctl output was not a JSON object"

    meta = data.get("smartctl")
    meta = meta if isinstance(meta, dict) else {}
    messages = meta.get("messages")
    messages = messages if isinstance(messages, list) else []
    errors = []
    for message in messages:
        if not isinstance(message, dict) or message.get("severity") != "error":
            continue
        text = message.get("string")
        errors.append(text if isinstance(text, str)
                      else "smartctl reported an error")

    process_status = _integer(getattr(result, "returncode", None))
    if process_status is None or not 0 <= process_status <= 0xff:
        return None, "smartctl process returned an invalid exit status"

    details = []
    if "exit_status" not in meta:
        exit_status = process_status
        details.append("smartctl JSON omitted its exit_status")
    else:
        exit_status = meta.get("exit_status")
    if _integer(exit_status) is None or not 0 <= exit_status <= 0xff:
        return None, "smartctl JSON had an invalid exit_status"
    if exit_status != process_status:
        details.append(
            "smartctl JSON exit_status 0x%x disagreed with process status 0x%x"
            % (exit_status, process_status))
        exit_status |= process_status

    meta = dict(meta)
    meta["exit_status"] = exit_status
    data = dict(data)
    data["smartctl"] = meta

    if exit_status & 0x02:
        # e.g. "Smartctl open device: /dev/disk4 failed: Unknown USB bridge"
        return None, ("; ".join(errors) if errors else
                       "smartctl could not open or query the device")

    smart_support = data.get("smart_support")
    smart_support = smart_support if isinstance(smart_support, dict) else {}
    if smart_support.get("available") is False:
        return None, "device does not support SMART"

    extracted = extract_smartctl(data)
    health_fields = (
        "passed", "critical_warning", "percentage_used", "power_on_hours",
        "data_units_written", "available_spare", "available_spare_threshold",
        "media_errors", "temperature_c", "reallocated_sector_ct",
        "reallocated_event_count", "current_pending_sector",
        "offline_uncorrectable", "reported_uncorrectable",
    )
    has_usable_data = any(extracted[field] is not None
                          for field in health_fields)
    has_usable_data = (has_usable_data
                       or bool(extracted["ata_failing_attributes"])
                       or bool(extracted["ata_usage_attributes_now"])
                       or bool(extracted["ata_failed_attributes_past"]))
    if not has_usable_data:
        return None, ("; ".join(errors) if errors else
                       "smartctl returned no usable SMART data")

    if exit_status & 0x05:
        details.append("smartctl reported an incomplete command or checksum "
                       "failure (exit status 0x%x)" % exit_status)
    return data, "; ".join(details) if details else None


def extract_smartctl(data):
    """Pull the NVMe/ATA fields this scope cares about out of a parsed
    smartctl -j document. Pure function of already-successful `data` —
    availability/error handling belongs to probe_smartctl.

    Every field is None when the drive/protocol does not report it. In
    particular `data_units_written` (and its derived `tbw_tb`) use an
    explicit `is not None` check so a genuine 0 (a brand-new drive, or a
    counter that has been reset) is preserved instead of being reported as
    unknown.
    """
    data = data if isinstance(data, dict) else {}
    meta = data.get("smartctl")
    meta = meta if isinstance(meta, dict) else {}
    exit_status = _integer(meta.get("exit_status"))
    if exit_status is None:
        exit_status = 0
    smart_status = data.get("smart_status")
    smart_status = smart_status if isinstance(smart_status, dict) else {}
    nvme_log = data.get("nvme_smart_health_information_log")
    nvme_log = nvme_log if isinstance(nvme_log, dict) else {}
    ata = data.get("ata_smart_attributes")
    ata = ata if isinstance(ata, dict) else {}
    ata_table = ata.get("table")
    ata_table = ata_table if isinstance(ata_table, list) else []

    ata_by_name = {}
    failing_now = []
    usage_now = []
    failed_past = []
    for entry in ata_table:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        raw_data = entry.get("raw")
        raw_data = raw_data if isinstance(raw_data, dict) else {}
        raw = _number(raw_data.get("value"))
        if isinstance(name, str):
            ata_by_name[name] = {
                "raw": raw,
                "value": _number(entry.get("value")),
            }
            when_failed = entry.get("when_failed")
            if isinstance(when_failed, str):
                failure = when_failed.strip().lower().replace("_", " ")
                if failure and failure not in ("-", "never"):
                    if "now" in failure:
                        flags = entry.get("flags")
                        flags = flags if isinstance(flags, dict) else {}
                        if flags.get("prefailure") is True:
                            failing_now.append(name)
                        else:
                            usage_now.append(name)
                    else:
                        failed_past.append(name)

    def ata_raw(name):
        attribute = ata_by_name.get(name)
        return attribute["raw"] if attribute is not None else None

    def ata_value(name):
        attribute = ata_by_name.get(name)
        return attribute["value"] if attribute is not None else None

    power_on_hours = _number(nvme_log.get("power_on_hours"))
    if power_on_hours is None:
        power_on_time = data.get("power_on_time")
        power_on_time = power_on_time if isinstance(power_on_time, dict) else {}
        power_on_hours = _number(power_on_time.get("hours"))
    if power_on_hours is None:
        power_on_hours = ata_raw("Power_On_Hours")

    temperature_c = _number(nvme_log.get("temperature"))
    if temperature_c is None:
        temperature = data.get("temperature")
        temperature = temperature if isinstance(temperature, dict) else {}
        temperature_c = _number(temperature.get("current"))
    if temperature_c is None:
        temperature_c = ata_raw("Temperature_Celsius")
    if temperature_c is None:
        temperature_c = ata_raw("Airflow_Temperature_Cel")

    data_units_written = _number(nvme_log.get("data_units_written"))
    tbw_tb = None
    if data_units_written is not None:
        tbw_tb = _terabytes(data_units_written, NVME_DATA_UNIT_BYTES)
        if tbw_tb is None:
            data_units_written = None
    if tbw_tb is None:
        for name, bytes_per_unit in _ATA_WRITE_UNITS:
            tbw_tb = _terabytes(ata_raw(name), bytes_per_unit)
            if tbw_tb is not None:
                break

    percentage_used = _number(nvme_log.get("percentage_used"))
    if percentage_used is None:
        for name in _ATA_USED_LIFE_NAMES:
            percentage_used = ata_raw(name)
            if percentage_used is None:
                percentage_used = ata_value(name)
            if percentage_used is not None:
                break
    if percentage_used is None:
        for name in _ATA_REMAINING_LIFE_NAMES:
            remaining = ata_value(name)
            if remaining is not None and 0 <= remaining <= 100:
                percentage_used = 100 - remaining
                break

    passed = (smart_status.get("passed")
              if isinstance(smart_status.get("passed"), bool) else None)
    if exit_status & 0x08:
        passed = False

    return {
        "model": (data.get("model_name")
                  if isinstance(data.get("model_name"), str) else None),
        "passed": passed,
        "smartctl_exit_status": exit_status,
        "critical_warning": _integer(nvme_log.get("critical_warning")),
        "percentage_used": percentage_used,
        "power_on_hours": power_on_hours,
        "data_units_written": data_units_written,
        "tbw_tb": tbw_tb,
        "available_spare": _number(nvme_log.get("available_spare")),
        "available_spare_threshold": _number(
            nvme_log.get("available_spare_threshold")),
        "media_errors": _number(nvme_log.get("media_errors")),
        "temperature_c": temperature_c,
        "reallocated_sector_ct": ata_raw("Reallocated_Sector_Ct"),
        "reallocated_event_count": ata_raw("Reallocated_Event_Count"),
        "current_pending_sector": ata_raw("Current_Pending_Sector"),
        "offline_uncorrectable": ata_raw("Offline_Uncorrectable"),
        "reported_uncorrectable": ata_raw("Reported_Uncorrect"),
        "ata_failing_attributes": failing_now,
        "ata_usage_attributes_now": usage_now,
        "ata_failed_attributes_past": failed_past,
    }
