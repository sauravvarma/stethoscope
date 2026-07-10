# Case 0001 — per-process energy attribution
status: treated
opened: 2026-07-10
links: case 0002 · ARCHITECTURE.md §4–5 · Appendix A S1/S3/S4
touches: core/rusage.py, core/validate.py, scopes/battery.py (future)

## 0001.1 · 2026-07-09 · hypothesis

The battery scope can attribute system drain to processes with real units:
`Δri_billed_energy / interval` gives nanojoules per second directly on Apple
Silicon ("preferred whenever nonzero"); the fallback is Apple's own Energy
Impact formula with per-board coefficients from `/usr/share/pmenergy/`; and
Σ per-process watts reconciles against `ioreg` Voltage × InstantAmperage,
with the residual honestly reported as non-CPU draw. (ARCHITECTURE.md as
first drafted, §4–5.)

## 0001.2 · 2026-07-10 · failure — billed_energy is frozen at polling timescales

Adversarial review (Appendix A, S1), reproduced permanently by
`core/validate.py`: on Mac17,9 / macOS 26.4.1, `ri_billed_energy` deltas are
**0/10 nonzero over 10 s at 1 s cadence** even for a pid burning a full core
— including the doc's own §5 example pid. Lifetime ledgers are populated
(23.4 J on `peopled`), so the kernel folds energy in lazily. "Preferred
whenever nonzero" selects a nonzero, non-moving source: `energy_rate` reads
0 W for exactly the runaways the scope exists to catch.

## 0001.3 · 2026-07-10 · failure — the modeled fallback cannot produce watts

S3, verified: pmenergy coefficients are dimensionless Energy-Impact weights
(nested under an `energy_constants` key); every `Mac-*.plist` is keyed by
Intel board-id, so Apple Silicon always falls back to `default.plist`; and
the formula wants `kgpu_time` / `knetwork_*` inputs rusage cannot supply.
"Attributed 9.8 W" on the fallback path is watts minus unitless.

## 0001.4 · 2026-07-10 · failure — watts_now is battery flow, not system draw

S4, verified live: on AC, Voltage × |InstantAmperage| shows charging power,
then ~0 W when topped off while the machine draws 30+ W. ioreg renders
signed fields as unsigned 64-bit (`18446744073709540666` observed).
Desktops have no `AppleSmartBattery` node at all. The reconciliation's
left-hand side only exists while discharging.

## 0001.5 · 2026-07-10 · option — powermetrics as the polling source

**Merit:** true per-process energy as macOS itself scores it, plus P/E-core
residency — the only genuinely watts-true per-process surface.
**Issues:** root-only and heavy; polling it violates the probe philosophy
(SIP-safe, cheap, no-root default path) that every scope is built on.

## 0001.6 · 2026-07-10 · option — modeled unitless score as primary

**Merit:** cheap, SIP-safe, no root; CPU-seconds + pkg-idle-wakeup weights
give a stable *ranking* comparable across processes; feeds baselines fine.
**Issues:** no physical units, so no reconciliation residual — the
`unattributed-draw` finding class cannot key off it.

## 0001.7 · 2026-07-10 · option — billed_energy over long windows

**Merit:** real nanojoules, free, already in the V4 struct.
**Issues:** update cadence unknown (only "not 1 s" is established); useless
for live views; needs empirical cadence work before it can be trusted even
as a slow signal.

## 0001.8 · 2026-07-10 · decision — layer all three, each where it is honest

ARCHITECTURE.md §4–5 rewritten: `energy_score` (option 0001.6) is the
primary vital — explicitly unitless, ranking-only; `Δri_billed_energy`
(0001.7) is demoted to a slow cross-check over minutes-to-hours windows;
`powermetrics` (0001.5) is the gated watts-truth tier under `inspect`; and
the §5 reconciliation renders only when discharging AND a watts-true source
is present, with the attribution table labeled "shares of modeled score,
not watts" otherwise. Chosen because it preserves the two invariants the
alternatives each break: probes stay SIP-safe/cheap on the default path,
and models are labeled as models with residuals reported, not fabricated.

## 0001.9 · 2026-07-10 · follow-up — RUSAGE_INFO_V6 is available

`core/validate.py` probing found flavor 6 returns rc=0 on this machine. V6
carries `ri_energy_billed_to_me` / `ri_energy_billed_to_others` — the
voucher-aware split that could restore *direct* attribution and disambiguate
daemon work done on other processes' behalf. Open thread: measure whether
its ledger updates at usable cadence before `scopes/battery.py` is written;
if it does, revisit 0001.8's ordering.

## 0001.10 · 2026-07-10 · follow-up — V6's live field is ri_energy_nj, and it moves at 1 s

Closes 0001.9's open thread, and corrects its field-name guess: the SDK
header's `rusage_info_v6` carries no `ri_energy_billed_to_me` — the new
energy fields are `ri_energy_nj` (total) and `ri_penergy_nj` (P-core
share). Measured on Mac17,9 / macOS 26.4.1, 11 samples at 1 s cadence:

* `ri_energy_nj`: **10/10 nonzero deltas** for a full-core burner
  (~8.4 J/s), for `peopled` (~1.0 J/s) and `CallHistorySyncHelper`
  (~1.2 J/s) — the same window where `ri_billed_energy` stayed 0/10 for
  all of them (consistent with 0001.2).
* `ri_penergy_nj` tracks P-core residency: ~100% of the burner's total,
  ~0.2% of peopled's (an E-core resident spinner).

So a real per-process **watts-at-cadence** source exists on this hardware,
from the same syscall the scopes already poll — no root, no powermetrics.
Reproduced permanently by `core/validate.py`'s `energy_nj cadence` check.

## 0001.11 · 2026-07-10 · decision — ri_energy_nj is the live energy vital where flavor 6 exists

Revises 0001.8's ordering on the strength of 0001.10 (its stated trigger):
`Δri_energy_nj / interval` becomes the primary **live, real-watts**
per-process signal wherever flavor 6 is available — carried by
`core/rusage.py` (`RUsageInfoV6`, declared in full per S9;
`proc_cpu_sample`; `rusage()['energy_nj']`) and already rendered by
`cpu top`'s POWER column (case 0005). Where flavor 6 is absent the value
is None and surfaces render "-" — never a fabricated zero, and never a
silent fallback to the frozen `ri_billed_energy`. Rejected alternatives:
keeping `energy_score` primary everywhere (0001.6 — stays as the fallback
ranking for non-V6 systems, its unitless caveats unchanged), and
`powermetrics` polling (0001.5 — still root-only/heavy, stays in the
inspect tier). `ri_billed_energy` remains demoted per 0001.8. Validate's
cadence check now also states explicitly that a frozen ledger means
*unmeasurable, not idle* — the misreading that produced case 0005.2's
false exoneration.
