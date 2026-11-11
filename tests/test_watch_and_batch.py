"""Scenario group 3 — the new MCP capabilities: batch_process() and watch_directory()."""

from __future__ import annotations

import shutil
import threading
import time

from conftest import call, rpc

# ----------------------------- batch_process ----------------------------- #


def test_batch_isolates_failures(fs):
    server, session, _ = fs
    res = call(server, session, "batch_process", {"directory": "resumes"})["structuredContent"]
    assert (res["total"], res["succeeded"], res["failed"]) == (6, 5, 1)
    broken = next(r for r in res["results"] if r["path"].endswith("broken_upload.pdf"))
    assert broken["status"] == "error" and broken["error_code"] == "PARSE_ERROR"
    assert all("text" in r["data"] for r in res["results"] if r["status"] == "ok")


def test_batch_with_explicit_paths_including_bad_ones(fs):
    server, session, _ = fs
    res = call(server, session, "batch_process", {
        "paths": ["resumes/priya_sharma.txt", "resumes/ghost.pdf", "../outside.txt", "resumes/priya_sharma.txt"],
        "operation": "metadata"})["structuredContent"]
    assert res["total"] == 3  # duplicate removed
    statuses = {r["path"]: r["status"] for r in res["results"]}
    assert statuses == {"resumes/priya_sharma.txt": "ok", "resumes/ghost.pdf": "error", "../outside.txt": "error"}


def test_batch_search_and_validation(fs):
    server, session, _ = fs
    res = call(server, session, "batch_process", {"directory": "resumes", "operation": "search", "query": "AWS"})
    hits = {r["path"]: r["data"]["match_count"] for r in res["structuredContent"]["results"] if r["status"] == "ok"}
    assert hits["resumes/priya_sharma.txt"] >= 1 and hits["resumes/sneha_kapoor.txt"] == 0
    missing = call(server, session, "batch_process", {"directory": "resumes", "operation": "search"})
    assert missing["isError"]
    neither = call(server, session, "batch_process", {"operation": "metadata"})
    assert neither["structuredContent"]["error"] == "INVALID_ARGUMENTS"


def test_batch_sends_progress_notifications(fs):
    server, session, notes = fs
    call(server, session, "batch_process", {"directory": "resumes", "operation": "word_count"},
         meta={"progressToken": "tok-1"})
    progress = [n["params"] for n in notes if n["method"] == "notifications/progress"]
    assert len(progress) == 6
    assert all(p["progressToken"] == "tok-1" and p["total"] == 6 for p in progress)
    assert sorted(p["progress"] for p in progress) == [1, 2, 3, 4, 5, 6]


def test_batch_uses_text_cache(fs):
    server, session, _ = fs
    call(server, session, "batch_process", {"directory": "resumes"})
    second = call(server, session, "batch_process", {"directory": "resumes"})["structuredContent"]
    assert second["cache_hits"] == 5  # every parsable file served from cache


def test_batch_limits(make_fs_server):
    server, session, _ = make_fs_server(batch_max_files=2)
    res = call(server, session, "batch_process", {"directory": "resumes"})
    assert res["structuredContent"]["error"] == "BATCH_TOO_LARGE"


def test_batch_fail_fast(make_fs_server):
    server, session, _ = make_fs_server(batch_max_workers=1)
    res = call(server, session, "batch_process", {
        "paths": ["resumes/broken_upload.pdf", "resumes/priya_sharma.txt", "resumes/sneha_kapoor.txt"],
        "fail_fast": True})["structuredContent"]
    assert res["failed"] == 1 and res["skipped"] == 2


# ----------------------------- watch_directory ----------------------------- #


