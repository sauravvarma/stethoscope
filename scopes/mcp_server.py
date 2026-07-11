#!/usr/bin/env python3
"""Strict, zero-dependency MCP server over newline-delimited stdio."""

import json
import math
import os
import sys
import time
from collections import namedtuple

from core import cli
from scopes import battery, checkup, cpu, disk, memory, smart


PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = (PROTOCOL_VERSION,)
MAX_INPUT_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 4 * 1024 * 1024
MAX_TOOL_CALLS = 256
MAX_REQUESTS = 4096
MAX_ID_BYTES = 256
MAX_JSON_NUMBER_CHARS = 64
MIN_INTEGER_ID = -(2 ** 63)
MAX_INTEGER_ID = 2 ** 63 - 1
MAX_INTERVAL = 60.0
MAX_LIMIT = 256
DEFAULT_INTERVAL = 0.5
DEFAULT_LIMIT = 20

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(_ROOT, "VERSION"), encoding="utf-8") as _version_file:
    VERSION = _version_file.read().strip()

SERVER_INFO = {"name": "stethoscope", "version": VERSION}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ParamsError(ValueError):
    """Tool or method parameters do not satisfy the public contract."""


def _strict_dumps(value):
    return json.dumps(
        value, allow_nan=False, ensure_ascii=True, separators=(",", ":"))


def _strict_loads(value):
    def reject_constant(constant):
        raise ValueError("invalid JSON constant: %s" % constant)

    def parse_integer(text):
        if len(text) > MAX_JSON_NUMBER_CHARS:
            raise ValueError("JSON integer is too long")
        return int(text)

    def parse_float(text):
        if len(text) > MAX_JSON_NUMBER_CHARS:
            raise ValueError("JSON number is too long")
        number = float(text)
        if not math.isfinite(number):
            raise ValueError("JSON number must be finite")
        return number

    return json.loads(
        value, parse_constant=reject_constant,
        parse_int=parse_integer, parse_float=parse_float)


def _result(message_id, result):
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id, code, message):
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": code, "message": message},
    }


def _object(value, name):
    if not isinstance(value, dict):
        raise ParamsError("%s must be an object" % name)
    return value


def _properties(value, allowed, required=()):
    extra = set(value).difference(allowed)
    if extra:
        raise ParamsError(
            "unexpected properties: %s" % ", ".join(sorted(extra)))
    missing = set(required).difference(value)
    if missing:
        raise ParamsError(
            "missing properties: %s" % ", ".join(sorted(missing)))


def _request_params(value, allowed=(), required=()):
    value = _object(value, "params")
    _properties(value, set(allowed) | {"_meta"}, required)
    if "_meta" in value:
        _object(value["_meta"], "_meta")
    return value


def _interval(value):
    if (isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            or value > MAX_INTERVAL):
        raise ParamsError("interval must be a finite number > 0 and <= 60")
    return float(value)


def _limit(value):
    if (isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            or value > MAX_LIMIT):
        raise ParamsError("limit must be an integer from 1 through 256")
    return value


