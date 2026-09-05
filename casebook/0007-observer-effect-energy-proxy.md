# Case 0007 — the energy proxy ranks its own observer
status: diagnosed
opened: 2026-07-27
links: case 0001 · case 0004 · case 0005 · README.md "For AI agents"
touches: scopes/battery.py (branch krishankumar95/pr08-battery-scope)

## 0007.1 · 2026-07-27 · hypothesis

`battery top` and `battery drainers` score processes with a transparent proxy,
`energy = CPU% + W_IDLE*idle_wakeups + W_INTR*intr_wakeups` (`W_IDLE = 0.10`,
`W_INTR = 0.05`). The bet, inherited from case 0001 and case 0004: wakeups are
the signal that CPU% alone misses, so a wakeup-weighted score will rank the
true drain sources on a machine that looks idle to `cpu top`.

The first half held. A live field diagnosis on Mac17,9 (M5 Pro, single
built-in XDR 3024x1964, 79 days uptime) had `cpu top` reporting an idle
machine (peak 136% of 1800%, median ~50%, never thermally throttled per
`pmset -g therm`) while the battery gauge showed 2240 mA @ 11.465 V = **25.7 W**
and `pmset` estimated 2:23 of runtime at 46% charge. CPU% and disk I/O both
excluded themselves. The battery scope was the only surface that produced a
non-empty answer, which is exactly what case 0001 predicted.

The second half did not hold.

## 0007.2 · 2026-07-27 · failure — the proxy's top-ranked drainer is the terminal rendering the proxy's output

18 samples of `battery top` (non-root), averaged per pid:

| rank | process | energy | CPU% | wakeups/s |
|---|---|---:|---:|---:|
| 1 | ghostty | 22.0 | 4.7% | **255.9** |
| 2 | claude (2.1.220) | 21.0 | 11.8% | 133.9 |
| 3 | Arc | 13.4 | 1.3% | 177.4 |

Visible wakeup total: min 749.5/s, median 975.4/s, max 1099.1/s.

Ground truth, 10 samples of `powermetrics --samplers tasks
--show-process-energy -r wakeups` (root), same machine, ~6 minutes later:

| process | pkg idle wk/s | intr wk/s | energy impact | CPU ms/s |
|---|---:|---:|---:|---:|
| kernel_task | 331.6 | 795.4 | 7.86 | 55.8 |
| coreaudiod | **194.3** | 344.6 | 9.82 | 14.9 |
| Arc | 65.3 | 112.3 | 3.61 | 8.2 |
| AppleCentauriBeta | 58.0 | 99.1 | 0.82 | 6.8 |
| WindowServer | 56.1 | 122.4 | **236.09** | 129.1 |
| AppleCentauriAlpha | 50.3 | 89.5 | 1.19 | 8.8 |
| ghostty | **32.2** | 68.3 | 63.40 | 32.1 |

The proxy ranked ghostty first at 255.9 wakeups/s. Ground truth puts it
seventh at 32.2/s, an **8x** overstatement. The per-sample series explains
why it is not sampling noise:

```
ghostty pkg-idle wakeups, per sample:  0.98  0.00  1.96  0.98  0.98  110.53  130.72  11.66
ghostty energy impact,    per sample:  0.01  0.01  0.14  0.01  0.01   13.78  484.65   8.62
```

Five of eight samples are at or near zero. The two large samples coincide with
the investigating agent printing capture output to that terminal. ghostty's
mean is an artifact of two bursts, and those bursts were **caused by running
stethoscope**. The terminal was not draining the battery; it was drawing the
diagnosis.

This is measurement backreaction, not a weighting error: no choice of
`W_IDLE`/`W_INTR` fixes it, because the wakeups are real. They are simply
attributable to the observer.

## 0007.3 · 2026-07-27 · failure — `drainers` integrates the artifact instead of averaging it out

`battery drainers` over a 14-minute window on battery (charge 46% → 44%):

| process | energy | CPU s | wakeups |
|---|---:|---:|---:|
| ghostty | 13751.8 | 38.9 | **202457** |
| claude (2.1.220) | 2972.0 | 44.3 | 43803 |
| node | 2183.5 | 1.1 | 34354 |
| callservicesd | 1428.1 | 2.1 | 21400 |

202457 wakeups over 840 s is 241/s sustained, which contradicts the 32.2/s
ground truth by 7.5x. `coreaudiod`, the true top wakeup source, does not
appear in the top 20 at all.

