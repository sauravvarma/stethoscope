# stethoscope machine-readable schema

`--json` commands emit structures directly from their data layer. They never
parse human-rendered output. Streaming commands write one JSON document per
completed sample interval, one document per line (NDJSON).

Start with the [agent walkthrough](docs/agent-walkthrough.md) for an
investigation sequence and MCP handshake. Human command synopses are in
[README.md](README.md) and the [man-page source](man/stethoscope.1); architecture
and review rationale are in [ARCHITECTURE.md](ARCHITECTURE.md),
[DESIGN.md](DESIGN.md), and
[docs/review-disposition.md](docs/review-disposition.md).

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

## MCP transport

`stethoscope mcp` exposes ten read-only tools over UTF-8 newline-delimited
JSON-RPC 2.0 on stdio using MCP protocol `2025-11-25`. Standard output contains
protocol messages only; diagnostics use standard error. A client must send a
valid `initialize` request first, then `notifications/initialized`, before
`ping`, `tools/list`, or `tools/call`. Valid notifications never receive a
response. The server advertises `{"tools":{"listChanged":false}}` and reads its
`serverInfo.version` from `VERSION`.

Each successful `tools/call` result has one compact, strict JSON text content
item and an identical `structuredContent` document. Exit codes `0` and `1`
produce `isError: false`; `2`, `3`, and `4` produce `isError: true`. Non-finite
or non-serializable values are rejected, never coerced. JSON-RPC errors are:
parse `-32700`, invalid request/lifecycle `-32600`, unknown method `-32601`,
invalid parameters or unknown tool `-32602`, and internal protocol/tool failure
`-32603`.

Input lines, output documents, repeated calls, strings, sampling intervals
(`0 < interval <= 60`), and limits (`1..256`) are bounded. Booleans are not
accepted as numbers or integers, and tool argument objects reject unknown
properties. Request IDs may be integers or strings; null, booleans, fractional
numbers, and reuse within a session are invalid. Request parameter objects may
carry the MCP-standard `_meta` object. Structurally valid notifications remain
silent, including recognized notifications whose object parameters fail
method-level validation. A malformed no-ID envelope is an invalid request, not a
notification, and receives `-32600`.

The concrete transport ceilings are 1 MiB per input line, 4 MiB per structured
tool document, 256 tool calls, and 4096 unique request IDs per server process;
string IDs are at most 256 UTF-8 bytes. The disk mount/lsof probes used by
`disk_busy` have a 15-second deadline and a 4 MiB combined output ceiling.
Exceeding a native-probe bound remains a structured exit-4 tool result rather
than terminating the MCP session.

Integer request IDs are restricted to signed 64-bit values, and incoming JSON
numeric tokens are rejected before conversion when their textual form exceeds
64 characters. Both `disk_holds` and `disk_busy` use the bounded native-probe
runner.

The tools are `disk_top`, `disk_holds`, `disk_busy`, `cpu_top`, `cpu_wakeups`,
`memory_top`, `battery_health`, `battery_top`, `smart_status`, and `checkup`.
Their `structuredContent` is the corresponding document described below;
`checkup` is returned directly rather than wrapped. Recording/history stores,
battery drainers, kill/eject actions, and root-heavy inspect commands are
deliberately not exposed.

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

## `battery health`

```json
{
  "schema": "stethoscope/1",
  "scope": "battery",
  "command": "health",
  "partial": false,
  "partial_reasons": [],
  "present": true,
  "probe_error": null,
  "pmset_error": null,
  "charge_pct": 72.0,
  "state": "discharging",
  "time_remaining": "3:07",
  "cycle_count": 200,
  "health_pct": 89.0,
  "condition": "Normal",
  "capacities": {"design_mah": 4382, "max_mah": 3900},
  "temperature_c": 31.0,
  "charging": false,
  "external_connected": false,
  "fully_charged": false,
  "voltage_mv": 11585,
  "current_ma": -1302,
  "battery_flow_watts": -15.08367
}
```

`present` is `true` for a battery, `false` for the supported desktop/no-battery
state, and `null` when ioreg itself failed. Every measurement remains present
as `null` when unavailable. `condition` is `Normal`, `Service Recommended`, or
`null`; missing health evidence is never labeled normal. `battery_flow_watts`
is signed voltage times current: negative while discharging and positive while
charging. It is battery flow, not whole-system draw on AC.

