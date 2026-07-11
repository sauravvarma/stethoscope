"""Hermetic protocol, transport, registry, and launcher tests for MCP."""

import io
import json
import math
import os
import subprocess
import unittest
from unittest import mock

from core import cli
from scopes import mcp_server as mcp


def initialize(session, version=mcp.PROTOCOL_VERSION, message_id=1):
    return session.handle({
        "jsonrpc": "2.0",
        "id": message_id,
        "method": "initialize",
        "params": {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "tests", "version": "1"},
        },
    })


def ready_session():
    session = mcp.Session()
    initialize(session)
    session.handle({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    })
    return session


def request(message_id, method, params=None):
    message = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def document(scope="cpu", command="top"):
    return {
        "schema": "stethoscope/1",
        "scope": scope,
        "command": command,
        "partial": False,
        "partial_reasons": [],
    }


class TestLifecycle(unittest.TestCase):
    def test_supported_handshake_and_server_version(self):
        response = initialize(mcp.Session(), message_id="init")
        self.assertEqual(response["id"], "init")
        self.assertEqual(
            response["result"]["protocolVersion"], mcp.PROTOCOL_VERSION)
        self.assertEqual(response["result"]["capabilities"], {
            "tools": {"listChanged": False},
        })
        self.assertEqual(
            response["result"]["serverInfo"]["version"], mcp.VERSION)
        with open(os.path.join(os.path.dirname(__file__), "..", "VERSION")) as fh:
            self.assertEqual(mcp.VERSION, fh.read().strip())

    def test_unsupported_version_negotiates_newest_supported(self):
        response = initialize(mcp.Session(), "1900-01-01")
        self.assertEqual(
            response["result"]["protocolVersion"], mcp.PROTOCOL_VERSION)

    def test_initialize_requires_all_fields_and_valid_shapes(self):
        base = {
            "protocolVersion": mcp.PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "x", "version": "1"},
        }
        cases = []
        for field in base:
            params = dict(base)
            del params[field]
            cases.append(params)
        cases.extend([
            [],
            dict(base, protocolVersion=1),
            dict(base, capabilities=[]),
            dict(base, clientInfo={}),
            dict(base, extra=True),
        ])
        for index, params in enumerate(cases):
            with self.subTest(params=params):
                response = mcp.Session().handle(
                    request(index, "initialize", params))
                self.assertEqual(
                    response["error"]["code"], mcp.INVALID_PARAMS)

    def test_initialize_must_be_first_request(self):
        response = mcp.Session().handle(request(1, "ping"))
        self.assertEqual(response["error"]["code"], mcp.INVALID_REQUEST)

    def test_duplicate_initialize_is_rejected(self):
        session = mcp.Session()
        initialize(session)
        response = initialize(session, message_id=2)
        self.assertEqual(response["error"]["code"], mcp.INVALID_REQUEST)

    def test_initialized_notification_gates_operation(self):
        session = mcp.Session()
        initialize(session)
        blocked = session.handle(request(2, "ping"))
        self.assertEqual(blocked["error"]["code"], mcp.INVALID_REQUEST)
        self.assertIsNone(session.handle({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }))
        self.assertEqual(session.handle(request(3, "ping"))["result"], {})

    def test_invalid_initialized_notification_is_silent_and_does_not_transition(self):
        session = mcp.Session()
        initialize(session)
        response = session.handle({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": [],
        })
        self.assertIsNone(response)
        self.assertFalse(session.ready)

    def test_metadata_is_valid_on_lifecycle_and_standard_requests(self):
        session = mcp.Session()
        response = session.handle(request(1, "initialize", {
            "protocolVersion": mcp.PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "tests", "version": "1"},
            "_meta": {"trace": "init"},
        }))
        self.assertIn("result", response)
        self.assertIsNone(session.handle({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {"_meta": {"trace": "ready"}},
        }))
        self.assertTrue(session.ready)
        self.assertEqual(session.handle(request(
            2, "ping", {"_meta": {"trace": "ping"}}))["result"], {})
        self.assertIn("tools", session.handle(request(
            3, "tools/list", {"_meta": {"trace": "list"}}))["result"])

    def test_metadata_must_be_an_object(self):
        session = ready_session()
        response = session.handle(request(2, "ping", {"_meta": "bad"}))
        self.assertEqual(response["error"]["code"], mcp.INVALID_PARAMS)


