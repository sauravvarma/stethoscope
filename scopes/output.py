#!/usr/bin/env python3
"""
stethoscope output — the agent-facing contract shared by every scope.

Three guarantees make each probe machine-consumable (see issues #14–#16):

  --json                 emit structured output straight from the data layer
                         instead of the human table. One JSON document per
                         sample; live views stream newline-delimited docs.
  --once / --duration N  non-interactive sampling: take one interval and exit,
                         or sample for N seconds and exit — so agents and
                         scripts never have to sit in a refresh loop.
  exit codes             probes double as checks: 0 = ran clean, 1 = ran and
                         found the thing (holders exist, health failing…),
                         2 = bad invocation, 3 = needs root.

The JSON shapes and their stability guarantees are documented in SCHEMA.md;
every document carries a top-level "schema" version so consumers can pin it.

No third-party dependencies — system Python 3 stdlib only.
"""

import json
import sys

# Bump when a JSON shape changes incompatibly; documented in SCHEMA.md.
SCHEMA_VERSION = 1

# Meaningful exit codes, shared across scopes (#16).
EXIT_OK = 0          # ran fine; nothing notable / clean
EXIT_FINDINGS = 1    # ran fine, and the probe found something (holders, fail…)
EXIT_USAGE = 2       # bad invocation
EXIT_PERM = 3        # needs root / permission denied


def document(scope, command, **fields):
    """Build a JSON document with the standard envelope every scope emits."""
    doc = {"schema": SCHEMA_VERSION, "scope": scope, "command": command}
    doc.update(fields)
    return doc


def emit_json(obj):
    """Write one JSON document + newline to stdout (NDJSON-friendly)."""
    json.dump(obj, sys.stdout, default=str)
    sys.stdout.write("\n")
    sys.stdout.flush()


class Opts:
    """The parsed common flags. `rest` holds leftover positionals (pid, volume)."""
    __slots__ = ("json", "once", "duration", "interval", "limit", "rest")

    def __init__(self):
        self.json = False
        self.once = False
        self.duration = None
        self.interval = 1.0
        self.limit = 20
        self.rest = []


class OptsError(ValueError):
    """Raised on a malformed flag value; callers map it to EXIT_USAGE."""


def parse_opts(args, defaults=None):
    """Pull the shared agent flags out of `args`.

    Recognises --json, --once, --duration N, --interval N, --limit N; anything
    else is preserved in Opts.rest in order. `defaults` may override interval /
    limit. Raises OptsError on a missing or non-numeric value.
    """
    o = Opts()
    if defaults:
        o.interval = defaults.get("interval", o.interval)
        o.limit = defaults.get("limit", o.limit)

    def value(i, name, cast):
        if i + 1 >= len(args):
            raise OptsError("%s needs a value" % name)
        try:
            return cast(args[i + 1])
        except ValueError:
            raise OptsError("%s wants a number, got %r" % (name, args[i + 1]))

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--json":
            o.json = True
        elif a == "--once":
            o.once = True
        elif a == "--duration":
            o.duration = value(i, "--duration", float)
            i += 1
        elif a == "--interval":
            o.interval = value(i, "--interval", float)
            i += 1
        elif a == "--limit":
            o.limit = value(i, "--limit", int)
            i += 1
        elif a.startswith("-") and a not in ("-",):
            raise OptsError("unknown option: %s" % a)
        else:
            o.rest.append(a)
        i += 1
    return o