A pmset failure leaves its supplemental state/time fields null and marks the
document partial with `pmset_unavailable`. An ioreg failure sets `probe_error`
and exits `4`. Service-recommended health exits `1`; healthy, unknown, absent,
or partial-without-a-finding exits `0`.

## `battery top`

```json
{
  "schema": "stethoscope/1",
  "scope": "battery",
  "command": "top",
  "partial": true,
  "partial_reasons": ["not_root"],
  "pmenergy_source": "/usr/share/pmenergy/default.plist",
  "system": {
    "cpu_pct": 104.2,
    "energy_rate_watts": 3.1,
    "energy_score_per_s": 1.8,
    "pkg_idle_wakeups_per_s": 20.0,
    "interrupt_wakeups_per_s": 800.0
  },
  "processes": [
    {
      "pid": 1234,
      "name": "worker",
      "cpu_pct": 92.0,
      "energy_rate_watts": 2.7,
      "energy_score_per_s": 1.5,
      "energy_share_pct": 83.3,
      "pkg_idle_wakeups_per_s": 10.0,
      "interrupt_wakeups_per_s": 400.0,
      "diskio_bytes_read_per_s": 0.0,
      "diskio_bytes_written_per_s": 4096.0
    }
  ]
}
```

`energy_rate_watts` is a real flavor-6 energy-ledger delta and is null where
unavailable. `energy_score_per_s` is Apple's unitless pmenergy-weighted ranking
formula; it is never labeled watts. All score inputs are normalized to the
sample interval, and CPU time uses the board's QoS-specific weights rather
than treating background and interactive work as equivalent. Interrupt
wakeups remain separate context and are not folded into the score. Non-root
visibility and missing pmenergy coefficients mark the document partial. Exit
code: `0`.

## `battery drainers`

```json
{
  "schema": "stethoscope/1",
  "scope": "battery",
  "command": "drainers",
  "partial": false,
  "partial_reasons": [],
  "present": true,
  "on_ac": false,
  "probe_error": null,
  "baseline_reset": false,
  "reset_reason": null,
  "charge_pct": 54.0,
  "charge_drop": 8.0,
  "elapsed_s": 3600.0,
  "pmenergy_source": "/usr/share/pmenergy/default.plist",
  "drainers": [
    {
      "pid": 1234,
      "name": "worker",
      "cpu_seconds_since": 120.0,
      "pkg_idle_wakeups_since": 500,
      "diskio_bytes_read_since": 1048576,
      "diskio_bytes_written_since": 4096,
      "energy_score_total": 124.2,
      "energy_joules_since": 850.0
    }
  ],
  "error": null
}
```

`on_ac` is always present: `true`/`false` when known and `null` for absent
hardware, probe failure, or unknown power state. The first invocation, AC
state, unplug transition, changed discharge session, reboot, charge increase,
or invalid baseline resets the baseline explicitly and returns
`baseline_reset: true` with a stable `reset_reason`. The persisted baseline is
schema-validated, boot/session/privilege-aware, atomically replaced, and never
follows user-controlled symlinks under sudo.

`energy_score_total` is cumulative and unitless; it is not the top command's
per-second score. `energy_joules_since` is a cumulative flavor-6 delta. Missing
power history, changed baseline visibility, non-root collection, or missing
pmenergy coefficients marks the result partial. Essential battery, power
state, boot-session, or baseline I/O failures set `error` and exit `4`.
Supported first-run/reset/no-battery states exit `0`.

## `battery inspect`

```json
{
  "schema": "stethoscope/1",
  "scope": "battery",
  "command": "inspect",
  "partial": false,
  "partial_reasons": [],
  "available": true,
  "reason": null,
  "observed_battery_flow_watts": -15.08,
  "observed_state": "discharging",
  "reconciliation_note": "powermetrics Energy Impact is unitless, not watts.",
  "tasks": [
    {
      "pid": 0,
      "name": "kernel_task",
      "energy_impact_per_s": 2.0,
      "energy_impact_total": 10.0
    }
  ]
}
```

