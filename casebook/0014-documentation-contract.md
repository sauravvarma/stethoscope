# Case 0014 — shipped documentation and review traceability
status: treated
opened: 2026-07-11
links: PR #46 · README.md · SCHEMA.md · man/stethoscope.1 · docs/agent-walkthrough.md · docs/review-disposition.md
touches: README.md, SCHEMA.md, ARCHITECTURE.md, DESIGN.md, man/stethoscope.1, docs/agent-walkthrough.md, docs/review-disposition.md

## 0014.1 · 2026-07-11 · hypothesis

Documentation is part of the command contract when humans and agents choose
privilege, flags, exit handling, and follow-up probes from it. A roadmap-shaped
README or speculative architecture can be more dangerous than missing prose:
it can advertise unavailable installation paths, hide shipped capabilities, or
cause an agent to mix real watts with a unitless score.

## 0014.2 · 2026-07-11 · failure — historical docs mixed releases and proposals

The pre-consolidation README still called memory, battery, SMART, JSON,
recording, triage, and the unified TUI future work. Architecture named modules
that do not exist and treated per-scope TUIs, watch/alerts, and MCP as one future
build step. The old design audit described only the disk shell. PR #46 artifacts
were absent, and its review had already identified a dead checkup link, missing
sampling options in a man synopsis, and a mislabeled wakeups document.

Separately, an earlier audit assumed MCP would be omitted. The shipped
`scopes/mcp_server.py` and its protocol tests invalidate that assumption: MCP is
now a current, bounded, read-only surface and must be documented as such.

## 0014.3 · 2026-07-11 · option — retain one aspirational narrative

**Merit:** one document can explain the long-term vision and preserve extensive
probe/statistical research without duplication.

**Issues:** readers cannot reliably distinguish executable behavior from an
idea. Command names, modules, flags, state ownership, and security boundaries
drift independently, and stale prose can become false operational guidance.

## 0014.4 · 2026-07-11 · option — contract-first docs with a review ledger

**Merit:** README and man source describe shipped help; SCHEMA remains the
machine authority; the walkthrough uses strict schema-derived examples; and a
finite ledger ties every inline concern to current evidence. Architecture can
retain deep rationale while labeling future research explicitly.

**Issues:** every command or schema change must update several cross-linked
surfaces. Examples and roff need validation, and the review ledger must remain a
closed historical set rather than an informal issue tracker.

## 0014.5 · 2026-07-11 · decision — documentation is a tested release surface

Choose 0014.4 and reject 0014.3. Separate shipped behavior from future packaging,
background cross-scope watcher/alerts, and advanced detector ideas in every
overview. Treat the
distribution-ready man source and strict walkthrough JSON examples as release
artifacts derived from source help and SCHEMA, not marketing prose.

Keep the 34-comment Copilot ledger complete and immutable in membership: each
row records PR, permanent comment link, concern, and final evidence. This makes
review disposition auditable even when stacked PR threads are superseded.

## 0014.6 · 2026-07-11 · follow-up — cross-links and validation close the loop

README, SCHEMA, man source, walkthrough, architecture, design, casebook, and
review ledger now link to each other. The checkup link has a real anchor; man
synopses are command-specific; the CPU wakeups example preserves command
`wakeups`; MCP is documented as ten read-only tools with its privacy boundary.
Casebook check/index, strict JSON parsing, roff rendering checks, link inspection,
tests, and `git diff --check` are the verification path for this treatment.
