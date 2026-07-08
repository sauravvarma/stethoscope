# stethoscope — design language (draft)

*A starting point, not a spec. "Vital signs for your Mac" — the personality is a
clinical instrument: calm, legible, a little clinical-green-on-black, never
alarmist unless something is actually alarming.*

## 1. Current visual language (audit of `disk_tui.py`)

What exists today, as-built:

- **Color pairs** (`curses` pairs 1–6, default terminal background):
  - `C_ACCENT` — cyan. Used for the status sub-bar (system read/write, refresh
    rate, live/paused state).
  - `C_READ` — green, `C_WRITE` — yellow. Wired onto disk read/write
    columns, respectively, so flow direction is visible without changing the
    table shape.
  - `C_BAR` — black-on-cyan. Title bar and footer.
  - `C_SEL` — white-on-blue. Selected row.
  - `C_CRIT` — red. Critical state tier for SMART failing/wear, battery
    service, and memory pressure critical.
- **Layout grammar**: title bar (row 0) → status sub-bar (row 1) → blank →
  column header (row 3, bold) → table body → footer (last row, keys legend).
  Cross-scope tabs live inside the title bar itself, not a separate tab row:
  `[1]disk [2]cpu [3]memory [4]battery [5]smart`. Disk keeps its
  Processes/Volumes subview inside the disk tab.
- **Popups**: `curses.newwin` box-drawn overlay, centered, bold title inline
  in the top border, dimmed hint line at the bottom (`any key to close`).
  Used for read-only detail (held files, volume holders).
- **Prompts**: destructive actions (`kill`, `eject`) reuse the footer message
  slot as an inline `question [y/N]` — no popup, no color escalation. A
  keypress other than `y`/`Y` cancels silently.
- **Typography**: bold for emphasis (column headers, selected row, popup
  title, bar text); dim for de-emphasis (the "not root" notice, popup hint
  line, empty-state text). No italics, no underline — `curses` support for
  either is inconsistent across terminals, so the tool doesn't rely on them.
- **Empty/degraded states**: `(no disk I/O this interval)`, `(no mounted
  volumes)`, `(no on-disk files held — try sudo)` — dimmed, single line,
  plain language, no error styling.

This is a clean, minimal base: five color pairs, two weights (bold/dim), one
popup pattern, one prompt pattern. The gaps are less about what's wrong and
more about what hasn't been decided yet as more scopes arrive.

## 2. Proposed design language

### Semantic color roles (not literal curses pairs — a vocabulary to map onto pairs per-scope)

| Role | Suggested color | Meaning |
|---|---|---|
| `accent` | cyan | Chrome: bars, active tab, sub-bar text. The instrument's "on" light. |
| `ingress` (read / in / charge / inbound) | green | Anything flowing *into* the system or *toward* health. |
| `egress` (write / out / discharge / outbound) | yellow | Anything flowing *out of* the system — not bad, just the other direction. |
| `selection` | blue bg / white fg | Cursor focus. Reserved exclusively for "this row is selected," never reused for status. |
| `healthy` | green | State, not flow — e.g. SMART "good", battery cycle count nominal. |
| `warning` | yellow | Degraded but not urgent — e.g. blocked syscall, battery below 20%. |
| `critical` | red (`C_CRIT`) | Needs attention now — SMART failing / pre-fail / high wear, battery `Service Recommended`, memory pressure `critical`. Reserve red exclusively for this tier so it stays meaningful. |
| `dim` (not a color, a weight) | terminal default + `A_DIM` | Secondary information: hints, disabled-state notices, popup footers. |

Decision: **green/yellow shift by scope, but not within one row.** `disk` uses
green/yellow only for read/write flow columns; memory, battery, and SMART use
green/yellow/red for state tiers in their status/verdict cells. The shell has
no mixed overview screen, so the roles remain legible. If an overview is added,
it must include non-color labels such as READ/WRITE and OK/WARN/CRITICAL.

### Typography-in-terminal rules

- **Bold** = "read this first": column headers, the active tab label, the
  selected row, values that just crossed into a warning/critical tier.
- **Dim** = "context, not content": empty states, footnotes, hints,
  timestamps, anything true but not actionable.
- **Plain weight** = the default, steady-state data. Most of the table.
- Never bold *and* colored red/yellow for the same cell unless it's a
  critical-tier alert — bold is cheap, so it must stay rare or it stops
  meaning "look here."

### Layout grammar (for scopes beyond `disk`)

```
┌ row 0: title bar ── scope name · [tab list] ──────────── mode · clock ┐
│ row 1: status sub-bar ── one line of live aggregate stats ────────────│
│ row 2: (blank spacer)                                                 │
│ row 3: column header (bold)                                           │
│ rows 4..n-2: table body (selection = blue bg, else plain)             │
│ row n-1: footer ── key legend, or inline confirm/message ─────────────│
└─────────────────────────────────────────────────────────────────────┘
```

- **Tab bar stays inside row 0**, not a dedicated row — screen real estate on
  a terminal is precious. The settled grammar is
  `[1]disk [2]cpu [3]memory [4]battery [5]smart`; number keys switch
  scopes, and `Tab` switches disk's Processes/Volumes subview.
- **Status sub-bar (row 1)** is the scope's "vitals at a glance" line — one
  line, always visible, no scrolling. Every scope should have exactly one.
- **Popups** stay the single mechanism for "more detail on the current
  selection" — box-drawn, centered, bold title in the border, dimmed footer
  hint. Don't invent a second popup style; if a scope needs richer detail
  than a popup can hold, that's a case for a dedicated full-screen view
  (like `inspect` dropping to a streamed sub-process), not a bigger popup.
- **Footer** is overloaded today (keys legend / inline message / confirm
  prompt share one line) — that's fine at this density; keep it a single
  line and let states take turns rather than stacking.

### Interaction conventions

- **Read-only actions** (view files, view holders, inspect) — no
  confirmation, instant popup or mode switch.
- **Destructive / irreversible actions** (kill, eject, and future ones like
  "clear SMART history" or "force-unmount") — always the inline
  `question? [y/N]` pattern in the footer, defaulting to No, any non-y/Y
  key cancels. Keep this pattern rather than a popup for confirmations: it's
  lower-ceremony and matches "instrument," not "installer wizard."
- **Escalating confirmations**: if a future action is destructive *and*
  system-critical (e.g. killing a root/daemon process), consider requiring
  the pid/volume name to be echoed in the prompt text (already true for
  `kill`/`eject` today) plus bolding the target name in `critical` color —
  not a second keypress, just a stronger visual cue before the same y/N.

## 3. Decisions from PR-05

1. **Flow vs. state color collision.** There is no combined overview screen in
   PR-05. Flow colors appear only in disk read/write columns; state colors appear
   in scope status/verdict cells. Non-color text labels remain present.
2. **`C_READ`/`C_WRITE` are wired.** Disk read-rate cells are green and
   write-rate cells are yellow, with selection color taking precedence.
3. **Red debuts as `C_CRIT`.** Critical memory pressure, battery `Service
   Recommended`, and SMART critical/failing drives use red.
4. **The TUI is cross-scope.** `stethoscope tui` is the primary shell with five
   title-bar tabs. `stethoscope disk tui` remains as a compatibility route into
   the same shell, focused on disk.
5. **Sparklines stay out of PR-05.** Tables remain instantaneous; trend cues can
   be introduced later without spending status-bar width now.
6. **Monochrome remains information-complete.** Every colored state also has
   text (`READ/s`, `WRITE/s`, `WARN`, `CRITICAL`, pressure names), so color is
   enhancement, not the only signal.
