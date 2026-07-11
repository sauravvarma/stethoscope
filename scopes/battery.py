#!/usr/bin/env python3
"""
stethoscope battery — what is draining the battery, and how healthy is it.

  battery health        charge, cycle count, capacity vs design, condition
  battery top            who is drawing power now (energy score + live watts)
  battery drainers       cumulative drain since you unplugged
  battery inspect         richer per-process detail via powermetrics (sudo)

`health` (issue #7) reads the battery gauge from `ioreg -rn AppleSmartBattery
-a` (full plist, never a text regex) plus `pmset -g batt` for the state
string and time estimate — no sudo, exact numbers: state of charge (from
ioreg's CurrentCapacity/MaxCapacity ratio, never pmset), cycle count, health
% (max capacity vs design), temperature, and condition.

`top` (issue #6) and `drainers` (issue #8) attribute power to processes.
Two currencies, kept apart everywhere (ARCHITECTURE.md §4-§5): where rusage
flavor 6 exists, `ri_energy_nj` deltas give real per-process watts —
`energy_rate_watts` / cumulative `energy_joules_since`, never
`ri_billed_energy` (its deltas are frozen at polling cadence, casebook
0001.10). Everywhere else — and always, alongside the real number where it
exists — `energy_score_per_s` / cumulative `energy_score_total` is an
explicitly unitless ranking: Apple's own Energy Impact formula (CPU seconds,
pkg-idle wakeups, and disk bytes, weighted by `/usr/share/pmenergy`'s
per-board coefficients), never rendered as watts. `top` scores the current
interval as a rate; `drainers` scores cumulative impact since the last
unplug (baseline persisted under `~/.stethoscope/`, atomic + schema
validated, so a corrupt file resets explicitly instead of crashing or
silently reporting zero drain).

`inspect` (the issues' richer tier) is root-only: a single `powermetrics`
sample gives Apple's per-process Energy Impact fields, richer than our own
model (folds in GPU/timer coalescing our formula cannot see) but still
unitless. Per-second and sample-total fields remain separate. The parser is
defensive and reports explicit unavailability whenever the expected shape
does not come back (core/power.py).

No third-party dependencies — system Python 3 + core/rusage.py,
core/power.py, core/cli.py, core/schema.py.
"""

import json
import math
import os
import pwd
import secrets
import signal
import stat
import sys
import time
from collections import namedtuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import cli, power, rusage, schema

list_pids = rusage.list_pids
proc_name = rusage.proc_name

_QOS_CLASSES = (
    "default",
    "maintenance",
    "background",
    "utility",
    "legacy",
    "user_initiated",
    "user_interactive",
)


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------

CLEAR = "\033[2J\033[H"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def _fmt(value, spec):
    """Render value with spec, or "-" when it is None. Never %d/%.Nf a
    None — the one rule every health/top/drainers renderer below follows.
    """
    return "-" if value is None else (spec % value)


def watts_str(watts):
    return "-" if watts is None else "%.2fW" % watts


def score_str(score):
    return "-" if score is None else "%.2f" % score


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _boolean(value):
    return value if isinstance(value, bool) else None


def warn_if_not_root():
    if not cli.is_root():
        sys.stderr.write(
            DIM + "note: not running as root — energy attribution for other "
            "users' processes is hidden. Re-run with sudo for full coverage.\n"
            + RESET)


def _visibility():
    partial = not cli.is_root()
    return partial, ["not_root"] if partial else []


# ---------------------------------------------------------------------------
# battery health (core.power's ioreg + pmset probes)
# ---------------------------------------------------------------------------

_HEALTH_FIELDS = (
    "present", "probe_error", "pmset_error", "charge_pct", "state",
    "time_remaining",
    "cycle_count", "health_pct", "condition", "capacities", "temperature_c",
    "charging", "external_connected", "fully_charged", "voltage_mv",
    "current_ma", "battery_flow_watts",
)


def _empty_health(present, probe_error):
    h = dict.fromkeys(_HEALTH_FIELDS)
    h["present"] = present
    h["probe_error"] = probe_error
    return h


