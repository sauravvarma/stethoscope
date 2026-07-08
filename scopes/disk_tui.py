#!/usr/bin/env python3
"""
stethoscope disk tui — a full-screen terminal GUI over the disk scope.

This is a thin presentation layer: every number and every action is produced by
the reusable blocks in disk.py. Nothing about disk I/O is re-implemented here.

    data:     disk.snapshot_diskio / rank_io   -> the live process table
              disk._mount_table / resolve_volume / collect_holders -> volumes
              disk.open_files                    -> a process's held files
              disk.proc_name / human / rate      -> formatting
    actions:  disk.cmd_inspect                   -> live fs_usage drill-down

Two views, switchable with 1/2 or Tab:

    Processes  live per-process disk I/O, ranked (the `top` view, navigable)
    Volumes    mounted volumes; Enter runs the reverse lookup (the `busy` view)

Keys (also shown in the footer):
    ↑/↓ j/k  move      1/2 Tab  switch view     p / space  pause     +/-  interval
    Processes:  Enter/f held files   i  inspect (fs_usage, sudo)   x  kill (confirm)
    Volumes:    Enter/r holders      e  eject (confirm)
    q  quit

Zero third-party dependencies — stdlib curses + disk.py. Run under sudo for
full coverage (other users' I/O and system-daemon holders are otherwise hidden).
"""

import curses
import os
import signal
import subprocess
import sys
import time

try:
    from scopes import disk as d          # via the stethoscope dispatcher
except ImportError:
    import disk as d                       # run directly: ./scopes/disk_tui.py

V_PROC, V_VOL = 0, 1

# color pair ids
C_ACCENT, C_READ, C_WRITE, C_BAR, C_SEL = 1, 2, 3, 4, 5


