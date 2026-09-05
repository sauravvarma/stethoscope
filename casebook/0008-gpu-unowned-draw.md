# Case 0008 — a 40 W GPU draw with no living owner
status: diagnosed
opened: 2026-09-05
links: case 0001 · case 0007 · ARCHITECTURE.md §3 · §5 · §7 · Appendix A A8
touches: (none yet — no scope reads the GPU)
machine: Mac17,9 (M5 Pro), macOS 25G83, AGXAcceleratorG17X

## 0008.1 · 2026-09-05 · hypothesis

The operator's symptom was thermal, not numeric: "the fans have not stopped
spinning", with CPU, GPU, memory, network and disk all named as suspects. The
working assumption, inherited from case 0001 and case 0005, was that a
sustained thermal load resolves to a per-process rate that some scope can
rank. Either a runaway on CPU, an io-churner on disk, or, if the machine
looked idle to both, a wakeup-storm of the shape case 0004 describes.

The machine had been up 11 h 47 m (booted 2026-09-05 04:43:08 +0530).

## 0008.2 · 2026-09-05 · failure — every vital stethoscope owns reads quiet while the machine draws 40 W

No scope stethoscope has, or has planned through v0.4, produces a non-empty
answer for this machine state:

| Vital | Reading | Source |
|---|---|---|
| CPU | **93.16% idle**, load avg 2.10 | `top -l 2` |
| Memory | **0 swapins / 0 swapouts**, 82% free | `vm_stat`, `memory_pressure` |
| Disk | 4.22 MB/s for one sample, then **0.01 MB/s** | `iostat -d -w 1 -c 3` |
| Network | ~63 MB in over the session, agent traffic only | `nettop -P -l 2` |
| Wakeups | nothing elevated; no storm signature | `top`, per-process |

Ground truth for what the machine was actually doing, from
`powermetrics --samplers gpu_power -i 1000 -n 3` (root, run by the operator
in a separate terminal, three consecutive samples):

```
GPU HW active residency:  100.00%   100.00%   100.00%
GPU idle residency:         0.00%     0.00%     0.00%
GPU SW requested state:    P13 100%  P13 100%  P13 100%
GPU HW active frequency:   1555 MHz  1619 MHz  1552 MHz
GPU Power:                 39358 mW  45978 mW  38603 mW
```

**39.4 to 46.0 W sustained into the GPU, zero idle residency, the maximum
software-requested performance state held 100% of the time.** The frequency
histogram sits entirely in the top three bins (1470/1578/1620 MHz); every bin
from 338 MHz to 1374 MHz reads 0%.

This is roughly six times the 6.11 W post-intervention figure recorded in
0007.11 on this same machine, and it is invisible to every probe in
ARCHITECTURE.md §3. The `rusage` spine, which §3 calls "one probe, three
scopes", has no GPU field at all.

Against case 0001: the reconciliation table in §5 would render this entire
load as `unattributed`, whose parenthetical is "display, GPU, radios". That is
the correct bucket and a useless answer. It names the residual without
identifying anything.

stethoscope's own probe-validation harness already reports both halves of this
gap, on this machine, without anyone having asked it to. `python3 -m
core.validate` run during this diagnosis:

```
INFO  pmenergy   board Mac17,9 -> default.plist (fallback); 15 coefficient keys;
                 rusage cannot supply: kgpu_time, knetwork_recv_bytes, ...
PASS  energy_nj cadence   pid 5671 (?): 0/10 nonzero deltas
```

Two things are visible there. Apple's own Energy Impact formula takes a
**`kgpu_time`** term, and `rusage`, the spine of every scope, cannot supply
it: the gap this case documents is already named in the validator's output and
in Appendix A finding S3, which notes the formula "needs network/GPU inputs
rusage lacks". And the validator, sampling the zombie as one of its subject
pids, renders it `pid 5671 (?)`: it could not resolve a name, which is 0008.5's
name-resolution failure reproduced by stethoscope's own code path.

## 0008.3 · 2026-09-05 · failure — the owner is a zombie, and killing its living relatives released nothing

The load originated from GTA5 running under CrossOver Preview. At first
observation the game process had already been dead for 2 h 55 m:

```
PID   PPID  STAT  %CPU   RSS  ELAPSED    COMMAND
5671     1  ?Es    0.0     0  02:55:42   (GTA5_Enhanced.ex)
```

`?Es` with `RSS 0` and a parenthesised name is an exited process that
`launchd` has not reaped. Two live CrossOver processes remained:

```
5451  winewrapper.exe --wait-children --start -- .../Epic Games Launcher.lnk
5454  wineserver
```

`wineserver` held **no children and no open GPU handles** (`pgrep -P 5454`
empty; `lsof -p 5454` matched nothing on metal/agx/gpu), so it was keeping the
prefix alive rather than doing the work.

The test, and the reason this entry is a failure rather than a diagnosis:
`osascript -e 'quit app "CrossOver Preview"'` returned 0 and changed nothing
(both pids still alive after 3 s, since they were reparented to `launchd` and
no longer owned by the app). `kill 5451 5454` then removed both. Sampled
across the following 30 s, **the GPU did not release**:

```
Device Utilization %   100  100  100  100  100  100
In use system memory   8.83 8.83 8.83 9.50 9.28 8.95  GB
Alloc system memory    11.06 11.06 11.06 11.79 11.76 11.69  GB
```

Utilization stayed pinned and the allocation kept churning by ~700 MB between
samples, which is live work rather than a frozen counter. `kill -9 5671` is a
no-op against a process that has already exited.

The conclusion that matters architecturally: **the GPU context outlived every
process that could be named as its owner.** There is no pid to rank, no
`comm` to normalize into a baseline key (§6.1's S10 name-normalization has
nothing to work with), and no rate to score. Per-process attribution is not
merely wrong here, it is inapplicable. This is the same shape as 0007.4,
reached from a different direction: *a ranked list of per-process rates cannot
express the finding.*

Remediation is a reboot. Nothing in userspace reclaims a leaked accelerator
context whose owning task is an unreaped zombie.

## 0008.4 · 2026-09-05 · failure — the free GPU probe was distrusted for the wrong reason, and it was right

`ioreg -c IOAccelerator -r -d 1` exposes a `PerformanceStatistics` dictionary
including `Device Utilization %`, `Renderer Utilization %`, `Tiler
Utilization %`, `In use system memory` and `Alloc system memory`. It is not in
the §3 probe table.

During the diagnosis this probe was **discounted on a heuristic**: it returned
exactly `100` on every sample, never 99, never 97, and a genuinely varying
workload was expected to fluctuate. That reasoning was recorded to the
operator as grounds for treating the counter as possibly stuck, and a root
`powermetrics` cross-check was requested to settle it.

The cross-check vindicated the counter. `Device Utilization % = 100`
corresponds exactly to `GPU HW active residency: 100.00%` with `idle
residency 0.00%`. The invariant reading was the signal, not an artifact: a
context pinned at P13 genuinely never drops below full residency.

Measured cost, five consecutive invocations:

```
real 0.02  0.01  0.01  0.01  0.01
```

**10 to 20 ms, free, no root.** That is the same order as the
`ioreg -rn AppleSmartBattery` probe already in §3 at ~14 ms, and three orders
below `pmset -g log` at ~1.9 s (S5). It qualifies on the §3 selection
philosophy as stated: SIP-safe, ships with the OS, cheap enough to poll.

Recorded as a failure rather than an option because the lesson is about
probe-contract method, in the spirit of case 0003: **a counter that does not
vary is not thereby suspect.** Saturation is a legitimate reading, and the
heuristic applied here would reject exactly the pathology the tool exists to
catch. Any validation added to `core/validate.py` for this probe must not
assert variance as a health condition.

## 0008.5 · 2026-09-05 · failure — the assertion holder outlived its process by three hours

`pmset -g assertions`, the probe 0007.4 identified as load-bearing and 0007.7
proposes to surface, reported this throughout:

```
pid 5671(GTA5_Enhanced.exe): [0x00002f760005a34d] 03:08:31
  PreventUserIdleDisplaySleep named: "Wine Process requesting no screen saver"
```

The holder had been dead for over three hours and the assertion age kept
advancing. This is direct evidence for 0007.11's argument that assertions must
be surfaced, and it adds a constraint that entry did not anticipate:

- **Holder liveness is not implied.** Any implementation that resolves a
  holder pid to a process will get an exited pid here.
- **Name resolution degrades.** `ps -p 5671 -o comm=` returns
  `(GTA5_Enhanced.ex)`: parenthesised to mark the exited state, and truncated
  to 16 characters. Feeding that into §6.1's name normalization produces a
  baseline key that is both punctuated and clipped.
- **Assertion-duration baselines need a liveness guard.** Appendix A finding
  A8 already flags "assertion-duration baseline" as a vital the taxonomy
  consumes but §4 never defines. An age that grows without bound because the
  holder cannot release it is a distinct condition from a long-running live
  holder, and `sleep-blocker` as written in §7 does not separate them.

Also observed, and separable from the leak: `caffeinate -i -t 300` with
`ppid` = the agent's `claude` process, exactly as 0007.11 describes. Two
instances were seen and each expired on its 300 s timer. This reproduces
0007.11 on a second date and confirms the renewal behaviour is per-tool-call
rather than continuous.

## 0008.6 · 2026-09-05 · option — a `gpu` scope

Add `scopes/gpu.py` (plus `gpu_tui.py` per the established pairing) reading
the 0008.4 probe: utilization, per-engine (renderer/tiler) residency,
allocated and in-use GPU memory, and performance-state residency.

**Merit.** Fills the measurement gap directly and cheaply, at a probe cost
that supports polling. Makes the §5 residual smaller and more honest by
moving GPU out of the "display, GPU, radios" bucket into a measured vital.
Fits the existing layer model with no new machinery: probe, vitals, scores,
findings. Gives `unattributed-draw` a way to say *which* of its three
suspects is responsible.