Powermetrics' per-second and sample-total Energy Impact values remain separate
and unitless. PID `0` is `kernel_task`; PID `-1` is powermetrics'
`DEAD_TASKS` aggregate for work that exited during the sample. `available`,
`reason`, observed fields, note, and `tasks` are stable even on errors.
Inspect requires root: no permission exits `3`; an unavailable or malformed
powermetrics sample exits `4`; success exits `0`.

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

## `triage` and `anomaly`

`triage` is the direct one-shot diagnosis command. `anomaly` requires exactly
one mode: `deviation`, `leaks`, `runaway`, or `triage`. Both accept `--json`,
`--interval N`, `--limit N`, `--since WHEN`, and `--store DIR`; they reject
`--once`, `--duration`, unknown modes, and extra positionals. Defaults are a
1-second completed live interval, limit 20, history since 24 hours ago, and the
canonical `baseline-raw/1` store. Interval is capped at 60 seconds and limit at
256.

All modes use the same stable result fields:

```json
{
  "schema": "stethoscope/1",
  "scope": "triage",
  "command": "triage",
  "partial": true,
  "partial_reasons": ["not_root"],
  "mode": "triage",
  "overall": "warn",
  "findings": [
    {
      "code": "memory_pressure_warn",
      "severity": "warn",
      "area": "memory",
      "detector": "point",
      "message": "kernel memory pressure is elevated",
      "score": 60,
      "confidence": "high",
      "drill_down": ["stethoscope memory top"],
      "evidence": {"pressure": "warn"}
    }
  ],
  "notes": [],
  "history": {
    "available": true,
    "error": null,
    "raw_schema": "baseline-raw/1",
    "store": "/Users/example/.stethoscope/baseline-raw",
    "since": 1783683000.0,
    "record_count": 120,
    "matching_context_records": 8,
    "cold": false,
    "replay_errors": [],
    "replay_error_count": 0,
    "replay_errors_omitted": 0,
    "files": ["2026-07-11.jsonl"],
    "source_partial_reasons": ["not_root"],
    "source_partial_reasons_omitted": 0,
    "trend_invalid_count": 0,
    "sampler_baseline_resets": 0
  },
  "current": {
    "recorded_at": 1783769400.0,
    "interval_s": 1.01,
    "partial": false,
    "partial_reasons": [],
    "context": {},
    "metrics": [],
    "processes": [],
    "points": {
      "memory": {"pressure": "warn"},
      "battery": {"present": false},
      "smart": {
        "available": true,
        "diskutil_available": true,
        "physical_drives_present": false,
        "smartctl_available": false,
        "drives": []
      }
    }
  },
  "error": null
}
```

For `anomaly`, `scope` is `anomaly` and `command`/`mode` name the selected
mode. For direct `triage`, both are `triage`. `current.points` is populated by
triage and is `null` in individual detector modes. `current` is `null` if the
live interval could not be collected. Success, usage-error JSON, and runtime
error JSON retain `overall`, `findings`, `notes`, `history`, `current`, and
`error`.

Every finding has a stable `code`, `severity` (`info`, `warn`, or `critical`),
`area`, `detector`, human `message`, integer ordinal `score` from 0 through 100,
ordinal `confidence` (`low`, `moderate`, or `high`), an array of
`drill_down` commands, and detector-specific `evidence`. Scores and confidence
are not probabilities. Findings sort by severity, score, area, detector, code,
and message; `overall` is the worst severity, or `ok` when there are no
findings.

System deviation uses only selected system metrics and the exact current
hour/timezone/privilege/power-state context. Degenerate robust bands are widened
with metric-specific absolute and relative floors: small memory noise remains
clean while a stable-zero CPU or wakeup baseline still detects a material
spike. Signed battery flow is excluded. Process deviation is not emitted, so it
cannot duplicate runaway findings.

Leak history retains only current `(pid, start_ticks)` identities. A finding
requires at least five samples spanning at least 30 minutes, mostly rising
footprint, slope of at least 1 MiB/min, no excessive drops, and no recent
plateau. Empty history does not trigger a short live fallback. Runaway compares
CPU, package-idle wakeups, and interrupt wakeups independently with each
normalized process name's contextual baseline when mature, otherwise with
explicit static thresholds. Static thresholds remain an anti-poisoning
backstop even after history matures, so a process cannot normalize a
persistently pegged CPU or wakeup storm. Current and historical stethoscope
sampler identities are excluded from process baselines and findings; dedicated
system sampler metrics still monitor tool overhead. The two wakeup counters are
never summed for an alarm.

