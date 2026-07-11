"""
stethoscope core.power — the battery-scope probes: ioreg, pmset, pmenergy,
and (root-only) powermetrics.

Same layering rule as core/vmstat.py (case 0007): this module is
libproc-free and owns every raw-text/plist macOS surface the battery scope
needs, returning only structures — scopes/battery.py never touches
subprocess or plistlib directly (ARCHITECTURE.md's "everything below the
surface layer returns structures, never text").

Four probes, in ARCHITECTURE.md §3's cost/privilege order:

  ioreg        `ioreg -rn AppleSmartBattery -a` — full plist, parsed with
               plistlib, never a text regex (Copilot review on the original
               battery PR: a "giant BatteryData blob" and nested
               PortControllerInfo/AdapterDetails structs sit alongside the
               scalar fields this scope needs; plistlib decodes the whole
               node correctly-typed and this module simply keeps only the
               top-level scalars). InstantAmperage/Amperage are signed
               two's-complement — ioreg has been observed rendering that as
               an unsigned 64-bit pattern (verified 18446744073709540666 ==
               -10950, ARCHITECTURE.md S4) even though on this machine/OS
               plistlib already yields the signed value; `signed64()`
               decodes both representations to the same signed result, so
               it is applied unconditionally, defensively.
  pmset        `pmset -g batt` — text only (no plist mode exists) for the
               two fields ioreg cannot give: the human charge/discharge
               *state* string and the time-remaining estimate. Charge
               *percentage* deliberately never comes from here — ioreg's
               CurrentCapacity/MaxCapacity ratio is the exact source (see
               battery_health() in scopes/battery.py).
  pmenergy     `/usr/share/pmenergy/*.plist` — Apple's Energy Impact
               coefficients, unitless weights keyed by the IORegistry
               board-id on Intel; Apple Silicon normally falls back to
               default.plist.
  powermetrics `powermetrics --samplers tasks --show-process-energy
               -f plist` — root-only, the `inspect` tier's richer
               per-process source. This sandbox has no root available to
               capture a real                sample, so parse_powermetrics_plist() handles its NUL-framed
               sample and keeps interval-total and per-second Energy Impact
               fields separate. It is exercised against synthetic plist
               bytes; unexpected shapes report explicit unavailability.

No third-party dependencies — system Python 3 + plistlib/subprocess only.
"""

import datetime
import math
import os
import plistlib
import re
import subprocess
from xml.parsers.expat import ExpatError

IOREG_BIN = "/usr/sbin/ioreg"
PMSET_BIN = "/usr/bin/pmset"
SYSCTL_BIN = "/usr/sbin/sysctl"
POWERMETRICS_BIN = "/usr/bin/powermetrics"
PMENERGY_DIR = "/usr/share/pmenergy"

