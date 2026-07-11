"""Secure append-only JSONL storage and baseline statistics.

The raw corpus is intentionally boring: one ``baseline-raw/1`` object per
line in a local daily file.  This module owns storage, replay, time parsing,
and bounded percentile computation; scope modules own collection and display.
"""

import datetime
import errno
import fcntl
import json
import math
import os
import pwd
import random
import re
import stat
import time

RAW_SCHEMA = "baseline-raw/1"
DEFAULT_RETENTION_DAYS = 30
MAX_RETENTION_DAYS = 3650
DEFAULT_RESERVOIR_SIZE = 512
MAX_RAW_LINE_BYTES = 1024 * 1024
MAX_REPLAY_ERRORS = 1024
MAX_JSON_NUMBER_CHARS = 128
MAX_PROCESSES_PER_RECORD = 4096
MAX_METRICS_PER_RECORD = 128
MAX_BASELINE_PROCESS_BUCKETS = 8192
MAX_BASELINE_GROUPS = 4096
DAILY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
LOCK_NAME = ".writer.lock"
REQUIRED_METRICS = {
    ("cpu", "cpu_pct"): "percent_of_one_core",
    ("cpu", "pkg_idle_wakeups_per_s"): "per_second",
    ("cpu", "interrupt_wakeups_per_s"): "per_second",
    ("disk", "read_bytes_per_s"): "bytes_per_second",
    ("disk", "write_bytes_per_s"): "bytes_per_second",
    ("battery", "energy_rate_watts"): "watts",
    ("battery", "energy_score_per_s"): "unitless_per_second",
    ("memory", "used_bytes"): "bytes",
    ("memory", "free_bytes"): "bytes",
    ("memory", "wired_bytes"): "bytes",
    ("memory", "compressed_bytes"): "bytes",
    ("battery", "charge_pct"): "percent",
    ("battery", "health_pct"): "percent",
    ("battery", "flow_watts"): "watts",
    ("sampler", "cpu_pct"): "percent_of_one_core",
    ("sampler", "footprint_bytes"): "bytes",
    ("sampler", "resident_size_bytes"): "bytes",
}
SIGNED_METRICS = {("battery", "flow_watts")}
PROCESS_METRICS = (
    "cpu_pct", "user_pct", "system_pct",
    "pkg_idle_wakeups_per_s", "interrupt_wakeups_per_s",
    "diskio_bytes_read_per_s", "diskio_bytes_written_per_s",
    "energy_rate_watts", "energy_score_per_s",
    "footprint_bytes", "resident_size_bytes",
)
PROCESS_FIELDS = frozenset(
    ("pid", "start_ticks", "name", "normalized_name") + PROCESS_METRICS)


class StoreError(RuntimeError):
    """A corpus operation failed and must be reported to the caller."""


class LockError(StoreError):
    """Another recorder owns the corpus writer lock."""


def _reject_constant(value):
    raise ValueError("non-finite JSON constant %s" % value)


def _parse_int(value):
    if len(value.lstrip("-")) > MAX_JSON_NUMBER_CHARS:
        raise ValueError("integer_too_long")
    return int(value)


def _parse_float(value):
    if len(value) > MAX_JSON_NUMBER_CHARS:
        raise ValueError("number_too_long")
    return float(value)


def _finite_tree(value):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, float):
            if not math.isfinite(item):
                return False
        elif isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                return False
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
        elif item is not None and not isinstance(item, (str, int, bool)):
            return False
    return True


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _timestamp_is_representable(value, nonnegative=False):
    if not _finite_number(value) or (nonnegative and value < 0):
        return False
    try:
        datetime.datetime.fromtimestamp(value)
    except (OverflowError, OSError, ValueError):
        return False
    return True


def effective_user():
    """Return ``(home, uid, gid)`` for the user whose corpus should be used."""
    if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        name = os.environ["SUDO_USER"]
        try:
            entry = pwd.getpwnam(name)
        except KeyError as exc:
            raise StoreError("cannot resolve SUDO_USER %r" % name) from exc
        return entry.pw_dir, entry.pw_uid, entry.pw_gid
    home = os.path.expanduser("~")
    return home, os.getuid(), os.getgid()


def default_store():
    return os.path.join(effective_user()[0], ".stethoscope", "baseline-raw")