Triage additionally classifies kernel memory pressure, battery service
condition, and SMART warnings. Unknown pressure is an info finding and partial,
not healthy. No battery and no physical drives are supported states.
`smartctl` absence is partial but optional; actual live/replay/probe failures
set `error` and exit `4`.
The SMART point structure distinguishes a working `diskutil` probe
(`diskutil_available`), whether it enumerated any hardware
(`physical_drives_present`), and optional `smartctl` availability. A failed
`diskutil` probe uses `physical_drives_present: null`; an empty successful
enumeration uses `false`. Triage reuses the memory and battery structures read
while producing its raw interval, then probes SMART once; those point
observations are not added to `baseline-raw/1`.

History is consumed through an incremental JSONL scan. Corruption preserves up
to 1024 `replay_errors` and exact `replay_error_count`/
`replay_errors_omitted`, marks the document partial, and exits `4` even if
findings exist. Source partial reasons propagate, but `not_root` or another
visibility limitation alone does not make the command fail.
If history cannot be opened, `history.available` is false and `history.error`
explains why; static runaway and independent point findings are still returned,
while the command exits `4`. `trend_invalid_count` reports ignored
out-of-order timestamps. `sampler_baseline_resets` reports conservative
normalized-name resets made when replay discovers that an earlier contributor
was a recorder process.

Exit is `1` only for a critical finding, `0` for clean/info/warn, `2` for
invalid invocation (including invalid `--since`), and `4` for live collection,
replay, store, or required probe failure.

## `checkup`

`checkup` is a one-shot full-body examination. It accepts exactly `--json`,
`--interval N`, `--limit N`, `--since WHEN`, and `--store DIR`, with the same
defaults, parsing, 60-second interval maximum, and 256-row maximum as `triage`.
It rejects `--once`, `--duration`, all positionals, and unsupported flags.

Checkup invokes canonical structured `triage` exactly once and does not
reclassify findings or parse rendered text. Its `overall`, `findings`, `notes`,
`partial`, `partial_reasons`, `history`, and `error` are the triage values,
including finding order, history provenance, corruption diagnostics, and
source-partial reasons.

```json
{
  "schema": "stethoscope/1",
  "scope": "checkup",
  "command": "checkup",
  "partial": true,
  "partial_reasons": ["not_root", "smartctl_unavailable"],
  "overall": "ok",
  "findings": [],
  "notes": [],
  "history": {},
  "sample": {
    "recorded_at": 1783769400.0,
    "interval_s": 1.01,
    "privilege": "user",
    "power_state": "battery",
    "process_count": 1
  },
  "vitals": {
    "cpu": {
      "state": "partial",
      "available": true,
      "partial": true,
      "rates": {
        "cpu_pct": {"value": 18.0, "unit": "percent_of_one_core"},
        "pkg_idle_wakeups_per_s": {"value": 3.0, "unit": "per_second"},
        "interrupt_wakeups_per_s": {"value": 9.0, "unit": "per_second"}
      },
      "top_consumers": [
        {
          "pid": 500,
          "start_ticks": 900,
          "name": "worker",
          "cpu_pct": 18.0,
          "user_pct": 15.0,
          "system_pct": 3.0,
          "pkg_idle_wakeups_per_s": 3.0,
          "interrupt_wakeups_per_s": 9.0
        }
      ]
    },
    "disk": {
      "state": "partial",
      "available": true,
      "partial": true,
      "rates": {
        "read_bytes_per_s": {"value": 1024.0, "unit": "bytes_per_second"},
        "write_bytes_per_s": {"value": 2048.0, "unit": "bytes_per_second"}
      },
      "top_consumers": [
        {
          "pid": 500,
          "start_ticks": 900,
          "name": "worker",
          "diskio_bytes_read_per_s": 1024.0,
          "diskio_bytes_written_per_s": 2048.0
        }
      ]
    },
    "memory": {
      "state": "partial",
      "available": true,
      "partial": true,
      "pressure": "normal",
      "total_bytes": 17179869184,
      "used_bytes": 8589934592,
      "free_bytes": 4294967296,
      "wired_bytes": 2147483648,
      "compressed_bytes": 1073741824,
      "errors": [],
      "top_consumers": [
        {
          "pid": 500,
          "start_ticks": 900,
          "name": "worker",
          "footprint_bytes": 536870912,
          "resident_size_bytes": 603979776
        }
      ]
    },
    "battery": {
      "state": "partial",
      "available": true,
      "partial": true,
      "present": true,
      "condition": "Normal",
      "charge_pct": 81.0,
      "health_pct": 92.0,
      "cycle_count": 120,
      "state_detail": "discharging",
      "external_connected": false,
      "battery_flow_watts": -7.2,
      "probe_error": null,
      "pmset_error": null,
      "rates": {
        "energy_rate_watts": {"value": 2.1, "unit": "watts"},
        "energy_score_per_s": {
          "value": null,
          "unit": "unitless_per_second"
        }
      },
      "top_consumers": [
        {
          "pid": 500,
          "start_ticks": 900,
          "name": "worker",
          "cpu_pct": 18.0,
          "energy_rate_watts": 2.1,
          "energy_score_per_s": null
        }
      ]
    },
    "smart": {
      "state": "partial",
      "available": true,
      "partial": true,
      "diskutil_available": true,
      "physical_drives_present": true,
      "smartctl_available": false,
      "drives": [
        {
          "device": "disk0",
          "smart_status": "verified",
          "smartctl_available": false,
          "smartctl_detail": "smartctl not found",
          "diskutil_detail": null,
          "warnings": []
        }
      ]
    }
  },
  "error": null
}
```

