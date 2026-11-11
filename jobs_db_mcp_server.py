#!/usr/bin/env python3
"""
jobs_db_mcp_server.py  (Bonus: Multi-MCP integration)
=====================================================

A second MCP server, backed by SQLite, that owns **job postings** and the
**history of match runs**. The matching agent talks to it *and* to the
filesystem server at the same time:

    filesystem MCP  → resumes (read, batch extract, watch)
    jobs-db MCP     → job requirements in, ranked results out

Tools:      list_jobs, get_job, add_job, save_match_results,
            get_match_history, get_top_candidates
Resources:  jobs://{job_id} (one per job, listed), db://schema

Run:  python jobs_db_mcp_server.py [--db data/jobs.db] [--seed-dir data/jobs]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from mcp_protocol import MCPServer, RequestContext, Resource, ResourceTemplate, ToolError, configure_logging

SERVER_NAME = "jobs-db-mcp-server"
SERVER_VERSION = "1.0.0"
PROJECT_DIR = Path(__file__).resolve().parent
log = logging.getLogger("jobs_db_mcp")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id           TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    company          TEXT,
    location         TEXT,
    min_years        INTEGER DEFAULT 0,
    required_skills  TEXT NOT NULL DEFAULT '[]',
    preferred_skills TEXT NOT NULL DEFAULT '[]',
    description      TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS match_runs (
    run_id               TEXT PRIMARY KEY,
    job_id               TEXT NOT NULL REFERENCES jobs(job_id),
    created_at           TEXT NOT NULL,
    candidates_evaluated INTEGER NOT NULL,
    metadata             TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS match_results (
    run_id            TEXT NOT NULL REFERENCES match_runs(run_id),
    rank              INTEGER NOT NULL,
    candidate         TEXT NOT NULL,
    resume_path       TEXT,
    score             REAL NOT NULL,
    matched_skills    TEXT NOT NULL DEFAULT '[]',
    missing_skills    TEXT NOT NULL DEFAULT '[]',
    explanation       TEXT,
    PRIMARY KEY (run_id, rank)
);
"""

