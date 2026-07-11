# Case 0011 — diagnosis and triage composition
status: treated
opened: 2026-07-11
links: case 0002 · case 0004 · case 0010 · ARCHITECTURE.md §6
touches: core/stats.py, diagnosis/taxonomy.py, diagnosis/rules.py, scopes/anomaly.py, scopes/checkup.py

## 0011.1 · 2026-07-11 · hypothesis

A useful one-shot diagnosis can compose independent evidence without claiming
a speculative root cause: statistics produce finite evidence, rules classify
structures, taxonomy orders findings, and the CLI alone gathers/replays/renders.
The invariant is that each finding remains independently defensible and names
the command that can confirm it.

## 0011.2 · 2026-07-11 · failure — the prior prototype crossed every layer

The PR #49 prototype put collection, SQL history queries, statistics,
classification, and terminal rendering in one 567-line scope. It keyed leak and
runaway history by PID, summed wakeup signals into one fallback alarm, issued a
three-second leak fallback on empty history, and depended on SQLite `COUNT`.
Those choices conflict with the shipped daily JSONL scanner, PID/start identity,
and the separate package-idle/interrupt contracts from cases 0010 and 0004.

## 0011.3 · 2026-07-11 · option — adapt the monolithic prototype

**Merit:** least new module surface and closest behavioral match to PR #49.
**Issues:** replay and probe mechanics would remain entangled with classifiers;
tests could only validate the whole scope; future renderers would be tempted to
parse CLI text; PID reuse and wakeup conflation would remain easy regressions.

## 0011.4 · 2026-07-11 · option — layered evidence and thin orchestration

**Merit:** pure finite-safe statistics and pure classifiers are hermetic;
JSONL replay can retain only current context, normalized names, and PID/start
identities; CLI rendering can sanitize every external string in one place.
**Issues:** more explicit structures and stable fields must be documented and
maintained.

## 0011.5 · 2026-07-11 · decision — compose findings, do not invent causes

Choose 0011.4 and reject 0011.3. `core/stats.py` owns bands/trends/evidence,
`diagnosis/rules.py` owns classification, `diagnosis/taxonomy.py` owns the
stable finding shape and deterministic worst-first ordering, and
`scopes/anomaly.py` owns one live collection, bounded `baseline.scan()` replay,
point probes, and rendering. Triage reports independent deviation, leak,
runaway, pressure, battery, and SMART findings without speculative correlation.
Cold history is a note rather than an expensive fallback; corruption remains
visible and fails the command even when useful findings were retained.

## 0011.6 · 2026-07-11 · failure — degraded evidence erased independent facts

The first orchestration draft aborted triage when JSONL history could not be
opened, discarding a live 100% CPU runaway and point-in-time health findings.
History failure is now isolated: the result retains static and point evidence,
sets `history.available: false`, supplies a stable error, and exits 4. Corrupt
history similarly retains findings and renders bounded diagnostics. Optional
pmset and smartctl gaps stay partial; only required probe failures are fatal.

## 0011.7 · 2026-07-11 · failure — the observer diagnosed itself

The recorder's own process row shared normalized names such as `python3` with
real workloads. Its low historical CPU could make another Python process look
critical, while a short triage interval could flag the triage process itself.
Current and historical `context.sampler` identities are now excluded from leak
and runaway targets and process baselines. If replay only discovers later that
an earlier contributor was a sampler, the affected name bucket is reset
conservatively; dedicated sampler system metrics remain available for overhead
monitoring.

## 0011.8 · 2026-07-11 · follow-up — one diagnosis, two presentations

Issue #25 adds `checkup` as a second presentation of this same composition
node, not a competing classifier. It invokes structured triage once, preserves
its ordered findings, overall verdict, notes, partial reasons, provenance, and
error/exit behavior, then derives full-body vitals from the same raw interval
and triage point structures. The interval collector now exposes its already-read
memory and battery observations outside the persisted `baseline-raw/1` sample;
triage reuses them and probes SMART once. Explicit states distinguish unknown
probes from supported absence of a battery or physical drives, so degraded
visibility cannot become success-shaped health.

## 0011.9 · 2026-07-11 · failure — provenance leaked into live vital state

The first checkup composition used triage's aggregate partial reasons for live
CPU and disk state. Historical `not_root` records could therefore label a
complete current root sample partial. It also ranked the observer in consumer
lists and omitted the already-collected process footprints from memory. The
current triage structure now retains its own partial state separately from
history provenance; checkup uses only current visibility for process-backed
vitals, excludes the exact sampler PID/start identity from every ranking, and
exposes memory consumers without another probe. Reused point failures are
deduplicated before constructing the canonical runtime error.
