#!/usr/bin/env python3
"""
matching_agent.py  —  Milestone 4 refactor
==========================================

LangGraph resume-matching agent that reaches the outside world **only
through MCP**. All the direct file-system tools from Milestones 1–3
(``open()``, ``os.listdir``, PDF/DOCX parsing, writing reports) are gone.
The agent is now an MCP *client*:

    filesystem MCP server   list_files · batch_process · watch_directory · write_file
    jobs-db MCP server      get_job · save_match_results        (bonus, optional)

Workflow (LangGraph state machine)
----------------------------------
    connect_and_discover → load_job → discover_resumes → extract_resumes
      → analyze_candidates → score_and_rank → explain_matches → publish_results
      → (watch mode) wait_for_new_resumes ⟲ extract_resumes …  → finalize

Usage
-----
    python matching_agent.py match --job JOB-001
    python matching_agent.py match --job JOB-001 --watch --watch-timeout 15 --max-cycles 2
    python matching_agent.py tools                 # MCP discovery report
    python matching_agent.py graph                 # print the Mermaid state diagram
    python matching_agent.py ask "Which resumes mention Kafka?"   # LLM + MCP tools (needs LLM_MODEL)
"""

from __future__ import annotations

import argparse
import json
import logging
import operator
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

import matching_engine as engine
from mcp_client import MCPClientError, MCPToolError, MultiMCPClient, as_langchain_tools

log = logging.getLogger("matching_agent")
AGENT_VERSION = "4.0.0"
RESUME_EXTENSIONS = (".pdf", ".docx", ".txt", ".md")
REQUIRED_FS_TOOLS = {"list_files", "batch_process", "write_file"}


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


class MatchState(TypedDict, total=False):
    # inputs
    job_id: str
    resume_dir: str
    top_k: int
    watch: bool
    watch_timeout: float
    max_watch_cycles: int
    # discovery
    servers: dict
    job_source: str
    # working data
    job: dict
    pending_files: list[str]  # files to process in this cycle
    documents: dict[str, str]  # resume path -> extracted text
    candidates: dict[str, dict]  # resume path -> profile
    ranked: list[dict]
    # outputs
    report_path: str
    run_ids: list[str]
    watch_id: str
    watch_cycles: int
    status: str
    # append-only logs (LangGraph reducers merge these across nodes)
    errors: Annotated[list[dict], operator.add]
    steps: Annotated[list[dict], operator.add]


def _step(node: str, detail: str, **extra: Any) -> dict:
    return {"steps": [{"node": node, "detail": detail, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       **extra}]}


def _is_resume(path: str) -> bool:
    return path.lower().endswith(RESUME_EXTENSIONS)


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #


