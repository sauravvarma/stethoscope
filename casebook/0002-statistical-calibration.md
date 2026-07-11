# Case 0002 — statistical evidence and calibration
status: treated
opened: 2026-07-10
links: case 0001 · ARCHITECTURE.md §6 · Appendix A A1–A4, A12
touches: core/stats.py (future), core/baseline.py (future), core/schema.py (future)

## 0002.1 · 2026-07-09 · hypothesis

Findings can carry a *calibrated posterior probability*: each evidence
clause (exceedance-run probability `p̂^r`, Poisson tail, MK p-value)
converts naturally to a log-likelihood ratio, clauses combine additively
(naive Bayes), and thresholds are set by ARL formulas against a false-alarm
budget. Baselines are compact sketches (count, median, MAD, quantiles) —
raw history is not stored. (ARCHITECTURE.md §6 as first drafted.)

## 0002.2 · 2026-07-10 · failure — p̂^r contradicts the doc's own constraint 3

A1: the run probability `p̂^r` assumes IID samples; constraint 3 of the
same section states the data is bursty and autocorrelated. A legitimate
5-minute compile scores log₁₀ p ≈ −390 — astronomically damning "evidence"
for normal behavior — and that number dominates any additive combination.
The section's own constraint disqualifies the section's own formula.

## 0002.3 · 2026-07-10 · failure — the posterior has no numerator and no prior

A2: an LLR needs P(evidence | culprit); the system is unsupervised — there
are no labeled culprits, ever. Treating −log p as an LLR silently assumes
P(evidence | culprit) ≈ 1, inflating every confidence. No class priors
exist either, and the worked example double-counted one signal (`cpu_pct`
z-score + `cpu_pct` run-length as separate clauses). `confidence: 0.94` was
the vibe the doc claimed to reject.

## 0002.4 · 2026-07-10 · failure — the calibration machinery had no data to run on

A3 + A4: exact median/MAD/quantiles are not incrementally computable — the
"compact sketch" as specified was impossible in stdlib. And ARL formulas
mapping (h, k, L) → false-alarm rate hold only under IID Gaussian
assumptions, which constraint 3 disqualifies; the only real calibration is
replay against recorded normal traces — which sketch-only storage discards.
The budget's unit (per rule? per process? per machine?) was also unstated.

## 0002.5 · 2026-07-10 · option — true Bayesian posteriors via labeled data

**Merit:** would make "posterior probability" literally true.
**Issues:** requires labeled culprit incidents that will never exist for a
single-machine, unsupervised, privacy-respecting tool. Dead on arrival.

## 0002.6 · 2026-07-10 · option — empirical-null calibration (conformal-flavored)

**Merit:** honest and achievable: calibrate the combined score against its
own distribution under recorded *normal* operation — "this combination
occurs under normal behavior less than once per week per machine" is a
claim the data can actually support. **Issues:** requires keeping raw
vitals as a first-class corpus (storage, retention, a background sampler so
the corpus isn't crisis-biased) — a real cost the sketch design was
avoiding.

## 0002.7 · 2026-07-10 · option — rename confidence to an ordinal score

**Merit:** zero machinery; immediately honest. **Issues:** an uncalibrated
ordinal score has no cross-time meaning ("0.7" today vs "0.7" next month),
so `watch`'s severity floor and an agent's trust decision have nothing to
stand on.

## 0002.8 · 2026-07-10 · decision — empirical null + honest naming, on a raw corpus

§6 rewritten: 0002.6 + 0002.7 combined. Raw vitals are stored
(`baseline-raw/1` JSONL, 30-day retention) with per-key ~512-float
reservoirs *derived* from them — exact median/MAD/quantiles over the
reservoir replaces the impossible sketch. Run-lengths use a two-state
Markov model (`p_enter · p_stay^(r−1)`; the worked example recomputes from
10⁻²⁶ to ~10⁻²·²). `confidence` is a false-positive-rate-controlled score
calibrated by replaying detectors against the machine's own recorded
normal, budget stated per machine. Rejected 0002.5 (no labels will ever
exist). The detector core is cut to what this calibration can cover:
exceedance, robust z, Markov run-lengths, CUSUM (chosen over EWMA — it
dates onsets, which findings need), MK + Theil–Sen; Poisson, Spearman, and
Holt deferred with admission criteria (A12).

## 0002.9 · 2026-07-10 · follow-up — what would reopen this

The calibration corpus is only as good as its coverage: if the background
sampler (build-order step 2) slips, baselines revert to crisis-biased
surface-time samples (A5) and the per-week FP claim silently degrades.
Reopen if v0.6 ships detectors before the sampler has ≥ 2 weeks of data.

## 0002.10 · 2026-07-11 · follow-up — cold-start floors and honest ordinal evidence

The first shipped diagnosis pass narrows the claim made in 0002.8: until replay
calibration has enough representative normal traces, `score` and `confidence`
are explicitly ordinal, never probabilities or false-positive-rate claims.
Robust bands use metric-specific absolute and relative floors rather than
discarding every narrow band. In hermetic regression data, five near-identical
6.15 GB memory readings tolerate about 1 MB of noise, while eight zero CPU
readings followed by 40% CPU still classify as critical. Online least-squares
leak evidence is bounded, requires five samples over 30 minutes and at least
1 MiB/min growth, and rejects recent plateaus and frequent drops. This preserves
0002.8's raw-corpus/replay direction while rejecting both the old razor-thin
percentile threshold and the opposite treatment of making all zero baselines
blind.

## 0002.11 · 2026-07-11 · failure — learned pathology disabled the guardrail

Initial implementation switched entirely to a learned process band after five
samples. A process recorded at 100% CPU twenty times could therefore make 100%
look normal and suppress the static 95% critical threshold—the baseline
poisoning failure §6 explicitly forbids. Static CPU and per-counter wakeup
thresholds now remain floors after history matures; learned and static evidence
are evaluated independently and the stronger result wins.

Small samples also no longer let one extreme point dominate p90/p99: before 20
values, robust bands use median/MAD plus absolute/relative floors only.
Quantiles join once the sample is large enough to represent a distribution.
Threshold crossings are inclusive, extreme finite scores clamp before integer
conversion, and the current footprint endpoint participates in leak evidence.
