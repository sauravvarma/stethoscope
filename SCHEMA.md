# stethoscope output schema

The agent-facing contract for `--json`. Every scope command that supports
`--json` emits one JSON **document** per sample to stdout (newline-delimited, so
live/`--duration` streams are valid NDJSON). This file is the stability
reference: fields listed here are what an agent may rely on.

## Versioning

Every document carries a top-level integer `schema`. This document describes
**`schema: 1`**.

- **Additive changes** (new fields) do **not** bump the version — consumers must
  ignore unknown fields.
- **Breaking changes** (removing/renaming a field, changing a type or unit) bump
  `schema` and are recorded in the changelog at the bottom.

## Envelope

Every document shares:

| field | type | meaning |
|---|---|---|
| `schema` | int | schema version (currently `1`) |
| `scope` | string | the scope, e.g. `"disk"` |
| `command` | string | the command, e.g. `"top"` |

Units: `*_per_s` are bytes/second (float), `read`/`write`/`*_total` are bytes
(int), times are seconds unless noted.

## Exit codes (all scopes)

Probes double as checks, so the exit code is part of the contract:

| code | name | meaning |
|---|---|---|
| `0` | ok | ran fine; nothing notable / clean |
| `1` | findings | ran fine and the probe found the thing (e.g. `busy`: holders exist) |
| `2` | usage | bad invocation |
| `3` | perm | needs root / permission denied |

## `disk top`

One document per sample. With `--once`, exactly one; with `--duration N`, a
stream for N seconds; otherwise an unbounded stream.

```json
{
  "schema": 1, "scope": "disk", "command": "top",
  "system": {"read_per_s": 158105.6, "write_per_s": 134348.8},
  "processes": [
    {"pid": 1263, "name": "corespotlightd",
     "read_per_s": 16384.0, "write_per_s": 0.0,
     "read_total": 20718518272, "write_total": 1717986918}
  ]
}
```

`processes` is ranked by total throughput, truncated to `--limit`. Exit: `0`.

## `disk holds <pid>`

```json
{
  "schema": 1, "scope": "disk", "command": "holds",
  "pid": 1000, "name": "bash",
  "cumulative": {"read": 16384, "write": 0},
  "holds": [
    {"reason": "working dir (cwd)", "type": "DIR", "path": "/Users/kris"},
    {"reason": "open (read)", "type": "REG", "path": "/tmp/data.txt"}
  ]
}
```

`cumulative` is `null` when the process's I/O is inaccessible. On lsof failure
an `error` string is present and `holds` is empty. Exit: `0`.

## `disk busy <volume|device>`

```json
{
  "schema": 1, "scope": "disk", "command": "busy",
  "target": "/Volumes/X9 Pro",
  "targets": [{"device": "/dev/disk6s2", "mount": "/Volumes/X9 Pro"}],
  "holders": [
    {"pid": 1263, "name": "mds", "user": "root",
     "reasons": {"open (read)": 3, "working dir (cwd)": 1},
     "paths": ["/Volumes/X9 Pro", "/Volumes/X9 Pro/a"],
     "io": {"read": 123, "write": 0}}
  ]
}
```

`holders` is ranked by hold count. `io` is `null` when inaccessible. When no
mounted volume matches, `targets`/`holders` are empty and `error` is set (exit
`2`). Otherwise exit is `1` if any holder exists, else `0`.

## `disk inspect <pid>`

`inspect` streams a live `fs_usage` trace and is **human-only** (no `--json`);
it requires root (exit `3` without it).

## `cpu top` / `cpu wakeups`

One document per sample (same `--once` / `--duration` behavior as `disk top`).
`top` ranks `processes` by `cpu_pct`, `wakeups` ranks by `wakeups_per_s`.

```json
{
  "schema": 1, "scope": "cpu", "command": "top",
  "system": {"cpu_pct": 143.9, "ncpu": 8},
  "processes": [
    {"pid": 29641, "name": "copilot", "cpu_pct": 93.9,
     "wakeups_per_s": 412.0, "idle_wakeups_per_s": 400.0,
     "interrupt_wakeups_per_s": 12.0}
  ]
}
```

