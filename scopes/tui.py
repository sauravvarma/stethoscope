#!/usr/bin/env python3
"""Unified curses shell over the canonical stethoscope scope data layers."""

import curses
import os
import signal
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import cli
from core import tui as widgets
from scopes import battery
from scopes import anomaly
from scopes import cpu
from scopes import disk
from scopes import memory
from scopes import smart


V_PROC, V_VOL = 0, 1
TABS = ("disk", "cpu", "memory", "battery", "smart")
TAB_LABELS = {
    "disk": "disk",
    "cpu": "cpu",
    "memory": "memory",
    "battery": "battery",
    "smart": "drives",
}
SMART_HEADERS = ("DEVICE", "MODEL", "LOCATION", "VERDICT", "WEAR", "TEMP")
SMART_MIN_INTERVAL = 5.0
LOOP_TIMEOUT_MS = 200
_PROBE_ERRORS = (
    OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError,
)

USAGE = """stethoscope tui — unified live terminal interface

usage: stethoscope tui

keys:
  1-5        disk / cpu / memory / battery / drives
  Tab        cycle global tabs
  v          switch disk process/volume subview
  up/down    move selection (j/k also work)
  p/space    pause; +/- changes the sampling interval
  d          run canonical triage and focus its findings
  [/]        select a finding; Enter opens its evidence
  q/Esc      quit

Disk process actions: Enter/f files, i inspect, x kill (confirm).
Disk volume actions: Enter/r holders, e eject (confirm).
"""


def tab_index(name):
    """Return a tab index, defaulting compatibility entry points to disk."""
    try:
        return TABS.index(name)
    except ValueError:
        return 0


def tab_index_for_key(key):
    """Map global numeric keys 1-5 to tab indexes."""
    if ord("1") <= key <= ord("5"):
        return key - ord("1")
    return None


def severity_for_memory_pressure(pressure):
    """Map only an explicit normal pressure reading to healthy."""
    value = str(pressure or "").lower()
    if value == "critical":
        return "critical"
    if value == "warn":
        return "warn"
    if value == "normal":
        return "healthy"
    return "unknown"


def severity_for_battery_health(health):
    """Preserve failed, absent, and unknown battery states."""
    if not health or health.get("probe_error"):
        return "error"
    if health.get("present") is False:
        return "absent"
    if health.get("present") is not True:
        return "unknown"
    if health.get("condition") == "Service Recommended":
        return "critical"
    if health.get("condition") == "Normal":
        return "healthy"
    return "unknown"


def battery_partial_reasons(health, model):
    """Return explicit health/model gaps that make attribution incomplete."""
    reasons = []
    health = health or {}
    model = model or {}
    if health.get("pmset_error"):
        reasons.append("pmset unavailable")
    if model.get("coefficients") is None:
        reasons.append("energy model unavailable")
    elif model.get("error"):
        reasons.append("energy model incomplete")
    return reasons


def severity_for_drive(health):
    if health.get("worst_severity") == "critical":
        return "critical"
    if health.get("worst_severity") == "warn":
        return "warn"
    if (health.get("diskutil_detail")
            or health.get("smartctl_available") is False
            or health.get("smartctl_detail")):
        return "partial"
    status = str(health.get("smart_status") or "").lower()
    if status in ("verified", "passed", "ok"):
        return "healthy"
    return "unknown"


def drive_verdict(health):
    return widgets.severity_label(severity_for_drive(health)).strip("[]")


def _optional(value, pattern):
    return "-" if value is None else pattern % value


def smart_row_fields(health):
    """Fields in exactly ``SMART_HEADERS`` order."""
    return (
        health.get("device") or "?",
        health.get("name") or "?",
        "internal" if health.get("internal") is True else
        "external" if health.get("internal") is False else "unknown",
        drive_verdict(health),
        _optional(health.get("percentage_used"), "%s%%"),
        _optional(health.get("temperature_c"), "%sC"),
    )


def format_smart_header():
    return "%-9s %-24s %-10s %-10s %7s %7s" % SMART_HEADERS


def format_smart_row(health):
    device, model, location, verdict, wear, temperature = smart_row_fields(health)
    return "%-9s %-24s %-10s %-10s %7s %7s" % (
        device, str(model)[:24], location, verdict, wear, temperature,
    )


def format_title(width, active_tab, is_root, clock_text):
    """Build one non-overlapping title line for wide and narrow terminals."""
    usable = max(0, int(width) - 1)
    if usable == 0:
        return ""
    active_index = tab_index(active_tab)
    active = "[%d]%s" % (
        active_index + 1, TAB_LABELS[TABS[active_index]])
    tabs = " ".join(
        ("[%d]%s" if index == active_index else "%d %s")
        % (index + 1, TAB_LABELS[tab])
        for index, tab in enumerate(TABS)
    )
    right = "%s %s" % ("root" if is_root else "user", clock_text)
    candidates = (
        " stethoscope " + tabs,
        " stethoscope " + active,
        active,
        "stethoscope",
    )
    for left in candidates:
        if len(left) + 1 + len(right) <= usable:
            return left + " " * (usable - len(left) - len(right)) + right
        if len(left) <= usable:
            return left
    return candidates[-1][:usable]