def battery_health():
    """A structured battery health readout — stable fields, always present,
    always null (never a fabricated number) where a value is unknown.

    `present` is a tri-state: True (battery found), False (no
    AppleSmartBattery node — a desktop Mac, a supported state), or None
    (the ioreg probe itself failed — `probe_error` names why). Callers
    must check `probe_error` before treating `present: False` as "no
    battery": a probe failure is never presented as an empty-but-clean
    "no battery" reading.
    """
    ioreg = power.read_ioreg_battery()
    if not ioreg.ok:
        return _empty_health(None, ioreg.error)
    if not ioreg.present:
        return _empty_health(False, None)

    fields = ioreg.fields
    design = _number(fields.get("DesignCapacity"))
    raw_max = _number(fields.get("AppleRawMaxCapacity"))
    if raw_max is None:
        raw_max = _number(fields.get("NominalChargeCapacity"))
    health_ratio = None
    health_pct = None
    if (design is not None and design > 0 and raw_max is not None
            and raw_max >= 0):
        try:
            candidate = raw_max / design * 100
        except OverflowError:
            candidate = None
        if candidate is not None and math.isfinite(candidate):
            health_ratio = candidate
            health_pct = round(candidate, 1)
    permanent_failure = _number(fields.get("PermanentFailureStatus"))
    failed = permanent_failure is not None and permanent_failure != 0
    reported_condition = fields.get("BatteryHealthCondition")
    if not isinstance(reported_condition, str):
        reported_condition = fields.get("BatteryHealth")
    if not isinstance(reported_condition, str):
        reported_condition = None
    reported_key = (
        reported_condition.strip().lower()
        if reported_condition is not None else None)
    reported_degraded = reported_key not in (None, "", "good", "normal", "unknown")
    if (failed or reported_degraded
            or (health_ratio is not None and health_ratio < 80)):
        condition = "Service Recommended"
    elif health_pct is not None or reported_key in ("good", "normal"):
        condition = "Normal"
    else:
        condition = None

    current_capacity = _number(fields.get("CurrentCapacity"))
    max_capacity = _number(fields.get("MaxCapacity"))
    charge_pct = None
    if (current_capacity is not None and max_capacity is not None
            and current_capacity >= 0 and max_capacity > 0):
        try:
            candidate = current_capacity / max_capacity * 100
        except OverflowError:
            candidate = None
        if candidate is not None and math.isfinite(candidate):
            charge_pct = round(min(100.0, candidate), 1)

    temp_raw = _number(fields.get("Temperature"))
    try:
        temperature_c = (
            round(temp_raw / 100.0, 1) if temp_raw is not None else None)
    except OverflowError:
        temperature_c = None

    voltage_mv = _number(fields.get("Voltage"))
    current_ma = fields.get("InstantAmperage")
    if current_ma is None:
        current_ma = fields.get("Amperage")
    current_ma = power.signed64(current_ma) if current_ma is not None else None
    flow_watts = power.battery_flow_watts(voltage_mv, current_ma)

    pmset = power.read_pmset_battery()

    h = _empty_health(True, None)
    h.update({
        "charge_pct": charge_pct,
        "state": pmset.state if pmset.ok else None,
        "time_remaining": pmset.time_remaining if pmset.ok else None,
        "pmset_error": (None if pmset.ok
                        else (pmset.error or "pmset_unavailable")),
        "cycle_count": _number(fields.get("CycleCount")),
        "health_pct": health_pct,
        "condition": condition,
        "capacities": {"design_mah": design, "max_mah": raw_max},
        "temperature_c": temperature_c,
        "charging": _boolean(fields.get("IsCharging")),
        "external_connected": _boolean(fields.get("ExternalConnected")),
        "fully_charged": _boolean(fields.get("FullyCharged")),
        "voltage_mv": voltage_mv,
        "current_ma": current_ma,
        "battery_flow_watts": flow_watts,
    })
    return h


def _render_health_human(h):
    if h["probe_error"]:
        sys.stderr.write(
            DIM + "battery health probe failed: %s\n" % h["probe_error"] + RESET)
        return
    if not h["present"]:
        print(DIM + "no battery detected (desktop Mac?)." + RESET)
        return
    print(BOLD + "stethoscope battery health" + RESET)
    line = "charge %s%%" % _fmt(h["charge_pct"], "%.0f")
    if h["state"]:
        line += "  \u00b7  %s" % h["state"]
    if h["time_remaining"]:
        line += "  \u00b7  %s remaining" % h["time_remaining"]
    print(line)
    cond = h["condition"] or "-"
    cond_str = (BOLD + cond + RESET) if cond not in ("-", "Normal") else cond
    print("health %s%%  \u00b7  %s cycles  \u00b7  condition %s" % (
        _fmt(h["health_pct"], "%.1f"), _fmt(h["cycle_count"], "%d"), cond_str))
    caps = h["capacities"] or {}
    print(DIM + "capacity %s / %s mAh (max / design)  \u00b7  %s\u00b0C  \u00b7  "
          "battery flow %s" % (
              _fmt(caps.get("max_mah"), "%d"), _fmt(caps.get("design_mah"), "%d"),
              _fmt(h["temperature_c"], "%.1f"), watts_str(h["battery_flow_watts"]))
          + RESET)
    if h.get("pmset_error"):
        print(DIM + "state/time unavailable: %s" % h["pmset_error"] + RESET)


def cmd_health(options):
    document, exit_code = health_result()
    if options.json:
        cli.emit_json(document)
    else:
        _render_health_human(document)
    return exit_code


def health_result():
    """Return the stable battery-health document and command exit code."""
    health = battery_health()
    reasons = ["pmset_unavailable"] if health.get("pmset_error") else []
    document = schema.document(
        "battery", "health", partial=bool(reasons),
        partial_reasons=reasons, **health)
    if health["probe_error"]:
        return document, cli.EXIT_ERROR
    if health["condition"] == "Service Recommended":
        return document, cli.EXIT_FINDINGS
    return document, cli.EXIT_OK


# ---------------------------------------------------------------------------
# energy scoring — Apple's Energy Impact formula, explicitly unitless
# ---------------------------------------------------------------------------

def _energy_score(coeffs, cpu_seconds, pkg_idle_wakeups, diskio_read_bytes,
                   diskio_write_bytes, qos_cpu_seconds=None):
    """Apple's Energy Impact formula over the vitals rusage can supply:
    CPU seconds, pkg-idle wakeups (the "hidden battery killer" —
    ARCHITECTURE.md §4 — never interrupt wakeups, kept as separate context),
    and disk bytes. `coeffs` is a pmenergy plist's "energy_constants" dict
    (unitless weights); None means the coefficients could not be loaded, in
    which case the score itself is None — never a fabricated zero that
    would look like "no drain" (core.power.pmenergy_coefficients() names
    why in that case).

    knetwork_* and kgpu_time are in every pmenergy plist but rusage has no
    network-byte or GPU-time counters to feed them (core/validate.py's
    check_pmenergy confirms this on every machine it runs on) — omitted
    here rather than silently multiplied by zero-valued inputs, which would
    look load-bearing.
    """
    if not isinstance(coeffs, dict):
        return None
    cpu_seconds = _number(cpu_seconds)
    wakeups = _number(pkg_idle_wakeups)
    read_bytes = _number(diskio_read_bytes)
    write_bytes = _number(diskio_write_bytes)
    cpu_weight = _number(coeffs.get("kcpu_time"))
    wake_weight = _number(coeffs.get("kcpu_wakeups"))
    read_weight = _number(coeffs.get("kdiskio_bytesread"))
    write_weight = _number(coeffs.get("kdiskio_byteswritten"))
    values = (
        cpu_seconds, wakeups, read_bytes, write_bytes,
        cpu_weight, wake_weight, read_weight, write_weight,
    )
    if any(value is None for value in values):
        return None
    try:
        cpu_score = cpu_weight * cpu_seconds
        if qos_cpu_seconds is not None:
            if not isinstance(qos_cpu_seconds, dict):
                return None
            qos_values = {
                name: _number(qos_cpu_seconds.get(name, 0))
                for name in _QOS_CLASSES
            }
            if any(value is None for value in qos_values.values()):
                return None
            qos_total = sum(qos_values.values())
            unclassified = max(0.0, cpu_seconds - qos_total)
            cpu_score = cpu_weight * unclassified
            for name, seconds in qos_values.items():
                weight = (cpu_weight if name == "maintenance" else
                          _number(coeffs.get("kqos_" + name)))
                if weight is None:
                    weight = cpu_weight
                cpu_score += weight * seconds
        score = (
            cpu_score
            + wake_weight * wakeups
            + read_weight * read_bytes
            + write_weight * write_bytes
        )
    except OverflowError:
        return None
    return score if math.isfinite(score) else None