class App:
    def __init__(self, stdscr):
        self.s = stdscr
        self.view = V_PROC
        self.interval = 1.0
        self.paused = False
        self.sel = 0
        self.msg = ""
        self.is_root = os.geteuid() == 0

        self.prev = d.snapshot_diskio()
        self.prev_t = time.time()
        self.rows = []
        self.sys_dr = self.sys_dw = 0.0
        self.volumes = []

        curses.curs_set(0)
        self._init_colors()
        self.refresh_volumes()

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

    def cp(self, i):
        return curses.color_pair(i) if curses.has_colors() else 0

    # -- safe drawing ----------------------------------------------------
    def put(self, y, x, text, attr=0):
        h, w = self.s.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        try:
            self.s.addnstr(y, x, text, w - 1 - x, attr)
        except curses.error:
            pass

    def fill(self, y, attr):
        h, w = self.s.getmaxyx()
        if 0 <= y < h:
            try:
                self.s.addstr(y, 0, " " * (w - 1), attr)
            except curses.error:
                pass

    # -- data ------------------------------------------------------------
    def refresh_procs(self):
        cur = d.snapshot_diskio()
        now = time.time()
        self.rows, self.sys_dr, self.sys_dw = d.rank_io(self.prev, cur, now - self.prev_t)
        self.prev, self.prev_t = cur, now

    def refresh_volumes(self):
        vols = [(dev, mp) for dev, mp in d._mount_table() if mp.startswith("/Volumes/")]
        self.volumes = vols or d._mount_table()

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

        # row 0: title bar
        self.fill(0, self.cp(C_BAR) | curses.A_BOLD)
        tabs = " stethoscope·disk "
        p = "[1]Processes" if self.view == V_PROC else " 1 Processes"
        v = "[2]Volumes" if self.view == V_VOL else " 2 Volumes"
        self.put(0, 0, tabs, self.cp(C_BAR) | curses.A_BOLD)
        self.put(0, 12, "%s  %s" % (p, v), self.cp(C_BAR) | curses.A_BOLD)
        right = "%s  %s" % ("root" if self.is_root else "user", time.strftime("%H:%M:%S"))
        self.put(0, max(0, w - len(right) - 2), right, self.cp(C_BAR) | curses.A_BOLD)

        # row 1: status sub-bar
        state = "PAUSED" if self.paused else "live"
        sub = ("system read %s  write %s   ·   refresh %.1fs  ·  %s"
               % (d.rate(self.sys_dr), d.rate(self.sys_dw), self.interval, state))
        self.put(1, 1, sub, self.cp(C_ACCENT))
        if not self.is_root:
            note = "not root — some I/O & holders hidden (sudo for full view)"
            self.put(1, max(1, w - len(note) - 2), note, curses.A_DIM)

        if self.view == V_PROC:
            self._draw_procs(h, w)
        else:
            self._draw_vols(h, w)

        # footer
        self.fill(h - 1, self.cp(C_BAR))
        if self.msg:
            self.put(h - 1, 1, self.msg, self.cp(C_BAR) | curses.A_BOLD)
        else:
            if self.view == V_PROC:
                keys = "↑↓/jk move  Enter/f files  i inspect  x kill  1/2 view  p pause  +/- rate  q quit"
            else:
                keys = "↑↓/jk move  Enter/r holders  e eject  1/2 view  p pause  q quit"
            self.put(h - 1, 1, keys, self.cp(C_BAR))
        self.s.refresh()

    def _draw_procs(self, h, w):
        self.put(3, 1, "%7s  %-26s %11s %11s %10s %10s" %
                 ("PID", "COMMAND", "READ/s", "WRITE/s", "RD TOT", "WR TOT"),
                 curses.A_BOLD)
        top = 4
        avail = h - top - 1
        if not self.rows:
            self.put(top, 2, "(no disk I/O this interval)", curses.A_DIM)
            return
        self.sel = max(0, min(self.sel, len(self.rows) - 1))
        start = max(0, self.sel - avail + 1) if self.sel >= avail else 0
        for i in range(start, min(len(self.rows), start + avail)):
            total, dr, dw, r, w_, pid, name = self.rows[i]
            y = top + (i - start)
            selected = (i == self.sel)
            base = self.cp(C_SEL) | curses.A_BOLD if selected else 0
            if selected:
                self.fill(y, self.cp(C_SEL))
            self.put(y, 1, "%7d  %-26s %11s %11s %10s %10s" %
                     (pid, name[:26], d.rate(dr), d.rate(dw), d.human(r), d.human(w_)),
                     base)

    def _draw_vols(self, h, w):
        self.put(3, 1, "%-22s %-14s %s" % ("VOLUME", "DEVICE", "MOUNT"), curses.A_BOLD)
        top = 4
        if not self.volumes:
            self.put(top, 2, "(no mounted volumes)", curses.A_DIM)
            return
        self.sel = max(0, min(self.sel, len(self.volumes) - 1))
        for i, (dev, mp) in enumerate(self.volumes):
            y = top + i
            if y >= h - 1:
                break
            selected = (i == self.sel)
            if selected:
                self.fill(y, self.cp(C_SEL))
            name = os.path.basename(mp) or mp
            self.put(y, 1, "%-22s %-14s %s" % (name[:22], dev.replace("/dev/", ""), mp),
                     self.cp(C_SEL) | curses.A_BOLD if selected else 0)

    # -- popups & prompts ------------------------------------------------
    def popup(self, title, lines):
        h, w = self.s.getmaxyx()
        body = lines or ["(nothing)"]
        pw = min(w - 2, max(len(title) + 6, max((len(l) for l in body), default=0) + 4))
        pw = max(pw, 24)
        ph = min(h - 2, len(body) + 4)
        win = curses.newwin(ph, pw, (h - ph) // 2, (w - pw) // 2)
        win.box()
        win.attron(curses.A_BOLD)
        win.addnstr(0, 2, " %s " % title, pw - 4)
        win.attroff(curses.A_BOLD)
        for i, ln in enumerate(body[:ph - 4]):
            try:
                win.addnstr(2 + i, 2, ln, pw - 4)
            except curses.error:
                pass
        if len(body) > ph - 4:
            win.addnstr(ph - 2, 2, "... (%d more)" % (len(body) - (ph - 4)), pw - 4,
                        curses.A_DIM)
        win.addnstr(ph - 1, 2, " any key to close ", pw - 4, curses.A_DIM)
        win.refresh()
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
    def selected_pid(self):
        if self.view == V_PROC and self.rows:
            row = self.rows[max(0, min(self.sel, len(self.rows) - 1))]
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
        # Leave curses, hand the terminal to the streaming fs_usage tracer, restore.
        curses.def_prog_mode()
        curses.endwin()
        try:
            os.system("clear")
            print("=== stethoscope disk inspect · pid %d (%s) — ctrl-C to return ===\n"
                  % (pid, name))
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
        dev, mp = self.volumes[max(0, min(self.sel, len(self.volumes) - 1))]
        procs = d.collect_holders(d.resolve_volume(mp))
        if not procs:
            self.popup("holders · %s" % mp,
                       ["No process is holding it — should eject cleanly."])
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
        dev, mp = self.volumes[max(0, min(self.sel, len(self.volumes) - 1))]
        if self.confirm("eject '%s'?" % mp):
            r = subprocess.run(["/usr/sbin/diskutil", "unmount", mp],
                               capture_output=True, text=True)
            self.msg = (r.stdout + r.stderr).strip().splitlines()[-1] if (r.stdout + r.stderr).strip() else "done"
            self.refresh_volumes()

    # -- input -----------------------------------------------------------
    def handle_key(self, ch):
        n = len(self.rows) if self.view == V_PROC else len(self.volumes)
        if ch in (ord("q"), 27):            # q / ESC
            return False
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.sel = min(self.sel + 1, max(0, n - 1))
        elif ch in (curses.KEY_UP, ord("k")):
            self.sel = max(self.sel - 1, 0)
        elif ch in (ord("1"),):
            self.view, self.sel = V_PROC, 0
        elif ch in (ord("2"),):
            self.view, self.sel = V_VOL, 0
        elif ch == ord("\t"):
            self.view = V_VOL if self.view == V_PROC else V_PROC
            self.sel = 0
        elif ch in (ord("p"), ord(" ")):
            self.paused = not self.paused
        elif ch == ord("+"):
            self.interval = min(10.0, round(self.interval + 0.5, 1))
        elif ch == ord("-"):
            self.interval = max(0.5, round(self.interval - 0.5, 1))
        elif self.view == V_PROC and ch in (ord("f"), curses.KEY_ENTER, 10, 13):
            self.act_files()
        elif self.view == V_PROC and ch == ord("i"):
            self.act_inspect()
        elif self.view == V_PROC and ch == ord("x"):
            self.act_kill()
        elif self.view == V_VOL and ch in (ord("r"), curses.KEY_ENTER, 10, 13):
            self.act_holders()
        elif self.view == V_VOL and ch == ord("e"):
            self.act_eject()
        else:
            self.msg = ""
        return True

    # -- main loop -------------------------------------------------------
    def run(self):
        self.s.timeout(200)   # ms; getch returns -1 on timeout so the clock ticks
        while True:
            if (not self.paused and self.view == V_PROC
                    and (time.time() - self.prev_t) >= self.interval):
                self.refresh_procs()
            self.draw()
            ch = self.s.getch()
            if ch == -1:
                continue
            if ch == curses.KEY_RESIZE:
                continue
            self.msg = ""
            if not self.handle_key(ch):
                break


def main():
    if not sys.stdout.isatty():
        sys.stderr.write("stethoscope disk tui needs an interactive terminal.\n")
        return 1
    # sudo (and bare/cron environments) often drop $TERM, which makes curses fail
    # with "setupterm: could not find terminal". Fall back to a sane default.
    if not os.environ.get("TERM"):
        os.environ["TERM"] = "xterm-256color"
    try:
        curses.wrapper(lambda stdscr: App(stdscr).run())
    except curses.error as e:
        sys.stderr.write(
            "curses could not start: %s\n"
            "TERM=%r may be unknown to this system. Try one of:\n"
            "    TERM=xterm-256color sudo -E %s\n"
            "    sudo -E %s        (preserve your shell's TERM)\n"
            % (e, os.environ.get("TERM"), sys.argv[0], sys.argv[0]))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
