"""Scenario group 5 — the refactored LangGraph agent driving MCP servers end to end."""

from __future__ import annotations

import builtins
import io
import pathlib
import shutil
import threading
import time

import pytest

from matching_agent import build_matching_graph, run_match
from mcp_client import MultiMCPClient


def initial(job_id: str, **kw) -> dict:
    return {"job_id": job_id, "resume_dir": "resumes", "top_k": 5, "watch": False, "watch_timeout": 3,
            "max_watch_cycles": 1, "errors": [], "steps": [], **kw}


def test_full_run_ranks_and_publishes(servers_config, data_dir):
    state = run_match("JOB-001", str(servers_config))
    assert state["status"] == "completed"
    assert [c["candidate"] for c in state["ranked"][:2]] == ["Priya Sharma", "Rahul Verma"]
    assert state["ranked"][1]["missing_skills"] == ["Kafka"]
    assert [e["file"] for e in state["errors"]] == ["resumes/broken_upload.pdf"]
    report = (data_dir / state["report_path"]).read_text()
    assert "Priya Sharma" in report and "broken_upload.pdf" in report
    assert state["job_source"] == "jobs_db.get_job" and len(state["run_ids"]) == 1
    visited = [s["node"] for s in state["steps"]]
    assert visited == ["connect_and_discover", "load_job", "discover_resumes", "extract_resumes",
                       "analyze_candidates", "score_and_rank", "explain_matches", "publish_results", "finalize"]


def test_results_persisted_in_second_mcp_server(servers_config):
    run_match("JOB-002", str(servers_config))
    with MultiMCPClient.from_config(servers_config) as mcp:
        top = mcp.call("get_top_candidates", {"job_id": "JOB-002", "limit": 1})
        history = mcp.call("get_match_history", {"job_id": "JOB-002"})
    assert top["candidates"][0]["candidate"] == "Arjun Reddy"
    assert history["count"] == 1


def test_works_with_filesystem_server_only(fs_only_config):
    state = run_match("JOB-003", str(fs_only_config))
    assert state["status"] == "completed" and state["job_source"] == "filesystem.read_file"
    assert state["ranked"][0]["candidate"] == "Meera Iyer" and state["run_ids"] == []


def test_unknown_job_fails_cleanly(servers_config):
    state = run_match("JOB-404", str(servers_config))
    assert state["status"] == "failed"
    assert "Unknown job_id" in state["errors"][0]["error"]
    assert [s["node"] for s in state["steps"]][-1] == "finalize"


def test_watch_mode_picks_up_new_resume(servers_config, data_dir):
    def drop_resume():
        time.sleep(2.0)
        shutil.copy(data_dir / "incoming/karthik_nair.txt", data_dir / "resumes/karthik_nair.txt")

    threading.Thread(target=drop_resume, daemon=True).start()
    state = run_match("JOB-001", str(servers_config), watch=True, watch_timeout=8, max_cycles=1)
    names = [c["candidate"] for c in state["ranked"]]
    assert "Karthik Nair" in names and names.index("Karthik Nair") <= 1
    nodes = [s["node"] for s in state["steps"]]
    assert nodes.count("extract_resumes") == 2 and "wait_for_new_resumes" in nodes
    assert len(state["run_ids"]) == 2  # one result set per cycle


def test_agent_does_no_direct_file_io(servers_config, monkeypatch):
    """Proof of the refactor: with open()/Path I/O disabled in the agent process, the run still succeeds,
    because every byte of resume data travels over MCP."""
    with MultiMCPClient.from_config(servers_config) as mcp:
        graph = build_matching_graph(mcp, on_event=lambda *_: None)

        def forbidden(*a, **k):
            raise AssertionError(f"agent touched the file system directly: {a[:1]}")

        monkeypatch.setattr(builtins, "open", forbidden)
        monkeypatch.setattr(io, "open", forbidden)
        for name in ("open", "read_text", "read_bytes", "write_text", "write_bytes", "iterdir", "glob"):
            monkeypatch.setattr(pathlib.Path, name, forbidden)
        state = graph.invoke(initial("JOB-001"))
    assert state["status"] == "completed" and len(state["ranked"]) == 5


def test_required_server_missing_raises(tmp_path):
    cfg = tmp_path / "bad.json"
    cfg.write_text('{"mcpServers": {"filesystem": {"command": "definitely-not-a-binary", "required": true}}}')
    with pytest.raises(RuntimeError, match="Required MCP server 'filesystem'"):
        MultiMCPClient.from_config(cfg).connect()