Every vital always has `state`, `available`, and `partial`. `state` is
`available`, `partial`, `unavailable`, or `absent`. `absent` is used only for a
successfully observed lack of battery hardware or physical drives; it is not a
healthy verdict. Failed or indeterminate probes are `unavailable`, and nullable
measurements render as unknown in human output. CPU, disk, battery rates, and
all process top-consumer arrays come from the same raw current interval.
Memory and battery health plus SMART structures come from triage points.
Top-consumer arrays exclude the exact sampler PID/start identity, are
deterministically ranked by the corresponding activity, and are capped by
`--limit`. Live vital state is derived from current probe and visibility state;
historical source-partial reasons remain at the top level but do not downgrade
an otherwise complete current vital.

When no valid live sample exists, `sample` is `null`, CPU/disk are unavailable,
and all five vital objects retain their stable fields. Usage JSON has the same
shape with `partial_reasons: ["usage_error"]`. Human output sanitizes all
external strings. JSON is one strict `allow_nan=false` document.

Exit is `1` only for a critical canonical finding, `2` for invalid invocation,
`4` for required live probe, replay, store, or runtime failure, and `0`
otherwise.

## Recording corpus: `baseline-raw/1`

`stethoscope record` appends strict JSON objects (not `stethoscope/1`
envelopes) to local-date `YYYY-MM-DD.jsonl` files. One complete object occupies
one line. Files are append-only and retained for 30 days by default.
Descriptor traversal rejects user-controlled symlinks. On macOS, immutable
root-owned aliases directly below `/` (notably `/tmp` and `/var`) are resolved
before the no-follow traversal so absolute `--store` paths produced by
`mktemp` remain usable.

```json
{
  "schema": "baseline-raw/1",
  "recorded_at": 1783765800.0,
  "interval_s": 60.02,
  "requested_interval_s": 60.0,
  "context": {
    "root": false,
    "privilege": "user",
    "power_state": "battery",
    "local_hour": 15,
    "timezone": "IST+05:30",
    "sampler": {
      "pid": 123,
      "start_ticks": 456,
      "name": "python3",
      "normalized_name": "python3"
    },
    "coverage": {
      "new_processes_zero_based": 1,
      "unmatched_current_processes": 0,
      "missing_endpoint_processes": 2
    }
  },
  "metrics": [
    {"scope": "cpu", "metric": "cpu_pct", "value": 42.0,
     "unit": "percent_of_one_core"},
    {"scope": "sampler", "metric": "footprint_bytes", "value": 9535488,
     "unit": "bytes"}
  ],
  "processes": [
    {
      "pid": 500, "start_ticks": 900, "name": "worker",
      "normalized_name": "worker",
      "cpu_pct": 12.0, "user_pct": 10.0, "system_pct": 2.0,
      "pkg_idle_wakeups_per_s": 1.0,
      "interrupt_wakeups_per_s": 20.0,
      "diskio_bytes_read_per_s": 0.0,
      "diskio_bytes_written_per_s": 4096.0,
      "energy_rate_watts": null, "energy_score_per_s": 0.4,
      "footprint_bytes": 104857600, "resident_size_bytes": 110100480
    }
  ],
  "partial": true,
  "partial_reasons": ["not_root", "process_endpoint_gaps"]
}
```