class TestRequestValidation(unittest.TestCase):
    def test_non_objects_and_bad_envelopes_are_invalid_requests(self):
        session = mcp.Session()
        cases = [
            None, [], 1, "request",
            {"jsonrpc": "1.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "id": 1},
            {"jsonrpc": "2.0", "id": 1, "method": 3},
            {"jsonrpc": "2.0", "id": 1, "method": "x", "params": 3},
            {"jsonrpc": "2.0", "id": 1, "method": "x", "extra": True},
        ]
        for value in cases:
            with self.subTest(value=value):
                response = session.handle(value)
                self.assertEqual(
                    response["error"]["code"], mcp.INVALID_REQUEST)

    def test_invalid_request_preserves_a_readable_id(self):
        response = mcp.Session().handle({
            "jsonrpc": "2.0", "id": "req-7", "method": 3,
        })
        self.assertEqual(response["id"], "req-7")
        self.assertEqual(response["error"]["code"], mcp.INVALID_REQUEST)

    def test_integer_and_string_ids_are_preserved(self):
        session = ready_session()
        self.assertEqual(session.handle(request(12, "ping"))["id"], 12)
        self.assertEqual(session.handle(request("abc", "ping"))["id"], "abc")

    def test_null_bool_and_fractional_ids_are_rejected(self):
        for bad_id in (
                None, True, False, 1.5,
                mcp.MIN_INTEGER_ID - 1, mcp.MAX_INTEGER_ID + 1):
            with self.subTest(message_id=bad_id):
                response = ready_session().handle(
                    request(bad_id, "ping"))
                self.assertEqual(response["id"], None)
                self.assertEqual(
                    response["error"]["code"], mcp.INVALID_REQUEST)

    def test_reused_id_is_rejected(self):
        session = ready_session()
        session.handle(request(7, "ping"))
        response = session.handle(request(7, "ping"))
        self.assertEqual(response["id"], 7)
        self.assertEqual(response["error"]["code"], mcp.INVALID_REQUEST)

    def test_request_id_size_and_session_namespace_are_bounded(self):
        response = ready_session().handle(request(
            "x" * (mcp.MAX_ID_BYTES + 1), "ping"))
        self.assertIsNone(response["id"])
        self.assertEqual(response["error"]["code"], mcp.INVALID_REQUEST)

        with mock.patch.object(mcp, "MAX_REQUESTS", 1):
            session = mcp.Session()
            initialize(session, message_id="init")
            session.handle({
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            })
            limited = session.handle(request("next", "ping"))
        self.assertEqual(limited["id"], "next")
        self.assertIn("limit", limited["error"]["message"])
        self.assertEqual(len(session.seen_ids), 1)

    def test_unknown_request_method(self):
        response = ready_session().handle(request(2, "no/such/method"))
        self.assertEqual(response["error"]["code"], mcp.METHOD_NOT_FOUND)

    def test_valid_known_and_unknown_notifications_are_silent(self):
        session = ready_session()
        for method, params in (
                ("ping", None),
                ("notifications/progress", {"progress": 1}),
                ("unknown/notification", [])):
            message = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                message["params"] = params
            with self.subTest(method=method):
                self.assertIsNone(session.handle(message))

    def test_method_parameter_contracts(self):
        session = ready_session()
        for index, method in enumerate(("ping", "tools/list"), 10):
            response = session.handle(
                request(index, method, {"unexpected": True}))
            self.assertEqual(response["error"]["code"], mcp.INVALID_PARAMS)


class TestRegistry(unittest.TestCase):
    EXPECTED = {
        "disk_top", "disk_holds", "disk_busy", "cpu_top", "cpu_wakeups",
        "memory_top", "battery_health", "battery_top", "smart_status",
        "checkup",
    }

    def test_exact_ten_tools_have_input_and_output_schemas(self):
        response = ready_session().handle(request(2, "tools/list"))
        tools = response["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, self.EXPECTED)
        self.assertEqual(len(tools), 10)
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
            self.assertEqual(tool["outputSchema"]["type"], "object")
            self.assertIn("schema", tool["outputSchema"]["required"])

    def test_required_and_optional_arguments_match_contract(self):
        specs = {item["name"]: item for item in mcp.tool_specs()}
        self.assertEqual(specs["disk_holds"]["inputSchema"]["required"], ["pid"])
        self.assertEqual(
            specs["disk_busy"]["inputSchema"]["required"], ["target"])
        self.assertEqual(specs["memory_top"]["inputSchema"]["required"], [])
        self.assertEqual(specs["battery_health"]["inputSchema"]["properties"], {})
        self.assertIn("disk", specs["smart_status"]["inputSchema"]["properties"])


class TestToolArguments(unittest.TestCase):
    def call(self, name, arguments, message_id=20):
        return ready_session().handle(request(
            message_id, "tools/call",
            {"name": name, "arguments": arguments}))

    def assert_invalid(self, name, arguments):
        response = self.call(name, arguments)
        self.assertEqual(response["error"]["code"], mcp.INVALID_PARAMS)

    def test_unknown_tool_and_bad_call_params(self):
        self.assert_invalid("not_a_tool", {})
        for params in (None, [], {}, {"name": 1}, {"name": "cpu_top", "x": 1}):
            response = ready_session().handle(
                request(20, "tools/call", params))
            self.assertEqual(response["error"]["code"], mcp.INVALID_PARAMS)

    def test_tool_call_accepts_request_metadata(self):
        result = document("battery", "health")
        tool = mcp.Tool(
            "battery_health", "test", mcp._EMPTY_SCHEMA,
            mcp._output_schema("battery", "health"),
            lambda arguments: (result, cli.EXIT_OK))
        session = ready_session()
        with mock.patch.dict(
                mcp.TOOL_REGISTRY, {"battery_health": tool}, clear=False):
            response = session.handle(request(20, "tools/call", {
                "name": "battery_health", "arguments": {},
                "_meta": {"progressToken": "p"},
            }))
        self.assertEqual(response["result"]["structuredContent"], result)

    def test_pid_target_and_disk_validation(self):
        for value in (True, 0, -1, 2.0, "2"):
            self.assert_invalid("disk_holds", {"pid": value})
        for value in ("", "   ", 1):
            self.assert_invalid("disk_busy", {"target": value})
            self.assert_invalid("smart_status", {"disk": value})

    def test_interval_types_ranges_and_nonfinite_values(self):
        for value in (
                True, 0, -1, 60.1, "1", float("nan"), float("inf")):
            self.assert_invalid("cpu_top", {"interval": value})

    def test_limit_types_ranges_and_extras(self):
        for value in (True, 0, -1, 257, 1.5, "1"):
            self.assert_invalid("memory_top", {"limit": value})
        self.assert_invalid("battery_health", {"limit": 1})
        self.assert_invalid("disk_top", {"other": 1})

    def test_repeated_tool_invocation_is_bounded(self):
        session = ready_session()
        session.tool_calls = mcp.MAX_TOOL_CALLS
        response = session.handle(request(
            20, "tools/call",
            {"name": "battery_health", "arguments": {}}))
        self.assertEqual(response["error"]["code"], mcp.INVALID_PARAMS)
        self.assertIn("limit", response["error"]["message"])


class TestToolResults(unittest.TestCase):
    def call_with(self, handler, exit_code=cli.EXIT_OK, value=None):
        value = document() if value is None else value
        tool = mcp.Tool(
            "cpu_top", "test", mcp._EMPTY_SCHEMA,
            mcp._output_schema("cpu", "top"),
            lambda arguments: (handler(value), exit_code))
        with mock.patch.dict(
                mcp.TOOL_REGISTRY, {"cpu_top": tool}, clear=False):
            return ready_session().handle(request(
                20, "tools/call",
                {"name": "cpu_top", "arguments": {}}))

    def test_text_is_compact_strict_json_identical_to_structured(self):
        response = self.call_with(lambda value: value)
        result = response["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(
            json.loads(result["content"][0]["text"]),
            result["structuredContent"])
        self.assertNotIn(": ", result["content"][0]["text"])

    def test_exit_zero_and_one_are_successful_tool_results(self):
        for code in (cli.EXIT_OK, cli.EXIT_FINDINGS):
            with self.subTest(code=code):
                self.assertFalse(
                    self.call_with(lambda value: value, code)["result"]["isError"])

    def test_exit_two_three_and_four_are_tool_errors(self):
        for code in (
                cli.EXIT_USAGE, cli.EXIT_PERMISSION, cli.EXIT_ERROR):
            with self.subTest(code=code):
                self.assertTrue(
                    self.call_with(lambda value: value, code)["result"]["isError"])

    def test_nan_and_nonserializable_results_are_internal_errors(self):
        for value in ({"bad": float("nan")}, {"bad": object()}):
            with self.subTest(value=value):
                response = self.call_with(lambda ignored: value)
                self.assertEqual(
                    response["error"]["code"], mcp.INTERNAL_ERROR)

    def test_invalid_exit_code_is_internal_error(self):
        response = self.call_with(lambda value: value, 99)
        self.assertEqual(response["error"]["code"], mcp.INTERNAL_ERROR)

    def test_result_size_is_bounded(self):
        with mock.patch.object(mcp, "MAX_RESULT_BYTES", 10):
            response = self.call_with(
                lambda ignored: {"large": "x" * 100})
        self.assertEqual(response["error"]["code"], mcp.INTERNAL_ERROR)

    def test_tool_exception_is_internal_error_without_details(self):
        def explode(_value):
            raise OSError("private path detail")

        response = self.call_with(explode)
        self.assertEqual(response["error"]["code"], mcp.INTERNAL_ERROR)
        self.assertNotIn("private path", response["error"]["message"])


class CountingOutput(io.StringIO):
    def __init__(self):
        super().__init__()
        self.writes = 0
        self.flushes = 0

    def write(self, value):
        self.writes += 1
        return super().write(value)

    def flush(self):
        self.flushes += 1
        return super().flush()


class TestTransport(unittest.TestCase):
    def test_malformed_json_returns_parse_error_then_recovers(self):
        messages = (
            "{broken\n"
            + json.dumps(request(1, "initialize", {
                "protocolVersion": mcp.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            })) + "\n"
        )
        output = io.StringIO()
        self.assertEqual(mcp.serve(io.StringIO(messages), output), cli.EXIT_OK)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], mcp.PARSE_ERROR)
        self.assertIn("result", responses[1])

    def test_nonstandard_nan_json_is_parse_error_and_recovers(self):
        source = io.StringIO("NaN\n{}\n")
        output = io.StringIO()
        mcp.serve(source, output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], mcp.PARSE_ERROR)
        self.assertEqual(responses[1]["error"]["code"], mcp.INVALID_REQUEST)

    def test_deeply_nested_json_is_parse_error_and_recovers(self):
        nested = "[" * 2000 + "]" * 2000
        source = io.StringIO(nested + "\n{}\n")
        output = io.StringIO()
        mcp.serve(source, output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], mcp.PARSE_ERROR)
        self.assertEqual(responses[1]["error"]["code"], mcp.INVALID_REQUEST)

    def test_oversized_integer_is_rejected_before_conversion_and_recovers(self):
        oversized = (
            '{"jsonrpc":"2.0","id":' +
            "9" * (mcp.MAX_JSON_NUMBER_CHARS + 1) +
            ',"method":"ping"}\n')
        output = io.StringIO()
        mcp.serve(io.StringIO(oversized + "{}\n"), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], mcp.PARSE_ERROR)
        self.assertEqual(responses[1]["error"]["code"], mcp.INVALID_REQUEST)

    def test_signed_64_bit_integer_ids_are_preserved(self):
        session = ready_session()
        for offset, message_id in enumerate(
                (mcp.MIN_INTEGER_ID, mcp.MAX_INTEGER_ID), 2):
            response = session.handle(request(message_id, "ping"))
            self.assertEqual(response["id"], message_id)

    def test_lone_surrogate_id_cannot_break_transport(self):
        bad = json.dumps(request("\ud800", "ping")) + "\n"
        output = io.StringIO()
        self.assertEqual(
            mcp.serve(io.StringIO(bad + "{}\n"), output), cli.EXIT_OK)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], mcp.INVALID_REQUEST)
        self.assertEqual(responses[1]["error"]["code"], mcp.INVALID_REQUEST)

    def test_valid_notification_produces_no_write_or_flush(self):
        source = io.StringIO(json.dumps({
            "jsonrpc": "2.0", "method": "unknown/notification",
        }) + "\n")
        output = CountingOutput()
        mcp.serve(source, output)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(output.writes, 0)
        self.assertEqual(output.flushes, 0)

    def test_complete_response_is_one_write_and_one_flush(self):
        source = io.StringIO(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "ping",
        }) + "\n")
        output = CountingOutput()
        mcp.serve(source, output)
        self.assertEqual(output.writes, 1)
        self.assertEqual(output.flushes, 1)
        self.assertEqual(output.getvalue().count("\n"), 1)

    def test_eof_exits_zero_without_output(self):
        output = CountingOutput()
        self.assertEqual(mcp.serve(io.StringIO(""), output), cli.EXIT_OK)
        self.assertEqual(output.getvalue(), "")

    def test_oversized_line_is_rejected_and_next_line_recovers(self):
        oversized = " " * (mcp.MAX_INPUT_BYTES + 1) + "\n"
        source = io.StringIO(oversized + json.dumps([]) + "\n")
        output = io.StringIO()
        mcp.serve(source, output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], mcp.INVALID_REQUEST)
        self.assertEqual(responses[1]["error"]["code"], mcp.INVALID_REQUEST)

    def test_invalid_utf8_is_parse_error(self):
        output = io.StringIO()
        mcp.serve(io.BytesIO(b"\xff\n"), output)
        response = json.loads(output.getvalue())
        self.assertEqual(response["error"]["code"], mcp.PARSE_ERROR)


