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

## Changelog

- `stethoscope/1`: stable common envelope and disk/CPU contracts.