def finding_popup_lines(finding):
    """Render canonical finding fields without interpreting their evidence."""
    lines = [
        "%s [%s/%s] score=%s confidence=%s" % (
            widgets.severity_label(finding.get("severity")),
            finding.get("area") or "unknown",
            finding.get("detector") or "unknown",
            finding.get("score") if finding.get("score") is not None else "?",
            finding.get("confidence") or "unknown",
        ),
        finding.get("message") or "details unavailable",
    ]
    if finding.get("onset"):
        lines.append("onset: %s" % finding["onset"])
    for key, value in sorted((finding.get("evidence") or {}).items()):
        lines.append("evidence %s: %s" % (key, value))
    for command in finding.get("drill_down") or ():
        lines.append("verify: %s" % command)
    remediation = finding.get("remediation")
    if remediation:
        lines.append("remediation: %s" % remediation)
    return lines


def worst_warning(warnings):
    """Return the most severe warning while preserving source order on ties."""
    ranks = {"critical": 3, "warn": 2, "info": 1}
    return max(
        warnings or (),
        key=lambda warning: ranks.get(warning.get("severity"), 0),
        default=None,
    )


class App:
    """Thin stateful shell; scope modules remain the sole source of values."""

    def __init__(self, stdscr, initial_tab="disk", clock=None):
        self.s = stdscr
        self.clock = clock or time.monotonic
        self.tab = tab_index(initial_tab)
        self.disk_view = V_PROC
        self.interval = 1.0
        self.paused = False
        self.selection = {name: 0 for name in TABS}
        self.msg = ""
        self.errors = {name: "" for name in TABS}
        self.is_root = cli.is_root()

        self.disk_prev = None
        self.disk_prev_t = None
        self.disk_rows = []
        self.disk_read = 0.0
        self.disk_write = 0.0
        self.volumes = None

        self.cpu_prev = None
        self.cpu_prev_t = None
        self.cpu_rows = []
        self.cpu_totals = None

        self.memory_rows = []
        self.system_memory = None

        self.battery_prev = None
        self.battery_prev_t = None
        self.battery_rows = []
        self.battery_totals = None
        self.battery_health = None
        self.power_model = None

        self.drive_collection = None
        self.diagnosis_document = None
        self.finding_index = 0
        self.findings_focused = False
        self.last_refresh = {name: None for name in TABS}
        self.histories = {
            name: widgets.RingHistory(60)
            for name in ("disk", "cpu", "memory", "battery")
        }

        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.palette = widgets.Palette()
        self.enter_tab(self.tab, initial=True)

    # -- probes ---------------------------------------------------------
    def _set_error(self, tab, prefix, error):
        self.errors[tab] = "%s: %s" % (prefix, widgets.sanitize(error))

    def _clear_error(self, tab):
        self.errors[tab] = ""

    def _prime_disk(self, now):
        try:
            self.disk_prev = disk.snapshot_diskio()
        except _PROBE_ERRORS as error:
            self.disk_prev = None
            self._set_error("disk", "disk probe failed", error)
        else:
            self._clear_error("disk")
        self.disk_prev_t = now
        self.last_refresh["disk"] = now

    def refresh_disk(self, now=None):
        now = self.clock() if now is None else now
        if self.disk_prev is None:
            self._prime_disk(now)
            return
        try:
            current = disk.snapshot_diskio()
            rows, read_rate, write_rate = disk.rank_io(
                self.disk_prev, current, max(0.000001, now - self.disk_prev_t))
        except _PROBE_ERRORS as error:
            self.disk_prev = None
            self._set_error("disk", "disk probe failed", error)
        else:
            self.disk_rows = rows
            self.disk_read = read_rate
            self.disk_write = write_rate
            self.disk_prev = current
            self.histories["disk"].append(read_rate + write_rate)
            self._clear_error("disk")
        self.disk_prev_t = now
        self.last_refresh["disk"] = now

    def refresh_volumes(self, now=None):
        now = self.clock() if now is None else now
        try:
            mounts = disk._mount_table()
        except _PROBE_ERRORS as error:
            self.volumes = []
            self._set_error("disk", "volume probe failed", error)
        else:
            external = [
                (device, mount) for device, mount in mounts
                if mount.startswith("/Volumes/")
            ]
            self.volumes = external or mounts
            self._clear_error("disk")
        self.last_refresh["disk"] = now

    def _prime_cpu(self, now):
        try:
            self.cpu_prev = cpu.snapshot_cpu()
        except _PROBE_ERRORS as error:
            self.cpu_prev = None
            self._set_error("cpu", "CPU probe failed", error)
        else:
            self._clear_error("cpu")
        self.cpu_prev_t = now
        self.last_refresh["cpu"] = now

    def refresh_cpu(self, now=None):
        now = self.clock() if now is None else now
        if self.cpu_prev is None:
            self._prime_cpu(now)
            return
        try:
            current = cpu.snapshot_cpu()
            rows, totals = cpu.rank_cpu(
                self.cpu_prev, current, max(0.000001, now - self.cpu_prev_t))
        except _PROBE_ERRORS as error:
            self.cpu_prev = None
            self._set_error("cpu", "CPU probe failed", error)
        else:
            self.cpu_rows = rows
            self.cpu_totals = totals
            self.cpu_prev = current
            self.histories["cpu"].append(totals.cpu_pct)
            self._clear_error("cpu")
        self.cpu_prev_t = now
        self.last_refresh["cpu"] = now

    def refresh_memory(self, now=None):
        now = self.clock() if now is None else now
        try:
            snapshot = memory.snapshot_footprint()
            rows = memory.rank_footprint(snapshot)
            system = memory.system_memory()
        except _PROBE_ERRORS as error:
            self._set_error("memory", "memory probe failed", error)
        else:
            self.memory_rows = rows
            self.system_memory = system
            if system.get("used") is not None:
                self.histories["memory"].append(system["used"])
            errors = system.get("errors") or []
            if not system.get("available", True):
                self.errors["memory"] = "memory probe incomplete: %s" % (
                    ", ".join(widgets.sanitize(item) for item in errors)
                    or "unknown probe error"
                )
            else:
                self._clear_error("memory")
        self.last_refresh["memory"] = now

    def _read_battery_points(self):
        try:
            health = battery.battery_health()
        except _PROBE_ERRORS as error:
            health = {"present": None, "probe_error": str(error)}
        try:
            model = battery.power_model()
        except _PROBE_ERRORS as error:
            model = {
                "coefficients": None, "source": None,
                "error": str(error), "available": False,
            }
        self.battery_health = health
        self.power_model = model
        self._update_battery_error()

    def _update_battery_error(self):
        health = self.battery_health or {}
        details = []
        if health.get("probe_error"):
            details.append("battery health failed: %s" % health["probe_error"])
        if health.get("pmset_error"):
            details.append("pmset failed: %s" % health["pmset_error"])
        model = self.power_model or {}
        if model.get("coefficients") is None:
            details.append(
                "energy model unavailable: %s" %
                (model.get("error") or "no coefficients"))
        elif model.get("error"):
            details.append("energy model incomplete: %s" % model["error"])
        if details:
            self.errors["battery"] = "; ".join(
                widgets.sanitize(detail) for detail in details)
        else:
            self._clear_error("battery")

    def _prime_battery(self, now):
        self._read_battery_points()
        try:
            self.battery_prev = battery.snapshot_power()
        except _PROBE_ERRORS as error:
            self.battery_prev = None
            self._set_error("battery", "power attribution probe failed", error)
        self.battery_prev_t = now
        self.last_refresh["battery"] = now

    def refresh_battery(self, now=None):
        now = self.clock() if now is None else now
        if self.battery_prev is None:
            self._prime_battery(now)
            return
        self._read_battery_points()
        coefficients = (
            self.power_model.get("coefficients")
            if self.power_model else None
        )
        try:
            current = battery.snapshot_power()
            rows, totals = battery.rank_top(
                self.battery_prev, current,
                max(0.000001, now - self.battery_prev_t), coefficients)
        except _PROBE_ERRORS as error:
            self.battery_prev = None
            self._set_error(
                "battery", "power attribution probe failed", error)
        else:
            self.battery_rows = rows
            self.battery_totals = totals
            self.battery_prev = current
            history_value = (
                totals.energy_rate_watts
                if totals.energy_rate_watts is not None
                else totals.energy_score_per_s
            )
            if history_value is not None:
                self.histories["battery"].append(history_value)
            self._update_battery_error()
        self.battery_prev_t = now
        self.last_refresh["battery"] = now

    def refresh_drives(self, now=None):
        now = self.clock() if now is None else now
        try:
            self.drive_collection = smart.collect_health()
        except _PROBE_ERRORS as error:
            self.drive_collection = {
                "drives": [], "partial": True,
                "partial_reasons": ["runtime_probe_error"],
                "enumeration_error": str(error),
                "selection_error": None,
                "smartctl_available": False,
                "smartctl_path": None,
            }
            self._set_error("smart", "drive probe failed", error)
        else:
            error = self.drive_collection.get("enumeration_error")
            if error:
                self.errors["smart"] = widgets.sanitize(error)
            elif self.drive_collection.get("partial"):
                reasons = self.drive_collection.get("partial_reasons") or []
                self.errors["smart"] = "drive probes partial: %s" % (
                    ", ".join(widgets.sanitize(reason) for reason in reasons))
            else:
                self._clear_error("smart")
        self.last_refresh["smart"] = now

    def refresh_diagnosis(self):
        """Run the canonical cross-scope classifier only on explicit request."""
        try:
            document, _exit_code = anomaly.run(
                "triage", interval=1.0, limit=20, scope="triage")
        except _PROBE_ERRORS as error:
            self.diagnosis_document = {
                "findings": [], "partial": True,
                "partial_reasons": ["runtime_failure"], "error": str(error),
            }
        else:
            self.diagnosis_document = document
        findings = self.diagnosis_document.get("findings") or []
        self.finding_index = min(self.finding_index, max(0, len(findings) - 1))
        self.findings_focused = bool(findings)
        self.msg = "diagnosis refreshed: %d finding(s)" % len(findings)

    def enter_tab(self, index, initial=False):
        self.tab = max(0, min(int(index), len(TABS) - 1))
        active = TABS[self.tab]
        now = self.clock()
        if active == "disk":
            if self.disk_view == V_VOL:
                if self.volumes is None:
                    self.refresh_volumes(now)
            else:
                self._prime_disk(now)
        elif active == "cpu":
            self._prime_cpu(now)
        elif active == "memory":
            self.refresh_memory(now)
        elif active == "battery":
            self._prime_battery(now)
        elif active == "smart":
            last = self.last_refresh["smart"]
            if last is None or now - last >= SMART_MIN_INTERVAL:
                self.refresh_drives(now)

    def maybe_refresh(self):
        if self.paused:
            return
        active = TABS[self.tab]
        now = self.clock()
        last = self.last_refresh[active]
        if active == "disk":
            if (self.disk_view == V_PROC and self.disk_prev_t is not None
                    and now - self.disk_prev_t >= self.interval):
                self.refresh_disk(now)
        elif active == "cpu":
            if (self.cpu_prev_t is not None
                    and now - self.cpu_prev_t >= self.interval):
                self.refresh_cpu(now)
        elif active == "memory":
            if last is None or now - last >= self.interval:
                self.refresh_memory(now)
        elif active == "battery":
            if (self.battery_prev_t is not None
                    and now - self.battery_prev_t >= self.interval):
                self.refresh_battery(now)
        elif active == "smart":
            cadence = max(SMART_MIN_INTERVAL, self.interval)
            if last is None or now - last >= cadence:
                self.refresh_drives(now)

    # -- drawing --------------------------------------------------------
    def put(self, y, x, text, attr=0):
        return widgets.safe_addstr(self.s, y, x, text, attr)

    def fill(self, y, attr=0):
        return widgets.safe_fill(self.s, y, attr)

    def _state_attr(self, severity):
        role = severity if severity in (
            "healthy", "warn", "critical", "unknown") else "unknown"
        return self.palette.attr(role, bold=severity == "critical")

    def draw(self):
        try:
            self.s.erase()
            height, width = self.s.getmaxyx()
        except curses.error:
            return
        self._draw_title(width)
        self._draw_status(width)
        active = TABS[self.tab]
        if active == "disk":
            self._draw_disk(height)
        elif active == "cpu":
            self._draw_cpu(height)
        elif active == "memory":
            self._draw_memory(height)
        elif active == "battery":
            self._draw_battery(height)
        else:
            self._draw_drives(height)
        self._draw_footer(height)
        try:
            self.s.refresh()
        except curses.error:
            pass

    def _draw_title(self, width):
        attr = self.palette.attr("bar", bold=True)
        self.fill(0, attr)
        self.put(0, 0, format_title(
            width, TABS[self.tab], self.is_root, time.strftime("%H:%M:%S")),
            attr)

    def _draw_status(self, width):
        active = TABS[self.tab]
        state = "PAUSED" if self.paused else "LIVE"
        attr = self.palette.attr("accent")
        if active == "disk":
            mode = "processes" if self.disk_view == V_PROC else "volumes"
            text = (
                "disk/%s read %s write %s | %.1fs | %s %s"
                % (mode, disk.rate(self.disk_read), disk.rate(self.disk_write),
                   self.interval, state,
                   self.histories["disk"].sparkline(max(0, width // 8)))
            )
        elif active == "cpu":
            totals = self.cpu_totals
            if totals is None:
                text = "cpu rates primed; waiting for next interval"
            else:
                text = (
                    "cpu %.1f%% | watts %s | wake pkg %.1f/s intr %.1f/s | %s %s"
                    % (totals.cpu_pct,
                       _optional(totals.watts, "%.2fW"),
                       totals.pkg_wakeups_per_s,
                       totals.interrupt_wakeups_per_s, state,
                       self.histories["cpu"].sparkline(max(0, width // 8)))
                )
        elif active == "memory":
            system = self.system_memory or {}
            severity = severity_for_memory_pressure(system.get("pressure"))
            partial = system.get("available") is False
            pressure = (
                str(system.get("pressure")).upper()
                if system.get("pressure") else "UNKNOWN"
            )
            text = (
                "%s memory used %s/%s wired %s compressed %s pressure %s | %s"
                % (widgets.severity_label(
                       "partial" if partial else severity),
                   memory.human(system.get("used")),
                   memory.human(system.get("total")),
                   memory.human(system.get("wired")),
                   memory.human(system.get("compressed")),
                   pressure, state)
            )
            attr = self._state_attr("unknown" if partial else severity)
        elif active == "battery":
            health = self.battery_health or {}
            severity = severity_for_battery_health(health)
            partial_reasons = battery_partial_reasons(
                health, self.power_model)
            partial_label = (
                " " + widgets.severity_label("partial")
                if partial_reasons and severity in (
                    "critical", "absent", "error") else "")
            label = (
                widgets.severity_label("partial")
                if partial_reasons and severity not in (
                    "critical", "absent", "error")
                else widgets.severity_label(severity) + partial_label)
            if severity == "error":
                text = "%s battery probe failed: %s" % (
                    label,
                    health.get("probe_error") or "unavailable")
            elif severity == "absent":
                text = "%s no battery detected (desktop Mac)%s" % (
                    label,
                    " | " + ", ".join(partial_reasons)
                    if partial_reasons else "")
            else:
                totals = self.battery_totals
                watts = (
                    totals.energy_rate_watts if totals is not None else None)
                score = (
                    totals.energy_score_per_s if totals is not None else None)
                text = (
                    "%s battery %s%% %s | real watts %s | unitless score %s | %s"
                    % (label,
                       health.get("charge_pct") if
                       health.get("charge_pct") is not None else "?",
                       health.get("condition") or "UNKNOWN",
                       _optional(watts, "%.2fW"),
                       _optional(score, "%.2f"),
                       state + (
                           " | " + ", ".join(partial_reasons)
                           if partial_reasons else ""))
                )
            attr = self._state_attr(
                "unknown" if partial_reasons and severity not in (
                    "critical", "error") else severity)
        else:
            collection = self.drive_collection or {}
            drives = collection.get("drives") or []
            if collection.get("enumeration_error"):
                severity = "error"
                text = "%s diskutil drive enumeration failed: %s" % (
                    widgets.severity_label(severity),
                    collection["enumeration_error"])
            elif not drives:
                severity = "absent"
                text = "%s no physical drives found" % (
                    widgets.severity_label(severity))
            else:
                critical = sum(
                    severity_for_drive(item) == "critical" for item in drives)
                warning = sum(
                    severity_for_drive(item) == "warn" for item in drives)
                unknown = sum(
                    severity_for_drive(item) in ("unknown", "partial")
                    for item in drives)
                severity = (
                    "critical" if critical else "warn" if warning else
                    "unknown" if unknown or collection.get("partial")
                    else "healthy"
                )
                text = (
                    "%s drives %d | critical %d warn %d unknown %d | refresh >=5s"
                    % (widgets.severity_label(severity), len(drives),
                       critical, warning, unknown)
                )
            attr = self._state_attr(severity)
        self.put(1, 1, text, attr)
        self._draw_findings(width)

        if (active in ("disk", "cpu", "memory", "battery")
                and not self.is_root):
            note = "[PARTIAL] not root; other users' processes may be hidden"
            self.put(3, 1, note, self.palette.attr("unknown", bold=True))

    def _draw_findings(self, width):
        document = self.diagnosis_document
        if document is None:
            text = "[UNKNOWN] diagnosis not sampled; press d to run triage"
            severity = "unknown"
        else:
            findings = document.get("findings") or []
            if findings:
                self.finding_index = min(
                    self.finding_index, len(findings) - 1)
                finding = findings[self.finding_index]
                severity = finding.get("severity", "info")
                text = "%s%s %d/%d %s [%s/%s] score=%s" % (
                    ">" if self.findings_focused else " ",
                    widgets.severity_label(severity),
                    self.finding_index + 1, len(findings),
                    finding.get("message") or "details unavailable",
                    finding.get("area") or "unknown",
                    finding.get("detector") or "unknown",
                    finding.get("score")
                    if finding.get("score") is not None else "?",
                )
                if document.get("partial") or document.get("error"):
                    reasons = document.get("partial_reasons") or []
                    detail = document.get("error") or ", ".join(reasons)
                    text += " | [PARTIAL] %s" % (
                        detail or "incomplete diagnosis")
            elif document.get("partial") or document.get("error"):
                severity = "unknown"
                reasons = document.get("partial_reasons") or []
                detail = document.get("error") or ", ".join(reasons)
                text = "[PARTIAL] no findings; diagnosis incomplete: %s" % (
                    detail or "unknown reason")
            else:
                severity = "healthy"
                text = "[HEALTHY] no active diagnosis findings"
        self.put(2, 1, text, self._state_attr(severity))

    def _selected(self, key, count):
        self.selection[key] = max(
            0, min(self.selection.get(key, 0), max(0, count - 1)))
        return self.selection[key]

    def _row_attr(self, selected, role=None):
        if selected:
            return self.palette.attr("selection", bold=True)
        return self.palette.attr(role) if role else 0

    def _draw_rows(self, key, rows, height, formatter, empty, roles=None,
                   bottom_reserved=0):
        top = 5
        available = max(0, height - top - 1 - bottom_reserved)
        if not rows:
            self.put(top, 2, empty, curses.A_DIM)
            return
        selected = self._selected(key, len(rows))
        start = max(0, selected - available + 1) if available else 0
        for index in range(start, min(len(rows), start + available)):
            y = top + index - start
            is_selected = index == selected
            if is_selected:
                self.fill(y, self.palette.attr("selection"))
            self.put(y, 0, ">" if is_selected else " ")
            role = roles(rows[index]) if roles else None
            self.put(
                y, 2, formatter(rows[index]),
                self._row_attr(is_selected, role))

    def _draw_disk(self, height):
        if self.disk_view == V_VOL:
            self.put(4, 2, "%-22s %-14s %s" % (
                "VOLUME", "DEVICE", "MOUNT"), curses.A_BOLD)
            volumes = self.volumes or []

            def format_volume(item):
                device, mount = item
                return "%-22s %-14s %s" % (
                    (os.path.basename(mount) or mount)[:22],
                    device.replace("/dev/", "")[:14], mount)

            self._draw_rows(
                "disk", volumes, height, format_volume,
                "(no mounted volumes)")
            return
        self.put(4, 2, "%7s  %-24s %10s %10s %10s %10s" % (
            "PID", "COMMAND", "READ/s", "WRITE/s", "RD TOT", "WR TOT"),
            curses.A_BOLD)

        def format_process(row):
            _total, read_rate, write_rate, read, written, pid, name = row
            return "%7d  %-24s %10s %10s %10s %10s" % (
                pid, str(name)[:24], disk.rate(read_rate),
                disk.rate(write_rate), disk.human(read), disk.human(written))

        self._draw_rows(
            "disk", self.disk_rows, height, format_process,
            "(no disk I/O this interval)")

    def _draw_cpu(self, height):
        self.put(4, 2, "%7s  %-20s %7s %7s %7s %8s %8s %8s" % (
            "PID", "COMMAND", "CPU%", "USER%", "SYS%", "WATTS",
            "PKG/s", "INTR/s"), curses.A_BOLD)

        def format_row(row):
            return "%7d  %-20s %7.1f %7.1f %7.1f %8s %8.1f %8.1f" % (
                row.pid, str(row.name)[:20], row.cpu_pct, row.user_pct,
                row.system_pct, _optional(row.watts, "%.2f"),
                row.pkg_wakeups_per_s, row.interrupt_wakeups_per_s)

        self._draw_rows(
            "cpu", self.cpu_rows, height, format_row,
            "(no CPU activity this interval)")

    def _draw_memory(self, height):
        self.put(4, 2, "%7s  %-28s %12s %12s" % (
            "PID", "COMMAND", "FOOTPRINT", "RESIDENT"), curses.A_BOLD)

        def format_row(row):
            footprint, resident, pid, name = row
            return "%7d  %-28s %12s %12s" % (
                pid, str(name)[:28], memory.human(footprint),
                memory.human(resident))

        self._draw_rows(
            "memory", self.memory_rows, height, format_row,
            "(no accessible processes; try sudo)")

    def _draw_battery(self, height):
        self.put(4, 2, "%7s  %-20s %7s %10s %10s %8s %8s" % (
            "PID", "COMMAND", "CPU%", "REAL W", "SCORE", "PKG/s", "INTR/s"),
            curses.A_BOLD)

        def format_row(row):
            return "%7d  %-20s %7.1f %10s %10s %8.1f %8.1f" % (
                row.pid, str(row.name)[:20], row.cpu_pct,
                _optional(row.energy_rate_watts, "%.2fW"),
                _optional(row.energy_score_per_s, "%.2f"),
                row.pkg_idle_wakeups_per_s,
                row.interrupt_wakeups_per_s)

        self._draw_rows(
            "battery", self.battery_rows, height, format_row,
            "(no attributed activity this interval)")

    def _draw_drives(self, height):
        self.put(4, 2, format_smart_header(), curses.A_BOLD)
        collection = self.drive_collection or {}
        drives = collection.get("drives") or []
        if collection.get("enumeration_error"):
            empty = "(diskutil failed; physical drives are unknown)"
        elif not drives:
            empty = "(no physical drives found)"
        else:
            empty = "(no drive data)"
        self._draw_rows(
            "smart", drives, height, format_smart_row, empty,
            roles=severity_for_drive, bottom_reserved=1)
        if drives and height > 7:
            selected = drives[self._selected("smart", len(drives))]
            warnings = selected.get("warnings") or []
            if warnings:
                worst = worst_warning(warnings)
                self.put(
                    height - 2, 2,
                    "%s %s: %s" % (
                        widgets.severity_label(worst.get("severity")),
                        worst.get("code") or "drive warning",
                        worst.get("message") or "details unavailable"),
                    self._state_attr(worst.get("severity")),
                )
            elif selected.get("diskutil_detail") or selected.get("smartctl_detail"):
                detail = (
                    selected.get("diskutil_detail")
                    or selected.get("smartctl_detail"))
                self.put(
                    height - 2, 2, "[PARTIAL] %s" % detail,
                    self.palette.attr("unknown", bold=True))

    def _draw_footer(self, height):
        attr = self.palette.attr("bar")
        self.fill(height - 1, attr)
        active = TABS[self.tab]
        message = self.msg or self.errors.get(active)
        if message:
            self.put(height - 1, 1, message, attr | curses.A_BOLD)
            return
        if active == "disk" and self.disk_view == V_PROC:
            keys = (
                "Tab/1-5 tabs v volumes arrows/jk move Enter/f files "
                "i inspect x kill p pause +/- rate q quit")
        elif active == "disk":
            keys = (
                "Tab/1-5 tabs v processes arrows/jk move Enter/r holders "
                "e eject p pause q quit")
        else:
            keys = "Tab/1-5 tabs arrows/jk move d diagnose [/] finding Enter details p pause +/- rate q quit"
        self.put(height - 1, 1, keys, attr)

    # -- disk actions ---------------------------------------------------
    def _active_count(self):
        active = TABS[self.tab]
        if active == "disk":
            return len(self.disk_rows) if self.disk_view == V_PROC else len(
                self.volumes or [])
        return len({
            "cpu": self.cpu_rows,
            "memory": self.memory_rows,
            "battery": self.battery_rows,
            "smart": (self.drive_collection or {}).get("drives") or [],
        }[active])

    def selected_pid(self):
        if (TABS[self.tab] != "disk" or self.disk_view != V_PROC
                or not self.disk_rows):
            return None, None
        row = self.disk_rows[self._selected("disk", len(self.disk_rows))]
        return row[5], row[6]

    def popup(self, title, lines):
        if not widgets.popup(
                self.s, self.palette, title, lines,
                timeout_ms=LOOP_TIMEOUT_MS):
            self.msg = "screen too small for popup"

    def show_finding(self):
        document = self.diagnosis_document or {}
        findings = document.get("findings") or []
        if not findings:
            self.msg = "no diagnosis finding selected"
            return
        finding = findings[self.finding_index]
        self.popup(
            "finding %s" % (finding.get("code") or "details"),
            finding_popup_lines(finding))

    def confirm(self, question):
        self.msg = "%s [y/N]" % question
        self.draw()
        try:
            self.s.timeout(-1)
            key = self.s.getch()
        except curses.error:
            key = -1
        finally:
            try:
                self.s.timeout(LOOP_TIMEOUT_MS)
            except curses.error:
                pass
        self.msg = ""
        return key in (ord("y"), ord("Y"))

    def act_files(self):
        pid, name = self.selected_pid()
        if pid is None:
            return
        try:
            items = disk.open_files(pid)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            self.msg = "lsof failed: %s" % widgets.sanitize(error)
            return
        lines = [
            "%-18s %-4s %s" % (reason, kind, path)
            for reason, kind, path in items
        ] or ["(no on-disk files held; permission may be partial)"]
        self.popup("held files pid %d (%s)" % (pid, name), lines)

    def act_inspect(self):
        pid, name = self.selected_pid()
        if pid is None:
            return
        try:
            curses.def_prog_mode()
            curses.endwin()
        except curses.error as error:
            self.msg = "could not suspend TUI: %s" % widgets.sanitize(error)
            return
        action_error = None
        try:
            sys.stdout.write("\033[2J\033[H")
            print("stethoscope disk inspect pid %d (%s); Ctrl-C returns" % (
                pid, widgets.sanitize(name)))
            status = disk.cmd_inspect(pid)
            if status != cli.EXIT_OK:
                action_error = "inspect exited with status %d" % status
        except KeyboardInterrupt:
            pass
        except _PROBE_ERRORS as error:
            action_error = "inspect failed: %s" % widgets.sanitize(error)
        try:
            input("\n[press Enter to return to the TUI] ")
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            try:
                curses.reset_prog_mode()
                self.s.clear()
                self.s.refresh()
            except curses.error as error:
                action_error = "TUI restore failed: %s" % widgets.sanitize(error)
        if action_error:
            self.msg = action_error

    def act_kill(self):
        pid, name = self.selected_pid()
        if pid is None:
            return
        if not self.confirm("kill pid %d (%s)?" % (pid, name)):
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as error:
            self.msg = "kill failed: %s" % widgets.sanitize(error)
        else:
            self.msg = "sent SIGTERM to %d" % pid

    def _selected_volume(self):
        volumes = self.volumes or []
        if not volumes:
            return None
        return volumes[self._selected("disk", len(volumes))]

    def _reason_summary(self, holds):
        counts = {}
        for reason, _path in holds:
            counts[reason] = counts.get(reason, 0) + 1
        return ", ".join(
            "%s x%d" % (reason, count) if count > 1 else reason
            for reason, count in sorted(
                counts.items(), key=lambda item: -item[1])
        )

    def act_holders(self):
        selected = self._selected_volume()
        if selected is None:
            return
        _device, mount = selected
        try:
            targets = disk.resolve_volume(mount)
            processes = disk.collect_holders(targets)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            self.msg = "holder lookup failed: %s" % widgets.sanitize(error)
            return
        if not processes:
            self.popup(
                "holders %s" % mount,
                ["No visible process is holding it; nonroot results are partial."
                 if not self.is_root else
                 "No process is holding it; it should eject cleanly."])
            return
        lines = []
        for pid in sorted(
                processes, key=lambda value: -len(processes[value]["holds"])):
            info = processes[pid]
            lines.append("pid %-6d %-18s user=%s" % (
                pid, info["name"], info["user"]))
            lines.append("  holding: %s" % self._reason_summary(info["holds"]))
            lines.extend("    %s" % path for _reason, path in info["holds"][:2])
        self.popup("holders %s (%d)" % (mount, len(processes)), lines)

    def act_eject(self):
        selected = self._selected_volume()
        if selected is None:
            return
        _device, mount = selected
        if not self.confirm("eject %r?" % mount):
            return
        try:
            result = subprocess.run(
                ["/usr/sbin/diskutil", "unmount", mount],
                capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            self.msg = "eject failed: %s" % widgets.sanitize(error)
            return
        output = (result.stdout + result.stderr).strip()
        self.msg = (
            output.splitlines()[-1] if output else
            "eject complete" if result.returncode == 0 else
            "eject failed with exit %d" % result.returncode
        )
        self.refresh_volumes()

    # -- input and loop -------------------------------------------------
    def handle_key(self, key):
        if key == 27 and self.findings_focused:
            self.findings_focused = False
            return True
        if key in (ord("q"), 27):
            return False
        if key == ord("d"):
            self.refresh_diagnosis()
            return True
        findings = (
            (self.diagnosis_document or {}).get("findings") or [])
        if key in (ord("["), ord("]")) and findings:
            delta = -1 if key == ord("[") else 1
            self.finding_index = (
                self.finding_index + delta) % len(findings)
            self.findings_focused = True
            return True
        if (self.findings_focused
                and key in (curses.KEY_ENTER, 10, 13)):
            self.show_finding()
            return True
        numeric_tab = tab_index_for_key(key)
        if numeric_tab is not None:
            self.msg = ""
            self.findings_focused = False
            self.enter_tab(numeric_tab)
            return True
        if key == ord("\t"):
            self.msg = ""
            self.findings_focused = False
            self.enter_tab((self.tab + 1) % len(TABS))
            return True
        if key == getattr(curses, "KEY_BTAB", -999):
            self.msg = ""
            self.findings_focused = False
            self.enter_tab((self.tab - 1) % len(TABS))
            return True

        active = TABS[self.tab]
        count = self._active_count()
        if key in (curses.KEY_DOWN, ord("j")):
            self.findings_focused = False
            self.selection[active] = min(
                self.selection.get(active, 0) + 1, max(0, count - 1))
        elif key in (curses.KEY_UP, ord("k")):
            self.findings_focused = False
            self.selection[active] = max(
                self.selection.get(active, 0) - 1, 0)
        elif active == "disk" and key == ord("v"):
            self.disk_view = V_VOL if self.disk_view == V_PROC else V_PROC
            self.selection["disk"] = 0
            if self.disk_view == V_VOL and self.volumes is None:
                self.refresh_volumes()
            elif self.disk_view == V_PROC:
                self._prime_disk(self.clock())
        elif key in (ord("p"), ord(" ")):
            self.paused = not self.paused
        elif key in (ord("+"), ord("=")):
            self.interval = min(10.0, round(self.interval + 0.5, 1))
        elif key == ord("-"):
            self.interval = max(0.5, round(self.interval - 0.5, 1))
        elif (active == "disk" and self.disk_view == V_PROC
              and key in (ord("f"), curses.KEY_ENTER, 10, 13)):
            self.act_files()
        elif active == "disk" and self.disk_view == V_PROC and key == ord("i"):
            self.act_inspect()
        elif active == "disk" and self.disk_view == V_PROC and key == ord("x"):
            self.act_kill()
        elif (active == "disk" and self.disk_view == V_VOL
              and key in (ord("r"), curses.KEY_ENTER, 10, 13)):
            self.act_holders()
        elif active == "disk" and self.disk_view == V_VOL and key == ord("e"):
            self.act_eject()
        else:
            self.msg = ""
        return True

    def run(self):
        try:
            self.s.timeout(LOOP_TIMEOUT_MS)
        except curses.error:
            pass
        while True:
            self.maybe_refresh()
            self.draw()
            try:
                key = self.s.getch()
            except curses.error:
                continue
            if key in (-1, curses.KEY_RESIZE):
                continue
            self.msg = ""
            if not self.handle_key(key):
                return


def main(argv=None, initial_tab="disk"):
    argv = sys.argv if argv is None else argv
    args = list(argv[1:])
    if args and args[0] in ("-h", "--help", "help"):
        if len(args) != 1:
            sys.stderr.write("tui help accepts no extra arguments\n")
            return cli.EXIT_USAGE
        print(USAGE)
        return cli.EXIT_OK
    if args:
        sys.stderr.write("tui accepts no arguments\n\n%s" % USAGE)
        return cli.EXIT_USAGE
    if not sys.stdout.isatty():
        sys.stderr.write("stethoscope tui needs an interactive terminal.\n")
        return cli.EXIT_ERROR
    if not os.environ.get("TERM"):
        os.environ["TERM"] = "xterm-256color"
    try:
        curses.wrapper(
            lambda stdscr: App(stdscr, initial_tab=initial_tab).run())
    except (curses.error, OSError) as error:
        sys.stderr.write(
            "curses could not start: %s\n"
            "TERM=%r may be unavailable; with sudo, preserve only TERM.\n"
            % (widgets.sanitize(error), os.environ.get("TERM")))
        return cli.EXIT_ERROR
    return cli.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