# ---------------------------------------------------------------------------
# battery top — live rate: real watts (V6) + unitless score, kept apart
# ---------------------------------------------------------------------------

def snapshot_power():
    """{(pid, start_abstime): sample} for every accessible process, via
    core.rusage.proc_power_sample — one struct read per pid (identity-keyed,
    so a reused pid cannot inherit a dead process's counters, S10).
    """
    snap = {}
    for pid in list_pids():
        sample = rusage.proc_power_sample(pid)
        if sample is not None:
            snap[sample["identity"]] = sample
    return snap


# energy_share_pct is None only when coefficients themselves are
# unavailable; pkg/interrupt wakeup rates stay separate fields (S8,
# casebook 0004) — interrupt wakeups are context only, never folded into
# the score.
BatteryTopRow = namedtuple("BatteryTopRow", (
    "pid", "name", "cpu_pct", "energy_rate_watts", "energy_score_per_s",
    "energy_share_pct", "pkg_idle_wakeups_per_s", "interrupt_wakeups_per_s",
    "diskio_read_bps", "diskio_write_bps",
))

BatterySysTotals = namedtuple("BatterySysTotals", (
    "cpu_pct", "energy_rate_watts", "energy_score_per_s",
    "pkg_idle_wakeups_per_s", "interrupt_wakeups_per_s",
))


def _diff_power(prev, cur, dt, coeffs):
    """Diff two snapshots over `dt` seconds. Returns (rows, sys_totals),
    unsorted; rank_top() sorts. A key absent from `prev` (new or reused
    pid, S10) is baselined to itself, contributing no rate its first
    interval — same rule as scopes/cpu.py's `_diff_cpu`.
    """
    dt = _number(dt)
    if dt is None or dt <= 0:
        dt = 1.0
    have_coeffs = _energy_score(coeffs, 0, 0, 0, 0) is not None
    rows = []
    sys_cpu = 0.0
    sys_watts = None
    sys_score = 0.0 if have_coeffs else None
    sys_pkg = 0.0
    sys_intr = 0.0
    for identity, s in cur.items():
        p = prev.get(identity, s)
        d_user = max(0, s["cpu_user_ns"] - p["cpu_user_ns"])
        d_sys = max(0, s["cpu_system_ns"] - p["cpu_system_ns"])
        cpu_ns = d_user + d_sys
        cpu_pct = cpu_ns / (dt * 1e9) * 100.0
        d_pkg = max(0, s["pkg_idle_wakeups"] - p["pkg_idle_wakeups"]) / dt
        d_intr = max(0, s["interrupt_wakeups"] - p["interrupt_wakeups"]) / dt
        d_read = max(0, s["diskio_bytes_read"] - p["diskio_bytes_read"]) / dt
        d_write = max(0, s["diskio_bytes_written"] - p["diskio_bytes_written"]) / dt
        qos_rates = {
            name: max(
                0,
                s.get("qos_cpu_ns", {}).get(name, 0)
                - p.get("qos_cpu_ns", {}).get(name, 0),
            ) / 1e9 / dt
            for name in _QOS_CLASSES
        }
        watts = None
        if s["energy_nj"] is not None and p["energy_nj"] is not None:
            watts = max(0, s["energy_nj"] - p["energy_nj"]) / dt / 1e9
            sys_watts = (sys_watts or 0.0) + watts
        score = _energy_score(
            coeffs, cpu_ns / 1e9 / dt, d_pkg, d_read, d_write,
            qos_cpu_seconds=qos_rates)
        sys_cpu += cpu_pct
        sys_pkg += d_pkg
        sys_intr += d_intr
        if have_coeffs and score is not None:
            sys_score += score
        if (cpu_ns > 0 or (watts or 0) > 0 or (score or 0) > 0
                or d_pkg > 0 or d_intr > 0 or d_read > 0 or d_write > 0):
            rows.append((identity[0], proc_name(identity[0], identity), cpu_pct,
                        watts, score, d_pkg, d_intr, d_read, d_write))

    out_rows = []
    for pid, name, cpu_pct, watts, score, d_pkg, d_intr, d_read, d_write in rows:
        if not have_coeffs:
            share = None
        elif sys_score and sys_score > 0:
            share = score / sys_score * 100.0
        else:
            share = 0.0
        out_rows.append(BatteryTopRow(
            pid, name, cpu_pct, watts, score, share, d_pkg, d_intr, d_read, d_write))
    sys_totals = BatterySysTotals(sys_cpu, sys_watts, sys_score, sys_pkg, sys_intr)
    return out_rows, sys_totals


def rank_top(prev, cur, dt, coeffs):
    """Diff two snapshots, ranked by the unitless energy score descending —
    the "default mode" ranking of ARCHITECTURE.md §5, which works
    everywhere/every cadence/every power state, unlike the real-watts
    column which is None wherever rusage flavor 6 is unavailable.
    """
    rows, sys_totals = _diff_power(prev, cur, dt, coeffs)
    rows.sort(key=lambda r: (
        -(r.energy_score_per_s or 0.0),
        -(r.energy_rate_watts or 0.0),
        -r.cpu_pct,
        r.pid,
    ))
    return rows, sys_totals


