# For AI agents: an anomaly-resolution walkthrough

stethoscope's probes are designed to be **primitives an agent can reason over**,
not screens a human watches. Every command takes `--json` and emits a structured
document straight from the data layer (see [SCHEMA.md](../SCHEMA.md)); every
probe returns a [meaningful exit code](../SCHEMA.md#exit-codes-all-scopes); and
`stethoscope mcp` exposes all of it as [MCP tools](#appendix-the-mcp-surface).

This is the intended loop, worked end to end:

> **notice a symptom → call the relevant probes → get structured vitals →
> correlate across scopes → propose a diagnosis with the exact command to
> confirm it.**

---

## Worked example: "the battery is draining fast"

### 1. Start with the cheapest whole-machine read

```sh
stethoscope checkup --json
```

`checkup` samples every scope once and returns a verdict plus findings, each
with a drill-down command:

```json
{
  "schema": 1, "scope": "checkup", "command": "checkup", "overall": "warn",
  "findings": [
    {"severity": "info", "area": "cpu",
     "message": "avconferenced (pid 1653) is using 96% CPU",
     "drill": "stethoscope cpu top"},
    {"severity": "warn", "area": "battery",
     "message": "battery condition: Service Recommended (health 78%, 900 cycles)",
     "drill": "stethoscope battery health"}
  ],
  "vitals": {
    "cpu": {"system_cpu_pct": 142.0, "ncpu": 8, "top": {"pid": 1653, "name": "avconferenced", "cpu_pct": 96.0}},
    "memory": {"pressure": "normal", "used_pct": 61.0},
    "battery": {"present": true, "charge_pct": 44, "health_pct": 78, "condition": "Service Recommended"},
    "smart": {"drives": [{"device": "disk0", "smart_status": "verified", "worst_severity": "ok"}]}
  }
}
```

Two things stand out: a process pegging a core, and a battery that's aged. The
CPU note is what explains *fast drain right now*; the battery condition explains
*why the ceiling is lower than it used to be*. Follow the drain first.

### 2. Confirm the drain and attribute it

```sh
stethoscope battery top --once --json
```

```json
{
  "schema": 1, "scope": "battery", "command": "top",
  "processes": [
    {"pid": 1653, "name": "avconferenced", "energy_score": 61.7,
     "cpu_pct": 4.1, "idle_wakeups_per_s": 0.0, "interrupt_wakeups_per_s": 288.0}
  ]
}
```

The energy score is dominated not by CPU% but by **interrupt wakeups** — 288/s.
That's the tell: this process is battery-hostile through a wakeup storm, not raw
compute. Cross-check the wakeups directly:

```sh
stethoscope cpu wakeups --once --json --limit 3
```

```json
{
  "schema": 1, "scope": "cpu", "command": "top",
  "processes": [
    {"pid": 1653, "name": "avconferenced", "cpu_pct": 4.1,
     "wakeups_per_s": 288.0, "idle_wakeups_per_s": 0.0, "interrupt_wakeups_per_s": 288.0}
  ]
}
```

### 3. Correlate across scopes

The same pid (1653) is the top energy consumer **and** the top waker, while
memory pressure is normal and SMART is clean. The signals agree: a single
process is driving the drain via a wakeup storm. The aged battery
(`Service Recommended`, health 78%) is a *separate, slower* finding — real, but
not the cause of the fast drain.

### 4. Diagnosis + the command to confirm

> **Diagnosis:** `avconferenced` (pid 1653) is causing the fast drain via a
> ~288/s interrupt-wakeup storm (energy score 61.7, far above baseline CPU).
> Secondary: battery health is at 78% with 900 cycles (`Service Recommended`) —
> worth planning a service, but not the acute cause.
>
> **Confirm / act:**
> ```sh
> stethoscope battery drainers          # cumulative impact since unplug
> stethoscope cpu wakeups                # watch the wakeup rate live
> ```

`battery drainers` is the clincher: it ranks *cumulative* energy since you
unplugged, so a process that has been quietly waking the CPU for an hour rises
to the top even if this instant looks calm.

---

## A second loop: "the disk is constantly busy"

```sh
sudo stethoscope disk top --once --json          # who — ranked by bytes/sec
sudo stethoscope disk inspect 1263                # why — live fs_usage, W = blocked on I/O
stethoscope disk holds 1263 --json                # what files it holds open
```

If the symptom is instead "a volume won't eject":

```sh
sudo stethoscope disk busy "/Volumes/X9 Pro" --json
```

exits `1` when holders exist and returns each holder with the *reason* it's
pinning the volume (`cwd`, `txt`/`mem`, an open fd) — so an agent can decide
whether to quit an app, `kill` a pid, or `diskutil unmount force`.

---

## Why this composes

- **One data layer, two surfaces.** The `--json` document and the human table are
  the same function's output, so what you diagnose from JSON is exactly what a
  human would see.
- **Exit codes are signals.** `busy` (holders), `memory watch` (leak),
  `smart`/`checkup` (critical) all exit non-zero, so probes drop into `if`
  statements and CI without parsing.
- **Cross-scope correlation is the point.** Rising memory slope *and* a wakeup
  storm on the same pid is a stronger diagnosis than either alone — and every
  scope keys on the same `pid`.

## Appendix: the MCP surface

```sh
stethoscope mcp      # newline-delimited JSON-RPC 2.0 over stdio
```

Ten tools, each returning the `--json` document above as MCP `structuredContent`:
`disk_top`, `disk_holds`, `disk_busy`, `cpu_top`, `cpu_wakeups`, `memory_top`,
`battery_health`, `battery_top`, `smart_status`, `checkup`. Point an MCP client
at the command and the loop above becomes tool calls the model makes directly.