class TestToolAdapters(unittest.TestCase):
    def test_memory_has_no_artificial_delay(self):
        with mock.patch.object(
                mcp.memory, "top_result",
                return_value=(document("memory", "top"), 0)) as result, \
                mock.patch.object(mcp.time, "sleep") as sleep:
            actual = mcp._tool_memory_top({"limit": 3})
        result.assert_called_once_with(3)
        sleep.assert_not_called()
        self.assertEqual(actual[0]["scope"], "memory")

    def test_disk_holds_and_busy_use_structured_helpers(self):
        with mock.patch.object(
                mcp.disk, "holds_result",
                return_value=(document("disk", "holds"), 0)) as holds:
            mcp._tool_disk_holds({"pid": 42})
        holds.assert_called_once_with(42)
        with mock.patch.object(
                mcp.disk, "busy_result",
                return_value=(document("disk", "busy"), 1)) as busy:
            mcp._tool_disk_busy({"target": "/Volumes/X"})
        busy.assert_called_once_with("/Volumes/X")

    def test_checkup_document_is_not_wrapped(self):
        source = document("checkup", "checkup")
        with mock.patch.object(
                mcp.checkup, "run", return_value=(source, 1)) as run:
            actual = mcp._tool_checkup({"interval": 2, "limit": 4})
        self.assertIs(actual[0], source)
        self.assertEqual(actual[1], 1)
        run.assert_called_once_with(interval=2.0, limit=4)


class TestLauncher(unittest.TestCase):
    def test_subprocess_handshake_and_stdout_purity(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        payload = "\n".join([
            json.dumps(request(1, "initialize", {
                "protocolVersion": mcp.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "subprocess", "version": "1"},
            })),
            json.dumps({
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }),
            json.dumps(request(2, "tools/list")),
            "",
        ])
        completed = subprocess.run(
            ["./stethoscope", "mcp"], cwd=root, input=payload,
            text=True, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, cli.EXIT_OK)
        self.assertEqual(completed.stderr, "")
        responses = [
            json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["id"], 1)
        self.assertEqual(len(responses[1]["result"]["tools"]), 10)

    def test_launcher_usage_error_exits_two(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        completed = subprocess.run(
            ["./stethoscope", "mcp", "extra"], cwd=root,
            text=True, capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, cli.EXIT_USAGE)
        self.assertEqual(completed.stdout, "")
        self.assertIn("usage:", completed.stderr)


if __name__ == "__main__":
    unittest.main()
