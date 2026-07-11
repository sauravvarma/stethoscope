# Agent walkthrough

This walkthrough uses the shipped CLI and read-only MCP server. All JSON blocks
are illustrative outputs with real `stethoscope/1` field names and units; values
will differ by machine. They are strict JSON: unavailable values are `null`, not
`NaN` or infinity. [SCHEMA.md](../SCHEMA.md) is authoritative.

Run non-root first. Elevate only a focused probe when missing visibility matters.
Process names, PIDs, users, open paths, and local history can disclose private
activity.

## 1. Start broad: checkup

```sh
./stethoscope checkup --interval 1 --limit 10 --json
```

Illustrative response:

```json
{
  "schema": "stethoscope/1",
  "scope": "checkup",
  "command": "checkup",
  "partial": true,
  "partial_reasons": ["not_root"],
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
  "sample": {
    "recorded_at": 1783769400.0,
    "interval_s": 1.01,
    "privilege": "user",
    "power_state": "battery",
    "process_count": 0
  },
  "vitals": {
    "cpu": {
      "state": "partial",
      "available": true,
      "partial": true,
      "rates": {
        "cpu_pct": {"value": 0.0, "unit": "percent_of_one_core"},
        "pkg_idle_wakeups_per_s": {"value": 0.0, "unit": "per_second"},
        "interrupt_wakeups_per_s": {"value": 0.0, "unit": "per_second"}
      },
      "top_consumers": []
    },
    "disk": {
      "state": "partial",
      "available": true,
      "partial": true,
      "rates": {
        "read_bytes_per_s": {"value": 0.0, "unit": "bytes_per_second"},
        "write_bytes_per_s": {"value": 0.0, "unit": "bytes_per_second"}
      },
      "top_consumers": []
    },
    "memory": {
      "state": "partial",
      "available": true,
      "partial": true,
      "pressure": "warn",
      "total_bytes": 17179869184,
      "used_bytes": 8589934592,
      "free_bytes": 4294967296,
      "wired_bytes": 2147483648,
      "compressed_bytes": 1073741824,
      "errors": [],
      "top_consumers": []
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
        "energy_rate_watts": {"value": 0.0, "unit": "watts"},
        "energy_score_per_s": {"value": null, "unit": "unitless_per_second"}
      },
      "top_consumers": []
    },
    "smart": {"state": "absent", "available": true, "partial": false, "diskutil_available": true, "physical_drives_present": false, "smartctl_available": false, "drives": []}
  },
  "error": null
}
```

Interpret `overall` and exit status together. `checkup` exits 1 only for a
critical canonical finding, 0 for clean/info/warn, 2 for usage, and 4 for a
required probe/replay/store/runtime failure. `partial` is not itself failure:
read `partial_reasons` and each vital's `state`, `available`, and `partial`.
Hardware absence is `absent`; failed or indeterminate probes are `unavailable`.

## 2. Ask for ranked diagnosis: triage

