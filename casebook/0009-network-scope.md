# Case 0009 — network scope: the fifth suspect no scope can clear
status: open
opened: 2026-09-05
links: case 0007 · case 0008 · README.md roadmap · ARCHITECTURE.md §3 · Appendix A S3
touches: (none yet: no probe reads the network)

## 0009.1 · 2026-09-05 · hypothesis

Opened from case 0008. The operator named five suspects for a thermal
symptom: CPU, GPU, memory, internet, I/O. Four had a scope shipped or on the
roadmap. Network had nothing at any milestone, and was cleared by hand with
`nettop -P -l 2 -J bytes_in,bytes_out` (about 63 MB in over the session, all
attributable to the agent's own traffic). Had the answer been a sync flood or
a radio, stethoscope would have had no surface for it, the same shape as the
GPU gap 0008.2 records.

There is also a diagnostic reason beyond completeness. 0007.4's cause was an
A2DP audio stream over Bluetooth: radio-side energy that no CPU, disk or
wakeup rate expressed, found only through an assertion. Network is the other
radio. "Why is this draining" will keep landing here.

**The bet.** Per-process network attribution is obtainable without root and
cheaply enough to poll, from surfaces that ship with the OS. Candidates,
with what is known today:

| Surface | Yields | Known cost / gap |
|---|---|---|
| `nettop -P -l 1 -J bytes_in,bytes_out` | per-process bytes in/out, no root; 21 process rows on this machine unprivileged | **5.0 s per call** measured x3 at the default sample interval; `-s` may shorten it, untested. Interval-bound, not CPU-bound |
| `proc_pid_rusage` | nothing: no network fields in any flavor | confirmed by `core.validate`: Apple's own Energy Impact formula wants `knetwork_recv_bytes` / `knetwork_sent_bytes` / packets and rusage cannot supply them (Appendix A S3) |
| `proc_pidinfo(PROC_PIDFDSOCKETINFO)` | per-socket state for a pid's descriptors: family, addresses, queue sizes | connections and their peers, not byte rates; would need the full `socket_fdinfo` struct declared (case 0003 rule) |
| `netstat -ib` | per-interface bytes: system truth, like `watts_now` is for battery | cost unmeasured; no process dimension |
| IOReport | unknown whether network/radio energy channels are published | worth a channel-group survey now that `core.ioreport` exists (0008.11) |

Vitals sketch, following the battery scope's split (§4): system truth
(per-interface bytes/s, interface and radio state) and process attribution
(bytes in/out per process, connection count, peers). The join that matters
for diagnosis is with the assertions probe (0007.7): a process holding a
network-related assertion while moving bytes is a different finding from one
merely moving bytes.

**What would move this to `diagnosed`.** Measurements, not opinions: `nettop`
at `-s 1` and its row count under root vs not; `netstat -ib` cost; whether
IOReport publishes anything network-side; and whether any surface yields a
per-process rate at 1 s cadence, which is the property that made
`accumulatedGPUTime` usable (0008.11) and `ri_billed_energy` unusable
(0001.10).

Out of scope: packet capture, DNS, anything requiring `tcpdump` or a network
extension. This is a rate-and-holder scope like `disk` and `gpu`, not a
sniffer.
