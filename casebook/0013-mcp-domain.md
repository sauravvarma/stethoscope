# Case 0013 — read-only Model Context Protocol domain
status: treated
opened: 2026-07-11
links: issue #26 · PR #45 · case 0006 · SCHEMA.md
touches: scopes/mcp_server.py, scopes/disk.py, scopes/cpu.py, scopes/memory.py, scopes/battery.py, scopes/smart.py, stethoscope, tests/test_mcp_server.py

## 0013.1 · 2026-07-11 · hypothesis

The stable `stethoscope/1` documents can be exposed to agents without an SDK,
subprocess text parsing, or a second implementation of each probe. A small
stateful JSON-RPC server can map ten read-only MCP tools directly to public
scope result helpers while preserving CLI documents and exit semantics.

## 0013.2 · 2026-07-11 · failure — the historical server trusted malformed input

PR #45 review 3546429234 found that scalar/list messages reached dictionary
operations and that requests and notifications were not distinguished before
dispatch. Review 3546429256 found malformed JSON was silently discarded instead
of receiving JSON-RPC `-32700`, obscuring client faults. The original server was
also stateless: it accepted tools before initialization, hard-coded an obsolete
protocol and package version, accepted coercible argument types, and serialized
tool results with permissive fallback conversion.

## 0013.3 · 2026-07-11 · option — depend on an MCP SDK

**Merit:** an SDK supplies protocol models, lifecycle helpers, and transport
machinery maintained alongside the specification.

**Issues:** it violates the standard-library/ctypes-only installation contract,
may not support system Python 3.9, and would move validation and serialization
behavior outside this repository's audited machine contract.

## 0013.4 · 2026-07-11 · option — strict bounded JSON-RPC over stdio

**Merit:** the transport remains dependency-free and explicit. One session owns
initialization state, request-ID reuse detection, and invocation limits; strict
JSON encoding rejects non-finite/non-serializable values; bounded lines,
arguments, calls, and results constrain resource use. Scope helpers return the
same `(document, exit_code)` consumed by CLI and MCP, so parity is testable.

**Issues:** this repository must maintain protocol validation and advance the
advertised MCP version deliberately. New methods or protocol versions require
explicit compatibility work rather than arriving through an SDK update.

## 0013.5 · 2026-07-11 · decision — bounded lifecycle and read-only tools

Choose 0013.4 and reject 0013.3. Speak MCP `2025-11-25` as UTF-8 newline-delimited
JSON-RPC 2.0. Require `initialize`, then `notifications/initialized`; valid
notifications are silent. Map parse/invalid/method/params/internal failures to
`-32700/-32600/-32601/-32602/-32603`, serialize a complete response before one
stdout write, and continue after message errors.

Expose only disk top/holds/busy, CPU top/wakeups, memory top, battery
health/top, SMART status, and checkup. Exclude recording, battery drainers,
kill/eject, arbitrary history stores, root-heavy inspect, and every other
mutating, destructive, or stateful operation. This limits an agent to current
observation. Process names, PIDs, users, and open paths can still reveal private
activity, so ordinary OS permissions remain the privacy boundary and non-root
partial coverage is preserved rather than bypassed.

## 0013.6 · 2026-07-11 · follow-up — review regressions and parity are executable

Transport tests send malformed JSON followed by a valid request and verify both
the parse error and recovery. Scalar, list, null, malformed envelopes, invalid
notifications, silent valid notifications, lifecycle ordering, IDs, bounds,
strict serialization, one-write flushing, EOF, and subprocess startup are all
covered. Scope tests compare every extracted public result helper with the
existing CLI document builders, keeping the two surfaces on one data path.

## 0013.7 · 2026-07-11 · failure — bounded framing did not bound every resource

Protocol review found that the first strict server still rejected MCP-standard
`_meta` request metadata, lost readable IDs on malformed envelopes, and could
terminate on deeply nested JSON or a lone escaped surrogate. It also retained
every unique request ID forever. Metadata is now accepted only as an object;
valid IDs are preserved on invalid-request errors; recursion is a recoverable
parse error; ASCII-safe JSON encoding and UTF-8 ID validation prevent surrogate
transport failures; and both ID size and session cardinality are bounded.

The same review found that `disk_busy` exposed volume-wide `lsof` and `mount`
probes without deadlines or capture limits. Those fixed-argv subprocesses now
use a selector-driven reader with a 15-second deadline and 4 MiB combined-output
ceiling. Timeout and overflow kill and reap the exact child process, become
stable partial disk documents with exit 4, and leave the MCP session alive.

## 0013.8 · 2026-07-11 · failure — validation happened after expensive conversion

Python 3.9 accepts integer tokens far larger than any interoperable JSON-RPC ID.
The 1 MiB line bound therefore still allowed seconds of big-integer conversion
and hundreds of KiB retained per accepted ID. JSON numeric callbacks now reject
overlong tokens before conversion, request IDs are signed 64-bit integers or
bounded UTF-8 strings, and non-finite overflowed floats are parse errors.

The per-process `disk_holds` path also still used unbounded `capture_output`
after volume-wide `disk_busy` had moved to the bounded runner. Every MCP-exposed
`lsof` invocation now shares the same output and deadline limits, with overflow
or timeout returned as the normal stable disk error document.

## 0013.9 · 2026-07-11 · failure — parser recursion varied across Python builds

The release CI runner's Python 3.14 JSON parser accepted a 2000-container
document that system Python 3.9 rejected with `RecursionError`. The same input
therefore produced invalid request `-32600` in CI but parse error `-32700`
locally, making the protocol contract depend on interpreter implementation.

Strict loading now performs an iterative, version-independent 64-container depth
check after decoding. Inputs beyond that ceiling are recoverable parse errors,
and boundary tests exercise both the accepted limit and the first rejected
depth without relying on the interpreter recursion limit.
