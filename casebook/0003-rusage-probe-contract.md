# Case 0003 — the rusage probe contract
status: treated
opened: 2026-07-10
links: case 0001 · ARCHITECTURE.md §3, §9 step 0–1 · Appendix A S2/S9/S10
touches: core/rusage.py, core/validate.py, scopes/disk.py, tests/test_core.py

## 0003.1 · 2026-07-08 · hypothesis

`proc_pid_rusage` is the one-probe spine for three scopes: the disk scope's
prefix-struct read of `rusage_info_v2` extends naturally to V4's energy and
QoS fields, whose time values are nanoseconds. (Implicit in disk.py v0.1
and ARCHITECTURE.md's first draft.)

## 0003.2 · 2026-07-10 · failure — time fields are ticks, and the prefix habit corrupts memory

S2, verified: time fields are mach-abstime ticks; timebase on Apple Silicon
is 125/3, so a 1.000 s CPU burn read "as ns" shows 0.024 s — a 41.7× error
invisible on Intel (timebase 1/1), i.e. the bug passes tests on the wrong
machine. S9: the kernel copies `sizeof(rusage_info_v{flavor})` for the
*requested* flavor — disk.py's "prefix of the struct" habit becomes silent
heap corruption the moment flavor 4 is requested. S10: pid-keyed diff loops
are exposed to pid reuse; name-keyed baselines collide (`node` is every LSP
and build daemon on a dev machine).

## 0003.3 · 2026-07-10 · option — patch constants into disk.py and keep going

**Merit:** smallest diff. **Issues:** every future scope re-imports the
same trap; the conversion has to be remembered at every call site; nothing
prevents the next prefix struct. The review existed because implicit
contracts don't survive a second consumer.

## 0003.4 · 2026-07-10 · option — extract core/rusage.py that enforces the contract

**Merit:** the struct is declared once, in full, sizeof-asserted (296
bytes), derived from the live SDK header rather than memory — the reviewer's
own 288-byte estimate was wrong, which proves the rule; conversion happens
inside the module so callers never see raw ticks; `proc_identity(pid)` →
(pid, start_abstime) gives diff loops a reuse-proof key. **Issues:** a
migration touching shipped disk-scope code — needs a regression gate.

## 0003.5 · 2026-07-10 · decision — extract, with two permanent enforcement layers

0003.4, shipped. Enforcement is not documentation: `core/validate.py`
(§9 step 0) parses the SDK header at runtime and diffs it against the
ctypes struct, plus measures the timebase against a real CPU burn — and
`tests/test_core.py` (18 tests) pins the struct size, the conversion math,
and `rank_io`'s diff behavior including pid-reuse and negative-delta
clamping. Regression gate held: `disk top` / `holds` output unchanged after
migration. Rejected 0003.3 because the contract must be enforced where the
struct lives, not remembered where it's used.

## 0003.6 · 2026-07-10 · follow-up — flavor ceiling

V6 is available on this machine (see case 0001.9). When any scope needs it,
the same rules apply: full struct from the header, sizeof-asserted,
validated by `core/validate.py` before first use.

## 0003.7 · 2026-07-10 · follow-up — mach-abstime ages are awake-ages

Live-machine investigation: a webpack dev server showed `ps etime` 63.8
wall-hours but rusage-derived age 37.7 h — `mach_absolute_time` does not
advance during machine sleep, so `start_time_epoch` (and any age computed
from `ri_proc_start_abstime`) measures *awake* time. Deliberately kept:
duty = CPU / awake-time is the honest denominator (a process can't burn
CPU while the machine sleeps). §4's `lifetime_duty` definition updated to
say "awake-age" explicitly; anything comparing rusage ages to wall-clock
timestamps (pmset log segments, baselines' hour buckets) must convert.

## 0003.8 · 2026-07-11 · follow-up — names follow process identity

The v0.2 diff loops correctly key counters by `(pid, start_abstime)`, but
`proc_name` still cached by bare pid. A long-running surface could therefore
attach a dead process's name to the next process assigned that pid. A TTL
would only narrow the wrong-name window and add arbitrary timing policy.
The cache now uses the same kernel identity as the samplers, prunes an old
identity when a pid is reused, and accepts an already sampled identity so
ranking does not pay for a second rusage call. A hermetic regression drives
one pid through two start times and verifies that only the identity change
re-resolves its path.
