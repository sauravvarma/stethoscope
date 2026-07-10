# Case 0004 — which wakeup counter alarms wakeup-storm
status: treated
opened: 2026-07-10
links: case 0003 · ARCHITECTURE.md §4/§7 · Appendix A S8
touches: core/rusage.py, scopes/cpu.py (future), diagnosis/rules.py (future)

## 0004.1 · 2026-07-10 · hypothesis

After S8 split the summed wakeup vital in two, the alarm signal for the
`wakeup-storm` class should key on `pkg_idle_wakeups_per_s` alone: it is
what Activity Monitor's "Idle Wake Ups" column reports and it maps directly
to the energy story (each pkg-idle wakeup drags the package out of idle);
`interrupt_wakeups_per_s` is orders of magnitude noisier and was demoted to
diagnostic context. (This session's D2 revision of ARCHITECTURE.md §4.)

## 0004.2 · 2026-07-10 · failure — the designated alarm reads zero during a live storm

End-to-end detection test (synthetic culprits, sampled through
`core.rusage` at 1 s over 12–20 s): a canonical 1 ms `time.sleep` storm —
the example shape named in the doc's own `wakeup-storm` row — measured
`pkg_idle_wakeups_per_s` = **0.0 on every single sample**, indistinguishable
from an idle control (also 0.0). The entire discriminating signal lived in
`interrupt_wakeups_per_s`: ~797/s sustained vs a ~1/s quiet baseline — a
clean ~800× separation. A pkg-idle-only alarm has *no signal to threshold
against* for this culprit class; it reads 0 whether the machine is quiet or
under storm. (Stronger than S8's original measurement, which saw 1 pkg-idle
in 2 s — this run saw exactly zero.) Caveat recorded: sleep-loop timers may
route through timer coalescing differently from the polling/network storms
pkg-idle is meant to catch; this test establishes blindness for the
sleep-loop shape specifically.

## 0004.3 · 2026-07-10 · option — keep the pkg-idle-only alarm

**Merit:** matches Apple's user-facing definition (Activity Monitor,
`powermetrics`), and pkg-idle is the counter with the direct package-energy
mechanism. **Issues:** demonstrably blind to the sleep-loop storm — the
class's own canonical example. An alarm that misses its named example is
not an alarm.

## 0004.4 · 2026-07-10 · option — alarm on either counter relative to its own baseline

**Merit:** catches both storm shapes; the interrupt counter's noisiness is
an *absolute-threshold* problem, and §6's detectors are baseline-relative
by design — ~800/s against a ~1/s own-baseline is exactly what a robust
z-score is for. **Issues:** interrupt baselines vary widely across process
classes, so the vital is meaningless until the process has a baseline (or a
population prior floor); slightly weaker energy story, since an interrupt
wakeup does not necessarily cost package-idle exit.

## 0004.5 · 2026-07-10 · decision — either counter, baseline-relative, never summed

0004.4. ARCHITECTURE.md §4 and §7 updated: `wakeup-storm` (and the wakeup
clause of `sync-loop`) alarm on **either** wakeup vital far above its own
baseline; the two counters are never summed (S8 stands); absolute
thresholds on interrupt wakeups are explicitly disallowed. Rejected 0004.3
because measured blindness to the canonical example outweighs definitional
alignment with Activity Monitor — the doc keeps pkg-idle as the vital that
*names* the energy mechanism, but not as the sole gate.

## 0004.6 · 2026-07-10 · follow-up — open questions for scopes/cpu.py

(a) Do real-world polling storms (chat apps, Electron timers, network
polling) trip pkg-idle nonzero, or is pkg-idle rare in practice on Apple
Silicon under timer coalescing? Sample a few known-chatty apps before the
cpu scope hard-codes anything. (b) The `powermetrics` inspect tier reports
per-process wakeup detail — wire `cpu inspect` to disambiguate which shape
a flagged storm is. (c) Synthetic-culprit note for future tests: naive
Python threading cannot exceed ~100% CPU (GIL); use GIL-releasing C calls
(hashlib on large buffers) or subprocesses.

## 0004.7 · 2026-07-10 · follow-up — 0004.6(a) answered: pkg-idle is dead in real workloads

Live investigation of this machine (60 s, 1 s cadence, 593 visible
processes, real workload including a 145%-CPU runaway and two spinning
sync daemons): `pkg_idle_wakeups_per_s` measured **0.00 for every process
on every sample**. Not just sleep-loop storms — *nothing* trips pkg-idle
on this Apple Silicon machine under timer coalescing. The interrupt
counter carried all signal (runaway node: 349/s; ghostty: 150/s at 1%
CPU). Decision 0004.5 (either counter, baseline-relative) is vindicated
in the strongest form: a pkg-idle-only alarm would flatline forever here.
Remaining open: whether pkg-idle ever fires on this hardware (worth one
`powermetrics` cross-check when the inspect tier lands); if it never
does, `scopes/cpu.py` may treat pkg-idle as Intel-era vestige and lead
with interrupt wakeups.
