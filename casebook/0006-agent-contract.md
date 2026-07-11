# Case 0006 — stable agent command contract
status: treated
opened: 2026-07-11
links: ARCHITECTURE.md §8–10 · issues 14–17
touches: core/schema.py, core/cli.py, scopes/disk.py, SCHEMA.md, tests/test_contract.py

## 0006.1 · 2026-07-11 · hypothesis

The existing structured data layers can become an agent interface by adding a
JSON switch to each renderer. A common envelope and exit-code table should be
enough for scripts to compose commands safely.

## 0006.2 · 2026-07-11 · failure — omission is itself a false signal

Review of the first disk prototype found five contract failures: three numeric
options accepted zero or negative values, two non-root JSON paths hid their
partial visibility, one error path omitted a normally stable field, and a
human-only command accepted `--json` while emitting text. The prototype also
caught broad import failures, which could replace the original exception with
a misleading fallback import. A syntactically valid document is not a safe
agent input when it silently overstates visibility or changes shape on error.

## 0006.3 · 2026-07-11 · option — parse and shape each command independently

**Merit:** every scope can remain locally simple and expose only what it needs.
**Issues:** option validation, reserved fields, partial visibility, and exit
codes drift immediately; contract fixes must be repeated in every scope.

## 0006.4 · 2026-07-11 · option — one structural envelope and one CLI parser

**Merit:** a schema identifier, stable nulls, strict JSON, positive numeric
validation, and command-specific flag rejection are enforced once. Documents
remain structures until a surface serializes them. **Issues:** the helper must
stay policy-light; scope-specific availability and findings cannot migrate
into a generic output module.

## 0006.5 · 2026-07-11 · decision — share mechanics, keep meaning in scopes

Option 0006.4 shipped. `core/schema.py` builds structures and
`core/cli.py` owns only surface mechanics; disk decides when results are
partial and when holders are findings. Option 0006.3 was rejected because the
review already demonstrated drift within one scope. The schema uses the
architecture's `stethoscope/1` identifier, always carries `partial` and
`partial_reasons`, and rejects unsupported agent flags instead of returning
success-shaped human text.
