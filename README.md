# stethoscope

**Interoception for your Mac** — a machine-health observability layer built on macOS's native introspection surfaces.

Interoception is the sense of your own internal body state. stethoscope gives that sense to a machine (and to the human or AI agent examining it): structured probes over CPU, memory, disk, I/O, battery, and drive health, presented for humans as a CLI/TUI and designed to be consumable as primitives by an agent reasoning about system health.

Each subsystem is a **scope** — a command namespace backed by a reusable data layer:

| Scope | Status | What it examines |
|---|---|---|
| `disk` | **shipped** | per-process disk I/O, blocked syscalls, open-file holds, eject blockers |
| `cpu` | planned | who's pegging cores, wakeups, scheduling pressure |
| `memory` | planned | footprint, memory pressure, leak candidates over time |
| `battery` | planned | energy impact per process — what's draining you |
| `smart` | planned | drive health, SMART data, external-drive life expectancy |

On top of the scopes, the roadmap adds **recording** (sample vitals over time), **anomaly detection** (flag what deviates from the machine's own baseline), and **`--json` everywhere** so an agent can call any probe and reason over the result.

No third-party dependencies — system Python 3 + `ctypes`.

```sh
sudo ./stethoscope disk top                     # who is doing disk I/O right now
sudo ./stethoscope disk inspect 12345           # why — live syscall trace of one pid
./stethoscope disk holds 12345                  # what files a process holds open
sudo ./stethoscope disk busy "/Volumes/X9 Pro"  # which pids won't let it eject
sudo -E ./stethoscope disk tui                  # full-screen interactive view
```

---

## The `disk` scope

Answers four questions, broad → narrow:

| Command | Question | Needs sudo? |
|---|---|---|
| `disk top` | **Who** is doing disk I/O right now? | recommended (else only your own processes) |
| `disk inspect <pid>` | **Why** — what paths, reads vs writes, is it blocking? | yes |
| `disk holds <pid>` | **What** files is a process holding open? | for other users' processes |
| `disk busy <volume>` | **Which** pids are pinning a disk? (reverse lookup) | recommended (else system daemons hidden) |

### Interactive TUI

`disk tui` is a curses front-end over the same building blocks — it re-implements *nothing* about disk I/O, it just presents the disk scope's functions:

```sh
sudo -E ./stethoscope disk tui     # -E preserves $TERM so curses can start
```

> `sudo` strips `$TERM` by default, which makes curses fail with
> `setupterm: could not find terminal`. Use `sudo -E`, or just run it — the tool
> falls back to `TERM=xterm-256color` when `$TERM` is missing.

| Panel | Backed by | |
|---|---|---|
| **Processes** | `snapshot_diskio` + `rank_io` | live ranked per-process I/O (the `top` view, navigable) |
| **Volumes** → holders | `_mount_table` + `resolve_volume` + `collect_holders` | reverse lookup (the `busy` view) in a popup |
| held-files popup | `open_files` | a process's on-disk holds |
| inspect | `cmd_inspect` | suspends the TUI and streams `fs_usage` |

Keys: `↑↓`/`jk` move · `1`/`2`/`Tab` switch view · `p`/space pause · `+`/`-` refresh rate · `q` quit. In **Processes**: `Enter`/`f` held files, `i` inspect, `x` kill (confirm). In **Volumes**: `Enter`/`r` holders, `e` eject (confirm). It runs at 200 ms input polling so keys stay snappy while I/O samples at the refresh interval.

The CLI and the TUI share one data layer (`scopes/disk.py`): `snapshot_diskio`, `rank_io`, `collect_holders`, `open_files`, `resolve_volume`, `proc_name`, `human`/`rate`. Fix a number in one place, both surfaces update.

### How macOS exposes disk I/O, and why this scope is built the way it is

A process doesn't touch the disk directly. It issues a **syscall** (`read`/`write`/`pread`/`fsync`…), which enters the **VFS** layer, usually hits the **unified buffer cache** (so most "reads" and "writes" never reach the device), and only on a cache miss or flush does the kernel enqueue a **block I/O request** to the storage driver, which the device completes asynchronously. Attributing physical disk activity back to a process means catching it at one of these layers. macOS gives three practical vantage points:

**1. `proc_pid_rusage()` — the spine of this scope (`top`).**
The kernel keeps a running tally per process: `ri_diskio_bytesread` and `ri_diskio_byteswritten` — cumulative bytes that were *charged to that process* as real device I/O. This is the exact number Activity Monitor's "Bytes Read/Written" column shows. We poll it across every pid (via `proc_listpids`) and diff between samples to get bytes/sec. It needs no tracing framework, survives **SIP**, and is cheap. Reading *other* users' processes requires root, hence `sudo`.

**2. `fs_usage` — the "why" (`inspect`).**
Apple's supported syscall tracer. For one pid it streams every filesystem operation with the **path**, **byte count**, **elapsed time**, and — critically — a **`W`** marker when the call *blocked* (the thread was scheduled off-CPU waiting on I/O). That `W` is your "process is holding / stalled on I/O" signal. Needs root.

**3. `lsof` — the "what's held" (`holds`) and the reverse lookup (`busy`).**
Every file a process has open is an entry in its file-descriptor table. `lsof` enumerates them; `disk holds` highlights the regular files and directories — the actual on-disk objects the process is keeping open.

The reverse direction — *given a volume, which pids are pinning it* — is the "why won't this eject" problem. Passing a **mount point** to `lsof` makes it list every open file on that filesystem, and the FD column tells you the *reason* for each hold:

| FD column | Meaning |
|---|---|
| `cwd` | a process's working directory is on the volume (a very common silent blocker) |
| `txt` / `mem` | executing from / `mmap`-ed a file on the volume |
| `3r` `4w` `5u` | an open file descriptor (read / write / read-write) |
| `rtd` | the volume is a process's root directory |

`disk busy` resolves a mount path (`/Volumes/X9 Pro`), volume name (`X9 Pro`), device (`/dev/disk6s2`), or whole disk (`disk6` → all its slices), groups holders by pid with a reason summary and example paths, and prints the `diskutil unmount force` / `unmountDisk` escape hatch. `fuser -c <mount>` gives the same pids as a bare list; `busy` is the annotated version. Run under sudo or system daemons like `mds` (Spotlight) and `fseventsd` that frequently hold external volumes stay invisible.

#### Why not DTrace / `iosnoop` / `iotop`?
They give the richest block-layer view (per-request latency, device queue) on paper, but with **SIP enabled** (the default on modern macOS) DTrace's `io` provider is unreliable and often blocked. It's not a dependable spine, so this tool doesn't build on it. If you disable SIP you can add block-level tracing on top of the same questions.

#### Honest limitation: buffered-write attribution

`ri_diskio_byteswritten` charges a process for I/O it is **accountable** for. Because of the unified buffer cache, application `write()`s are buffered and the *physical* flush to the SSD is frequently performed later by kernel flush threads — so a burst of `dd`/app writes may show up delayed, spread out, or attributed to a system process rather than the originating one. This is a property of macOS's I/O accounting (Activity Monitor behaves identically), not a bug in the tool. **Cache-missing reads** and **sustained real workloads** (databases, indexing, backups, builds) attribute cleanly, which is the common case you actually want to catch. For exact byte-for-byte causation on a specific process, use `inspect` (`fs_usage`), which traces the syscalls themselves.

---

## Layout

```
stethoscope          the dispatcher — `stethoscope <scope> <command>`
scopes/
  disk.py            disk scope: data layer + CLI commands (self-documenting header)
  disk_tui.py        disk scope: curses TUI over the same data layer
```

Design rule: each scope is one module exposing a **data layer** (pure functions returning structures) with thin **presentation** on top. Future agent-facing output (`--json`) and anomaly detection build on the data layer, never on the rendered text.
