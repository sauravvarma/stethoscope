# Case 0012 — unified diagnosis-aware TUI
status: treated
opened: 2026-07-11
links: issues #5, #9, #13, #29 · case 0006 · case 0011 · ARCHITECTURE.md §8 · supersedes PR #48
touches: core/tui.py, scopes/tui.py, scopes/disk_tui.py, scopes/battery.py, scopes/smart.py, tests/test_tui.py

## 0012.1 · 2026-07-11 · hypothesis

A unified terminal can remain a thin surface if it calls the canonical scope
functions, samples only the active tab, and treats diagnosis as an explicit
cross-scope action. Shared drawing primitives can make resize failures,
untrusted terminal text, color fallbacks, and bounded histories consistent
without moving measurements or classifications into curses code.

## 0012.2 · 2026-07-11 · failure — the historical shell hid degraded states

PR #48 eagerly probed every scope at startup, depended on APIs replaced by the
current data layers, and rendered unknown memory pressure as healthy (Copilot
3546437170). Its SMART heading also placed VERDICT where rows placed LOCATION
(Copilot 3546437218). Narrow terminals allowed the right-aligned clock to
overwrite tabs, and missing optional probes could look like clean empty data.

## 0012.3 · 2026-07-11 · option — keep independent full-screen scope loops

**Merit:** each scope owns a small refresh loop and can optimize its own keys.
**Issues:** navigation, palette, resize safety, diagnosis, and disk actions
would be duplicated. A unified launcher would still need to coordinate five
eagerly constructed applications and their incompatible sampling state.

## 0012.4 · 2026-07-11 · option — one lazy shell over public scope adapters

**Merit:** one global tab and key contract; inactive probes have zero startup
cost; disk's inspect, files, kill, holders, and eject flows remain available;
shared widgets sanitize and clip every write; structured diagnosis can be
shown without parsing CLI text.
**Issues:** the shell owns more presentation state and must carefully discard
failed rate baselines so later deltas cannot be divided by the wrong interval.

## 0012.5 · 2026-07-11 · decision — lazy tabs and explicit canonical triage

Choose 0012.4. `core/tui.py` owns safe drawing, semantic colors, severity
labels, bounded ring histories, sparklines, and popups. `scopes/tui.py` owns
only presentation state. Entering a tab primes or reads that scope; inactive
tabs do not probe. `d` invokes `scopes.anomaly.run("triage")` once, then the
dedicated findings strip supports selection and evidence drill-down. This
keeps diagnosis canonical without turning every live refresh into a blocking
all-scope sample. `scopes/disk_tui.py` remains an executable compatibility
wrapper focused on the disk tab.

## 0012.6 · 2026-07-11 · failure — failed intervals and optional probes looked healthy

Independent review found that disk, CPU, and battery refresh failures retained
the old counters but advanced the timestamp. The next success therefore
divided a multi-interval delta by one interval and inflated every rate. Failed
rate snapshots now discard their baseline and re-prime before ranking.

The same review found that normal memory pressure could mask a failed
`vm_stat`/size probe, and that stale pmenergy coefficients could survive a
failed refresh. Memory availability now takes precedence over pressure health;
battery health and energy-model probes replace state independently on every
read, and missing pmset/model capability is visibly partial. SMART rows reserve
a detail line so warnings cannot overwrite the selected drive.

## 0012.7 · 2026-07-11 · follow-up — terminal behavior is tested at two levels

Hermetic fake-window tests cover lazy probe counts, narrow geometry, degraded
states, finding evidence, SMART column order, disk compatibility routing, and
all recovered-rate baselines. A real pseudo-terminal smoke initializes curses,
draws the unified shell, follows the quit path, and exits cleanly. Color is
never the only signal: every health state has a printable label.
