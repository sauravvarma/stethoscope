"""Shared command-line parsing, JSON emission, and exit-code contract."""

import json
import math
import os
import sys

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_PERMISSION = 3
EXIT_ERROR = 4


class OptionsError(ValueError):
    """A malformed or unsupported command-line option."""


class Options:
    __slots__ = ("json", "once", "duration", "interval", "limit", "rest",
                 "provided")

    def __init__(self, interval=1.0, limit=20):
        self.json = False
        self.once = False
        self.duration = None
        self.interval = interval
        self.limit = limit
        self.rest = []
        self.provided = set()


def _number(args, index, name, cast):
    if index + 1 >= len(args):
        raise OptionsError("%s needs a value" % name)
    raw = args[index + 1]
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        raise OptionsError("%s wants a number, got %r" % (name, raw))
    if isinstance(value, float) and not math.isfinite(value):
        raise OptionsError("%s must be finite" % name)
    return value


def parse_options(args, interval=1.0, limit=20):
    """Parse common surface flags and preserve positional arguments."""
    options = Options(interval=interval, limit=limit)
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--json":
            options.json = True
            options.provided.add("json")
        elif arg == "--once":
            options.once = True
            options.provided.add("once")
        elif arg == "--duration":
            options.duration = _number(args, index, arg, float)
            options.provided.add("duration")
            index += 1
        elif arg == "--interval":
            options.interval = _number(args, index, arg, float)
            options.provided.add("interval")
            index += 1
        elif arg == "--limit":
            options.limit = _number(args, index, arg, int)
            options.provided.add("limit")
            index += 1
        elif arg.startswith("-") and arg != "-":
            raise OptionsError("unknown option: %s" % arg)
        else:
            options.rest.append(arg)
        index += 1

    if options.interval <= 0:
        raise OptionsError("--interval must be > 0")
    if options.limit <= 0:
        raise OptionsError("--limit must be > 0")
    if options.duration is not None and options.duration <= 0:
        raise OptionsError("--duration must be > 0")
    return options


def require_options(options, command, allowed):
    """Reject parsed flags that a command cannot honor."""
    unsupported = options.provided.difference(allowed)
    if unsupported:
        names = ", ".join("--" + name for name in sorted(unsupported))
        raise OptionsError("%s does not support %s" % (command, names))


def require_positionals(options, command, count):
    """Require exactly ``count`` positional arguments."""
    if len(options.rest) != count:
        raise OptionsError("%s needs %d argument%s" %
                           (command, count, "" if count == 1 else "s"))
    return options.rest


def emit_json(value, stream=None):
    """Write one strict JSON document followed by a newline."""
    stream = stream or sys.stdout
    json.dump(value, stream, allow_nan=False, separators=(",", ":"))
    stream.write("\n")
    stream.flush()


def is_root():
    return os.geteuid() == 0