def _directory_fd(path, create):
    """Open an absolute directory by descriptor without following symlinks."""
    path = os.path.abspath(path)
    parts = [part for part in path.split(os.sep) if part]
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(os.sep, flags)
    home, uid, gid = effective_user()
    owner = (uid, gid)
    home = os.path.abspath(home)
    try:
        below_home = os.path.commonpath((path, home)) == home
    except ValueError:
        below_home = False
    current_path = os.sep
    try:
        for part in parts:
            created = False
            child = None
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                created = True
                child = os.open(part, flags, dir_fd=fd)
            current_path = os.path.join(current_path, part)
            try:
                info = os.fstat(child)
                if not stat.S_ISDIR(info.st_mode):
                    raise StoreError(
                        "store path component is not a directory: %s" % part)
                if created and os.geteuid() == 0 and owner != (0, 0):
                    os.fchown(child, owner[0], owner[1])
                    info = os.fstat(child)
                if uid != 0:
                    if info.st_uid == uid:
                        traversable = bool(info.st_mode & stat.S_IXUSR)
                    elif info.st_gid == gid:
                        traversable = bool(info.st_mode & stat.S_IXGRP)
                    else:
                        traversable = bool(info.st_mode & stat.S_IXOTH)
                    if not traversable:
                        raise StoreError(
                            "store path is not traversable by its owner")
                if (below_home
                        and os.path.commonpath((current_path, home)) == home
                        and info.st_uid != uid):
                    raise StoreError(
                        "store path below the user home has unexpected owner")
            except (OSError, StoreError):
                os.close(child)
                raise
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if parts and info.st_uid != owner[0]:
            raise StoreError("store directory has unexpected owner")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise StoreError("store directory is group/world writable")
        return fd
    except FileNotFoundError:
        os.close(fd)
        raise
    except (OSError, StoreError) as exc:
        os.close(fd)
        if isinstance(exc, StoreError):
            raise
        raise StoreError("cannot open store %s: %s" % (path, exc)) from exc


def _open_regular(directory_fd, name, flags, mode=0o600):
    flags |= getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        fd = os.open(name, flags | os.O_CREAT | os.O_EXCL,
                     mode, dir_fd=directory_fd)
        created = True
    except FileExistsError:
        fd = os.open(name, flags, mode, dir_fd=directory_fd)
    if created and os.geteuid() == 0:
        _, uid, gid = effective_user()
        if (uid, gid) != (0, 0):
            os.fchown(fd, uid, gid)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise StoreError("store entry is not a regular file: %s" % name)
    expected_uid = effective_user()[1]
    if info.st_uid != expected_uid:
        os.close(fd)
        raise StoreError("store entry has unexpected owner: %s" % name)
    if info.st_nlink != 1:
        os.close(fd)
        raise StoreError("store entry has multiple hard links: %s" % name)
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        os.close(fd)
        raise StoreError("store entry is group/world writable: %s" % name)
    return fd


