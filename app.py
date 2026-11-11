"""
app.py: Streamlit dashboard for the MCP resume-matching agent.

    streamlit run app.py

The UI is just another MCP client. It reuses the same MultiMCPClient and
LangGraph workflow as the CLI, so every resume, job and result shown here
travels over JSON-RPC to the MCP servers.

The one exception is the "Upload resume" box. It drops a file into the watched
folder, playing the part of an external upload portal, so watch mode has
something to detect.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from matching_agent import build_matching_graph, get_llm  # noqa: E402
from mcp_client import MCPClientError, MCPToolError, MultiMCPClient  # noqa: E402

SERVERS_CONFIG = Path(os.environ.get("MATCHER_SERVERS_CONFIG", ROOT / "mcp_servers.json"))
DATA = Path(os.environ.get("MATCHER_DATA_DIR", ROOT / "data"))
RESUME_TYPES = ["pdf", "docx", "txt", "md"]
TIER_COLOURS = {"Strong": "green", "Good": "blue", "Partial": "orange", "Weak": "gray"}

st.set_page_config(page_title="Resume Matcher · MCP", page_icon="🧩", layout="wide")


# --------------------------------------------------------------------------- #
# MCP connection (shared, long-lived) + JSON-RPC traffic recorder
# --------------------------------------------------------------------------- #


class Traffic:
    """Thread-safe ring buffer of every JSON-RPC message, for the inspector tab."""

    def __init__(self, size: int = 400):
        self.items: deque[dict] = deque(maxlen=size)
        self.count = 0

    def __call__(self, server: str, direction: str, message: dict) -> None:
        self.count += 1
        self.items.append({"n": self.count, "time": datetime.now().strftime("%H:%M:%S.%f")[:-3],
                           "server": server, "dir": direction, "message": message})


@st.cache_resource(show_spinner="Starting MCP servers…")
def get_mcp() -> tuple[MultiMCPClient, Traffic]:
    if not (DATA / "resumes").exists():
        sys.path.insert(0, str(ROOT / "scripts"))
        import generate_sample_data

        generate_sample_data.generate(DATA)
    traffic = Traffic()
    mcp = MultiMCPClient.from_config(SERVERS_CONFIG, trace=traffic).connect()
    return mcp, traffic


def mcp_or_reconnect() -> tuple[MultiMCPClient, Traffic]:
    mcp, traffic = get_mcp()
    if not mcp.clients or not all(c.alive for c in mcp.clients.values()):
        get_mcp.clear()
        mcp, traffic = get_mcp()
    return mcp, traffic


def safe_call(mcp: MultiMCPClient, tool: str, args: dict | None = None, default=None):
    try:
        return mcp.call(tool, args or {})
    except (MCPToolError, MCPClientError) as exc:
        st.error(str(exc))
        return default


mcp, traffic = mcp_or_reconnect()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def load_jobs() -> list[dict]:
    if mcp.has_tool("list_jobs"):
        return safe_call(mcp, "list_jobs", default={"jobs": []})["jobs"]
    listing = safe_call(mcp, "list_files", {"directory": "jobs", "pattern": "*.json"}, {"files": []})
    jobs = []
    for f in listing["files"]:
        data = safe_call(mcp, "read_file", {"path": f["path"]})
        if data:
            jobs.append(json.loads(data["content"]))
    return jobs


def get_job(job_id: str) -> dict | None:
    if mcp.has_tool("get_job"):
        return safe_call(mcp, "get_job", {"job_id": job_id})
    data = safe_call(mcp, "read_file", {"path": f"jobs/{job_id}.json"})
    return json.loads(data["content"]) if data else None


def chips(skills: list[str], colour: str) -> str:
    return " ".join(f":{colour}-background[{s}]" for s in skills) or "—"


COMPONENTS = [  # (label, weight, colour) in stacking order, darkest = most important
    ("Required skills", 50, "#1d4ed8"),
    ("Preferred skills", 20, "#60a5fa"),
    ("Text similarity", 20, "#0d9488"),
    ("Experience", 10, "#99f6e4"),
]


def score_breakdown_chart(ranked: list[dict]):
    import altair as alt

    rows = []
    for c in ranked:
        b = c["breakdown"]
        values = [b["required_coverage"], b["preferred_coverage"], min(1.0, b["text_similarity"] / 0.6),
                  b["experience_fit"]]
        for i, ((label, weight, _), v) in enumerate(zip(COMPONENTS, values)):
            rows.append({"Candidate": f"#{c['rank']} {c['candidate']}", "Component": f"{label} ({weight})",
                         "Points": round(weight * v, 1), "order": i})
    df = pd.DataFrame(rows)
    order = [f"#{c['rank']} {c['candidate']}" for c in ranked]
    return (
        alt.Chart(df)
        .mark_bar(size=20, cornerRadiusEnd=2)
        .encode(
            x=alt.X("sum(Points):Q", title=None, scale=alt.Scale(domain=[0, 100])),
            y=alt.Y("Candidate:N", sort=order, title=None, axis=alt.Axis(labelLimit=180)),
            color=alt.Color("Component:N", sort=[f"{l} ({w})" for l, w, _ in COMPONENTS],
                            scale=alt.Scale(domain=[f"{l} ({w})" for l, w, _ in COMPONENTS],
                                            range=[col for _, _, col in COMPONENTS]),
                            legend=alt.Legend(orient="bottom", title=None, columns=2, labelLimit=200)),
            order=alt.Order("order:Q"),
            tooltip=["Candidate", "Component", "Points"],
        )
        .properties(height=alt.Step(32))
    )


def workflow_dot(graph, visited: list[str]) -> str:
    """Graphviz DOT of the LangGraph workflow with the path actually taken highlighted."""
    g = graph.get_graph()
    path = ["__start__", *visited, "__end__"] if visited else []
    taken = set(zip(path, path[1:]))
    seen = set(path)
    lines = ['digraph G {', 'rankdir=TB; bgcolor="transparent";',
             'node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11, '
             'fillcolor="#f3f4f6", color="#d1d5db", fontcolor="#374151"];',
             'edge [color="#c7cbd1", fontname="Helvetica", fontsize=9];']
    for node in g.nodes:
        label = {"__start__": "start", "__end__": "end"}.get(node, node)
        shape = ', shape=ellipse' if node.startswith("__") else ""
        if node in seen:
            lines.append(f'"{node}" [label="{label}"{shape}, fillcolor="#dbeafe", color="#2563eb", '
                         f'fontcolor="#1e3a8a", penwidth=1.6];')
        else:
            lines.append(f'"{node}" [label="{label}"{shape}];')
    for e in g.edges:
        style = "dashed" if e.conditional else "solid"
        if (e.source, e.target) in taken:
            lines.append(f'"{e.source}" -> "{e.target}" [style={style}, color="#2563eb", penwidth=2.2];')
        else:
            lines.append(f'"{e.source}" -> "{e.target}" [style={style}];')
    lines.append("}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Sidebar: server status + settings
# --------------------------------------------------------------------------- #

with st.sidebar:
    st.title("🧩 Resume Matcher")
    st.caption("LangGraph agent · Model Context Protocol")
    st.subheader("MCP servers")
    for name, client in mcp.clients.items():
        info = client.server_info
        st.markdown(f":green[●] **{name}**  \n"
                    f"<small>{info.get('name')} v{info.get('version')} · protocol {client.protocol_version} · "
                    f"{len(mcp.tools[name])} tools</small>", unsafe_allow_html=True)
    for name, reason in mcp.skipped.items():
        st.markdown(f":gray[○] **{name}**  \n<small>skipped: {reason}</small>", unsafe_allow_html=True)
    if st.button("Reconnect servers", width="stretch"):
        mcp.close()
        get_mcp.clear()
        st.rerun()

    st.subheader("Settings")
    top_k = st.slider("Shortlist size", 1, 10, 5)
    llm = get_llm()
    st.caption(f"Explanations: **{'LLM' if llm else 'rule-based'}**"
               + ("" if llm else " (set `LLM_MODEL` to use an LLM)"))
    st.caption(f"JSON-RPC messages so far: **{traffic.count}**")


# --------------------------------------------------------------------------- #
# Tabs
# --------------------------------------------------------------------------- #

tab_match, tab_resumes, tab_inspect, tab_history = st.tabs(
    ["🎯 Match", "📄 Resumes", "🔌 MCP Inspector", "🕘 History"])

# =============================== MATCH =============================== #

with tab_match:
    jobs = load_jobs()
    if not jobs:
        st.warning("No job postings found.")
        st.stop()
    left, right = st.columns([3, 2], gap="large")
    with left:
        job_id = st.selectbox("Job posting", [j["job_id"] for j in jobs],
                              format_func=lambda jid: f"{jid} · {next(j['title'] for j in jobs if j['job_id'] == jid)}")
        job = get_job(job_id) or {}
        with st.container(border=True):
            st.markdown(f"**{job.get('title', '')}** · {job.get('company', '')} · {job.get('location', '')} · "
                        f"{job.get('min_years', 0)}+ yrs")
            st.markdown("Required: " + chips(job.get("required_skills", []), "blue"))
            st.markdown("Preferred: " + chips(job.get("preferred_skills", []), "violet"))
            st.caption(job.get("description", ""))
    with right:
        watch = st.toggle("Watch mode: keep waiting for new resumes", value=False)
        watch_timeout = st.slider("Wait per cycle (s)", 3, 60, 10, disabled=not watch)
        max_cycles = st.slider("Watch cycles", 1, 5, 1, disabled=not watch)
        simulate = st.checkbox("Simulate an upload (karthik_nair.txt) 4 s into the run", value=watch,
                               disabled=not watch)
        run = st.button("▶ Run matching agent", type="primary", width="stretch")

    if run:
        events: list[tuple[str, str]] = []
        main_thread = threading.get_ident()
        traffic_before = traffic.count
        if watch and simulate:
            (DATA / "resumes" / "karthik_nair.txt").unlink(missing_ok=True)
            threading.Timer(4, lambda: shutil.copy(DATA / "incoming" / "karthik_nair.txt",
                                                   DATA / "resumes")).start()
        t0 = time.perf_counter()
        with st.status("Running the LangGraph agent over MCP…", expanded=True) as status:
            def on_event(node: str, msg: str) -> None:
                events.append((node, msg))
                # progress notifications arrive on the MCP reader thread; only the script thread may draw
                if threading.get_ident() == main_thread and not msg.strip().startswith("progress"):
                    st.markdown(f"`{node}` {msg}")

            graph = build_matching_graph(mcp, llm=llm, on_event=on_event)
            state = graph.invoke({"job_id": job_id, "resume_dir": "resumes", "top_k": top_k, "watch": watch,
                                  "watch_timeout": float(watch_timeout), "max_watch_cycles": max_cycles,
                                  "errors": [], "steps": []}, {"recursion_limit": 100})
            elapsed = time.perf_counter() - t0
            ok = state.get("status") == "completed"
            status.update(label=f"{'Completed' if ok else 'Failed'} in {elapsed:.1f}s · "
                                f"{traffic.count - traffic_before} JSON-RPC messages",
                          state="complete" if ok else "error", expanded=False)
        st.session_state["result"] = {"state": state, "graph": graph, "elapsed": elapsed,
                                      "messages": traffic.count - traffic_before}

    result = st.session_state.get("result")
    if result and result["state"].get("job", {}).get("job_id") == job_id:
        state = result["state"]
        if state.get("status") == "failed":
            st.error(" · ".join(e["error"] for e in state.get("errors", [])))
        else:
            ranked = state["ranked"]
            skipped = [e for e in state.get("errors", []) if "file" in e]
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Candidates ranked", len(ranked))
            m2.metric("Top score", f"{ranked[0]['score']:.1f}" if ranked else "—")
            m3.metric("Strong matches", sum(c["tier"] == "Strong" for c in ranked))
            m4.metric("Files skipped", len(skipped))
            m5.metric("Watch cycles", state.get("watch_cycles", 0))

            df = pd.DataFrame([{
                "Rank": c["rank"], "Candidate": c["candidate"], "Score": c["score"], "Tier": c["tier"],
                "Years": c["years_experience"], "Missing required": ", ".join(c["missing_skills"]) or "—",
                "Resume": c["resume_path"].split("/")[-1],
            } for c in ranked])
            st.dataframe(df, hide_index=True, width="stretch", column_config={
                "Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%.1f"),
                "Years": st.column_config.NumberColumn("Years", format="%g"),
            })
            if skipped:
                st.warning("Skipped: " + " · ".join(f"`{e['file']}` ({e['error']})" for e in skipped))

            chart_col, graph_col = st.columns([3, 2], gap="large")
            with chart_col:
                st.markdown("**Score breakdown** (points out of 100)")
                st.altair_chart(score_breakdown_chart(ranked), width="stretch")
                st.markdown(f"**Top {min(top_k, len(ranked))} shortlist**")
                for c in ranked[:top_k]:
                    colour = TIER_COLOURS[c["tier"]]
                    with st.expander(f"#{c['rank']}  {c['candidate']}  ·  {c['score']}  ·  {c['tier']}"):
                        st.markdown(f":{colour}[**{c['tier']} match**] · {c['headline']}")
                        st.write(c["explanation"])
                        st.markdown("Matched: " + chips(c["matched_skills"], "green"))
                        st.markdown("Missing: " + chips(c["missing_skills"], "red"))
                        st.caption(f"Resume: `{c['resume_path']}` · {c.get('email') or ''}")
            with graph_col:
                st.markdown("**Path taken through the workflow**")
                st.graphviz_chart(workflow_dot(result["graph"], [s["node"] for s in state["steps"]]),
                                  width="stretch")

            report = safe_call(mcp, "read_file", {"path": state["report_path"]})
            d1, d2 = st.columns(2)
            if report:
                d1.download_button("⬇ Download Markdown report", report["content"],
                                   file_name=Path(state["report_path"]).name, width="stretch")
            if state.get("run_ids"):
                d2.info(f"Saved to jobs-db MCP server · runs {', '.join(state['run_ids'])}")

# =============================== RESUMES =============================== #

with tab_resumes:
    fs = mcp.server("filesystem")
    resumes = [r for r in fs.list_resources() if r["uri"].startswith("resume://")]
    left, right = st.columns([2, 3], gap="large")
    with left:
        st.markdown(f"**{len(resumes)} resumes** (from `resources/list`)")
        st.dataframe(pd.DataFrame([{"File": r["name"], "Type": r["name"].rsplit(".", 1)[-1].upper(),
                                    "KB": round(r.get("size", 0) / 1024, 1)} for r in resumes]),
                     hide_index=True, width="stretch")
        uploaded = st.file_uploader("Upload resume into the watched folder", type=RESUME_TYPES,
                                    accept_multiple_files=True,
                                    help="Plays the part of an external upload portal. The agent itself only "
                                         "reads files through MCP.")
        if uploaded:
            for f in uploaded:
                (DATA / "resumes" / Path(f.name).name).write_bytes(f.getvalue())
            st.success(f"Added {len(uploaded)} file(s). Run the agent, or turn on watch mode first to see "
                       f"them picked up live.")
        if st.button("Reset sample data", help="Removes uploaded resumes and restores the original six"):
            sys.path.insert(0, str(ROOT / "scripts"))
            import generate_sample_data

            keep = set(generate_sample_data.RESUMES) | set(generate_sample_data.CORRUPT)
            for p in (DATA / "resumes").iterdir():
                if p.name not in keep:
                    p.unlink()
            st.rerun()
    with right:
        if resumes:
            choice = st.selectbox("Preview", [r["uri"] for r in resumes])
            try:
                text = fs.read_resource(choice)
                st.caption(f"`resources/read {choice}`: {len(text.split())} words, extracted server-side")
                st.code(text, language=None, wrap_lines=True)
            except MCPClientError as exc:
                st.error(f"JSON-RPC error {exc.code}: {exc.message}")
                if exc.data:
                    st.json(exc.data)

# =============================== INSPECTOR =============================== #

EXAMPLES = {
    "list_files": {"directory": "resumes"},
    "read_file": {"path": "resumes/priya_sharma.txt", "max_chars": 300},
    "write_file": {"path": "output/inspector_note.md", "content": "Hello from the MCP inspector"},
    "search_files": {"query": "kafka", "directory": "resumes"},
    "get_file_metadata": {"path": "resumes/rahul_verma.pdf"},
    "watch_directory": {"timeout_seconds": 0, "include_existing": True},
    "stop_watch": {"watch_id": "w-xxxxxxxx"},
    "list_watches": {},
    "batch_process": {"directory": "resumes", "operation": "word_count"},
    "list_jobs": {},
    "get_job": {"job_id": "JOB-001"},
    "add_job": {"job_id": "JOB-004", "title": "Platform Engineer", "required_skills": ["Kubernetes", "AWS"]},
    "save_match_results": {"job_id": "JOB-001", "results": [{"candidate": "Test", "score": 50}]},
    "get_match_history": {"limit": 5},
    "get_top_candidates": {"job_id": "JOB-001", "limit": 3},
}

with tab_inspect:
    server_name = st.selectbox("Server", list(mcp.clients))
    client = mcp.server(server_name)
    c1, c2 = st.columns([1, 2], gap="large")
    with c1:
        with st.container(border=True):
            st.markdown("**initialize → result**")
            st.json({"protocolVersion": client.protocol_version, "serverInfo": client.server_info,
                     "capabilities": client.capabilities}, expanded=1)
            if client.instructions:
                st.caption(client.instructions)
            st.caption(f"ping: {client.ping():.1f} ms")
        st.markdown("**Resources & templates**")
        res = client.list_resources()
        tmpl = client.list_resource_templates()
        st.dataframe(pd.DataFrame([{"URI": r["uri"], "MIME": r.get("mimeType", "")} for r in res]
                                  + [{"URI": t["uriTemplate"], "MIME": "template"} for t in tmpl]),
                     hide_index=True, width="stretch", height=250)
        uri = st.text_input("resources/read", value=res[0]["uri"] if res else "")
        if st.button("Read resource"):
            try:
                st.code(client.read_resource(uri)[:3000], language=None, wrap_lines=True)
            except MCPClientError as exc:
                st.error(f"JSON-RPC error {exc.code}: {exc.message}")
    with c2:
        tools = mcp.tools[server_name]
        tool_name = st.selectbox("Tool", list(tools))
        tool = tools[tool_name]
        st.caption(tool.get("description", ""))
        with st.expander("inputSchema"):
            st.json(tool.get("inputSchema", {}))
        args_text = st.text_area("arguments (JSON)", json.dumps(EXAMPLES.get(tool_name, {}), indent=2),
                                 height=140, key=f"args-{server_name}-{tool_name}")
        if st.button("tools/call", type="primary"):
            try:
                args = json.loads(args_text or "{}")
            except json.JSONDecodeError as exc:
                st.error(f"Arguments are not valid JSON: {exc}")
            else:
                req_col, res_col = st.columns(2)
                req_col.markdown("**→ request**")
                req_col.json({"jsonrpc": "2.0", "method": "tools/call",
                              "params": {"name": tool_name, "arguments": args}})
                res_col.markdown("**← response**")
                try:
                    raw = client.call_tool_raw(tool_name, args, timeout=90)
                    if raw.get("isError"):
                        res_col.error("Tool returned isError: true")
                    res_col.json(raw.get("structuredContent", raw), expanded=2)
                except MCPClientError as exc:
                    res_col.error(f"JSON-RPC error {exc.code}: {exc.message}")
                    if exc.data:
                        res_col.json(exc.data)

    st.divider()
    t1, t2 = st.columns([3, 1])
    t1.markdown(f"**JSON-RPC traffic** (latest {min(60, len(traffic.items))} of {traffic.count} messages)")
    only = t2.selectbox("Filter", ["all", *mcp.clients], label_visibility="collapsed")
    rows = [m for m in list(traffic.items) if only == "all" or m["server"] == only][-60:]
    st.code("\n".join(f"{m['time']}  {m['dir']} {m['server']:<10} {json.dumps(m['message'])[:180]}"
                      for m in reversed(rows)) or "no traffic yet", language=None)

# =============================== HISTORY =============================== #

with tab_history:
    if not mcp.has_tool("get_match_history"):
        st.info("Connect the jobs-db MCP server (see mcp_servers.json) to keep match history.")
    else:
        hist_job = st.selectbox("Job", [j["job_id"] for j in load_jobs()], key="hist-job")
        history = safe_call(mcp, "get_match_history", {"job_id": hist_job, "limit": 20}, {"runs": []})
        if not history["runs"]:
            st.info("No runs yet. Run the agent from the Match tab.")
        else:
            st.dataframe(pd.DataFrame([{
                "Run": r["run_id"], "When (UTC)": r["created_at"], "Candidates": r["candidates_evaluated"],
                "Watch cycle": r["metadata"].get("cycle", 0),
                "Skipped files": ", ".join(r["metadata"].get("skipped_files", [])) or "—",
            } for r in history["runs"]]), hide_index=True, width="stretch")
            top = safe_call(mcp, "get_top_candidates", {"job_id": hist_job, "limit": 10}, {"candidates": []})
            st.markdown(f"**Latest run `{top.get('run_id')}`** (via `get_top_candidates`)")
            st.dataframe(pd.DataFrame([{
                "Rank": c["rank"], "Candidate": c["candidate"], "Score": c["score"],
                "Missing": ", ".join(c["missing_skills"]) or "—", "Why": c["explanation"],
            } for c in top["candidates"]]), hide_index=True, width="stretch", column_config={
                "Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%.1f")})
