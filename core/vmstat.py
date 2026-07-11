"""
stethoscope core.vmstat — vm_stat + sysctl system memory-pressure probe.

`proc_pid_rusage()` (core/rusage.py) is a per-process ledger; it has nothing
to say about the machine as a whole. The kernel's own view of *system*
memory comes from two different tools, neither of which is libproc, so
they live in their own module rather than core/rusage.py (which stays
libproc-only, casebook 0003):

  * `vm_stat` — a page-count table (free/active/inactive/wired/compressed)
    in units of the machine's own page size, printed as text.
  * `sysctl kern.memorystatus_vm_pressure_level` — the kernel's own
    pressure verdict (1 normal / 2 warn / 4 critical), the same signal
    macOS uses to decide when to start killing things.

Every subprocess call and its text parsing stays in this file, one layer
below the memory scope (scopes/memory.py); every public function here
returns a structure, never rendered text, so scopes/memory.py never parses
a byte of subprocess output itself.

No third-party dependencies — system Python 3 + subprocess only.
"""

import re
import subprocess

VM_STAT = "/usr/bin/vm_stat"
SYSCTL = "/usr/sbin/sysctl"

_PAGE_SIZE_RE = re.compile(r"page size of (\d+) bytes")
_DEFAULT_PAGE_SIZE = 4096

# kern.memorystatus_vm_pressure_level values (xnu's vm_pressure_level_t).
_PRESSURE_LEVELS = {1: "normal", 2: "warn", 4: "critical"}


class ProbeError(RuntimeError):
    """A system-memory probe failed or returned unusable data."""


def _run(cmd):
    """Run one probe and return stdout, or raise a stable ProbeError."""
    label = cmd[0].rsplit("/", 1)[-1]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except OSError as exc:
        raise ProbeError("%s_unavailable" % label) from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError("%s_timeout" % label) from exc
    if completed.returncode != 0:
        raise ProbeError("%s_failed" % label)
    if not completed.stdout.strip():
        raise ProbeError("%s_empty" % label)
    return completed.stdout


def _sysctl_int(name):
    """One sysctl integer, or ProbeError when the value is unavailable."""
    raw = _run([SYSCTL, "-n", name]).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ProbeError("sysctl_invalid:%s" % name) from exc


def parse_vm_stat(text):
    """Parse `vm_stat` text into {'<lowercased label>': bytes}.

    vm_stat's page size line ("Mach Virtual Memory Statistics: (page size
    of NNNN bytes)") sets the multiplier for every count line below it;
    4096 is the fallback only if that line is missing entirely. Lines that
    are not "label: NNNN." pairs are skipped rather than raised on —
    vm_stat's exact column set is not a stable contract, only its
    presence is; a future macOS adding/removing a line must not crash this.
    """
    page_size = _DEFAULT_PAGE_SIZE
    match = _PAGE_SIZE_RE.search(text)
    if match:
        page_size = int(match.group(1))
    counts = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        value = value.strip().rstrip(".")
        if value.isdigit():
            counts[label.strip().lower()] = int(value) * page_size
    return counts


def pressure_name(level):
    """Map a kern.memorystatus_vm_pressure_level integer to a name.

    'unknown' for a missing sysctl read or a level this table does not
    recognise — never a fabricated 'normal' (an unreadable pressure OID is
    not evidence of low pressure).
    """
    if level is None:
        return "unknown"
    return _PRESSURE_LEVELS.get(level, "unknown")


def system_memory():
    """A system memory summary in bytes, plus the kernel's pressure name.

    `used` is vm_stat's own "not immediately reclaimable" breakdown
    (active + wired + compressed pages); `free` is free + speculative
    pages. `total` comes from hw.memsize. Every key is always present. Probe
    failures produce nullable values plus stable error codes; they never
    fabricate zero-byte totals that look like valid measurements.
    """
    errors = []
    try:
        counts = parse_vm_stat(_run([VM_STAT]))
        if not counts:
            raise ProbeError("vm_stat_unparseable")
    except ProbeError as exc:
        counts = None
        errors.append(str(exc))

    try:
        total = _sysctl_int("hw.memsize")
    except ProbeError as exc:
        total = None
        errors.append(str(exc))

    try:
        level = _sysctl_int("kern.memorystatus_vm_pressure_level")
    except ProbeError as exc:
        level = None
        errors.append(str(exc))

    active = counts.get("pages active", 0) if counts is not None else None
    wired = counts.get("pages wired down", 0) if counts is not None else None
    compressed = (counts.get("pages occupied by compressor", 0)
                  if counts is not None else None)
    return {
        "available": not errors,
        "errors": errors,
        "total": total,
        "used": (active + wired + compressed
                 if counts is not None else None),
        "free": (counts.get("pages free", 0)
                 + counts.get("pages speculative", 0)
                 if counts is not None else None),
        "active": active,
        "inactive": (counts.get("pages inactive", 0)
                     if counts is not None else None),
        "wired": wired,
        "compressed": compressed,
        "pressure": pressure_name(level),
    }