def power_model():
    """Return the public pmenergy model consumed by CLI and TUI surfaces.

    Keeping this adapter here prevents presentation layers from reaching
    through the battery scope into ``core.power``.
    """
    coefficients, source, error = power.pmenergy_coefficients()
    return {
        "coefficients": coefficients,
        "source": source,
        "error": error,
        "available": coefficients is not None,
    }


def _top_entry(row):
    return {
        "pid": row.pid,
        "name": row.name,
        "cpu_pct": row.cpu_pct,
        "energy_rate_watts": row.energy_rate_watts,
        "energy_score_per_s": row.energy_score_per_s,
        "energy_share_pct": row.energy_share_pct,
        "pkg_idle_wakeups_per_s": row.pkg_idle_wakeups_per_s,
        "interrupt_wakeups_per_s": row.interrupt_wakeups_per_s,
        "diskio_bytes_read_per_s": row.diskio_read_bps,
        "diskio_bytes_written_per_s": row.diskio_write_bps,
    }


def _top_document(rows, sys_totals, limit, pmenergy_source, extra_reasons):
    partial, reasons = _visibility()
    reasons = reasons + list(extra_reasons)
    partial = partial or bool(extra_reasons)
    return schema.document(
        "battery", "top", partial=partial, partial_reasons=reasons,
        pmenergy_source=pmenergy_source,
        system={
            "cpu_pct": sys_totals.cpu_pct,
            "energy_rate_watts": sys_totals.energy_rate_watts,
            "energy_score_per_s": sys_totals.energy_score_per_s,
            "pkg_idle_wakeups_per_s": sys_totals.pkg_idle_wakeups_per_s,
            "interrupt_wakeups_per_s": sys_totals.interrupt_wakeups_per_s,
        },
        processes=[_top_entry(row) for row in rows[:limit]])


def top_result(prev, cur, dt, limit, model=None, ranked=None):
    """Return one structured battery-top interval and its exit code."""
    model = model or power_model()
    coefficients = model["coefficients"]
    reasons = (() if coefficients is not None
               else ("no_pmenergy_coefficients",))
    rows, sys_totals = ranked or rank_top(prev, cur, dt, coefficients)
    return (_top_document(
        rows, sys_totals, limit, model["source"], reasons), cli.EXIT_OK)


def _top_frame(rows, sys_totals, interval, limit, styled=True):
    clear = CLEAR if styled else ""
    bold = BOLD if styled else ""
    dim = DIM if styled else ""
    reset = RESET if styled else ""
    out = [clear]
    out.append(bold + "stethoscope battery top \u00b7 energy impact \u00b7 %s \u00b7 "
               "refresh %.0fs" % (time.strftime("%H:%M:%S"), interval) + reset)
    out.append(dim + "SCORE is Apple's unitless Energy Impact formula (never "
               "watts); RWATTS is real live power where the OS supplies it "
               "(ctrl-c to quit)" + reset)
    out.append("")
    out.append(bold + "%7s  %-24s %7s %8s %9s %7s %8s %8s" %
               ("PID", "COMMAND", "%CPU", "RWATTS", "SCORE", "SHARE%",
                "PKG/s", "INTR/s") + reset)
    if not rows:
        out.append(dim + "  (no activity this interval)" + reset)
    for row in rows[:limit]:
        out.append("%7d  %-24s %7.1f %8s %9s %7s %8.1f %8.1f" % (
            row.pid, cli.safe_text(row.name)[:24], row.cpu_pct,
            watts_str(row.energy_rate_watts),
            score_str(row.energy_score_per_s),
            "-" if row.energy_share_pct is None else "%.1f" % row.energy_share_pct,
            row.pkg_idle_wakeups_per_s, row.interrupt_wakeups_per_s))
    return "\n".join(out) + "\n"


def cmd_top(options):
    """Live per-process energy attribution. Honors --json/--once/--duration
    /--interval/--limit (core.cli's shared agent contract).
    """
    if not options.json:
        warn_if_not_root()
    model = power_model()
    coeffs = model["coefficients"]

    prev = snapshot_power()
    prev_t = time.monotonic()
    started = prev_t
    while True:
        time.sleep(options.interval)
        cur = snapshot_power()
        now = time.monotonic()
        rows, sys_totals = rank_top(prev, cur, now - prev_t, coeffs)
        document, exit_code = top_result(
            prev, cur, now - prev_t, options.limit, model=model,
            ranked=(rows, sys_totals))
        if options.json:
            cli.emit_json(document)
        else:
            sys.stdout.write(_top_frame(
                rows, sys_totals, options.interval, options.limit,
                styled=sys.stdout.isatty()))
            sys.stdout.flush()
        if options.once or (
                options.duration is not None and now - started >= options.duration):
            return exit_code
        prev, prev_t = cur, now


# ---------------------------------------------------------------------------
# battery drainers — cumulative since unplug, persisted baseline
# ---------------------------------------------------------------------------

BASELINE_SCHEMA = "battery-baseline/1"
BASELINE_FILENAME = "battery_baseline.json"


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _reject_json_constant(value):
    raise ValueError("non-finite JSON value: %s" % value)


def _effective_home():
    """The effective sudo user's home — never root's, when invoked via
    plain `sudo` (ARCHITECTURE.md §6.1 / finding S6): resolve SUDO_USER's
    home instead of os.path.expanduser("~"), which would resolve to
    /var/root and split the baseline store from the unprivileged user who
    reads it on every other invocation.
    """
    if os.geteuid() == 0:
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user:
            try:
                return pwd.getpwnam(sudo_user).pw_dir
            except KeyError as exc:
                raise OSError("cannot resolve SUDO_USER %r" % sudo_user) from exc
    return os.path.expanduser("~")


