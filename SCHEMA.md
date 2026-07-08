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

## Changelog

- **schema 1** — initial contract: `disk` `top`/`holds`/`busy`, `cpu`
  `top`/`wakeups`, exit codes.