def build_matching_graph(mcp: MultiMCPClient, llm: Any = None,
                         on_event: Callable[[str, str], None] | None = None):
    """Build the LangGraph workflow. ``mcp`` is the only door to files and data."""

    emit = on_event or (lambda node, msg: log.info("[%s] %s", node, msg))

    # ---------------- nodes ---------------- #

    def connect_and_discover(state: MatchState) -> dict:
        """Capability discovery: what servers and tools are available right now?"""
        servers = {name: sorted(tools) for name, tools in mcp.tools.items()}
        missing = REQUIRED_FS_TOOLS - set(mcp.tools.get("filesystem", {}))
        if missing:
            raise RuntimeError(f"filesystem MCP server is missing required tools: {sorted(missing)}")
        for name, tools in servers.items():
            emit("discover", f"{name}: {len(tools)} tools → {', '.join(tools)}")
        for name, reason in mcp.skipped.items():
            emit("discover", f"{name}: skipped ({reason})")
        return {"servers": servers, "watch_cycles": 0, "documents": {}, "candidates": {}, "run_ids": [],
                **_step("connect_and_discover", f"{len(servers)} MCP servers connected")}

    def load_job(state: MatchState) -> dict:
        job_id = state["job_id"]
        try:
            if mcp.has_tool("get_job"):  # multi-MCP path: job lives in the jobs DB server
                job, source = mcp.call("get_job", {"job_id": job_id}), "jobs_db.get_job"
            else:  # fallback: job JSON on disk, still read through the filesystem MCP server
                data = mcp.call("read_file", {"path": f"jobs/{job_id}.json"})
                job, source = json.loads(data["content"]), "filesystem.read_file"
        except (MCPToolError, MCPClientError, json.JSONDecodeError) as exc:
            emit("load_job", f"cannot load job {job_id}: {exc}")
            return {"status": "failed", "errors": [{"node": "load_job", "error": str(exc)}],
                    **_step("load_job", "job not found")}
        emit("load_job", f"{job['job_id']} '{job['title']}' via {source} — required: "
                         f"{', '.join(job.get('required_skills', []))}")
        return {"job": job, "job_source": source, "status": "running",
                **_step("load_job", f"loaded {job_id} via {source}")}

    def discover_resumes(state: MatchState) -> dict:
        update: dict[str, Any] = {}
        # Start the watch *before* reading, so a resume dropped in mid-run is never missed.
        if state.get("watch") and not state.get("watch_id"):
            w = mcp.call("watch_directory", {"directory": state["resume_dir"], "timeout_seconds": 0})
            update["watch_id"] = w["watch_id"]
            emit("discover_resumes", f"watch {w['watch_id']} started on {w['directory']}")
        listing = mcp.call("list_files", {"directory": state["resume_dir"]})
        files = [f["path"] for f in listing["files"] if _is_resume(f["path"])]
        emit("discover_resumes", f"{len(files)} resumes found in {listing['directory']}")
        return {**update, "pending_files": files, **_step("discover_resumes", f"{len(files)} files")}

    def extract_resumes(state: MatchState) -> dict:
        files = state.get("pending_files", [])

        def progress(p: dict) -> None:
            emit("extract_resumes", f"  progress {p['progress']:g}/{p.get('total', '?'):g} {p.get('message', '')}")

        batch = mcp.call("batch_process", {"paths": files, "operation": "extract_text"}, on_progress=progress)
        documents = dict(state.get("documents", {}))
        errors = []
        for r in batch["results"]:
            if r["status"] == "ok" and r["data"]["text"].strip():
                documents[r["path"]] = r["data"]["text"]
            else:
                reason = r.get("error") or "no extractable text"
                errors.append({"node": "extract_resumes", "file": r["path"], "error": reason})
                emit("extract_resumes", f"  skipped {r['path']}: {reason}")
        emit("extract_resumes", f"batch_process: {batch['succeeded']}/{batch['total']} ok in "
                                f"{batch['duration_ms']} ms using {batch['workers']} workers")
        return {"documents": documents, "errors": errors,
                **_step("extract_resumes", f"{batch['succeeded']} ok, {batch['failed']} failed",
                        duration_ms=batch["duration_ms"])}

    def analyze_candidates(state: MatchState) -> dict:
        candidates = dict(state.get("candidates", {}))
        for path in state.get("pending_files", []):
            if path in state["documents"]:
                candidates[path] = engine.extract_profile(path, state["documents"][path])
        emit("analyze_candidates", f"{len(candidates)} candidate profiles")
        return {"candidates": candidates, "pending_files": [],
                **_step("analyze_candidates", f"{len(candidates)} profiles")}

    def score_and_rank(state: MatchState) -> dict:
        ranked = engine.rank_candidates(state["candidates"], state["documents"], state["job"])
        for c in ranked[: state.get("top_k", 5)]:
            emit("score_and_rank", f"  #{c['rank']} {c['candidate']:<16} {c['score']:>5}  {c['tier']}")
        return {"ranked": ranked, **_step("score_and_rank", f"ranked {len(ranked)} candidates")}

    def explain_matches(state: MatchState) -> dict:
        job, ranked = state["job"], [dict(c) for c in state["ranked"]]
        top_k = state.get("top_k", 5)
        used_llm = False
        if llm is not None:
            try:
                prompt = (
                    "You are a technical recruiter. For each candidate write ONE sentence explaining the fit for "
                    f"the role '{job['title']}'. Return JSON: {{\"<candidate name>\": \"<sentence>\"}}.\n\n"
                    f"Job: {json.dumps(job)}\n\nCandidates: "
                    + json.dumps([{k: c[k] for k in ("candidate", "score", "matched_skills", "missing_skills",
                                                       "years_experience")} for c in ranked[:top_k]])
                )
                raw = llm.invoke(prompt).content
                raw = raw[raw.find("{"): raw.rfind("}") + 1]
                notes = json.loads(raw)
                for c in ranked[:top_k]:
                    c["explanation"] = notes.get(c["candidate"]) or engine.template_explanation(c, job)
                used_llm = True
            except Exception as exc:  # LLM is a nice-to-have; never fail the run because of it
                emit("explain_matches", f"LLM explanation failed ({exc}); using rule-based explanations")
        for c in ranked:
            c.setdefault("explanation", engine.template_explanation(c, job))
        return {"ranked": ranked, **_step("explain_matches", "llm" if used_llm else "rule-based")}

    def publish_results(state: MatchState) -> dict:
        job, ranked = state["job"], state["ranked"]
        report = render_report(state)
        report_path = f"output/match_report_{job['job_id']}.md"
        mcp.call("write_file", {"path": report_path, "content": report})
        emit("publish_results", f"report written via filesystem.write_file → {report_path}")
        run_ids = list(state.get("run_ids", []))
        if mcp.has_tool("save_match_results"):
            saved = mcp.call("save_match_results", {
                "job_id": job["job_id"],
                "results": [{k: c[k] for k in ("candidate", "resume_path", "score", "matched_skills",
                                                "missing_skills", "explanation")} for c in ranked],
                "metadata": {"agent_version": AGENT_VERSION, "cycle": state.get("watch_cycles", 0),
                             "skipped_files": [e["file"] for e in state.get("errors", []) if "file" in e]},
            })
            run_ids.append(saved["run_id"])
            emit("publish_results", f"{saved['saved']} results saved via jobs_db.save_match_results "
                                    f"(run {saved['run_id']})")
        return {"report_path": report_path, "run_ids": run_ids,
                **_step("publish_results", report_path)}

    def wait_for_new_resumes(state: MatchState) -> dict:
        cycle = state.get("watch_cycles", 0) + 1
        emit("wait_for_new_resumes", f"cycle {cycle}/{state.get('max_watch_cycles', 1)}: waiting up to "
                                     f"{state.get('watch_timeout', 10)}s for new resumes …")
        res = mcp.call("watch_directory", {"watch_id": state["watch_id"],
                                           "timeout_seconds": state.get("watch_timeout", 10)},
                       timeout=state.get("watch_timeout", 10) + 30)
        changed = [p for p in res["new_files"] + res["modified_files"] if _is_resume(p)]
        update: dict[str, Any] = {"watch_cycles": cycle, "pending_files": changed}
        if res["deleted_files"]:
            update["candidates"] = {k: v for k, v in state["candidates"].items() if k not in res["deleted_files"]}
            update["documents"] = {k: v for k, v in state["documents"].items() if k not in res["deleted_files"]}
        emit("wait_for_new_resumes", f"new: {res['new_files'] or '-'}  modified: {res['modified_files'] or '-'}  "
                                     f"deleted: {res['deleted_files'] or '-'}")
        return {**update, **_step("wait_for_new_resumes", f"{len(changed)} changed",
                                  deleted=res["deleted_files"])}

    def finalize(state: MatchState) -> dict:
        if state.get("watch_id"):
            try:
                mcp.call("stop_watch", {"watch_id": state["watch_id"]})
                emit("finalize", f"watch {state['watch_id']} stopped")
            except (MCPToolError, MCPClientError):
                pass
        status = "failed" if state.get("status") == "failed" else "completed"
        return {"status": status, **_step("finalize", status)}

    # ---------------- routing ---------------- #

    def after_load_job(state: MatchState) -> str:
        return "finalize" if state.get("status") == "failed" else "discover_resumes"

    def after_discover(state: MatchState) -> str:
        if state.get("pending_files"):
            return "extract_resumes"
        return "wait_for_new_resumes" if state.get("watch") else "finalize"

    def after_publish(state: MatchState) -> str:
        if state.get("watch") and state.get("watch_cycles", 0) < state.get("max_watch_cycles", 1):
            return "wait_for_new_resumes"
        return "finalize"

    def after_wait(state: MatchState) -> str:
        if state.get("pending_files"):
            return "extract_resumes"
        last = state["steps"][-1] if state.get("steps") else {}
        if last.get("deleted"):
            return "score_and_rank"  # a resume was removed: re-rank without re-extracting
        if state.get("watch_cycles", 0) < state.get("max_watch_cycles", 1):
            return "wait_for_new_resumes"
        return "finalize"

    g = StateGraph(MatchState)
    for fn in (connect_and_discover, load_job, discover_resumes, extract_resumes, analyze_candidates,
               score_and_rank, explain_matches, publish_results, wait_for_new_resumes, finalize):
        g.add_node(fn.__name__, fn)
    g.add_edge(START, "connect_and_discover")
    g.add_edge("connect_and_discover", "load_job")
    g.add_conditional_edges("load_job", after_load_job, ["discover_resumes", "finalize"])
    g.add_conditional_edges("discover_resumes", after_discover,
                            ["extract_resumes", "wait_for_new_resumes", "finalize"])
    g.add_edge("extract_resumes", "analyze_candidates")
    g.add_edge("analyze_candidates", "score_and_rank")
    g.add_edge("score_and_rank", "explain_matches")
    g.add_edge("explain_matches", "publish_results")
    g.add_conditional_edges("publish_results", after_publish, ["wait_for_new_resumes", "finalize"])
    g.add_conditional_edges("wait_for_new_resumes", after_wait,
                            ["extract_resumes", "score_and_rank", "wait_for_new_resumes", "finalize"])
    g.add_edge("finalize", END)
    return g.compile()


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def render_report(state: MatchState) -> str:
    job, ranked = state["job"], state["ranked"]
    top_k = state.get("top_k", 5)
    lines = [
        f"# Match report — {job['job_id']}: {job['title']}",
        "",
        f"*Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} by matching_agent "
        f"v{AGENT_VERSION} via MCP ({', '.join(state.get('servers', {}))})*",
        "",
        f"**Required:** {', '.join(job.get('required_skills', []))}  ",
        f"**Preferred:** {', '.join(job.get('preferred_skills', []))}  ",
        f"**Minimum experience:** {job.get('min_years', 0)} years",
        "",
        "| Rank | Candidate | Score | Tier | Years | Missing required |",
        "|---:|---|---:|---|---:|---|",
    ]
    for c in ranked:
        lines.append(f"| {c['rank']} | {c['candidate']} | {c['score']} | {c['tier']} | "
                     f"{c['years_experience']:g} | {', '.join(c['missing_skills']) or '—'} |")
    lines += ["", f"## Top {min(top_k, len(ranked))} shortlist", ""]
    for c in ranked[:top_k]:
        b = c["breakdown"]
        lines += [
            f"### {c['rank']}. {c['candidate']} — {c['score']}/100 ({c['tier']})",
            f"- Resume: `{c['resume_path']}`",
            f"- {c['explanation']}",
            f"- Breakdown: required {b['required_coverage']:.0%}, preferred {b['preferred_coverage']:.0%}, "
            f"text similarity {b['text_similarity']:.2f}, experience fit {b['experience_fit']:.0%}",
            "",
        ]
    skipped = [e for e in state.get("errors", []) if "file" in e]
    if skipped:
        lines += ["## Files that could not be processed", ""]
        lines += [f"- `{e['file']}` — {e['error']}" for e in skipped]
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Optional LLM (explanations + "ask" mode)
# --------------------------------------------------------------------------- #


