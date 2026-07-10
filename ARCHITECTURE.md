# stethoscope — energy & CPU diagnosis architecture

*How stethoscope identifies battery drains, finds processes burning cores, and
classifies the culprits — as one layered pipeline whose every stage is reusable
by the CLI, the TUI, alerts, and agent inferencing.*

This document designs the `cpu` (v0.2) and `battery` (v0.3) scopes **and** the
shared diagnosis machinery above them (baselines v0.6, triage/doctor v0.7). It
is written so that each layer can ship independently, in roadmap order, without
rework.

---

## 1. The problem, stated precisely

"My battery is draining" is a *symptom at the system level*. The answer a user
(or agent) actually wants is an *attribution at the process level* plus a
*classification* — not "the battery fell 24% in 70 minutes" but:

> Your webpack dev server has been burning 1.5 cores for 37 hours
> (**runaway-worker**), two Apple sync daemons are stuck in a retry loop
> (**sync-loop**), and a `caffeinate` assertion is preventing sleep
> (**sleep-blocker**).

Getting from symptom to that sentence requires four distinct kinds of work,
and the architecture separates them because different surfaces reuse them at
different depths:

1. **Measurement** — read raw counters from macOS (probes).
2. **Derivation** — turn counters into rates, slopes, and durations with units
   (vitals).
3. **Attribution** — connect system-level drain to per-process energy
   responsibility (the battery scope's core trick).
4. **Anomaly scoring** — statistical, not AI: score each vital against the
   machine's own learned distributions (z-scores, changepoints, drain-slope
   regression) to decide *how abnormal* the numbers are.
5. **Classification** — match the scored evidence against a taxonomy of known
   culprit patterns and emit findings with probabilistic confidence and
   remediation (diagnosis).

Steps 1–5 are deterministic, on-device math — stethoscope finds issues **by
itself, with no AI in the loop**. The AI layer sits *beside* the statistical
one, not above it: an agent consumes the **vitals** (step 2) directly over
`--json`/MCP and reasons however it likes; the findings (step 5) are offered
to it as a head start, not imposed as the answer. Two brains, one data layer:

```
                       ┌── statistical engine ──→ findings ──→ TUI · alerts
probes ──→ vitals ────┤                                        (self-sufficient)
                       └── AI agent (--json/MCP) — reasons over raw vitals,
                           may read findings, may run its own drill-downs
```

## 2. The layer model

```
┌──────────────────────────────────────────────────────────────────────┐
│  SURFACES        CLI · TUI · --json · watch/alerts · MCP (agents)    │
│                  render / notify / expose — never compute            │
├──────────────────────────────────────────────────────────────────────┤
│  DIAGNOSIS       taxonomy + rules  →  Finding{class, evidence, …}    │
│  diagnosis/      cross-scope; consumes anomaly scores + vitals only  │
├──────────────────────────────────────────────────────────────────────┤
│  STATISTICS      anomaly scores, changepoints, trends, forecasts     │
│  core/stats      pure math over vitals + this machine's baselines    │
│  core/baseline   record vitals over time; distribution queries       │
├──────────────────────────────────────────────────────────────────────┤
│  VITALS          per-scope data layers: snapshots → diffs → rates    │
│  scopes/*.py     pure functions returning typed structures           │
├──────────────────────────────────────────────────────────────────────┤
│  PROBES          one macOS introspection surface each, no policy     │
│  core/*          libproc/rusage · pmset · ioreg · powermetrics       │
└──────────────────────────────────────────────────────────────────────┘
```

**The load-bearing rule** (inherited from the disk scope, now made explicit
for every future scope): *a layer may consume any layer **below** it, never
above or beside it — and everything below the surface layer returns
structures, never text.* The diagram above already practices the "any layer
below" form: diagnosis reads scores *and* vitals, and agents read vitals
directly — strict adjacency was never the real rule. The TUI, an alert rule,
and an MCP tool asking "who is burning CPU" all call the same
`scopes/cpu.py` function and get the same structure. Fix a number in one
place, every surface updates.

**Where state lives.** Exactly three modules are stateful: the `Sampler`
(ring buffers of recent intervals), `core/baseline.py` (the on-disk store
and its reservoirs), and `watch`'s incident table (open findings,
hysteresis). Everything else — vitals, scores, rules — is pure functions
over what those three hold.

### Proposed module layout

```
stethoscope              dispatcher (unchanged)
core/
  rusage.py              libproc bindings — extracted from disk.py, upgraded
                         to RUSAGE_INFO_V4 (adds ri_billed_energy, QoS time)
  validate.py            probe-validation harness (§9 step 0): checks every
                         probe contract on the running machine
  sampling.py            Sampler: snapshot/diff/windowed-rate primitives
  schema.py              the typed structures: Sample, Vital, Finding
  baseline.py            record + query "normal" (JSONL under ~/.stethoscope/)
  stats.py               the statistical engine (§6): robust scores, CUSUM,
                         trend tests, forecasts — pure stdlib math
  tui.py                 shared TUI widgets: sparklines, meters, per-core
                         bars, findings strip (extracted disk_tui patterns)
scopes/
  disk.py, disk_tui.py   (existing — disk.py's bindings migrate to core/)
  cpu.py, cpu_tui.py     cpu scope: data layer + CLI, and its live TUI
  battery.py, battery_tui.py   battery scope: same pairing
diagnosis/
  taxonomy.py            the culprit classes (§7) as data, not code
  rules.py               classifiers: anomaly scores + vitals → Finding list
```

Every scope keeps the disk scope's pairing — `<scope>.py` data layer,
`<scope>_tui.py` full-screen live view — and a unified `stethoscope tui`
composes all scopes as tabs (§8).

`core/` is the price of the second scope: `disk.py` currently owns the
`proc_pid_rusage` bindings, and `cpu`/`battery` need the same struct read to
more fields. Extract once, before writing `cpu.py`.

---

## 3. Probes — the macOS surfaces we build on

Same selection philosophy as the disk scope: **SIP-safe, ships with the OS,
cheap enough to poll.** Heavier tools are reserved for the `inspect` tier.