The cumulative view is *more* corrupted than the instantaneous one, not less.
The 14-minute baseline window was precisely the period in which the agent ran
its captures, so `drainers` integrated the observer effect rather than
diluting it. Worse, `drainers` is the surface most likely to be trusted, since
its whole premise is that a longer window is more truthful than a snapshot.

Contradicts the implicit assumption in 0007.1 that cumulative scoring is the
robust variant.

## 0007.4 · 2026-07-27 · failure — the actual cause was not a rate, and no ranking could have surfaced it

The real diagnosis came from `pmset -g assertions`, which the battery scope
does not read:

```
pid 416(coreaudiod): 01:43:39 PreventUserIdleSystemSleep
  named: "com.apple.audio.00-13-EF-A0-0A-A0:output.context.preventuseridlesleep"
  Created for PID: 71316 (com.apple.WebKit.GPU)
  Resources: audio-out 00-13-EF-A0-0A-A0:output
```

`00:13:EF:A0:0A:A0` resolves via `system_profiler SPBluetoothDataType` to
`D50s`, a connected Bluetooth A2DP output device. A WebKit page had held an
audio output stream to it for 1h43m. The consequences:

- `coreaudiod` at 194.3 pkg-idle + 344.6 interrupt wakeups/s, servicing A2DP
- `AppleCentauriAlpha` + `AppleCentauriBeta` at 108.3 wakeups/s combined,
  the Bluetooth/Wi-Fi combo controller
- ~302 wakeups/s from one causal chain, all correctly ranked by powermetrics
  and all invisible in the proxy's top 3
- `PreventUserIdleSystemSleep` held for 1h43m, so the machine never idle-slept
  and never cooled

The load-bearing fact is the **assertion**, not any process's rate. A ranked
list of per-process rates cannot express "this process is holding a
system-wide sleep assertion on behalf of another pid, over a radio". The
scope's output shape was the limitation, not its arithmetic.

Confirmation after the stream ended (no deliberate remediation; the page
simply stopped): draw 25.7 W → **12.0 W**, battery temperature 30.9 °C →
30.6 °C, runtime estimate 2:23 → **5:22**, and `coreaudiod` plus both
Centauri daemons dropped out of the ranking entirely.

Also worth recording against case 0001: the *energy impact* ranking and the
*wakeup* ranking are nearly inverted. WindowServer is 236.09 energy impact at
56.1 wakeups/s, while coreaudiod is 9.82 energy impact at 194.3 wakeups/s.
These are two distinct problems and a single scalar score conflates them.

## 0007.5 · 2026-07-27 · option — exclude the observer's own process ancestry

Walk the controlling terminal / process ancestry of the stethoscope
invocation and suppress or flag those pids.

**Merit.** Directly targets the demonstrated failure. Cheap, no new probe. The
tty owner is discoverable without root.

**Issues.** Wrong when the terminal genuinely *is* the drain (a runaway TUI is
a real diagnosis the tool should be able to make). Suppression hides evidence,
which conflicts with the "never a fake zero" principle from the `POWER` column.
Ancestry is not reliably the renderer under multiplexers, remote sessions, or
when output is redirected to a file consumed by an agent.

## 0007.6 · 2026-07-27 · option — report burstiness, not just the mean

Carry per-process variance across samples and mark a process `bursty` vs
`sustained`, or report a median instead of a mean.

**Merit.** The ghostty series (five zeros, two spikes) is trivially separable
from coreaudiod's flat 194/s by any dispersion measure. Preserves all
evidence, adds a column rather than removing rows. Generalizes past the
observer case to any spiky workload, and directly serves the agent contract in
README.md, since "sustained" is the claim an agent actually wants to reason
over.

**Issues.** Does not attribute causation, so a human still has to notice that
the bursts are self-inflicted. Needs a defensible sample count before variance
is meaningful, which interacts with case 0002's calibration work. `drainers`
accumulates counters rather than retaining a series, so this is a real change
to its state file, not a display tweak.

## 0007.7 · 2026-07-27 · option — read `pmset -g assertions` as a first-class vital

Surface power assertions (holder pid, target pid, resource, age) as their own
output block alongside the ranking.

**Merit.** It is the only surface that would have produced the actual answer
in 0007.4, including the cross-pid attribution (`Created for PID: 71316`) and
the resource identity that resolves to a Bluetooth device. Cheap, no root
required, stable text contract. An assertion held for 1h43m is a far stronger
signal than any rate, since it means the machine cannot sleep at all.

