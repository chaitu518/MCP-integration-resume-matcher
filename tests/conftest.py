"""Shared fixtures: every test runs against a fresh copy of the sample data in a temp folder."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import generate_sample_data  # noqa: E402
from filesystem_mcp_server import ServerConfig, build_server  # noqa: E402
from mcp_protocol import MCPServer, Session  # noqa: E402


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    out = tmp_path / "data"
    generate_sample_data.generate(out)
    return out


@pytest.fixture
def make_fs_server(data_dir: Path):
    """Build an in-process filesystem server (no subprocess) with optional config overrides."""
    servers: list[MCPServer] = []

    def factory(**overrides) -> tuple[MCPServer, Session, list[dict]]:
        cfg = ServerConfig.load(config_path=None, overrides={"root_dir": str(data_dir), "watch_poll_interval": 0.1,
                                                              **overrides})
        server = build_server(cfg)
        notes: list[dict] = []
        session = server.new_session(sink=notes.append)
        rpc(server, session, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "pytest", "version": "0"}})
        server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
        servers.append(server)
        return server, session, notes

    yield factory
    for s in servers:
        s.shutdown()


@pytest.fixture
def fs(make_fs_server):
    return make_fs_server()


_ids = iter(range(1, 10**9))


def rpc(server: MCPServer, session: Session, method: str, params: dict | None = None) -> dict:
    """Send one request in-process and return the full JSON-RPC response."""
    msg = {"jsonrpc": "2.0", "id": next(_ids), "method": method}
    if params is not None:
        msg["params"] = params
    return server.handle_message(msg, session)


def call(server: MCPServer, session: Session, tool: str, args: dict | None = None, meta: dict | None = None) -> dict:
    params = {"name": tool, "arguments": args or {}}
    if meta:
        params["_meta"] = meta
    return rpc(server, session, "tools/call", params)["result"]


@pytest.fixture
def servers_config(tmp_path: Path, data_dir: Path) -> Path:
    """mcp_servers.json pointing both servers at the temp data folder."""
    cfg = {
        "mcpServers": {
            "filesystem": {
                "command": sys.executable,
                "args": [str(ROOT / "filesystem_mcp_server.py"), "--root", str(data_dir), "--log-level", "WARNING"],
                "env": {"FS_MCP_WATCH_POLL_INTERVAL": "0.2"},
                "required": True,
            },
            "jobs_db": {
                "command": sys.executable,
                "args": [str(ROOT / "jobs_db_mcp_server.py"), "--db", str(tmp_path / "jobs.db"),
                         "--seed-dir", str(data_dir / "jobs"), "--log-level", "WARNING"],
                "required": False,
            },
        }
    }
    path = tmp_path / "mcp_servers.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


@pytest.fixture
def fs_only_config(tmp_path: Path, servers_config: Path) -> Path:
    cfg = json.loads(servers_config.read_text())
    del cfg["mcpServers"]["jobs_db"]
    path = tmp_path / "fs_only.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path