def _positive_pid(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ParamsError("pid must be a positive integer")
    return value


def _nonempty_string(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ParamsError("%s must be a nonempty string" % name)
    if len(value.encode("utf-8")) > 4096:
        raise ParamsError("%s is too long" % name)
    return value


def _sampling_args(arguments):
    arguments = _object(arguments, "arguments")
    _properties(arguments, {"interval", "limit"})
    return (
        _interval(arguments.get("interval", DEFAULT_INTERVAL)),
        _limit(arguments.get("limit", DEFAULT_LIMIT)),
    )


def _limit_args(arguments):
    arguments = _object(arguments, "arguments")
    _properties(arguments, {"limit"})
    return _limit(arguments.get("limit", DEFAULT_LIMIT))


def _no_args(arguments):
    arguments = _object(arguments, "arguments")
    _properties(arguments, set())


def _tool_disk_top(arguments):
    interval, limit = _sampling_args(arguments)
    previous = disk.snapshot_diskio()
    started = time.monotonic()
    time.sleep(interval)
    current = disk.snapshot_diskio()
    return disk.top_result(previous, current, time.monotonic() - started, limit)


def _tool_disk_holds(arguments):
    arguments = _object(arguments, "arguments")
    _properties(arguments, {"pid"}, {"pid"})
    return disk.holds_result(_positive_pid(arguments["pid"]))


def _tool_disk_busy(arguments):
    arguments = _object(arguments, "arguments")
    _properties(arguments, {"target"}, {"target"})
    return disk.busy_result(_nonempty_string(arguments["target"], "target"))


def _tool_cpu(arguments, command):
    interval, limit = _sampling_args(arguments)
    previous = cpu.snapshot_cpu()
    started = time.monotonic()
    time.sleep(interval)
    current = cpu.snapshot_cpu()
    return cpu.result(
        command, previous, current, time.monotonic() - started, limit)


def _tool_memory_top(arguments):
    return memory.top_result(_limit_args(arguments))


def _tool_battery_health(arguments):
    _no_args(arguments)
    return battery.health_result()


def _tool_battery_top(arguments):
    interval, limit = _sampling_args(arguments)
    model = battery.power_model()
    previous = battery.snapshot_power()
    started = time.monotonic()
    time.sleep(interval)
    current = battery.snapshot_power()
    return battery.top_result(
        previous, current, time.monotonic() - started, limit, model=model)


def _tool_smart_status(arguments):
    arguments = _object(arguments, "arguments")
    _properties(arguments, {"disk"})
    selected = None
    if "disk" in arguments:
        selected = _nonempty_string(arguments["disk"], "disk")
    return smart.status_result(selected)


def _tool_checkup(arguments):
    interval, limit = _sampling_args(arguments)
    return checkup.run(interval=interval, limit=limit)


def _object_schema(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_INTERVAL_SCHEMA = {
    "type": "number", "exclusiveMinimum": 0, "maximum": MAX_INTERVAL,
}
_LIMIT_SCHEMA = {
    "type": "integer", "minimum": 1, "maximum": MAX_LIMIT,
}
_SAMPLING_SCHEMA = _object_schema({
    "interval": _INTERVAL_SCHEMA,
    "limit": _LIMIT_SCHEMA,
})
_LIMIT_ONLY_SCHEMA = _object_schema({"limit": _LIMIT_SCHEMA})
_EMPTY_SCHEMA = _object_schema({})


def _output_schema(scope, command, properties=None, required=()):
    common = {
        "schema": {"type": "string", "const": "stethoscope/1"},
        "scope": {"type": "string", "const": scope},
        "command": {"type": "string", "const": command},
        "partial": {"type": "boolean"},
        "partial_reasons": {
            "type": "array", "items": {"type": "string"},
        },
    }
    common.update(properties or {})
    return {
        "type": "object",
        "properties": common,
        "required": [
            "schema", "scope", "command", "partial", "partial_reasons",
        ] + list(required),
        "additionalProperties": True,
    }


_SYSTEM_PROCESSES = {
    "system": {"type": "object"},
    "processes": {"type": "array", "items": {"type": "object"}},
}
_TOP_REQUIRED = ("system", "processes")


Tool = namedtuple(
    "Tool", ("name", "description", "input_schema", "output_schema", "handler"))

TOOLS = (
    Tool("disk_top", "Rank processes by current disk I/O.",
         _SAMPLING_SCHEMA,
         _output_schema("disk", "top", _SYSTEM_PROCESSES, _TOP_REQUIRED),
         _tool_disk_top),
    Tool("disk_holds", "List on-disk files held by a process.",
         _object_schema(
             {"pid": {"type": "integer", "minimum": 1}}, ("pid",)),
         _output_schema("disk", "holds", {
             "pid": {"type": "integer"},
             "name": {"type": "string"},
             "cumulative": {"type": ["object", "null"]},
             "holds": {"type": "array", "items": {"type": "object"}},
             "error": {"type": ["string", "null"]},
         }, ("pid", "name", "cumulative", "holds", "error")),
         _tool_disk_holds),
    Tool("disk_busy", "List processes pinning a mounted volume or device.",
         _object_schema(
             {"target": {
                 "type": "string", "minLength": 1, "maxLength": 4096,
             }}, ("target",)),
         _output_schema("disk", "busy", {
             "target": {"type": "string"},
             "targets": {"type": "array", "items": {"type": "object"}},
             "holders": {"type": "array", "items": {"type": "object"}},
             "error": {"type": ["string", "null"]},
         }, ("target", "targets", "holders", "error")),
         _tool_disk_busy),
    Tool("cpu_top", "Rank processes by CPU use over an interval.",
         _SAMPLING_SCHEMA,
         _output_schema("cpu", "top", _SYSTEM_PROCESSES, _TOP_REQUIRED),
         lambda arguments: _tool_cpu(arguments, "top")),
    Tool("cpu_wakeups", "Rank processes by wakeup rate over an interval.",
         _SAMPLING_SCHEMA,
         _output_schema("cpu", "wakeups", _SYSTEM_PROCESSES, _TOP_REQUIRED),
         lambda arguments: _tool_cpu(arguments, "wakeups")),
    Tool("memory_top", "Rank process footprints and report memory pressure.",
         _LIMIT_ONLY_SCHEMA,
         _output_schema("memory", "top", _SYSTEM_PROCESSES, _TOP_REQUIRED),
         _tool_memory_top),
    Tool("battery_health", "Report battery capacity, cycles, and condition.",
         _EMPTY_SCHEMA, _output_schema("battery", "health", {
             "present": {"type": ["boolean", "null"]},
             "probe_error": {"type": ["string", "null"]},
             "charge_pct": {"type": ["number", "null"]},
             "cycle_count": {"type": ["number", "null"]},
             "health_pct": {"type": ["number", "null"]},
             "condition": {"type": ["string", "null"]},
         }, ("present", "probe_error", "charge_pct", "cycle_count",
             "health_pct", "condition")),
         _tool_battery_health),
    Tool("battery_top", "Rank current per-process energy attribution.",
         _SAMPLING_SCHEMA,
         _output_schema("battery", "top", {
             **_SYSTEM_PROCESSES,
             "pmenergy_source": {"type": ["string", "null"]},
         }, ("pmenergy_source",) + _TOP_REQUIRED),
         _tool_battery_top),
    Tool("smart_status", "Report SMART health and pre-failure warnings.",
         _object_schema({"disk": {
             "type": "string", "minLength": 1, "maxLength": 4096,
         }}),
         _output_schema("smart", "status", {
             "drives": {"type": "array", "items": {"type": "object"}},
             "error": {"type": ["string", "null"]},
         }, ("drives", "error")), _tool_smart_status),
    Tool("checkup", "Run the canonical one-shot full-body examination.",
         _SAMPLING_SCHEMA, _output_schema("checkup", "checkup", {
             "overall": {"type": "string"},
             "findings": {"type": "array", "items": {"type": "object"}},
             "vitals": {"type": "object"},
             "error": {"type": ["string", "null"]},
         }, ("overall", "findings", "vitals", "error")),
         _tool_checkup),
)
TOOL_REGISTRY = {tool.name: tool for tool in TOOLS}


def tool_specs():
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema,
            "outputSchema": tool.output_schema,
        }
        for tool in TOOLS
    ]


def _valid_id(value):
    if isinstance(value, str):
        try:
            return len(value.encode("utf-8")) <= MAX_ID_BYTES
        except UnicodeError:
            return False
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and MIN_INTEGER_ID <= value <= MAX_INTEGER_ID
    )


