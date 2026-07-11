"""Hermetic tests for the shared CLI and JSON contract."""

import io
import json
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from core import cli, schema
from scopes import disk


class TestOptions(unittest.TestCase):
    def test_defaults_and_values(self):
        options = cli.parse_options([
            "--json", "--once", "--duration", "3", "--interval", "0.5",
            "--limit", "4", "target",
        ])
        self.assertTrue(options.json)
        self.assertTrue(options.once)
        self.assertEqual(options.duration, 3.0)
        self.assertEqual(options.interval, 0.5)
        self.assertEqual(options.limit, 4)
        self.assertEqual(options.rest, ["target"])

    def test_numeric_values_must_be_positive_and_finite(self):
        for args in (
                ["--interval", "0"], ["--interval", "-1"],
                ["--duration", "0"], ["--duration", "nan"],
                ["--limit", "0"]):
            with self.subTest(args=args), self.assertRaises(cli.OptionsError):
                cli.parse_options(args)

    def test_missing_bad_and_unknown_values_fail(self):
        for args in (
                ["--limit"], ["--interval", "fast"], ["--unknown"]):
            with self.subTest(args=args), self.assertRaises(cli.OptionsError):
                cli.parse_options(args)

    def test_command_rejects_unsupported_flags(self):
        options = cli.parse_options(["--json", "--once"])
        with self.assertRaises(cli.OptionsError):
            cli.require_options(options, "holds", {"json"})


class TestSchema(unittest.TestCase):
    def test_document_has_stable_envelope(self):
        document = schema.document("disk", "top", rows=[])
        self.assertEqual(document, {
            "schema": "stethoscope/1",
            "scope": "disk",
            "command": "top",
            "partial": False,
            "partial_reasons": [],
            "rows": [],
        })

    def test_reserved_fields_cannot_be_overwritten(self):
        with self.assertRaises(ValueError):
            schema.document("disk", "top", schema="other")

    def test_json_emission_is_one_strict_line(self):
        stream = io.StringIO()
        cli.emit_json(schema.document("disk", "top"), stream)
        self.assertEqual(stream.getvalue().count("\n"), 1)
        self.assertEqual(json.loads(stream.getvalue())["scope"], "disk")

    def test_human_external_text_replaces_terminal_controls(self):
        self.assertEqual(cli.safe_text("ok\x1b[2J\nname"), "ok?[2J?name")


class TestDiskContract(unittest.TestCase):
    def test_top_document_marks_non_root_visibility_partial(self):
        rows = [(300.0, 200.0, 100.0, 1000, 500, 7, "writer")]
        with mock.patch.object(cli, "is_root", return_value=False):
            document = disk._top_document(rows, 200.0, 100.0, 20)
        self.assertTrue(document["partial"])
        self.assertEqual(document["partial_reasons"], ["not_root"])
        self.assertEqual(document["processes"][0]["pid"], 7)

    def test_holds_error_keeps_cumulative_field(self):
        options = cli.parse_options(["--json"])
        with mock.patch.object(disk, "proc_name", return_value="bash"), \
                mock.patch.object(disk, "proc_diskio", return_value=(1, 2)), \
                mock.patch.object(
                    disk, "open_files",
                    side_effect=subprocess.TimeoutExpired(["lsof"], 15)):
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = disk.cmd_holds(42, options)
        document = json.loads(stream.getvalue())
        self.assertEqual(result, cli.EXIT_ERROR)
        self.assertIn("cumulative", document)
        self.assertIsNone(document["cumulative"])
        self.assertEqual(document["holds"], [])

    def test_busy_json_reports_findings_and_partial_visibility(self):
        options = cli.parse_options(["--json"])
        processes = {
            9: {
                "name": "mds",
                "user": "root",
                "holds": [("open (read)", "/Volumes/X/a")],
            }
        }
        with mock.patch.object(
                disk, "resolve_volume",
                return_value=[("/dev/disk2s1", "/Volumes/X")]), \
                mock.patch.object(disk, "collect_holders",
                                  return_value=processes), \
                mock.patch.object(disk, "proc_diskio", return_value=None), \
                mock.patch.object(cli, "is_root", return_value=False):
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = disk.cmd_busy("/Volumes/X", options)
        document = json.loads(stream.getvalue())
        self.assertEqual(result, cli.EXIT_FINDINGS)
        self.assertTrue(document["partial"])
        self.assertEqual(document["holders"][0]["pid"], 9)

    def test_inspect_rejects_agent_flags(self):
        stderr = io.StringIO()
        with mock.patch.object(disk, "cmd_inspect") as inspect, \
                redirect_stderr(stderr):
            result = disk.main(["stethoscope disk", "inspect", "42", "--json"])
        self.assertEqual(result, cli.EXIT_USAGE)
        self.assertFalse(inspect.called)
        self.assertIn("does not support --json", stderr.getvalue())

    def test_top_once_emits_exactly_one_document(self):
        options = cli.parse_options(["--json", "--once"])
        snapshots = [
            {(7, 70): (100, 200)},
            {(7, 70): (300, 200)},
        ]
        with mock.patch.object(disk, "snapshot_diskio",
                               side_effect=snapshots), \
                mock.patch.object(disk.time, "sleep"), \
                mock.patch.object(disk.time, "monotonic",
                                  side_effect=[10.0, 11.0]), \
                mock.patch.object(cli, "is_root", return_value=True):
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = disk.cmd_top(options)
        self.assertEqual(result, cli.EXIT_OK)
        self.assertEqual(len(stream.getvalue().splitlines()), 1)

    def test_top_duration_repeats_until_elapsed(self):
        options = cli.parse_options([
            "--json", "--duration", "0.8", "--interval", "0.5",
        ])
        snapshots = [
            {(7, 70): (0, 0)},
            {(7, 70): (100, 0)},
            {(7, 70): (200, 0)},
        ]
        with mock.patch.object(disk, "snapshot_diskio",
                               side_effect=snapshots), \
                mock.patch.object(disk.time, "sleep") as sleep, \
                mock.patch.object(disk.time, "monotonic",
                                  side_effect=[10.0, 10.5, 11.0]), \
                mock.patch.object(cli, "is_root", return_value=True):
            stream = io.StringIO()
            with redirect_stdout(stream):
                result = disk.cmd_top(options)
        self.assertEqual(result, cli.EXIT_OK)
        self.assertEqual(len(stream.getvalue().splitlines()), 2)
        self.assertEqual(sleep.call_count, 2)

    def test_unstyled_top_frame_has_no_terminal_control_codes(self):
        frame = disk._top_frame([], 0, 0, 1.0, 20, styled=False)
        self.assertNotIn("\033", frame)


if __name__ == "__main__":
    unittest.main()
