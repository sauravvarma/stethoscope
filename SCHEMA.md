# stethoscope machine-readable schema

`--json` commands emit structures directly from their data layer. They never
parse human-rendered output. Streaming commands write one JSON document per
completed sample interval, one document per line (NDJSON).

## Compatibility

Every document has `"schema": "stethoscope/1"`.

- Consumers must ignore unknown fields.
- Adding a field is compatible and does not change the schema identifier.
- Removing or renaming a field, changing its type, or changing a documented
  unit requires a new schema identifier.
- JSON `null` means known-but-unavailable. Stable fields are not omitted merely
  because a probe failed or the hardware is absent.

## Common envelope

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | Contract identifier, currently `stethoscope/1` |
| `scope` | string | Data namespace, such as `disk` |
| `command` | string | Invoked command, such as `top` |
| `partial` | boolean | `true` when visibility is known to be incomplete |
| `partial_reasons` | array of strings | Machine-readable reasons, currently including `not_root` |

Rates named `*_per_s` are per second. Byte counters and totals are bytes.
Times are seconds unless a field documents another unit.

## Common flags

- `--json`: emit this contract instead of human text.
- `--once`: live commands complete one requested sample interval and exit.
- `--duration N`: live commands emit completed intervals until at least `N`
  seconds have elapsed.
- `--interval N`: positive sample interval in seconds.
- `--limit N`: positive maximum row count.

Static commands reject sampling flags they cannot honor. Human-only commands
reject `--json` rather than returning non-JSON output.

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | Probe completed and no command-defined finding was present |
| `1` | Probe completed and found a reportable condition |
| `2` | Invalid invocation |
| `3` | Required permission is unavailable |
| `4` | Probe/runtime failure |

Hardware absence or an optional external tool being unavailable is represented
in data when it is a supported state; it is not automatically a probe failure.

## `disk top`

```json
{
  "schema": "stethoscope/1",
  "scope": "disk",
  "command": "top",
  "partial": true,
  "partial_reasons": ["not_root"],
  "system": {"read_per_s": 158105.6, "write_per_s": 134348.8},
  "processes": [
    {
      "pid": 1263,
      "name": "corespotlightd",
      "read_per_s": 16384.0,
      "write_per_s": 0.0,
      "read_total": 20718518272,
      "write_total": 1717986918
    }
  ]
}
```

`processes` is ranked by total throughput and truncated to `--limit`. Exit
code: `0`.

## `disk holds <pid>`

```json
{
  "schema": "stethoscope/1",
  "scope": "disk",
  "command": "holds",
  "partial": false,
  "partial_reasons": [],
  "pid": 1000,
  "name": "bash",
  "cumulative": {"read": 16384, "write": 0},
  "holds": [
    {"reason": "working dir (cwd)", "type": "DIR", "path": "/Users/example"}
  ],
  "error": null
}
```

`cumulative` is `null` when inaccessible. On a probe failure, `cumulative` is
also present and `null`, `holds` is empty, and `error` is a string. Exit code:
`0` on a completed probe, `4` on probe failure.

## `disk busy <volume|device>`

```json
{
  "schema": "stethoscope/1",
  "scope": "disk",
  "command": "busy",
  "partial": true,
  "partial_reasons": ["not_root"],
  "target": "/Volumes/X9 Pro",
  "targets": [{"device": "/dev/disk6s2", "mount": "/Volumes/X9 Pro"}],
  "holders": [
    {
      "pid": 1263,
      "name": "mds",
      "user": "root",
      "reasons": {"open (read)": 3},
      "paths": ["/Volumes/X9 Pro/a"],
      "io": {"read": 123, "write": 0}
    }
  ],
  "error": null
}
```

`io` is `null` when inaccessible. No matching mounted target returns empty
`targets`/`holders`, an error string, and exit `2`. Otherwise exit is `1` when
holders exist and `0` when clean.

## `disk inspect <pid>`

`inspect` is a human-only live `fs_usage` trace. Agent/sampling flags are
rejected with exit `2`; missing root permission returns exit `3`.

## `cpu top` and `cpu wakeups`

Both commands emit the same shape. `top` orders `processes` by `cpu_pct`;
`wakeups` orders by `total_wakeups_per_s`. The two kernel wakeup counters
remain separate fields because diagnosis compares each with its own baseline;
the total exists only as a view/ranking convenience.

```json
{
  "schema": "stethoscope/1",
  "scope": "cpu",
  "command": "wakeups",
  "partial": true,
  "partial_reasons": ["not_root"],
  "system": {
    "cpu_pct": 143.9,
    "watts": 4.8,
    "pkg_idle_wakeups_per_s": 2.0,
    "interrupt_wakeups_per_s": 812.0,
    "total_wakeups_per_s": 814.0,
    "ncpu": 8
  },
  "processes": [
    {
      "pid": 29641,
      "name": "worker",
      "cpu_pct": 93.9,
      "user_pct": 90.0,
      "system_pct": 3.9,
      "watts": 3.2,
      "total_cpu_seconds": 120.5,
      "lifetime_duty_pct": 71.2,
      "pkg_idle_wakeups_per_s": 1.0,
      "interrupt_wakeups_per_s": 400.0,
      "total_wakeups_per_s": 401.0
    }
  ]
}
```

