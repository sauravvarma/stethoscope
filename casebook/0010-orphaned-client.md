# Case 0010 — a process no scope can call abandoned
status: diagnosed
opened: 2026-09-17
links: case 0001 · case 0007 · case 0008 · case 0009 · ARCHITECTURE.md §3 · §5 · Appendix A S3
touches: (none yet — no scope reads process lineage)
machine: Mac17,9 (M5 Pro), macOS 27.0 build 26A428, booted 2026-09-15 01:10:49 +0530

## 0010.1 · 2026-09-17 · hypothesis

The operator's symptom was the inverse of case 0008's. Not "the fans have not
stopped spinning" but "my battery is dipping slowly and consistently even when
I have no process ongoing". Where 0008 began with a load loud enough to hear,
this one began with a load quiet enough to deny.

The working assumption, inherited from 0001 and 0005, was the same one 0008
started from: a sustained drain resolves to a per-process rate that some scope
can rank. With the gpu scope shipped since 0008.12, three of the five suspects
now have an owner-ranking scope. The expectation was that disk, cpu or gpu
would name the drain, and that the residual would be small enough to wave at.

## 0010.2 · 2026-09-17 · failure — the three shipped scopes account for 0.3 W of 17.6 W

Ground truth for the draw, decoded from `AppleSmartBattery` rather than
`powermetrics`, because 0007.12 recorded that root `powermetrics` is
unreachable by an agent and that finding still held (`sudo -n` returned
nothing):

```
InstantAmperage = 18446744073709550126   ->  -1490 mA   (two's complement, 2^64 - 1490)
Voltage         = 11318                       11.318 V
                                         ->   16.86 W
```

Ten consecutive samples at 2 s intervals read `-1558 mA @ 11.30 V` without
variance: **17.6 W sustained**. An idle M5 Pro belongs near 3 to 6 W.

What the shipped scopes said about it:

| Scope | Reading | Share of 17.6 W |
|---|---|---|
| `cpu top` | attributed power **0.2 W** | 1.1% |
| `gpu top` | **0.1 W**, unattributed **0.0%** | 0.6% |
| `disk top` | absent from the ranking entirely | 0% |
| **total** | **0.3 W** | **1.7%** |

The gpu scope deserves credit here: it answered its question correctly and
fast. `attributed to live processes 14.9% · unattributed 0.0%` cleared the
GPU as a suspect in one sample, which is precisely the discrimination 0008
was built to provide. The 0008 pathology was absent and the scope proved it.

But 98.3% of the draw sat outside every ranking stethoscope can produce. The
causes, found with `ioreg` and `pmset` and nothing the tool owns:

| Source | Draw | Probe |
|---|---|---|
| Display backlight | ~4 to 8 W | `BrightnessMicroAmps` 3199 of 13799 max; `rawBrightness` 1488/2047 |
| iPhone charging over USB | up to ~7.5 W | `IOUSB` `iPhone@00100000` active, `ExternalConnected = No` |
| WiFi, SSD, ANE, fabric | ~2 W | residual |

This is 0001's `unattributed` bucket again, whose §5 parenthetical is
"display, GPU, radios". 0008 removed GPU from that parenthetical. The display
and the radios are still in it, and on this machine they were the whole
answer.

## 0010.3 · 2026-09-17 · failure — the drain was holding its own observer

The largest single controllable item was not a device. `pmset -g assertions`:

```
pid 69476(caffeinate): PreventUserIdleSystemSleep named: "caffeinate command-line tool"
        Details: caffeinate asserting for 300 secs
```

Traced by parent:

```
69476  caffeinate -i -t 300
  └─ 68855  claude
       └─ 68804  -zsh
```

The agent conducting the diagnosis was itself holding the machine awake, on a
rolling 300 s lease, renewed for as long as the session stayed open. The pid
changed between samples (68954, then 69476) because each lease expires and is
replaced, which is why a single `pmset` snapshot looks like a transient and a
second one taken minutes later looks like a different transient.