def _state_dir():
    return os.path.join(_effective_home(), ".stethoscope")


def _effective_ids():
    if os.geteuid() != 0:
        return None
    sudo_user = os.environ.get("SUDO_USER")
    if not sudo_user:
        return None
    try:
        entry = pwd.getpwnam(sudo_user)
    except KeyError as exc:
        raise OSError("cannot resolve SUDO_USER %r" % sudo_user) from exc
    return entry.pw_uid, entry.pw_gid


def _open_state_directory(directory, create):
    created = False
    if create:
        try:
            os.mkdir(directory, 0o700)
            created = True
        except FileExistsError:
            pass
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(directory, flags)
    info = os.fstat(directory_fd)
    if not stat.S_ISDIR(info.st_mode):
        os.close(directory_fd)
        raise OSError("battery state path is not a directory")
    owner = _effective_ids()
    try:
        if owner is not None and (created or info.st_uid == 0):
            os.fchown(directory_fd, owner[0], owner[1])
    except OSError:
        os.close(directory_fd)
        raise
    return directory_fd


def _validate_baseline(obj):
    """None if `obj` is a structurally valid baseline document; otherwise a
    machine-readable reason. Guards every field a loader touches — a
    malformed-but-valid-JSON baseline (wrong top-level type, missing keys,
    wrong-typed process entries) must reset explicitly, never crash and
    never look like a clean baseline (Copilot #42 finding on the original
    battery PR).
    """
    if not isinstance(obj, dict):
        return "not_an_object"
    if obj.get("schema") != BASELINE_SCHEMA:
        return "schema_mismatch"
    saved_at = _number(obj.get("saved_at"))
    if (saved_at is None or saved_at < 0
            or saved_at > time.time() + 86400):
        return "invalid_saved_at"
    sample_abstime = obj.get("sample_abstime")
    if (not _is_int(sample_abstime) or sample_abstime < 0
            or sample_abstime > (1 << 64) - 1):
        return "invalid_sample_abstime"
    if not isinstance(obj.get("on_ac"), bool):
        return "invalid_on_ac"
    if not isinstance(obj.get("root"), bool):
        return "invalid_visibility"
    boot_session_uuid = obj.get("boot_session_uuid")
    if (not isinstance(boot_session_uuid, str)
            or not boot_session_uuid):
        return "invalid_boot_session"
    unplugged_at = obj.get("unplugged_at")
    if unplugged_at is not None:
        unplugged_at = _number(unplugged_at)
        if (unplugged_at is None or unplugged_at < 0
                or unplugged_at > time.time() + 86400):
            return "invalid_unplugged_at"
    charge_pct = obj.get("charge_pct")
    if charge_pct is not None:
        charge_pct = _number(charge_pct)
        if charge_pct is None or not 0 <= charge_pct <= 100:
            return "invalid_charge_pct"
    processes = obj.get("processes")
    if not isinstance(processes, dict):
        return "invalid_processes_field"
    for pid, entry in processes.items():
        if not isinstance(pid, str) or not pid.isdigit() or int(pid) <= 0:
            return "invalid_process_entry"
        if not isinstance(entry, dict):
            return "invalid_process_entry"
        for field in ("start_ticks", "cpu_ns", "pkg_idle_wakeups",
                      "diskio_bytes_read", "diskio_bytes_written"):
            if not _is_int(entry.get(field)):
                return "invalid_process_entry"
            if entry[field] < 0:
                return "invalid_process_entry"
            if entry[field] > (1 << 64) - 1:
                return "invalid_process_entry"
        qos_cpu_ns = entry.get("qos_cpu_ns")
        if (not isinstance(qos_cpu_ns, dict)
                or set(qos_cpu_ns) != set(_QOS_CLASSES)):
            return "invalid_process_entry"
        if any(
                not _is_int(qos_cpu_ns[name])
                or qos_cpu_ns[name] < 0
                or qos_cpu_ns[name] > (1 << 64) - 1
                for name in _QOS_CLASSES):
            return "invalid_process_entry"
        if entry["start_ticks"] > sample_abstime:
            return "invalid_process_entry"
        energy = entry.get("energy_nj")
        if (energy is not None
                and (not _is_int(energy) or energy < 0
                     or energy > (1 << 64) - 1)):
            return "invalid_process_entry"
    return None


def _load_baseline(path):
    """(baseline, reset_reason). reset_reason is None only when a
    structurally valid baseline was loaded; otherwise names why the
    baseline is being treated as absent — "no_baseline" (first run,
    nothing on disk yet) is a normal state, everything else is a malformed
    or truncated file that must reset rather than crash or silently diff
    against garbage.
    """
    directory = os.path.dirname(path)
    filename = os.path.basename(path)
    directory_fd = None
    try:
        directory_fd = _open_state_directory(directory, create=False)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(filename, flags, dir_fd=directory_fd)
        with os.fdopen(file_fd) as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None, "no_baseline"
    except (OSError, UnicodeError):
        return None, "baseline_read_failed"
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    try:
        obj = json.loads(
            raw, parse_constant=_reject_json_constant)
    except ValueError:
        return None, "malformed_json"
    reason = _validate_baseline(obj)
    if reason is not None:
        return None, reason
    return obj, None


