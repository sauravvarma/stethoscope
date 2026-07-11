# stethoscope 0.2 architecture and future extensions

This document describes the shipped implementation first. It preserves the
probe, vital, attribution, statistics, and diagnosis rationale behind the code,
then labels ideas that are not commands today. The executable contract is
[SCHEMA.md](SCHEMA.md); user surfaces are indexed from [README.md](README.md).

## 1. The diagnostic problem

"My battery is draining" is a system symptom. A useful answer needs
process-level attribution plus classification: which process is active, how it
differs from this machine's history, whether the evidence is CPU, wakeups, disk,
memory, battery condition, or drive health, and which focused probe verifies it.

The shipped pipeline separates five concerns:

1. **Probe:** read a macOS introspection surface.
2. **Derive:** turn cumulative snapshots into rates and typed states.
3. **Record:** append validated raw intervals and replay bounded history.
4. **Diagnose:** compare current values with robust contextual history/static
   backstops and emit evidence-bearing findings.
5. **Present:** render the same structures as CLI, JSON, TUI, or MCP.

All diagnosis is deterministic and on-device. An AI agent is an optional
consumer of vitals and findings, not a dependency.

```text
native probes -> scope data -> baseline/stats -> diagnosis
                    |              |              |
                    +-------- CLI / JSON / TUI / MCP
```

The load-bearing rule is: reusable layers return structures, not rendered text.
Surfaces do not parse another surface's output. `checkup` composes structured
triage data; MCP invokes the same result helpers used by CLI commands.

## 2. Shipped module layout

```text
stethoscope
  dispatcher and exact scope registry

core/
  cli.py       option validation, command-specific enforcement, strict JSON,
               exit codes and text sanitization
  rusage.py    complete ctypes rusage layouts, timebase conversion,
               process snapshots and (pid, start_ticks) identity
  power.py     power-state and pmenergy coefficient probes
  vmstat.py    vm_stat/sysctl memory probes
  smart.py     diskutil/smartctl probing and parsing
  baseline.py  secure baseline-raw/1 JSONL append/replay, time parsing,
               bounded reservoirs and contextual percentiles
  stats.py     finite-safe percentiles, robust bands, online trend,
               leak and runaway evidence
  schema.py    stethoscope/1 document envelope
  tui.py       semantic curses palette, safe drawing, ring histories, popups
  validate.py  native probe validation

scopes/
  disk.py      disk rates, held files, volume holders, inspect
  cpu.py       CPU, energy and wakeup rates
  memory.py    footprints, system pressure and process watch
  battery.py   health, attribution, unplug baseline and powermetrics inspect
  smart.py     drive-health command
  record.py    interval collection, corpus writer and history views
  anomaly.py   replay, detector orchestration and triage
  checkup.py   five-vital examination composed from triage
  tui.py       one centralized five-tab shell
  disk_tui.py  compatibility wrapper into the centralized shell
  mcp_server.py
               bounded read-only MCP 2025-11-25 stdio server

diagnosis/
  rules.py     pure deviation, leak, runaway and point-health classifiers
  taxonomy.py  stable finding construction, sorting and overall severity
```

There is no `core/sampling.py` and no per-scope CPU/memory/battery TUI. Snapshot
and interval ownership remains in each scope/recording layer; visual primitives
are shared through `core/tui.py`.

## 3. State ownership

State is explicit and narrow:

- A live CLI interval owns two snapshots and elapsed monotonic time.
- `memory watch` owns its bounded in-process trend and latched leak flag.
- `record` exclusively locks and appends daily
  `~/.stethoscope/baseline-raw/YYYY-MM-DD.jsonl` files. `core/baseline.py`
  validates records, performs bounded incremental replay, and computes
  reservoirs/percentiles. It resolves sudo state to the invoking user's home.
- `battery drainers` owns one schema-validated, boot/session/privilege-aware
  `~/.stethoscope/battery_baseline.json`, replaced atomically.
- The unified TUI owns only presentation state: active tab, selections, previous
  snapshots, 60-point sparklines, explicit diagnosis result, and probe errors.
- An MCP process owns lifecycle readiness, request IDs, and resource counters;
  it does not own or expose recording/drainer state.

Triage streams the JSONL corpus once. It retains reservoirs only for current
context/normalized names and online trends only for current `(pid,start_ticks)`
targets. Corrupt rows are bounded diagnostics, not unbounded in-memory replay.

## 4. Native probes

Probe selection favors SIP-safe, OS-native surfaces cheap enough to poll.
Heavier tools stay in focused inspect paths.

