# stethoscope — design language (draft)

*A starting point, not a spec. "Vital signs for your Mac" — the personality is a
clinical instrument: calm, legible, a little clinical-green-on-black, never
alarmist unless something is actually alarming.*

## 1. Current visual language (audit of `disk_tui.py`)

What exists today, as-built:

- **Color pairs** (`curses` pairs 1–5, default terminal background):
  - `C_ACCENT` — cyan. Used for the status sub-bar (system read/write, refresh
    rate, live/paused state).
  - `C_READ` — green, `C_WRITE` — yellow. **Defined but currently unused** —
    no code path applies them to the read/write columns. Worth noting as the
    single biggest color gap: the read-vs-write semantic exists in the
    palette but not on screen yet.
  - `C_BAR` — black-on-cyan. Title bar and footer.
  - `C_SEL` — white-on-blue. Selected row.
- **Layout grammar**: title bar (row 0) → status sub-bar (row 1) → blank →
  column header (row 3, bold) → table body → footer (last row, keys legend).
  Two tabs (`Processes` / `Volumes`) live inside the title bar itself, not a
  separate tab row.
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
| `critical` | red (new) | Needs attention now — e.g. disk near-full, SMART pre-fail, thermal throttle. Currently **no scope uses red**; reserve it exclusively for this tier so it stays meaningful. |
| `dim` (not a color, a weight) | terminal default + `A_DIM` | Secondary information: hints, disabled-state notices, popup footers. |

Key proposal: **green/yellow's meaning should shift with context but never
overload two meanings on one screen.** `disk`'s table uses green/yellow for
read/write (a flow). A future `battery` scope's health line would use the
same green/yellow/red for state tiers, but the two never appear in the same
view, so the color stays unambiguous. If that turns out to be false (a
combined dashboard view mixes both), we need a fourth pair of hues for state
so flow and health are visually distinct — flagged below as an open question.

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
  a terminal is precious, and `disk`'s pattern of folding tabs into the title
  bar should generalize: `[1]disk [2]cpu [3]memory [4]battery [5]smart`.
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

## 3. Open questions

1. **Flow vs. state color collision.** If a future combined/overview screen
   shows read/write flow *and* health tiers side by side, green/yellow will
   mean two different things at once. Do we need a second hue pair for
   state (e.g. blue-green/orange) or a non-color cue (icons like `●`/`▲`)?
2. **Should `C_READ`/`C_WRITE` actually get wired up** in the disk table
   (currently defined, unused), or was leaving read/write monochrome a
   deliberate low-noise choice worth keeping as-is?
3. **Red's debut.** No scope uses red yet. Which is the first real
   "critical" signal — a SMART pre-fail flag, a >90% full disk, a
   battery health "Service Recommended"? Worth prototyping red on one
   real case before it becomes a system-wide convention.
4. **Cross-scope tab bar** — once `cpu`/`memory`/`battery`/`smart` exist,
   does `disk tui` become `stethoscope tui` with all scopes as tabs, or do
   scopes stay independently launched? Changes whether row 0 needs to hold
   5 tab labels instead of 2.
5. **Sparklines / trend cues.** Everything today is instantaneous or
   cumulative. Does a "vitals" tool need a tiny inline trend (e.g. last 10
   samples as a sparkline) for read/write or battery drain, and if so where
   does it fit without breaking the one-line status bar?
6. **Non-color accessibility fallback.** `curses.has_colors()` is already
   checked, but is monochrome-mode information-complete (e.g. can a
   colorblind or `TERM=dumb` user still tell read from write, healthy from
   critical) without color as the only signal?