| Probe | Surface | What it yields | Cost / privilege |
|---|---|---|---|
| `rusage` | `proc_pid_rusage()` `RUSAGE_INFO_V4` | per-pid cumulative: user/system CPU time, **pkg-idle & interrupt wakeups** (distinct vitals, §4), QoS-tier CPU time, disk bytes, footprint. Time fields are **mach-abstime ticks** — converted via `mach_timebase_info` in `core/rusage.py`, never exposed raw. `ri_billed_energy` (nJ) is lifetime-cumulative with a **lazy ledger** — deltas ≈ 0 at 1 s cadence even for a full-core burn — so it is a cross-check tier, not a polling vital. Flavor 6 (`RUSAGE_INFO_V6`, where the OS has it) adds `ri_energy_nj` / `ri_penergy_nj`, a ledger that **does** move at 1 s cadence (10/10 nonzero deltas under burn, casebook 0001.10) — the live per-process watts source; absent flavor 6 the value is None, never zero | free; other users' pids need root (same rule as disk) |
| `battery` | `ioreg -rn AppleSmartBattery` | `Voltage`, `InstantAmperage` (signed, two's-complement-decoded) → **battery flow in watts**; charge %, cycle count, health. Absent on desktops | free, no root (~14 ms) |
| `power log` | `pmset -g log` | historical: AC↔battery transitions, charge % at each event, sleep/wake/DarkWake causes | **~1.9 s / ~50 k lines per call** — parse once at startup, then tail; the live drain slope comes from polling `ioreg` `CurrentCapacity` instead |
| `assertions` | `pmset -g assertions` | who is preventing sleep, and their stated reason | free, no root (~12 ms) |
| `powermetrics` | `powermetrics --samplers tasks` | per-process energy impact as macOS itself scores it, P/E-core residency, per-process wakeup detail | **root**, heavy — `inspect` tier only |
| `pmenergy` | `/usr/share/pmenergy/*.plist` | Apple's per-board coefficients for the Energy Impact formula (§5) — **unitless weights**, keyed by Intel board-id; Apple Silicon falls back to `default.plist` | free, read once |

Why these and not others: coalition/task-level APIs (`coalinfo`) are private
and unstable; DTrace is SIP-blocked (same reasoning as the disk scope). The
`rusage` struct is the spine again — the *same* syscall the disk scope already
polls, read to a deeper struct version. One probe, three scopes.

## 4. Vitals — the derived metrics with units

A **Sample** is one poll of a probe (cumulative counters, timestamped). A
**Vital** is what you get from two samples: a rate or state with a unit. The
`Sampler` in `core/sampling.py` generalizes what `disk.py`'s snapshot-and-diff
loop does today, plus one new capability the disk scope didn't need:
**windows** — a ring buffer of the last N intervals, because "burning a core"
is a claim about *sustained* behavior, not one hot interval.

### The `cpu` scope's vitals

| Vital | Derivation | Why it matters |
|---|---|---|
| `cpu_pct` | Δ(user+system time) / interval | the obvious one |
| `duty_cycle` | fraction of window intervals with cpu_pct > 50 | separates *sustained* burn from bursts — a compile spikes, a runaway holds |
| `lifetime_duty` | total CPU time / process **awake-age** | the long-memory version: 26 h CPU over 37 awake-hours = 71%, damning regardless of the current instant. Awake-age deliberately: `mach_absolute_time` does not advance during machine sleep, so age derived from `ri_proc_start_abstime` excludes sleep — the honest denominator for a duty claim (casebook 0003.7) |
| `pkg_idle_wakeups_per_s` | Δri_pkg_idle_wkups / interval | **the hidden battery killer** — each pkg-idle wakeup drags the whole package out of its idle state; this is Activity Monitor's "Idle Wake Ups" column |
| `interrupt_wakeups_per_s` | Δri_interrupt_wkups / interval | the other wakeup shape — orders of magnitude noisier in absolute terms, so never summed with pkg-idle, but load-bearing: a canonical 1 ms sleep-loop storm measures **zero** pkg-idle wakeups while holding ~800 interrupt wakeups/s against a ~1/s quiet baseline (casebook 0004). `wakeup-storm` alarms on **either** counter far above its *own baseline* — an absolute threshold on interrupt wakeups would over-alarm; a pkg-idle-only alarm is blind to sleep-loop storms |
| `qos_mix` | share of CPU time per QoS tier (v4 fields) | background-QoS work rides efficiency cores (cheap); user-interactive burn rides P-cores (expensive). Caveat: QoS is *scheduler intent*, not core residency — processes that never adopt QoS show a degenerate all-`legacy` mix; true P/E residency stays in the `powermetrics` inspect tier |

All time-derived vitals consume converted seconds from `core/rusage.py`,
never raw ticks — the rusage time fields are mach-abstime ticks, a 41.7×
error on Apple Silicon if read as nanoseconds.

### The `battery` scope's vitals

Two levels, deliberately separate — **system truth** and **process
attribution** — because the first is exact and the second is a model, and the
diagnosis layer must know which is which:

**System truth** (from `battery` + `power log` probes):
- `watts_now` — Voltage × InstantAmperage, with `InstantAmperage` decoded as
  **signed** two's-complement (ioreg renders it unsigned). This is **battery
  flow**, signed: it equals system draw *only while discharging*; on AC it
  shows charging power, ~0 W once topped off. Desktops have no
  `AppleSmartBattery` node at all — `battery status` degrades to "no battery".
- `drain_slope` — %/hour over recent history, **segmented by power state**
  (awake-active / awake-idle / asleep) from the pmset log. A 20%/h slope while
  *asleep* and while *active* are entirely different diagnoses.
- `sleep_integrity` — held assertions (name, owner, age) + DarkWake frequency
  during the last sleep period. "Draining because it never actually sleeps"
  is a common answer with zero per-process CPU signature.

**Process attribution** (from `rusage`):
- `energy_rate` — Δ`ri_energy_nj` / interval, **real watts at polling
  cadence** where rusage flavor 6 exists (measured: 10/10 nonzero 1 s
  deltas under burn — casebook 0001.10/0001.11). The primary live
  per-process energy vital on such systems, already rendered by `cpu top`'s
  POWER column; where flavor 6 is absent it is None and surfaces render
  the absence, never a fabricated zero.
- `energy_score` — the per-process estimate for systems *without* flavor 6,
  and **explicitly unitless**: Apple's own Energy-Impact formula — CPU
  seconds and pkg-idle wakeups weighted by the coefficients in the
  applicable `pmenergy` plist (`default.plist` on Apple Silicon; the
  `Mac-*` plists are Intel board-ids). This is what Activity Monitor's
  Energy pane computes; it ranks processes and feeds baselines, but it is
  **never rendered as watts** — the coefficients are dimensionless weights.
  P/E-core *residency* detail stays in the `powermetrics` inspect tier
  (root), though flavor 6's `ri_penergy_nj` gives the P-core energy share
  for free.
- Δ`ri_billed_energy` — the kernel's actually-billed nanojoules — is a
  **slow cross-check**, not a polling vital: the ledger updates lazily, so
  deltas are zero at 1 s cadence even for a full-core burn (measured on this
  hardware — 0/10 nonzero deltas over 10 s under sustained burn). Compared
  over minutes-to-hours windows it validates the modeled ranking; at any
  shorter window it is flagged unavailable-at-cadence.
- `energy_share` — a process's `energy_score` over the sum across processes:
  shares of the modeled score, labeled as such.

### Context vitals

The taxonomy's signatures (§7) turn on context — "user-launched", "machine
otherwise idle", "how old is this process" — so those are vitals too, defined
here like everything else, not smuggled in by the rules:

| Vital | Probe / derivation | Notes |
|---|---|---|
| `frontmost_app` | `lsappinfo front` | one cheap exec (~10 ms); chosen over `osascript`/NSWorkspace, which pays AppleScript startup per call |
| `user_idle_s` | `ioreg -c IOHIDSystem` → `HIDIdleTime` | ns since last HID input; "otherwise idle" = this, not CPU load |
| `proc_start_time` | `ri_proc_start_abstime` (rusage V4, converted) | process age feeds `lifetime_duty` and pid-reuse identity |
| `provenance` | executable path + launchd domain — **a heuristic, stated as one** | under `/System` or `/usr` + system domain → system daemon; under `/Applications` or `$HOME` → user-launched |

### Vitals are the reuse boundary

Everything above this layer — TUI columns, alert thresholds, agent JSON,
classifier inputs — consumes vitals by name. A vital's definition (name, unit,
derivation, scope) lives in `core/schema.py` and is **the** stable contract
that v0.5's public schema promises. Surfaces never re-derive.

