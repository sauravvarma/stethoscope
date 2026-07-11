# Case 0005 — cpu scope — who is burning CPU now
status: treated
opened: 2026-07-10
links: case 0001 · case 0003 · ARCHITECTURE.md §3–4 · Appendix A S1/S2/S10
touches: scopes/cpu.py, core/rusage.py, core/validate.py, stethoscope

## 0005.1 · 2026-07-10 · hypothesis

A blind agent restricted to stethoscope as its only system-data source can
answer "which processes are constantly hogging my CPU?" on a machine with
two known runaways: `peopled` at ~62% CPU and `CallHistorySyncHelper` at
~43%, spinning in an XPC init loop for 62 days (89 h / 61 h lifetime CPU).
If it cannot, the gap defines the cpu scope's minimum viable surface.

## 0005.2 · 2026-07-10 · failure — no CPU surface, and the accidental one exonerates the culprits

Experiment run 2026-07-10, context-free agent, stethoscope-only constraint:

* The only CPU signal in the tool was an implementation detail of
  `core/validate.py`'s billed-energy check, which prints "the 3 busiest
  accessible pids by lifetime CPU" — pids without names, without the values
  it sorted on. The agent recovered the right candidates (1504, 997, 733)
  only by bouncing pids through `disk holds` for name resolution.
* It then diffed the one rate-adjacent value the tool prints,
  `ri_billed_energy`, got **0/10 nonzero deltas over ~3 min** for processes
  burning 62% and 43% CPU at that moment (verified via `ps`), and reported
  "essentially idle — could not determine". The frozen ledger (0001.2 / S1)
  turned a coverage gap into a confident **false exoneration** of exactly
  the runaways the tool exists to catch.
* Verdict: could not determine. Blocking gap: no `cpu top`; aggravating
  gap: a frozen counter presented without a "frozen ≠ idle" warning.

## 0005.3 · 2026-07-10 · option — expose the lifetime-CPU ranking that already leaks from validate

**Merit:** near-zero code; the ranking did surface the right two pids.
**Issues:** lifetime totals are biased toward long-lived daemons — the
opposite of "hogging *now*"; no names, no rates, top-3 only. Formalizing a
diagnostic's side effect as a surface entrenches an accident.

## 0005.4 · 2026-07-10 · option — energy deltas as the activity signal

**Merit:** one field, real units, already printed by validate.
**Issues:** `ri_billed_energy` is frozen at polling cadence on this
hardware (0001.2); 0005.2 measured it reading 0 W for two 60%-class
runaways. Disqualified as primary by direct evidence.

## 0005.5 · 2026-07-10 · option — snapshot-and-diff of ri_user_time + ri_system_time (chosen)

**Merit:** exactly the disk scope's proven pattern (`snapshot → diff →
rank → render`) over different V4 fields; timebase conversion and
pid-reuse identity already enforced by `core/rusage.py` (cases 0003, S2,
S10); answers the instantaneous question directly.
**Issues:** an interval rate alone can't distinguish a burst from a
constant hog — needs the lifetime view alongside; other users' processes
still need root (same rule as disk).

## 0005.6 · 2026-07-10 · decision — cpu top: %CPU + lifetime duty + live watts, one row per process

`scopes/cpu.py` ships `cpu top` built on option 0005.5, rejecting 0005.3
(accumulation bias, accidental surface) and 0005.4 (frozen ledger). Each
row carries the three views that 0005.2 showed are needed together:
**%CPU this interval** (user/sys split), **lifetime CPU time and duty**
(cumulative CPU over awake-age, casebook 0003.7 denominator — the
"constantly" in "constantly hogging"), and **live watts** from
`ri_energy_nj` (flavor 6, case 0001.10) rendered as "-" where flavor 6 is
absent, never a fabricated zero. Probe access stays inside `core/rusage.py`
(`proc_cpu_sample`), preserving the S2/S9/S10 invariants. Rerunning the
0005.2 scenario against the shipped scope ranks peopled (61.8%, 89h10m,
9.8% duty) and CallHistorySyncHelper (42.6%, 60h54m, 6.7% duty) #1 and #2
in the first frame.

## 0005.7 · 2026-07-10 · follow-up — validate now says "frozen ≠ idle"

The aggravating gap in 0005.2: `core/validate.py`'s cadence check now
names the sampled processes and, when the billed ledger is frozen, states
that the signal is *unmeasurable at cadence, not idle*. Windowed vitals
(`duty_cycle`, wakeup rates — ARCHITECTURE.md §4) remain open for the
Sampler work; this case covers the top surface only.

## 0005.8 · 2026-07-10 · follow-up — blind re-run passes; residual gaps are the v0.5 agent contract

The 0005.1 experiment repeated against the shipped scope (fresh
context-free agent, stethoscope-only): both culprits named decisively in
every frame — peopled ~62% (19% user / 42% sys, read correctly as
syscall-spinning) and CallHistorySyncHelper ~43%, together ~105% of the
observed ~135% system load; CPU TIME + DUTY% substantiated "constantly"
without external tools. Gaps the agent still hit, all already roadmapped
rather than new design debt: no one-shot / `--samples` mode and no
`--json` on `cpu top` (v0.5 agent contract — it had to background, kill,
and strip ANSI), COMMAND column truncation with no path option (it
borrowed `disk holds` for name resolution), no sort-by-lifetime option,
and no per-pid drill-down for the user/sys split (v0.2 wakeup vitals).

## 0005.9 · 2026-07-11 · follow-up — the v0.5 agent-contract gap closes; wakeups joins top

0005.8's residual gap is closed: `cpu top` now takes `core/cli.py`'s shared
`--json` / `--once` / `--duration N` / `--interval N` / `--limit N`
surface (the same contract `disk top` already carries, ARCHITECTURE.md's
agent-contract line), so the 0005.8 agent's background-kill-and-strip-ANSI
workaround is no longer needed — one `--json --once` call returns exactly
one schema-versioned document. Every row now also carries the v0.2 wakeup
vitals this case's own follow-up list named as missing:
`pkg_idle_wakeups_per_s` and `interrupt_wakeups_per_s` (case 0004), plus a
`total_wakeups_per_s` for ranking. A new `cpu wakeups` command ranks by that
total — case 0004's "who is waking the CPU" question, sharing `cpu top`'s
data layer (`core/rusage.py`'s `proc_cpu_sample`, extended in place rather
than duplicated) and rendering the same row shape sorted differently, so
0005.6's %CPU/duty/watts view and 0004's wakeup view stay one coherent
surface instead of two. Non-root visibility is now marked explicitly:
`--json` output sets `partial: true` / `partial_reasons: ["not_root"]`
rather than only warning on stderr, matching `disk`'s contract. Still open
per 0005.8: COMMAND column truncation with no path option, and no
sort-by-lifetime option on `top` — neither is a wakeup- or agent-contract
gap, so both stay out of this case's scope.
