# stethoscope unified TUI design language

This is the shipped design contract for `core/tui.py` and `scopes/tui.py`.
`scopes/disk_tui.py` is only a compatibility wrapper into the same five-tab
shell. The personality is a clinical instrument: calm, dense, explicit about
uncertainty, and urgent only for a real critical state.

## 1. Semantic palette

`core.tui.Palette` maps roles to curses pairs with the terminal background:

| Role | Default | Meaning |
|---|---|---|
| `accent` | cyan | live status and instrument chrome |
| `read` | green | shared disk-ingress vocabulary; not applied per cell in 0.2 |
| `write` | yellow | shared disk-egress vocabulary; not applied per cell in 0.2 |
| `bar` | black on cyan | title and footer bars |
| `selection` | white on blue | selected table row only |
| `healthy` | green | observed healthy state |
| `warn` | yellow | degraded/advisory state |
| `critical` | red | action-worthy critical state |
| `unknown` | magenta | unknown, partial, absent, or failed evidence |

Color never carries state alone. Every health state is also rendered as
`[HEALTHY]`, `[INFO]`, `[WARN]`, `[CRITICAL]`, `[UNKNOWN]`, `[ABSENT]`,
`[ERROR]`, or `[PARTIAL]`. If curses has no colors, all content remains
information-complete. Bold means focus/critical emphasis; dim means secondary
or empty-state text. The UI does not depend on italic or underline support.

Flow color and health color may share hues because labels and columns supply
non-color semantics. A selected row always uses the selection role, so cursor
focus cannot be confused with health.

The current row renderer applies one attribute to a complete row, so disk
read/write cells remain monochrome rather than incorrectly coloring both flows
with one role. `READ/s` and `WRITE/s` headings provide the distinction. The
roles stay available for a future cell-aware renderer.

## 2. Global navigation

The shell has exactly five global tabs:

1. disk
2. CPU
3. memory
4. battery
5. drives (SMART)

Numeric keys select directly; Tab and Shift-Tab cycle. Arrow keys or `j`/`k`
move the active table selection. `p`/Space pauses, `+`/`-` changes the interval
between 0.5 and 10 seconds, and `q`/Esc quits. Tab selection and table selection
are retained independently.

Diagnosis is explicit, not a hidden periodic cost. Pressing `d` runs canonical
triage once and focuses the findings strip. `[`/`]` changes the selected
finding. Enter while findings are focused opens its evidence and drill-down
commands. Esc first leaves findings focus.

## 3. Layout grammar

```text
row 0       title bar: stethoscope, five tabs, root/user, clock
row 1       active-tab aggregate status and bounded sparkline
row 2       canonical findings strip
row 3       non-root/partial visibility notice when applicable
row 4       active table's bold column headings
rows 5..n-2 scrollable table body or explicit empty/degraded text
row n-1     context key legend, probe error, message, or [y/N] prompt
```

The findings row is always present. Before diagnosis it says to press `d`;
afterward it shows healthy, partial/error, or one indexed finding. This prevents
"not sampled" from looking like "no findings."

The footer is a single state slot. The normal key legend yields to the active
probe error, action result, or confirmation prompt rather than stacking rows and
shrinking data.

## 4. Tab content

- **Disk:** process rows show PID, command, read/write rates, and cumulative
  bytes. `v` switches to the volume subview. Process actions are files
  (`Enter`/`f`), streamed inspect (`i`), and confirmed SIGTERM (`x`). Volume
  actions are holders (`Enter`/`r`) and confirmed unmount (`e`).
- **CPU:** rows show CPU/user/system, real watts where available, wakeup rates,
  lifetime CPU, and duty.
- **Memory:** status labels pressure without treating unknown as healthy; rows
  rank footprint and resident size.
- **Battery:** status labels health/presence/partial model state and keeps real
  watts separate from unitless score; rows rank attribution.
- **Drives:** rows align device, model, location, verdict, wear, and temperature.
  The status distinguishes failed enumeration, no physical drives, optional
  detail gaps, warnings, and critical findings.

Disk is the only tab with a nested process/volume subview and mutating actions.
The global `1`-`5` keys never become local disk subview keys.

## 5. Sampling and degraded states

Only the active tab probes. Entering a tab initializes its data, so inactive
disk/CPU/battery delta snapshots do not age into misleading rates. Returning to
the disk process view re-primes it. Volume data is loaded lazily. SMART refresh
is clamped to at least five seconds. Pause freezes sampling, not navigation.

Failures are local to a tab and do not terminate the shell. The UI distinguishes:

- non-root visibility: `[PARTIAL]`, with hidden-process explanation;
- no battery or no physical drives: `[ABSENT]`, not healthy;
- unavailable pressure or health: `[UNKNOWN]`;
- failed native probe: `[ERROR]`/explicit footer detail;
- optional smartctl or energy-model gaps: `[PARTIAL]`;
- flavor-6 watts unavailable: `-`, never `0`;
- no interval activity: a dim, plain-language empty row.

External text is sanitized before rendering. All drawing is clipped through
`safe_addstr`; resize and lower-right curses errors do not crash the interface.

## 6. Narrow terminals

The title chooses progressively shorter forms: full five-tab title with
root/user and clock, active-tab-only title, active token, then `stethoscope`.
If none fits, it clips safely. Status, findings, headers, rows, and footer are
also clipped rather than wrapped into neighboring rows.

Tables calculate body capacity from rows 5 through the line above the footer and
scroll to keep the selected row visible. A popup requires at least 5 rows by 10
columns; otherwise the footer reports `screen too small for popup`. This is an
explicit degraded state, not an uncaught curses exception.

## 7. Popups, streams, and prompts

Read-only detail uses one centered boxed popup pattern: title in the border,
bounded body, omitted-line count, and `any key to close` footer. Findings, held
files, and volume holders use it.

Disk inspect is too dynamic for a popup. The shell saves curses state, clears the
screen, streams `fs_usage`, waits for return, then restores the shell. Failures
are returned to the footer.

Destructive actions use an inline footer prompt containing the exact PID/name or
volume: `question [y/N]`. Only `y`/`Y` confirms; every other key defaults to No.
Kill sends SIGTERM. Eject currently invokes `diskutil unmount`.

## 8. Resolved and future questions

Resolved in 0.2:

- Non-color accessibility is complete through labels, headings, and units.
- Red is reserved for explicit critical state.
- Read/write is information-complete through headings; per-cell flow color is
  intentionally deferred until the row renderer supports mixed attributes.
- One centralized shell replaces per-scope TUIs.
- Findings are opt-in through `d`, never an unexplained background probe.
- Narrow screens clip and degrade explicitly.

Genuinely unresolved future enhancements:

- horizontal column reduction for extremely narrow but otherwise usable screens;
- scrollable popup bodies rather than the current bounded omitted-line summary;
- configurable key bindings or refresh bounds;
- richer per-row trend cues beyond the current aggregate sparklines.

These are not shipped controls. Any implementation must retain non-color labels,
lazy active-tab sampling, explicit degraded states, and confirmation defaults.

Related references: [README.md](README.md), [SCHEMA.md](SCHEMA.md),
[ARCHITECTURE.md](ARCHITECTURE.md), the
[agent walkthrough](docs/agent-walkthrough.md), and
[review dispositions](docs/review-disposition.md).