class Corpus:
    """Descriptor-relative corpus access with one nonblocking writer lock."""

    def __init__(self, path=None, retention_days=DEFAULT_RETENTION_DAYS):
        if isinstance(retention_days, bool) or not isinstance(retention_days, int):
            raise ValueError("retention_days must be an integer")
        if retention_days <= 0:
            raise ValueError("retention_days must be > 0")
        if retention_days > MAX_RETENTION_DAYS:
            raise ValueError(
                "retention_days must be <= %d" % MAX_RETENTION_DAYS)
        self.path = os.path.abspath(path or default_store())
        self.retention_days = retention_days
        self.directory_fd = None
        self.lock_fd = None

    def acquire(self):
        if self.lock_fd is not None:
            return self
        self.directory_fd = _directory_fd(self.path, create=True)
        try:
            self.lock_fd = _open_regular(
                self.directory_fd, LOCK_NAME, os.O_RDWR)
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise LockError("record store already has a writer") from exc
                raise StoreError("cannot lock record store: %s" % exc) from exc
            return self
        except StoreError:
            self.close()
            raise
        except OSError as exc:
            self.close()
            raise StoreError("cannot open record writer lock: %s" % exc) from exc

    def close(self):
        if self.lock_fd is not None:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self.lock_fd)
                self.lock_fd = None
        if self.directory_fd is not None:
            os.close(self.directory_fd)
            self.directory_fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def append(self, record):
        """Append one complete strict JSON object with one write(2) call."""
        if self.lock_fd is None:
            raise StoreError("writer lock is not held")
        reason = validate_record(record)
        if reason is not None:
            raise StoreError("invalid raw record: %s" % reason)
        try:
            payload = (json.dumps(
                record, allow_nan=False, separators=(",", ":"),
                sort_keys=True) + "\n").encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise StoreError("record JSON encoding failed: %s" % exc) from exc
        if len(payload) > MAX_RAW_LINE_BYTES:
            raise StoreError("raw record exceeds the maximum line size")
        name = daily_name(record["recorded_at"])
        try:
            fd = _open_regular(
                self.directory_fd, name, os.O_RDWR | os.O_APPEND)
            try:
                size = os.fstat(fd).st_size
                if size and os.pread(fd, 1, size - 1) != b"\n":
                    raise StoreError(
                        "cannot append %s: existing final line is incomplete"
                        % name)
                written = os.write(fd, payload)
                if written != len(payload):
                    raise StoreError("short append to %s" % name)
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise StoreError("cannot append %s: %s" % (name, exc)) from exc

    def retain(self, now=None):
        """Delete complete daily files older than the configured local dates."""
        if self.lock_fd is None:
            raise StoreError("writer lock is not held")
        now = time.time() if now is None else now
        if not _timestamp_is_representable(now):
            raise StoreError("retention time is outside the local clock range")
        try:
            cutoff = (
                datetime.datetime.fromtimestamp(now).date()
                - datetime.timedelta(days=self.retention_days - 1))
        except (OverflowError, ValueError) as exc:
            raise StoreError("retention cutoff is outside the date range") from exc
        failures = []
        try:
            names = os.listdir(self.directory_fd)
        except OSError as exc:
            raise StoreError("cannot list record store: %s" % exc) from exc
        for name in names:
            match = DAILY_RE.match(name)
            if not match:
                continue
            try:
                day = datetime.date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if day < cutoff:
                try:
                    os.unlink(name, dir_fd=self.directory_fd)
                except OSError as exc:
                    failures.append("%s:%s" % (name, exc))
        if failures:
            raise StoreError("retention failed: %s" % ", ".join(failures))


def daily_name(recorded_at):
    try:
        timestamp = float(recorded_at)
    except (TypeError, ValueError, OverflowError) as exc:
        raise StoreError("recorded_at must be an epoch timestamp") from exc
    if not _timestamp_is_representable(timestamp, nonnegative=True):
        raise StoreError("recorded_at is outside the local clock range")
    try:
        day = datetime.datetime.fromtimestamp(timestamp)
    except (OverflowError, OSError, ValueError) as exc:
        raise StoreError("recorded_at is outside the local clock range") from exc
    return day.strftime("%Y-%m-%d.jsonl")


