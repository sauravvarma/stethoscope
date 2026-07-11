#!/usr/bin/env python3
"""Compatibility entry point for the disk-focused unified TUI."""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scopes import tui

App = tui.App
V_PROC = tui.V_PROC
V_VOL = tui.V_VOL


def main(argv=None):
    argv = sys.argv if argv is None else argv
    return tui.main(argv, initial_tab="disk")


if __name__ == "__main__":
    sys.exit(main())
