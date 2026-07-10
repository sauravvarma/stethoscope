# The casebook

**How stethoscope documents its own diagnoses.** Every nontrivial piece of
work — a new scope, a probe bet, a statistical method, a design reversal —
gets a **case**: the original hypothesis, what failed, the treatments
considered with their merits and issues, the prescription chosen and why,
and every follow-up that later touches the same problem. The tool diagnoses
machines; the casebook diagnoses the tool.

## Why this shape

- **One case = one problem node, forever.** Improvements to the same problem
  months apart append entries to the same file — the node accumulates
  history instead of scattering it across PRs, commit messages, and doc
  revisions. `git log --follow` gives the when; the case gives the why.
- **Append-only entries.** Past entries are never rewritten (a correction is
  a new entry that cites the one it corrects). The only mutable line in a
  case is its `status:` header, and a status change always lands together
  with the entry that justifies it.
- **Plain markdown, in-repo.** Version controlled by the same commits that
  change the code, diffable, greppable, Obsidian-linkable.
- **Idempotent tooling.** `casebook.py index` regenerates `INDEX.md`
  deterministically from case headers — run it twice, nothing changes.
  `casebook.py check` validates the invariants below and is CI-able.

## Case file format

One file per case: `NNNN-slug.md`. Header, then entries:

```markdown
# Case 0001 — per-process energy attribution
status: treated
opened: 2026-07-10
links: case 0002 · ARCHITECTURE.md §4–5 · Appendix A S1/S3/S4
touches: core/rusage.py, scopes/battery.py (future)

## 0001.1 · 2026-07-09 · hypothesis
...

## 0001.2 · 2026-07-10 · failure — billed_energy is frozen at 1 s
...
```

**Statuses** (lifecycle): `open` → `diagnosed` (failures understood, no fix
chosen) → `treated` (prescription applied) → `closed` (verified over time).
`reopened` when a follow-up invalidates the treatment — the case keeps its
number and its history.

**Entry kinds** — every entry heading is `## NNNN.k · YYYY-MM-DD · kind`
with an optional ` — short title`; `k` is sequential from 1 and never
reused:

| kind | contains |
|---|---|
| `hypothesis` | what we believed and why we believed it |
| `failure` | observed evidence against — with the numbers, and how they were obtained |
| `option` | one treatment considered: its **merit** and its **issues**, both stated |
| `decision` | the prescription: which option(s) chosen, why, and what invariant it preserves |
| `follow-up` | later work on the same node: verification, new evidence, extension, or the trigger for `reopened` |

## Rules

1. Open or append a case **with** the change, not after it. If the work has
   a hypothesis worth testing, the hypothesis entry exists before the code.
2. Never edit a past entry. Corrections cite: *"contradicts 0001.3: …"*.
3. Every `decision` names the options it rejected. A decision with one
   option listed is a red flag in review.
4. Numbers over adjectives: a `failure` entry without measured evidence is
   an opinion, and belongs in the entry that produces the evidence.
5. Cross-link both ways: cases reference ARCHITECTURE.md sections and
   Appendix A finding IDs; code comments cite cases where the constraint is
   non-obvious (`# never a prefix struct — casebook 0003`).
6. After any casebook change: `python3 casebook/casebook.py check index`.

## The tool

```
python3 casebook/casebook.py check          validate all invariants (exit 1 on violation)
python3 casebook/casebook.py index          regenerate INDEX.md (idempotent)
python3 casebook/casebook.py new <slug> -t "Title"     create the next-numbered case
python3 casebook/casebook.py append <NNNN> <kind>      add the next entry skeleton
```

No third-party dependencies — system Python 3 only, like everything here.
