"""Pure classifiers over live, baseline, trend, and point-probe structures."""

from core import stats
from diagnosis import taxonomy

MIB = 1024 * 1024

# Signed battery flow is intentionally absent: charging and discharging have
# opposite meanings, and direction cannot be inferred from the value alone.
SYSTEM_METRIC_POLICIES = {
    ("cpu", "cpu_pct"): {"direction": "high", "absolute": 20.0, "relative": 0.5},
    ("cpu", "pkg_idle_wakeups_per_s"): {
        "direction": "high", "absolute": 50.0, "relative": 1.0},
    ("cpu", "interrupt_wakeups_per_s"): {
        "direction": "high", "absolute": 250.0, "relative": 1.0},
    ("disk", "read_bytes_per_s"): {
        "direction": "high", "absolute": 5 * MIB, "relative": 1.0},
    ("disk", "write_bytes_per_s"): {
        "direction": "high", "absolute": 5 * MIB, "relative": 1.0},
    ("memory", "used_bytes"): {
        "direction": "high", "absolute": 256 * MIB, "relative": 0.05},
    ("memory", "free_bytes"): {
        "direction": "low", "absolute": 256 * MIB, "relative": 0.10},
    ("memory", "wired_bytes"): {
        "direction": "high", "absolute": 128 * MIB, "relative": 0.10},
    ("memory", "compressed_bytes"): {
        "direction": "high", "absolute": 128 * MIB, "relative": 0.10},
    ("battery", "energy_rate_watts"): {
        "direction": "high", "absolute": 2.0, "relative": 0.5},
    ("battery", "energy_score_per_s"): {
        "direction": "high", "absolute": 1.0, "relative": 0.5},
}

_METRIC_LABELS = {
    "cpu_pct": "system CPU",
    "pkg_idle_wakeups_per_s": "package-idle wakeups",
    "interrupt_wakeups_per_s": "interrupt wakeups",
    "read_bytes_per_s": "disk reads",
    "write_bytes_per_s": "disk writes",
    "used_bytes": "used memory",
    "free_bytes": "free memory",
    "wired_bytes": "wired memory",
    "compressed_bytes": "compressed memory",
    "energy_rate_watts": "process energy rate",
    "energy_score_per_s": "Energy Impact score",
}


def _drill_for(area, pid=None, device=None):
    if area == "cpu":
        return ["stethoscope cpu top", "stethoscope cpu wakeups"]
    if area == "memory":
        commands = ["stethoscope memory top"]
        if pid is not None:
            commands.insert(0, "stethoscope memory watch %d" % pid)
        return commands
    if area == "disk":
        return ["stethoscope disk top"]
    if area == "battery":
        return ["stethoscope battery health", "stethoscope battery top"]
    if area == "smart":
        return ["stethoscope smart status%s" %
                (" %s" % device if device else "")]
    return ["stethoscope %s" % area]


def system_deviation_findings(current_metrics, baseline_values,
                              min_baseline_count=5):
    """Classify selected system metrics against the current context only."""
    findings = []
    for metric in current_metrics:
        key = (metric.get("scope"), metric.get("metric"))
        policy = SYSTEM_METRIC_POLICIES.get(key)
        if policy is None:
            continue
        source = baseline_values.get(key, ())
        values = source.values if hasattr(source, "values") else source
        values = list(values)
        if len(values) < min_baseline_count:
            continue
        band = stats.robust_band(
            values, policy["absolute"], policy["relative"])
        result = stats.classify_deviation(
            metric.get("value"), band, policy["direction"])
        if result is None:
            continue
        confidence = (
            "high" if len(values) >= 100 else
            "moderate" if len(values) >= 20 else
            "low")
        label = _METRIC_LABELS.get(key[1], key[1])
        relation = "above" if policy["direction"] == "high" else "below"
        findings.append(taxonomy.finding(
            "system_%s_deviation" % key[1],
            result["severity"], key[0], "deviation",
            "%s is %s its contextual baseline" % (label, relation),
            result["score"], confidence, _drill_for(key[0]),
            {
                "metric": key[1],
                "current": result["value"],
                "direction": result["direction"],
                "threshold": result["threshold"],
                "baseline": result["band"],
            }))
    return taxonomy.sort_findings(findings)


