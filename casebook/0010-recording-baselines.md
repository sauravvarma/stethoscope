# Case 0010 — recording, history, and hourly baselines
status: treated
opened: 2026-07-11
links: issues #18–#20 · case 0003 · case 0006 · case 0009 · supersedes PR #47
touches: core/baseline.py, core/rusage.py, scopes/record.py, tests/test_record.py, SCHEMA.md

## 0010.1 · 2026-07-11 · hypothesis

A local, append-only corpus is a better anomaly substrate than PR #47's
SQLite design. Sampling is single-writer and sequential, retention is daily,
and consumers need corruption visibility more than transactions or indexes.
The Python 3.9 standard library is enough: daily JSONL keeps the raw evidence
inspectable and versioned while replay builds summaries without a daemon or
third-party dependency.

## 0010.2 · 2026-07-11 · decision — append and storage hardening

The corpus uses deterministic local-date `YYYY-MM-DD.jsonl` files and one
strict `baseline-raw/1` object per line. `record` holds a nonblocking `flock`
for its lifetime and appends with `O_APPEND` in one `write(2)` call; it never
rewrites a daily file. Descriptor-relative opens use `O_NOFOLLOW`, verify
regular files, ownership, hard-link count, and writable permissions, and
`fchown` newly created directories/files to the effective `SUDO_USER` before
use. Existing user-controlled paths are never chowned. Retention unlinks
complete daily files older than the configured 30-day local-date window.
Lock, permission, append, and retention failures are explicit exit-4 errors.

## 0010.3 · 2026-07-11 · decision — corruption is evidence

Replay validates every line independently with strict JSON constants and the
raw schema. It reports file, line, and reason for malformed JSON, non-object
values, invalid fields, and non-finite numbers. A final line lacking a newline
is parsed when complete but still reported as `partial_final_line`. Any replay
error marks history/baseline output partial and returns 4; an absent or empty
store remains an explicit clean/cold state and returns 0.

## 0010.4 · 2026-07-11 · decision — bounded contextual percentiles

Percentiles use deterministic-seed reservoir sampling capped at 512 values per
bucket while retaining the exact observed count. Buckets separate local hour,
timezone, root/user privilege, power state, scope, metric, and normalized
process name where applicable. Each reports `count`, `sample_count`, p50, p90,
p99, and `cold`; no empty bucket is presented as a learned normal.

## 0010.5 · 2026-07-11 · decision — one process read, bounded rows

`proc_power_sample` now includes physical footprint and resident bytes already
present in its one libproc struct. Each interval therefore needs one struct
read per accessible process per endpoint for CPU, QoS CPU, wakeups, disk,
energy, and memory. The corpus retains the top 20 active and top 20 footprint
processes, unioned by PID/start identity, plus the sampler itself. Activity
floors are 0.1% CPU, 1 wakeup/s, 1024 disk bytes/s, or 0.01 real watts; this
bounds the default process array at 41 while preserving idle memory consumers.

## 0010.6 · 2026-07-11 · failure — once and positional semantics

Earlier live-loop patterns made it tempting to cap `--once` sleeps for tests
or parse all remaining words as inert arguments. Both violate the issue
contract. `record --once --interval N` now performs one full requested sleep
and one completed delta. `history` accepts zero or one actual scope, and
`history baseline` accepts zero or one scope; extra/unknown positionals and
unsupported flags return usage error 2.

## 0010.7 · 2026-07-11 · follow-up — measured sampler footprint

A non-root release-tree run on the development Mac at a one-second interval
measured 0.251% of one core over the interval, 9.09 MiB physical footprint,
and 13.66 MiB resident size while retaining 33 process rows. The canonical
60-second default amortizes the two short libproc walks further. Every raw
sample records the sampler's own CPU, physical footprint, resident size, PID,
start identity, and normalized name so footprint regressions remain visible
on each machine rather than relying only on this one measurement.

## 0010.8 · 2026-07-11 · failure — poisoned tails and unbounded replay

