"""
mcp_protocol.py
===============

A small, dependency-free implementation of the **Model Context Protocol (MCP)**
server side, built directly on **JSON-RPC 2.0**.

Both MCP servers in this project (``filesystem_mcp_server.py`` and the bonus
``jobs_db_mcp_server.py``) are built on this module, so the protocol rules live
in one place.

What it implements (spec revisions 2024-11-05 → 2025-11-25):

* JSON-RPC 2.0 framing: requests, notifications, responses, batch arrays,
  standard error codes (-32700 / -32600 / -32601 / -32602 / -32603)
* MCP lifecycle: ``initialize`` → ``notifications/initialized`` → operation,
  protocol-version negotiation, capability advertisement, ``ping``
* Tools: ``tools/list`` (paginated) and ``tools/call`` with input validation,
  ``structuredContent`` and ``isError`` results
* Resources: ``resources/list``, ``resources/templates/list``,
  ``resources/read``, ``resources/subscribe`` / ``unsubscribe`` and the
  ``notifications/resources/updated`` / ``list_changed`` notifications
* Utilities: ``logging/setLevel`` + ``notifications/message``, progress
  notifications (``_meta.progressToken``), cancellation
  (``notifications/cancelled``)
* Transports: **stdio** (newline-delimited JSON) and **Streamable HTTP**
  (JSON response mode, ``Mcp-Session-Id`` sessions, Origin validation)

Nothing here is specific to files or resumes — servers register tools and
resources on an :class:`MCPServer` instance.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

JSONRPC_VERSION = "2.0"

#: Protocol revisions this server can negotiate, newest first.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

# JSON-RPC 2.0 standard error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Implementation-defined server errors (JSON-RPC reserves -32000..-32099)
RESOURCE_NOT_FOUND = -32002  # used by the MCP spec for resources/read
SERVER_NOT_INITIALIZED = -32003
REQUEST_CANCELLED = -32800

LOG_LEVELS = ["debug", "info", "notice", "warning", "error", "critical", "alert", "emergency"]

log = logging.getLogger("mcp")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class JSONRPCError(Exception):
    """A protocol-level error. Becomes a JSON-RPC ``error`` object."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict:
        err = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


class ToolError(Exception):
    """A tool *execution* failure.

    Per the MCP spec this is NOT a protocol error: it is returned as a normal
    ``tools/call`` result with ``isError: true`` so the calling model can see
    what went wrong and self-correct.
    """

    def __init__(self, message: str, code: str = "TOOL_ERROR", details: Any = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details


# --------------------------------------------------------------------------- #
# Registry objects
# --------------------------------------------------------------------------- #


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict, "RequestContext"], Any]
    title: str | None = None
    output_schema: dict | None = None
    annotations: dict | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.title:
            d["title"] = self.title
        if self.output_schema:
            d["outputSchema"] = self.output_schema
        if self.annotations:
            d["annotations"] = self.annotations
        return d


@dataclass
class Resource:
    uri: str
    name: str
    reader: Callable[[], str | bytes]
    description: str | None = None
    mime_type: str | None = None
    title: str | None = None
    size: int | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"uri": self.uri, "name": self.name}
        if self.title:
            d["title"] = self.title
        if self.description:
            d["description"] = self.description
        if self.mime_type:
            d["mimeType"] = self.mime_type
        if self.size is not None:
            d["size"] = self.size
        return d


@dataclass
class ResourceTemplate:
    uri_template: str
    name: str
    reader: Callable[[dict], str | bytes]
    description: str | None = None
    mime_type: str | None = None
    title: str | None = None
    _regex: re.Pattern = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Supports RFC 6570 level-1 ``{var}`` (one path segment) and
        # ``{+var}`` / ``{var*}`` (may span ``/``).
        pattern, pos = "", 0
        for m in re.finditer(r"\{(\+?)([A-Za-z_][A-Za-z0-9_]*)(\*?)\}", self.uri_template):
            pattern += re.escape(self.uri_template[pos : m.start()])
            greedy = m.group(1) == "+" or m.group(3) == "*"
            pattern += f"(?P<{m.group(2)}>{'.+' if greedy else '[^/]+'})"
            pos = m.end()
        pattern += re.escape(self.uri_template[pos:])
        self._regex = re.compile("^" + pattern + "$")

    def match(self, uri: str) -> dict | None:
        m = self._regex.match(uri)
        return m.groupdict() if m else None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"uriTemplate": self.uri_template, "name": self.name}
        if self.title:
            d["title"] = self.title
        if self.description:
            d["description"] = self.description
        if self.mime_type:
            d["mimeType"] = self.mime_type
        return d


