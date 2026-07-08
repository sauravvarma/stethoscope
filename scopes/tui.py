#!/usr/bin/env python3
"""stethoscope tui — a pluggable curses shell for every scope.

The shell is presentation only: every metric comes from the existing scope data
layers, then curses renders the shared data with a common tab/title/status/footer
language. ``stethoscope disk tui`` delegates here and starts focused on disk.
"""

import curses
import os
import signal
import subprocess
import sys
import time

try:
    from scopes import battery as batt
    from scopes import core
    from scopes import cpu
    from scopes import disk as d
    from scopes import memory
    from scopes import smart
except ImportError:   # invoked with scopes/ directly on sys.path
    import battery as batt
    import core
    import cpu
    import disk as d
    import memory
    import smart

V_PROC, V_VOL = 0, 1

# color pair ids
C_ACCENT, C_READ, C_WRITE, C_BAR, C_SEL, C_CRIT = 1, 2, 3, 4, 5, 6

TABS = ("disk", "cpu", "memory", "battery", "smart")


def tab_index(name):
    """Return the index for a tab name, defaulting to disk for unknown input."""
    try:
        return TABS.index(name)
    except ValueError:
        return 0


def tab_index_for_key(ch):
    """Map top-level number keys 1..5 to tab indexes; otherwise None."""
    if ch in (ord("1"), ord("2"), ord("3"), ord("4"), ord("5")):
        return ch - ord("1")
    return None


def severity_for_memory_pressure(pressure):
    return "critical" if pressure == "critical" else "warn" if pressure == "warn" else "ok"


def severity_for_battery_health(health):
    if not health.get("present"):
        return "ok"
    return "critical" if health.get("condition") == "Service Recommended" else "ok"


def severity_pair(severity):
    """Map semantic severity to curses pair ids (pure for unit testing)."""
    if severity == "critical":
        return C_CRIT
    if severity == "warn":
        return C_WRITE
    if severity == "ok":
        return C_READ
    return C_ACCENT


def health_verdict(health):
    sev = health.get("worst_severity") or "ok"
    if sev == "critical":
        return "CRITICAL"
    if sev == "warn":
        return "WARN"
    status = (health.get("smart_status") or "unknown").lower()
    return "HEALTHY" if status in ("verified", "ok", "passed") else status.upper()


def format_disk_row(row):
    _, dr, dw, r, w_, pid, name = row
    return ("%7d  %-26s " % (pid, name[:26]), d.rate(dr), d.rate(dw),
            " %10s %10s" % (d.human(r), d.human(w_)))


def format_cpu_row(row):
    cpu_pct, wake_ps, _idle_ps, _intr_ps, pid, name = row
    return "%7d  %-30s %8.1f%% %10.1f" % (pid, name[:30], cpu_pct, wake_ps)


def format_memory_row(row):
    foot, res, pid, name = row
    return "%7d  %-30s %12s %12s" % (pid, name[:30], core.human(foot), core.human(res))


def format_battery_row(row):
    score, cpu_pct, idle_ps, intr_ps, pid, name = row
    return "%7d  %-26s %8.1f %7.1f%% %10.1f" % (
        pid, name[:26], score, cpu_pct, idle_ps + intr_ps)


def format_smart_row(health):
    wear = "%s%%" % health["percentage_used"] if health.get("percentage_used") is not None else "?"
    size = core.human(health["size_bytes"]) if health.get("size_bytes") else "?"
    loc = "internal" if health.get("internal") else "external"
    return "%-8s %-28s %-9s %-10s %-7s %s" % (
        health.get("device", "?"), (health.get("name") or "?")[:28], size,
        (health.get("smart_status") or "unknown")[:10], wear, loc)


