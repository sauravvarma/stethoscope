"""Reusable, scope-independent curses primitives for stethoscope TUIs."""

import curses
import math
from collections import deque


ROLE_NAMES = (
    "accent", "read", "write", "bar", "selection",
    "healthy", "warn", "critical", "unknown",
)
_PAIR_IDS = {name: index + 1 for index, name in enumerate(ROLE_NAMES)}
_SPARKS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


def sanitize(value, limit=None):
    """Return printable terminal text, optionally bounded to ``limit`` chars."""
    text = "".join(char if char.isprintable() else "?" for char in str(value))
    if limit is not None:
        return text[:max(0, int(limit))]
    return text


def safe_addstr(window, y, x, text, attr=0):
    """Draw clipped printable text without leaking curses resize errors."""
    try:
        height, width = window.getmaxyx()
    except curses.error:
        return False
    if y < 0 or y >= height or x < 0 or x >= width:
        return False
    count = max(0, width - 1 - x)
    if count <= 0:
        return False
    try:
        window.addnstr(y, x, sanitize(text, count), count, attr)
    except (curses.error, UnicodeError):
        return False
    return True


def safe_fill(window, y, attr=0, char=" "):
    """Fill a screen row while leaving the lower-right cell untouched."""
    try:
        height, width = window.getmaxyx()
    except curses.error:
        return False
    if y < 0 or y >= height or width <= 1:
        return False
    fill = sanitize(char or " ")[0] * (width - 1)
    try:
        window.addnstr(y, 0, fill, width - 1, attr)
    except (curses.error, UnicodeError):
        return False
    return True


class Palette:
    """Semantic color roles with a complete monochrome fallback."""

    def __init__(self, curses_module=curses):
        self.curses = curses_module
        self.enabled = False
        self.initialize()

    def initialize(self):
        try:
            if not self.curses.has_colors():
                return
            self.curses.start_color()
            try:
                self.curses.use_default_colors()
                background = -1
            except curses.error:
                background = self.curses.COLOR_BLACK
            colors = {
                "accent": (self.curses.COLOR_CYAN, background),
                "read": (self.curses.COLOR_GREEN, background),
                "write": (self.curses.COLOR_YELLOW, background),
                "bar": (self.curses.COLOR_BLACK, self.curses.COLOR_CYAN),
                "selection": (self.curses.COLOR_WHITE, self.curses.COLOR_BLUE),
                "healthy": (self.curses.COLOR_GREEN, background),
                "warn": (self.curses.COLOR_YELLOW, background),
                "critical": (self.curses.COLOR_RED, background),
                "unknown": (self.curses.COLOR_MAGENTA, background),
            }
            for role, pair_id in _PAIR_IDS.items():
                foreground, bg = colors[role]
                self.curses.init_pair(pair_id, foreground, bg)
            self.enabled = True
        except curses.error:
            self.enabled = False

    def attr(self, role, bold=False, dim=False):
        role = role if role in _PAIR_IDS else "unknown"
        value = self.curses.color_pair(_PAIR_IDS[role]) if self.enabled else 0
        if bold:
            value |= self.curses.A_BOLD
        if dim:
            value |= self.curses.A_DIM
        return value


def severity_label(severity):
    """Color-independent state cue used beside every health color."""
    labels = {
        "ok": "[HEALTHY]",
        "info": "[INFO]",
        "healthy": "[HEALTHY]",
        "warn": "[WARN]",
        "critical": "[CRITICAL]",
        "unknown": "[UNKNOWN]",
        "absent": "[ABSENT]",
        "error": "[ERROR]",
        "partial": "[PARTIAL]",
    }
    return labels.get(severity, "[UNKNOWN]")


def sparkline(values, width=40):
    """Render finite numeric values as a sparkline bounded to ``width``."""
    width = max(0, int(width))
    if not values or width == 0:
        return ""
    finite = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(value):
            finite.append(float(value))
    if not finite:
        return ""
    finite = finite[-width:]
    low, high = min(finite), max(finite)
    if low == high:
        return _SPARKS[0] * len(finite)
    span = high - low
    return "".join(
        _SPARKS[int((value - low) / span * (len(_SPARKS) - 1))]
        for value in finite
    )


class RingHistory:
    """Small bounded history suitable for rate sparklines."""

    def __init__(self, capacity=60):
        capacity = int(capacity)
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self._values = deque(maxlen=capacity)

    def append(self, value):
        self._values.append(value)

    def values(self):
        return list(self._values)

    def sparkline(self, width=40):
        return sparkline(self._values, width)

    def __len__(self):
        return len(self._values)


def popup(window, palette, title, lines, timeout_ms=200, curses_module=curses):
    """Show a bounded read-only popup. Return False if the screen is too small."""
    try:
        height, width = window.getmaxyx()
    except curses.error:
        return False
    if height < 5 or width < 10:
        return False
    body = [sanitize(line) for line in (lines or ["(nothing)"])]
    title = sanitize(title)
    popup_width = min(
        width - 2,
        max(8, len(title) + 6, max((len(line) for line in body), default=0) + 4),
    )
    popup_height = min(height - 2, max(3, len(body) + 4))
    try:
        child = curses_module.newwin(
            popup_height, popup_width,
            max(0, (height - popup_height) // 2),
            max(0, (width - popup_width) // 2),
        )
    except curses.error:
        return False
    try:
        child.box()
    except curses.error:
        pass
    safe_addstr(child, 0, 2, " %s " % title, palette.attr("accent", bold=True))
    available = max(0, popup_height - 4)
    for index, line in enumerate(body[:available]):
        safe_addstr(child, index + 2, 2, line)
    if len(body) > available and popup_height >= 3:
        safe_addstr(
            child, popup_height - 2, 2,
            "... (%d more)" % (len(body) - available),
            palette.attr("unknown", dim=True),
        )
    safe_addstr(
        child, popup_height - 1, 2, " any key to close ",
        palette.attr("unknown", dim=True),
    )
    try:
        child.refresh()
        window.timeout(-1)
        child.getch()
    except curses.error:
        return False
    finally:
        try:
            window.timeout(timeout_ms)
        except curses.error:
            pass
    return True