`cpu_pct` is machine-relative, not per-core: a process saturating two cores
reads ~`200.0`, and `system.cpu_pct` sums all processes (approaches
`ncpu × 100`). Exit: `0`.

## `memory top`

Ranks `processes` by `footprint` (bytes). One document per sample. `system`
summarises memory in bytes plus a `pressure` string (`normal`/`warn`/`critical`).

```json
{
  "schema": 1, "scope": "memory", "command": "top",
  "system": {"total": 8589934592, "used": 6039797760, "free": 121667584,
             "active": 1395916800, "inactive": 1289748480,
             "wired": 2299002880, "compressed": 2744877056, "pressure": "warn"},
  "processes": [
    {"pid": 1234, "name": "WindowServer", "footprint": 734003200, "resident": 812345344}
  ]
}
```

Exit: `0`.

## `memory watch <pid>`

One document per sample while watching a single pid's footprint trend.

```json
{
  "schema": 1, "scope": "memory", "command": "watch",
  "pid": 1234, "name": "leaky", "footprint": 524288000, "resident": 560000000,
  "slope_mb_per_min": 12.4, "samples": 30, "leak_candidate": true
}
```

`slope_mb_per_min` is a least-squares fit over the samples so far;
`leak_candidate` is true once the slope is sustained (> 1 MB/min, ≥ 5 samples).
Exit: `1` if a leak candidate was seen, else `0`.

## `battery health`

No sampling — one document. `present` is `false` on a desktop with no battery.

```json
{
  "schema": 1, "scope": "battery", "command": "health", "present": true,
  "charge_pct": 62, "state": "discharging", "time_remaining": "2:31",
  "cycle_count": 371, "health_pct": 81.1, "condition": "Normal",
  "design_capacity_mah": 4382, "max_capacity_mah": 3555,
  "temperature_c": 30.9, "charging": false, "external_connected": false,
  "fully_charged": false, "serial": "…"
}
```

`health_pct` is max capacity ÷ design; `condition` is `Service Recommended`
when the gauge reports a permanent failure or health < 80%. Exit: `0`.

## `battery top`