def leak_findings(current_processes, leak_trends, limit=10):
    """Classify current PID/start identities using only their own history."""
    findings = []
    for process in current_processes:
        identity = (process.get("pid"), process.get("start_ticks"))
        accumulator = leak_trends.get(identity)
        if accumulator is None:
            continue
        evidence = stats.leak_evidence(accumulator)
        if evidence is None:
            continue
        pid = process.get("pid")
        name = process.get("name") or "?"
        confidence = (
            "high"
            if (evidence["sample_count"] >= 30
                and evidence["span_seconds"] >= 2 * 60 * 60)
            else "moderate")
        finding_evidence = dict(evidence)
        finding_evidence.update({
            "pid": pid,
            "start_ticks": process.get("start_ticks"),
            "normalized_name": process.get("normalized_name"),
        })
        findings.append(taxonomy.finding(
            "process_footprint_leak", evidence["severity"], "memory", "leak",
            "%s (pid %s) has sustained footprint growth" % (name, pid),
            evidence["score"], confidence, _drill_for("memory", pid=pid),
            finding_evidence))
    return taxonomy.sort_findings(findings)[:limit]


def runaway_findings(current_processes, process_baselines, limit=10):
    """Classify CPU and each wakeup counter independently."""
    findings = []
    metric_names = {
        "cpu_pct": "CPU",
        "pkg_idle_wakeups_per_s": "package-idle wakeups",
        "interrupt_wakeups_per_s": "interrupt wakeups",
    }
    for process in current_processes:
        normalized = process.get("normalized_name")
        per_name = process_baselines.get(normalized, {})
        for metric, label in metric_names.items():
            source = per_name.get(metric, ())
            values = source.values if hasattr(source, "values") else source
            evidence = stats.runaway_evidence(
                metric, process.get(metric), values)
            if evidence is None:
                continue
            pid = process.get("pid")
            stable = dict(evidence)
            stable.update({
                "pid": pid,
                "start_ticks": process.get("start_ticks"),
                "normalized_name": normalized,
            })
            findings.append(taxonomy.finding(
                "process_%s_runaway" % metric,
                evidence["severity"], "cpu", "runaway",
                "%s (pid %s) has runaway %s" %
                (process.get("name") or "?", pid, label),
                evidence["score"], evidence["confidence"],
                _drill_for("cpu", pid=pid), stable))
    return taxonomy.sort_findings(findings)[:limit]


def point_findings(memory, battery, drives):
    """Classify current pressure, battery service, and SMART structures."""
    findings = []
    pressure = (memory or {}).get("pressure")
    if pressure == "critical":
        findings.append(taxonomy.finding(
            "memory_pressure_critical", "critical", "memory", "point",
            "kernel memory pressure is critical", 100, "high",
            _drill_for("memory"), {"pressure": pressure}))
    elif pressure == "warn":
        findings.append(taxonomy.finding(
            "memory_pressure_warn", "warn", "memory", "point",
            "kernel memory pressure is elevated", 60, "high",
            _drill_for("memory"), {"pressure": pressure}))
    elif pressure not in ("normal",):
        findings.append(taxonomy.finding(
            "memory_pressure_unknown", "info", "memory", "point",
            "kernel memory pressure is unavailable", 10, "low",
            _drill_for("memory"), {"pressure": pressure or "unknown"}))

    battery = battery or {}
    if (battery.get("present") is True
            and battery.get("condition") not in (None, "", "Normal")):
        findings.append(taxonomy.finding(
            "battery_service_condition", "warn", "battery", "point",
            "battery condition requires service", 70, "high",
            _drill_for("battery"),
            {"condition": battery.get("condition"),
             "health_pct": battery.get("health_pct")}))

    for drive in drives or ():
        device = drive.get("device")
        for warning in drive.get("warnings") or ():
            severity = warning.get("severity")
            if severity not in ("warn", "critical"):
                severity = "warn"
            findings.append(taxonomy.finding(
                "smart_%s" % (warning.get("code") or "warning"),
                severity, "smart", "point",
                "%s: %s" % (device or "drive",
                            warning.get("message") or "SMART warning"),
                95 if severity == "critical" else 65, "high",
                _drill_for("smart", device=device),
                {
                    "device": device,
                    "warning_code": warning.get("code"),
                    "smart_status": drive.get("smart_status"),
                }))
    return taxonomy.sort_findings(findings)


# Descriptive aliases kept at this pure layer for callers and tests.
classify_system_deviations = system_deviation_findings
classify_leaks = leak_findings
classify_runaways = runaway_findings
classify_points = point_findings
