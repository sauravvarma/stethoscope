"""Unit tests for the MCP server (scopes/mcp_server.py).

The JSON-RPC dispatch and tool registry are tested directly; the tool handlers
that hit live kernel data are exercised via a stubbed registry.
"""

import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scopes"))
import mcp_server as mcp  # noqa: E402


class TestInitialize(unittest.TestCase):
    def test_handshake(self):
        resp = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(resp["id"], 1)
        self.assertEqual(resp["result"]["protocolVersion"], mcp.PROTOCOL_VERSION)
        self.assertIn("tools", resp["result"]["capabilities"])
        self.assertEqual(resp["result"]["serverInfo"]["name"], "stethoscope")

    def test_notification_gets_no_response(self):
        self.assertIsNone(mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))


class TestToolsList(unittest.TestCase):
    def test_lists_all_tools_with_schemas(self):
        resp = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = resp["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertEqual(names, set(mcp.TOOLS))
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")
        holds = next(t for t in tools if t["name"] == "disk_holds")
        self.assertEqual(holds["inputSchema"]["required"], ["pid"])


class TestToolsCall(unittest.TestCase):
    def test_calls_handler_and_wraps_document(self):
        doc = {"schema": 1, "scope": "cpu", "command": "top", "processes": []}
        with mock.patch.dict(mcp.TOOLS,
                             {"cpu_top": ("d", {}, lambda a: doc)}, clear=False):
            resp = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                               "params": {"name": "cpu_top", "arguments": {}}})
        self.assertFalse(resp["result"]["isError"])
        self.assertEqual(resp["result"]["structuredContent"], doc)
        self.assertEqual(json.loads(resp["result"]["content"][0]["text"]), doc)

    def test_unknown_tool_is_error(self):
        resp = mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                           "params": {"name": "nope", "arguments": {}}})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_tool_exception_becomes_tool_error(self):
        def boom(a):
            raise RuntimeError("kaboom")
        with mock.patch.dict(mcp.TOOLS, {"cpu_top": ("d", {}, boom)}, clear=False):
            resp = mcp.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                               "params": {"name": "cpu_top", "arguments": {}}})
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("kaboom", resp["result"]["content"][0]["text"])


class TestUnknownMethod(unittest.TestCase):
    def test_request_gets_method_not_found(self):
        resp = mcp.handle({"jsonrpc": "2.0", "id": 6, "method": "bogus"})
        self.assertEqual(resp["error"]["code"], -32601)


class TestServeLoop(unittest.TestCase):
    def test_ndjson_in_ndjson_out(self):
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
        stdout = io.StringIO()
        mcp.serve(stdin, stdout)
        lines = [l for l in stdout.getvalue().splitlines() if l]
        self.assertEqual(len(lines), 2)   # initialize + tools/list; notification silent
        self.assertEqual(json.loads(lines[0])["id"], 1)
        self.assertEqual(json.loads(lines[1])["id"], 2)


if __name__ == "__main__":
    unittest.main()