| Probe | Data | Cost / privilege |
|---|---|---|
| `proc_listpids` + `proc_pid_rusage` | CPU times, separate package-idle/interrupt wakeups, disk bytes, footprint, start ticks, energy ledgers | cheap; other users may require root |
| `mach_timebase_info` | conversion of rusage mach-abstime fields | cached |
| `ioreg -rn AppleSmartBattery` | capacity, cycles, condition, voltage/current, battery presence | no root; absent on desktops |
| `pmset` | supplemental power state/session data | no root; structured partial when unavailable |
| `/usr/share/pmenergy/*.plist` | board-specific, unitless Energy Impact weights | optional OS data |
| `powermetrics --samplers tasks` | richer task Energy Impact | heavy, root-only inspect |
| `vm_stat` and `sysctl` | system memory gauges and pressure | no root |
| `diskutil` | physical drive inventory and base SMART verdict | OS-native |
| `smartctl` | wear, endurance, attributes, temperature | optional executable |
| `lsof` | held files and volume blockers | visibility improves with root |
| `fs_usage` | live per-PID filesystem syscalls and paths | root-only inspect |

### Rusage contract

The ctypes declaration is a complete structure for the requested flavor, never
a prefix buffer. macOS copies the full flavor size. CPU/start fields are
mach-abstime ticks and are converted through the platform timebase; treating
them as nanoseconds creates a large Apple Silicon error.

Snapshot identity is `(pid, start_ticks)`, never PID alone. A new or reused PID
is zero-baselined rather than diffed against a different process. Access gaps
remain coverage/partial metadata.

## 5. Vitals and units

A snapshot is cumulative probe state. A vital is a rate, gauge, or categorical
state derived from one or two snapshots. Units are part of the public contract.

### CPU

| Vital | Derivation / meaning |
|---|---|
| `cpu_pct` | delta user+system seconds / interval; may exceed 100 on multicore |
| `user_pct`, `system_pct` | separate interval CPU components |
| `total_cpu_seconds` | lifetime converted CPU time |
| `lifetime_duty_pct` | lifetime CPU / awake-age from start ticks |
| `pkg_idle_wakeups_per_s` | package-idle wakeup delta / interval |
| `interrupt_wakeups_per_s` | interrupt wakeup delta / interval |
| `total_wakeups_per_s` | display/ranking convenience only |
| `watts` | flavor-6 `ri_energy_nj` delta / interval, or `null` |

The two wakeup counters are never summed for detection. Their quiet ranges
differ by orders of magnitude; a timer storm can be almost entirely interrupt
wakeups. Diagnosis compares each counter to its own historical/static policy.

### Disk and memory

Disk read/write rates are deltas of charged kernel bytes. They inherit macOS
buffer-cache attribution: an exact syscall/path question belongs to `inspect`.

Memory's primary process gauge is `ri_phys_footprint`, with resident size kept
separately. `memory watch` computes an online least-squares footprint slope,
requires sustained mostly-rising history without a recent plateau, and latches
the candidate once reached. System pressure is `normal`, `warn`, `critical`, or
`unknown`; unknown never maps to healthy.

### Battery and process energy

Three quantities remain deliberately separate:

- `battery_flow_watts` is signed voltage times current at the battery terminals:
  negative while discharging, positive while charging. On AC it is not total
  system draw.
- `energy_rate_watts` is a real flavor-6 process energy delta. If the OS does not
  expose that ledger, the value is `null`, never zero.
- `energy_score_per_s` is Apple's pmenergy-weighted CPU/wakeup/disk ranking. Its
  coefficients are unitless, so the score is unitless and never reconciled as
  watts. `energy_share_pct` is a share of this modeled score.

The older billed-energy ledger updates too lazily for a one-second polling vital.
It is not substituted for flavor-6 live watts.

`battery drainers` accumulates unitless score and real joules, when available,
from a validated unplug baseline. AC, first run, reboot, changed session,
visibility change, charge increase, malformed state, and I/O failure remain
distinct reset/partial/error outcomes.

## 6. Recording and baselines

`record` writes one strict `baseline-raw/1` object per completed interval and per
line. Daily files are append-only, default retention is 30 days, and a writer
lock enforces single-writer ownership. The recorder stores:

- exact interval, requested interval, timezone/local hour, power and privilege
  context;
- stable system metrics with units;
- the union of bounded top CPU, wakeup, disk, energy and footprint identities;
- sampler identity/overhead and endpoint coverage;
- partial visibility reasons.

Process history is keyed by normalized name for cross-run baselines, while
within-run trends use `(pid,start_ticks)`. Context keys include local hour,
timezone, privilege and power state. Reservoirs are bounded at 512 values; group
and candidate counts are bounded as well.

Replay is strict and incremental. Missing/empty history is cold but valid.
Malformed, non-finite, overlong, nested, or truncated records preserve prior
usable rows, mark `corrupt_store`, and produce exit 4.

## 7. Shipped statistics and diagnosis

The implementation is intentionally smaller than the original research design:

1. Finite-safe p10/p50/p90/p99 and median absolute deviation form a robust band.
2. Metric-specific absolute and relative floors widen degenerate bands, so tiny
   noise does not alarm while a stable-zero baseline can still detect a spike.
