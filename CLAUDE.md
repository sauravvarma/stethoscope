# stethoscope — contributor instructions

macOS machine-health observability. Read README.md for what it does,
ARCHITECTURE.md for the layer model, DESIGN.md for the visual language.

## Hard constraints

- System Python 3 + stdlib + ctypes only. Zero third-party dependencies.
- Structures, never text, below the surface layer (ARCHITECTURE.md §2).
- Never declare a prefix of a kernel struct; never read mach-abstime fields
  as nanoseconds — all rusage access goes through `core/rusage.py`
  (casebook 0003).

## The casebook (mandatory)

Every nontrivial piece of work — new scope, probe, statistical method,
design reversal, bug whose fix changes a design assumption — is documented
in `casebook/` as it happens: hypothesis, failures (with measured numbers),
options considered (merit AND issues), the decision and why, follow-ups.
Format and rules: `casebook/README.md`.

- One case per problem node; later work on the same problem **appends
  entries to the existing case**, never opens a duplicate.
- Open the case with the change, not after. Append-only; corrections are
  new entries citing the old.
- Tooling: `python3 casebook/casebook.py new <slug> -t "Title"` /
  `append <NNNN> <kind>`; always finish with
  `python3 casebook/casebook.py check index`.
- Cross-link: cases cite ARCHITECTURE.md sections / Appendix A finding IDs;
  non-obvious constraints in code cite their case (`# casebook 0003`).

## Verification before any change is "done"

- `python3 -m unittest discover tests`
- `python3 -m core.validate` (probe contracts on the running machine)
- disk-scope regression: `./stethoscope disk top` output shape unchanged
- `python3 casebook/casebook.py check index`
