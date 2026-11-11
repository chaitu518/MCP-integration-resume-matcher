"""Scenario group 4 — resource discovery and the Streamable HTTP transport."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from conftest import rpc


def test_resource_discovery(fs):
    server, session, _ = fs
    uris = [r["uri"] for r in rpc(server, session, "resources/list")["result"]["resources"]]
    assert "config://server" in uris and "server://capabilities" in uris
    assert {"resume://priya_sharma.txt", "resume://rahul_verma.pdf", "resume://meera_iyer.docx"} <= set(uris)
    templates = [t["uriTemplate"] for t in rpc(server, session, "resources/templates/list")["result"]["resourceTemplates"]]
    assert templates == ["resume://{filename}", "fs://files/{+path}", "watch://{watch_id}"]


def test_read_resources(fs):
    server, session, _ = fs

    def read(uri: str) -> dict:
        return rpc(server, session, "resources/read", {"uri": uri})["result"]["contents"][0]

    resume = read("resume://rahul_verma.pdf")
    assert resume["mimeType"] == "text/plain" and "Rahul Verma" in resume["text"]
    assert "Acme Capital" in read("fs://files/jobs/JOB-001.json")["text"]
    cfg = json.loads(read("config://server")["text"])
    assert cfg["batch_max_workers"] == 4 and cfg["resolved_root"].endswith("data")
    catalogue = json.loads(read("server://capabilities")["text"])
    assert {"watch_directory", "batch_process"} <= {t["name"] for t in catalogue["tools"]}


def test_resource_list_updates_when_files_change(fs, data_dir):
    server, session, _ = fs
    (data_dir / "resumes" / "new_person.md").write_text("# New Person\nPython")
    uris = [r["uri"] for r in rpc(server, session, "resources/list")["result"]["resources"]]
    assert "resume://new_person.md" in uris


# ----------------------------- HTTP transport ----------------------------- #


@pytest.fixture
def http_server(fs):
    server, _, _ = fs
    httpd = server.make_http_server("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def post(url: str, body: dict | list, headers: dict | None = None) -> tuple[int, dict, dict | list | None]:
    req = urllib.request.Request(url, json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, dict(r.headers), json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, dict(e.headers), json.loads(raw) if raw else None


def test_http_session_flow(http_server):
    url = http_server + "/mcp"
    status, headers, body = post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                       "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
    assert status == 200 and body["result"]["protocolVersion"] == "2025-06-18"
    sid = headers["Mcp-Session-Id"]
    h = {"Mcp-Session-Id": sid, "MCP-Protocol-Version": "2025-06-18"}
    status, _, body = post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, h)
    assert status == 202 and body is None
    status, _, body = post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                 "params": {"name": "list_files", "arguments": {"directory": "resumes"}}}, h)
    assert status == 200 and body["result"]["structuredContent"]["count"] == 6


def test_http_rejects_bad_requests(http_server):
    url = http_server + "/mcp"
    assert post(url, {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0] == 400  # no session
    assert post(url, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"Mcp-Session-Id": "bogus"})[0] == 404
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    assert post(url, init, {"Origin": "https://evil.example"})[0] == 403  # DNS-rebinding protection
    assert post(url, init, {"MCP-Protocol-Version": "1999-01-01"})[0] == 400


def test_http_health_and_session_delete(http_server):
    with urllib.request.urlopen(http_server + "/health", timeout=5) as r:
        assert json.loads(r.read())["status"] == "ok"
    _, headers, _ = post(http_server + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                "params": {"protocolVersion": "2025-06-18"}})
    req = urllib.request.Request(http_server + "/mcp", method="DELETE",
                                 headers={"Mcp-Session-Id": headers["Mcp-Session-Id"]})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
    status, _, _ = post(http_server + "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "ping"},
                        {"Mcp-Session-Id": headers["Mcp-Session-Id"]})
    assert status == 404