Adversarial review found that a valid JSON object missing only its final
newline could be concatenated with the next append, making both objects
unreplayable. Writers now inspect the existing final byte under the corpus
lock and reject an incomplete tail before writing. Replay also no longer
materializes a requested corpus: it scans bounded lines into per-bucket
reservoirs, rejects overlong lines, and reports every malformed or partial
line. Raw validation requires the complete stable metric/process shape and
handles oversized JSON integers without crashing.

The same review tightened sudo paths so every component below the effective
user's home is user-owned and traversable, made machine error documents retain
their normal fields, normalized power state to `ac`/`battery`/`unknown`,
preserved meaningful process-role suffixes during name normalization, escaped
all human-rendered process labels, and applied history limits per metric rather
than comparing incompatible units.

## 0010.9 · 2026-07-11 · failure — churn, clocks, and derived bounds

Endpoint review showed that self-baselining every current-only PID erased work
from jobs born during the interval, while vanished jobs disappeared without a
trace. Process start ticks now zero-base only jobs proven to have started
inside the sampling window; unmatched or missing endpoints are counted in the
raw context and mark the sample partial. Rates use equivalent first/second
scan midpoints so scan overhead is symmetric rather than diluting every delta.

Streaming alone was not a memory bound: unique process names and corrupt-line
diagnostics could still grow without limit. Per-metric candidate reservoirs,
context/global bucket ceilings, lazy PRNG state, a 4096-process raw ceiling,
and bounded diagnostics now cap memory. Every corruption is still represented
by an exact `replay_error_count`; the first 1024 details are retained and
`replay_errors_omitted` counts the rest. Bucket eviction is likewise explicit
through `dropped_values` and `history_bucket_limit`.

Finally, raw timestamps must be locally representable, JSON integers/floats
and nesting are bounded before validation, percentile interpolation cannot
overflow on opposite finite extremes, local-clock `--since` resolves through
the platform's DST rules, and command-specific maxima reject sleep/date
overflows as usage errors before a sample is appended.

## 0010.10 · 2026-07-11 · failure — impossible values and concurrent retention

The release pass found four final contract gaps. Naive ISO timestamps at the
edge of year 9999 could overflow while acquiring the local UTC offset;
conversion now maps that clock-range failure to usage error 2. A concurrent
writer could retain an old file after history listed it but before history
opened it; that expected disappearance is now skipped while every opened file
keeps descriptor-stable contents.

Raw validation now rejects negative CPU, wakeup, disk, energy, byte, and
process measurements while preserving signed battery flow. Finally, a
nonempty but incomplete pmenergy coefficient dictionary no longer produces a
fabricated system score of zero: coefficient capability is tested through the
actual scoring function, missing process and system scores stay null, and the
sample records `battery:no_pmenergy_coefficients`.

## 0010.11 · 2026-07-11 · failure — option token consumed as a path

The shared parser accepted `record --store --once` by treating `--once` as the
directory and then starting the default indefinite loop. String-valued options
now reject a following option token as a missing value, so both `--store` and
`--since` fail with usage exit 2 instead of consuming control flags.

## 0010.12 · 2026-07-11 · failure — mixed-unit retention lost scope leaders

The first bounded process union sorted active rows lexicographically by energy,
watts, CPU, wakeups, and disk. With a small limit, an energy row could displace
the actual disk or CPU leader before checkup or history saw it, and the sampler
could consume a footprint slot before presentation excluded it. The collector
still performs one endpoint read, but now retains the bounded identity union of
per-domain CPU, wakeup, disk, energy, and footprint top-N rows. The sampler is
excluded before those limits and added explicitly afterward. This raises the
worst-case raw bound from `2N + 1` to `5N + 1` (1281 at the accepted maximum),
still below raw validation's 4096-process ceiling, while making every
downstream per-scope ranking complete. The bounded line ceiling is 4 MiB so the
maximum production union still fits when every executable basename reaches the
filesystem's 255-byte component limit; oversized hostile input remains
rejected and replayed with bounded memory.