Live per-process **energy-impact score** (a transparent proxy: `cpu_pct` plus
weighted idle/interrupt wakeups — not macOS's private Energy Impact). One
document per sample.

```json
{
  "schema": 1, "scope": "battery", "command": "top",
  "processes": [
    {"pid": 1653, "name": "avconferenced", "energy_score": 24.4,
     "cpu_pct": 4.1, "idle_wakeups_per_s": 0.0, "interrupt_wakeups_per_s": 288.0}
  ]
}
```

## `battery drainers`

Cumulative energy impact since the last unplug, using a baseline file. When on
AC or with no baseline yet, it (re)sets the baseline and returns
`baseline_reset: true`.

```json
{
  "schema": 1, "scope": "battery", "command": "drainers",
  "baseline_reset": false, "charge_pct": 55, "charge_drop": 12, "elapsed_s": 3600.0,
  "drainers": [
    {"pid": 1653, "name": "avconferenced", "energy_score": 84.2,
     "cpu_seconds": 42.1, "idle_wakeups": 1200, "interrupt_wakeups": 90000}
  ]
}
```

Exit: `0`.

## `smart [disk]`

SMART health for each physical drive (or one named `disk`). No sampling — one
document. Fields beyond `smart_status` require `smartctl`; without it only the
verdict is present.

```json
{
  "schema": 1, "scope": "smart", "command": "status",
  "drives": [
    {"device": "disk0", "internal": true, "source": "smartctl",
     "name": "APPLE SSD AP0256Q", "size_bytes": 251000193024, "solid_state": true,
     "smart_status": "verified", "passed": true, "percentage_used": 3,
     "power_on_hours": 1393, "tbw_tb": 30.18, "available_spare": 100,
     "available_spare_threshold": 99, "media_errors": 0, "temperature_c": 45,
     "life": {"remaining_life_pct": 97, "remaining_hours": 45040,
              "remaining_years": 5.1, "confidence": "moderate"},
     "warnings": [{"severity": "warn", "message": "…"}],
     "worst_severity": "ok"}
  ]
}
```

`warnings[].severity` is `critical` or `warn`; `worst_severity` is `ok`/`warn`/
`critical`. `life` is `null` when wear is too low to extrapolate. Exit: `1` if
any drive is `critical` (so `smart` doubles as a health check), else `0`.

## `checkup`

A one-shot cross-scope exam. `vitals` holds a per-scope summary; `findings` is
sorted worst-first with a drill-down command each; `overall` is the worst
severity (info-level notes stay `ok`).

```json
{
  "schema": 1, "scope": "checkup", "command": "checkup", "overall": "warn",
  "findings": [
    {"severity": "warn", "area": "battery",
     "message": "battery condition: Service Recommended (health 78%, 900 cycles)",
     "drill": "stethoscope battery health"}
  ],
  "vitals": {
    "cpu": {"system_cpu_pct": 42.1, "ncpu": 8, "top": {"pid": 1, "name": "x", "cpu_pct": 30.0}},
    "memory": {"pressure": "warn", "used": 6039797760, "total": 8589934592, "used_pct": 70.3},
    "battery": {"present": true, "charge_pct": 61, "health_pct": 81.1, "cycle_count": 371, "condition": "Normal"},
    "smart": {"drives": [{"device": "disk0", "name": "…", "smart_status": "verified", "percentage_used": 3, "worst_severity": "ok"}]}
  }
}
```

Exit: `1` when `overall` is `critical`, else `0`.

## `record`

Foreground sampler that appends one SQLite history row per metric. With
`--once`, exactly one sample is written; otherwise it samples every
`--interval N` seconds and prunes rows older than `--max-age-days N`.

```json
{
  "schema": 1, "scope": "record", "command": "record",
  "db": "/Users/me/Library/Application Support/stethoscope/history.db",
  "cutoff": 1783450000,
  "rows_written": 18,
  "sample": {
    "ts": 1784660000, "interval": 60.0,
    "cpu": {"system_cpu_pct": 42.1},
    "memory": {"used": 6039797760, "total": 8589934592, "pressure": "normal"},
    "battery": {"present": true, "charge_pct": 61, "health_pct": 81.1},
    "disk": {"read_per_s": 158105.6, "write_per_s": 134348.8},
    "record": {"rows": 18}
  }
}
```

The SQLite schema is additive and queryable:
`samples(ts INTEGER, scope TEXT, metric TEXT, pid INTEGER, name TEXT, value REAL)`.
System metrics use `pid: null`; optional top-process metrics carry `pid`/`name`.
The sampler also records its own footprint as `record.self_*`. Exit: `0`.

## `history`

Queries recorded samples since a relative (`1h`, `30m`, `2d`, `3am`) or ISO
timestamp. `--scope` filters summaries to one scope.

```json
{
  "schema": 1, "scope": "history", "command": "query",
  "since": 1784656400, "until": 1784660000, "scope_filter": null,
  "metrics": [
    {"scope": "cpu", "metric": "system_cpu_pct", "count": 60,
     "min": 8.0, "max": 143.9, "peak": 143.9, "mean": 31.2,
     "latest": {"value": 42.1, "ts": 1784660000}}
  ],
  "top_consumers": [
    {"scope": "cpu", "metric": "process_cpu_pct", "pid": 29641,
     "name": "copilot", "count": 4, "peak": 93.9, "mean": 51.2}
  ]
}
```

Exit: `0` on a valid query, even when no rows match.

## `history baseline`

Computes this machine's normal per-hour-of-day percentiles from recorded system
metrics (`pid: null` rows only). `--since` and `--scope` are optional filters.

```json
{
  "schema": 1, "scope": "history", "command": "baseline",
  "since": null, "scope_filter": null,
  "baselines": [
    {"hour": 9, "scope": "cpu", "metric": "system_cpu_pct",
     "count": 30, "p50": 18.4, "p90": 62.1, "p99": 91.0}
  ]
}
```

Exit: `0`.

## Changelog

- **schema 1** — initial contract: `disk` `top`/`holds`/`busy`, `cpu`
  `top`/`wakeups`, `memory` `top`/`watch`, `battery` `health`/`top`/`drainers`,
  `smart` `status`, `checkup`, `record`, `history`/`history baseline`, exit
  codes.
