"""Unit tests for the shared agent-output contract (scopes/output.py)."""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scopes"))
import output  # noqa: E402


class TestExitCodes(unittest.TestCase):
    def test_distinct_and_stable(self):
        self.assertEqual(
            (output.EXIT_OK, output.EXIT_FINDINGS, output.EXIT_USAGE, output.EXIT_PERM),
            (0, 1, 2, 3))


class TestDocumentAndEmit(unittest.TestCase):
    def test_document_envelope(self):
        doc = output.document("disk", "top", system={"read_per_s": 1.0})
        self.assertEqual(doc["schema"], output.SCHEMA_VERSION)
        self.assertEqual(doc["scope"], "disk")
        self.assertEqual(doc["command"], "top")
        self.assertEqual(doc["system"], {"read_per_s": 1.0})

    def test_emit_json_is_one_line_and_parses(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            output.emit_json(output.document("disk", "holds", pid=1))
        out = buf.getvalue()
        self.assertTrue(out.endswith("\n"))
        self.assertEqual(out.count("\n"), 1)          # single NDJSON line
        self.assertEqual(json.loads(out)["pid"], 1)


class TestParseOpts(unittest.TestCase):
    def test_defaults(self):
        o = output.parse_opts([])
        self.assertFalse(o.json or o.once)
        self.assertIsNone(o.duration)
        self.assertEqual((o.interval, o.limit, o.rest), (1.0, 20, []))

    def test_flags_and_positionals(self):
        o = output.parse_opts(["--json", "--once", "--interval", "2.5",
                               "--limit", "5", "1234"])
        self.assertTrue(o.json and o.once)
        self.assertEqual(o.interval, 2.5)
        self.assertEqual(o.limit, 5)
        self.assertEqual(o.rest, ["1234"])

    def test_duration(self):
        self.assertEqual(output.parse_opts(["--duration", "3"]).duration, 3.0)

    def test_positional_volume_with_spaces_preserved(self):
        o = output.parse_opts(["--json", "/Volumes/X9 Pro"])
        self.assertEqual(o.rest, ["/Volumes/X9 Pro"])

    def test_bad_numeric_raises(self):
        with self.assertRaises(output.OptsError):
            output.parse_opts(["--interval", "fast"])

    def test_missing_value_raises(self):
        with self.assertRaises(output.OptsError):
            output.parse_opts(["--limit"])

    def test_unknown_flag_raises(self):
        with self.assertRaises(output.OptsError):
            output.parse_opts(["--bogus"])


if __name__ == "__main__":
    unittest.main()