class App:
    def __init__(self, stdscr, initial_tab="disk"):
        self.s = stdscr
        self.tab = tab_index(initial_tab)
        self.disk_view = V_PROC
        self.interval = 1.0
        self.paused = False
        self.sel = {name: 0 for name in TABS}
        self.msg = ""
        self.is_root = os.geteuid() == 0

        self.prev_disk = d.snapshot_diskio()
        self.prev_disk_t = time.time()
        self.disk_rows = []
        self.sys_dr = self.sys_dw = 0.0
        self.volumes = []

        self.prev_cpu_mach, self.prev_cpu = cpu.snapshot()
        self.prev_cpu_t = time.time()
        self.cpu_rows = []
        self.sys_cpu = 0.0

        self.mem_rows = []
        self.sysmem = {}

        self.prev_batt_mach, self.prev_batt = batt.snapshot()
        self.prev_batt_t = time.time()
        self.batt_rows = []
        self.batt_health = {"present": False}

        self.smart_rows = []
        self.last_refresh = {name: 0.0 for name in TABS}

        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self._init_colors()
        self.refresh_volumes()
        self.refresh_memory()
        self.refresh_battery_health()
        self.refresh_smart()

    # -- color -----------------------------------------------------------
    def _init_colors(self):
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        curses.init_pair(C_ACCENT, curses.COLOR_CYAN, bg)
        curses.init_pair(C_READ, curses.COLOR_GREEN, bg)
        curses.init_pair(C_WRITE, curses.COLOR_YELLOW, bg)
        curses.init_pair(C_BAR, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(C_SEL, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(C_CRIT, curses.COLOR_RED, bg)

    def cp(self, i):
        return curses.color_pair(i) if curses.has_colors() else 0

    def sev_attr(self, severity):
        return self.cp(severity_pair(severity)) | (curses.A_BOLD if severity == "critical" else 0)

    # -- safe drawing ----------------------------------------------------
    def put(self, y, x, text, attr=0):
        h, w = self.s.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        try:
            self.s.addnstr(y, x, text, max(0, w - 1 - x), attr)
        except curses.error:
            pass

    def fill(self, y, attr):
        h, w = self.s.getmaxyx()
        if 0 <= y < h:
            try:
                self.s.addstr(y, 0, " " * max(0, w - 1), attr)
            except curses.error:
                pass

    def win_put(self, win, y, x, text, n, attr=0):
        try:
            win.addnstr(y, x, text, n, attr)
        except curses.error:
            pass

    # -- data ------------------------------------------------------------
    def refresh_procs(self):
        cur = d.snapshot_diskio()
        now = time.time()
        self.disk_rows, self.sys_dr, self.sys_dw = d.rank_io(self.prev_disk, cur, now - self.prev_disk_t)
        self.prev_disk, self.prev_disk_t = cur, now
        self.last_refresh["disk"] = now

    def refresh_volumes(self):
        vols = [(dev, mp) for dev, mp in d._mount_table() if mp.startswith("/Volumes/")]
        self.volumes = vols or d._mount_table()
        self.last_refresh["disk"] = time.time()

    def refresh_cpu(self):
        cur_mach, cur = cpu.snapshot()
        now = time.time()
        self.cpu_rows, self.sys_cpu = cpu.rank_cpu(
            self.prev_cpu, cur, self.prev_cpu_mach, cur_mach, now - self.prev_cpu_t)
        self.prev_cpu, self.prev_cpu_mach, self.prev_cpu_t = cur, cur_mach, now
        self.last_refresh["cpu"] = now

    def refresh_memory(self):
        self.mem_rows = memory.rank_mem(core.snapshot_rusage())
        self.sysmem = memory.system_memory()
        self.last_refresh["memory"] = time.time()

    def refresh_battery_health(self):
        self.batt_health = batt.battery_health()

    def refresh_battery(self):
        cur_mach, cur = batt.snapshot()
        now = time.time()
        self.batt_rows = batt.rank_energy(
            self.prev_batt, cur, self.prev_batt_mach, cur_mach, now - self.prev_batt_t)
        self.prev_batt, self.prev_batt_mach, self.prev_batt_t = cur, cur_mach, now
        self.refresh_battery_health()
        self.last_refresh["battery"] = now

    def refresh_smart(self):
        self.smart_rows = [smart.drive_health(dev, internal)
                           for dev, internal in smart.list_physical_drives()]
        self.last_refresh["smart"] = time.time()

    def maybe_refresh(self):
        if self.paused:
            return
        active = TABS[self.tab]
        now = time.time()
        if active == "disk" and self.disk_view == V_PROC and now - self.prev_disk_t >= self.interval:
            self.refresh_procs()
        elif active == "cpu" and now - self.prev_cpu_t >= self.interval:
            self.refresh_cpu()
        elif active == "memory" and now - self.last_refresh["memory"] >= self.interval:
            self.refresh_memory()
        elif active == "battery" and now - self.prev_batt_t >= self.interval:
            self.refresh_battery()
        elif active == "smart" and now - self.last_refresh["smart"] >= max(5.0, self.interval):
            self.refresh_smart()

    def _reason_summary(self, holds):
        counts = {}
        for reason, _ in holds:
            counts[reason] = counts.get(reason, 0) + 1
        return ", ".join("%s×%d" % (r, c) if c > 1 else r
                         for r, c in sorted(counts.items(), key=lambda x: -x[1]))

    # -- rendering -------------------------------------------------------
    def draw(self):
        self.s.erase()
        h, w = self.s.getmaxyx()
        self._draw_title(w)
        self._draw_status(w)

        active = TABS[self.tab]
        if active == "disk":
            self._draw_disk(h, w)
        elif active == "cpu":
            self._draw_cpu(h, w)
        elif active == "memory":
            self._draw_memory(h, w)
        elif active == "battery":
            self._draw_battery(h, w)
        elif active == "smart":
            self._draw_smart(h, w)

        self._draw_footer(h)
        self.s.refresh()

    def _draw_title(self, w):
        self.fill(0, self.cp(C_BAR) | curses.A_BOLD)
        self.put(0, 0, " stethoscope ", self.cp(C_BAR) | curses.A_BOLD)
        x = 13
        for i, name in enumerate(TABS):
            label = "[%d]%s" % (i + 1, name) if i == self.tab else " %d %s" % (i + 1, name)
            self.put(0, x, label, self.cp(C_BAR) | (curses.A_BOLD if i == self.tab else 0))
            x += len(label) + 2
        right = "%s  %s" % ("root" if self.is_root else "user", time.strftime("%H:%M:%S"))
        self.put(0, max(0, w - len(right) - 2), right, self.cp(C_BAR) | curses.A_BOLD)

    def _draw_status(self, w):
        state = "PAUSED" if self.paused else "live"
        active = TABS[self.tab]
        if active == "disk":
            mode = "processes" if self.disk_view == V_PROC else "volumes"
            sub = "disk/%s · system read %s  write %s · refresh %.1fs · %s" % (
                mode, d.rate(self.sys_dr), d.rate(self.sys_dw), self.interval, state)
            self.put(1, 1, sub, self.cp(C_ACCENT))
            if not self.is_root:
                note = "not root — some I/O & holders hidden (sudo for full view)"
                self.put(1, max(1, w - len(note) - 2), note, curses.A_DIM)
        elif active == "cpu":
            sub = "cpu · system %.1f%% of %d cores · refresh %.1fs · %s" % (
                self.sys_cpu, cpu.NCPU, self.interval, state)
            self.put(1, 1, sub, self.cp(C_ACCENT))
        elif active == "memory":
            p = self.sysmem.get("pressure", "unknown")
            sub = "memory · used %s/%s · wired %s · compressed %s · pressure %s · %s" % (
                core.human(self.sysmem.get("used", 0)), core.human(self.sysmem.get("total", 0)),
                core.human(self.sysmem.get("wired", 0)), core.human(self.sysmem.get("compressed", 0)),
                p, state)
            self.put(1, 1, sub, self.sev_attr(severity_for_memory_pressure(p)))
        elif active == "battery":
            h = self.batt_health
            if not h.get("present"):
                sub = "battery · no battery detected · %s" % state
            else:
                sub = "battery · charge %s%% · health %s%% · condition %s · %s" % (
                    h.get("charge_pct"), h.get("health_pct"), h.get("condition"), state)
            self.put(1, 1, sub, self.sev_attr(severity_for_battery_health(h)))
        elif active == "smart":
            crit = sum(1 for r in self.smart_rows if r.get("worst_severity") == "critical")
            warn = sum(1 for r in self.smart_rows if r.get("worst_severity") == "warn")
            sub = "smart · %d drive(s) · %d critical · %d warn · %s" % (
                len(self.smart_rows), crit, warn, state)
            self.put(1, 1, sub, self.sev_attr("critical" if crit else "warn" if warn else "ok"))

    def _draw_footer(self, h):
        self.fill(h - 1, self.cp(C_BAR))
        if self.msg:
            self.put(h - 1, 1, self.msg, self.cp(C_BAR) | curses.A_BOLD)
            return
        active = TABS[self.tab]
        if active == "disk" and self.disk_view == V_PROC:
            keys = "1-5 tabs  Tab proc/vol  ↑↓/jk move  Enter/f files  i inspect  x kill  p pause  +/- rate  q quit"
        elif active == "disk":
            keys = "1-5 tabs  Tab proc/vol  ↑↓/jk move  Enter/r holders  e eject  p pause  q quit"
        else:
            keys = "1-5 tabs  ↑↓/jk move  p pause  +/- rate  q quit"
        self.put(h - 1, 1, keys, self.cp(C_BAR))

    def _selected_attr(self, selected):
        return self.cp(C_SEL) | curses.A_BOLD if selected else 0

    def _draw_disk(self, h, w):
        if self.disk_view == V_PROC:
            self._draw_procs(h, w)
        else:
            self._draw_vols(h, w)

    def _draw_procs(self, h, w):
        self.put(3, 1, "%7s  %-26s %11s %11s %10s %10s" %
                 ("PID", "COMMAND", "READ/s", "WRITE/s", "RD TOT", "WR TOT"), curses.A_BOLD)
        top = 4
        avail = h - top - 1
        if not self.disk_rows:
            self.put(top, 2, "(no disk I/O this interval)", curses.A_DIM)
            return
        sel = self._clamp_sel("disk", len(self.disk_rows))
        start = max(0, sel - avail + 1) if sel >= avail else 0
        for i in range(start, min(len(self.disk_rows), start + avail)):
            y = top + (i - start)
            selected = (i == sel)
            if selected:
                self.fill(y, self.cp(C_SEL))
            prefix, read_s, write_s, suffix = format_disk_row(self.disk_rows[i])
            attr = self._selected_attr(selected)
            x = 1
            self.put(y, x, prefix, attr); x += len(prefix)
            self.put(y, x, "%11s" % read_s, attr if selected else self.cp(C_READ)); x += 11
            self.put(y, x, " %11s" % write_s, attr if selected else self.cp(C_WRITE)); x += 12
            self.put(y, x, suffix, attr)

    def _draw_vols(self, h, w):
        self.put(3, 1, "%-22s %-14s %s" % ("VOLUME", "DEVICE", "MOUNT"), curses.A_BOLD)
        top = 4
        if not self.volumes:
            self.put(top, 2, "(no mounted volumes)", curses.A_DIM)
            return
        sel = self._clamp_sel("disk", len(self.volumes))
        for i, (dev, mp) in enumerate(self.volumes):
            y = top + i
            if y >= h - 1:
                break
            selected = (i == sel)
            if selected:
                self.fill(y, self.cp(C_SEL))
            name = os.path.basename(mp) or mp
            self.put(y, 1, "%-22s %-14s %s" % (name[:22], dev.replace("/dev/", ""), mp),
                     self._selected_attr(selected))

    def _draw_cpu(self, h, w):
        self.put(3, 1, "%7s  %-30s %8s %10s" % ("PID", "COMMAND", "CPU%", "WAKE/s"), curses.A_BOLD)
        self._draw_plain_rows("cpu", self.cpu_rows, h, format_cpu_row, "(no CPU activity this interval)")

    def _draw_memory(self, h, w):
        self.put(3, 1, "%7s  %-30s %12s %12s" % ("PID", "COMMAND", "FOOTPRINT", "RESIDENT"), curses.A_BOLD)
        self._draw_plain_rows("memory", self.mem_rows, h, format_memory_row, "(no accessible processes — try sudo)")

    def _draw_battery(self, h, w):
        self.put(3, 1, "%7s  %-26s %8s %8s %10s" % ("PID", "COMMAND", "ENERGY", "CPU%", "WAKE/s"), curses.A_BOLD)
        self._draw_plain_rows("battery", self.batt_rows, h, format_battery_row, "(no energy activity this interval)")

    def _draw_smart(self, h, w):
        self.put(3, 1, "%-8s %-28s %-9s %-10s %-7s %s" %
                 ("DEVICE", "NAME", "SIZE", "SMART", "WEAR", "VERDICT"), curses.A_BOLD)
        top = 4
        if not self.smart_rows:
            self.put(top, 2, "(no physical drives found)", curses.A_DIM)
            return
        sel = self._clamp_sel("smart", len(self.smart_rows))
        for i, row in enumerate(self.smart_rows):
            y = top + i
            if y >= h - 1:
                break
            selected = (i == sel)
            if selected:
                self.fill(y, self.cp(C_SEL))
            attr = self._selected_attr(selected) if selected else self.sev_attr(row.get("worst_severity", "ok"))
            self.put(y, 1, format_smart_row(row) + "  " + health_verdict(row), attr)

    def _draw_plain_rows(self, key, rows, h, formatter, empty):
        top = 4
        avail = h - top - 1
        if not rows:
            self.put(top, 2, empty, curses.A_DIM)
            return
        sel = self._clamp_sel(key, len(rows))
        start = max(0, sel - avail + 1) if sel >= avail else 0
        for i in range(start, min(len(rows), start + avail)):
            y = top + (i - start)
            selected = (i == sel)
            if selected:
                self.fill(y, self.cp(C_SEL))
            self.put(y, 1, formatter(rows[i]), self._selected_attr(selected))

    # -- popups & prompts ------------------------------------------------
    def popup(self, title, lines):
        h, w = self.s.getmaxyx()
        body = lines or ["(nothing)"]
        max_w = max(1, w - 2)
        max_h = max(1, h - 2)
        pw = min(max_w, max(24, len(title) + 6,
                            max((len(l) for l in body), default=0) + 4))
        ph = min(max_h, len(body) + 4)
        if pw <= 0 or ph <= 0:
            return
        win = curses.newwin(ph, pw, max(0, (h - ph) // 2), max(0, (w - pw) // 2))
        try:
            win.box()
        except curses.error:
            pass
        self.win_put(win, 0, 2, " %s " % title, pw - 4, curses.A_BOLD)
        for i, ln in enumerate(body[:ph - 4]):
            self.win_put(win, 2 + i, 2, ln, pw - 4)
        if len(body) > ph - 4:
            self.win_put(win, ph - 2, 2, "... (%d more)" % (len(body) - (ph - 4)), pw - 4, curses.A_DIM)
        self.win_put(win, ph - 1, 2, " any key to close ", pw - 4, curses.A_DIM)
        try:
            win.refresh()
        except curses.error:
            pass
        self.s.timeout(-1)
        win.getch()
        self.s.timeout(200)

    def confirm(self, question):
        self.msg = question + "  [y/N]"
        self.draw()
        self.s.timeout(-1)
        ch = self.s.getch()
        self.s.timeout(200)
        self.msg = ""
        return ch in (ord("y"), ord("Y"))

    # -- actions ---------------------------------------------------------
    def _active_count(self):
        active = TABS[self.tab]
        if active == "disk":
            return len(self.disk_rows) if self.disk_view == V_PROC else len(self.volumes)
        return len({"cpu": self.cpu_rows, "memory": self.mem_rows,
                    "battery": self.batt_rows, "smart": self.smart_rows}[active])

    def _clamp_sel(self, key, n):
        self.sel[key] = max(0, min(self.sel.get(key, 0), max(0, n - 1)))
        return self.sel[key]

    def selected_pid(self):
        if TABS[self.tab] == "disk" and self.disk_view == V_PROC and self.disk_rows:
            row = self.disk_rows[self._clamp_sel("disk", len(self.disk_rows))]
            return row[5], row[6]
        return None, None

    def act_files(self):
        pid, name = self.selected_pid()
        if not pid:
            return
        try:
            items = d.open_files(pid)
        except Exception as e:
            self.msg = "lsof failed: %s" % e
            return
        lines = ["%-18s %-4s %s" % (r, t, n) for r, t, n in items] or \
                ["(no on-disk files held — try sudo)"]
        self.popup("held files · pid %d (%s)" % (pid, name), lines)

    def act_inspect(self):
        pid, name = self.selected_pid()
        if not pid:
            return
        curses.def_prog_mode()
        curses.endwin()
        try:
            os.system("clear")
            print("=== stethoscope disk inspect · pid %d (%s) — ctrl-C to return ===\n" % (pid, name))
            d.cmd_inspect(pid)
        except KeyboardInterrupt:
            pass
        try:
            input("\n[press Enter to return to the TUI] ")
        except (EOFError, KeyboardInterrupt):
            pass
        curses.reset_prog_mode()
        self.s.clear()
        self.s.refresh()

    def act_kill(self):
        pid, name = self.selected_pid()
        if not pid:
            return
        if self.confirm("kill pid %d (%s)?" % (pid, name)):
            try:
                os.kill(pid, signal.SIGTERM)
                self.msg = "sent SIGTERM to %d" % pid
            except Exception as e:
                self.msg = "kill failed: %s" % e

    def act_holders(self):
        if not self.volumes:
            return
        dev, mp = self.volumes[self._clamp_sel("disk", len(self.volumes))]
        procs = d.collect_holders(d.resolve_volume(mp))
        if not procs:
            self.popup("holders · %s" % mp, ["No process is holding it — should eject cleanly."])
            return
        lines = []
        for pid in sorted(procs, key=lambda p: -len(procs[p]["holds"])):
            info = procs[pid]
            lines.append("pid %-6d %-18s user=%s" % (pid, info["name"], info["user"]))
            lines.append("   holding: %s" % self._reason_summary(info["holds"]))
            for _, path in info["holds"][:2]:
                lines.append("     %s" % path)
        self.popup("holders · %s  (%d)" % (mp, len(procs)), lines)

    def act_eject(self):
        if not self.volumes:
            return
        dev, mp = self.volumes[self._clamp_sel("disk", len(self.volumes))]
        if self.confirm("eject '%s'?" % mp):
            r = subprocess.run(["/usr/sbin/diskutil", "unmount", mp], capture_output=True, text=True)
            out = (r.stdout + r.stderr).strip()
            self.msg = out.splitlines()[-1] if out else "done"
            self.refresh_volumes()

    # -- input -----------------------------------------------------------
    def handle_key(self, ch):
        if ch in (ord("q"), 27):
            return False
        new_tab = tab_index_for_key(ch)
        if new_tab is not None:
            self.tab = new_tab
            self.msg = ""
            return True
        active = TABS[self.tab]
        n = self._active_count()
        if ch in (curses.KEY_DOWN, ord("j")):
            self.sel[active] = min(self.sel.get(active, 0) + 1, max(0, n - 1))
        elif ch in (curses.KEY_UP, ord("k")):
            self.sel[active] = max(self.sel.get(active, 0) - 1, 0)
        elif active == "disk" and ch == ord("\t"):
            self.disk_view = V_VOL if self.disk_view == V_PROC else V_PROC
            self.sel["disk"] = 0
        elif ch in (ord("p"), ord(" ")):
            self.paused = not self.paused
        elif ch == ord("+"):
            self.interval = min(10.0, round(self.interval + 0.5, 1))
        elif ch == ord("-"):
            self.interval = max(0.5, round(self.interval - 0.5, 1))
        elif active == "disk" and self.disk_view == V_PROC and ch in (ord("f"), curses.KEY_ENTER, 10, 13):
            self.act_files()
        elif active == "disk" and self.disk_view == V_PROC and ch == ord("i"):
            self.act_inspect()
        elif active == "disk" and self.disk_view == V_PROC and ch == ord("x"):
            self.act_kill()
        elif active == "disk" and self.disk_view == V_VOL and ch in (ord("r"), curses.KEY_ENTER, 10, 13):
            self.act_holders()
        elif active == "disk" and self.disk_view == V_VOL and ch == ord("e"):
            self.act_eject()
        else:
            self.msg = ""
        return True

    # -- main loop -------------------------------------------------------
    def run(self):
        self.s.timeout(200)
        while True:
            self.maybe_refresh()
            self.draw()
            ch = self.s.getch()
            if ch == -1:
                continue
            if ch == curses.KEY_RESIZE:
                continue
            self.msg = ""
            if not self.handle_key(ch):
                break


def main(initial_tab="disk"):
    if not sys.stdout.isatty():
        sys.stderr.write("stethoscope tui needs an interactive terminal.\n")
        return 1
    if not os.environ.get("TERM"):
        os.environ["TERM"] = "xterm-256color"
    try:
        curses.wrapper(lambda stdscr: App(stdscr, initial_tab=initial_tab).run())
    except curses.error as e:
        sys.stderr.write(
            "curses could not start: %s\n"
            "TERM=%r may be unknown to this system. Try one of:\n"
            "    TERM=xterm-256color sudo -E %s tui\n"
            "    sudo -E %s tui        (preserve your shell's TERM)\n"
            % (e, os.environ.get("TERM"), sys.argv[0], sys.argv[0]))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
