#!/usr/bin/env python3
"""Compatibility entry point for ``stethoscope disk tui``.

The curses implementation lives in scopes.tui so the same shell can render every
scope. This module keeps the historic import path and command working.
"""

import sys

try:
    from scopes.tui import App, main
except ImportError:   # invoked with scopes/ directly on sys.path
    from tui import App, main


if __name__ == "__main__":
    sys.exit(main(initial_tab="disk"))
