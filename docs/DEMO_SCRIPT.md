# Demo video script (5–6 minutes)

**Setup before recording**
- Terminal font ≥ 16 pt, dark theme, window ~120 columns wide.
- Open `docs/diagrams.html` in a browser tab and `filesystem_mcp_server.py` in your editor.
- Run `python demo.py` once as a dry run. It resets its own state on every run.
- Record with `python demo.py --pause` so you control the pace with Enter (add `--upto 5` to stop after section 5 and switch to the dashboard).

| Time | Screen | What to say (paraphrase in your own words) |
|---|---|---|
| **0:00–0:30** | `diagrams.html`, component view | "In Milestone 3 the agent opened files itself. In Milestone 4 every file operation goes through MCP. The agent is now an MCP client talking JSON-RPC 2.0 to two servers: a filesystem server and, as the bonus, a SQLite jobs server. Both are built on a small protocol core I wrote against the MCP spec." |
| **0:30–1:00** | Editor: scroll `filesystem_mcp_server.py` | Point at `ServerConfig` (defaults → JSON file → env vars → CLI), the `FileSystem` sandbox, the five Milestone 1 tools, then `watch_directory` and `batch_process`. Mention the tool annotations (`readOnlyHint`, …). |
| **1:00–1:30** | `demo.py` §1 | "This is the raw handshake: `initialize` with protocol version 2025-11-25, the server's capabilities (tools, resources with subscribe and listChanged, logging), then `notifications/initialized`." |
| **1:30–2:00** | §2 | "Discovery: 9 tools, the resume resources with their `resume://` URIs, and three URI templates. Reading `resume://rahul_verma.pdf` returns plain text. PDF parsing happens on the server, so the agent never needs pypdf." |
| **2:00–2:30** | §3 | "Two kinds of error. Unknown tool, missing resource and unknown method are JSON-RPC errors: -32602, -32002, -32601. A path-traversal attempt is a *tool* error with `isError: true` and code ACCESS_DENIED, so an LLM can read it and correct itself." |
| **2:30–3:00** | §4 | "`batch_process` extracts six resumes on four worker threads and streams `notifications/progress`. One upload is a corrupt PDF: it fails on its own and the other five succeed." |
| **3:00–3:30** | §5 | "`watch_directory` starts a watch and returns a watch_id. I drop a new resume in. The blocking call returns with `new_files`, and the server also *pushed* `resources/list_changed` and `resources/updated`." |
| **3:30–4:45** | §6 (the agent) + `diagrams.html` state machine | "Now the LangGraph agent. It discovers both servers (the web-search server is configured but disabled) and loads JOB-001 from the jobs DB. It starts the watch *before* reading so nothing is missed, batch-extracts, scores, writes the report through `write_file` and saves the run through `save_match_results`. Then it waits. A new resume arrives, only that file is extracted, Karthik is re-ranked to #2, and a second run is saved." Switch to the state diagram and trace the loop `publish_results → wait_for_new_resumes → extract_resumes`. |
| **4:45–5:15** | §7, then open `data/output/match_report_JOB-001.md` | "The results are persisted in the second MCP server and read back with `get_top_candidates`. This is the Markdown report the agent wrote through MCP." |
| **5:15–5:50** | Terminal: `pytest -q`, then show `test_agent_does_no_direct_file_io` | "58 tests: protocol compliance, interop with the official MCP SDK, HTTP transport, watch and batch, and end-to-end agent runs. This test disables `open()` and all `Path` I/O inside the agent process and the run still passes. That proves all file access now goes through MCP." |
| **5:50–6:00** | README | "The same server plugs into Claude Desktop unchanged, and it runs in Docker over HTTP." |

**Optional extras if you have time**
- `python matching_agent.py match --job JOB-001 --trace-rpc` shows every JSON-RPC message the agent sends.
- `python filesystem_mcp_server.py --transport http` plus a `curl` to `/health`.

## Alternative: record with the dashboard (`streamlit run app.py`)

If a UI-led video looks better, swap the terminal sections for these. Keep §1 (handshake) from `demo.py`, since raw JSON-RPC is the point of the assignment.

| Time | Screen | Talking points |
|---|---|---|
| 0:00–0:40 | Diagrams + sidebar | Architecture; the sidebar shows both MCP servers connected (protocol 2025-11-25) and web-search configured but disabled |
| 0:40–1:20 | Terminal: `python demo.py` §1 and §3 only | Handshake, plus the protocol-error vs tool-error distinction |
| 1:20–2:10 | **MCP Inspector** tab | `initialize` result → resources and templates → call `batch_process`, then `read_file` with `../../etc/passwd` (isError) → scroll the live JSON-RPC log |
| 2:10–2:40 | **Resumes** tab | `resources/list`; preview a PDF and a DOCX extracted server-side; preview `broken_upload.pdf` to show the error |
| 2:40–4:20 | **Match** tab, watch mode ON + simulated upload | Live status lines per node; Karthik appears mid-run; ranked table, score breakdown, highlighted workflow path showing the watch loop |
| 4:20–4:50 | **History** tab | Two runs saved in the jobs-db MCP server (initial and after the upload) |
| 4:50–5:40 | Terminal: `pytest -q` | 59 tests; point at `test_agent_does_no_direct_file_io` |