---

## 5. Attribution — connecting the two ends

The battery scope's core question is: *does the per-process story explain the
system-level drain?* Watts and the modeled score are different currencies, so
`battery top` has two modes and never mixes them:

**Default mode — the unitless ranking.** The attribution table shows each
process's `energy_score` and `energy_share`, labeled explicitly as *shares of
modeled score, not watts*. This works everywhere, at every cadence, on every
power state.

**Reconciliation mode — gated.** The watts-reconciliation table renders only
when (a) the machine is **discharging** (so `watts_now` *is* system draw) and
(b) a watts-true attribution source exists — a `powermetrics` snapshot (root,
inspect tier). Only then are the two ends comparable:

```
observed draw          14.2 W   (ioreg: battery flow, discharging)
attributed             9.8 W   (powermetrics snapshot, per-process)
  node (93467)          6.1 W   43%
  peopled (1504)        2.2 W   15%
  CallHistorySyncHelper 1.4 W   10%
unattributed            4.4 W   (display, GPU, radios — see `battery inspect`)
```

The unattributed residual is a feature, not an error: it keeps the model
honest and tells the diagnosis layer when the culprit *isn't a process* (e.g.
display at full brightness, or a discrete-GPU-class draw), routing to a
different finding class instead of falsely blaming the top CPU consumer. It
is only computable in the gated mode — a residual between watts and a
unitless score would be meaningless, so the default mode does not pretend to
one.

## 6. The statistical engine — anomalies and forecasting without AI

This is the layer that makes stethoscope diagnostic on its own. Every method
below is chosen against five hard constraints:

1. **stdlib-only** — no numpy/scipy; each detector is a few dozen lines of
   pure Python and fast at our data sizes (windows of 10²–10³ samples, a few
   hundred processes) — which is exactly why the detector core is kept small
   (see the deferred list in §6.3): the engine's total size is bounded by
   admitting methods one at a time, each against a stated criterion.
2. **Unsupervised** — there are no labels; "abnormal" can only mean "unlike
   this machine's own history."
3. **Heavy-tailed, spiky data** — CPU% and wakeup distributions have outliers
   as their *normal state* (every compile is a spike). Methods assuming
   Gaussian data on the raw series are disqualified up front.
4. **Cheap enough for the TUI refresh loop** — scores update at 1 s cadence.
5. **Explainable** — every score must reduce to a number a human can verify
   ("14× its usual level, sustained for 11 minutes, since 14:32"), because
   findings carry their evidence (§7).

### 6.1 Baselines — learning "normal for this machine"

**Storage.** Raw vitals are recorded as versioned JSONL
(`{"schema": "baseline-raw/1"}`), daily files under
`~/.stethoscope/baseline/`, retained 30 days. From the raw stream,
`core/baseline.py` maintains per-key **bounded reservoirs** — ~512 floats per
(process, vital, context) key — over which median, MAD, and quantiles are
computed *exactly*; sketches are always derived and recomputable from raw,
never the other way around. The arithmetic is small: 512 floats × even
50 k keys ≈ 200 MB worst-case in memory, and in practice a few thousand live
keys ≈ a few MB; raw JSONL at 60 s cadence is tens of MB/day recorded naively
— order a GB across the retention window — so the sampler records a process
only when any of its vitals clears a small activity floor, which keeps the
window to hundreds of MB. Raw history is first-class because it is the
**calibration corpus**: detector thresholds are tuned by *replaying* the
detectors against recorded normal traces, not by average-run-length formulas
(which assume IID Gaussian data — disqualified by constraint 3). Rates are
stored as `log1p`; hour buckets are keyed to local time with the timezone
recorded per sample.

**Recording.** A minimal background sampler — a launchd agent polling at
~60 s cadence — ships *with* the baseline milestone, because recording only
when a surface runs would over-represent crisis periods: users open a health
tool when the machine misbehaves. Single-writer rule: the sampler owns the
store; interactive surfaces enqueue samples to it or skip recording — they
never append concurrently. Under sudo the store must not fork
(plain `sudo` would write to `/var/root`; `sudo -E` would leave root-owned
files that break later unprivileged appends): when euid == 0, resolve
`SUDO_USER`'s home and `chown` new files to `SUDO_UID`/`SUDO_GID`.