This is case 0007 in a new organ. 0007 recorded that the energy proxy ranks
its own observer; here the observer does not merely rank high, it *creates*
the assertion that prevents the idle state whose absence is the complaint.
The operator's "even when I have no process ongoing" was true of their own
intent and false of their machine, and the falsifying process belonged to the
tool asking the question.

Recorded as a separate follow-up on the node that owns it: 0007.13.

## 0010.4 · 2026-09-17 · failure — a live process with no living server, and no scope that could say so

`stethoscope cpu top` ranked, at 1.3%:

```
67940  explorer.exe    1.3   0.6   0.7   0.00W   2m00s   1.1
```

The name is wrong twice over. `ps` reports the process as
`C:\windows\system32\winedevice.exe`; the string `explorer.exe` reaches the
ranking from a mapped temp image
(`/private/var/folders/.../winetemp-.../explorer.exe`) that the process holds
open but is not executing. The comm-based naming path picked the wrong one of
sixteen `txt` mappings. This is 0008.5's name-resolution failure in a third
form: 0008 could not resolve a name at all and rendered `pid 5671 (?)`; here
the scope resolved one confidently and was wrong.

What the process actually was:

```
PID 67940   PPID 1   started Wed Sep 16 11:09:49   age 12h23m   CPU 2:11.69   RSS 32 MB
CrossOver.app : not running
wineserver    : NOT RUNNING
```

`PPID 1` is reparenting to `launchd`, which is the signature of an orphan. The
decisive fact is the second line: **no wineserver exists**. Every Wine process
depends on wineserver for its entire kernel ABI. This one had outlived the
process that gives its syscalls meaning, by 12 hours.

Corroborating state, all of it holding:

- cwd `~/Library/Application Support/CrossOver/Bottles/Steam/drive_c/windows/system32`
- `winebus.so` (HID bus) and `winebth.so` (Bluetooth) loaded — both register
  macOS run loop sources on a native thread
- stale socket dir `/private/tmp/.wine-501/server-100000f-17fd483/` with a
  `lock` file and two `tmpmap-*` segments still mapped
- pipe fds 3, 4, 9, 10, 11 **unpaired** — peers died with wineserver
- 15 Rosetta AOT mappings under `/var/db/oah/`

Two stale server dirs existed, one per bottle session that day:

| Dir | mtime | Bottle |
|---|---|---|
| `server-100000f-15cb4a4` | 16 Sep 04:04 | Epic Games Store |
| `server-100000f-17fd483` | 16 Sep 11:10 | Steam |

So the leak recurs per session, and had already recurred twice that day. The
04:04 session leaked a directory; the 11:10 session leaked a live process too.

Measured impact, over a clean 60 s window after the observer was quieted:

| Vital | Reading |
|---|---|
| CPU | 0.87 s over 60 s = **1.45% of one core** (0.08% of 18) |
| Idle wakeups | **4/s** |
| Memory | **32 MB** RSS, 0.0% |
| Network | **zero** sockets (`lsof -i` empty) |
| Disk | **zero** — absent from `disk top` |
| GPU | **zero** — absent from `gpu top` |
| Battery | below the noise floor; see 0010.5 |

**The finding is not that this process was expensive.** It was not: 1.45% of
one core is the cost of a poll loop, and against a 17.6 W draw its share is
tens of milliwatts. The finding is that stethoscope had no vocabulary for what
was wrong with it. Every scope ranked it correctly as noise and every scope
was answering the wrong question. The right question was not *how much is this
process consuming* but *should this process exist at all*, and no probe in
§3 has a field that bears on it.

0008 found a GPU draw with no living owner. This is the mirror image: a living
owner with no draw, and nothing to own it back.

## 0010.5 · 2026-09-17 · failure — two probes that did not survive contact

Two measurement attempts failed in ways worth recording, because both will
recur for anyone extending the tool toward this case.

**`sample(1)` cannot walk a Rosetta stack.** `sample 67940 5 10` completed and
produced 2.5 MB of output in which every frame of every thread is the same
unresolved address:

```
417 Thread_930320
+ 417 ???  (in ntdll.so)  load address 0x2086da000 + 0x34125  [0x20870e125]
+   417 ???  (in ntdll.so)  load address 0x2086da000 + 0x34125  [0x20870e125]
+     417 ???  (in ntdll.so)  load address 0x2086da000 + 0x34125  [0x20870e125]
```

