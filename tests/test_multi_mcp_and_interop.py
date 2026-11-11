"""Scenario group 6 — multi-server client routing, LangChain adapter, and interop with the official MCP SDK."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from conftest import ROOT
from mcp_client import MCPClientError, MCPToolError, MultiMCPClient, as_langchain_tools, load_server_specs


def test_routing_across_servers(servers_config):
    with MultiMCPClient.from_config(servers_config) as mcp:
        assert mcp.owner_of("batch_process") == "filesystem"
        assert mcp.owner_of("get_job") == "jobs_db"
        assert mcp.call("jobs_db.list_jobs")["count"] == 3  # qualified name
        assert mcp.call("list_files", {"directory": "jobs"})["count"] == 3
        with pytest.raises(MCPClientError):
            mcp.call("no_such_tool")
        with pytest.raises(MCPToolError) as exc:
            mcp.call("get_job", {"job_id": "nope"})
        assert exc.value.code == "NOT_FOUND"
        report = mcp.discovery_report()
        assert report["filesystem"]["protocol"] == "2025-11-25"
        assert "watch://{watch_id}" in report["filesystem"]["resource_templates"]
        assert report["jobs_db"]["resources"] == 4  # 3 jobs + schema


def test_progress_and_notifications_reach_client(servers_config):
    with MultiMCPClient.from_config(servers_config) as mcp:
        seen = []
        mcp.call("batch_process", {"directory": "resumes"}, on_progress=seen.append)
        assert len(seen) == 6
        fs = mcp.server("filesystem")
        fs.set_log_level("debug")
        assert fs.ping() < 1000


def test_optional_server_failure_is_tolerated(tmp_path, servers_config):
    cfg = json.loads(servers_config.read_text())
    cfg["mcpServers"]["broken"] = {"command": sys.executable, "args": ["-c", "import sys; sys.exit(3)"]}
    path = tmp_path / "with_broken.json"
    path.write_text(json.dumps(cfg))
    with MultiMCPClient.from_config(path) as mcp:
        assert set(mcp.clients) == {"filesystem", "jobs_db"} and "broken" in mcp.skipped


def test_env_expansion_disables_server_with_missing_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("SOME_API_KEY", raising=False)
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"mcpServers": {"x": {"command": "${PYTHON}", "env": {"K": "${SOME_API_KEY}"}}}}))
    spec = load_server_specs(path)[0]
    assert spec.command == sys.executable and spec.enabled is False


def test_langchain_tool_adapter(servers_config):
    with MultiMCPClient.from_config(servers_config) as mcp:
        tools = {t.name: t for t in as_langchain_tools(mcp)}
        assert {"read_file", "batch_process", "get_job", "watch_directory"} <= set(tools)
        out = json.loads(tools["read_file"].invoke({"path": "resumes/priya_sharma.txt", "max_chars": 12}))
        assert out["content"] == "Priya Sharma"
        err = json.loads(tools["read_file"].invoke({"path": "../x.txt"}))
        assert err["error"]["error"] == "ACCESS_DENIED"


# --------------------- official MCP Python SDK interoperability --------------------- #

mcp_sdk = pytest.importorskip("mcp", reason="official MCP SDK not installed (pip install mcp)")


def test_official_sdk_client_can_use_our_server(data_dir):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def scenario():
        params = StdioServerParameters(command=sys.executable,
                                       args=[str(ROOT / "filesystem_mcp_server.py"), "--root", str(data_dir),
                                             "--log-level", "WARNING"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                resources = {str(r.uri) for r in (await session.list_resources()).resources}
                batch = await session.call_tool("batch_process", {"directory": "resumes", "operation": "word_count"})
                text = (await session.read_resource("resume://meera_iyer.docx")).contents[0].text
                return tools, resources, batch, text

    tools, resources, batch, text = asyncio.run(scenario())
    assert {"watch_directory", "batch_process", "read_file"} <= tools
    assert "resume://rahul_verma.pdf" in resources
    sc = batch.structuredContent if hasattr(batch, "structuredContent") else batch.structured_content
    assert sc["succeeded"] == 5
    assert "Meera Iyer" in text