def get_llm():
    """Return a LangChain chat model if LLM_MODEL is set (e.g. 'openai:gpt-4o-mini'), else None."""
    model = os.environ.get("LLM_MODEL")
    if not model:
        return None
    try:
        from langchain.chat_models import init_chat_model

        return init_chat_model(model, temperature=0)
    except Exception as exc:
        log.warning("LLM '%s' unavailable (%s); continuing without it", model, exc)
        return None


def build_tool_calling_agent(mcp: MultiMCPClient, llm: Any):
    """A minimal ReAct loop where the LLM picks MCP tools discovered at runtime."""
    from langchain_core.messages import SystemMessage, ToolMessage

    tools = as_langchain_tools(mcp)
    by_name = {t.name: t for t in tools}
    model = llm.bind_tools(tools)

    class ChatState(TypedDict):
        messages: Annotated[list, operator.add]

    def call_model(state: ChatState) -> dict:
        system = SystemMessage(content="You help recruiters. Use the MCP tools to inspect resumes in 'resumes/' "
                                       "and job postings. Be concise and cite file names.")
        return {"messages": [model.invoke([system, *state["messages"]])]}

    def call_tools(state: ChatState) -> dict:
        out = []
        for tc in state["messages"][-1].tool_calls:
            out.append(ToolMessage(content=by_name[tc["name"]].invoke(tc["args"]), tool_call_id=tc["id"]))
        return {"messages": out}

    g = StateGraph(ChatState)
    g.add_node("model", call_model)
    g.add_node("tools", call_tools)
    g.add_edge(START, "model")
    g.add_conditional_edges("model", lambda s: "tools" if getattr(s["messages"][-1], "tool_calls", None) else END,
                            ["tools", END])
    g.add_edge("tools", "model")
    return g.compile()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def run_match(job_id: str, servers: str, resume_dir: str = "resumes", top_k: int = 5, watch: bool = False,
              watch_timeout: float = 10, max_cycles: int = 1, trace=None, on_event=None) -> MatchState:
    with MultiMCPClient.from_config(servers, trace=trace) as mcp:
        graph = build_matching_graph(mcp, llm=get_llm(), on_event=on_event)
        return graph.invoke(
            {"job_id": job_id, "resume_dir": resume_dir, "top_k": top_k, "watch": watch,
             "watch_timeout": watch_timeout, "max_watch_cycles": max_cycles, "errors": [], "steps": []},
            {"recursion_limit": 100},
        )


