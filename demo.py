#!/usr/bin/env python3
"""
demo.py — scripted walkthrough for the 5–6 minute demo video.

    python demo.py            # run straight through (~1 min of output)
    python demo.py --pause    # wait for Enter between sections (use this while recording)
    python demo.py --pause --upto 5   # stop cleanly after section 5 (then switch to the dashboard)

Sections
  1. Raw JSON-RPC handshake with the filesystem MCP server
  2. Capability & resource discovery
  3. Error handling (protocol errors vs tool errors)
  4. batch_process() with live progress notifications
  5. watch_directory() catching a new resume + server push notifications
  6. Agent end-to-end: LangGraph + two MCP servers + watch mode
  7. Reading the persisted results back from the jobs-db MCP server
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from mcp_client import MCPClient, MCPClientError, MultiMCPClient
from matching_agent import print_summary, run_match

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PAUSE = False
UPTO = 7
BOLD, DIM, CYAN, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[36m", "\033[32m", "\033[33m", "\033[0m"


def section(n: int, title: str) -> None:
    if n > UPTO:
        print(f"\n{BOLD}{GREEN}Done with sections 1-{UPTO}.{RESET}")
        sys.exit(0)
    if PAUSE:
        input(f"\n{DIM}— press Enter for section {n} —{RESET}")
    print(f"\n{BOLD}{CYAN}━━ {n}. {title} {'━' * max(3, 60 - len(title))}{RESET}")


def show(label: str, obj, limit: int = 900) -> None:
    text = obj if isinstance(obj, str) else json.dumps(obj, indent=2)
    print(f"{YELLOW}{label}{RESET}\n{text[:limit]}{' …' if len(text) > limit else ''}")


def trace(server: str, direction: str, msg: dict) -> None:
    text = json.dumps(msg)
    colour = GREEN if direction == "←" else CYAN
    print(f"  {colour}{direction} {server}{RESET} {DIM}{text[:150]}{'…' if len(text) > 150 else ''}{RESET}")


def reset_demo_state() -> None:
    if not (DATA / "resumes").exists():
        sys.path.insert(0, str(ROOT / "scripts"))
        import generate_sample_data

        generate_sample_data.generate(DATA)
    (DATA / "resumes" / "karthik_nair.txt").unlink(missing_ok=True)
    # Clear match history but keep the DB file, so an already-running dashboard keeps working.
    db = DATA / "jobs.db"
    if db.exists():
        import sqlite3

        with sqlite3.connect(db) as conn:
            conn.execute("DELETE FROM match_results")
            conn.execute("DELETE FROM match_runs")
    for f in (DATA / "output").glob("match_report_*.md"):
        f.unlink()


def main() -> None:
    global PAUSE, UPTO
    ap = argparse.ArgumentParser()
    ap.add_argument("--pause", action="store_true", help="wait for Enter between sections")
    ap.add_argument("--upto", type=int, default=7, help="stop after this section (1-7)")
    opts = ap.parse_args()
    PAUSE, UPTO = opts.pause, opts.upto
    if os.name == "nt":
        os.system("")  # turn on ANSI colour codes in the classic Windows console
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    reset_demo_state()
    server_cmd = [str(ROOT / "filesystem_mcp_server.py"), "--log-level", "WARNING"]

    # 1 ------------------------------------------------------------------
    section(1, "JSON-RPC 2.0 handshake (initialize → initialized)")
    fs = MCPClient("filesystem", sys.executable, server_cmd, env={"FS_MCP_WATCH_POLL_INTERVAL": "0.5"}, trace=trace)
    fs.connect()
    show("negotiated:", {"protocolVersion": fs.protocol_version, "serverInfo": fs.server_info,
                         "capabilities": fs.capabilities})
    fs.trace = None  # quieter from here on

    # 2 ------------------------------------------------------------------
    section(2, "Discovery: tools, resources, resource templates")
    for t in fs.list_tools():
        print(f"  tool  {t['name']:<18} {DIM}{t['description'][:70]}…{RESET}")
    for r in fs.list_resources():
        print(f"  res   {r['uri']:<30} {DIM}{r.get('mimeType', '')}{RESET}")
    for t in fs.list_resource_templates():
        print(f"  tmpl  {t['uriTemplate']:<30} {DIM}{t.get('description', '')}{RESET}")
    show("\nresources/read resume://rahul_verma.pdf  (PDF → text, server-side):",
         fs.read_resource("resume://rahul_verma.pdf"), 260)

    # 3 ------------------------------------------------------------------
    section(3, "Error handling: protocol errors vs tool errors")
    fs.trace = trace
    for method, params in [("tools/call", {"name": "delete_everything", "arguments": {}}),
                           ("resources/read", {"uri": "resume://ghost.pdf"}),
                           ("does/not/exist", {})]:
        try:
            fs.request(method, params)
        except MCPClientError as e:
            print(f"  {BOLD}→ JSON-RPC error {e.code}{RESET}: {e.message}")
    raw = fs.call_tool_raw("read_file", {"path": "../../etc/passwd"})
    print(f"  {BOLD}→ tool result isError={raw['isError']}{RESET}: {raw['structuredContent']}")
    fs.trace = None

    # 4 ------------------------------------------------------------------
    section(4, "batch_process(): 6 resumes, 4 workers, one corrupt file")
    res = fs.call_tool("batch_process", {"directory": "resumes", "operation": "word_count"},
                       on_progress=lambda p: print(f"  {DIM}notifications/progress {p['progress']:g}/{p['total']:g} "
                                                   f"{p.get('message', '')}{RESET}"))
    for r in res["results"]:
        mark = f"{GREEN}ok {RESET}" if r["status"] == "ok" else f"{YELLOW}ERR{RESET}"
        detail = f"{r['data']['words']} words" if r["status"] == "ok" else r["error"]
        print(f"  {mark} {r['path']:<28} {detail}")
    print(f"  total={res['total']} succeeded={res['succeeded']} failed={res['failed']} "
          f"in {res['duration_ms']} ms")

    # 5 ------------------------------------------------------------------
    section(5, "watch_directory(): a new resume arrives")
    pushed = []
    fs.on_notification(lambda m, p: pushed.append(m) if m.startswith("notifications/resources") else None)
    w = fs.call_tool("watch_directory", {"timeout_seconds": 0})
    fs.subscribe(f"watch://{w['watch_id']}")
    print(f"  watch {w['watch_id']} started on {w['directory']}; copying karthik_nair.txt in 2s …")
    threading.Timer(2, lambda: shutil.copy(DATA / "incoming/karthik_nair.txt", DATA / "resumes")).start()
    t0 = time.time()
    got = fs.call_tool("watch_directory", {"watch_id": w["watch_id"], "timeout_seconds": 15})
    print(f"  returned after {time.time() - t0:.1f}s → new_files={got['new_files']}")
    time.sleep(0.3)
    print(f"  server pushed: {sorted(set(pushed))}")
    fs.call_tool("stop_watch", {"watch_id": w["watch_id"]})
    (DATA / "resumes" / "karthik_nair.txt").unlink()
    fs.close()

    # 6 ------------------------------------------------------------------
    section(6, "Agent: LangGraph workflow over two MCP servers (+ watch mode)")
    print(f"  {DIM}Karthik's resume will be dropped in 4s, while the agent is watching.{RESET}\n")
    threading.Timer(4, lambda: shutil.copy(DATA / "incoming/karthik_nair.txt", DATA / "resumes")).start()
    state = run_match("JOB-001", str(ROOT / "mcp_servers.json"), watch=True, watch_timeout=10, max_cycles=1,
                      on_event=lambda node, msg: print(f"  [{node:<20}] {msg}"))
    print_summary(state, 5)

    # 7 ------------------------------------------------------------------
    section(7, "Results persisted in the jobs-db MCP server")
    with MultiMCPClient.from_config(ROOT / "mcp_servers.json") as mcp:
        top = mcp.call("get_top_candidates", {"job_id": "JOB-001", "limit": 3})
        for c in top["candidates"]:
            print(f"  #{c['rank']} {c['candidate']:<14} {c['score']:>5}  {DIM}{c['explanation'][:80]}…{RESET}")
        print(f"  match runs stored: {mcp.call('get_match_history', {'job_id': 'JOB-001'})['count']}")
    print(f"\n{BOLD}{GREEN}Demo complete.{RESET} Report: data/{state['report_path']}")


if __name__ == "__main__":
    main()