# --------------------------------------------------------------------------- #
# Sessions and request context
# --------------------------------------------------------------------------- #


class Session:
    """Per-connection protocol state (one for stdio, one per HTTP session)."""

    def __init__(self, sink: Callable[[dict], None] | None = None):
        self.id = uuid.uuid4().hex
        self.initialized = False  # initialize request answered
        self.ready = False  # notifications/initialized received
        self.protocol_version: str | None = None
        self.client_info: dict = {}
        self.client_capabilities: dict = {}
        self.log_level = "info"
        self.subscriptions: set[str] = set()
        self.cancelled: set[Any] = set()
        self.created = time.time()
        self._sink = sink
        self._lock = threading.Lock()

    def send(self, message: dict) -> None:
        if self._sink is None:
            return  # e.g. HTTP JSON-response mode has no server→client stream
        with self._lock:
            self._sink(message)


class RequestContext:
    """Handed to tool handlers so they can report progress, log, and notice cancellation."""

    def __init__(self, server: "MCPServer", session: Session, request_id: Any, meta: dict | None):
        self.server = server
        self.session = session
        self.request_id = request_id
        self.progress_token = (meta or {}).get("progressToken")

    def is_cancelled(self) -> bool:
        return self.request_id in self.session.cancelled

    def report_progress(self, progress: float, total: float | None = None, message: str | None = None) -> None:
        if self.progress_token is None:
            return
        params: dict[str, Any] = {"progressToken": self.progress_token, "progress": progress}
        if total is not None:
            params["total"] = total
        if message:
            params["message"] = message
        self.session.send(_notification("notifications/progress", params))

    def log(self, level: str, data: Any, logger_name: str | None = None) -> None:
        self.server.log_to_session(self.session, level, data, logger_name)


def _notification(method: str, params: dict | None = None) -> dict:
    msg: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def _error_response(req_id: Any, err: JSONRPCError) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "error": err.to_dict()}


def _result_response(req_id: Any, result: dict) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "result": result}


# --------------------------------------------------------------------------- #
# Minimal JSON-Schema validation for tool arguments
# --------------------------------------------------------------------------- #

