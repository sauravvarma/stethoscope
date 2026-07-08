#!/usr/bin/env python3
"""
stethoscope mcp — expose the scopes as Model Context Protocol tools.

The whole point of the --json contract was to make each probe an agent
primitive; this is where that pays off. `stethoscope mcp` speaks MCP over stdio
(newline-delimited JSON-RPC 2.0) so an agent can list and call the probes as
tools, get structured vitals back, and reason across scopes — the original
vision (#26).

Tools map 1:1 onto the scopes' data layers (never their rendered text):

    disk_top · disk_holds · disk_busy · cpu_top · cpu_wakeups · memory_top
    battery_health · battery_top · smart_status · checkup

The MCP protocol is implemented with the standard library only (json + stdio) —
no SDK — so the zero-dependency rule holds. Run it as the command an MCP client
launches:

    stethoscope mcp        # (usually configured in the client, not run by hand)

Reading other users' processes / holders needs root, same as the CLI.
"""

import json
import sys
import time

try:
    from scopes import core, output, disk, cpu, memory, battery, smart, checkup
except ImportError:   # invoked with scopes/ directly on sys.path
    import core
    import output
    import disk
    import cpu
    import memory
    import battery
    import smart
    import checkup

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "stethoscope", "version": "0.1.0"}


# ---------------------------------------------------------------------------
# tool implementations — each returns one JSON document (same shape as --json)
# ---------------------------------------------------------------------------

def _tool_disk_top(interval=0.5, limit=20):
    prev = disk.snapshot_diskio()
    t = time.time()
    time.sleep(interval)
    cur = disk.snapshot_diskio()
    rows, dr, dw = disk.rank_io(prev, cur, time.time() - t)
    return disk._top_document(rows, dr, dw, limit)


def _tool_disk_holds(pid):
    items = disk.open_files(int(pid))
    io = disk.proc_diskio(int(pid))
    return output.document("disk", "holds", pid=int(pid), name=disk.proc_name(int(pid)),
                           cumulative=({"read": io[0], "write": io[1]} if io else None),
                           holds=[{"reason": r, "type": t, "path": n} for r, t, n in items])


def _tool_disk_busy(target):
    targets = disk.resolve_volume(target)
    procs = disk.collect_holders(targets)
    return output.document(
        "disk", "busy", target=target,
        targets=[{"device": d, "mount": m} for d, m in targets],
        holders=[disk._busy_holder(pid, procs[pid])
                 for pid in sorted(procs, key=lambda p: -len(procs[p]["holds"]))])


def _tool_cpu(command="top", interval=0.5, limit=20):
    pm, pv = cpu.snapshot()
    time.sleep(interval)
    cm, cv = cpu.snapshot()
    rows, sys_cpu = cpu.rank_cpu(pv, cv, pm, cm, interval)
    if command == "wakeups":
        rows.sort(key=lambda r: r[1], reverse=True)
    return cpu._document(rows, sys_cpu, command, limit)


def _tool_memory_top(limit=20):
    rows = memory.rank_mem(core.snapshot_rusage())
    return memory._top_document(rows, memory.system_memory(), limit)


def _tool_battery_health():
    return output.document("battery", "health", **battery.battery_health())


def _tool_battery_top(interval=0.5, limit=20):
    pm, pv = battery.snapshot()
    time.sleep(interval)
    cm, cv = battery.snapshot()
    rows = battery.rank_energy(pv, cv, pm, cm, interval)
    return battery._top_document(rows, limit)


def _tool_smart():
    drives = [smart.drive_health(d, i) for d, i in smart.list_physical_drives()]
    return output.document("smart", "status", drives=drives)


def _tool_checkup():
    return output.document("checkup", "checkup", **checkup.run_checkup())


# name -> (description, inputSchema properties, handler)
_INT = {"type": "integer"}
TOOLS = {
    "disk_top": ("Rank processes by current disk I/O (bytes/sec).",
                 {"limit": _INT}, lambda a: _tool_disk_top(limit=a.get("limit", 20))),
    "disk_holds": ("List the on-disk files a process holds open.",
                   {"pid": _INT}, lambda a: _tool_disk_holds(a["pid"])),
    "disk_busy": ("Which processes pin a volume/device open (why it won't eject).",
                  {"target": {"type": "string"}}, lambda a: _tool_disk_busy(a["target"])),
    "cpu_top": ("Rank processes by CPU%.",
                {"limit": _INT}, lambda a: _tool_cpu("top", limit=a.get("limit", 20))),
    "cpu_wakeups": ("Rank processes by idle+interrupt wakeups/sec.",
                    {"limit": _INT}, lambda a: _tool_cpu("wakeups", limit=a.get("limit", 20))),
    "memory_top": ("Rank processes by memory footprint; includes system pressure.",
                   {"limit": _INT}, lambda a: _tool_memory_top(a.get("limit", 20))),
    "battery_health": ("Battery charge, cycle count, health %, and condition.",
                       {}, lambda a: _tool_battery_health()),
    "battery_top": ("Rank processes by energy-impact score (CPU + wakeups).",
                    {"limit": _INT}, lambda a: _tool_battery_top(limit=a.get("limit", 20))),
    "smart_status": ("SMART health, wear and pre-failure warnings for each drive.",
                     {}, lambda a: _tool_smart()),
    "checkup": ("One-shot full-body exam across every scope, with a verdict.",
                {}, lambda a: _tool_checkup()),
}


def _tool_specs():
    return [{"name": name,
             "description": desc,
             "inputSchema": {"type": "object", "properties": props,
                             "required": [k for k in props]
                             if name in ("disk_holds", "disk_busy") else []}}
            for name, (desc, props, _) in TOOLS.items()]


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 / MCP dispatch
# ---------------------------------------------------------------------------

def _result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle(msg):
    """Handle one JSON-RPC message; return a response dict, or None for notifications."""
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        return _result(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO})
    if method == "ping":
        return _result(msg_id, {})
    if method is not None and method.startswith("notifications/"):
        return None    # notifications get no response
    if method == "tools/list":
        return _result(msg_id, {"tools": _tool_specs()})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        entry = TOOLS.get(name)
        if entry is None:
            return _error(msg_id, -32602, "unknown tool: %s" % name)
        try:
            doc = entry[2](args)
        except Exception as e:   # surface tool errors as an MCP tool error, not a crash
            return _result(msg_id, {
                "content": [{"type": "text", "text": "error: %s" % e}],
                "isError": True})
        return _result(msg_id, {
            "content": [{"type": "text", "text": json.dumps(doc, default=str)}],
            "structuredContent": doc,
            "isError": False})

    if msg_id is None:
        return None        # unknown notification
    return _error(msg_id, -32601, "method not found: %s" % method)


def serve(stdin=None, stdout=None):
    """Run the stdio MCP loop: one JSON-RPC message per line, in and out."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        resp = handle(msg)
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()


def main(argv=None):
    serve()
    return output.EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv))