def validate_record(record):
    if not isinstance(record, dict):
        return "not_an_object"
    if record.get("schema") != RAW_SCHEMA:
        return "schema_mismatch"
    timestamp = record.get("recorded_at")
    if not _timestamp_is_representable(timestamp, nonnegative=True):
        return "invalid_recorded_at"
    if not isinstance(record.get("context"), dict):
        return "invalid_context"
    context = record["context"]
    if (not isinstance(context.get("root"), bool)
            or context.get("privilege") not in ("root", "user")
            or context["root"] != (context["privilege"] == "root")
            or context.get("power_state") not in ("ac", "battery", "unknown")
            or isinstance(context.get("local_hour"), bool)
            or not isinstance(context.get("local_hour"), int)
            or not 0 <= context["local_hour"] <= 23
            or not isinstance(context.get("timezone"), str)
            or not context["timezone"]
            or len(context["timezone"]) > 64
            or not all(char.isprintable() for char in context["timezone"])
            or not isinstance(context.get("sampler"), dict)
            or not isinstance(context.get("coverage"), dict)):
        return "invalid_context"
    sampler = context["sampler"]
    if (isinstance(sampler.get("pid"), bool)
            or not isinstance(sampler.get("pid"), int)
            or sampler["pid"] <= 0
            or (sampler.get("start_ticks") is not None
                and (isinstance(sampler["start_ticks"], bool)
                     or not isinstance(sampler["start_ticks"], int)
                     or sampler["start_ticks"] < 0))
            or not isinstance(sampler.get("name"), str)
            or not sampler["name"]
            or not isinstance(sampler.get("normalized_name"), str)
            or not sampler["normalized_name"]
            or len(sampler["normalized_name"]) > 1024
            or not all(char.isprintable()
                       for char in sampler["normalized_name"])):
        return "invalid_context"
    coverage = context["coverage"]
    coverage_fields = (
        "new_processes_zero_based",
        "unmatched_current_processes",
        "missing_endpoint_processes",
    )
    if (set(coverage) != set(coverage_fields)
            or any(isinstance(coverage.get(field), bool)
                   or not isinstance(coverage.get(field), int)
                   or coverage[field] < 0
                   for field in coverage_fields)):
        return "invalid_context"
    for field in ("interval_s", "requested_interval_s"):
        value = record.get(field)
        if not _finite_number(value) or value <= 0:
            return "invalid_%s" % field
    if (not isinstance(record.get("metrics"), list)
            or len(record["metrics"]) > MAX_METRICS_PER_RECORD):
        return "invalid_metrics"
    seen_metrics = set()
    for metric in record["metrics"]:
        if (not isinstance(metric, dict)
                or not isinstance(metric.get("scope"), str) or not metric["scope"]
                or len(metric["scope"]) > 64
                or not all(char.isprintable() for char in metric["scope"])
                or not isinstance(metric.get("metric"), str) or not metric["metric"]
                or len(metric["metric"]) > 64
                or not all(char.isprintable() for char in metric["metric"])
                or not isinstance(metric.get("unit"), str) or not metric["unit"]
                or len(metric["unit"]) > 64
                or not all(char.isprintable() for char in metric["unit"])
                or "value" not in metric):
            return "invalid_metric_entry"
        key = (metric["scope"], metric["metric"])
        if key in seen_metrics:
            return "duplicate_metric_entry"
        seen_metrics.add(key)
        expected_unit = REQUIRED_METRICS.get(key)
        if expected_unit is not None and metric["unit"] != expected_unit:
            return "invalid_metric_unit"
        value = metric.get("value")
        if value is not None and not _finite_number(value):
            return "invalid_metric_entry"
        if (value is not None and key in REQUIRED_METRICS
                and key not in SIGNED_METRICS and value < 0):
            return "invalid_metric_domain"
    missing_metrics = set(REQUIRED_METRICS).difference(seen_metrics)
    if missing_metrics:
        scope, metric = sorted(missing_metrics)[0]
        return "missing_metric:%s.%s" % (scope, metric)
    if (not isinstance(record.get("processes"), list)
            or len(record["processes"]) > MAX_PROCESSES_PER_RECORD):
        return "invalid_processes"
    seen_processes = set()
    for process in record["processes"]:
        if (not isinstance(process, dict)
                or isinstance(process.get("pid"), bool)
                or not isinstance(process.get("pid"), int)
                or process["pid"] <= 0
                or isinstance(process.get("start_ticks"), bool)
                or not isinstance(process.get("start_ticks"), int)
                or process["start_ticks"] < 0
                or not isinstance(process.get("name"), str)
                or not process["name"]
                or not isinstance(process.get("normalized_name"), str)
                or not process["normalized_name"]
                or len(process["normalized_name"]) > 1024
                or not all(char.isprintable()
                           for char in process["normalized_name"])):
            return "invalid_process_entry"
        identity = (process["pid"], process["start_ticks"])
        if identity in seen_processes:
            return "duplicate_process_entry"
        seen_processes.add(identity)
        if set(process).difference(PROCESS_FIELDS):
            return "unknown_process_field"
        for field in PROCESS_METRICS:
            if field not in process:
                return "missing_process_metric:%s" % field
            value = process[field]
            if value is not None and not _finite_number(value):
                return "invalid_process_metric:%s" % field
            if value is not None and value < 0:
                return "invalid_process_metric:%s" % field
    if not isinstance(record.get("partial"), bool):
        return "invalid_partial"
    if (not isinstance(record.get("partial_reasons"), list)
            or len(record["partial_reasons"]) > 64
            or not all(isinstance(reason, str)
                       and reason and len(reason) <= 256
                       and all(char.isprintable() for char in reason)
                       for reason in record["partial_reasons"])):
        return "invalid_partial_reasons"
    if record["partial"] != bool(record["partial_reasons"]):
        return "inconsistent_partial_state"
    endpoint_gap = bool(
        coverage["unmatched_current_processes"]
        or coverage["missing_endpoint_processes"])
    if endpoint_gap != ("process_endpoint_gaps" in record["partial_reasons"]):
        return "inconsistent_process_coverage"
    if not _finite_tree(record):
        return "non_finite_or_non_json_value"
    return None