def print_summary(state: MatchState, top_k: int) -> None:
    print()
    if state.get("status") == "failed":
        print("Run failed:", *[e["error"] for e in state.get("errors", [])], sep="\n  ")
        return
    job = state["job"]
    print(f"=== {job['job_id']} · {job['title']} ===")
    print(f"{'#':>2}  {'Candidate':<16} {'Score':>6}  {'Tier':<8} Missing required")
    for c in state["ranked"][:top_k]:
        print(f"{c['rank']:>2}  {c['candidate']:<16} {c['score']:>6}  {c['tier']:<8} "
              f"{', '.join(c['missing_skills']) or '—'}")
    skipped = [e for e in state.get("errors", []) if "file" in e]
    if skipped:
        print(f"\nSkipped {len(skipped)} file(s): " + ", ".join(e["file"] for e in skipped))
    print(f"\nReport: data/{state['report_path']}")
    if state.get("run_ids"):
        print(f"Saved runs: {', '.join(state['run_ids'])}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Resume matching agent (LangGraph + MCP)")
    ap.add_argument("--servers", default=str(Path(__file__).resolve().parent / "mcp_servers.json"))
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("match", help="rank resumes for a job")
    m.add_argument("--job", required=True)
    m.add_argument("--resume-dir", default="resumes")
    m.add_argument("--top", type=int, default=5)
    m.add_argument("--watch", action="store_true", help="keep watching for new resumes after the first pass")
    m.add_argument("--watch-timeout", type=float, default=10)
    m.add_argument("--max-cycles", type=int, default=2)
    m.add_argument("--trace-rpc", action="store_true", help="print every JSON-RPC message")
    sub.add_parser("tools", help="print the MCP discovery report")
    sub.add_parser("graph", help="print the LangGraph state machine as Mermaid")
    a = sub.add_parser("ask", help="free-form question answered by an LLM using MCP tools")
    a.add_argument("question")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles/pipes: never crash on → or —
        sys.stdout.reconfigure(errors="replace")

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    def on_event(node: str, msg: str) -> None:
        print(f"[{node:<20}] {msg}", flush=True)

    def trace(server: str, direction: str, message: dict) -> None:
        text = json.dumps(message)
        print(f"    {direction} {server:<10} {text[:160]}{'…' if len(text) > 160 else ''}", flush=True)

    if args.cmd == "match":
        t0 = time.perf_counter()
        state = run_match(args.job, args.servers, args.resume_dir, args.top, args.watch, args.watch_timeout,
                          args.max_cycles, trace if args.trace_rpc else None, on_event)
        print_summary(state, args.top)
        print(f"Done in {time.perf_counter() - t0:.1f}s")
        return 0 if state.get("status") == "completed" else 1

    if args.cmd == "tools":
        with MultiMCPClient.from_config(args.servers) as mcp:
            print(json.dumps({"connected": mcp.discovery_report(), "skipped": mcp.skipped}, indent=2))
        return 0

    if args.cmd == "graph":
        with MultiMCPClient([]) as mcp:  # structure only; no servers needed
            print(build_matching_graph(mcp).get_graph().draw_mermaid())
        return 0

    if args.cmd == "ask":
        llm = get_llm()
        if llm is None:
            print("Set LLM_MODEL (e.g. 'openai:gpt-4o-mini' or 'google_genai:gemini-2.0-flash') and the "
                  "provider's API key to use ask mode.", file=sys.stderr)
            return 2
        from langchain_core.messages import HumanMessage

        with MultiMCPClient.from_config(args.servers) as mcp:
            result = build_tool_calling_agent(mcp, llm).invoke({"messages": [HumanMessage(args.question)]})
            print(result["messages"][-1].content)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
