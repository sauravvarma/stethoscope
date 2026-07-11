# Case 0009 — battery scope — health, energy top, unplug drainers
status: treated
opened: 2026-07-11
links: case 0001 · case 0003 · case 0005 · case 0006 · issues #6–#8 · PR #42 review · ARCHITECTURE.md §3–6
touches: core/power.py, core/rusage.py, scopes/battery.py, tests/test_battery.py

## 0009.1 · 2026-07-11 · hypothesis

The disk/cpu/memory template (probe layer below the scope → snapshot/diff/
rank → `core.cli`/`core.schema` agent contract, case 0006) should extend to
battery: `battery health` (issue #7) is a single-shot vitals read off
`ioreg -rn AppleSmartBattery -a` plus `pmset -g batt` for state/time;
`battery top` (issue #6) is a live per-process energy ranking on the disk/
cpu `_run` loop shape; `battery drainers` (issue #8) turns a persisted
baseline into "what has drained the battery since unplug". None of the
three needs a new kernel API — `core/rusage.py`'s flavor-6
`proc_pid_rusage` already carries `ri_energy_nj` (case 0001.11's verified
live per-process power source) and diskio/wakeup counters; the missing
piece is the *system* side (ioreg/pmset/pmenergy/powermetrics), which has
no home yet.

## 0009.2 · 2026-07-11 · option — where the ioreg/pmset/pmenergy/powermetrics probes live

**Merit (inline in scopes/battery.py):** fewest files, matches the original
PR #42 prototype (`scopes/core.py`-era, since removed).
**Issues:** case 0006's own principle — data functions return structures,
never rendered text, and the scope module never touches subprocess/plist
directly — is exactly what case 0007.3 already established a precedent
for (`core/vmstat.py`). Folding four independent subprocess/plist probes
into the scope module would also make `scopes/battery.py` untestable
without mocking subprocess at import time for every test, not just the
ones that need it.
**Chosen:** a new `core/power.py`, one layer below the scope, libproc-free:
owns `read_ioreg_battery()` (plistlib, not text regex — ioreg's `-a` flag
emits a real plist array), `read_pmset_battery()` (text regex is
unavoidable here — `pmset -g batt` has no plist mode — but scoped to
*only* the two fields it can't get elsewhere: charge state and time
estimate), `pmenergy_coefficients()`, `battery_flow_watts()`, and
`parse_powermetrics_plist()`/`read_powermetrics_tasks()`. `scopes/
battery.py` calls these and never opens a subprocess or a plist itself.

## 0009.3 · 2026-07-11 · option — ioreg signed current: reuse core.validate.signed64 or reimplement

core/validate.py already carries a tested `signed64()` (Appendix S4:
`InstantAmperage`/`Amperage` are occasionally rendered as the unsigned
64-bit two's-complement pattern instead of a plain negative int — verified
on this hardware: `18446744073709540666 == -10950`).
**Merit (import from core.validate):** one implementation, already tested.
**Issues:** core/validate.py is the *validation harness* — a diagnostic
tool that inspects probes from outside, not a probe dependency itself.
Having the probe layer (`core/power.py`) import from the thing that
validates the probe layer inverts that relationship for no real benefit
(the function is four lines and has no state).
**Chosen:** `core/power.py` reimplements `signed64()` locally, byte-for-byte
identical in behavior (`tests/test_battery.py::TestSigned64` pins the
exact S4 value), idempotent on both the signed and unsigned representation
so it is always safe to apply.

## 0009.4 · 2026-07-11 · option — extend core.rusage vs read raw structs per call site (battery top/drainers)

Both `battery top` and `battery drainers` need the same four counters per
process per sample: CPU seconds, pkg-idle wakeups, diskio bytes, and V6
energy — one flavor-6 `proc_pid_rusage` struct read covers all four.
**Merit (compose proc_cpu_sample + proc_diskio, no new helper):** zero new
surface on a shared file.
**Issues:** `proc_cpu_sample` and `proc_diskio` each do their own
`_raw_rusage(pid)` struct read; calling both per pid per sample doubles
the syscall for data that lives in one struct, on every interval, for
every accessible process — real cost on a live loop (case 0005's `cpu
top` already treats syscall count per interval as a first-class design
constraint).
**Chosen:** one narrow addition, `core.rusage.proc_power_sample(pid)` —
a single-struct-read helper returning exactly the four counters this scope
needs (plus identity, for the same reused-pid safety every other scope's
snapshot dict already relies on). Matches case 0007.2's precedent (read
the raw counters a scope needs directly, don't grow the general `rusage()`
KEYS contract for one caller).

## 0009.5 · 2026-07-11 · decision — rate (top) vs cumulative (drainers) units kept structurally distinct

PR #42's Copilot review (finding 1) flagged that the original prototype's
`W_IDLE`/`W_INTR` constants blurred rate and cumulative units in both
docstring and use. This scope keeps three distinct quantities that are
never divided into one another by accident:
- `energy_rate_watts` — real watts, from V6 `ri_energy_nj` deltas divided
  by wall-clock elapsed time; `None` whenever V6 is unavailable, never a
  substitute unitless number wearing a watts label.
- `energy_score_per_s` — an explicitly unitless per-second Apple-style
  Energy Impact estimate (`_energy_score()`), built from current
  `/usr/share/pmenergy` coefficients over CPU-seconds/pkg-idle-wakeups/
  diskio-byte deltas — used for `top`'s ranking since real watts are
  usually `None` on non-root runs — with `energy_share_pct` alongside it,
  never presented as watts.
- `energy_score_total` (`drainers`) — the same unitless formula applied to
  *cumulative* since-baseline deltas, not divided by elapsed time at all;
  a fourth quantity, `charge_drop`/`elapsed_s`, documents wall-clock scale
  for the whole run without conflating it with any one process's share.
Interrupt wakeups (`interrupt_wakeups_per_s`) are reported as separate
context in both `top` and never folded into either score — pkg-idle
wakeups are pmenergy's `kcpu_wakeups` input, interrupts are not.

## 0009.6 · 2026-07-11 · decision — health: tri-state present/probe_error, never a fabricated %d

PR #42 findings 2–3: `cmd_health` crashed formatting `None` fields with
`%d`, and `cmd_drainers` didn't distinguish "no battery" from "ioreg
failed". `battery_health()` returns a tri-state `present`
(`True`/`False`/`None`) with a separate `probe_error` field — `False` +
`probe_error=None` means ioreg ran cleanly and reported no battery (a
supported desktop-Mac state); `None` + `probe_error="ioreg_failed: ..."`
means the probe itself broke. Every other field in the health dict is
`None` whenever its source datum is missing (`_empty_health`), and
`_render_health_human` formats every numeric field through a `_fmt`
helper that only ever writes `-` for `None` — no `%d`/`%.1f` is ever
applied directly to a value that can be `None`
(`tests/test_battery.py::TestHealthRenderingNeverFormatsNoneWithD`).
`cmd_health` exits `EXIT_ERROR` on `probe_error`, `EXIT_FINDINGS` only on
an actual "Service Recommended" condition (verified for real on this
machine's own aged battery — 79.6% health, cycle count 373), `EXIT_OK`
otherwise, including the no-battery case.

## 0009.7 · 2026-07-11 · decision — drainers: schema-validated baseline, on_ac always emitted, identity-safe deltas

PR #42 finding 3 (malformed baseline crashes) and finding 4 (SCHEMA.md's
drainers shape omitted `on_ac`) are both closed here. `_load_baseline`
validates the loaded JSON is a dict with the exact expected keys/types
(`schema_mismatch`, `not_an_object`, `invalid_processes_field`,
`invalid_process_entry`, `malformed_json` are all distinct, machine-
readable `reset_reason` values — never a bare boolean or a silent
best-effort parse) and `_save_baseline` writes atomically
(`tempfile.mkstemp` + `os.replace` in the same directory, per ARCHITECTURE
§6.1) to the effective sudo user's home (`_effective_home`,
SUDO_UID/GID-aware, per the same section). `on_ac` is emitted in every
`drainers` document unconditionally — `None` only when `present` is
`False`/probe failed, `True`/`False` otherwise — closing PR #42's
SCHEMA.md gap directly in the data layer rather than leaving it doc-only.
Reset triggers are three distinct, explicit reasons: `on_ac` (currently
plugged in), `unplugged` (the stored baseline was itself saved while on
AC — the actual unplug moment), and whatever `_load_baseline` reported
(`no_baseline`/`malformed_json`/etc.) — reproducing the original
prototype's reset semantics but auditable instead of implicit.

Separately, `rank_drainers` fixes a bug the original PR carried silently:
a process id absent from the baseline (new since baseline, or a reused
pid) was `continue`-skipped rather than counted. Here it is instead
baselined to zero and counted in full — a pid's whole cumulative
footprint since it started, if that start postdates the baseline, is a
real contribution to "what drained the battery since unplug" and dropping
it silently underreports exactly the newly-spawned process a user is
usually looking for.

## 0009.8 · 2026-07-11 · option — battery inspect: claim powermetrics watts-reconciliation or not

Issue #6/ARCHITECTURE §5 describe a richer, root-only reconciliation tier
against `powermetrics`' real per-task energy accounting.
**Merit (claim it):** matches the issue's ask for a "why" tier beyond the
unitless default.
**Issues:** `powermetrics`' plist `energy_impact` field is Apple's own
Energy Impact score (the same number Activity Monitor's Energy column
shows) — not a documented watts quantity, and no root was obtainable in
this sandbox on this hardware to confirm the plist's actual task-level
schema at all (only the permission-denial path — `"powermetrics must be
invoked as the superuser"`, exit 1 — was verified live). Labeling an
unverified, likely-unitless number as reconciled watts would be exactly
the fabrication the task's own instructions rule out.
**Chosen:** `battery inspect` ships root-gated (`EXIT_PERMISSION` without
root) with a defensively-written, hermetically-tested parser
(`parse_powermetrics_plist`/`read_powermetrics_tasks`, `TestReadPowermetrics
Tasks`/`TestParsePowermetricsPlist`) that surfaces `energy_impact` labeled
exactly as that — never as watts — with an explicit
`reconciliation_note` stating it is not a watts reconciliation. Probe
failures (`timeout`, `powermetrics_missing`, a parse failure) return
`available: false` with a named `reason`, never a partially-fabricated
task list.

## 0009.9 · 2026-07-11 · decision — battery scope shipped

`core/power.py` (probe layer), one narrow addition to `core/rusage.py`
(`proc_power_sample`), and `scopes/battery.py` (`health`/`top`/`drainers`/
`inspect`) ship together. `health`/`drainers`/`inspect` reject unsupported
flags via `core.cli.require_options`; `top` follows `cpu.py`'s live-loop
convention (`--json`/`--once`/`--duration`/`--interval`/`--limit`).
Verified against this machine's real battery: `battery health` correctly
surfaced this MacBook Air's own aged battery (`Service Recommended`,
79.6% health) and exited `1`; `battery top --once --json` ranked live
processes by `energy_score_per_s` with `partial: true`/`not_root` (no
sudo available); `battery drainers` exercised the full baseline lifecycle
live — first run (`no_baseline` reset), a real second-run diff with
nonzero `cpu_seconds_since`/`energy_score_total`, and both `not_an_object`
and `malformed_json` resets against hand-corrupted state files.
`battery inspect --json` correctly returned `EXIT_PERMISSION`/
`root_required` without root. `python3 -m core.validate`: 9 checks, 0
FAIL, unchanged by the `proc_power_sample` addition. `python3 -m
unittest discover -s tests`: 187 tests, 0 failures (94 new in
tests/test_battery.py). Powermetrics' actual data-success plist schema
remains unverified on real hardware in this environment (no root
available) — documented as a caveat rather than a blocker; the parser is
hermetically tested against synthetic fixtures and fails closed
(named `reason`, not a crash or fabricated data) if the real schema ever
diverges from the defensive shape assumed here.

## 0009.10 · 2026-07-11 · failure — integration exposed false healthy and false rate signals

Parent integration review found that a missing design/max capacity was
labeled `Normal`, a lone mAh `CurrentCapacity` could be mislabeled as a
percentage, unknown AC state was coerced to `false`, and the CPU term in
`energy_score_per_s` was not divided by the sample interval while wakeups and
disk bytes were. Those are all success-shaped errors. Health now needs actual
capacity or an explicit Apple condition before saying `Normal`; unknown power
state stops `drainers` without changing its baseline; every live score input
is interval-normalized; and pmset/pmenergy degradation is reflected in
`partial_reasons`.

## 0009.11 · 2026-07-11 · decision — identify the discharge session, boot, and visibility

A saved process identity alone cannot prove that two invocations belong to
the same "since unplug" window. The baseline now records the boot-session
UUID, mach sample tick, collection privilege, and the latest AC-to-battery
transition parsed from `pmset -g log`. A changed unplug timestamp or boot UUID
resets the window. If power history is unavailable, the result is partial and
a charge increase still forces a reset. A process absent from the baseline is
zero-baselined only when its start tick proves it began after that baseline;
newly visible long-running processes are skipped rather than charged their
whole lifetime, and a privilege mismatch marks the result partial.

Persistence also moved from path-based post-write `chown` to directory/file
descriptors opened with `O_NOFOLLOW`. Ownership is applied with `fchown`
before atomic rename, read/write/ownership errors return exit `4`, and
non-finite, unbounded, or invalid-UTF-8 state is rejected explicitly.

## 0009.12 · 2026-07-11 · follow-up — preserve native probe semantics

`/usr/share/pmenergy`'s Intel files are keyed by IORegistry `board-id`, not
`hw.model`; the probe now selects that exact filename and keeps
`default.plist` as the Apple Silicon fallback. Powermetrics output is a
NUL-delimited plist stream, may include PID 0 (`kernel_task`), and can expose
both per-second and sample-total Energy Impact. The parser accepts exactly one
framed sample, preserves both unitless fields separately, validates every
scalar, and sanitizes process names only at human terminal boundaries. Plist
XML parse errors are caught at every external boundary.

## 0009.13 · 2026-07-11 · follow-up — QoS is part of Energy Impact

The rusage struct's QoS CPU ledgers sum to aggregate CPU time, and pmenergy
ships distinct `kqos_*` weights (background work can cost far less than
default/interactive work). Applying only `kcpu_time` could therefore reverse
rankings. `proc_power_sample` now carries all seven cumulative QoS ledgers;
live and cumulative scoring weight each class, use `kcpu_time` for maintenance
or unclassified time, and persist the counters in the unplug baseline.

## 0009.14 · 2026-07-11 · follow-up — preserve exited-task energy

Powermetrics reserves PID `-1` for its `DEAD_TASKS` aggregate. Rejecting every
negative PID silently removed work from processes that exited during the
sample. The inspect parser now accepts `-1` and PID `0` (`kernel_task`) while
continuing to reject other negative identifiers.
