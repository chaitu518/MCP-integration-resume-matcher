"""
mcp_client.py
=============

The agent's side of MCP: a small synchronous **MCP client** (JSON-RPC 2.0 over
stdio) plus a **multi-server manager** that routes tool calls to whichever
server owns the tool.

    with MultiMCPClient.from_config("mcp_servers.json") as mcp:
        mcp.call("list_files", {"directory": "resumes"})          # → filesystem server
        mcp.call("get_job", {"job_id": "JOB-001"})               # → jobs-db server

The server list uses the same ``{"mcpServers": {...}}`` shape as Claude
Desktop, so the same servers can be plugged into any MCP host.

This client talks plain JSON-RPC, so it works with any spec-compliant MCP
server (Python, Node, …), not only the ones in this repo.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("mcp_client")

CLIENT_PROTOCOL_VERSION = "2025-11-25"
ACCEPTED_PROTOCOL_VERSIONS = {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
PROJECT_DIR = Path(__file__).resolve().parent


class MCPClientError(Exception):
    """The server answered with a JSON-RPC error object (protocol-level failure)."""

    def __init__(self, code: int, message: str, data: Any = None, server: str = "?"):
        super().__init__(f"[{server}] JSON-RPC error {code}: {message}")
        self.code, self.message, self.data, self.server = code, message, data, server


class MCPToolError(Exception):
    """A tool ran but reported failure (``isError: true``)."""

    def __init__(self, tool: str, payload: Any, server: str = "?"):
        message = payload.get("message") if isinstance(payload, dict) else str(payload)
        super().__init__(f"[{server}] tool '{tool}' failed: {message}")
        self.tool, self.payload, self.server = tool, payload, server
        self.code = payload.get("error") if isinstance(payload, dict) else None


# --------------------------------------------------------------------------- #
# Single-server client
# --------------------------------------------------------------------------- #


class MCPClient:
    """Synchronous MCP client for a server launched as a subprocess (stdio transport)."""

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float = 60.0,
        trace: Callable[[str, str, dict], None] | None = None,
    ):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.cwd = cwd
        self.timeout = timeout
        self.trace = trace  # trace(server, direction "→"/"←", message) – used by the demo
        self.proc: subprocess.Popen | None = None
        self.server_info: dict = {}
        self.capabilities: dict = {}
        self.instructions: str | None = None
        self.protocol_version: str | None = None
        self.notifications: deque[dict] = deque(maxlen=500)
        self._listeners: list[Callable[[str, dict], None]] = []
        self._progress: dict[Any, Callable[[dict], None]] = {}
        self._ids = itertools.count(1)
        self._pending: dict[int, dict] = {}
        self._cond = threading.Condition()
        self._write_lock = threading.Lock()
        self._stderr_tail: deque[str] = deque(maxlen=50)

    # ---- lifecycle ----
    def connect(self) -> "MCPClient":
        env = {**os.environ, **self.env, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        self.proc = subprocess.Popen(
            [self.command, *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=self.cwd, env=env, bufsize=0,
        )
        threading.Thread(target=self._read_stdout, daemon=True, name=f"{self.name}-stdout").start()
        threading.Thread(target=self._read_stderr, daemon=True, name=f"{self.name}-stderr").start()
        self._initialize()
        return self

    def _initialize(self) -> None:
        result = self.request("initialize", {
            "protocolVersion": CLIENT_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "resume-matching-agent", "version": "4.0.0"},
        })
        version = result.get("protocolVersion")
        if version not in ACCEPTED_PROTOCOL_VERSIONS:
            self.close()
            raise MCPClientError(-32602, f"Server negotiated unsupported protocol version {version}", server=self.name)
        self.protocol_version = version
        self.server_info = result.get("serverInfo", {})
        self.capabilities = result.get("capabilities", {})
        self.instructions = result.get("instructions")
        self.notify("notifications/initialized")
        log.info("connected to %s (%s v%s, protocol %s)", self.name, self.server_info.get("name"),
                 self.server_info.get("version"), version)

    def close(self) -> None:
        if not self.proc:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()  # spec: closing stdin is the stdio shutdown signal
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()
        self.proc = None

    def __enter__(self) -> "MCPClient":
        return self.connect()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # ---- I/O ----
    def _send(self, message: dict) -> None:
        if not self.alive:
            raise MCPClientError(-32000, "Server process is not running. stderr: " + " | ".join(self._stderr_tail),
                                 server=self.name)
        if self.trace:
            self.trace(self.name, "→", message)
        data = (json.dumps(message) + "\n").encode("utf-8")
        with self._write_lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("%s: non-JSON line on stdout: %r", self.name, raw[:200])
                continue
            for m in msg if isinstance(msg, list) else [msg]:
                self._dispatch(m)
        with self._cond:  # wake any waiters: the server is gone
            self._cond.notify_all()

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            self._stderr_tail.append(line)
            log.debug("[%s stderr] %s", self.name, line)

    def _dispatch(self, msg: dict) -> None:
        if self.trace:
            self.trace(self.name, "←", msg)
        if "id" in msg and ("result" in msg or "error" in msg):
            with self._cond:
                self._pending[msg["id"]] = msg
                self._cond.notify_all()
            return
        method = msg.get("method", "")
        params = msg.get("params", {})
        if "id" in msg:  # server→client request; we advertise no client capabilities
            self._send({"jsonrpc": "2.0", "id": msg["id"],
                        "error": {"code": -32601, "message": f"Method not found: {method}"}})
            return
        self.notifications.append({"method": method, "params": params, "received_at": time.time()})
        if method == "notifications/progress":
            cb = self._progress.get(params.get("progressToken"))
            if cb:
                cb(params)
        for listener in self._listeners:
            try:
                listener(method, params)
            except Exception as exc:  # pragma: no cover
                log.warning("notification listener failed: %s", exc)

    def on_notification(self, listener: Callable[[str, dict], None]) -> None:
        self._listeners.append(listener)

    # ---- JSON-RPC primitives ----
    def notify(self, method: str, params: dict | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def request(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        req_id = next(self._ids)
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        deadline = time.monotonic() + (timeout or self.timeout)
        with self._cond:
            while req_id not in self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self.alive:
                    break
                self._cond.wait(min(remaining, 0.5))
            response = self._pending.pop(req_id, None)
        if response is None:
            if self.alive:
                self.notify("notifications/cancelled", {"requestId": req_id, "reason": "client timeout"})
                raise TimeoutError(f"[{self.name}] {method} timed out")
            raise MCPClientError(-32000, "Server exited. stderr: " + " | ".join(self._stderr_tail), server=self.name)
        if "error" in response:
            err = response["error"]
            raise MCPClientError(err.get("code"), err.get("message"), err.get("data"), server=self.name)
        return response["result"]

    def _paged(self, method: str, key: str) -> list[dict]:
        items, cursor = [], None
        while True:
            result = self.request(method, {"cursor": cursor} if cursor else {})
            items.extend(result.get(key, []))
            cursor = result.get("nextCursor")
            if not cursor:
                return items

    # ---- MCP API ----
    def ping(self) -> float:
        t0 = time.perf_counter()
        self.request("ping")
        return (time.perf_counter() - t0) * 1000

    def list_tools(self) -> list[dict]:
        return self._paged("tools/list", "tools")

    def list_resources(self) -> list[dict]:
        return self._paged("resources/list", "resources")

    def list_resource_templates(self) -> list[dict]:
        return self._paged("resources/templates/list", "resourceTemplates")

    def read_resource(self, uri: str) -> str:
        contents = self.request("resources/read", {"uri": uri})["contents"]
        return "\n".join(c.get("text", "") for c in contents)

    def subscribe(self, uri: str) -> None:
        self.request("resources/subscribe", {"uri": uri})

    def set_log_level(self, level: str) -> None:
        self.request("logging/setLevel", {"level": level})

    def call_tool_raw(self, name: str, arguments: dict | None = None, timeout: float | None = None,
                      on_progress: Callable[[dict], None] | None = None) -> dict:
        params: dict[str, Any] = {"name": name, "arguments": arguments or {}}
        token = None
        if on_progress:
            token = f"{self.name}-{name}-{next(self._ids)}"
            params["_meta"] = {"progressToken": token}
            self._progress[token] = on_progress
        try:
            return self.request("tools/call", params, timeout)
        finally:
            if token:
                self._progress.pop(token, None)

    def call_tool(self, name: str, arguments: dict | None = None, timeout: float | None = None,
                  on_progress: Callable[[dict], None] | None = None) -> Any:
        """Call a tool and return its structured result (dict) or text. Raises MCPToolError on isError."""
        result = self.call_tool_raw(name, arguments, timeout, on_progress)
        payload = result.get("structuredContent")
        if payload is None:
            text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
            try:
                payload = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                payload = text
        if result.get("isError"):
            raise MCPToolError(name, payload, server=self.name)
        return payload


# --------------------------------------------------------------------------- #
# Multi-server manager
# --------------------------------------------------------------------------- #


@dataclass
class ServerSpec:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    enabled: bool = True
    required: bool = False
    description: str = ""


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: str, missing: set[str]) -> str:
    builtins = {"PYTHON": sys.executable, "PROJECT_DIR": str(PROJECT_DIR)}

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key in builtins:
            return builtins[key]
        if key not in os.environ:
            missing.add(key)
        return os.environ.get(key, "")

    return _VAR.sub(sub, value)


def load_server_specs(path: str | Path) -> list[ServerSpec]:
    """Load ``{"mcpServers": {name: {...}}}`` and expand ``${PYTHON}``, ``${PROJECT_DIR}``, ``${ENV_VAR}``."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    specs = []
    for name, cfg in raw.get("mcpServers", {}).items():
        missing: set[str] = set()
        spec = ServerSpec(
            name=name,
            command=_expand(cfg["command"], missing),
            args=[_expand(a, missing) for a in cfg.get("args", [])],
            env={k: _expand(v, missing) for k, v in cfg.get("env", {}).items()},
            cwd=_expand(cfg["cwd"], missing) if cfg.get("cwd") else None,
            enabled=cfg.get("enabled", True),
            required=cfg.get("required", False),
            description=cfg.get("description", ""),
        )
        if missing and spec.enabled:
            log.warning("server '%s' disabled: missing environment variables %s", name, sorted(missing))
            spec.enabled = False
            if spec.required:
                raise RuntimeError(f"Required MCP server '{name}' needs env vars {sorted(missing)}")
        specs.append(spec)
    return specs