_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def validate_arguments(schema: dict, value: Any, path: str = "arguments") -> list[str]:
    """Validate ``value`` against the subset of JSON Schema used by our tools.

    Returns a list of human-readable problems (empty list = valid).
    """
    problems: list[str] = []
    expected = schema.get("type")
    if expected:
        types_ = expected if isinstance(expected, list) else [expected]
        if not any(_TYPE_CHECKS[t](value) for t in types_):
            return [f"{path}: expected {' or '.join(types_)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: must be one of {schema['enum']}, got {value!r}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path}: must be <= {schema['maximum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(f"{path}: must have at least {schema['minLength']} characters")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append(f"{path}: must have at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{path}: must have at most {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                problems += validate_arguments(schema["items"], item, f"{path}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                problems.append(f"{path}: missing required property '{req}'")
        for key, item in value.items():
            if key in props:
                problems += validate_arguments(props[key], item, f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                problems.append(f"{path}: unknown property '{key}'")
    return problems


# --------------------------------------------------------------------------- #
# The server
# --------------------------------------------------------------------------- #


class MCPServer:
    """Transport-agnostic MCP server: a registry plus a JSON-RPC dispatcher."""

    def __init__(
        self,
        name: str,
        version: str,
        title: str | None = None,
        instructions: str | None = None,
        page_size: int = 50,
        max_workers: int = 8,
    ):
        self.name = name
        self.version = version
        self.title = title
        self.instructions = instructions
        self.page_size = page_size
        self.tools: dict[str, ToolSpec] = {}
        self.resources: dict[str, Resource] = {}
        self.resource_providers: list[Callable[[], Iterable[Resource]]] = []
        self.templates: list[ResourceTemplate] = []
        self.sessions: dict[str, Session] = {}
        self.shutdown_hooks: list[Callable[[], None]] = []
        self._sessions_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"{name}-worker")
        self._methods: dict[str, Callable[[Session, dict, Any], dict]] = {
            "initialize": self._initialize,
            "ping": lambda s, p, i: {},
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
            "resources/list": self._resources_list,
            "resources/templates/list": self._templates_list,
            "resources/read": self._resources_read,
            "resources/subscribe": self._subscribe,
            "resources/unsubscribe": self._unsubscribe,
            "logging/setLevel": self._set_level,
        }

    # ---------------- registration API ---------------- #

    def tool(
        self,
        name: str,
        description: str,
        input_schema: dict,
        title: str | None = None,
        output_schema: dict | None = None,
        annotations: dict | None = None,
    ) -> Callable:
        """Decorator: register ``fn(args: dict, ctx: RequestContext)`` as an MCP tool."""

        def decorator(fn: Callable[[dict, RequestContext], Any]) -> Callable:
            schema = dict(input_schema)
            schema.setdefault("type", "object")
            self.tools[name] = ToolSpec(name, description, schema, fn, title, output_schema, annotations)
            return fn

        return decorator

    def add_resource(self, resource: Resource) -> None:
        self.resources[resource.uri] = resource

    def add_resource_provider(self, provider: Callable[[], Iterable[Resource]]) -> None:
        """Register a callable that lists resources dynamically (e.g. files on disk)."""
        self.resource_providers.append(provider)

    def add_template(self, template: ResourceTemplate) -> None:
        self.templates.append(template)

    # ---------------- notifications ---------------- #

    def broadcast(self, method: str, params: dict | None = None) -> None:
        msg = _notification(method, params)
        for session in list(self.sessions.values()):
            if session.ready or session.initialized:
                session.send(msg)

    def notify_resources_changed(self) -> None:
        self.broadcast("notifications/resources/list_changed")

    def notify_resource_updated(self, uri: str) -> None:
        msg = _notification("notifications/resources/updated", {"uri": uri})
        for session in list(self.sessions.values()):
            if uri in session.subscriptions:
                session.send(msg)

    def log_to_session(self, session: Session, level: str, data: Any, logger_name: str | None = None) -> None:
        if LOG_LEVELS.index(level) < LOG_LEVELS.index(session.log_level):
            return
        params: dict[str, Any] = {"level": level, "data": data}
        params["logger"] = logger_name or self.name
        session.send(_notification("notifications/message", params))

    def log_all(self, level: str, data: Any, logger_name: str | None = None) -> None:
        for session in list(self.sessions.values()):
            if session.initialized:
                self.log_to_session(session, level, data, logger_name)

    # ---------------- session management ---------------- #

    def new_session(self, sink: Callable[[dict], None] | None = None) -> Session:
        session = Session(sink)
        with self._sessions_lock:
            self.sessions[session.id] = session
        return session

    def close_session(self, session_id: str) -> None:
        with self._sessions_lock:
            self.sessions.pop(session_id, None)

    # ---------------- dispatch ---------------- #

    def handle_raw(self, raw: str | bytes, session: Session) -> str | None:
        """Handle one JSON text (single message or batch). Returns JSON text or None."""
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return json.dumps(_error_response(None, JSONRPCError(PARSE_ERROR, "Parse error", str(exc))))

        if isinstance(payload, list):  # JSON-RPC 2.0 batch
            if not payload:
                return json.dumps(_error_response(None, JSONRPCError(INVALID_REQUEST, "Invalid Request: empty batch")))
            responses = [r for r in (self.handle_message(m, session) for m in payload) if r is not None]
            return json.dumps(responses) if responses else None

        response = self.handle_message(payload, session)
        return json.dumps(response) if response is not None else None

    def handle_message(self, msg: Any, session: Session) -> dict | None:
        """Handle one decoded JSON-RPC message. Returns a response dict or None."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != JSONRPC_VERSION:
            return _error_response(
                msg.get("id") if isinstance(msg, dict) else None,
                JSONRPCError(INVALID_REQUEST, "Invalid Request: expected a JSON-RPC 2.0 object"),
            )

        # A response to a server→client request (we never send any): ignore.
        if "method" not in msg and ("result" in msg or "error" in msg):
            return None

        method = msg.get("method")
        has_id = "id" in msg
        req_id = msg.get("id")

        if not isinstance(method, str):
            return _error_response(req_id, JSONRPCError(INVALID_REQUEST, "Invalid Request: 'method' must be a string"))
        if has_id and (req_id is None or isinstance(req_id, bool) or not isinstance(req_id, (str, int))):
            return _error_response(
                None, JSONRPCError(INVALID_REQUEST, "Invalid Request: 'id' must be a string or integer")
            )

        params = msg.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            err = JSONRPCError(INVALID_PARAMS, "Invalid params: MCP methods take a params object")
            return _error_response(req_id, err) if has_id else None

        # ---- notifications (no response, ever) ----
        if not has_id:
            self._handle_notification(method, params, session)
            return None

        # ---- requests ----
        try:
            if method not in ("initialize", "ping") and not session.initialized:
                raise JSONRPCError(SERVER_NOT_INITIALIZED, "Server not initialized: send 'initialize' first")
            handler = self._methods.get(method)
            if handler is None:
                raise JSONRPCError(METHOD_NOT_FOUND, f"Method not found: {method}")
            result = handler(session, params, req_id)
            if req_id in session.cancelled:
                session.cancelled.discard(req_id)
                return None  # spec: do not respond to a cancelled request
            return _result_response(req_id, result)
        except JSONRPCError as err:
            return _error_response(req_id, err)
        except Exception as exc:  # pragma: no cover - defensive
            log.error("Internal error handling %s: %s\n%s", method, exc, traceback.format_exc())
            return _error_response(req_id, JSONRPCError(INTERNAL_ERROR, "Internal error", str(exc)))

    def _handle_notification(self, method: str, params: dict, session: Session) -> None:
        if method == "notifications/initialized":
            session.ready = True
        elif method == "notifications/cancelled":
            if "requestId" in params:
                session.cancelled.add(params["requestId"])
                log.info("Request %s cancelled by client: %s", params["requestId"], params.get("reason"))
        # Unknown notifications are ignored, as JSON-RPC requires.

    # ---------------- method handlers ---------------- #

    def _initialize(self, session: Session, params: dict, req_id: Any) -> dict:
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            raise JSONRPCError(INVALID_PARAMS, "Invalid params: 'protocolVersion' is required")
        # Version negotiation: echo the client's version if we support it,
        # otherwise offer our latest and let the client decide.
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        session.protocol_version = version
        session.client_info = params.get("clientInfo", {})
        session.client_capabilities = params.get("capabilities", {})
        session.initialized = True
        server_info: dict[str, Any] = {"name": self.name, "version": self.version}
        if self.title:
            server_info["title"] = self.title
        result: dict[str, Any] = {
            "protocolVersion": version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": True, "listChanged": True},
                "logging": {},
            },
            "serverInfo": server_info,
        }
        if self.instructions:
            result["instructions"] = self.instructions
        log.info("Session %s initialized (protocol %s, client %s)", session.id[:8], version, session.client_info)
        return result

    def _paginate(self, items: list[dict], params: dict, key: str) -> dict:
        offset = 0
        cursor = params.get("cursor")
        if cursor is not None:
            try:
                offset = int(json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())["offset"])
                if offset < 0:
                    raise ValueError
            except Exception:
                raise JSONRPCError(INVALID_PARAMS, "Invalid params: invalid cursor")
        page = items[offset : offset + self.page_size]
        result: dict[str, Any] = {key: page}
        if offset + self.page_size < len(items):
            nxt = json.dumps({"offset": offset + self.page_size}).encode()
            result["nextCursor"] = base64.urlsafe_b64encode(nxt).decode()
        return result

    def _tools_list(self, session: Session, params: dict, req_id: Any) -> dict:
        return self._paginate([t.to_dict() for t in self.tools.values()], params, "tools")

    def _tools_call(self, session: Session, params: dict, req_id: Any) -> dict:
        name = params.get("name")
        if not isinstance(name, str):
            raise JSONRPCError(INVALID_PARAMS, "Invalid params: 'name' is required")
        tool = self.tools.get(name)
        if tool is None:
            raise JSONRPCError(INVALID_PARAMS, f"Unknown tool: {name}", {"available": sorted(self.tools)})
        args = params.get("arguments") or {}
        problems = validate_arguments(tool.input_schema, args)
        if problems:
            # Input validation errors are tool-execution errors (isError) so the model can fix its call.
            return _tool_error_result(ToolError("Invalid arguments: " + "; ".join(problems), "INVALID_ARGUMENTS"))
        ctx = RequestContext(self, session, req_id, params.get("_meta"))
        started = time.perf_counter()
        try:
            value = tool.handler(args, ctx)
        except ToolError as exc:
            return _tool_error_result(exc)
        except Exception as exc:
            log.error("Tool %s crashed: %s\n%s", name, exc, traceback.format_exc())
            return _tool_error_result(ToolError(f"{type(exc).__name__}: {exc}", "UNEXPECTED_ERROR"))
        log.info("tools/call %s ok in %.1f ms", name, (time.perf_counter() - started) * 1000)
        return _tool_success_result(value)

    def _all_resources(self) -> list[Resource]:
        out = list(self.resources.values())
        for provider in self.resource_providers:
            try:
                out.extend(provider())
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("Resource provider failed: %s", exc)
        return out

    def _resources_list(self, session: Session, params: dict, req_id: Any) -> dict:
        return self._paginate([r.to_dict() for r in self._all_resources()], params, "resources")

    def _templates_list(self, session: Session, params: dict, req_id: Any) -> dict:
        return self._paginate([t.to_dict() for t in self.templates], params, "resourceTemplates")

    def _resources_read(self, session: Session, params: dict, req_id: Any) -> dict:
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise JSONRPCError(INVALID_PARAMS, "Invalid params: 'uri' is required")
        mime: str | None = None
        reader: Callable[[], str | bytes] | None = None
        for res in self._all_resources():
            if res.uri == uri:
                reader, mime = res.reader, res.mime_type
                break
        if reader is None:
            for tmpl in self.templates:
                vars_ = tmpl.match(uri)
                if vars_ is not None:
                    reader, mime = (lambda t=tmpl, v=vars_: t.reader(v)), tmpl.mime_type
                    break
        if reader is None:
            raise JSONRPCError(RESOURCE_NOT_FOUND, "Resource not found", {"uri": uri})
        try:
            data = reader()
        except FileNotFoundError:
            raise JSONRPCError(RESOURCE_NOT_FOUND, "Resource not found", {"uri": uri})
        except ToolError as exc:
            code = RESOURCE_NOT_FOUND if exc.code == "NOT_FOUND" else INVALID_PARAMS
            raise JSONRPCError(code, "Resource not found" if code == RESOURCE_NOT_FOUND
                               else f"Cannot read resource: {exc.message}", {"uri": uri, "reason": exc.message})
        except (PermissionError, ValueError) as exc:
            raise JSONRPCError(INVALID_PARAMS, f"Cannot read resource: {exc}", {"uri": uri})
        if isinstance(data, bytes):
            content = {"uri": uri, "mimeType": mime or "application/octet-stream", "blob": base64.b64encode(data).decode()}
        else:
            content = {"uri": uri, "mimeType": mime or "text/plain", "text": data}
        return {"contents": [content]}

    def _subscribe(self, session: Session, params: dict, req_id: Any) -> dict:
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise JSONRPCError(INVALID_PARAMS, "Invalid params: 'uri' is required")
        session.subscriptions.add(uri)
        return {}

    def _unsubscribe(self, session: Session, params: dict, req_id: Any) -> dict:
        session.subscriptions.discard(params.get("uri"))
        return {}

    def _set_level(self, session: Session, params: dict, req_id: Any) -> dict:
        level = params.get("level")
        if level not in LOG_LEVELS:
            raise JSONRPCError(INVALID_PARAMS, f"Invalid params: level must be one of {LOG_LEVELS}")
        session.log_level = level
        return {}

    # ---------------- transports ---------------- #

    def run_stdio(self) -> None:
        """Serve MCP over stdio: one JSON-RPC message per line on stdin/stdout.

        Requests are executed on a thread pool so a long-running tool
        (e.g. ``watch_directory``) never blocks ``ping`` or cancellation.
        """
        out = sys.stdout.buffer
        out_lock = threading.Lock()

        def write(message: dict | list) -> None:
            data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
            with out_lock:
                out.write(data)
                out.flush()

        session = self.new_session(sink=write)
        log.info("%s v%s listening on stdio", self.name, self.version)

        def process(line: bytes) -> None:
            response = self.handle_raw(line, session)
            if response is not None:
                with out_lock:
                    out.write((response + "\n").encode("utf-8"))
                    out.flush()

        for line in sys.stdin.buffer:
            if not line.strip():
                continue
            # Lifecycle and cancellation messages are handled inline so they stay ordered.
            inline = (
                b'"initialize"' in line
                or b"notifications/" in line
                or not session.initialized
            )
            if inline:
                process(line)
            else:
                self._executor.submit(process, line)
        log.info("stdin closed, finishing in-flight requests and shutting down")
        self.shutdown()

    def shutdown(self) -> None:
        """Graceful shutdown: run hooks (e.g. stop watchers), then drain in-flight requests."""
        for hook in self.shutdown_hooks:
            try:
                hook()
            except Exception as exc:  # pragma: no cover
                log.warning("shutdown hook failed: %s", exc)
        self._executor.shutdown(wait=True)

    def run_http(self, host: str = "127.0.0.1", port: int = 8765, path: str = "/mcp",
                 allowed_origins: list[str] | None = None) -> ThreadingHTTPServer:
        """Serve MCP over Streamable HTTP (JSON-response mode). Blocks."""
        httpd = self.make_http_server(host, port, path, allowed_origins)
        log.info("%s v%s listening on http://%s:%s%s", self.name, self.version, host, httpd.server_port, path)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
        return httpd

    def make_http_server(self, host: str = "127.0.0.1", port: int = 8765, path: str = "/mcp",
                         allowed_origins: list[str] | None = None) -> ThreadingHTTPServer:
        server = self
        origins = set(allowed_origins or ["http://localhost", "http://127.0.0.1"])

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # route to logging, never stdout
                log.debug("http: " + fmt, *args)

            def _send(self, status: int, body: Any = None, headers: dict | None = None) -> None:
                data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
                self.send_response(status)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def _origin_ok(self) -> bool:
                origin = self.headers.get("Origin")
                if origin is None:
                    return True
                return any(origin == o or origin.startswith(o + ":") for o in origins)

            def do_GET(self) -> None:
                if self.path == "/health":
                    self._send(200, {"status": "ok", "server": server.name, "version": server.version,
                                     "sessions": len(server.sessions), "tools": len(server.tools)})
                elif self.path == path:
                    # We do not offer a server-initiated SSE stream.
                    self._send(405, {"error": "SSE stream not supported; use POST"}, {"Allow": "POST, DELETE"})
                else:
                    self._send(404, {"error": "not found"})

            def do_DELETE(self) -> None:
                sid = self.headers.get("Mcp-Session-Id")
                if self.path != path or not sid or sid not in server.sessions:
                    self._send(404, {"error": "unknown session"})
                    return
                server.close_session(sid)
                self._send(200, {"status": "session terminated"})

            def do_POST(self) -> None:
                if self.path != path:
                    self._send(404, {"error": "not found"})
                    return
                if not self._origin_ok():
                    self._send(403, {"error": "origin not allowed"})
                    return
                pv = self.headers.get("MCP-Protocol-Version")
                if pv and pv not in SUPPORTED_PROTOCOL_VERSIONS:
                    self._send(400, {"error": f"unsupported MCP-Protocol-Version {pv}"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw)
                except Exception:
                    self._send(400, _error_response(None, JSONRPCError(PARSE_ERROR, "Parse error")))
                    return
                messages = payload if isinstance(payload, list) else [payload]
                is_init = any(isinstance(m, dict) and m.get("method") == "initialize" for m in messages)
                sid = self.headers.get("Mcp-Session-Id")
                headers: dict[str, str] = {}
                if is_init:
                    session = server.new_session(sink=None)
                    headers["Mcp-Session-Id"] = session.id
                else:
                    if not sid:
                        self._send(400, {"error": "missing Mcp-Session-Id header"})
                        return
                    session = server.sessions.get(sid)
                    if session is None:
                        self._send(404, {"error": "unknown or expired session"})
                        return
                response = server.handle_raw(raw, session)
                if response is None:
                    self._send(202, None, headers)  # only notifications/responses were sent
                else:
                    self._send(200, response.encode(), headers)

        return ThreadingHTTPServer((host, port), Handler)


# --------------------------------------------------------------------------- #
# Tool result helpers
# --------------------------------------------------------------------------- #


def _tool_success_result(value: Any) -> dict:
    if isinstance(value, str):
        return {"content": [{"type": "text", "text": value}], "isError": False}
    text = json.dumps(value, indent=2, default=str, ensure_ascii=False)
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": False}
    if isinstance(value, dict):
        result["structuredContent"] = json.loads(json.dumps(value, default=str))
    return result


def _tool_error_result(exc: ToolError) -> dict:
    payload = {"error": exc.code, "message": exc.message}
    if exc.details is not None:
        payload["details"] = exc.details
    return {
        "content": [{"type": "text", "text": json.dumps(payload, default=str)}],
        "structuredContent": json.loads(json.dumps(payload, default=str)),
        "isError": True,
    }


def configure_logging(level: str = "INFO") -> None:
    """Log to **stderr** only — stdout is reserved for JSON-RPC on the stdio transport."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        stream=sys.stderr,
    )