**Issues.** Parsed text, not a struct, which sits badly with the
ARCHITECTURE.md §2 rule that structures live below the surface layer, so it
needs a parser with an explicit contract and tests. Assertion names are
undocumented and version-fragile. It is a different output shape from a ranked
table, so it complicates the `--json` schema owed by v0.5.

## 0007.8 · 2026-07-27 · option — attribute wakeups to a responsible pid

Use powermetrics' `--show-responsible-pid` concept: charge wakeups to the
process that caused them rather than the one that serviced them.

**Merit.** Correct in principle for both failures here. coreaudiod's wakeups
belong to WebKit.GPU (pid 71316); ghostty's belong to the agent writing to the
tty. One mechanism, both cases.

**Issues.** Requires root via powermetrics, which breaks the no-sudo promise
that makes `battery health` and `top` useful unprivileged. No public
unprivileged API exposes the responsible pid. Would make the proxy depend on
the tool it is supposed to approximate, collapsing the layering decided in
0001.8.

## 0007.9 · 2026-07-27 · option — take the weights from Apple's own `pmenergy` coefficients

`python3 -m core.validate` reports that the board resolves to a `pmenergy`
plist (`Mac17,9 -> default.plist`) exposing 15 coefficient keys, among them
`kcpu_wakeups`, `kcpu_time`, `kdiskio_bytesread`, `kdiskio_byteswritten` and
the five `kqos_*` tiers. These are the platform's own weightings, the thing
`W_IDLE = 0.10` / `W_INTR = 0.05` currently guesses at.

**Merit.** Replaces two invented constants with per-board values Apple
publishes, on disk, readable without root. Directly addresses the "platform
specific weighting" caveat that powermetrics' own man page attaches to its
energy number. Also supplies the QoS tiers, which would let the score
distinguish a background-QoS wakeup from a user-interactive one, a distinction
the current proxy cannot make at all.

**Issues.** Does not fix 0007.2 or 0007.3 in the slightest: better weights on
correctly-measured-but-wrongly-attributed wakeups still rank the observer
first. The board fell back to `default.plist` rather than a Mac17,9-specific
file, so the coefficients may not be tuned for this machine. Validate also
records that rusage cannot supply five of the fifteen keys (`kgpu_time` and
all four `knetwork_*`), so any score built on them is partial by construction,
and partial in exactly the dimension that made WindowServer the top energy
consumer in 0007.4.

## 0007.10 · 2026-07-27 · follow-up — what must be decided before this becomes `treated`

No prescription chosen yet; the branch is unmerged and the scope's arithmetic
is not obviously the thing to change first. Open questions, in the order they
block each other:

a. Is the single `ENERGY` scalar defensible at all, given 0007.4's inverted
   rankings? If not, 0007.6 and the column layout are one decision, not two.
b. Does `drainers` retain a series (0007.6) or stay counter-only? This is the
   state-file compatibility question and should be settled before pr08 merges,
   since the baseline format is user-visible under
   `~/Library/Application Support/stethoscope/battery_baseline.json`.
c. Do assertions (0007.7) belong in the `battery` scope or in the future
   `checkup`? They are diagnostic context rather than a per-process vital.
d. 0007.5 and 0007.8 are alternatives to each other, not complements. Neither
   should land before (a).
e. 0007.9 is orthogonal to the attribution failures and can land independently,
   but it is a weights change to a score whose *shape* (a) may discard, so it
   is wasted work if sequenced first.

One methodological note for every future field diagnosis, independent of the
above: **the agent running stethoscope perturbs what stethoscope measures**,
and the perturbation lands on exactly the wakeup counter the battery scope
weights most. Captures intended to rank wakeups should be written to a file
rather than a tty, and the observer's own ancestry should be recorded
alongside the sample so the contamination is at least auditable after the
fact. This is a live hazard for the agent loop described in README.md
"For AI agents", where the agent is by construction the thing generating
terminal output while sampling.

## 0007.11 · 2026-07-27 · follow-up — the remediation was deliberate, and the observer also holds a sleep assertion

Corrects 0007.4: the closing paragraph states "no deliberate remediation; the
page simply stopped". That is wrong. The operator had closed Arc, Safari, and
a number of long-lived Claude Code sessions. The cessation was an
intervention, not a coincidence, which upgrades the evidence from an
observed correlation to a controlled test of the 0007.4 diagnosis.

Measured after the intervention, with `battery top` redirected to a file
rather than a tty (applying 0007.10):