class MultiMCPClient:
    """Connects to several MCP servers and routes each tool call to the server that owns the tool."""

    def __init__(self, specs: list[ServerSpec], trace: Callable[[str, str, dict], None] | None = None,
                 timeout: float = 120.0):
        self.specs = specs
        self.trace = trace
        self.timeout = timeout
        self.clients: dict[str, MCPClient] = {}
        self.tools: dict[str, dict[str, dict]] = {}  # server -> tool name -> tool definition
        self.skipped: dict[str, str] = {}  # server -> reason

    @classmethod
    def from_config(cls, path: str | Path = PROJECT_DIR / "mcp_servers.json", **kw: Any) -> "MultiMCPClient":
        return cls(load_server_specs(path), **kw)

    def connect(self) -> "MultiMCPClient":
        for spec in self.specs:
            if not spec.enabled:
                self.skipped[spec.name] = "disabled in config"
                continue
            client = MCPClient(spec.name, spec.command, spec.args, spec.env, spec.cwd, self.timeout, self.trace)
            try:
                client.connect()
                self.tools[spec.name] = {t["name"]: t for t in client.list_tools()}
                self.clients[spec.name] = client
            except Exception as exc:
                client.close()
                if spec.required:
                    self.close()
                    raise RuntimeError(f"Required MCP server '{spec.name}' failed to start: {exc}") from exc
                log.warning("optional MCP server '%s' unavailable: %s", spec.name, exc)
                self.skipped[spec.name] = str(exc)
        return self

    def close(self) -> None:
        for client in self.clients.values():
            client.close()
        self.clients.clear()

    def __enter__(self) -> "MultiMCPClient":
        return self.connect()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- routing ----
    def owner_of(self, tool: str) -> str | None:
        owners = [s for s, tools in self.tools.items() if tool in tools]
        return owners[0] if owners else None

    def has_tool(self, tool: str, server: str | None = None) -> bool:
        if server:
            return tool in self.tools.get(server, {})
        return self.owner_of(tool) is not None

    def call(self, tool: str, arguments: dict | None = None, server: str | None = None, **kw: Any) -> Any:
        """Call ``tool`` (optionally qualified as ``server.tool``) on whichever server provides it."""
        if server is None and "." in tool and tool.split(".", 1)[0] in self.clients:
            server, tool = tool.split(".", 1)
        server = server or self.owner_of(tool)
        if server is None or server not in self.clients:
            raise MCPClientError(-32602, f"No connected MCP server provides tool '{tool}'")
        return self.clients[server].call_tool(tool, arguments, **kw)

    def server(self, name: str) -> MCPClient:
        return self.clients[name]

    def discovery_report(self) -> dict:
        report = {}
        for name, client in self.clients.items():
            report[name] = {
                "server": client.server_info,
                "protocol": client.protocol_version,
                "capabilities": sorted(client.capabilities),
                "tools": sorted(self.tools[name]),
                "resources": len(client.list_resources()) if "resources" in client.capabilities else 0,
                "resource_templates": [t["uriTemplate"] for t in client.list_resource_templates()]
                if "resources" in client.capabilities else [],
            }
        return report