def _save_baseline(path, on_ac, charge_pct, snap, unplugged_at=None,
                   boot_session_uuid=None):
    """Atomic write (temp file + os.replace in the same directory) so a
    concurrent reader never observes a truncated baseline; chowned to the
    effective sudo user when run as root (S6).
    """
    if not isinstance(on_ac, bool):
        raise ValueError("on_ac must be known before saving a baseline")
    directory = os.path.dirname(path)
    filename = os.path.basename(path)
    if boot_session_uuid is None:
        boot_session_uuid = power.read_boot_session_uuid()
    if boot_session_uuid is None:
        raise ValueError("boot_session_unavailable")
    sample_abstime = rusage.mach_absolute_time()
    processes = {}
    for (pid, start_ticks), s in snap.items():
        processes[str(pid)] = {
            "start_ticks": start_ticks,
            "cpu_ns": s["cpu_user_ns"] + s["cpu_system_ns"],
            "qos_cpu_ns": {
                name: s.get("qos_cpu_ns", {}).get(name, 0)
                for name in _QOS_CLASSES
            },
            "pkg_idle_wakeups": s["pkg_idle_wakeups"],
            "diskio_bytes_read": s["diskio_bytes_read"],
            "diskio_bytes_written": s["diskio_bytes_written"],
            "energy_nj": s["energy_nj"],
        }
    payload = {
        "schema": BASELINE_SCHEMA,
        "saved_at": time.time(),
        "sample_abstime": sample_abstime,
        "boot_session_uuid": boot_session_uuid,
        "root": cli.is_root(),
        "on_ac": on_ac,
        "unplugged_at": unplugged_at,
        "charge_pct": charge_pct,
        "processes": processes,
    }
    reason = _validate_baseline(payload)
    if reason is not None:
        raise ValueError(reason)
    directory_fd = _open_state_directory(directory, create=True)
    tmp_name = ".%s-%s.tmp" % (filename, secrets.token_hex(8))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    file_fd = None
    try:
        file_fd = os.open(
            tmp_name, flags, 0o600, dir_fd=directory_fd)
        owner = _effective_ids()
        if owner is not None:
            os.fchown(file_fd, owner[0], owner[1])
        with os.fdopen(file_fd, "w") as fh:
            file_fd = None
            json.dump(payload, fh, allow_nan=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(
            tmp_name, filename,
            src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    except (OSError, ValueError):
        if file_fd is not None:
            os.close(file_fd)
        try:
            os.unlink(tmp_name, dir_fd=directory_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(directory_fd)


BatteryDrainerRow = namedtuple("BatteryDrainerRow", (
    "pid", "name", "cpu_seconds_since", "pkg_idle_wakeups_since",
    "diskio_bytes_read_since", "diskio_bytes_written_since",
    "energy_score_total", "energy_joules_since",
))


def rank_drainers(baseline, snap, coeffs):
    """Cumulative energy impact since the baseline, ranked by
    `energy_score_total` descending — dimensionally distinct from `top`'s
    `energy_score_per_s` (this is a total over the whole "since unplug"
    window, never a rate; Copilot #42 flagged the original implementation
    for blurring rate and cumulative units under the same constants).

    Identity-safe: a missing or reused pid is zero-baselined only when its
    start tick proves that it began after the baseline. A pre-existing
    process that merely became visible under different privileges is skipped
    rather than charged its entire lifetime.
    """
    baseline_processes = baseline["processes"]
    baseline_abstime = baseline["sample_abstime"]
    rows = []
    for (pid, start_ticks), s in snap.items():
        base = baseline_processes.get(str(pid))
        if base is not None and base.get("start_ticks") == start_ticks:
            base_cpu_ns = base["cpu_ns"]
            base_qos = base.get("qos_cpu_ns", {})
            base_pkg = base["pkg_idle_wakeups"]
            base_read = base["diskio_bytes_read"]
            base_write = base["diskio_bytes_written"]
            base_energy = base.get("energy_nj")
        else:
            if start_ticks <= baseline_abstime:
                continue
            base_cpu_ns = base_pkg = base_read = base_write = 0
            base_qos = {}
            base_energy = 0 if s["energy_nj"] is not None else None

        cpu_ns = max(0, (s["cpu_user_ns"] + s["cpu_system_ns"]) - base_cpu_ns)
        cpu_seconds = cpu_ns / 1e9
        qos_seconds = {
            name: max(
                0,
                s.get("qos_cpu_ns", {}).get(name, 0)
                - base_qos.get(name, 0),
            ) / 1e9
            for name in _QOS_CLASSES
        }
        d_pkg = max(0, s["pkg_idle_wakeups"] - base_pkg)
        d_read = max(0, s["diskio_bytes_read"] - base_read)
        d_write = max(0, s["diskio_bytes_written"] - base_write)
        score = _energy_score(
            coeffs, cpu_seconds, d_pkg, d_read, d_write,
            qos_cpu_seconds=qos_seconds)
        joules = None
        if s["energy_nj"] is not None and base_energy is not None:
            joules = max(0, s["energy_nj"] - base_energy) / 1e9

        if cpu_ns > 0 or d_pkg > 0 or (score or 0) > 0 or (joules or 0) > 0:
            rows.append(BatteryDrainerRow(
                pid, proc_name(pid, (pid, start_ticks)), cpu_seconds, d_pkg,
                d_read, d_write, score, joules))
    rows.sort(key=lambda r: -(r.energy_score_total or 0.0))
    return rows


def _drainer_entry(row):
    return {
        "pid": row.pid,
        "name": row.name,
        "cpu_seconds_since": row.cpu_seconds_since,
        "pkg_idle_wakeups_since": row.pkg_idle_wakeups_since,
        "diskio_bytes_read_since": row.diskio_bytes_read_since,
        "diskio_bytes_written_since": row.diskio_bytes_written_since,
        "energy_score_total": row.energy_score_total,
        "energy_joules_since": row.energy_joules_since,
    }


def _drainers_document(partial, reasons, **fields):
    body = {
        "present": None,
        "on_ac": None,
        "probe_error": None,
        "baseline_reset": False,
        "reset_reason": None,
        "charge_pct": None,
        "charge_drop": None,
        "elapsed_s": None,
        "pmenergy_source": None,
        "drainers": [],
        "error": None,
    }
    body.update(fields)
    return schema.document(
        "battery", "drainers", partial=partial,
        partial_reasons=list(dict.fromkeys(reasons)), **body)


def cmd_drainers(options):
    h = battery_health()
    partial, reasons = _visibility()

    if h["probe_error"]:
        if options.json:
            cli.emit_json(_drainers_document(
                partial, reasons, probe_error=h["probe_error"],
                error="battery_probe_failed"))
        else:
            sys.stderr.write(
                DIM + "battery drainers probe failed: %s\n" % h["probe_error"] + RESET)
        return cli.EXIT_ERROR

    if not h["present"]:
        if options.json:
            cli.emit_json(_drainers_document(
                partial, reasons, present=False))
        else:
            print(DIM + "no battery detected (desktop Mac?) \u2014 'since "
                  "unplug' does not apply." + RESET)
        return cli.EXIT_OK

    on_ac = h["external_connected"]
    charge_pct = h["charge_pct"]
    if on_ac is None:
        reasons.append("power_state_unknown")
        if options.json:
            cli.emit_json(_drainers_document(
                True, reasons, present=True, charge_pct=charge_pct,
                error="power_state_unknown"))
        else:
            sys.stderr.write(
                "battery drainers: AC connection state is unavailable\n")
        return cli.EXIT_ERROR

    boot_session_uuid = power.read_boot_session_uuid()
    if boot_session_uuid is None:
        reasons.append("boot_session_unavailable")
        if options.json:
            cli.emit_json(_drainers_document(
                True, reasons, present=True, on_ac=on_ac,
                charge_pct=charge_pct, error="boot_session_unavailable"))
        else:
            sys.stderr.write(
                "battery drainers: boot session identifier unavailable\n")
        return cli.EXIT_ERROR

    unplugged_at = None
    if not on_ac:
        transition_state, transition_epoch, transition_error = (
            power.read_last_power_transition())
        if transition_error is not None or transition_state != "battery":
            reasons.append("power_history_unavailable")
            partial = True
        else:
            unplugged_at = transition_epoch

    try:
        path = os.path.join(_state_dir(), BASELINE_FILENAME)
    except OSError:
        if options.json:
            cli.emit_json(_drainers_document(
                True, reasons + ["baseline_store_unavailable"],
                present=True, on_ac=on_ac, charge_pct=charge_pct,
                error="baseline_store_unavailable"))
        else:
            sys.stderr.write("battery drainers: baseline store unavailable\n")
        return cli.EXIT_ERROR

    snap = snapshot_power()
    baseline, load_reset_reason = _load_baseline(path)
    if load_reset_reason == "baseline_read_failed":
        if options.json:
            cli.emit_json(_drainers_document(
                True, reasons + ["baseline_read_failed"],
                present=True, on_ac=on_ac, charge_pct=charge_pct,
                error="baseline_read_failed"))
        else:
            sys.stderr.write("battery drainers: baseline read failed\n")
        return cli.EXIT_ERROR

    if on_ac:
        reset_reason = "on_ac"
    elif baseline is None:
        reset_reason = load_reset_reason or "no_baseline"
    elif baseline.get("on_ac") is True:
        reset_reason = "unplugged"
    elif baseline["boot_session_uuid"] != boot_session_uuid:
        reset_reason = "system_restarted"
    elif (unplugged_at is not None
          and baseline.get("unplugged_at") != unplugged_at):
        reset_reason = "power_session_changed"
    elif (unplugged_at is None
          and charge_pct is not None
          and baseline.get("charge_pct") is not None
          and charge_pct > baseline["charge_pct"] + 1.0):
        reset_reason = "charge_increased"
    else:
        reset_reason = None

    if reset_reason is not None:
        try:
            _save_baseline(
                path, on_ac, charge_pct, snap,
                unplugged_at=unplugged_at,
                boot_session_uuid=boot_session_uuid)
        except (OSError, ValueError):
            if options.json:
                cli.emit_json(_drainers_document(
                    True, reasons + ["baseline_write_failed"],
                    present=True, on_ac=on_ac, charge_pct=charge_pct,
                    error="baseline_write_failed"))
            else:
                sys.stderr.write("battery drainers: baseline write failed\n")
            return cli.EXIT_ERROR
        if options.json:
            cli.emit_json(_drainers_document(
                partial, reasons, present=True, on_ac=on_ac,
                baseline_reset=True, reset_reason=reset_reason,
                charge_pct=charge_pct))
        else:
            print(DIM + "baseline set (%s) at charge %s%%. Run again after "
                  "some time on battery." % (
                      reset_reason, _fmt(charge_pct, "%.0f")) + RESET)
        return cli.EXIT_OK

    if baseline["root"] != cli.is_root():
        reasons.append("baseline_visibility_changed")
        partial = True

    coeffs, coeffs_path, _ = power.pmenergy_coefficients()
    if coeffs is None:
        reasons.append("no_pmenergy_coefficients")
        partial = True
    rows = rank_drainers(baseline, snap, coeffs)
    elapsed_s = max(0.0, time.time() - baseline["saved_at"])
    charge_drop = None
    if charge_pct is not None and baseline.get("charge_pct") is not None:
        charge_drop = baseline["charge_pct"] - charge_pct

    if options.json:
        cli.emit_json(_drainers_document(
            partial, reasons, present=True, on_ac=on_ac,
            charge_pct=charge_pct, charge_drop=charge_drop,
            elapsed_s=round(elapsed_s, 1), pmenergy_source=coeffs_path,
            drainers=[_drainer_entry(row) for row in rows[:options.limit]]))
        return cli.EXIT_OK

    mins = elapsed_s / 60.0
    print(BOLD + "stethoscope battery drainers \u00b7 since unplug" + RESET)
    print(DIM + "%.0f min on battery  \u00b7  charge dropped %s%%  (now %s%%)" % (
        mins, _fmt(charge_drop, "%.0f"), _fmt(charge_pct, "%.0f")) + RESET)
    print()
    print(BOLD + "%7s  %-24s %9s %9s %10s %10s" %
          ("PID", "COMMAND", "CPU s", "PKG WAKE", "SCORE", "JOULES") + RESET)
    if not rows:
        print(DIM + "  (nothing notable \u2014 or processes have since exited)"
              + RESET)
    for row in rows[:options.limit]:
        print("%7d  %-24s %9.1f %9d %10s %10s" % (
            row.pid, cli.safe_text(row.name)[:24], row.cpu_seconds_since,
            row.pkg_idle_wakeups_since, score_str(row.energy_score_total),
            "-" if row.energy_joules_since is None else "%.1f" % row.energy_joules_since))
    return cli.EXIT_OK


# ---------------------------------------------------------------------------
# battery inspect — root-only richer tier via powermetrics
# ---------------------------------------------------------------------------

def _inspect_document(available, reason, **fields):
    body = {
        "available": available,
        "reason": reason,
        "observed_battery_flow_watts": None,
        "observed_state": None,
        "reconciliation_note": None,
        "tasks": [],
    }
    body.update(fields)
    return schema.document("battery", "inspect", **body)


def cmd_inspect(options):
    """Root-only per-process Energy Impact detail via a single powermetrics
    sample. Reports explicit unavailability rather than fabricated numbers
    whenever the richer sample cannot be taken or does not parse as
    expected (core.power.read_powermetrics_tasks).
    """
    if not cli.is_root():
        if options.json:
            cli.emit_json(_inspect_document(False, "root_required"))
        else:
            sys.stderr.write(
                "inspect needs root (powermetrics). Re-run: sudo %s inspect\n"
                % sys.argv[0])
        return cli.EXIT_PERMISSION

    h = battery_health()
    tasks, err = power.read_powermetrics_tasks()
    if err is not None:
        if options.json:
            cli.emit_json(_inspect_document(
                False, err,
                observed_battery_flow_watts=h.get("battery_flow_watts"),
                observed_state=h.get("state")))
        else:
            sys.stderr.write(
                DIM + "battery inspect: powermetrics unavailable (%s) \u2014 "
                "no numbers fabricated.\n" % err + RESET)
        return cli.EXIT_ERROR

    def ranking_value(task):
        rate = task.get("energy_impact_per_s")
        return rate if rate is not None else (task.get("energy_impact_total") or 0)

    ranked = sorted(tasks, key=lambda task: -ranking_value(task))
    note = ("powermetrics Energy Impact is unitless, not watts. Normalized "
            "per-second and sample-total fields stay separate and are not "
            "reconciled against ioreg battery-flow watts "
            "(ARCHITECTURE.md \u00a75).")
    reasons = ["battery_probe_failed"] if h.get("probe_error") else []

    if options.json:
        document = _inspect_document(
            True, None,
            observed_battery_flow_watts=h.get("battery_flow_watts"),
            observed_state=h.get("state"), reconciliation_note=note,
            tasks=[{"pid": t["pid"], "name": t["name"],
                    "energy_impact_per_s": t.get("energy_impact_per_s"),
                    "energy_impact_total": t.get("energy_impact_total")}
                   for t in ranked[:options.limit]])
        if reasons:
            document["partial"] = True
            document["partial_reasons"] = reasons
        cli.emit_json(document)
        return cli.EXIT_OK

    print(BOLD + "stethoscope battery inspect \u00b7 powermetrics (root)" + RESET)
    print(DIM + note + RESET)
    print()
    print(BOLD + "%7s  %-28s %13s %13s" %
          ("PID", "COMMAND", "IMPACT/s", "IMPACT TOTAL") + RESET)
    if not ranked:
        print(DIM + "  (no tasks reported this sample)" + RESET)
    for t in ranked[:options.limit]:
        print("%7d  %-28s %13s %13s" % (
            t["pid"], cli.safe_text(t["name"])[:28],
            score_str(t.get("energy_impact_per_s")),
            score_str(t.get("energy_impact_total"))))
    return cli.EXIT_OK


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

USAGE = """stethoscope battery — power drain and battery health for macOS

  battery [health] [--json]                  charge, cycles, capacity vs
                                              design, condition (no sudo)
  battery top [--interval N] [--limit N] [--once | --duration N] [--json]
                                              who is drawing power now
  battery drainers [--limit N] [--json]      cumulative drain since unplug
  battery inspect [--limit N] [--json]       richer per-process detail via
                                              powermetrics (needs sudo)

`top`/`drainers` ENERGY SCORE is Apple's own Energy Impact formula (CPU
time + pkg-idle wakeups + disk bytes, weighted by /usr/share/pmenergy's
unitless per-board coefficients) — a ranking score, never watts. Where
rusage flavor 6 is available, RWATTS/JOULES are real per-process power
alongside it, never `ri_billed_energy` (its deltas are frozen at cadence).

Not running as root hides other users' processes; `top`/`drainers` still
run and mark --json output partial (reason "not_root") rather than
failing. `inspect` needs root outright (powermetrics) and reports explicit
unavailability rather than a fabricated reading when the sample cannot be
taken.

Run under sudo to see all processes / use inspect:  sudo ./stethoscope battery top
Exit codes: 0 ok \u00b7 1 findings (health: condition degraded) \u00b7 2 usage
\u00b7 3 permission (inspect without root) \u00b7 4 probe failure
"""


def main(argv):
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    args = argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return cli.EXIT_OK

    mode = "health"
    if args and not args[0].startswith("-"):
        mode = args.pop(0)

    try:
        options = cli.parse_options(args)
        if mode == "health":
            cli.require_options(options, mode, {"json"})
            cli.require_positionals(options, mode, 0)
            return cmd_health(options)
        if mode == "top":
            cli.require_positionals(options, mode, 0)
            return cmd_top(options)
        if mode == "drainers":
            cli.require_options(options, mode, {"json", "limit"})
            cli.require_positionals(options, mode, 0)
            return cmd_drainers(options)
        if mode == "inspect":
            cli.require_options(options, mode, {"json", "limit"})
            cli.require_positionals(options, mode, 0)
            return cmd_inspect(options)
    except cli.OptionsError as exc:
        sys.stderr.write("%s\n" % exc)
        return cli.EXIT_USAGE

    sys.stderr.write("unknown mode: %s\n\n%s" % (mode, USAGE))
    return cli.EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main(sys.argv))
