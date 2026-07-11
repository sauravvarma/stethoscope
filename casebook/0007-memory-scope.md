# Case 0007 — memory scope — footprint ranking + leak watch
status: treated
opened: 2026-07-11
links: case 0003 · case 0005 · case 0006 · issues 3–4 · PR #41 review
touches: scopes/memory.py, core/vmstat.py, tests/test_memory.py

## 0007.1 · 2026-07-11 · hypothesis

The disk/cpu template (`snapshot → diff/rank → render`, shared `core.rusage`
identity, the `core.cli`/`core.schema` agent contract from case 0006) should
extend cleanly to memory: `memory top` (issue #3) ranks accessible processes
by `ri_phys_footprint` — the number Activity Monitor's "Memory" column shows
— with a system summary from `vm_stat` + the kernel's own pressure verdict;
`memory watch <pid>` (issue #4) turns one process's footprint samples into a
slope and a leak verdict, the primitive a future automatic leak sweep can
run over every process. Neither needs a new probe: both live entirely on
data `proc_pid_rusage` flavor 4 already returns.

## 0007.2 · 2026-07-11 · option — extend core.rusage.rusage() with resident_size vs read the raw struct directly

**Merit (extend):** one converted, fully-keyed dict for every scope,
matching the existing public API shape.
**Issues:** `rusage()`'s KEYS contract (tests/test_core.py) is a shared,
tested surface belonging to every scope, not just memory; adding a field
there to serve one scope's ranking loop is a shared-contract change for a
value (`ri_resident_size`) the struct already carries. It would also cost
one full vitals conversion (QoS times, energy, wakeups — none of which
`memory top` uses) per accessible pid, every interval, over every process
on the machine.
**Chosen:** read `core.rusage._raw_rusage(pid)` directly for the two raw
counters this scope needs (`ri_phys_footprint`, `ri_resident_size`),
exactly as `disk.snapshot_diskio` already does for its own two counters.
No duplicate struct, no new core.rusage surface, no shared-KEYS regression.

## 0007.3 · 2026-07-11 · option — where vm_stat/sysctl pressure parsing lives

**Merit (inline in scopes/memory.py):** fewer files; matches the original
pre-refactor prototype.
**Issues:** `core/rusage.py`'s own contract (case 0003) is libproc-only;
folding subprocess-text parsing into it would blur that boundary for every
future caller. Keeping it inline in scopes/memory.py also means the scope
module itself parses raw text, which case 0006's "data functions return
structures, never rendered text" principle argues against holding above
the probe layer.
**Chosen:** a new `core/vmstat.py` — one layer below the scope, libproc-free,
owns `vm_stat` and `kern.memorystatus_vm_pressure_level` parsing and
returns only structures (`parse_vm_stat`, `pressure_name`,
`system_memory`). `scopes/memory.py` never touches subprocess text.

## 0007.4 · 2026-07-11 · option — leak signal: slope-threshold only vs latched slope + plateau

**Merit (threshold only):** simplest to implement and to explain — one
comparison, `slope_mb_per_min(samples) > LEAK_SLOPE_MB_PER_MIN`.
**Issues:** recomputed every sample, it can flip a documented "sustained
growth" verdict back to false the moment growth pauses — exactly what a
GC pass or a compaction cycle does to a real leaking process without
actually fixing it, and it never tests whether growth is still *recent*,
only whether the all-time average crosses a bar. Issue #4 explicitly asks
for a plateau check and enough samples before flagging at all — a bare
threshold satisfies neither.
**Chosen:** `leak_state(samples, latched)` — a small pure function. A new
trip needs `MIN_LEAK_SAMPLES` (5) of history, an overall slope above
`LEAK_SLOPE_MB_PER_MIN`, and a recent `PLATEAU_WINDOW`-sample slope that
has *not* flattened (`is_plateaued`); once tripped, `latched` carries
forward and is never cleared by a later plateau. Transparent (three
inputs, three named checks) and directly unit-testable without sampling a
real process (tests/test_memory.py's `TestLeakState`/`TestPlateau`).

## 0007.5 · 2026-07-11 · decision — memory top + memory watch shipped on the disk/cpu template

`scopes/memory.py` ships both commands on option 0007.2/0007.3/0007.4's
choices. `top` ranks every accessible `(pid, start_abstime)` by footprint
(no activity filter — a footprint is a snapshot, not a rate, so "idle this
interval" has no memory analogue, unlike disk/cpu), reporting
`footprint_bytes` and `resident_size_bytes` per process plus a
`core.vmstat.system_memory()` summary. `watch` samples one pid, reports
`slope_mb_per_min`, `plateaued`, and the latched `leak_candidate`, and
exits `1` (`EXIT_FINDINGS`) once a run ever latches. Both honor
`--json`/`--once`/`--duration`/`--interval`; `top` additionally honors
`--limit`, `watch` explicitly rejects it (no ranking, nothing to limit).
Verification: `python3 -m unittest discover tests` — 122 tests, 0 failures.

## 0007.6 · 2026-07-11 · follow-up — PR #41 review applied before this ships

The prototype PR (#41) that first proposed this scope drew three review
comments, all addressed here rather than carried forward as debt:

* **Latch, not a per-sample flip.** 0007.4 already builds this in —
  `leak_state`'s `latched` argument is the previous sample's verdict, and a
  plateau after a trip cannot clear it (`test_latch_stays_true_once_
  tripped_even_after_plateau`).
* **ESRCH vs EPERM.** `proc_pid_rusage` returning `None` used to collapse
  "pid doesn't exist" and "pid exists, no permission" into one outcome,
  which mapped a plain typo'd pid to a misleading permission error.
  `pid_status()` calls `os.kill(pid, 0)` — no signal sent, existence/
  permission probe only — and `cmd_watch` now returns `EXIT_USAGE` for a
  gone pid and `EXIT_PERMISSION` for one that exists but is inaccessible.
  Same distinction covers a pid recycled mid-watch (S10): a changed
  `start_abstime` is treated as "the original target is gone", never as
  silently adopting the new process's counters.
* **Contract-level watch tests.** The prototype tested only `top`'s JSON
  shape. `tests/test_memory.py`'s `TestWatchContract` now drives `cmd_watch`
  through a scripted `_raw_rusage` sequence to assert `leak_candidate`,
  `slope_mb_per_min`, `running`, and the `EXIT_FINDINGS`/`EXIT_USAGE`/
  `EXIT_PERMISSION` exit codes directly, including a process exiting
  mid-watch and a latched leak surviving that exit.

## 0007.7 · 2026-07-11 · follow-up — an absent probe is not zero memory

Integration review found that the first `core.vmstat` draft converted every
missing binary, timeout, nonzero exit, or parse failure into an empty string;
`system_memory()` then emitted zero for every byte count. The pressure field
said `unknown`, but a machine-readable consumer could still mistake the zero
totals for measurements. Probe failures now become stable error codes carried
in `system.errors`, affected numeric fields are `null`, `available` is false,
and the document is marked partial with reason `system_memory_probe`.
Per-process footprint data remains usable instead of turning one failed
system summary into a command-wide error.