class Session:
    """One stateful MCP lifecycle and request-ID namespace."""

    def __init__(self):
        self.initialized = False
        self.ready = False
        self.seen_ids = set()
        self.tool_calls = 0

    def handle(self, message):
        if not isinstance(message, dict):
            return _error(None, INVALID_REQUEST, "request must be an object")

        notification = "id" not in message
        message_id = None
        if not notification and _valid_id(message.get("id")):
            message_id = message["id"]
        allowed = {"jsonrpc", "id", "method", "params"}
        if set(message).difference(allowed):
            return _error(
                message_id, INVALID_REQUEST, "invalid request envelope")
        if message.get("jsonrpc") != "2.0":
            return _error(
                message_id, INVALID_REQUEST, "jsonrpc must be '2.0'")
        method = message.get("method")
        if not isinstance(method, str) or not method:
            return _error(
                message_id, INVALID_REQUEST,
                "method must be a nonempty string")
        if "params" in message and not isinstance(
                message["params"], (dict, list)):
            return _error(
                message_id, INVALID_REQUEST, "params must be structured")

        if not notification:
            if message_id is None:
                return _error(None, INVALID_REQUEST, "invalid request id")
            if message_id in self.seen_ids:
                return _error(message_id, INVALID_REQUEST, "request id was reused")
            if len(self.seen_ids) >= MAX_REQUESTS:
                return _error(
                    message_id, INVALID_REQUEST, "request limit exceeded")
            self.seen_ids.add(message_id)

        if notification:
            return self._notification(method, message.get("params"))
        if method == "initialize":
            return self._initialize(message_id, message.get("params"))
        if not self.initialized:
            return _error(
                message_id, INVALID_REQUEST, "initialize must be the first request")
        if not self.ready:
            return _error(
                message_id, INVALID_REQUEST,
                "notifications/initialized is required before operation")
        if method == "ping":
            return self._ping(message_id, message.get("params"))
        if method == "tools/list":
            return self._list_tools(message_id, message.get("params"))
        if method == "tools/call":
            return self._call_tool(message_id, message.get("params"))
        return _error(message_id, METHOD_NOT_FOUND, "method not found")

    def _notification(self, method, params):
        if method == "notifications/initialized":
            try:
                if params is not None:
                    _request_params(params)
            except ParamsError:
                return None
            if self.initialized and not self.ready:
                self.ready = True
        return None

    def _initialize(self, message_id, params):
        if self.initialized:
            return _error(
                message_id, INVALID_REQUEST, "initialize was already completed")
        try:
            params = _request_params(
                params, {"protocolVersion", "capabilities", "clientInfo"},
                {"protocolVersion", "capabilities", "clientInfo"})
            requested = params["protocolVersion"]
            if not isinstance(requested, str) or not requested:
                raise ParamsError("protocolVersion must be a nonempty string")
            _object(params["capabilities"], "capabilities")
            client_info = _object(params["clientInfo"], "clientInfo")
            if (not isinstance(client_info.get("name"), str)
                    or not client_info["name"]
                    or not isinstance(client_info.get("version"), str)
                    or not client_info["version"]):
                raise ParamsError(
                    "clientInfo requires nonempty name and version strings")
        except ParamsError as exc:
            return _error(message_id, INVALID_PARAMS, str(exc))

        selected = (
            requested if requested in SUPPORTED_PROTOCOL_VERSIONS
            else SUPPORTED_PROTOCOL_VERSIONS[-1])
        self.initialized = True
        return _result(message_id, {
            "protocolVersion": selected,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": dict(SERVER_INFO),
        })

    @staticmethod
    def _ping(message_id, params):
        try:
            if params is not None:
                _request_params(params)
        except ParamsError as exc:
            return _error(message_id, INVALID_PARAMS, str(exc))
        return _result(message_id, {})

    @staticmethod
    def _list_tools(message_id, params):
        try:
            if params is not None:
                _request_params(params)
        except ParamsError as exc:
            return _error(message_id, INVALID_PARAMS, str(exc))
        return _result(message_id, {"tools": tool_specs()})

    def _call_tool(self, message_id, params):
        try:
            params = _request_params(
                params, {"name", "arguments"}, {"name"})
            name = params["name"]
            if not isinstance(name, str) or not name:
                raise ParamsError("name must be a nonempty string")
            tool = TOOL_REGISTRY.get(name)
            if tool is None:
                raise ParamsError("unknown tool: %s" % name)
            arguments = params.get("arguments", {})
            if self.tool_calls >= MAX_TOOL_CALLS:
                raise ParamsError("tool call limit exceeded")
            self.tool_calls += 1
            document, exit_code = tool.handler(arguments)
            if (isinstance(exit_code, bool)
                    or exit_code not in (
                        cli.EXIT_OK, cli.EXIT_FINDINGS, cli.EXIT_USAGE,
                        cli.EXIT_PERMISSION, cli.EXIT_ERROR)):
                raise RuntimeError("tool returned an invalid exit code")
            text = _strict_dumps(document)
            if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
                raise RuntimeError("tool result exceeds the size limit")
        except ParamsError as exc:
            return _error(message_id, INVALID_PARAMS, str(exc))
        except (TypeError, ValueError, OverflowError, RuntimeError) as exc:
            return _error(message_id, INTERNAL_ERROR, "tool failure: %s" % exc)
        except Exception:
            return _error(message_id, INTERNAL_ERROR, "unexpected tool failure")

        return _result(message_id, {
            "content": [{"type": "text", "text": text}],
            "structuredContent": document,
            "isError": exit_code in (
                cli.EXIT_USAGE, cli.EXIT_PERMISSION, cli.EXIT_ERROR),
        })


