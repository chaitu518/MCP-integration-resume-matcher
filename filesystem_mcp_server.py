#!/usr/bin/env python3
"""
filesystem_mcp_server.py
========================

Milestone 4 — the Milestone 1 file-system tools, re-exposed as a
**Model Context Protocol** server (JSON-RPC 2.0 over stdio or HTTP).

Tools (``tools/list``)
----------------------
Milestone 1 tools, now MCP tools:
    list_files, read_file, write_file, search_files, get_file_metadata
New MCP-specific capabilities:
    watch_directory   – monitor a folder for new / changed / deleted resumes
    stop_watch, list_watches
    batch_process     – process many files concurrently with per-file error isolation

Resources (``resources/list`` + ``resources/templates/list``)
-------------------------------------------------------------
    resume://{filename}     text of every resume in the resume folder (listed)
    fs://files/{+path}      any readable file under the sandbox root (template)
    watch://{watch_id}      live state of a directory watch (template, subscribable)
    config://server         the effective server configuration
    server://capabilities   machine-readable catalogue of tools + resources

Configuration (lowest → highest precedence)
-------------------------------------------
    built-in defaults  <  JSON config file (--config / FS_MCP_CONFIG)
                       <  environment variables (FS_MCP_*)  <  CLI flags

Run
---
    python filesystem_mcp_server.py                       # stdio (for agents / Claude Desktop)
    python filesystem_mcp_server.py --transport http      # http://127.0.0.1:8765/mcp
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import logging
import mimetypes
import os
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp_protocol import (
    MCPServer,
    RequestContext,
    Resource,
    ResourceTemplate,
    ToolError,
    configure_logging,
)

SERVER_NAME = "filesystem-mcp-server"
SERVER_VERSION = "1.0.0"
PROJECT_DIR = Path(__file__).resolve().parent

log = logging.getLogger("filesystem_mcp")

# --------------------------------------------------------------------------- #
# Configuration management
# --------------------------------------------------------------------------- #


@dataclass
class ServerConfig:
    root_dir: str = "data"  # sandbox: every path must resolve inside this folder
    resume_dir: str = "resumes"  # relative to root_dir
    output_dir: str = "output"  # relative to root_dir
    allowed_extensions: list[str] = field(
        default_factory=lambda: [".txt", ".md", ".pdf", ".docx", ".json", ".csv"]
    )
    resume_extensions: list[str] = field(default_factory=lambda: [".pdf", ".docx", ".txt", ".md"])
    writable_extensions: list[str] = field(default_factory=lambda: [".txt", ".md", ".json", ".csv"])
    max_file_size_mb: float = 10.0
    allow_write: bool = True
    batch_max_workers: int = 4
    batch_max_files: int = 200
    text_cache_size: int = 256
    watch_poll_interval: float = 1.0
    watch_max_timeout: float = 300.0
    max_watches: int = 10
    page_size: int = 50
    log_level: str = "INFO"
    transport: str = "stdio"
    http_host: str = "127.0.0.1"
    http_port: int = 8765

    # ------------------------------------------------------------------ #
    ENV_PREFIX = "FS_MCP_"

    @classmethod
    def load(cls, config_path: str | None = None, overrides: dict | None = None) -> "ServerConfig":
        cfg = cls()
        path = config_path or os.environ.get("FS_MCP_CONFIG")
        if path is None and (PROJECT_DIR / "config" / "filesystem_server.json").exists():
            path = str(PROJECT_DIR / "config" / "filesystem_server.json")
        if path:
            with open(path, encoding="utf-8") as fh:
                cfg._apply(json.load(fh), source=f"file {path}")
        env = {
            k[len(cls.ENV_PREFIX) :].lower(): v
            for k, v in os.environ.items()
            if k.startswith(cls.ENV_PREFIX) and k != "FS_MCP_CONFIG"
        }
        cfg._apply(env, source="environment", from_strings=True)
        cfg._apply({k: v for k, v in (overrides or {}).items() if v is not None}, source="CLI")
        cfg.validate()
        return cfg

    def _apply(self, values: dict, source: str, from_strings: bool = False) -> None:
        known = {f for f in self.__dataclass_fields__}
        for key, value in values.items():
            if key not in known:
                if not from_strings:
                    raise ValueError(f"Unknown config key '{key}' in {source}")
                continue
            current = getattr(self, key)
            if from_strings:
                value = self._coerce(value, current)
            setattr(self, key, value)

    @staticmethod
    def _coerce(raw: str, current: Any) -> Any:
        if isinstance(current, bool):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        if isinstance(current, int):
            return int(raw)
        if isinstance(current, float):
            return float(raw)
        if isinstance(current, list):
            return [p.strip() for p in raw.split(",") if p.strip()]
        return raw

    def validate(self) -> None:
        if self.transport not in ("stdio", "http"):
            raise ValueError("transport must be 'stdio' or 'http'")
        if self.batch_max_workers < 1 or self.batch_max_files < 1:
            raise ValueError("batch limits must be >= 1")
        if self.max_file_size_mb <= 0:
            raise ValueError("max_file_size_mb must be > 0")
        if self.watch_poll_interval <= 0:
            raise ValueError("watch_poll_interval must be > 0")
        for attr in ("allowed_extensions", "resume_extensions", "writable_extensions"):
            setattr(self, attr, [e.lower() if e.startswith(".") else "." + e.lower() for e in getattr(self, attr)])

    @property
    def root(self) -> Path:
        p = Path(self.root_dir)
        return (p if p.is_absolute() else PROJECT_DIR / p).resolve()

    @property
    def resume_path(self) -> Path:
        return (self.root / self.resume_dir).resolve()

    @property
    def output_path(self) -> Path:
        return (self.root / self.output_dir).resolve()

    def public_dict(self) -> dict:
        d = asdict(self)
        d["resolved_root"] = str(self.root)
        return d


# --------------------------------------------------------------------------- #
# File-system core (sandboxed)
# --------------------------------------------------------------------------- #


class FileSystem:
    """All disk access goes through here so sandboxing is enforced in one place."""

    def __init__(self, config: ServerConfig):
        self.cfg = config
        self.root = config.root
        self.root.mkdir(parents=True, exist_ok=True)
        config.resume_path.mkdir(parents=True, exist_ok=True)
        config.output_path.mkdir(parents=True, exist_ok=True)
        self._cache: OrderedDict[tuple, str] = OrderedDict()
        self._cache_lock = threading.Lock()
        self.cache_hits = 0

    # ---- paths ----
    def resolve(self, user_path: str, must_exist: bool = True) -> Path:
        if not isinstance(user_path, str) or not user_path.strip():
            raise ToolError("Path must be a non-empty string", "INVALID_PATH")
        candidate = Path(user_path)
        full = (candidate if candidate.is_absolute() else self.root / candidate).resolve()
        if full != self.root and not full.is_relative_to(self.root):
            raise ToolError(f"Access denied: '{user_path}' is outside the sandbox root", "ACCESS_DENIED")
        if must_exist and not full.exists():
            raise ToolError(f"Not found: '{user_path}'", "NOT_FOUND")
        return full

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() if path != self.root else "."

    def check_readable(self, path: Path) -> None:
        if not path.is_file():
            raise ToolError(f"'{self.rel(path)}' is not a file", "NOT_A_FILE")
        if path.suffix.lower() not in self.cfg.allowed_extensions:
            raise ToolError(
                f"Extension '{path.suffix}' is not allowed (allowed: {', '.join(self.cfg.allowed_extensions)})",
                "UNSUPPORTED_TYPE",
            )
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > self.cfg.max_file_size_mb:
            raise ToolError(f"File is {size_mb:.1f} MB; limit is {self.cfg.max_file_size_mb} MB", "FILE_TOO_LARGE")

    # ---- listing ----
    def iter_files(self, directory: Path, pattern: str = "*", recursive: bool = False,
                   include_hidden: bool = False) -> list[Path]:
        if not directory.is_dir():
            raise ToolError(f"'{self.rel(directory)}' is not a directory", "NOT_A_DIRECTORY")
        it = directory.rglob("*") if recursive else directory.glob("*")
        out = []
        for p in it:
            if not p.is_file():
                continue
            if not include_hidden and any(part.startswith(".") for part in p.relative_to(directory).parts):
                continue
            if fnmatch.fnmatch(p.name.lower(), pattern.lower()):
                out.append(p)
        return sorted(out)

    def describe(self, path: Path) -> dict:
        st = path.stat()
        return {
            "path": self.rel(path),
            "name": path.name,
            "extension": path.suffix.lower(),
            "size_bytes": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
        }

    # ---- text extraction (with an LRU cache keyed on mtime+size) ----
    def extract_text(self, path: Path) -> str:
        self.check_readable(path)
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        with self._cache_lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self.cache_hits += 1
                return self._cache[key]
        text = self._extract_uncached(path)
        with self._cache_lock:
            self._cache[key] = text
            while len(self._cache) > self.cfg.text_cache_size:
                self._cache.popitem(last=False)
        return text

    @staticmethod
    def _extract_uncached(path: Path) -> str:
        ext = path.suffix.lower()
        if ext == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError as exc:  # pragma: no cover
                raise ToolError("PDF support needs 'pypdf' (pip install pypdf)", "MISSING_DEPENDENCY") from exc
            try:
                reader = PdfReader(str(path))
                return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
            except Exception as exc:
                raise ToolError(f"Could not parse PDF '{path.name}': {exc}", "PARSE_ERROR") from exc
        if ext == ".docx":
            try:
                import docx  # python-docx
            except ImportError as exc:  # pragma: no cover
                raise ToolError("DOCX support needs 'python-docx'", "MISSING_DEPENDENCY") from exc
            try:
                document = docx.Document(str(path))
                parts = [p.text for p in document.paragraphs]
                for table in document.tables:
                    for row in table.rows:
                        parts.append(" | ".join(cell.text for cell in row.cells))
                return "\n".join(parts).strip()
            except Exception as exc:
                raise ToolError(f"Could not parse DOCX '{path.name}': {exc}", "PARSE_ERROR") from exc
        return path.read_text(encoding="utf-8", errors="replace")

    def metadata(self, path: Path) -> dict:
        self.check_readable(path)
        st = path.stat()
        info = self.describe(path)
        info.update(
            {
                "created": datetime.fromtimestamp(st.st_ctime, timezone.utc).isoformat(timespec="seconds"),
                "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "is_resume": path.suffix.lower() in self.cfg.resume_extensions
                and path.is_relative_to(self.cfg.resume_path),
            }
        )
        if path.suffix.lower() == ".pdf":
            try:
                from pypdf import PdfReader

                info["pages"] = len(PdfReader(str(path)).pages)
            except Exception:
                info["pages"] = None
        return info


# --------------------------------------------------------------------------- #
# Directory watching
# --------------------------------------------------------------------------- #


class DirectoryWatch(threading.Thread):
    """Polls a directory and queues created / modified / deleted events.

    Polling (instead of inotify) is deliberate: it works the same on Linux,
    macOS, Windows, Docker bind-mounts and network shares. A file is only
    reported once its size+mtime are stable for one poll interval, so a
    large PDF that is still being copied is not processed half-written.
    """

    def __init__(self, watch_id: str, directory: Path, patterns: list[str], recursive: bool,
                 interval: float, on_events):
        super().__init__(daemon=True, name=f"watch-{watch_id}")
        self.watch_id = watch_id
        self.directory = directory
        self.patterns = [p.lower() for p in patterns]
        self.recursive = recursive
        self.interval = interval
        self.on_events = on_events
        self.created_at = time.time()
        self.total_events = 0
        self._pending: list[dict] = []
        self._cond = threading.Condition()
        self._stop_evt = threading.Event()
        self._unsettled: dict[Path, tuple[int, int]] = {}
        self.snapshot = self._scan()

    def _scan(self) -> dict[Path, tuple[int, int]]:
        snap: dict[Path, tuple[int, int]] = {}
        it = self.directory.rglob("*") if self.recursive else self.directory.glob("*")
        for p in it:
            try:
                if p.is_file() and not p.name.startswith(".") and any(
                    fnmatch.fnmatch(p.name.lower(), pat) for pat in self.patterns
                ):
                    st = p.stat()
                    snap[p] = (st.st_mtime_ns, st.st_size)
            except FileNotFoundError:
                continue
        return snap

    def poll_once(self) -> list[dict]:
        current = self._scan()
        events: list[dict] = []
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for path, sig in current.items():
            known = self.snapshot.get(path)
            if known == sig:
                self._unsettled.pop(path, None)
                continue
            # changed or new: wait until it is stable for one interval
            if self._unsettled.get(path) != sig:
                self._unsettled[path] = sig
                continue
            self._unsettled.pop(path, None)
            events.append({"type": "created" if known is None else "modified", "path": path,
                           "size_bytes": sig[1], "detected_at": now})
            self.snapshot[path] = sig
        for path in list(self.snapshot):
            if path not in current:
                del self.snapshot[path]
                self._unsettled.pop(path, None)
                events.append({"type": "deleted", "path": path, "size_bytes": None, "detected_at": now})
        if events:
            with self._cond:
                self._pending.extend(events)
                self.total_events += len(events)
                self._cond.notify_all()
            self.on_events(self, events)
        return events

    def run(self) -> None:
        while not self._stop_evt.wait(self.interval):
            try:
                self.poll_once()
            except Exception as exc:  # pragma: no cover - keep watching
                log.warning("watch %s poll failed: %s", self.watch_id, exc)

    def stop(self) -> None:
        self._stop_evt.set()

    @property
    def active(self) -> bool:
        return not self._stop_evt.is_set()

    def drain(self, timeout: float, cancelled) -> list[dict]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._pending and self.active and not cancelled():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(min(0.25, remaining))
            events, self._pending = self._pending, []
        return events

    def peek(self) -> list[dict]:
        with self._cond:
            return list(self._pending)


# --------------------------------------------------------------------------- #
# Server construction
# --------------------------------------------------------------------------- #

PATH_PROP = {"type": "string", "description": "Path relative to the sandbox root (e.g. 'resumes/jane.pdf')."}
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


def build_server(config: ServerConfig) -> MCPServer:
    fs = FileSystem(config)
    server = MCPServer(
        SERVER_NAME,
        SERVER_VERSION,
        title="Resume File System MCP Server",
        instructions=(
            "Sandboxed access to a resume folder. Use list_files / read_file for single files, "
            "batch_process to extract many resumes at once, and watch_directory to wait for new "
            f"resumes. Resumes live in '{config.resume_dir}/', reports go to '{config.output_dir}/'."
        ),
        page_size=config.page_size,
        max_workers=max(8, config.batch_max_workers + 4),
    )
    watches: dict[str, DirectoryWatch] = {}
    watches_lock = threading.Lock()
    server.fs = fs  # exposed for tests
    server.watches = watches

    def stop_all_watches() -> None:
        with watches_lock:
            for w in watches.values():
                w.stop()

    server.shutdown_hooks.append(stop_all_watches)

    def watch_event_dto(ev: dict) -> dict:
        return {**ev, "path": fs.rel(ev["path"])}

    # =================== Milestone 1 tools =================== #

    @server.tool(
        "list_files",
        "List files in a directory inside the sandbox, with size and modification time.",
        {
            "properties": {
                "directory": {**PATH_PROP, "default": "."},
                "pattern": {"type": "string", "default": "*", "description": "Glob such as '*.pdf'."},
                "recursive": {"type": "boolean", "default": False},
                "include_hidden": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        title="List files",
        annotations=READ_ONLY,
    )
    def list_files(args: dict, ctx: RequestContext) -> dict:
        directory = fs.resolve(args.get("directory", "."))
        files = fs.iter_files(directory, args.get("pattern", "*"), args.get("recursive", False),
                              args.get("include_hidden", False))
        return {"directory": fs.rel(directory), "count": len(files), "files": [fs.describe(f) for f in files]}

    @server.tool(
        "read_file",
        "Read a file's text. PDF and DOCX files are converted to plain text automatically.",
        {
            "properties": {
                "path": PATH_PROP,
                "max_chars": {"type": "integer", "minimum": 1, "description": "Truncate long files."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        title="Read file",
        annotations=READ_ONLY,
    )
    def read_file(args: dict, ctx: RequestContext) -> dict:
        path = fs.resolve(args["path"])
        text = fs.extract_text(path)
        limit = args.get("max_chars")
        truncated = bool(limit and len(text) > limit)
        return {
            "path": fs.rel(path),
            "format": path.suffix.lower().lstrip("."),
            "chars": len(text),
            "truncated": truncated,
            "content": text[:limit] if truncated else text,
        }

    @server.tool(
        "write_file",
        "Write a text file inside the sandbox (e.g. a match report). Creates parent folders.",
        {
            "properties": {
                "path": PATH_PROP,
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["overwrite", "append", "create"], "default": "overwrite",
                         "description": "'create' fails if the file exists."},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        title="Write file",
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    )
    def write_file(args: dict, ctx: RequestContext) -> dict:
        if not config.allow_write:
            raise ToolError("Writes are disabled by server configuration (allow_write=false)", "READ_ONLY")
        path = fs.resolve(args["path"], must_exist=False)
        if path.suffix.lower() not in config.writable_extensions:
            raise ToolError(f"Cannot write '{path.suffix}' files (allowed: {config.writable_extensions})",
                            "UNSUPPORTED_TYPE")
        mode = args.get("mode", "overwrite")
        if mode == "create" and path.exists():
            raise ToolError(f"'{fs.rel(path)}' already exists", "ALREADY_EXISTS")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = args["content"].encode("utf-8")
        with open(path, "ab" if mode == "append" else "wb") as fh:
            fh.write(data)
        ctx.log("info", f"wrote {len(data)} bytes to {fs.rel(path)}")
        return {"path": fs.rel(path), "bytes_written": len(data), "mode": mode}

    @server.tool(
        "search_files",
        "Search file contents (including PDF/DOCX text) for a keyword; returns matching lines.",
        {
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "directory": {**PATH_PROP, "default": "."},
                "pattern": {"type": "string", "default": "*"},
                "recursive": {"type": "boolean", "default": True},
                "case_sensitive": {"type": "boolean", "default": False},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 50},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        title="Search files",
        annotations=READ_ONLY,
    )
    def search_files(args: dict, ctx: RequestContext) -> dict:
        directory = fs.resolve(args.get("directory", "."))
        files = [f for f in fs.iter_files(directory, args.get("pattern", "*"), args.get("recursive", True))
                 if f.suffix.lower() in config.allowed_extensions]
        query = args["query"]
        cs = args.get("case_sensitive", False)
        needle = query if cs else query.lower()
        limit = args.get("max_results", 50)
        matches, skipped = [], []
        for f in files:
            try:
                text = fs.extract_text(f)
            except ToolError as exc:
                skipped.append({"path": fs.rel(f), "reason": exc.message})
                continue
            for no, line in enumerate(text.splitlines(), 1):
                if needle in (line if cs else line.lower()):
                    matches.append({"path": fs.rel(f), "line": no, "text": line.strip()[:300]})
                    if len(matches) >= limit:
                        break
            if len(matches) >= limit:
                break
        return {"query": query, "files_searched": len(files), "match_count": len(matches),
                "matches": matches, "skipped": skipped}

    @server.tool(
        "get_file_metadata",
        "Get metadata for a file: size, timestamps, MIME type, SHA-256, page count for PDFs.",
        {"properties": {"path": PATH_PROP}, "required": ["path"], "additionalProperties": False},
        title="File metadata",
        annotations=READ_ONLY,
    )
    def get_file_metadata(args: dict, ctx: RequestContext) -> dict:
        return fs.metadata(fs.resolve(args["path"]))

    # =================== New MCP capabilities =================== #

    def on_watch_events(watch: DirectoryWatch, events: list[dict]) -> None:
        summary = [watch_event_dto(e) for e in events]
        server.log_all("info", {"watch_id": watch.watch_id, "events": summary}, "watch_directory")
        server.notify_resource_updated(f"watch://{watch.watch_id}")
        if any(e["type"] in ("created", "deleted") for e in events):
            server.notify_resources_changed()  # resume:// listing changed

    @server.tool(
        "watch_directory",
        "Monitor a directory for new, modified or deleted resumes. The first call starts a watch "
        "and returns a watch_id; call again with that watch_id to receive the next events. Blocks up "
        "to timeout_seconds waiting for changes (0 = return immediately). The server also pushes "
        "notifications/resources/list_changed and notifications/resources/updated (watch://{id}).",
        {
            "properties": {
                "directory": {**PATH_PROP, "default": config.resume_dir},
                "patterns": {"type": "array", "items": {"type": "string"},
                             "default": ["*" + e for e in config.resume_extensions]},
                "timeout_seconds": {"type": "number", "minimum": 0, "maximum": config.watch_max_timeout,
                                    "default": 10},
                "watch_id": {"type": "string", "description": "Continue an existing watch."},
                "recursive": {"type": "boolean", "default": False},
                "include_existing": {"type": "boolean", "default": False,
                                     "description": "On a new watch, also return files already present."},
            },
            "additionalProperties": False,
        },
        title="Watch directory",
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    )
    def watch_directory(args: dict, ctx: RequestContext) -> dict:
        timeout = float(args.get("timeout_seconds", 10))
        existing: list[dict] = []
        if args.get("watch_id"):
            with watches_lock:
                watch = watches.get(args["watch_id"])
            if watch is None:
                raise ToolError(f"Unknown watch_id '{args['watch_id']}'", "UNKNOWN_WATCH",
                                {"active_watches": list(watches)})
            created = False
        else:
            directory = fs.resolve(args.get("directory", config.resume_dir))
            if not directory.is_dir():
                raise ToolError(f"'{fs.rel(directory)}' is not a directory", "NOT_A_DIRECTORY")
            with watches_lock:
                if len(watches) >= config.max_watches:
                    raise ToolError(f"Too many active watches (max {config.max_watches}); stop one first",
                                    "WATCH_LIMIT")
                watch_id = "w-" + uuid.uuid4().hex[:8]
                watch = DirectoryWatch(
                    watch_id, directory,
                    args.get("patterns") or ["*" + e for e in config.resume_extensions],
                    args.get("recursive", False), config.watch_poll_interval, on_watch_events,
                )
                watches[watch_id] = watch
            watch.start()
            created = True
            if args.get("include_existing"):
                existing = [fs.describe(p) for p in sorted(watch.snapshot)]
            ctx.log("info", f"started watch {watch.watch_id} on {fs.rel(directory)}")

        started = time.monotonic()
        events = watch.drain(timeout, ctx.is_cancelled)
        dto = [watch_event_dto(e) for e in events]
        return {
            "watch_id": watch.watch_id,
            "directory": fs.rel(watch.directory),
            "created_watch": created,
            "active": watch.active,
            "waited_seconds": round(time.monotonic() - started, 2),
            "events": dto,
            "new_files": [e["path"] for e in dto if e["type"] == "created"],
            "modified_files": [e["path"] for e in dto if e["type"] == "modified"],
            "deleted_files": [e["path"] for e in dto if e["type"] == "deleted"],
            "existing_files": existing,
            "next_step": f"Call watch_directory with watch_id='{watch.watch_id}' to keep monitoring, "
                         f"or stop_watch to end it.",
        }

    @server.tool(
        "stop_watch",
        "Stop a directory watch started by watch_directory.",
        {"properties": {"watch_id": {"type": "string"}}, "required": ["watch_id"], "additionalProperties": False},
        title="Stop watch",
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    def stop_watch(args: dict, ctx: RequestContext) -> dict:
        with watches_lock:
            watch = watches.pop(args["watch_id"], None)
        if watch is None:
            raise ToolError(f"Unknown watch_id '{args['watch_id']}'", "UNKNOWN_WATCH")
        watch.stop()
        return {"watch_id": watch.watch_id, "stopped": True, "total_events": watch.total_events,
                "undelivered_events": [watch_event_dto(e) for e in watch.peek()]}

    @server.tool(
        "list_watches",
        "List active directory watches.",
        {"properties": {}, "additionalProperties": False},
        title="List watches",
        annotations=READ_ONLY,
    )
    def list_watches(args: dict, ctx: RequestContext) -> dict:
        with watches_lock:
            items = list(watches.values())
        return {"count": len(items), "watches": [
            {"watch_id": w.watch_id, "directory": fs.rel(w.directory), "patterns": w.patterns,
             "files_tracked": len(w.snapshot), "pending_events": len(w.peek()), "total_events": w.total_events,
             "age_seconds": round(time.time() - w.created_at, 1)} for w in items]}

    def _batch_op(op: str, path: Path, args: dict) -> dict:
        if op == "extract_text":
            text = fs.extract_text(path)
            limit = args.get("max_chars")
            return {"chars": len(text), "words": len(text.split()),
                    "truncated": bool(limit and len(text) > limit),
                    "text": text[:limit] if limit else text}
        if op == "metadata":
            return fs.metadata(path)
        if op == "word_count":
            text = fs.extract_text(path)
            return {"words": len(text.split()), "lines": text.count("\n") + 1, "chars": len(text)}
        if op == "search":
            query = args.get("query") or ""
            if not query:
                raise ToolError("operation 'search' needs 'query'", "INVALID_ARGUMENTS")
            text = fs.extract_text(path)
            hits = [{"line": i, "text": ln.strip()[:300]} for i, ln in enumerate(text.splitlines(), 1)
                    if query.lower() in ln.lower()]
            return {"query": query, "match_count": len(hits), "matches": hits[:20]}
        raise ToolError(f"Unknown operation '{op}'", "INVALID_ARGUMENTS")

    @server.tool(
        "batch_process",
        "Process many files in one call, concurrently. Each file succeeds or fails independently, so one "
        "corrupt resume never breaks the batch. Give explicit 'paths' or a 'directory' + 'pattern'. "
        "Sends notifications/progress when the request carries a progressToken.",
        {
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "directory": {**PATH_PROP},
                "pattern": {"type": "string", "default": "*"},
                "recursive": {"type": "boolean", "default": False},
                "operation": {"type": "string", "enum": ["extract_text", "metadata", "word_count", "search"],
                              "default": "extract_text"},
                "query": {"type": "string", "description": "Keyword for operation='search'."},
                "max_chars": {"type": "integer", "minimum": 1, "description": "Truncate extracted text."},
                "max_workers": {"type": "integer", "minimum": 1, "maximum": 32},
                "fail_fast": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        title="Batch process files",
        annotations=READ_ONLY,
    )
    def batch_process(args: dict, ctx: RequestContext) -> dict:
        op = args.get("operation", "extract_text")
        if op == "search" and not args.get("query"):
            raise ToolError("operation 'search' needs 'query'", "INVALID_ARGUMENTS")
        targets: list[tuple[str, Path | None, str | None]] = []  # (label, path, error)
        if args.get("paths"):
            for p in args["paths"]:
                try:
                    targets.append((p, fs.resolve(p), None))
                except ToolError as exc:
                    targets.append((p, None, exc.message))
        elif args.get("directory") is not None:
            directory = fs.resolve(args["directory"])
            for f in fs.iter_files(directory, args.get("pattern", "*"), args.get("recursive", False)):
                if f.suffix.lower() in config.allowed_extensions:
                    targets.append((fs.rel(f), f, None))
        else:
            raise ToolError("Provide either 'paths' or 'directory'", "INVALID_ARGUMENTS")

        seen, unique = set(), []
        for t in targets:
            if t[0] not in seen:
                seen.add(t[0])
                unique.append(t)
        if len(unique) > config.batch_max_files:
            raise ToolError(f"Batch has {len(unique)} files; limit is {config.batch_max_files}", "BATCH_TOO_LARGE")

        workers = min(args.get("max_workers") or config.batch_max_workers, config.batch_max_workers, max(1, len(unique)))
        hits_before = fs.cache_hits
        started = time.perf_counter()
        results: dict[str, dict] = {}
        stop = threading.Event()

        def run_one(label: str, path: Path | None, pre_error: str | None) -> dict:
            t0 = time.perf_counter()
            if pre_error:
                if args.get("fail_fast"):
                    stop.set()
                return {"path": label, "status": "error", "error": pre_error, "error_code": "INVALID_PATH",
                        "duration_ms": 0.0}
            if stop.is_set() or ctx.is_cancelled():
                return {"path": label, "status": "skipped", "error": "batch stopped", "duration_ms": 0.0}
            try:
                data = _batch_op(op, path, args)
                return {"path": fs.rel(path), "status": "ok", "data": data,
                        "duration_ms": round((time.perf_counter() - t0) * 1000, 1)}
            except ToolError as exc:
                if args.get("fail_fast"):
                    stop.set()  # set inside the worker so no queued file starts after a failure
                return {"path": fs.rel(path), "status": "error", "error": exc.message, "error_code": exc.code,
                        "duration_ms": round((time.perf_counter() - t0) * 1000, 1)}

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="batch") as pool:
            futures = {pool.submit(run_one, *t): t[0] for t in unique}
            done = 0
            for fut in as_completed(futures):
                res = fut.result()
                results[futures[fut]] = res
                done += 1
                ctx.report_progress(done, len(unique), f"{res['status']}: {res['path']}")

        ordered = [results[t[0]] for t in unique]
        ok = sum(r["status"] == "ok" for r in ordered)
        failed = sum(r["status"] == "error" for r in ordered)
        return {
            "operation": op,
            "total": len(ordered),
            "succeeded": ok,
            "failed": failed,
            "skipped": len(ordered) - ok - failed,
            "workers": workers,
            "cache_hits": fs.cache_hits - hits_before,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "results": ordered,
        }

    # =================== Resources =================== #

    def resume_resources():
        if not config.resume_path.is_dir():
            return []
        for p in fs.iter_files(config.resume_path):
            if p.suffix.lower() in config.resume_extensions:
                yield Resource(
                    uri=f"resume://{p.name}",
                    name=p.name,
                    title=f"Resume: {p.stem}",
                    description=f"Plain-text content of {fs.rel(p)} ({p.suffix.lstrip('.').upper()})",
                    mime_type="text/plain",
                    size=p.stat().st_size,
                    reader=lambda p=p: fs.extract_text(p),
                )

    server.add_resource_provider(resume_resources)

    def read_resume(v: dict) -> str:
        path = fs.resolve(f"{config.resume_dir}/{v['filename']}")
        return fs.extract_text(path)

    server.add_template(ResourceTemplate(
        "resume://{filename}", "resume", read_resume, title="Resume by file name",
        description="Text of a resume in the resume folder", mime_type="text/plain"))

    def read_any(v: dict) -> str:
        return fs.extract_text(fs.resolve(v["path"]))

    server.add_template(ResourceTemplate(
        "fs://files/{+path}", "file", read_any, title="Any file in the sandbox",
        description="Text of any readable file, path relative to the sandbox root", mime_type="text/plain"))

    def read_watch(v: dict) -> str:
        watch = watches.get(v["watch_id"])
        if watch is None:
            raise FileNotFoundError(v["watch_id"])
        return json.dumps({"watch_id": watch.watch_id, "directory": fs.rel(watch.directory), "active": watch.active,
                           "files": sorted(fs.rel(p) for p in watch.snapshot),
                           "pending_events": [watch_event_dto(e) for e in watch.peek()]}, indent=2)

    server.add_template(ResourceTemplate(
        "watch://{watch_id}", "watch", read_watch, title="Directory watch state",
        description="Live state of a watch; subscribe to get notifications/resources/updated",
        mime_type="application/json"))

    server.add_resource(Resource(
        "config://server", "server-config", lambda: json.dumps(config.public_dict(), indent=2),
        title="Server configuration", description="Effective configuration after file/env/CLI merging",
        mime_type="application/json"))

    def capabilities_doc() -> str:
        return json.dumps({
            "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "tools": [{"name": t.name, "title": t.title, "description": t.description} for t in server.tools.values()],
            "resource_templates": [t.to_dict() for t in server.templates],
            "static_resources": [r.to_dict() for r in server.resources.values()],
        }, indent=2)

    server.add_resource(Resource(
        "server://capabilities", "capabilities", capabilities_doc, title="Capability catalogue",
        description="Human/machine-readable list of every tool and resource this server exposes",
        mime_type="application/json"))

    return server


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Filesystem MCP server for the resume matching agent")
    ap.add_argument("--config", help="Path to a JSON config file")
    ap.add_argument("--root", dest="root_dir", help="Sandbox root directory")
    ap.add_argument("--resume-dir", dest="resume_dir")
    ap.add_argument("--transport", choices=["stdio", "http"])
    ap.add_argument("--host", dest="http_host")
    ap.add_argument("--port", dest="http_port", type=int)
    ap.add_argument("--log-level", dest="log_level")
    ap.add_argument("--read-only", dest="allow_write", action="store_const", const=False)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    overrides = {k: v for k, v in vars(args).items() if k != "config"}
    config = ServerConfig.load(args.config, overrides)
    configure_logging(config.log_level)
    server = build_server(config)
    log.info("sandbox root: %s", config.root)
    if config.transport == "http":
        server.run_http(config.http_host, config.http_port)
    else:
        server.run_stdio()


if __name__ == "__main__":
    main()