# --------------------------------------------------------------------------- #
# Optional: expose MCP tools as LangChain tools (for LLM tool-calling agents)
# --------------------------------------------------------------------------- #


def as_langchain_tools(mcp: MultiMCPClient, include: set[str] | None = None) -> list:
    """Wrap every discovered MCP tool as a LangChain ``StructuredTool``.

    The tool list is *discovered at runtime* — the agent no longer hard-codes
    file-system functions; adding a tool to an MCP server makes it available to
    the LLM without touching agent code.
    """
    from langchain_core.tools import StructuredTool

    tools = []
    for server, defs in mcp.tools.items():
        for name, definition in defs.items():
            if include and name not in include:
                continue

            def _run(_server: str = server, _name: str = name, **kwargs: Any) -> str:
                try:
                    result = mcp.call(_name, kwargs, server=_server)
                except MCPToolError as exc:
                    return json.dumps({"error": exc.payload})
                return result if isinstance(result, str) else json.dumps(result, default=str)[:20000]

            schema = dict(definition.get("inputSchema") or {"type": "object", "properties": {}})
            schema.setdefault("title", name)
            schema.setdefault("description", definition.get("description", ""))
            tools.append(StructuredTool.from_function(
                func=_run, name=name, description=definition.get("description", "")[:1000],
                args_schema=schema, infer_schema=False,
            ))
    return tools
