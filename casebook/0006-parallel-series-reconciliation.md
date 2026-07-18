# Case 0006 — reconciling the krishankumar95 parallel series
status: diagnosed
opened: 2026-07-10
links: case 0001 · case 0003 · case 0004 · case 0005 · ARCHITECTURE.md §3–4, §6–8 · Appendix A S1/S2/S9/S10 · PRs #36–#49, #51
touches: scopes/disk.py, core/rusage.py, scopes/output.py (future), tests/, .github/

## 0006.1 · 2026-07-10 · hypothesis

The 14 stacked single-commit PRs from krishankumar95 (#36–#49, branches
`krishankumar95/pr01…pr15`, all based on old main `2c5f7dc`) can be merged
into the current lineage (`release/v0.1.1` + PR #51) without losing either
side's work. The series covers most of the v0.2–v1.0 roadmap: disk fixes,
test harness + CI, a shared sampling core, a `--json` agent contract, four
scopes (cpu/memory/battery/smart), recording, anomaly detection, checkup,
an MCP server, a unified TUI, and docs.

## 0006.2 · 2026-07-10 · failure — the lineages diverged at the root, and the series repeats four systemic defects

Vetted 2026-07-10 by three independent review passes (foundation #36–39,
scopes #40–43, platform #44–49), each commit diffed against its parent and
against the current lineage's probe contracts. The series was written
against old main: it predates `core/rusage.py`, `core/validate.py`, the
casebook, and ARCHITECTURE.md's Appendix A findings — so nothing merges
mechanically; every keeper is a port. Four defects repeat because the
scopes were stamped from one template:

1. **Bare-pid snapshot diffing (S10)** in every live `top` loop
   (`cpu.rank_cpu`, `disk.rank_io`, `memory watch`, `battery top`,
   record's 60 s sampler, anomaly's 24 h joins). `RUsage.start` is carried
   in their own tuple and used only for the name cache. Harmless at 0.5 s
   intervals; a real splice/fabrication risk at 60 s+ windows.
2. **Idle + interrupt wakeups summed** (violates §4 / case 0004: a
   sleep-loop storm is interrupt-only — measured ~800/s against a ~1/s
   baseline with **zero** pkg-idle wakeups — so a summed rank is blind to
   it while absolute interrupt counts drown idle ones). Appears in
   #40 `cpu wakeups`, #42's score, both TUI tables, #49's detectors.
3. **Text parsing below the surface layer**: regex over `vm_stat`,
   `diskutil list/info`, `ioreg` where structured routes exist
   (`host_statistics64`, `diskutil -plist` + plistlib, `ioreg -a`).
4. **Invented statistics where the architecture specifies measured ones**:
   #42's energy score weights wakeups/s against CPU-percent in `top` but
   raw wakeup counts against CPU-seconds in `drainers` — two incomparable
   numbers emitted under one JSON name — where flavor-6 `ri_energy_nj`
   (0001.10, measured 10/10 moving at 1 s) gives real nanojoules; #49's
   deviation detector fires on single-sample p99 exceedance with
   min_count=3 (vs §6's ≥50 maturity gate + run-length persistence), its
   leak detector uses mean-based OLS where §6.3 prescribes
   Mann–Kendall + Theil–Sen, and its cold-start fix (`4034f19`) skips any
   low-variance band — permanently blinding the detector on exactly the
   metrics where exceedance is most informative — and carries a latent
   `None` crash (`p99 - p50` unguarded when p50 is absent).

Credit where due, verified: **S9 is not violated anywhere** — their
`RUsageInfo` is byte-complete `rusage_info_v2` (160 B, checked field-by-
field against the SDK header) passed at flavor 2, though its docstring
says "Prefix of rusage_info_v2", normalizing the habit S9 exists to kill;
S2 is dodged cleverly (CPU% as tick/tick ratio needs no timebase; where
seconds are needed, `abstime_to_seconds` converts correctly); S1 is
dodged by omission (nothing reads `ri_billed_energy`); and #42's
`drainers` since-unplug baseline is the one S10-correct diff in the
series, with a test. Test quality is consistently good and hermetic.

Process notes: all 14 commits carry `Co-authored-by: Copilot` trailers
(must be dropped at squash per repo policy — human co-authorship credit
stays); none adds a casebook entry (the rule postdates their base); the
man page in #46 is already stale for commands its own stack adds above it.

## 0006.3 · 2026-07-10 · option — adopt the series wholesale, rebase our lineage onto it

**Merit:** lands the entire roadmap at once; the work is competent and
tested; avoids porting cost.
**Issues:** its spine (`scopes/core.py`) is a strictly weaker reinvention
of `core/rusage.py` — V2-only (no QoS/energy fields, so cpu/battery
scopes are capped), raw ticks exposed to callers (the S2 41.7x trap armed
for whoever divides by wall time first), bare-pid snapshots (S10), no
struct/header validation; adopting it would demolish cases 0001/0003/0005
and PR #51's V6 energy work, and re-import defects 1–4 above wholesale.

## 0006.4 · 2026-07-10 · option — reject the series wholesale

**Merit:** zero reconciliation cost; the current lineage's contracts stay
pristine.
**Issues:** throws away the best pieces the current lineage lacks
entirely: the `--json`/exit-code/SCHEMA agent contract (#39 — the exact
gap the blind re-review flagged, 0005.8, issues #14/#15), the repo's only
CI workflow (#37), two live disk bugs fixed (#36), a stdlib MCP server
(#45), a memory scope, a smart scope, and the drainers baseline mechanism.
Also discards a contributor's tested work where it is genuinely
complementary — bad precedent and bad economics.

## 0006.5 · 2026-07-10 · option — selective port onto the current lineage (chosen in 0006.6)

**Merit:** keeps both invariants — the probe contracts stay authoritative,
and every genuinely new capability lands; each port gets a casebook entry
and adversarial testing on the way in.
**Issues:** everything is manual (the spines are incompatible, so no
cherry-pick applies cleanly); sequencing matters (surfaces need scopes
first); slower than 0006.3.

## 0006.6 · 2026-07-10 · decision — per-PR dispositions and port order

Option 0006.5, rejecting 0006.3 (weaker spine, defects imported) and
0006.4 (real capability discarded). Dispositions, with the evidence that
drove each:

| PR | Verdict | Basis |
|---|---|---|
| #36 disk fixes | **port** | `resolve_volume` whole-disk prefix bug (`disk1` swallows `disk10`s slices — still live at our `scopes/disk.py` whole-disk branch) and identity-keyed `proc_name` cache (our cache never invalidates on pid reuse) are real and absent here; the argtypes hunk duplicates `b3acfe6` |
| #37 tests+CI | **port** | hermetic parse-layer tests (resolve_volume, mount table, fd classify, lsof) + the repo's only CI; drop bare-pid rank_io fixtures (contradict S10 as shipped), convert to package imports |
| #38 shared core | **reject** | reinvention of `core/rusage.py`, strictly weaker (V2 "prefix" habit, raw ticks, bare-pid); only novelty is a dispatcher registry — a taste call we deliberately didn't take |
| #39 agent contract | **port** | versioned JSON envelope, NDJSON, exit codes (0 ok/1 findings/2 usage/3 permission), `--once`/`--duration`, SCHEMA.md — design-compliant (emitted from structures), well-tested, and the top gap in 0005.8; revisit its lsof-failure-exits-0 choice on the way in |
| #40 cpu scope | **reject, harvest** | superseded by PR #51 on every correctness axis (S10 keys, flavor-6 watts, duty%); harvest the `cpu wakeups` mode *split per §4* and its --json wiring |
| #41 memory scope | **port later** | fills a real gap; needs identity-keyed watch, windowed slope (theirs never forgets an early growth spurt), ctypes for vm_stat/sysctl |
| #42 battery scope | **split** | keep `health` + the persisted since-unplug drainers baseline (the series' one S10-correct diff); reject the dimensionally-incoherent energy score in favor of `ri_energy_nj` (0001.11) / §5 pmenergy weights; fix `CurrentCapacity` mAh-vs-% fallback |
| #43 smart scope | **port later** | most salvageable scope — different probe domain, barely touches the spine, correct NVMe constants and pre-failure matrix; swap diskutil regex for `-plist` + plistlib; surface the needs-root smartctl degradation |
| #44 checkup | **port later** | sound composition with per-scope failure isolation; ports only after the scopes it composes exist here |
| #45 MCP server | **port later** | best code in the series — stdlib-only hand-rolled JSON-RPC/stdio, correct MCP semantics, strongest tests; rebind the tool table; promote the underscore document-builders it reaches into |
| #46 docs | **regenerate last** | agent walkthrough salvages nearly verbatim; man page must be regenerated against whatever command set actually ships |
| #47 recording | **reject, harvest** | §6.1 already decided the store (versioned JSONL, bounded reservoirs, context-conditioned keys) — this SQLite table is a weaker parallel schema with top-5 selection bias and no busy_timeout; steal the launchd framing and the self-metering idea |
| #48 TUI shell | **port later** | clean presentation-only skeleton for §8's unified tabs; fix the stale-interval first frame after tab switches |
| #49 anomaly | **reject** | parallel, weaker implementation of §6/§7 (single-sample exceedance, OLS slope, uncalibrated scores, blinding cold-start fix with a latent crash); superseded by the documented engine |

**Port order:** (1) #36 fixes → (2) #37 tests+CI → (3) #39 agent contract
extended to `cpu top`, harvesting #40's wakeup split → (4) scopes
#43, #41, #42-partial → (5) surfaces #48 → #44 → #45 → #46. Rejected PRs
(#38, #40, #47, #49) to be closed with comments crediting the harvested
ideas. Every port lands with its own casebook entry and adversarial
verification (blind-agent tests, per case 0005's method).

## 0006.7 · 2026-07-10 · follow-up — immediate scope: ports 1–3 only

Picking up #36–#39 now (fixes, tests+CI, agent contract), each tested
aggressively and closed out with a blind adversarial re-review; the scope
and surface ports (#41–#46, #48) and the PR-closure round stay open
against this case. ARCHITECTURE §8 to be updated as the agent contract
ships, so the docs and the surface never drift the way #46's man page did.
