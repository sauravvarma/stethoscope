# stethoscope

**Vital signs for your Mac.** stethoscope 0.2 is a macOS observability tool for
humans and agents. It ships disk, CPU/wakeup, memory, battery, drive-health,
recording/history, anomaly/triage, checkup, unified TUI, strict JSON, and
read-only MCP surfaces from one standard-library-only Python codebase.

![stethoscope terminal interface](assets/tui.svg)

## Install

Requirements are macOS and the system `python3`. There is nothing to compile and
no Python package to install:

```sh
git clone https://github.com/sauravvarma/stethoscope.git
cd stethoscope
./stethoscope --help
./stethoscope checkup
```

A Homebrew formula and tap are still pending in
[#27](https://github.com/sauravvarma/stethoscope/issues/27) and
[#30](https://github.com/sauravvarma/stethoscope/issues/30). No Homebrew install
command is documented until that packaging is verified.

The default paths are user-local. `record` writes daily JSONL files below
`~/.stethoscope/baseline-raw/`; `battery drainers` keeps its unplug baseline at
`~/.stethoscope/battery_baseline.json`.

## Quick start

```sh
./stethoscope tui                              # unified five-tab live view

./stethoscope disk top --once                 # current per-process disk I/O
./stethoscope disk holds 12345                # files held by one process
sudo ./stethoscope disk busy "/Volumes/X9"    # processes pinning a volume
sudo ./stethoscope disk inspect 12345         # live fs_usage trace

./stethoscope cpu top --once                  # CPU, real watts when available
./stethoscope cpu wakeups --once              # package-idle + interrupt wakeups
./stethoscope memory top --once               # footprints and system pressure
./stethoscope memory watch 12345 --duration 60

./stethoscope battery health
./stethoscope battery top --once
./stethoscope battery drainers
sudo ./stethoscope battery inspect
./stethoscope smart                           # all physical drives

./stethoscope checkup                         # full-body exam
./stethoscope triage                          # ranked canonical findings
./stethoscope anomaly leaks                   # one focused detector

./stethoscope record --once                   # append one completed 60s interval
./stethoscope history cpu --since 3am
./stethoscope history baseline battery
```

For machine consumption, add `--json` only where the command supports it:

```sh
./stethoscope checkup --json
./stethoscope cpu wakeups --once --interval 1 --limit 10 --json
./stethoscope history cpu --since 3h --limit 10 --json
```

Output is strict JSON (`NaN` and infinity are rejected). Streaming commands emit
one `stethoscope/1` document per completed interval, one line at a time. Static
and human-only commands reject sampling or JSON flags they cannot honor; options
are command-specific.

## Unified TUI

Run `./stethoscope tui` non-root by default and elevate focused commands rather
than the whole shell. If complete TUI process visibility is necessary, use
`sudo --preserve-env=TERM ./stethoscope tui`; confirmed disk kill and unmount
actions then execute as root. The compatibility entry point
`./stethoscope disk tui` opens the same shell on the disk tab.

| Key | Action |
|---|---|
| `1`-`5` | disk, CPU, memory, battery, drives |
| `Tab` / `Shift-Tab` | next / previous global tab |
| `Up`/`Down`, `j`/`k` | move the selected row |
| `p` / `Space` | pause or resume active-tab sampling |
| `+` / `-` | increase or decrease the interval |
| `d` | explicitly run canonical triage and focus findings |
| `[` / `]` | select the previous / next finding |
| `Enter` while findings are focused | open evidence and drill-down commands |
| `v` on disk | switch process / volume subview |
| `Enter` / `f` on a disk process | held-files popup |
| `i` on a disk process | suspend curses for `fs_usage` inspect |
| `x` on a disk process | send `SIGTERM`, after `[y/N]` confirmation |
| `Enter` / `r` on a disk volume | volume-holder popup |
| `e` on a disk volume | unmount, after `[y/N]` confirmation |
| `q` / `Esc` | quit (`Esc` first leaves finding focus) |

Only the active tab samples; entering a tab initializes its probe. SMART refresh
is rate-limited. Probe failures, absent hardware, partial visibility, and unknown
health are explicit labels rather than healthy-looking empty rows. See
[DESIGN.md](DESIGN.md) for the current visual and interaction contract.

## Commands and status

| Surface | Shipped behavior |
|---|---|
| `tui` | centralized disk/CPU/memory/battery/drives shell |
| `disk top`, `inspect`, `holds`, `busy`, `tui` | I/O rates, syscall trace, open files, eject blockers |
| `cpu top`, `cpu wakeups` | interval CPU, flavor-6 watts, lifetime duty, separate wakeup counters |
| `memory top`, `memory watch` | footprint, pressure, trend, latched leak candidate |
| `battery health`, `top`, `drainers`, `inspect` | battery state, real energy, unitless ranking, unplug attribution |
| `smart status [disk]` | diskutil verdict plus optional smartctl detail |
| [`checkup`](#checkup-triage-and-history) | one-shot five-vital exam using canonical triage |
| `triage`; `anomaly deviation|leaks|runaway|triage` | current/history-based findings |
| `record`; `history [scope]`; `history baseline [scope]` | append-only corpus and retrospective percentiles |
| `mcp` | ten read-only MCP tools over newline JSON-RPC stdio |
| `--json` | stable command-specific `stethoscope/1` documents |

Future work is deliberately separate: release/Homebrew packaging
([#27](https://github.com/sauravvarma/stethoscope/issues/27),
[#30](https://github.com/sauravvarma/stethoscope/issues/30)) and a background
cross-scope watcher/notification/alert service are not shipped commands.

## Checkup, triage, and history

`checkup` is the broad entry point. It takes one completed live interval,
collects point health for memory, battery, and drives, and reuses the canonical
triage findings without parsing rendered text. `triage` ranks deviation, leak,
runaway, pressure, battery, and SMART evidence. Individual `anomaly` modes narrow
that work.

History is intentionally cold until `record` has accumulated samples. A missing
or empty store is a clean cold state; malformed or truncated JSONL is reported as
partial and exits 4. Findings retain exact drill-down commands. Read
[the agent walkthrough](docs/agent-walkthrough.md) for a complete investigation.

## Permissions and partial visibility

The default is non-root. macOS exposes a process's accounting to its owner or to
root, so other users' processes and system daemons may be hidden. Commands that
can still produce useful data set `partial: true` with reasons such as
`not_root`; they do not silently claim complete coverage.

- `disk inspect` and `battery inspect` require root and exit 3 without it.
- `memory watch` distinguishes a missing process from an inaccessible one.
- `disk holds`/`busy` and process rankings can be incomplete without root.
- If a root TUI is necessary, preserve only `TERM`; do not pass the complete
  user environment to a root-capable process.
- State paths resolve to the invoking sudo user's home rather than `/var/root`.
- MCP never raises privilege. Its process names, PIDs, users, and open paths can
  still be sensitive; run it under the least privilege appropriate for the
  client.

## Measurement and platform limitations

- `smartctl` is optional. `diskutil` supplies the dependency-free SMART verdict;
  missing smartctl yields structured partial detail, not a fabricated failure.
- A desktop/no-battery Mac is a supported `absent` state. An ioreg failure is
  different and is reported as unavailable/runtime failure.
- Real per-process watts come only from rusage flavor 6 and are `null` where the
  OS lacks it. `energy_score_per_s` is Apple's modeled, **unitless** ranking and
  is never watts.
- `battery drainers` needs a valid same-session unplug baseline. First run, AC,
  reboot, visibility changes, corrupt state, and a new unplug session are
  explicit reset/partial states.
- History cannot infer a mature baseline from data that was never recorded.
  Source visibility follows the recorded privilege context.
- Process identity is `(pid, start_ticks)`, not PID alone, so PID reuse does not
  splice counters or trends.
- Disk accounting follows macOS's buffered-I/O attribution. Exact syscall/path
  causation belongs to `disk inspect`.

## For AI agents

The authoritative output contract is [SCHEMA.md](SCHEMA.md). Start with
[docs/agent-walkthrough.md](docs/agent-walkthrough.md), then use findings'
`drill_down` commands rather than guessing. Exit codes are:

- 0: completed; clean or advisory-only according to that command
- 1: a command-defined reportable finding (critical-only for triage/checkup)
- 2: invalid invocation
- 3: required permission unavailable
- 4: probe, store, replay, or runtime failure

`./stethoscope mcp` ships a strict MCP `2025-11-25` stdio server with ten
read-only tools: `disk_top`, `disk_holds`, `disk_busy`, `cpu_top`,
`cpu_wakeups`, `memory_top`, `battery_health`, `battery_top`, `smart_status`,
and `checkup`. It deliberately excludes record/history stores, battery drainers,
inspect, kill, and eject. Tool exit 0/1 is a successful MCP result; 2/3/4 sets
`isError`. See the [man-page source](man/stethoscope.1).

## Architecture

```text
stethoscope               dispatcher
core/
  cli.py                   options, strict JSON, exit codes
  rusage.py                libproc/rusage snapshots and PID/start identity
  power.py                 power-state and pmenergy probes
  vmstat.py                system-memory probes
  smart.py                 diskutil/smartctl data layer
  baseline.py              secure baseline-raw/1 JSONL store and replay
  stats.py                 robust bands, trends, leak/runaway evidence
  schema.py                stethoscope/1 envelope
  tui.py                   curses palette, safe drawing, history, popups
  validate.py              probe validation
scopes/
  disk.py, cpu.py, memory.py, battery.py, smart.py
  record.py, anomaly.py, checkup.py
  tui.py                   centralized five-tab shell
  disk_tui.py              compatibility wrapper
  mcp_server.py            bounded read-only MCP stdio transport
diagnosis/
  rules.py, taxonomy.py    evidence-bearing findings and ordering
tests/                     stdlib unittest coverage
casebook/                  append-only engineering decisions
```

Deep probe, vital, statistics, and diagnosis rationale lives in
[ARCHITECTURE.md](ARCHITECTURE.md). The documentation/review contract is recorded
in [case 0014](casebook/0014-documentation-contract.md), with individual review
outcomes in [docs/review-disposition.md](docs/review-disposition.md).

## Reference

- [Machine-readable schema](SCHEMA.md)
- [Agent walkthrough](docs/agent-walkthrough.md)
- [Man-page source](man/stethoscope.1)
- [Architecture](ARCHITECTURE.md)
- [Unified TUI design](DESIGN.md)
- [Casebook index](casebook/INDEX.md)
- [Review disposition ledger](docs/review-disposition.md)
- [MIT license](LICENSE)