| vital | before | after | change |
|---|---:|---:|---|
| battery draw | 25.7 W | **6.11 W** | −76% |
| visible wakeups/s (median) | 975.4 | **346.9** | −64% |
| battery temperature | 30.9 °C | 30.6 °C | |
| runtime estimate | 2:23 | 5:22 | |

`coreaudiod`, `AppleCentauriAlpha` and `AppleCentauriBeta` are absent from
both the ranking and `pmset -g assertions`. Arc is absent entirely. The
0007.4 causal chain is confirmed by removal.

**The new finding.** With the audio assertion gone, the only remaining
`PreventUserIdleSystemSleep` holder is:

```
pid 59546(caffeinate): PreventUserIdleSystemSleep "caffeinate command-line tool"
  Details: caffeinate asserting for 300 secs
  ppid 56465 (claude), argv: caffeinate -i -t 300
```

The agent session spawns `caffeinate -i` and renews it on a 300 s timer, so
for as long as an agent is alive the machine **cannot idle-sleep**. This is a
second, distinct observer effect, and a more serious one than 0007.2: the
first injects wakeups into the ranking, this one changes the power state of
the system under measurement. Every battery measurement taken during an agent
session is taken on a machine that has been prevented from sleeping, and
sleep is the single largest lever on both drain and temperature.

Two consequences:

- It strengthens 0007.7 from "would have found the answer" to "is required".
  The scope must surface assertion holders partly so that **its own
  invocation's assertion appears in its own output**. A tool that cannot show
  the operator perturbing the measurement is not honest about its numbers.
- `drainers` is unusable as specified when run under an agent. Its window is
  by definition a window in which the machine could not sleep, so its
  cumulative totals describe a counterfactual machine. This is a stronger
  objection than 0007.3 and should be resolved before pr08 merges.

Not fully remediated: five agent sessions aged 9, 10, 17 and 18 days are still
running, each carrying its own caffeinate capability. The intervention was
partial, which is consistent with the residual 346.9 wakeups/s still being
dominated by agent-related processes.



## 0007.12 · 2026-09-05 · follow-up — the ground truth this case rests on is unreachable by an agent

Case 0008 re-diagnosed this machine on 2026-09-05 and hit a constraint that
bears directly on this case's method rather than on its subject.

Every quantitative claim in 0007.2, 0007.4 and 0007.11 is anchored to root
`powermetrics` as ground truth: the proxy is wrong *because* powermetrics
ranks ghostty seventh, the causal chain is confirmed *because* powermetrics
attributes 302 wakeups/s to the audio path. ARCHITECTURE.md §3 formalises
this by placing `powermetrics` in the inspect tier, and README's "For AI
agents" describes a loop in which an agent notices a symptom, calls probes,
and proposes a diagnosis with a drill-down command to confirm it.

**An agent cannot run that drill-down.** `sudo` requires a TTY to prompt for
a password, and the agent's shell has none. Measured on 2026-09-05, four
invocations from inside a Claude Code session:

```
sudo powermetrics ...        sudo: a terminal is required to read the password
sudo powermetrics ... -D     sudo: a terminal is required to read the password
sudo powermetrics ... -S     sudo: a terminal is required to read the password
sudo -S powermetrics ...     Password:
                             sudo: no password was provided
```

The fourth is the informative one: sudo reached its prompt and found no stdin
to read from. This is not a missing flag, and no flag combination resolves it.
The operator obtained the 0008.2 numbers by running the command in a separate
interactive terminal and pasting the output back.

Consequences for this case and for the agent contract:

- **0007's own evidence could not have been gathered autonomously.** The
  entries stand, since a human ran the commands, but the loop described in
  README cannot reproduce them unattended. Any `verify` string in a `Finding`
  (§7) that names a root command is an instruction the agent must hand back
  to a human, not one it can execute.
- **`Finding` needs to say so.** `remediation` already carries
  `reversible`; `verify` carries no equivalent. A field distinguishing
  agent-executable drill-downs from human-only ones would make the contract
  honest, and would let a surface render "run this yourself" rather than
  failing opaquely.
- **It raises the value of free, unprivileged probes.** 0008.4 measured
  `ioreg -c IOAccelerator` at 10 to 20 ms with no root. Probes an agent can
  actually reach are worth more per unit of information than richer ones it
  cannot, and §3's cost column should arguably carry a privilege-reachability
  note alongside the timing.

This does not weaken 0007.10's methodological note, it compounds it. The agent
both perturbs the measurement (0007.2), changes the power state of the system
under measurement (0007.11), and cannot access the instrument that would
adjudicate either (this entry).