**Keying** is what makes the store useful:

- by **normalized process name**, not pid — strip versioned directories from
  the path, collapse helper suffixes (`Foo Helper (Renderer)` → `Foo Helper`),
  so `peopled` today is comparable to `peopled` last month and `node` doesn't
  splinter per install path (though `node` stays multimodal by nature — every
  LSP and build daemon shares the name; the reservoir absorbs that);
- **conditioned on context** — power state (AC/battery, active/idle), a
  coarse hour-of-day bucket, **and the privilege level of the sample** —
  root sees ~300 more pids than a user does, and mixing the two skews every
  population denominator;
- sample identity inside a diff loop is **(pid, start_abstime)**, never bare
  pid — pid reuse would otherwise splice two processes' counters.

Conditioning multiplies key cardinality, so lookups back off hierarchically
with min-count gates: full key → drop the hour bucket → drop power state →
population prior. A key's quantiles are not consulted until it holds ≥ 50
samples; below that, the next level up answers.

**Baselines must defend themselves against poisoning.** A baseline learned
naively certifies any pathology older than its own memory — a daemon that has
been spinning for 60 days *is* its own baseline. The defenses, with their
cold-start order of authority:

- **Static priors as the sole early authority** — machine-independent truths
  ("no system daemon idles at 60% CPU") that need no learning. For a key's
  first 7 days *only they* judge it: a first-week pathology can neither hide
  from them nor certify itself as baseline. They remain the floor and the
  backstop afterwards.
- **Quarantined ingestion** — once a key has a mature baseline, samples
  flagged anomalous by any detector do not update it. Quarantine never
  applies before maturity (that would be circular: nothing recorded, nothing
  learned).
- **Deliberate admission** — a genuine new normal is admitted by an explicit
  command, `stethoscope baseline accept <name>`, never by erosion.
- **Two-speed memory** — per key, a fast reservoir over the last 24 h and a
  slow one over the last 30 d. The divergence check — fast median outside
  the slow reservoir's p5–p95 band — is itself a drift signal, and a culprit
  can't easily outwait the slow window.

### 6.2 Anomaly detection

**Detector #0 — baseline exceedance.** The baseline is not just substrate for
the fancier scores; comparing against it is the first and most explainable
detector in its own right, in three forms: **quantile exceedance** (above its
own p95/p99 for this context), **novelty** (a process name with *no* baseline
drawing meaningful energy — new login item, freshly installed tool), and
**population priors** (a process exceeding what its *class* ever does, per
the static floors above). Its honest limits define the rest of this section:
p95 exceedance fires 5% of the time *by construction* — ~15 false "anomalies"
per second across ~300 processes at 1 s cadence — so it only becomes a
usable alarm when multiplied by persistence (run-lengths, below); it is
structurally blind to pathologies older than its memory (hence the poisoning
defenses above); and it detects *displacement*, not *direction*, so slow
trends reach it late (hence the trend tests in §6.3).

**Robust deviation — median/MAD z-score.** The workhorse. For vital x against
its baseline key: `z = (x − median) / (1.4826·MAD)`. Median and MAD have a
50% breakdown point — half the history can be garbage (spikes, one-off
migrations) before the score degrades, which is exactly the tolerance
heavy-tailed vitals need. A mean/σ z-score fails constraint 3: one long
compile in the history inflates σ and hides the real anomaly forever after.