3. System deviation uses only matching context and requires minimum history.
4. `OnlineTrend` keeps constant totals plus a bounded recent window. Leak
   evidence requires at least five samples over 30 minutes, mostly rising
   footprint, at least 1 MiB/min, and no recent plateau.
5. Runaway evidence compares CPU, package-idle wakeups, and interrupt wakeups
   independently. Static warning/critical thresholds remain anti-poisoning
   backstops even when a historical band is mature.
6. Point rules classify kernel memory pressure, battery service condition, and
   SMART warnings.

A finding has stable code, severity (`info`, `warn`, `critical`), area, detector,
message, ordinal score 0-100, ordinal confidence, drill-down commands, and
detector-specific evidence. Scores/confidence are not probabilities. Sorting is
deterministic. Triage exits 1 only when overall severity is critical.

The current taxonomy is evidence-oriented (`deviation`, `leak`, `runaway`,
`point`), not the broader speculative culprit taxonomy once proposed. Ideas such
as CUSUM onset dating, Mann-Kendall/Theil-Sen forecasting, Markov run-length
confidence, sleep-blocker classes, and cross-process sync-loop classification
remain future research until implemented and reflected in SCHEMA.

## 8. Shipped surfaces

### CLI and JSON

The dispatcher ships `tui`, `disk`, `cpu`, `memory`, `battery`, `smart`,
`checkup`, `mcp`, `triage`, `anomaly`, `record`, and `history`.

Flags are command-specific. Sampled ranking commands accept interval/limit and
once/duration; static commands accept only useful flags; triage/checkup accept
interval/limit/history selectors but reject streaming controls; human-only
inspect/TUI commands reject JSON. `core.cli.require_options` enforces this.

JSON uses the `stethoscope/1` envelope and `allow_nan=False`. Streaming output is
NDJSON. Exit 0/1/2/3/4 means complete-clean-or-advisory, reportable finding,
usage, permission, and probe/store/runtime failure respectively, with
command-specific finding thresholds documented in SCHEMA.

### Unified TUI

One `scopes/tui.py` shell owns five tabs: disk, CPU, memory, battery, and drives.
`disk_tui.py` only selects disk initially. Entering a tab primes or refreshes
that tab; the loop samples only the active tab. Drives are clamped to at least a
five-second refresh. Triage runs only when the user presses `d`.

Rows are centralized: title, active status, findings, partial notice, column
header, table, footer. Disk alone has process/volume subviews and actions.
Findings selection is independent of row selection and opens canonical evidence.
See [DESIGN.md](DESIGN.md).

### MCP

`stethoscope mcp` ships ten read-only tools over strict newline JSON-RPC 2.0,
MCP `2025-11-25`. It requires initialize/initialized lifecycle, bounds input,
results, IDs and calls, and maps existing scope result helpers directly.
Command exits 0/1 are successful tool results; 2/3/4 set `isError`.

MCP does not expose recording/history stores, drainers, kill/eject, or root-heavy
inspect. It preserves OS permissions and partial visibility. Protocol details
are in [SCHEMA.md](SCHEMA.md#mcp-transport) and the
[agent walkthrough](docs/agent-walkthrough.md#5-mcp-handshake-and-tool-use).

## 9. Delivered capabilities and remaining work

| Capability | State |
|---|---|
| Native disk/CPU/memory/battery/SMART probes | shipped |
| Strict `stethoscope/1` JSON and five exit codes | shipped |
| Secure `baseline-raw/1` recording and history | shipped |
| Robust deviation/leak/runaway/point diagnosis | shipped |
| Canonical triage and full checkup | shipped |
| Centralized five-tab TUI | shipped |
| Ten-tool read-only MCP server | shipped |
| Homebrew formula/tap and release packaging | future; issues #27/#30 |
| Background cross-scope watcher, notification sinks, alerts | future |
| Advanced onset/forecast/cross-process classifiers | future research |

## 10. Invariants

- Structures, never rendered text, cross reusable layers.
- Every number has a stable name and unit.
- PID history is joined with start identity.
- Real watts and unitless modeled scores never mix.
- Missing hardware, optional tools, permission gaps, and failed probes are
  distinct states.
- Separate wakeup counters remain separate detector inputs.
- Baseline storage is strict, bounded, append-only, and context-aware.
- Findings carry evidence and exact drill-down commands.
- CLI, JSON, TUI, and MCP share data helpers rather than reimplement probes.
- The default path is non-root and privacy-preserving.
- No third-party runtime dependencies.

Historical adversarial probe decisions are retained in
[casebook cases 0001-0013](casebook/INDEX.md). Documentation/review traceability
is in [case 0014](casebook/0014-documentation-contract.md) and
[docs/review-disposition.md](docs/review-disposition.md).