Twelve threads, 417 samples each, zero symbols and no frame variation. The
x86-64 guest stack under Rosetta is not walkable by the native sampler, so the
"why" half of the diagnosis — the equivalent of `disk inspect` — has no
instrument for translated processes. The root cause in 0010.4 was assembled
from `lsof`, `ps` and process lineage instead, none of which needed the stack.

**Battery draw is not a stable enough series to attribute against.** Across
the session the same decoded probe read 17.6 W, then 23.1 W, then 11.3 W, the
variance tracking the diagnostic load rather than the subject. Mid-session the
operator connected the charger (`ExternalConnected = Yes`, `+4609 mA`), at
which point discharge attribution became impossible outright. Any future
battery scope that attributes watts to processes by differencing this series
will be measuring itself, exactly as 0007 predicted for the energy proxy.

## 0010.6 · 2026-09-17 · option — a lineage column on the existing cpu scope

Add an orphan/abandonment signal to `cpu top`'s existing `STATE` column: flag
`PPID 1` for a process whose session leader is gone, or whose known server
peer is absent.

**Merit.** No new scope, no new probe: `PPID` is already in the `kinfo_proc`
the cpu scope reads, so the marginal cost is a column and a predicate. It
appears where the operator is already looking.

**Issues.** `PPID 1` alone is far too noisy to flag — most daemons are
legitimately parented to `launchd`, and a raw orphan flag would light up half
the ranking. The signal that actually mattered here was *peer* liveness
(no wineserver), which is application-specific knowledge that does not
generalise into a rusage-backed scope without a rule table.

## 0010.7 · 2026-09-17 · option — a `process` scope answering "should this exist"

A scope whose ranking key is not a rate at all: lineage, age, session,
orphanhood, holder-of-stale-resources, peer liveness.

**Merit.** It is the question this case actually needed, and it is the one
question no rate-ranking scope can be extended to answer, because a correctly
idle leak ranks last in every rate. It would also have caught 0008's zombie
from the other side.

**Issues.** It breaks the §3 "one probe, three scopes" spine — the rusage
contract of 0003 supplies none of these fields. It risks becoming a rules
engine of per-application special cases (wine, docker, simulators), which is
a maintenance shape the tool has so far avoided.

## 0010.8 · 2026-09-17 · option — leave it to `checkup`

Treat orphan detection as a composition-layer concern: a `checkup` rule that
cross-references running processes against stale lock/socket directories in
`/tmp`, rather than a scope anyone runs directly.

**Merit.** Keeps the scopes rate-pure. Stale-resource-without-owner is
genuinely a whole-body finding, not a per-vital one, which is what `checkup`
is for. The detector written during this diagnosis is already this shape and
is nine lines of shell.

**Issues.** `checkup` does not exist on any branch that also has the gpu
scope; it lives only on the krishankumar95 series (0006). Choosing this option
makes the case blocked on the 0006 reconciliation rather than on its own
merits.

## 0010.9 · 2026-09-17 · follow-up — the detector, and what is still unverified

A standalone detector was written and run, exit 1 on leak:

```
CrossOver.app : not running
wineserver    : NOT RUNNING
wine procs    : 67940
stale srv dirs: 2
>> LEAK: wine process alive with no wineserver.
>> fix: kill 67940
```

It encodes exactly the predicate 0010.4 turned on: a wine process alive with
no wineserver. Reproduction is to launch a CrossOver bottle, quit CrossOver,
and re-run it; the observed rate on 2026-09-16 was two sessions out of two.

**The kill was not performed.** `kill 67940` was refused by the agent's
permission classifier, so the after-state is unmeasured and the treatment is
unverified. The case stays `diagnosed` rather than `treated` for that reason
and no other: the root cause is established, the prescription is not chosen,
and the one experiment that would confirm the process exits cleanly on
`SIGTERM` (rather than requiring `-9`, which would itself be a finding worth
reporting upstream to CodeWeavers) has not been run.