def scan(path=None, since=None, visitor=None):
    """Stream valid matching records through ``visitor`` and report corruption.

    Every malformed line is reported.  A final line without ``\n`` is parsed
    when possible but still reported as ``partial_final_line``.
    """
    corpus_path = os.path.abspath(path or default_store())
    try:
        directory_fd = _directory_fd(corpus_path, create=False)
    except StoreError:
        raise
    except FileNotFoundError:
        return {
            "record_count": 0, "errors": [], "error_count": 0,
            "errors_omitted": 0, "files": [],
        }
    if since is not None and not _finite_number(since):
        os.close(directory_fd)
        raise ValueError("since must be a finite epoch timestamp")
    record_count = 0
    errors = []
    error_count = 0
    files = []

    def report_error(name, line, reason):
        nonlocal error_count
        error_count += 1
        if len(errors) < MAX_REPLAY_ERRORS:
            errors.append({
                "file": name, "line": line, "reason": reason,
            })

    try:
        try:
            names = sorted(name for name in os.listdir(directory_fd)
                           if DAILY_RE.match(name))
        except OSError as exc:
            raise StoreError("cannot list record store: %s" % exc) from exc
        for name in names:
            fd = None
            try:
                try:
                    fd = os.open(
                        name,
                        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=directory_fd)
                except FileNotFoundError:
                    continue
                files.append(name)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    os.close(fd)
                    raise StoreError("record entry is not regular: %s" % name)
                if (info.st_uid != effective_user()[1]
                        or info.st_nlink != 1
                        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
                    os.close(fd)
                    raise StoreError(
                        "record entry has unsafe ownership/permissions: %s"
                        % name)
                with os.fdopen(fd, "rb") as stream:
                    fd = None
                    index = 0
                    while True:
                        raw = stream.readline(MAX_RAW_LINE_BYTES + 1)
                        if not raw:
                            break
                        index += 1
                        if len(raw) > MAX_RAW_LINE_BYTES:
                            complete = raw.endswith(b"\n")
                            while not complete:
                                chunk = stream.readline(MAX_RAW_LINE_BYTES + 1)
                                if not chunk:
                                    break
                                complete = chunk.endswith(b"\n")
                            report_error(name, index, "line_too_long")
                            if not complete:
                                report_error(
                                    name, index, "partial_final_line")
                            continue
                        complete = raw.endswith(b"\n")
                        if not complete:
                            report_error(name, index, "partial_final_line")
                        try:
                            text = raw.decode("utf-8")
                            obj = json.loads(
                                text, parse_constant=_reject_constant,
                                parse_int=_parse_int, parse_float=_parse_float)
                            reason = validate_record(obj)
                            if reason is not None:
                                raise ValueError(reason)
                        except (UnicodeError, ValueError, TypeError,
                                OverflowError, RecursionError) as exc:
                            report_error(name, index, str(exc))
                            continue
                        if since is None or obj["recorded_at"] >= since:
                            record_count += 1
                            if visitor is not None:
                                visitor(obj)
            except (OSError, UnicodeError) as exc:
                if fd is not None:
                    os.close(fd)
                raise StoreError("cannot read %s: %s" % (name, exc)) from exc
    finally:
        os.close(directory_fd)
    return {
        "record_count": record_count,
        "errors": errors,
        "error_count": error_count,
        "errors_omitted": error_count - len(errors),
        "files": files,
    }


def replay(path=None, since=None):
    """Return materialized records for callers that explicitly need raw replay."""
    records = []
    result = scan(path, since, records.append)
    records.sort(key=lambda item: item["recorded_at"])
    return {
        "records": records,
        "errors": result["errors"],
        "error_count": result["error_count"],
        "errors_omitted": result["errors_omitted"],
        "files": result["files"],
    }


_RELATIVE_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(s|sec(?:ond)?s?|m|min(?:ute)?s?|"
    r"h|hours?|d|days?)\s*(?:ago)?\s*$", re.I)
_CLOCK_RE = re.compile(r"^\s*(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap]m)\s*$",
                       re.I)


def parse_since(value, now=None):
    """Parse a relative duration, ISO-8601 timestamp, or local clock (``3am``)."""
    try:
        now = time.time() if now is None else float(now)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("current time must be an epoch timestamp") from exc
    if not _timestamp_is_representable(now):
        raise ValueError("current time is outside the clock range")
    if value is None:
        return now - 24 * 60 * 60
    match = _RELATIVE_RE.match(value)
    if match:
        amount = float(match.group(1))
        if not math.isfinite(amount):
            raise ValueError("--since duration must be finite")
        unit = match.group(2).lower()[0]
        scale = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        timestamp = now - amount * scale
        if not _timestamp_is_representable(timestamp):
            raise ValueError("--since duration is outside the clock range")
        return timestamp
    match = _CLOCK_RE.match(value)
    if match:
        hour = int(match.group(1)) % 12
        if match.group(3).lower() == "pm":
            hour += 12
        minute = int(match.group(2) or 0)
        current = datetime.datetime.fromtimestamp(now)
        candidates = []
        for days_back in (0, 1):
            day = current.date() - datetime.timedelta(days=days_back)
            fields = (day.year, day.month, day.day, hour, minute, 0)
            for is_dst in (-1, 0, 1):
                try:
                    timestamp = time.mktime(
                        fields + (0, 0, is_dst))
                except (OverflowError, OSError, ValueError):
                    continue
                local = time.localtime(timestamp)
                if (local.tm_year, local.tm_mon, local.tm_mday,
                        local.tm_hour, local.tm_min, local.tm_sec) == fields:
                    if timestamp <= now:
                        candidates.append(timestamp)
        if not candidates:
            raise ValueError("--since local time is outside the clock range")
        return max(candidates)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            "--since wants a duration (3h), ISO timestamp, or local time (3am)"
        ) from exc
    try:
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        timestamp = parsed.timestamp()
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("--since timestamp is outside the clock range") from exc
    if not _timestamp_is_representable(timestamp):
        raise ValueError("--since timestamp is outside the clock range")
    return timestamp