**Issues.** The probe is **device-global, not per-process**. It answers "the
GPU is saturated" and never "by whom", so it cannot feed the per-process
attribution that §5's reconciliation table and case 0001 are built around. It
would be the first scope whose vitals have no pid dimension, which the
`Finding.subject` schema in §7 assumes exists (`subject.names`,
`subject.pids`). Per-process GPU attribution requires `powermetrics
--samplers gpu_power` at the inspect tier, which 0008.7 in case 0007's
companion finding shows an agent cannot reach. Roadmap cost is real: this is
a scope not currently on it, competing with `memory` (v0.2) and `battery`
(v0.3).

## 0008.7 · 2026-09-05 · option — GPU vitals inside the `battery` scope

Treat GPU utilization and GPU memory as system-truth vitals of the existing
`battery` scope, alongside `watts_now`, `drain_slope` and `sleep_integrity`,
rather than as a scope of their own.

**Merit.** §4 already splits the battery scope into "system truth" and
"process attribution" precisely because the first is exact and the second is
a model. A device-global GPU reading is system truth by that definition and
needs no pid dimension to belong there. Nothing new appears on the roadmap;
v0.3 absorbs it. It sits directly beside the residual it explains, so
`unattributed-draw` can consult it without a cross-scope join.

**Issues.** The battery scope degrades to "no battery" on desktops (S4), yet
GPU saturation is a real diagnosis on a Mac Studio or Mac mini. Filing a
universally available vital under a scope that does not exist on some hardware
is a structural mistake. It also makes `battery` the catch-all for
non-process system truth, which is how scopes stop being about one thing.

## 0008.8 · 2026-09-05 · option — leave the vital out, route `unattributed-draw` on owner liveness

Add no GPU probe. Instead extend the `unattributed-draw` rule so that when the
residual is large and no process signature accounts for it, the finding cites
zombie processes holding assertions or recently-exited GPU-heavy processes as
candidate explanations, and prescribes a reboot.

**Merit.** Smallest change by far, and it lands in the layer where the
insight actually lives (§7 taxonomy) rather than adding a probe. Requires no
roadmap movement. Preserves the "never a fake zero" principle by admitting
the tool cannot attribute the draw while still routing the operator somewhere
useful.

**Issues.** The residual is only computable in §5's **gated** reconciliation
mode, which requires discharging **and** a root `powermetrics` snapshot. This
machine was on AC power throughout, so `watts_now` was charging flow and no
residual existed to trigger on. The rule would not have fired for the case
that motivated it. It also keeps stethoscope unable to answer "is the GPU
busy", a question a 10 ms free probe can answer outright, which is hard to
defend in a tool whose tagline is interoception.

## 0008.9 · 2026-09-05 · option — assertion-holder liveness as a vital, independent of any GPU work

Narrower and orthogonal to 0008.6 through 0008.8: when the assertions probe is
implemented per 0007.7, carry a `holder_alive` boolean and an
`assertion_orphaned` derived vital, and give `sleep-blocker` a distinct
severity or a sibling class for assertions whose holder has exited.

**Merit.** Cheap, uses a probe already committed to, and addresses a defect
that is certain rather than hypothesised: 0008.5 measured it. An orphaned
assertion is strictly actionable, since it can only be cleared by reboot, so
the remediation is unambiguous and `remediation.reversible` is honestly
false. Partially discharges Appendix A finding A8.

**Issues.** Solves the smaller half of this case. An orphaned
`PreventUserIdleDisplaySleep` costs a machine that will not idle-sleep, which
matters, but it is not the 40 W. Shipping only this would close the case on
the symptom that was easiest to measure rather than the one that was
expensive.

## 0008.10 · 2026-09-05 · follow-up — what must be decided before this becomes `treated`

No prescription chosen. The options are not independent and should be settled
in this order:

a. **Is device-global system truth a first-class vital shape?** 0008.6 and
   0008.7 are the same question asked twice: where does a reading with no pid
   dimension live. §7's `Finding.subject` assumes pids exist, so answering
   this is a schema decision before it is a scope decision. 0008.8 is the
   answer "it does not, keep it in the residual" and should be rejected or
   accepted explicitly rather than by default.
b. **0008.8 is disqualified by its own trigger condition** unless §5's
   gating changes. It cannot fire on AC power, and this diagnosis happened on
   AC power. If it is retained, the gate is the thing to redesign, not the
   rule.
c. **0008.9 is independent of (a) and (b)** and can land with 0007.7 whenever
   the assertions probe is implemented. It should not wait on the GPU
   question.
d. **Per-process GPU attribution is out of reach for now.** The only
   per-process GPU source is root `powermetrics`, which is inspect-tier by
   §3 and, per the companion entry filed against case 0007, unobtainable
   non-interactively by an agent. Any design that promises per-process GPU
   watts is promising something the agent contract cannot deliver.

One methodological note, extending 0007.10's: this diagnosis was performed
**without stethoscope**, using `ps`, `top`, `iostat`, `vm_stat`, `nettop`,
`ioreg` and `pmset` by hand, because no scope covers the finding. The tool's
own absence from a successful field diagnosis on its author's machine is the
strongest available argument about where the gap is. Case 0007 at least had
`battery top` producing a wrong answer; here there was nothing to run.
