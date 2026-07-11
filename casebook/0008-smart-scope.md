# Case 0008 — smart scope
status: treated
opened: 2026-07-11
links: issues #10, #11, #12 · PR #43 review
touches: core/smart.py, scopes/smart.py, tests/test_smart.py

## 0008.1 · 2026-07-11 · hypothesis

Drive health can follow the disk/cpu split: a dependency-free probe
(`diskutil list physical` / `diskutil info`) always gives a SMART verdict,
and `smartctl -j -a` — used opportunistically, never required — adds the
NVMe health log or ATA/SATA attribute table needed for wear, a life
prognosis, and pre-failure warnings (#10, #11, #12).

## 0008.2 · 2026-07-11 · failure — an earlier prototype's five contract bugs

PR #43's review of a prior prototype found: (1) smartctl discovery only
checked Homebrew `bin`, missing the common `sbin` install location; (2) a
truthiness check on `data_units_written` reported a brand-new drive's valid
0 TBW as unknown; (3) diskutil's `not supported` verdict was never replaced
by a smartctl pass/fail verdict, so externals stuck at "not supported" even
with good smartctl data; (4) a missing `available_spare` rendered the human
line as literal `spare None%`; (5) SCHEMA.md's example showed `moderate`
confidence at 3% wear while the code and tests said `<5%` wear is `low`.
Each is a small omission, but together they show the same pattern as 0006.2:
an omitted or wrong detail is itself a false signal in a health probe.

## 0008.3 · 2026-07-11 · failure — a non-fatal smartctl message discarded good data

While validating against this machine's real NVMe drive, `smartctl -j -a`
returned a fully usable `smart_status` and `nvme_smart_health_information_log`
alongside one incidental `severity: "error"` message (a failed Error
Information Log read, exit-status bit `0x40`). An initial implementation
treated *any* error-severity message as "smartctl unavailable" — the exact
opposite of the USB-bridge case (#10's "no pass-through" caveat), where the
device cannot be opened at all (exit-status bit `0x02`, no `smart_status` or
health log present). Conflating them would discard real wear/life data on
ordinary hardware merely because smartctl logged one unrelated warning.

## 0008.4 · 2026-07-11 · option — treat every smartctl message as fatal

**Merit:** simplest possible rule; matches "don't fabricate healthy data" by
refusing to use any document that logged a problem. **Issue:** 0008.3 shows
this discards a perfectly good NVMe health log on real hardware whenever one
side query fails, which is common and unrelated to whether the drive can be
read at all — the opposite of "tells you more when it can" (#10).

## 0008.5 · 2026-07-11 · decision — key off smartctl's own open/query bit

`core.smart.probe_smartctl` treats data as unavailable only when smartctl's
own `exit_status` bit `0x02` (device open/query failed) is set, or when
`smart_support.available` is `false`, or when the parsed document carries
none of `smart_status` / `nvme_smart_health_information_log` /
`ata_smart_attributes` at all — the USB-bridge/no-pass-through case (#10).
A drive with usable data plus one incidental error message is used as-is.
Every PR #43 finding is folded in: `find_smartctl` checks PATH via
`shutil.which` then `/opt/homebrew/{bin,sbin}` and `/usr/local/{bin,sbin}`;
`extract_smartctl` uses `is not None` for `data_units_written` so 0 TBW
survives; `drive_health` lets a smartctl `false` verdict always win to
`failing`, and a `true` verdict only replace an `unknown`/`not supported`
diskutil status (never downgrading an existing `failing`); the human
renderer prints `?` for a missing spare instead of `None%`; and
`life_estimate` keeps `<5%` wear at `low` confidence, adding a `moderate`
(5–20%) and `high` (≥20%) band above it so confidence grows with the amount
of wear actually observed, consistent with `tests/test_smart.py`.

## 0008.6 · 2026-07-11 · follow-up — malformed objects and ATA temperature

Integration review exercised valid JSON shapes that smartctl should never
emit but an external probe still has to reject safely: `null`, arrays, and
non-object nested metadata previously reached `.get()` calls and crashed.
The probe now rejects a non-object document explicitly and every nested
smartctl container is type-guarded before extraction. The same review found
that ATA temperature was parsed and assessed but only rendered inside the
NVMe wear block; temperature now renders independently whenever smartctl
supplies it. A nonzero `diskutil` exit is also a probe failure rather than an
empty healthy drive list.

## 0008.7 · 2026-07-11 · follow-up — review the assembled contract

A final diff review caught four integration-level false signals that isolated
probe tests did not: unsupported SMART rendered as `healthy`; malformed scalar
values could still reach arithmetic or bit operations; a per-drive `diskutil
info` failure did not mark the document partial; and the documented warning
`code` and consumed-life fields were absent. The renderer now reserves
`healthy` for an explicit verified verdict, scalar extraction is finite and
type-safe, partial reasons cover diskutil detail failures, and tests assert the
complete documented warning/life shape.

## 0008.8 · 2026-07-11 · follow-up — retain health-bearing exit bits

Re-review found that treating every smartctl exit bit other than device-open
failure as cosmetic lost current ATA threshold signals: bit `0x08` is a
failing SMART status and `0x10` is a pre-failure attribute at threshold.
Extraction now preserves the exit bitmask, forces `passed: false` for `0x08`,
and assesses `0x10` as critical and old-age bit `0x20` as a warning. Command
or checksum failures retain usable measurements but mark the probe partial.
The same pass bounded derived TBW/life arithmetic against finite overflows and
kept unknown power-on time visibly unknown rather than rendering zero.

## 0008.9 · 2026-07-11 · follow-up — cover SATA and reconcile status sources

A final semantics pass corrected two assumptions. Smartctl bit `0x40` means
the device error log contains records and `0x80` means failed self-tests; they
are warning inputs, not ignorable side-query noise. Bit `0x20` is described
conservatively as a usage attribute crossing its threshold, while named ATA
`when_failed` entries preserve whether a specific attribute is failing now or
failed previously. Common SATA wear, host-write, and
`Reported_Uncorrect` attributes now feed the same wear/TBW/warning structure
as NVMe without treating vendor-ambiguous numeric IDs as portable.

The smartctl JSON bitmask is also reconciled with the subprocess return code.
Missing or disagreeing JSON status marks otherwise-usable data partial, and
the union of both sources is retained so malformed output cannot suppress a
health-bearing bit.

## 0008.10 · 2026-07-11 · follow-up — respect ATA attribute class

SMART's `FAILING_NOW` text applies to both pre-failure and old-age attributes;
the attribute's `flags.prefailure` bit determines the class. Treating every
named current threshold as critical made a usage/temperature threshold look
like imminent media failure. Extraction now keeps current pre-failure and
current old-age attribute names separately: pre-failure is critical, usage is
a warning, and prior crossings remain warnings.