Stable system metrics cover CPU, both wakeup counters, disk read/write,
memory used/free/wired/compressed, battery charge/health/flow, real energy
where available, unitless energy score, and sampler CPU/footprint/resident.
Nullable values remain present. Processes are the PID/start-identity union of
the top-N CPU, wakeup, disk, energy, and footprint rows plus the sampler.
Activity floors keep rate-based groups meaningful; the sampler is excluded
before each top-N limit and then added explicitly.
`context.power_state` is normalized to `ac`, `battery`, or `unknown`.
Rates, counters, percentages, and byte gauges must be nonnegative; battery
flow is the signed exception (negative while discharging).
Processes proven by their start tick to have begun within an interval are
zero-based so their work is retained. Endpoint misses are counted in
`context.coverage`; either an older current-only process or a process missing
from the second endpoint adds `process_endpoint_gaps` rather than silently
claiming complete attribution. Snapshot timing uses equivalent scan midpoints.

## `record`

`record --json` emits one `stethoscope/1` `record/sample` document per
successfully appended interval, derived from the same raw sample. Flags:
`--json`, `--once`, `--duration N`, `--interval N`, `--limit N`,
`--store DIR`, and `--retention-days N`. Defaults are 60 seconds, 20 rows per
top-N set, and 30 days. `--once` always completes exactly one requested
interval.
`--limit` is capped at 256, `--interval` at one day, explicit `--duration` at
365 days, and retention at 3650 days; out-of-range values are usage errors.

Record documents always include `stored`, `recorded_at`, `interval_s`,
`requested_interval_s`, `context`, `metrics`, `processes`, and `error`.
`stored` remains true when an append succeeded but a later retention or output
step failed. A daily file with an incomplete final line blocks further appends
to that file and returns exit 4 rather than concatenating two JSON objects.

## `history [scope]` and `history baseline [scope]`

History flags are `--since WHEN`, `--limit N`, `--store DIR`, and `--json`;
`WHEN` accepts relative durations (`3h`), ISO-8601 timestamps, and local clock
times (`3am`). The optional scope is actually applied and unknown/extra
positionals are rejected.

Summary documents use command `summary` and contain `record_count`, `cold`,
`summaries`, and `top_consumers`. Baseline documents use command `baseline`
and contain contextual `buckets`. Percentile rows expose exact `count`,
bounded `sample_count`, `p50`, `p90`, and `p99`. Baseline buckets additionally
carry local hour, timezone, privilege, power state, scope, metric, normalized
process name (or `null` for system metrics), and `cold`.
History scans files incrementally; only bounded per-bucket reservoirs and
result metadata are retained in memory. `--limit` is applied independently to
each process metric (and, for contextual baselines, each context/metric), so
byte-valued metrics never displace CPU, wakeup, or energy rows.
Candidate cardinality is bounded as well. If churn exceeds those bounds,
`dropped_values` is nonzero, `history_bucket_limit` appears in
`partial_reasons`, and the command still exits 0 because the retained summary
is usable.

Replay counts every bad line diagnostic in `replay_error_count` and retains up
to 1024 details in `replay_errors`, each with `file`, `line`, and `reason`;
`replay_errors_omitted` gives the exact remainder. Malformed, invalid,
non-finite, overlong, deeply nested, or partial-final-line input sets
`partial: true`, includes `corrupt_store`, and exits 4. Source samples' own
partial reasons are also retained, but visibility limitations alone do not
turn a usable history summary into exit 4. Missing/empty stores are clean,
cold, and exit 0.

## Changelog

- `stethoscope/1`: stable common envelope and
  disk/CPU/memory/battery/SMART/record/history/triage/anomaly/checkup contracts.
- `baseline-raw/1`: append-only daily recording corpus.