```sh
./stethoscope triage --since 24h --interval 1 --limit 20 --json
```

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
    "partial": true,
    "partial_reasons": ["not_root"],
    "context": {"privilege": "user", "power_state": "battery"},
    "metrics": [],
    "processes": [],
    "points": {
      "memory": {"pressure": "warn"},
      "battery": {"present": true, "condition": "Normal"},
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

Scores are ordinal 0-100 and confidence is `low`, `moderate`, or `high`; neither
is a probability. Findings are deterministically ordered and carry `drill_down`
command templates. The schema names the launcher `stethoscope`; in a source
checkout, replace that leading token with `./stethoscope`. A process identity is
`(pid, start_ticks)`: never join current data to historical data by PID alone
because macOS reuses PIDs.

Use `anomaly deviation`, `anomaly leaks`, or `anomaly runaway` when only one
detector is relevant. These commands are one-shot; they reject `--once` and
`--duration`.

## 3. Focus the evidence

### CPU and wakeups

```sh
./stethoscope cpu wakeups --once --interval 1 --limit 10 --json
```

The command remains `wakeups`, not `top`:

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

`total_wakeups_per_s` is a ranking convenience. Diagnosis compares package-idle
and interrupt rates independently against their own baselines. `watts` is a real
rusage flavor-6 energy delta and may be `null`; absence is not zero.

### Battery

```sh
./stethoscope battery health --json
./stethoscope battery top --once --interval 1 --limit 10 --json
./stethoscope battery drainers --limit 10 --json
```

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
      "pid": 29641,
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

Do not compare unlike units. `energy_rate_watts` is real watts where flavor 6
exists. `energy_score_per_s` and `energy_share_pct` come from Apple's unitless
pmenergy-weighted ranking. Battery flow from `health` is signed battery-terminal
power, not system draw while charging.

`drainers` is stateful but read-only with respect to the machine: it maintains
`~/.stethoscope/battery_baseline.json`. First run, AC power, reboot, a changed
unplug session, visibility changes, or corrupt state can reset or partially
invalidate attribution. Inspect `baseline_reset`, `reset_reason`, `on_ac`, and
`error` before interpreting the ranking.

### History and cold/corrupt stores

Use a fresh store to observe the cold-state contract before recording:

```sh
STORE="$(mktemp -d /tmp/stethoscope-agent.XXXXXX)"
./stethoscope history cpu --since 24h --limit 10 --store "$STORE" --json
```

```json
{
  "schema": "stethoscope/1",
  "scope": "history",
  "command": "summary",
  "partial": false,
  "partial_reasons": [],
  "raw_schema": "baseline-raw/1",
  "store": "/tmp/stethoscope-agent.ABC123",
  "since": 1783683000.0,
  "requested_scope": "cpu",
  "record_count": 0,
  "cold": true,
  "summaries": [],
  "top_consumers": [],
  "replay_errors": [],
  "replay_error_count": 0,
  "replay_errors_omitted": 0,
  "dropped_values": 0,
  "error": null
}
```

Missing/empty history is a valid cold result and exits 0. Corrupt, non-finite,
overlong, deeply nested, or partial-final-line JSONL preserves usable records,
sets `partial` with `corrupt_store`, reports bounded line diagnostics, and exits
4. Source `not_root` reasons propagate without making a usable summary fail.

Populate and query the same store afterward:

```sh
./stethoscope record --once --store "$STORE" --json
./stethoscope history cpu --since 24h --limit 10 --store "$STORE" --json
./stethoscope history baseline cpu --since 7d --limit 10 --store "$STORE" --json
```

### Disk drill-down

```sh
./stethoscope disk top --once --interval 1 --limit 10 --json
./stethoscope disk holds 29641 --json
sudo ./stethoscope disk busy "/Volumes/X9 Pro" --json
sudo ./stethoscope disk inspect 29641
```

```json
{
  "schema": "stethoscope/1",
  "scope": "disk",
  "command": "busy",
  "partial": false,
  "partial_reasons": [],
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

`disk busy` exits 1 when it finds holders. `disk inspect` is deliberately
human-only, accepts no flags, and requires root. Do not attempt it through MCP.

## 4. Exit semantics

Always retain both the document and process status:

| Exit | Meaning |
|---:|---|
| 0 | completed; no command-defined reportable condition (triage/checkup may contain advisory findings) |
| 1 | completed with a reportable finding; critical-only for triage/checkup |
| 2 | invalid invocation |
| 3 | required permission unavailable |
| 4 | probe, store, replay, transport, or runtime failure |

Hardware absence and optional-tool absence are structured states, not automatic
failures. A partial result may be useful; an exit-4 result may also retain useful
findings or history alongside its `error`.

## 5. MCP handshake and tool use

Launch `./stethoscope mcp` as a child process and exchange exactly one UTF-8 JSON
object per line. Standard output is protocol-only; diagnostics use stderr. The
required lifecycle is `initialize`, silent `notifications/initialized`, then
`tools/list` or `tools/call`.

Client lines:

```jsonl
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"example-agent","version":"1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"cpu_wakeups","arguments":{"interval":1,"limit":10}}}
```

The initialize response advertises protocol `2025-11-25`, server version
`0.2.0`, and `tools.listChanged: false`. A successful call returns one compact
JSON text item plus identical `structuredContent`. Scope exits 0 and 1 have
`isError: false`; exits 2, 3, and 4 have `isError: true`.

The ten tools are `disk_top`, `disk_holds`, `disk_busy`, `cpu_top`,
`cpu_wakeups`, `memory_top`, `battery_health`, `battery_top`, `smart_status`,
and `checkup`. The server deliberately does **not** expose record/history stores,
battery drainers, kill, eject, or root-heavy inspect. It does not bypass macOS
permissions, so non-root partial visibility remains the safe default.

See [README.md](../README.md), the [man-page source](../man/stethoscope.1),
[ARCHITECTURE.md](../ARCHITECTURE.md), [DESIGN.md](../DESIGN.md), and the
[review ledger](review-disposition.md).
