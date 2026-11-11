"""Scenario group 1 — JSON-RPC 2.0 + MCP lifecycle compliance."""

from __future__ import annotations

import json
import subprocess
import sys

from conftest import ROOT, rpc
from mcp_protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    LATEST_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    RESOURCE_NOT_FOUND,
    SERVER_NOT_INITIALIZED,
)


def test_initialize_negotiates_requested_version(fs):
    server, _, _ = fs
    session = server.new_session()
    res = rpc(server, session, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                              "clientInfo": {"name": "x", "version": "1"}})
    assert res["jsonrpc"] == "2.0"
    assert res["result"]["protocolVersion"] == "2024-11-05"
    caps = res["result"]["capabilities"]
    assert {"tools", "resources", "logging"} <= set(caps)
    assert caps["resources"] == {"subscribe": True, "listChanged": True}
    assert res["result"]["serverInfo"]["name"] == "filesystem-mcp-server"


def test_initialize_unknown_version_gets_server_latest(fs):
    server, _, _ = fs
    res = rpc(server, server.new_session(), "initialize", {"protocolVersion": "1999-01-01", "capabilities": {}})
    assert res["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION


def test_requests_before_initialize_are_rejected_but_ping_allowed(fs):
    server, _, _ = fs
    session = server.new_session()
    assert rpc(server, session, "tools/list")["error"]["code"] == SERVER_NOT_INITIALIZED
    assert rpc(server, session, "ping")["result"] == {}


def test_parse_error(fs):
    server, session, _ = fs
    resp = json.loads(server.handle_raw("{not json", session))
    assert resp["error"]["code"] == PARSE_ERROR and resp["id"] is None


def test_invalid_requests(fs):
    server, session, _ = fs
    assert server.handle_message({"id": 1, "method": "ping"}, session)["error"]["code"] == INVALID_REQUEST
    assert server.handle_message({"jsonrpc": "2.0", "id": 1, "method": 5}, session)["error"]["code"] == INVALID_REQUEST
    bad_id = server.handle_message({"jsonrpc": "2.0", "id": True, "method": "ping"}, session)
    assert bad_id["error"]["code"] == INVALID_REQUEST
    arr = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": [1]}, session)
    assert arr["error"]["code"] == INVALID_PARAMS


def test_method_not_found(fs):
    server, session, _ = fs
    assert rpc(server, session, "prompts/list")["error"]["code"] == METHOD_NOT_FOUND


def test_notifications_get_no_response(fs):
    server, session, _ = fs
    assert server.handle_message({"jsonrpc": "2.0", "method": "notifications/whatever"}, session) is None


def test_batch_requests(fs):
    server, session, _ = fs
    raw = json.dumps([
        {"jsonrpc": "2.0", "id": "a", "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": "b", "method": "nope"},
    ])
    responses = json.loads(server.handle_raw(raw, session))
    assert [r["id"] for r in responses] == ["a", "b"]
    assert responses[1]["error"]["code"] == METHOD_NOT_FOUND
    empty = json.loads(server.handle_raw("[]", session))
    assert empty["error"]["code"] == INVALID_REQUEST


def test_tools_list_pagination_and_bad_cursor(make_fs_server):
    server, session, _ = make_fs_server(page_size=4)
    names, cursor, pages = [], None, 0
    while True:
        res = rpc(server, session, "tools/list", {"cursor": cursor} if cursor else {})["result"]
        names += [t["name"] for t in res["tools"]]
        pages += 1
        cursor = res.get("nextCursor")
        if not cursor:
            break
    assert pages == 3 and len(names) == len(set(names)) == 9
    assert rpc(server, session, "tools/list", {"cursor": "garbage"})["error"]["code"] == INVALID_PARAMS


def test_tool_definitions_are_well_formed(fs):
    server, session, _ = fs
    for tool in rpc(server, session, "tools/list")["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"] and tool["annotations"]["readOnlyHint"] in (True, False)


def test_unknown_tool_is_protocol_error_but_bad_args_are_tool_errors(fs):
    server, session, _ = fs
    unknown = rpc(server, session, "tools/call", {"name": "rm_rf", "arguments": {}})
    assert unknown["error"]["code"] == INVALID_PARAMS
    bad_args = rpc(server, session, "tools/call", {"name": "read_file", "arguments": {"pathx": 1}})["result"]
    assert bad_args["isError"] is True
    assert bad_args["structuredContent"]["error"] == "INVALID_ARGUMENTS"
    assert "missing required property 'path'" in bad_args["structuredContent"]["message"]


def test_resource_not_found_code(fs):
    server, session, _ = fs
    err = rpc(server, session, "resources/read", {"uri": "resume://ghost.pdf"})["error"]
    assert err["code"] == RESOURCE_NOT_FOUND and err["data"]["uri"] == "resume://ghost.pdf"
    assert rpc(server, session, "resources/read", {"uri": "nope://x"})["error"]["code"] == RESOURCE_NOT_FOUND


def test_logging_set_level(fs):
    server, session, _ = fs
    assert rpc(server, session, "logging/setLevel", {"level": "debug"})["result"] == {}
    assert rpc(server, session, "logging/setLevel", {"level": "loud"})["error"]["code"] == INVALID_PARAMS


def test_cancelled_request_gets_no_response(fs):
    server, session, _ = fs
    session.cancelled.add(99)
    msg = {"jsonrpc": "2.0", "id": 99, "method": "tools/call", "params": {"name": "list_watches", "arguments": {}}}
    assert server.handle_message(msg, session) is None


def test_stdio_transport_end_to_end(data_dir):
    """Real subprocess: newline-delimited JSON on stdout only, logs on stderr, clean exit on stdin EOF."""
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "batch_process", "arguments": {"directory": "resumes", "operation": "word_count"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
    ]
    stdin = "\n".join(json.dumps(m) for m in lines) + "\n"
    proc = subprocess.run([sys.executable, str(ROOT / "filesystem_mcp_server.py"), "--root", str(data_dir)],
                          input=stdin, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    responses = {r["id"]: r for r in map(json.loads, proc.stdout.strip().splitlines())}
    assert set(responses) == {1, 2, 3}  # the notification produced nothing
    batch = responses[2]["result"]["structuredContent"]
    assert batch["total"] == 6 and batch["failed"] == 1
    assert "listening on stdio" in proc.stderr  # logs go to stderr, never stdout
