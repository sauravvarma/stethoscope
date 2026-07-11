"""Stable machine-readable document envelopes.

Data layers return structures. Surface commands wrap those structures with
this module before a CLI or protocol encoder serializes them.
"""

SCHEMA_VERSION = "stethoscope/1"

_RESERVED = {"schema", "scope", "command", "partial", "partial_reasons"}


def document(scope, command, partial=False, partial_reasons=None, **fields):
    """Return one schema-versioned command document."""
    conflict = _RESERVED.intersection(fields)
    if conflict:
        raise ValueError("reserved document field(s): %s" %
                         ", ".join(sorted(conflict)))
    return {
        "schema": SCHEMA_VERSION,
        "scope": scope,
        "command": command,
        "partial": bool(partial),
        "partial_reasons": list(partial_reasons or ()),
        **fields
    }
