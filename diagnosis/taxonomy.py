"""Stable finding construction, ordering, and overall verdicts."""

import math

SEVERITIES = ("ok", "info", "warn", "critical")
CONFIDENCES = ("low", "moderate", "high")
_SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}


def finding(code, severity, area, detector, message, score, confidence,
            drill_down, evidence):
    """Return one validated finding structure."""
    if severity not in _SEVERITY_RANK:
        raise ValueError("invalid finding severity: %s" % severity)
    if confidence not in CONFIDENCES:
        raise ValueError("invalid finding confidence: %s" % confidence)
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ValueError("finding score must be an ordinal number")
    try:
        finite = math.isfinite(score)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError("finding score must be finite")
    commands = [str(command) for command in drill_down if command]
    return {
        "code": str(code),
        "severity": severity,
        "area": str(area),
        "detector": str(detector),
        "message": str(message),
        "score": int(max(0, min(100, round(score)))),
        "confidence": confidence,
        "drill_down": commands,
        "evidence": dict(evidence or {}),
    }


def sort_findings(findings):
    """Return findings deterministically ordered worst/evidence-first."""
    return sorted(findings, key=lambda item: (
        -_SEVERITY_RANK.get(item.get("severity"), -1),
        -int(item.get("score", 0)),
        item.get("area", ""),
        item.get("detector", ""),
        item.get("code", ""),
        item.get("message", ""),
    ))


def overall(findings):
    """Return the worst finding severity, or ``ok`` for no findings."""
    worst = "ok"
    for item in findings:
        severity = item.get("severity", "ok")
        if _SEVERITY_RANK.get(severity, 0) > _SEVERITY_RANK[worst]:
            worst = severity
    return worst