def handle(message, session):
    """Handle one decoded message in ``session``."""
    return session.handle(message)


def _write_response(stdout, response):
    payload = _strict_dumps(response) + "\n"
    stdout.write(payload)
    stdout.flush()


def _decode_line(raw):
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return raw


def _line_size(raw):
    return len(raw) if isinstance(raw, bytes) else len(raw.encode("utf-8"))


def _drain_line(stdin, raw):
    newline = b"\n" if isinstance(raw, bytes) else "\n"
    while raw and not raw.endswith(newline):
        raw = stdin.readline(MAX_INPUT_BYTES + 1)


def serve(stdin=None, stdout=None):
    """Serve one recoverable JSON-RPC session until EOF."""
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout
    session = Session()
    while True:
        raw = stdin.readline(MAX_INPUT_BYTES + 1)
        if raw in (b"", ""):
            return cli.EXIT_OK
        try:
            oversized = _line_size(raw) > MAX_INPUT_BYTES
        except UnicodeError:
            _write_response(stdout, _error(None, PARSE_ERROR, "parse error"))
            continue
        if oversized:
            _drain_line(stdin, raw)
            _write_response(
                stdout, _error(None, INVALID_REQUEST, "request line is too large"))
            continue
        try:
            line = _decode_line(raw)
            message = _strict_loads(line)
        except (UnicodeError, ValueError, TypeError, RecursionError):
            _write_response(stdout, _error(None, PARSE_ERROR, "parse error"))
            continue
        try:
            response = session.handle(message)
        except Exception:
            if isinstance(message, dict) and "id" not in message:
                response = None
            else:
                message_id = (
                    message.get("id")
                    if isinstance(message, dict)
                    and _valid_id(message.get("id")) else None)
                response = _error(
                    message_id, INTERNAL_ERROR, "internal protocol failure")
        if response is not None:
            _write_response(stdout, response)


USAGE = "usage: stethoscope mcp\n"


def main(argv=None):
    argv = list(argv or sys.argv)
    if len(argv) != 1:
        sys.stderr.write(USAGE)
        return cli.EXIT_USAGE
    try:
        return serve()
    except Exception as exc:
        sys.stderr.write("mcp transport failure: %s\n" % exc)
        return cli.EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