`watts` is `null` where rusage flavor 6 is unavailable; it is never replaced
with a fabricated zero. `%CPU` can exceed 100 for a multi-core process. Exit
code: `0`; malformed invocations return `2`.

## `memory top`

```json
{
  "schema": "stethoscope/1",
  "scope": "memory",
  "command": "top",
  "partial": false,
  "partial_reasons": [],
  "system": {
    "available": true,
    "errors": [],
    "total": 17179869184,
    "used": 11274289152,
    "free": 732954624,
    "active": 5046586572,
    "inactive": 2147483648,
    "wired": 3221225472,
    "compressed": 3006477108,
    "pressure": "normal"
  },
  "processes": [
    {
      "pid": 1234,
      "name": "WindowServer",
      "footprint_bytes": 734003200,
      "resident_size_bytes": 812345344
    }
  ]
}
```

System-memory probe failures set affected byte fields to `null`, add stable
codes to `system.errors`, set `system.available` false, and mark the document
partial with reason `system_memory_probe`. `pressure` is `normal`, `warn`,
`critical`, or `unknown`; unknown is never equivalent to healthy. Exit code:
`0`; malformed invocations return `2`.

## `memory watch <pid>`

Each sample contains:

```json
{
  "schema": "stethoscope/1",
  "scope": "memory",
  "command": "watch",
  "partial": false,
  "partial_reasons": [],
  "pid": 1234,
  "name": "worker",
  "running": true,
  "footprint_bytes": 268435456,
  "resident_size_bytes": 301989888,
  "slope_mb_per_min": 4.2,
  "plateaued": false,
  "leak_candidate": true,
  "samples": 12
}
```

`leak_candidate` latches once sustained positive growth without a plateau is
observed. If the process exits or its PID is reused, one final document has
`running: false` and nullable footprint/resident/slope/plateau fields. Exit is
`1` when the run latched a leak candidate, `0` otherwise, `2` for a missing
PID, and `3` when the process exists but is inaccessible.

## `smart status [disk]`

```json
{
  "schema": "stethoscope/1",
  "scope": "smart",
  "command": "status",
  "partial": false,
  "partial_reasons": [],
  "drives": [
    {
      "device": "disk0",
      "internal": true,
      "name": "APPLE SSD",
      "size_bytes": 251000193024,
      "solid_state": true,
      "smart_status": "verified",
      "diskutil_detail": null,
      "source": "smartctl",
      "smartctl_available": true,
      "smartctl_detail": null,
      "smartctl_exit_status": 0,
      "passed": true,
      "percentage_used": 12,
      "power_on_hours": 5000,
      "data_units_written": 123456,
      "tbw_tb": 0.06,
      "available_spare": 100,
      "available_spare_threshold": 10,
      "media_errors": 0,
      "critical_warning": 0,
      "reallocated_sector_ct": null,
      "reallocated_event_count": null,
      "current_pending_sector": null,
      "offline_uncorrectable": null,
      "reported_uncorrectable": null,
      "ata_failing_attributes": [],
      "ata_usage_attributes_now": [],
      "ata_failed_attributes_past": [],
      "temperature_c": 42,
      "life": {
        "remaining_life_pct": 88,
        "consumed_life_pct": 12,
        "remaining_hours": 36667,
        "remaining_years": 4.2,
        "confidence": "moderate"
      },
      "warnings": [],
      "worst_severity": "ok"
    }
  ],
  "error": null
}
```

`diskutil` supplies the dependency-free verdict and inventory. When available,
`smartctl` supplies wear, endurance, temperature, and warning inputs. Every
optional measurement remains present as `null` when unknown; a valid zero is
not treated as missing. `smart_status` is `verified`, `failing`,
`not supported`, or `unknown`. `life.confidence` is `low`, `moderate`, or
`high`. Each warning contains stable `code`, `severity`, and `message` fields.
`smartctl_exit_status` preserves smartctl's bitmask so failing status,
attribute-threshold, device-error-log, and self-test-log signals are not lost.
The ATA attribute arrays distinguish current pre-failure thresholds, current
old-age/usage thresholds, and prior threshold crossings when smartctl supplies
them. `tbw_tb` may be derived from NVMe data units or common ATA host-write
counters; `data_units_written` remains NVMe-specific.

Missing smartctl detail marks the document partial with
`smartctl_unavailable` or `smartctl_probe_incomplete`; the drive and diskutil
verdict remain usable. A per-drive diskutil info failure adds
`diskutil_probe_incomplete`. A diskutil enumeration failure sets
`partial_reasons: ["diskutil_unavailable"]`, returns an empty `drives` array,
sets `error`, and exits `4`. Exit is `1` when any drive has a critical state
or warning, `0` when no finding is present, and `2` for an unknown requested
disk or malformed invocation.
As a static command, SMART supports `--json` but rejects sampling flags.

## Changelog

- `stethoscope/1`: stable common envelope and disk/CPU/memory/SMART contracts.