**Onset detection — CUSUM changepoint.** Answering *when* it went wrong is
half the diagnosis ("burning since 14:32" immediately suggests "what did you
start at 14:32?"). Page's cumulative sum, `S⁺_t = max(0, S⁺_{t−1} + (x_t −
μ₀ − k))`, alarms when `S⁺ > h` and — by backtracking to where the sum last
touched zero — dates the shift. Findings carry that timestamp. Parameters
come from the baseline, not taste: `μ₀` = the key's median from slow memory,
`k` = ½ · (1.4826·MAD), and `h` is tuned by replaying against the
calibration corpus (§6.1). CUSUM also covers the slow-ramp case an EWMA
chart would otherwise own — small persistent shifts accumulate in `S⁺` —
which is why EWMA is deferred (below): CUSUM dates onsets, which findings
need; EWMA doesn't. The Page–Hinkley variant runs on the battery drain slope
to date drain-regime changes the same way.

**Sustained burn — Markov run-lengths.** "Burning a core" is a claim about
persistence, so we make it probabilistic instead of eyeballing a duty cycle —
but honestly: the samples are autocorrelated (constraint 3 says so), and an
IID model (`p̂^r`) would score a legitimate 5-minute compile as
astronomically damning evidence. Instead, per key, a two-state Markov model
estimated from the reservoir's transition counts: `p_enter` (probability an
interval below the threshold is followed by one above) and `p_stay`
(above followed by above). The probability of a run of `r` consecutive
exceedances is `p_enter · p_stay^(r−1)`, and that log-probability is the
evidence weight in §6.4. Worked example: a process hot 5% of the time whose
hot intervals cluster (`p_enter` ≈ 0.05, `p_stay` ≈ 0.9 from its own
transitions) sustains a 20-interval run with probability
0.05 · 0.9¹⁹ ≈ 0.007 (log₁₀ ≈ −2.2) — suspicious, not the absurd
0.05²⁰ ≈ 10⁻²⁶ the IID model would claim for behavior the process exhibits
every workday.

### 6.3 Trend and forecasting

**Monotonic trend — Mann–Kendall test + Theil–Sen slope.** The nonparametric
pair for "is this genuinely growing, and how fast": Mann–Kendall gives a
p-value for monotonic trend using only sign comparisons (immune to outliers
and to any distributional assumption); Theil–Sen estimates the slope as the
median of all pairwise slopes (O(n²), trivial at window sizes). Uses:
memory-leak candidates (monotonic footprint growth), drain-slope
acceleration, disk-fill trends.

**Time-to-threshold forecasts.** The user-facing payoff: *"battery empty in
1:10–1:45 at current behavior"*, *"disk full in ~9 days"*, *"this leak hits
memory pressure tonight"*. Method: Theil–Sen slope over the state-segmented
series (battery-on-active and battery-on-idle forecast separately — mixing
them is how macOS's own estimate gets whipsawed), extrapolated to the
threshold, with a **prediction interval from empirical residual quantiles**
rather than a Gaussian formula — no distributional assumption, and honest
intervals are the point. Always reported as a range, never a point estimate.

### Deferred detectors

The v0.6 detector core is deliberately small — baseline exceedance (#0),
robust z, Markov run-lengths, CUSUM, Mann–Kendall + Theil–Sen, and the
time-to-threshold forecasts — because each of these earns its keep against a
taxonomy signature and can be calibrated by replay. Everything else waits,
with its admission criterion stated:

- **EWMA control chart** — admitted only if replay shows CUSUM missing slow
  ramps that matter; its small-shift role is otherwise covered by CUSUM
  accumulation and the MK trend test.
- **Poisson tails for wakeups** — admitted only if recorded wakeup data for
  a key is *not* overdispersed (variance ≈ mean); real wakeup counts usually
  are, and the robust z on log-counts already handles them.
- **Spearman co-movement** — admitted for merging daemon-pair findings only
  with the confounder guard: the *pair* must correlate while the anomalous
  *population* doesn't (thermal events and backups correlate everything),
  and zero-tie windows are excluded.
- **Holt double-exponential smoothing** — admitted if the TUI's live
  forecast line proves too laggy on regime changes (brightness change, a
  rebuild finishing); until then the Theil–Sen forecast is the only one, and
  a Theil–Sen/recent-window disagreement is surfaced as "regime changed"
  rather than averaged.

### 6.4 Evidence combination — from scores to confidence

A finding's `confidence` is a **false-positive-rate-controlled score, not a
matched-clause count** — and not a posterior probability, which unsupervised
data cannot earn (there is no P(evidence | culprit) to invert). Each evidence
clause contributes a log-weight — the Markov run log-probability, the MK
p-value, the robust-z tail all convert naturally — and clauses combine
additively in log-space. The combined score is then calibrated
**empirically against this machine's own null**: its distribution under
recorded normal operation (the calibration corpus, §6.1). What `confidence`
reports is exactly that: "this evidence combination occurs under normal
behavior less than once per week" — the false-alarm budget, whose unit is
**per machine**. That budget, not taste, is what tunes `h` above and the
clause-combination threshold here.

Partially-supported hypotheses ship as low-confidence findings rather than
being suppressed — the TUI dims them, `watch` ignores them below its
severity floor, and an agent can treat them as leads worth verifying.

## 7. Diagnosis — the culprit taxonomy

Classification is **rule-based and evidence-carrying**: a rule is a culprit
class's signature written in terms of §6's scores (robust z, run-length
probability, changepoint, trend), and every finding cites the scored numbers
that triggered it — so a human can verify it in the TUI and an agent can
re-check each piece of evidence independently. Rules read vitals, scores, and
baselines; they never touch probes.

### The taxonomy (`diagnosis/taxonomy.py`)

Each class below is defined by its **evidence signature** — the combination of
vitals that distinguishes it. These eight cover the culprits that actually
occur; the set is data-driven and extensible.

| Class | Evidence signature | Example (from a real diagnosis) |
|---|---|---|
| `runaway-worker` | user-launched process · `duty_cycle` ≈ 1 sustained · high `lifetime_duty` · continues while machine is otherwise idle | webpack dev server at 149% CPU, 26 h CPU over 37 h alive |
| `sync-loop` | system daemon (`provenance`) · sustained CPU **and** elevated wakeups (either counter vs baseline) · low `diskio` rate from the disk scope's vitals (spinning, not syncing — a cross-scope join) · often a co-moving daemon *pair* · far above its own baseline | `peopled` + `CallHistorySyncHelper` at 61%/44%, 128 h CPU combined |
| `wakeup-storm` | low `cpu_pct` but **either** wakeup vital ≫ its own baseline (order 10²–10³ over) — sleep-loop storms live entirely in `interrupt_wakeups_per_s` (casebook 0004) | a chat app polling timers at 2% CPU, 3000 idle wakeups/s; a 1 ms sleep loop at 0.3% CPU, ~800 interrupt wakeups/s |
| `sleep-blocker` | holds a `PreventUserIdleSystemSleep`/`PreventSystemSleep` assertion beyond an expected duration | forgotten `caffeinate`; a stuck Time Machine |
| `wake-storm` | drain during *sleep* segments + frequent DarkWakes with a repeating wake reason in the pmset log | network peripheral re-waking the machine all night |
| `io-churner` | energy driven by sustained disk rate rather than CPU (cross-scope: joins the **disk** scope's vitals) | runaway log writer, thrashing indexer |
| `foreground-load` | high energy but frontmost/user-active app doing plausible work | a video export; a game — reported, ranked, **not alarmed** |
| `unattributed-draw` | large reconciliation residual (§5) with no process signature | display brightness, GPU, hotspot radio |

Two classes deserve a note on *why* they're separable:

- **`runaway-worker` vs `foreground-load`** differ only in *context* — the
  same 150% CPU is fine in an active compile and damning at 3 a.m. with the
  lid open and no user input. That's why user-activity state (from the
  `assertions` probe's `UserIsActive` and the pmset log) is an input to
  classification, not just decoration.
- **`sync-loop` vs `runaway-worker`** differ in *who owns the fix*: killing a
  relaunching daemon pair is the remedy for one, killing (or fixing the watch
  config of) your own tool for the other. Classification exists precisely to
  make the remediation specific.

### The Finding structure (`core/schema.py`)

```python
Finding = {
  "finding_id":  "sync-loop:CallHistorySyncHelper+peopled:2026-07-09T06",
                                      # deterministic: class + normalized name-set + onset bucket —
                                      # what watch's dedupe and hysteresis key on
  "class":       "sync-loop",
  "severity":    "warning",          # healthy | warning | critical (design language tiers)
  "state":       "open",             # open | resolved | recurred (lifecycle, below)
  "subject":     {"names": ["peopled", "CallHistorySyncHelper"],   # the stable identity
                  "pids": [1504, 997]},                            # volatile detail — daemons respawn
  "evidence": [                       # every clause a rule matched, with the scored numbers
    {"vital": "cpu_pct", "value": 61.3, "robust_z": 14.2, "baseline_median": 0.4,
     "run_length_s": 660, "markov_run_log10": -31.5, "p_enter": 0.05, "p_stay": 0.9},
    {"vital": "pkg_idle_wakeups_per_s", "value": 810, "robust_z": 9.8, "baseline_median": 22},
    {"pair": ["peopled", "CallHistorySyncHelper"], "onset_gap_s": 14},   # CUSUM onsets agree
  ],
  "onset":       "2026-07-09T06:54:11+05:30",   # CUSUM changepoint — when it went rogue
  "confidence":  0.94,                # FP-rate-calibrated score (§6.4): this evidence combination
                                      # occurs under normal operation less than once per week
  "explanation": "Contacts/call-history sync daemons co-spinning far above baseline",
  "verify":      "stethoscope cpu inspect 1504",     # drill-down to confirm
  "remediation": {"action": "killall peopled CallHistorySyncHelper",
                  "reversible": true, "note": "launchd respawns them; breaks the retry loop"},
}
```

The lifecycle: a finding opens when its rule first fires, **resolves** when
its combined score stays below the exit threshold for M consecutive
intervals (exit lower than entry — hysteresis lives here, not in `watch`),
and **recurs** — same `finding_id` reactivated, not a new incident — if it
fires again within the dedupe horizon. `subject` is keyed by the normalized
name-set precisely because pids go stale the moment launchd respawns a
daemon; pids are carried as the drill-down detail of the current episode.

Every surface consumes this one structure: the TUI renders `severity` with the
design-language color tiers and shows `evidence` in a popup; `watch` decides
whether to notify on it; an agent reads `verify` to know the exact next probe
call, and `remediation.reversible` to know what it may do autonomously versus
what needs the human. **`confidence` is honest**: it is the §6.4
FP-rate-controlled score — a claim about how rarely normal operation produces
this evidence, not a posterior — and weakly-supported hypotheses ship as
low-confidence findings rather than being suppressed — an agent can gather
the missing evidence; a threshold-only alerter can't.

## 8. Surfaces — one pipeline, four consumers

### CLI (per scope, mirrors the disk scope's who/why grammar)

```
stethoscope cpu top                  WHO is burning cores (cpu_pct, duty, wakeups)
stethoscope cpu inspect <pid>        WHY — thread sample + powermetrics detail (sudo)
stethoscope battery status           system truth: watts, slope by segment, health
stethoscope battery top              WHO — attribution ranking; watts reconciliation when gated (§5)
stethoscope battery blockers         sleep assertions + DarkWake history
stethoscope triage                   cross-scope: run the classifier, print findings
```

`triage` is the flagship reuse proof: it is *nothing but* the pipeline —
sample all scopes' vitals, consult baselines, run `diagnosis/rules.py`, render
findings. The manual investigation that motivated this document (`top` →
`ps` → `pmset -g log` → `sample` → verdict) becomes one command.

### TUI — every scope gets one, and they compose

The disk scope's pairing is the law for all scopes: `cpu tui` and
`battery tui` ship *with* their scopes, full-screen live views over the same
data layers — and a unified `stethoscope tui` composes every scope as tabs in
row 0 (`[1]disk [2]cpu [3]battery …`), resolving DESIGN.md open question 4.

The ambition is `top`/`btop` class, but **diagnosis-aware** — btop shows you
gauges and leaves the verdict to you; stethoscope shows gauges *and* the
verdict, with the evidence one keypress away:

- **Sparklines everywhere** (DESIGN.md open question 5): the `Sampler`'s ring
  buffers are already the last N intervals, so per-row trend cues (CPU, watts,
  wakeups) are a rendering problem, not a data problem. `core/tui.py` owns
  the widgets — block-glyph sparklines, meters, per-core bars — shared by all
  scope TUIs.
- **cpu tui**: per-core utilization bars in the status sub-bar; table columns
  are the §4 vitals (cpu%, duty, pkg-idle wakeups/s, QoS mix) each with a
  sparkline; sort by any vital; `i` inspect drops to the
  powermetrics/thread-sample view like disk's `fs_usage` pattern.
- **battery tui**: status sub-bar is system truth — battery flow, drain
  slope, the Theil–Sen forecast as a live time-to-empty *range*
  (`1:10–1:45`); the table is the attribution view (§5) — the unitless
  ranking by default, the watts reconciliation with its unattributed-draw
  row when the gate (discharging + powermetrics snapshot) is met; a second
  tab lists sleep blockers and DarkWake history.
- **Findings strip**: one dedicated line under the status sub-bar showing
  active findings (`▲ sync-loop peopled+CallHistorySyncHelper · since 06:54 ·
  94%`), rendered in severity colors — red's debut (DESIGN.md open question
  3) is a `critical` finding. `Enter` on a finding opens the evidence popup:
  the scored clauses, the onset time, the remediation with its
  reversibility. `x` kill keeps the inline `[y/N]` confirmation; irreversible
  remediations escalate per the design language.

### Alerts (`stethoscope watch`)

A long-running loop — the same pipeline as `triage` on a cadence, plus state:
**dedupe** (a finding fires once per subject per incident, not per interval),
**hysteresis** (entry/exit thresholds differ, so flapping doesn't spam), and
**sinks** (macOS notification via `osascript`, stdout log line, webhook).
Alert rules contain *no thresholds of their own* — they are just "notify on
findings ≥ severity X", because thresholds live in diagnosis where all
surfaces share them.

### Agents (`--json`, then MCP)

Every command above takes `--json --once` and emits schema-versioned
structures (`{"schema": "stethoscope/1", ...}`). The MCP server (v1.0) maps
tools 1:1 onto the same calls. The agent's primary input is the **vitals** —
raw structured measurements it reasons over with its own judgment, exactly as
a human expert would read `top` output. The statistical engine's findings are
offered as a *head start*, not an answer key: an agent can accept a
high-confidence finding and jump straight to its `verify` command, or ignore
the findings entirely and correlate vitals across scopes itself (the doc's
opening example — rising energy *and* a wakeup storm *and* a sleep assertion
— is exactly such a cross-scope join). Findings are designed to survive that
scrutiny: evidence clauses are re-checkable numbers, `onset` is a testable
claim, remediation is marked reversible-or-not, confidence is calibrated
against this machine's own recorded null rather than a vibe.

## 9. Build order

Dependency-ordered, matching the roadmap milestones:

0. **Probe validation** — `python3 -m core.validate` on the target hardware.
   §3's table is only trusted after it passes; it has already run once
   (Appendix A's S-series was found and confirmed this way), and it runs
   again on every new machine or macOS version before anything builds on
   the probes.
1. **Extract `core/`** — move `disk.py`'s bindings to `core/rusage.py`,
   upgrade to `RUSAGE_INFO_V4`: full-struct declaration, sizeof-asserted
   (S9); timebase conversion baked in (S2). The storage schema
   `baseline-raw/1` is decided here, before anything records. Add
   `core/sampling.py` + `core/schema.py`; pull the reusable TUI patterns out
   of `disk_tui.py` into `core/tui.py`. Regression gate: `disk top` output
   unchanged.
2. **`cpu` scope (v0.2)** — vitals, `top`/`inspect`, `cpu_tui.py`. The
   minimal background sampler (§6.1) ships here too, so recorded history is
   crisis-unbiased and exists long before the statistics need it.
3. **`battery` scope (v0.3)** — system-truth vitals first (`status`,
   `blockers`: exact data, no model), then attribution (`top`) with the
   dual direct/modeled strategy, then `battery_tui.py`.
4. **`core/stats.py` (v0.6, first half)** — the §6 engine, testable in pure
   isolation against synthetic series (a leak, a step change, a wakeup
   storm) before any rule consumes it. Forecasts land in the TUIs here.
5. **`diagnosis/` + `triage` (v0.6, second half)** — taxonomy, rules,
   findings; findings strip + evidence popups in the TUIs; red debuts.
6. **`watch` + unified `stethoscope tui` + MCP (v0.7/v1.0)** — thin
   consumers of a pipeline that, by then, already works.

## 10. Design invariants (the checklist for every future PR)

- A layer consumes only layers below it, never above or beside; structures,
  never text, below the surface layer.
- Every derived number is a named vital with a unit, defined once.
- Every finding carries its evidence; no verdicts without numbers.
- Models (energy attribution) are labeled as models and reconciled against
  ground truth; residuals are reported, not hidden.
- Probes are SIP-safe and OS-native; anything needing root is an `inspect`
  tier, never the default path.
- Thresholds prefer the machine's own baseline; static priors are the floor,
  the cold-start fallback, and the poisoning backstop.
- Raw vitals are retained (versioned JSONL) as the calibration corpus;
  sketches are derived and recomputable, never the only copy.
- Anomalous samples never update baselines silently (quarantined ingestion);
  a new normal is admitted deliberately, not by erosion.
- Statistical methods are robust and nonparametric by default (median/MAD,
  rank, sign) — Gaussian assumptions on raw vitals are disqualified.
- Detector thresholds are set by a false-alarm budget — per machine,
  calibrated by replay against the recorded corpus — not by taste.
- Forecasts are ranges with empirical intervals, never point estimates.
- The pipeline diagnoses with no AI in the loop; agents consume vitals (and
  optionally findings) — they are a consumer, never a dependency.
- No third-party dependencies — system Python 3 only.

---

## Appendix A — Adversarial review (2026-07-10)

Two independent adversarial reviews of this document: one attacking the macOS
probe contract (findings **S1–S12**, empirically tested on an M5 Pro /
`Mac17,9`, macOS 26.4.1, arm64 — via live ctypes probes, `ioreg`, `pmset`),
one attacking the statistical engine and layering (findings **A1–A15**,
verified against `scopes/disk.py` / `disk_tui.py`). Findings marked
**[verified]** were reproduced on real hardware or in the shipped code;
**[analysis]** are certain from the text; **[judgment]** are contestable.
Sections above are being revised against this list; each finding's
disposition is tracked here.

### Systems / probe-contract findings

| # | Severity | Finding | Status |
|---|---|---|---|
| S1 | blocker [verified] | `Δri_billed_energy` is frozen at polling timescales — deltas are **zero at 1 s intervals even for processes burning full cores** (kernel folds energy into the ledger lazily). "Preferred whenever nonzero" selects a nonzero, non-moving source; `energy_rate` reads 0 W for exactly the runaways the scope exists to catch. | §4 rewritten: modeled score primary, billed_energy demoted to lifetime cross-check |
| S2 | blocker [verified] | rusage time fields (`ri_user_time`, `ri_system_time`, QoS times, `ri_proc_start_abstime`) are **mach-abstime ticks, not ns** — 41.7× error on Apple Silicon (timebase 125/3; Intel is 1/1, so the bug passes tests on the wrong machine). A 1.000 s CPU burn reads as 0.024 s unconverted. | `core/rusage.py` bakes in `mach_timebase_info` conversion; §3 annotated |
| S3 | blocker [verified] | The modeled energy fallback cannot produce watts: `pmenergy` coefficients are dimensionless; every `Mac-*.plist` is keyed by **Intel board-id** (Apple Silicon gets `default.plist` only); formula needs network/GPU inputs rusage lacks. §5's "attributed 9.8 W" on the fallback path is watts-minus-unitless. | §4/§5 rewritten: modeled path is a unitless ranking score, never watts |
| S4 | blocker [verified] | `watts_now` = V × \|I\| measures **battery flow, not system draw** — on AC it shows charging power, ~0 W when topped off. ioreg renders signed fields as unsigned 64-bit (verified `18446744073709540666`). Desktops have no `AppleSmartBattery` node. | §4/§5: reconciliation gated on discharge state; signed parsing specified |
| S5 | risk [verified cost] | `pmset -g log` costs **1.8 s CPU / 50 k lines per call** — polled near a refresh loop, stethoscope tops its own table. Format is version-fragile prose. | §3 annotated: parse once + tail; slope from `ioreg CurrentCapacity` |
| S6 | risk [analysis] | sudo splits the baseline store: plain `sudo` writes to `/var/root`; `sudo -E` creates root-owned JSONL that breaks later unprivileged appends. | §6.1: store ownership + `SUDO_USER` resolution rule added |
| S7 | risk [verified] | Privilege-dependent visibility (607/904 pids without root) contaminates baselines and `energy_share` denominators. | §6.1: privilege level added to the baseline context key |
| S8 | risk [verified] | Summing `ri_pkg_idle_wkups + ri_interrupt_wkups` buries the signal: a 1 ms-timer loop produced 1 pkg-idle vs 89,719 interrupt wakeups. | §4: split into two vitals; alarm keys on either counter vs its own baseline — a later live test showed sleep-loop storms register **zero** pkg-idle (casebook 0004) |
| S9 | risk → latent blocker [certain] | The `disk.py:51` "prefix struct" habit becomes **heap corruption** at V4: the kernel copies `sizeof(rusage_info_v4)` for the requested flavor. | `core/rusage.py` declares V4 in full, sizeof-asserted against the SDK header |
| S10 | risk [verified] | Name-keyed baselines are multimodal on dev machines (`node` = every LSP/build daemon); pid-keyed diffs are exposed to pid reuse. | §6.1: name normalization; sample identity = (pid, start_abstime) |
| S11 | risk [partially verified] | `qos_mix` is scheduler intent, not core residency; non-app processes accrue ~100% `qos_legacy` (degenerate mix). | §4 caveat added; residency stays in the `powermetrics` inspect tier |
| S12 | nit→risk [analysis] | §6.1 claims both raw JSONL recording and sketch-only storage — order-GB/day vs. undefined flush semantics; pick one. | §6.1 rewritten: raw corpus + derived sketches (see A3/A4) |

Verified-accurate for balance: `powermetrics` gating, `pmset -g assertions`
(free, shows `UserIsActive` + assertion ages), `ioreg` cost (14 ms), wakeup
fields exist, rusage EPERM semantics, and all inherited disk-scope claims.

### Statistics / architecture findings

| # | Severity | Finding | Status |
|---|---|---|---|
| A1 | blocker [certain] | Run-length probability `p̂^r` assumes IID samples; constraint 3 admits the data is autocorrelated. A legitimate 5-min compile scores log₁₀ p ≈ −390 — astronomically damning "evidence" for normal behavior, and it dominates §6.4's combination. | §6.2 rewritten: two-state Markov persistence / empirical run-length baseline |
| A2 | blocker [certain] | "Calibrated posterior" is unearnable: no P(evidence \| culprit) (unsupervised — treating −log p as an LLR assumes it's ≈ 1), no priors, and the §7 example double-counts `cpu_pct` twice. `confidence: 0.94` is the vibe the doc claims to reject. | Renamed: FP-rate-controlled score ("fires under normal behavior < once/week") |
| A3 | blocker [certain] | The "compact sketch" is impossible as specified: exact quantiles need data; MAD is a two-pass statistic with no incremental form. No numpy, no t-digest. | §6.1: bounded reservoir (~512 floats/key) with stated memory budget |
| A4 | blocker [certain] | The false-alarm budget is incomputable: ARL formulas assume IID Gaussian (disqualified by constraint 3); real calibration needs replay against recorded normal traces — which the sketch-only design discards. Budget unit (per rule? per process? per machine) unstated. | §6.1/§6.4: raw vitals kept as first-class calibration corpus; budget per machine |
| A5 | blocker [certain] | Baselines learn from crisis-biased samples: users run a health tool when the machine misbehaves. No background sampler exists before v0.7. | §9: minimal background sampler moved to step 2 |
| A6 | risk [certain arithmetic] | Key cardinality × conditioning ≈ 36–50 k keys; most hold too few samples for a meaningful p99. No fallback hierarchy or min-count gates. | §6.1: hierarchical backoff (key → drop hour → drop power state → prior) |
| A7 | risk [certain circularity] | Quarantined ingestion cold-start: a first-week pathology either never accrues a baseline or becomes the certified baseline that quarantine then defends. Defenses specified at slogan level. | §6.1: cold-start policy (priors sole authority < N days); explicit admission |
| A8 | risk [certain gaps] | Taxonomy rules consume vitals §4 never defines: "user-launched", "frontmost", system-idle, process age, "useful I/O", assertion-duration baseline. `triage` as "nothing but the pipeline" is quietly false. | §4/§7: missing vitals added with probes, or signatures trimmed |
| A9 | risk [violations certain] | Strict-adjacency layering is violated by the doc itself (diagnosis reads vitals+scores; TUIs render vitals; agents read vitals) and by the code (`disk_tui.py:269` calls `cmd_inspect`, which prints). Statefulness is homeless. | §2/§10: rule → "any layer below, never above/beside"; state ownership named |
| A10 | risk [certain gap] | Finding schema lacks incident identity and lifecycle — `watch`'s dedupe/hysteresis has nothing to key on; `subject.pids` goes stale on daemon respawn. | §7: `finding_id`, subject by name-set, lifecycle states + exit condition |
| A11 | risk [certain] | Build order records baseline data (v0.2) two milestones before its format is designed (v0.6). | §9: versioned raw-JSONL schema decided at step 1–2; sketches derived |
| A12 | risk [judgment] | "A few dozen lines" is a 1,500–3,000-line stats library in denial; synthetic tests validate implementations against their own (wrong) assumptions. | §6 scope cut: exceedance + robust z + MK/Theil–Sen + corrected run-lengths; EWMA-or-CUSUM pick one, defer Poisson/Spearman/Holt |
| A13 | nit→risk [judgment] | Spearman co-movement confirms confounding (thermal events, backups correlate everything); zero-ties destabilize ranks. | Deferred with guard noted (pair correlates AND population doesn't) |
| A14 | nit [certain] | Modeled energy has no watts → reconciliation residual and `unattributed-draw` silently break off Apple Silicon direct path. | Subsumed by S3/S4 rewrite |
| A15 | nit [certain] | Sharp edges: concurrent JSONL writers, log(0) counts, hour buckets vs timezones, σ_ewma/μ₀/k sources unspecified. | §6.1/§6.2 notes added |

### Joint verdict

The architecture's skeleton — probes → vitals → stats → diagnosis → surfaces,
structures-not-text, evidence-carrying findings, honest residuals — survives
both reviews intact. What failed was the **probe contract** (S1–S4: the
battery scope's flagship output was unbuildable end-to-end by any path the
doc described) and the **statistical self-image** (A1–A4: the engine's two
proudest claims, composable evidence and calibrated posteriors, rested on an
independence assumption the doc itself forbids and a likelihood that doesn't
exist). Both are fixable by subtraction: measure what the OS actually
exposes at polling timescales, and claim only the calibration the data can
deliver.