JOB_FIELDS = ["job_id", "title", "company", "location", "min_years", "required_skills", "preferred_skills",
              "description"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobsDB:
    def __init__(self, db_path: Path):
        self.path = db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    @staticmethod
    def job_row(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["required_skills"] = json.loads(d["required_skills"])
        d["preferred_skills"] = json.loads(d["preferred_skills"])
        return d

    def upsert_job(self, job: dict) -> None:
        with self._write_lock, self.conn() as c:
            c.execute(
                "INSERT INTO jobs (job_id,title,company,location,min_years,required_skills,preferred_skills,"
                "description,created_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
                "title=excluded.title, company=excluded.company, location=excluded.location, "
                "min_years=excluded.min_years, required_skills=excluded.required_skills, "
                "preferred_skills=excluded.preferred_skills, description=excluded.description",
                (job["job_id"], job["title"], job.get("company"), job.get("location"), int(job.get("min_years", 0)),
                 json.dumps(job.get("required_skills", [])), json.dumps(job.get("preferred_skills", [])),
                 job.get("description", ""), now()),
            )

    def seed(self, seed_dir: Path) -> int:
        with self.conn() as c:
            if c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]:
                return 0
        n = 0
        for f in sorted(seed_dir.glob("*.json")):
            self.upsert_job(json.loads(f.read_text(encoding="utf-8")))
            n += 1
        return n


def build_server(db: JobsDB) -> MCPServer:
    server = MCPServer(
        SERVER_NAME, SERVER_VERSION, title="Jobs Database MCP Server",
        instructions="Job postings and match history. Use get_job for requirements; save_match_results "
                     "to persist a ranked shortlist.",
    )
    server.db = db
    ro = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

    def require_job(c: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = c.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            ids = [r[0] for r in c.execute("SELECT job_id FROM jobs ORDER BY job_id")]
            raise ToolError(f"Unknown job_id '{job_id}'", "NOT_FOUND", {"available_job_ids": ids})
        return row

    @server.tool("list_jobs", "List job postings (id, title, location, minimum years).",
                 {"properties": {"location": {"type": "string"}}, "additionalProperties": False},
                 title="List jobs", annotations=ro)
    def list_jobs(args: dict, ctx: RequestContext) -> dict:
        with db.conn() as c:
            sql, params = "SELECT job_id,title,company,location,min_years FROM jobs", ()
            if args.get("location"):
                sql, params = sql + " WHERE lower(location) = lower(?)", (args["location"],)
            rows = [dict(r) for r in c.execute(sql + " ORDER BY job_id", params)]
        return {"count": len(rows), "jobs": rows}

    @server.tool("get_job", "Get the full requirements of one job posting.",
                 {"properties": {"job_id": {"type": "string"}}, "required": ["job_id"], "additionalProperties": False},
                 title="Get job", annotations=ro)
    def get_job(args: dict, ctx: RequestContext) -> dict:
        with db.conn() as c:
            return db.job_row(require_job(c, args["job_id"]))

    @server.tool(
        "add_job", "Create or update a job posting.",
        {"properties": {
            "job_id": {"type": "string", "minLength": 1}, "title": {"type": "string", "minLength": 1},
            "company": {"type": "string"}, "location": {"type": "string"},
            "min_years": {"type": "integer", "minimum": 0},
            "required_skills": {"type": "array", "items": {"type": "string"}},
            "preferred_skills": {"type": "array", "items": {"type": "string"}},
            "description": {"type": "string"}},
         "required": ["job_id", "title", "required_skills"], "additionalProperties": False},
        title="Add job",
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
    def add_job(args: dict, ctx: RequestContext) -> dict:
        db.upsert_job(args)
        server.notify_resources_changed()
        return {"job_id": args["job_id"], "saved": True}

    result_item = {
        "type": "object",
        "properties": {
            "candidate": {"type": "string"}, "resume_path": {"type": "string"},
            "score": {"type": "number", "minimum": 0, "maximum": 100},
            "matched_skills": {"type": "array", "items": {"type": "string"}},
            "missing_skills": {"type": "array", "items": {"type": "string"}},
            "explanation": {"type": "string"},
        },
        "required": ["candidate", "score"],
    }

    @server.tool(
        "save_match_results", "Persist a ranked list of candidates for a job as a new match run.",
        {"properties": {"job_id": {"type": "string"}, "results": {"type": "array", "items": result_item},
                        "metadata": {"type": "object"}},
         "required": ["job_id", "results"], "additionalProperties": False},
        title="Save match results",
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
    def save_match_results(args: dict, ctx: RequestContext) -> dict:
        run_id = "run-" + uuid.uuid4().hex[:10]
        ranked = sorted(args["results"], key=lambda r: r["score"], reverse=True)
        with db._write_lock, db.conn() as c:
            require_job(c, args["job_id"])
            c.execute("INSERT INTO match_runs VALUES (?,?,?,?,?)",
                      (run_id, args["job_id"], now(), len(ranked), json.dumps(args.get("metadata", {}))))
            c.executemany(
                "INSERT INTO match_results VALUES (?,?,?,?,?,?,?,?)",
                [(run_id, i, r["candidate"], r.get("resume_path"), float(r["score"]),
                  json.dumps(r.get("matched_skills", [])), json.dumps(r.get("missing_skills", [])),
                  r.get("explanation")) for i, r in enumerate(ranked, 1)])
        server.notify_resource_updated(f"jobs://{args['job_id']}")
        return {"run_id": run_id, "job_id": args["job_id"], "saved": len(ranked)}

    @server.tool(
        "get_match_history", "List previous match runs, newest first.",
        {"properties": {"job_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100,
                                                                 "default": 10}},
         "additionalProperties": False},
        title="Match history", annotations=ro)
    def get_match_history(args: dict, ctx: RequestContext) -> dict:
        with db.conn() as c:
            sql, params = "SELECT * FROM match_runs", []
            if args.get("job_id"):
                sql += " WHERE job_id = ?"
                params.append(args["job_id"])
            sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
            params.append(args.get("limit", 10))
            runs = []
            for r in c.execute(sql, params):
                d = dict(r)
                d["metadata"] = json.loads(d["metadata"])
                runs.append(d)
        return {"count": len(runs), "runs": runs}

    @server.tool(
        "get_top_candidates", "Top candidates for a job from its most recent match run.",
        {"properties": {"job_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "default": 5},
                        "min_score": {"type": "number", "minimum": 0, "maximum": 100, "default": 0}},
         "required": ["job_id"], "additionalProperties": False},
        title="Top candidates", annotations=ro)
    def get_top_candidates(args: dict, ctx: RequestContext) -> dict:
        with db.conn() as c:
            require_job(c, args["job_id"])
            run = c.execute("SELECT run_id, created_at FROM match_runs WHERE job_id = ? "
                            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (args["job_id"],)).fetchone()
            if run is None:
                return {"job_id": args["job_id"], "run_id": None, "candidates": []}
            rows = c.execute("SELECT * FROM match_results WHERE run_id = ? AND score >= ? ORDER BY rank LIMIT ?",
                             (run["run_id"], args.get("min_score", 0), args.get("limit", 5))).fetchall()
        cands = []
        for r in rows:
            d = dict(r)
            d["matched_skills"] = json.loads(d["matched_skills"])
            d["missing_skills"] = json.loads(d["missing_skills"])
            cands.append(d)
        return {"job_id": args["job_id"], "run_id": run["run_id"], "run_at": run["created_at"], "candidates": cands}

    # ---- resources ----
    def job_resources():
        with db.conn() as c:
            rows = c.execute("SELECT job_id, title FROM jobs ORDER BY job_id").fetchall()
        for r in rows:
            yield Resource(f"jobs://{r['job_id']}", r["job_id"], title=r["title"], mime_type="application/json",
                           description=f"Job posting {r['job_id']}: {r['title']}",
                           reader=lambda jid=r["job_id"]: read_job({"job_id": jid}))

    def read_job(v: dict) -> str:
        with db.conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE job_id = ?", (v["job_id"],)).fetchone()
        if row is None:
            raise FileNotFoundError(v["job_id"])
        return json.dumps(db.job_row(row), indent=2)

    server.add_resource_provider(job_resources)
    server.add_template(ResourceTemplate("jobs://{job_id}", "job", read_job, title="Job posting",
                                         mime_type="application/json"))
    server.add_resource(Resource("db://schema", "schema", lambda: SCHEMA.strip(), title="Database schema",
                                 mime_type="text/plain", description="SQLite DDL for jobs and match history"))
    return server


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Jobs database MCP server")
    ap.add_argument("--db", default=os.environ.get("JOBS_DB_PATH", str(PROJECT_DIR / "data" / "jobs.db")))
    ap.add_argument("--seed-dir", default=os.environ.get("JOBS_SEED_DIR", str(PROJECT_DIR / "data" / "jobs")))
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--log-level", default=os.environ.get("JOBS_DB_LOG_LEVEL", "INFO"))
    args = ap.parse_args(argv)
    configure_logging(args.log_level)
    db = JobsDB(Path(args.db))
    seeded = db.seed(Path(args.seed_dir)) if Path(args.seed_dir).is_dir() else 0
    if seeded:
        log.info("seeded %d jobs from %s", seeded, args.seed_dir)
    server = build_server(db)
    if args.transport == "http":
        server.run_http(port=args.port)
    else:
        server.run_stdio()


if __name__ == "__main__":
    main()
