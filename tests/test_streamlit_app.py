"""Scenario group 8: the Streamlit UI drives the same agent over MCP (headless AppTest)."""

from __future__ import annotations

import pytest

from conftest import ROOT

AppTest = pytest.importorskip("streamlit.testing.v1", reason="streamlit not installed").AppTest


def test_dashboard_runs_agent_and_inspector(servers_config, data_dir, monkeypatch):
    monkeypatch.setenv("MATCHER_SERVERS_CONFIG", str(servers_config))
    monkeypatch.setenv("MATCHER_DATA_DIR", str(data_dir))
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
    at.run()
    assert not at.exception
    assert [t.label for t in at.tabs] == ["🎯 Match", "📄 Resumes", "🔌 MCP Inspector", "🕘 History"]

    next(b for b in at.button if "Run matching agent" in b.label).click().run()
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Candidates ranked"] == "5" and metrics["Files skipped"] == "1"
    assert (data_dir / "output" / "match_report_JOB-001.md").exists()  # written through MCP write_file

    next(b for b in at.button if b.label == "tools/call").click().run()  # MCP Inspector playground
    assert not at.exception
    assert any("tools/call" in str(j.value) for j in at.json)  # request/response rendered