def test_watch_detects_new_modified_and_deleted(fs, data_dir):
    server, session, _ = fs
    start = call(server, session, "watch_directory", {"timeout_seconds": 0, "include_existing": True})
    s = start["structuredContent"]
    assert s["created_watch"] and s["new_files"] == [] and len(s["existing_files"]) == 6
    wid = s["watch_id"]

    shutil.copy(data_dir / "incoming/karthik_nair.txt", data_dir / "resumes/karthik_nair.txt")
    got = call(server, session, "watch_directory", {"watch_id": wid, "timeout_seconds": 5})["structuredContent"]
    assert got["new_files"] == ["resumes/karthik_nair.txt"] and got["waited_seconds"] < 5

    time.sleep(0.05)
    (data_dir / "resumes/karthik_nair.txt").write_text("Karthik Nair\nupdated", encoding="utf-8")
    got = call(server, session, "watch_directory", {"watch_id": wid, "timeout_seconds": 5})["structuredContent"]
    assert got["modified_files"] == ["resumes/karthik_nair.txt"]

    (data_dir / "resumes/karthik_nair.txt").unlink()
    got = call(server, session, "watch_directory", {"watch_id": wid, "timeout_seconds": 5})["structuredContent"]
    assert got["deleted_files"] == ["resumes/karthik_nair.txt"]


def test_watch_timeout_and_pattern_filter(fs, data_dir):
    server, session, _ = fs
    wid = call(server, session, "watch_directory", {"timeout_seconds": 0})["structuredContent"]["watch_id"]
    (data_dir / "resumes/notes.csv").write_text("not a resume")
    t0 = time.monotonic()
    got = call(server, session, "watch_directory", {"watch_id": wid, "timeout_seconds": 0.6})["structuredContent"]
    assert got["events"] == [] and 0.5 <= time.monotonic() - t0 < 3


def test_watch_pushes_notifications(fs, data_dir):
    server, session, notes = fs
    wid = call(server, session, "watch_directory", {"timeout_seconds": 0})["structuredContent"]["watch_id"]
    rpc(server, session, "resources/subscribe", {"uri": f"watch://{wid}"})
    shutil.copy(data_dir / "incoming/karthik_nair.txt", data_dir / "resumes/karthik_nair.txt")
    deadline = time.time() + 5
    while time.time() < deadline and not any(n["method"] == "notifications/resources/updated" for n in notes):
        time.sleep(0.05)
    methods = [n["method"] for n in notes]
    assert "notifications/resources/updated" in methods
    assert "notifications/resources/list_changed" in methods
    assert "notifications/message" in methods
    state = rpc(server, session, "resources/read", {"uri": f"watch://{wid}"})["result"]["contents"][0]["text"]
    assert "karthik_nair.txt" in state


def test_watch_errors_list_and_stop(make_fs_server):
    server, session, _ = make_fs_server(max_watches=1)
    unknown = call(server, session, "watch_directory", {"watch_id": "w-nope", "timeout_seconds": 0})
    assert unknown["structuredContent"]["error"] == "UNKNOWN_WATCH"
    wid = call(server, session, "watch_directory", {"timeout_seconds": 0})["structuredContent"]["watch_id"]
    assert call(server, session, "watch_directory", {"timeout_seconds": 0})["structuredContent"]["error"] == "WATCH_LIMIT"
    assert call(server, session, "list_watches")["structuredContent"]["count"] == 1
    assert call(server, session, "stop_watch", {"watch_id": wid})["structuredContent"]["stopped"] is True
    assert call(server, session, "list_watches")["structuredContent"]["count"] == 0


def test_watch_returns_early_when_cancelled(fs):
    server, session, _ = fs
    wid = call(server, session, "watch_directory", {"timeout_seconds": 0})["structuredContent"]["watch_id"]
    msg = {"jsonrpc": "2.0", "id": 4242, "method": "tools/call",
           "params": {"name": "watch_directory", "arguments": {"watch_id": wid, "timeout_seconds": 30}}}
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", server.handle_message(msg, session)))
    t0 = time.monotonic()
    t.start()
    time.sleep(0.3)
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/cancelled",
                           "params": {"requestId": 4242, "reason": "user aborted"}}, session)
    t.join(5)
    assert not t.is_alive() and time.monotonic() - t0 < 3
    assert out["r"] is None  # spec: no response for a cancelled request