_SUBPROCESS_TIMEOUT_S = 10
_PLIST_ERRORS = (
    plistlib.InvalidFileException,
    ExpatError,
    ValueError,
    TypeError,
)
_PLIST_IO_ERRORS = (OSError,) + _PLIST_ERRORS


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def signed64(value):
    """Decode a possibly-unsigned-64-bit rendering of a signed integer.

    ioreg has been observed emitting InstantAmperage as the raw unsigned
    64-bit bit pattern of a negative two's-complement value
    (ARCHITECTURE.md S4, verified: 18446744073709540666 -> -10950); other
    times (this machine, this OS) it already comes through signed. Both are
    idempotent under this decode, so it is always applied.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if value < -(1 << 63) or value >= (1 << 64):
        return None
    return value - (1 << 64) if value >= (1 << 63) else value


# ---------------------------------------------------------------------------
# ioreg — AppleSmartBattery, full plist
# ---------------------------------------------------------------------------

class IoregBattery:
    """Result of one `ioreg -rn AppleSmartBattery -a` probe.

    Exactly one of these three states holds, and callers must branch on it
    before touching `fields` (never infer probe failure from empty fields):

      ok=True,  present=False, fields=None   — no AppleSmartBattery node.
                A supported, expected state on desktops — not an error.
      ok=True,  present=True,  fields={...}  — success; fields holds every
                top-level scalar (int/bool/float/str) key ioreg returned,
                with nested dict/list/bytes blobs (BatteryData,
                PortControllerInfo, AdapterDetails, ...) dropped.
      ok=False, present=None,  fields=None   — the probe itself failed:
                `error` names why (command missing, timed out, or the
                output did not parse as a plist). Distinct from the
                "no battery" state above.
    """
    __slots__ = ("ok", "present", "fields", "error")

    def __init__(self, ok, present, fields, error):
        self.ok = ok
        self.present = present
        self.fields = fields
        self.error = error


def read_ioreg_battery():
    """Probe `ioreg -rn AppleSmartBattery -a`. See IoregBattery for the
    three-way (failure / absent / present) contract.
    """
    try:
        proc = subprocess.run(
            [IOREG_BIN, "-rn", "AppleSmartBattery", "-a"],
            capture_output=True, timeout=_SUBPROCESS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return IoregBattery(False, None, None, "ioreg_failed: %s" % exc)
    if proc.returncode != 0:
        return IoregBattery(
            False, None, None,
            "ioreg_failed: exit %d" % proc.returncode)
    try:
        nodes = plistlib.loads(proc.stdout) if proc.stdout.strip() else []
    except _PLIST_ERRORS as exc:
        return IoregBattery(False, None, None, "ioreg_parse_failed: %s" % exc)
    if not isinstance(nodes, list) or not nodes:
        # No AppleSmartBattery node at all — a desktop Mac. Supported state,
        # not a probe error.
        return IoregBattery(True, False, None, None)
    node = nodes[0]
    if not isinstance(node, dict):
        return IoregBattery(False, None, None, "ioreg_parse_failed: node not a dict")
    fields = {k: v for k, v in node.items()
              if isinstance(v, (int, float, str, bool))}
    return IoregBattery(True, True, fields, None)


# ---------------------------------------------------------------------------
# pmset — state/time-remaining text fields only
# ---------------------------------------------------------------------------

class PmsetBattery:
    """`pmset -g batt`'s state string and time-remaining estimate.

    ok=False means the command itself could not be run or produced no
    recognizable line; state/time_remaining are always None in that case.
    This is treated as a soft-degrade by callers (ioreg is authoritative
    for charge/health; pmset only supplements two display fields), never a
    probe-failure exit.
    """
    __slots__ = ("ok", "state", "time_remaining", "error")

    def __init__(self, ok, state, time_remaining, error):
        self.ok = ok
        self.state = state
        self.time_remaining = time_remaining
        self.error = error


def read_pmset_battery():
    """Probe `pmset -g batt` for the state string and time estimate only —
    never the charge percentage (ioreg's CurrentCapacity/MaxCapacity ratio
    is the exact source for that, computed by the caller).

    No plist mode exists for `pmset -g batt`; this is the one text-format
    parse in this module, deliberately narrowed to two fields.
    """
    try:
        proc = subprocess.run([PMSET_BIN, "-g", "batt"], capture_output=True,
                              text=True, timeout=_SUBPROCESS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return PmsetBattery(False, None, None, "pmset_failed: %s" % exc)
    if proc.returncode != 0:
        return PmsetBattery(False, None, None,
                            "pmset_failed: exit %d" % proc.returncode)
    # "<pct>%; <state>; <H:MM> remaining ..." or "<pct>%; <state>; (no
    # estimate) ..." when macOS has not yet calibrated an estimate.
    m = re.search(
        r"\d{1,3}%;\s*([A-Za-z][A-Za-z ]*?);\s*(?:(\d+:\d{2})\s+remaining|\(no estimate\))",
        proc.stdout)
    if not m:
        return PmsetBattery(False, None, None, "pmset_unparsed")
    return PmsetBattery(True, m.group(1).strip(), m.group(2), None)


def read_last_power_transition():
    """Return (state, epoch, error) for the latest AC/battery transition."""
    try:
        proc = subprocess.run(
            [PMSET_BIN, "-g", "log"], capture_output=True, text=True,
            timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, None, "pmset_log_failed: %s" % exc
    if proc.returncode != 0:
        return None, None, "pmset_log_failed: exit %d" % proc.returncode

    previous = None
    latest_state = None
    latest_epoch = None
    pattern = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}).*"
        r"Using\s+(AC|Batt)\s*\(")
    for line in proc.stdout.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        state = "ac" if match.group(2) == "AC" else "battery"
        if state == previous:
            continue
        try:
            timestamp = datetime.datetime.strptime(
                match.group(1), "%Y-%m-%d %H:%M:%S %z").timestamp()
        except ValueError:
            continue
        previous = state
        latest_state = state
        latest_epoch = timestamp
    if latest_state is None:
        return None, None, "pmset_log_unparsed"
    return latest_state, latest_epoch, None


# ---------------------------------------------------------------------------
# pmenergy — Energy Impact coefficients
# ---------------------------------------------------------------------------
def read_board_id():
    """Return IOPlatformExpertDevice's board-id, or None when unavailable."""
    try:
        proc = subprocess.run(
            [IOREG_BIN, "-rd1", "-c", "IOPlatformExpertDevice", "-a"],
            capture_output=True, timeout=_SUBPROCESS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        nodes = plistlib.loads(proc.stdout)
    except _PLIST_ERRORS:
        return None
    if not isinstance(nodes, list) or not nodes or not isinstance(nodes[0], dict):
        return None
    board_id = nodes[0].get("board-id")
    if isinstance(board_id, bytes):
        board_id = board_id.decode("ascii", "replace").rstrip("\0")
    return board_id if isinstance(board_id, str) and board_id else None


def read_boot_session_uuid():
    """Return macOS's identifier for the current boot session."""
    try:
        proc = subprocess.run(
            [SYSCTL_BIN, "-n", "kern.bootsessionuuid"],
            capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    if not re.fullmatch(
            r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}",
            value):
        return None
    return value.lower()

def pmenergy_coefficients(pmenergy_dir=PMENERGY_DIR):
    """Return (coefficients, chosen_path, error).

    Intel coefficient files are named for IOPlatformExpertDevice's board-id,
    not `sysctl hw.model`. Apple Silicon normally has no matching plist and
    therefore uses default.plist.

    On success coefficients is the dict under the plist's
    "energy_constants" key (kcpu_time, kcpu_wakeups, kdiskio_bytesread,
    kdiskio_byteswritten, ... — unitless weights, never watts). On failure
    coefficients is None and error names why.
    """
    try:
        plists = sorted(os.listdir(pmenergy_dir))
    except OSError as exc:
        return None, None, "pmenergy_dir_unavailable: %s" % exc
    board_id = read_board_id()
    match = next(
        (name for name in plists
         if board_id and os.path.splitext(name)[0] == board_id),
        None)
    chosen = match or ("default.plist" if "default.plist" in plists else None)
    if not chosen:
        return None, None, "no_matching_plist"
    path = os.path.join(pmenergy_dir, chosen)
    try:
        with open(path, "rb") as fh:
            data = plistlib.load(fh)
    except _PLIST_IO_ERRORS as exc:
        return None, None, "pmenergy_parse_failed: %s" % exc
    coefficients = data.get("energy_constants") if isinstance(data, dict) else None
    if not isinstance(coefficients, dict) or not coefficients:
        return None, None, "no_energy_constants"
    if any(_number(value) is None for value in coefficients.values()):
        return None, None, "invalid_energy_constants"
    return dict(coefficients), path, None


# ---------------------------------------------------------------------------
# battery flow — signed watts, discharge-vs-charge labeled by the caller
# ---------------------------------------------------------------------------

def battery_flow_watts(voltage_mv, current_ma):
    """Signed Voltage(mV) * InstantAmperage(mA) / 1e6 -> watts.

    This is **battery flow**, not system draw: it equals system power draw
    only while discharging (negative here); on AC it is charging power,
    ~0 W once topped off (ARCHITECTURE.md S4). None if either input is
    missing. `current_ma` must already be signed (see signed64()).
    """
    voltage_mv = _number(voltage_mv)
    current_ma = _number(current_ma)
    if voltage_mv is None or current_ma is None:
        return None
    try:
        watts = voltage_mv * current_ma / 1e6
    except OverflowError:
        return None
    return watts if math.isfinite(watts) else None


# ---------------------------------------------------------------------------
# powermetrics — root-only per-process detail, the `inspect` tier
# ---------------------------------------------------------------------------

def parse_powermetrics_plist(data):
    """Parse one `powermetrics -f plist` sample's bytes.

    Returns (tasks, error). Each task preserves the normalized
    `energy_impact_per_s` and interval-total `energy_impact` separately.
    Both are unitless and never presented as watts. This sandbox has no root
    available to confirm the field names against a live sample, so unexpected
    scalar types are skipped rather than relabeled. error is None on success;
    otherwise a machine-readable reason ("parse_failed", "not_a_dict",
    "no_tasks_field") and tasks is None — explicit unavailability, never a
    fabricated empty-but-successful result.
    """
    if not isinstance(data, bytes):
        return None, "parse_failed: output was not bytes"
    samples = [sample for sample in data.split(b"\0") if sample.strip()]
    if len(samples) != 1:
        return None, "sample_count_%d" % len(samples)
    try:
        obj = plistlib.loads(samples[0])
    except _PLIST_ERRORS as exc:
        return None, "parse_failed: %s" % exc
    if not isinstance(obj, dict):
        return None, "not_a_dict"
    tasks = obj.get("tasks")
    if not isinstance(tasks, list):
        return None, "no_tasks_field"
    rows = []
    for entry in tasks:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("pid")
        name = entry.get("name")
        if (not isinstance(pid, int) or isinstance(pid, bool) or pid < -1
                or not isinstance(name, str) or not name):
            continue
        total = entry.get("energy_impact")
        rate = entry.get("energy_impact_per_s")
        total = None if total is None else _number(total)
        rate = None if rate is None else _number(rate)
        if ((entry.get("energy_impact") is not None and total is None)
                or (entry.get("energy_impact_per_s") is not None
                    and rate is None)):
            continue
        if ((total is not None and total < 0)
                or (rate is not None and rate < 0)):
            continue
        rows.append({
            "pid": pid,
            "name": name,
            "energy_impact_per_s": rate,
            "energy_impact_total": total,
        })
    return rows, None


def read_powermetrics_tasks(sample_ms=1000):
    """Root-only single-sample per-process energy detail.

    Returns (tasks, error): tasks is None with a machine-readable error
    ("root_required", "powermetrics_missing", "timeout", "probe_failed",
    or one of parse_powermetrics_plist's reasons) whenever the richer tier
    is unavailable — never a fabricated/empty-but-successful reading.
    """
    cmd = [POWERMETRICS_BIN, "-n", "1", "-i", str(int(sample_ms)),
           "--samplers", "tasks", "--show-process-energy", "-f", "plist"]
    timeout_s = max(_SUBPROCESS_TIMEOUT_S, sample_ms / 1000.0 + 10)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
    except FileNotFoundError:
        return None, "powermetrics_missing"
    except OSError as exc:
        return None, "probe_failed: %s" % exc
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except subprocess.SubprocessError as exc:
        return None, "probe_failed: %s" % exc
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace").lower()
        if "superuser" in stderr or "root" in stderr or "permission" in stderr:
            return None, "root_required"
        return None, "probe_failed: exit %d" % proc.returncode
    return parse_powermetrics_plist(proc.stdout)
