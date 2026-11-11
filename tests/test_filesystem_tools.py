"""Scenario group 2 — Milestone 1 tools served over MCP."""

from __future__ import annotations

from conftest import call


def test_list_files(fs):
    server, session, _ = fs
    res = call(server, session, "list_files", {"directory": "resumes", "pattern": "*.pdf"})
    assert res["isError"] is False
    names = [f["name"] for f in res["structuredContent"]["files"]]
    assert names == ["broken_upload.pdf", "rahul_verma.pdf"]


def test_read_file_extracts_pdf_and_docx(fs):
    server, session, _ = fs
    pdf = call(server, session, "read_file", {"path": "resumes/rahul_verma.pdf"})["structuredContent"]
    assert pdf["format"] == "pdf" and "Spring Boot" in pdf["content"]
    docx = call(server, session, "read_file", {"path": "resumes/meera_iyer.docx"})["structuredContent"]
    assert "Airflow" in docx["content"]


def test_read_file_truncation(fs):
    server, session, _ = fs
    res = call(server, session, "read_file", {"path": "resumes/priya_sharma.txt", "max_chars": 20})
    assert res["structuredContent"]["truncated"] is True
    assert len(res["structuredContent"]["content"]) == 20


def test_sandbox_blocks_path_traversal(fs):
    server, session, _ = fs
    for bad in ["../../etc/passwd", "/etc/passwd", "resumes/../../secret.txt"]:
        res = call(server, session, "read_file", {"path": bad})
        assert res["isError"] is True and res["structuredContent"]["error"] == "ACCESS_DENIED", bad


def test_write_file_modes(fs, data_dir):
    server, session, _ = fs
    ok = call(server, session, "write_file", {"path": "output/notes/a.md", "content": "hello"})
    assert ok["isError"] is False and (data_dir / "output/notes/a.md").read_text() == "hello"
    call(server, session, "write_file", {"path": "output/notes/a.md", "content": " world", "mode": "append"})
    assert (data_dir / "output/notes/a.md").read_text() == "hello world"
    dup = call(server, session, "write_file", {"path": "output/notes/a.md", "content": "x", "mode": "create"})
    assert dup["structuredContent"]["error"] == "ALREADY_EXISTS"
    exe = call(server, session, "write_file", {"path": "output/evil.sh", "content": "rm -rf /"})
    assert exe["structuredContent"]["error"] == "UNSUPPORTED_TYPE"


def test_write_disabled_by_config(make_fs_server):
    server, session, _ = make_fs_server(allow_write=False)
    res = call(server, session, "write_file", {"path": "output/x.md", "content": "x"})
    assert res["isError"] and res["structuredContent"]["error"] == "READ_ONLY"


def test_search_files_spans_formats(fs):
    server, session, _ = fs
    res = call(server, session, "search_files", {"query": "kafka", "directory": "resumes"})["structuredContent"]
    files = {m["path"] for m in res["matches"]}
    assert {"resumes/priya_sharma.txt", "resumes/meera_iyer.docx"} <= files
    assert any("broken_upload.pdf" in s["path"] for s in res["skipped"])


def test_get_file_metadata(fs):
    server, session, _ = fs
    meta = call(server, session, "get_file_metadata", {"path": "resumes/rahul_verma.pdf"})["structuredContent"]
    assert meta["pages"] == 1 and meta["is_resume"] is True and len(meta["sha256"]) == 64


def test_not_found_and_size_limit(make_fs_server):
    server, session, _ = make_fs_server(max_file_size_mb=0.0001)
    assert call(server, session, "read_file", {"path": "nope.txt"})["structuredContent"]["error"] == "NOT_FOUND"
    big = call(server, session, "read_file", {"path": "resumes/priya_sharma.txt"})
    assert big["structuredContent"]["error"] == "FILE_TOO_LARGE"