class Reservoir:
    """Deterministic-seed, bounded reservoir with exact observed count."""

    def __init__(self, size=DEFAULT_RESERVOIR_SIZE, seed=0):
        if size <= 0:
            raise ValueError("reservoir size must be > 0")
        self.size = size
        self.count = 0
        self.values = []
        self._seed = seed
        self._random = None

    def add(self, value):
        if not _finite_number(value):
            raise ValueError("reservoir values must be finite numbers")
        self.count += 1
        if len(self.values) < self.size:
            self.values.append(float(value))
            return
        if self._random is None:
            self._random = random.Random(self._seed)
        index = self._random.randrange(self.count)
        if index < self.size:
            self.values[index] = float(value)

    def percentile(self, percent):
        if not self.values:
            return None
        if (isinstance(percent, bool)
                or not isinstance(percent, (int, float))
                or not math.isfinite(percent)
                or not 0 <= percent <= 100):
            raise ValueError("percentile must be between 0 and 100")
        ordered = sorted(self.values)
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * percent / 100.0
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:
            return ordered[lower]
        fraction = position - lower
        value = (
            ordered[lower] * (1.0 - fraction)
            + ordered[upper] * fraction)
        if not math.isfinite(value):
            raise ValueError("percentile interpolation overflowed")
        return value

    def summary(self):
        return {
            "count": self.count,
            "sample_count": len(self.values),
            "p50": self.percentile(50),
            "p90": self.percentile(90),
            "p99": self.percentile(99),
        }


class CandidateReservoirs:
    """Bound process-name cardinality while retaining the strongest candidates."""

    def __init__(self, capacity, reservoir_size=DEFAULT_RESERVOIR_SIZE, seed=0):
        if capacity <= 0:
            raise ValueError("candidate capacity must be > 0")
        self.capacity = capacity
        self.reservoir_size = reservoir_size
        self.seed = seed
        self.entries = {}
        self.dropped_values = 0

    def add(self, key, value, allow_new=True):
        reservoir = self.entries.get(key)
        if reservoir is not None:
            reservoir.add(value)
            return 0
        if allow_new and len(self.entries) < self.capacity:
            reservoir = Reservoir(self.reservoir_size, self.seed)
            reservoir.add(value)
            self.entries[key] = reservoir
            return 1
        if self.entries:
            weakest_key, weakest = min(
                self.entries.items(),
                key=lambda item: (
                    item[1].percentile(90), str(item[0])))
            if value > weakest.percentile(90):
                self.dropped_values += weakest.count
                del self.entries[weakest_key]
                reservoir = Reservoir(self.reservoir_size, self.seed)
                reservoir.add(value)
                self.entries[key] = reservoir
                return 0
        self.dropped_values += 1
        return 0


def _metric_values(record):
    for metric in record.get("metrics", []):
        if not isinstance(metric, dict):
            continue
        value = metric.get("value")
        if _finite_number(value):
            yield (metric.get("scope"), metric.get("metric"), None, value)
    for process in record.get("processes", []):
        if not isinstance(process, dict):
            continue
        name = process.get("normalized_name")
        for field, value in process.items():
            if field in ("pid", "start_ticks", "name", "normalized_name"):
                continue
            if _finite_number(value):
                scope = {
                    "cpu_pct": "cpu", "user_pct": "cpu", "system_pct": "cpu",
                    "pkg_idle_wakeups_per_s": "cpu",
                    "interrupt_wakeups_per_s": "cpu",
                    "diskio_bytes_read_per_s": "disk",
                    "diskio_bytes_written_per_s": "disk",
                    "energy_rate_watts": "battery",
                    "energy_score_per_s": "battery",
                    "footprint_bytes": "memory",
                    "resident_size_bytes": "memory",
                }.get(field, "process")
                yield scope, field, name, value


class BaselineAccumulator:
    """Incrementally build bounded contextual percentile reservoirs."""

    def __init__(self, scope=None, reservoir_size=DEFAULT_RESERVOIR_SIZE, seed=0,
                 process_candidates=64):
        self.scope = scope
        self.reservoir_size = reservoir_size
        self.seed = seed
        self.buckets = {}
        self.process_candidates = process_candidates
        self.process_groups = {}
        self.process_bucket_count = 0
        self.dropped_values = 0

    def add(self, record):
        context = record.get("context", {})
        common = (
            context.get("local_hour"), context.get("timezone"),
            context.get("privilege"), context.get("power_state"))
        for metric_scope, metric, process_name, value in _metric_values(record):
            if self.scope is not None and metric_scope != self.scope:
                continue
            key = common + (metric_scope, metric, process_name)
            if process_name is None:
                if key not in self.buckets:
                    if len(self.buckets) >= MAX_BASELINE_GROUPS:
                        self.dropped_values += 1
                        continue
                    self.buckets[key] = Reservoir(
                        self.reservoir_size, self.seed)
                self.buckets[key].add(value)
                continue
            group_key = common + (metric_scope, metric)
            candidates = self.process_groups.get(group_key)
            if candidates is None:
                if len(self.process_groups) >= MAX_BASELINE_GROUPS:
                    self.dropped_values += 1
                    continue
                candidates = CandidateReservoirs(
                    self.process_candidates, self.reservoir_size, self.seed)
                self.process_groups[group_key] = candidates
            new_key = process_name not in candidates.entries
            allow_new = (
                not new_key
                or self.process_bucket_count < MAX_BASELINE_PROCESS_BUCKETS)
            change = candidates.add(
                process_name, value, allow_new=allow_new)
            self.process_bucket_count += change

    def rows(self):
        rows = []
        combined = dict(self.buckets)
        for group_key, candidates in self.process_groups.items():
            self.dropped_values += candidates.dropped_values
            candidates.dropped_values = 0
            for process_name, reservoir in candidates.entries.items():
                combined[group_key + (process_name,)] = reservoir
        for key in sorted(combined, key=lambda item: tuple(
                "" if part is None else str(part) for part in item)):
            (local_hour, timezone, privilege, power_state, metric_scope,
             metric, name) = key
            summary = combined[key].summary()
            rows.append({
                "local_hour": local_hour,
                "timezone": timezone,
                "privilege": privilege,
                "power_state": power_state,
                "scope": metric_scope,
                "metric": metric,
                "normalized_process_name": name,
                "cold": summary["count"] == 0,
                **summary,
            })
        return rows


def percentile_baselines(records, scope=None, reservoir_size=DEFAULT_RESERVOIR_SIZE,
                         seed=0):
    """Group values by local context and return explicit cold/percentile rows."""
    accumulator = BaselineAccumulator(scope, reservoir_size, seed)
    for record in records:
        accumulator.add(record)
    return accumulator.rows()
