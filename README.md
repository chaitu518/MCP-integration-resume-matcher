# Resume Matching Agent: Milestone 4 (Model Context Protocol)

The Milestone 3 LangGraph matching agent, refactored so that **all file and data access goes through MCP servers**.
The Milestone 1 file-system tools now live in a standalone, spec-compliant MCP server that any MCP host can use
(this agent, Claude Desktop, or the official MCP SDK).

```
┌──────────────── matching_agent.py (LangGraph) ────────────────┐
│ StateGraph ─► matching_engine.py (pure scoring, no I/O)       │
│     └──────► MultiMCPClient (mcp_client.py)                   │
└───────────────┬───────────────────────────┬───────────────────┘
      JSON-RPC 2.0 / stdio         JSON-RPC 2.0 / stdio
                ▼                           ▼
  filesystem_mcp_server.py        jobs_db_mcp_server.py  (bonus)
  M1 tools + watch_directory      job postings + match history
  + batch_process + resources     (SQLite)
```

Diagrams: [docs/architecture.md](docs/architecture.md) (component view, LangGraph state machine, agent↔MCP sequence)
· Tests: [docs/test_scenarios.md](docs/test_scenarios.md) · Video: [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md)

## Quick start

```bash
pip install -r requirements.txt
python scripts/generate_sample_data.py      # sample resumes (PDF/DOCX/TXT/MD) + 3 job postings
python matching_agent.py match --job JOB-001
streamlit run app.py                        # dashboard UI → http://localhost:8501
python demo.py                              # the full scripted walkthrough used in the video
pytest -q                                   # 59 tests, ~15 s
```

### Windows (PowerShell)

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1   # finds Python 3.10+, creates .venv, installs, makes sample data
.\.venv\Scripts\python.exe matching_agent.py match --job JOB-001
.\.venv\Scripts\python.exe -m streamlit run app.py
.\.venv\Scripts\python.exe demo.py
.\.venv\Scripts\python.exe -m pytest -q
```

If `python` fails with *"The system cannot find the path specified"*, the `python` on your PATH is broken. It is usually
the Microsoft Store alias or a deleted install. Install Python 3.12 from python.org, then turn off *Settings → Apps →
Advanced app settings → App execution aliases → python.exe / python3.exe*, and open a new terminal.

Other commands:

```bash
python matching_agent.py match --job JOB-001 --watch --watch-timeout 30 --max-cycles 3   # keep watching for uploads
python matching_agent.py match --job JOB-002 --trace-rpc   # print every JSON-RPC message
python matching_agent.py tools                              # MCP discovery report for all servers
python matching_agent.py graph                              # Mermaid diagram of the LangGraph workflow
LLM_MODEL=openai:gpt-4o-mini python matching_agent.py ask "Which candidates know Kafka?"   # LLM + MCP tools
python filesystem_mcp_server.py --transport http            # http://127.0.0.1:8765/mcp  (+ /health)
```

## Deliverables → files

| Deliverable | Where |
|---|---|
| MCP filesystem server | [`filesystem_mcp_server.py`](filesystem_mcp_server.py) (+ protocol core [`mcp_protocol.py`](mcp_protocol.py)) |
| Refactored LangGraph agent with MCP client | [`matching_agent.py`](matching_agent.py), [`mcp_client.py`](mcp_client.py) |
| JSON-RPC 2.0 compliance + resource discovery | `mcp_protocol.py`; proven by `tests/test_jsonrpc_compliance.py`, `tests/test_resources_and_http.py` |
| `watch_directory()` and `batch_process()` | `filesystem_mcp_server.py`; `tests/test_watch_and_batch.py` |
| State machine / workflow diagram | [`docs/architecture.md`](docs/architecture.md), [`docs/diagrams.html`](docs/diagrams.html), [`docs/diagrams.png`](docs/diagrams.png) |
| Test scenarios | [`tests/`](tests), [`docs/test_scenarios.md`](docs/test_scenarios.md), [`docs/test_results.txt`](docs/test_results.txt) |
| Dashboard UI | [`app.py`](app.py) (Streamlit) |
| Demo video | Script: [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md), driver: [`demo.py`](demo.py) |
| Bonus: multi-MCP | [`jobs_db_mcp_server.py`](jobs_db_mcp_server.py), [`mcp_servers.json`](mcp_servers.json) (web-search slot included, disabled) |

## Dashboard (`streamlit run app.py`)

The UI is another MCP client. It reuses the same `MultiMCPClient` and LangGraph workflow as the CLI.

| Tab | What it shows |
|---|---|
| **Match** | Pick a job, run the agent with live node-by-node status, toggle watch mode (with a simulated upload mid-run), ranked table, stacked score breakdown, shortlist with explanations, and the LangGraph path actually taken (highlighted). Download the report, which is read back through `read_file` |
| **Resumes** | `resources/list` of every resume, server-side text preview via `resources/read` (the corrupt PDF shows its error), upload into the watched folder |
| **MCP Inspector** | `initialize` result, resources and templates, a tool playground showing the `tools/call` request next to the raw response, and a live JSON-RPC traffic log |
| **History** | Match runs and top candidates from the jobs-db MCP server |

![Match tab](docs/ui_match.png)

## Part A: the MCP server

### Protocol (`mcp_protocol.py`)
Written directly against the MCP specification (revisions 2024-11-05 → 2025-11-25) with only the standard library:

- **JSON-RPC 2.0:** requests, notifications (never answered), batch arrays, `id` validation, standard error codes
  `-32700 / -32600 / -32601 / -32602 / -32603`, plus `-32002` resource-not-found and `-32003` not-initialized.
- **Lifecycle:** `initialize` with version negotiation, `notifications/initialized`, `ping`, graceful shutdown on stdin EOF.
- **Tools:** paginated `tools/list` with JSON-Schema `inputSchema` and annotations; `tools/call` with argument validation,
  `structuredContent`, and `isError` results for execution failures.
- **Resource discovery:** `resources/list`, `resources/templates/list` (RFC 6570 templates), `resources/read`,
  `resources/subscribe`, and push notifications `resources/updated` and `resources/list_changed`.
- **Utilities:** `logging/setLevel` + `notifications/message`, `notifications/progress`, `notifications/cancelled`.
- **Transports:** stdio (requests run on a thread pool, so a blocking `watch_directory` never blocks `ping` or cancel)
  and Streamable HTTP (JSON-response mode, `Mcp-Session-Id`, Origin check, `MCP-Protocol-Version` header, `/health`).

Interop is verified: `tests/test_multi_mcp_and_interop.py` drives the server with the **official `mcp` Python SDK client**.

### Tools

| Tool | Origin | Notes |
|---|---|---|
| `list_files`, `read_file`, `write_file`, `search_files`, `get_file_metadata` | Milestone 1 | PDF/DOCX → text on the server; sandboxed paths; write allow-list |
| `watch_directory` | **new** | First call starts a watch and returns `watch_id`. Later calls block up to `timeout_seconds` and return created/modified/deleted files. A file is reported only once its size is stable, so half-copied PDFs are skipped. The server also pushes `resources/list_changed` + `resources/updated` for `watch://{id}` |
| `stop_watch`, `list_watches` | new | Watch management |
| `batch_process` | **new** | `extract_text` / `metadata` / `word_count` / `search` over `paths` or `directory`+`pattern`. Thread pool, per-file error isolation, progress notifications, LRU text cache (keyed on mtime+size), dedupe, limits, `fail_fast`, cancellation |

### Resources
`resume://{filename}` (each resume is also *listed*) · `fs://files/{+path}` · `watch://{watch_id}` ·
`config://server` · `server://capabilities`

### Configuration management
Precedence: defaults < JSON file (`config/filesystem_server.json` or `--config` / `FS_MCP_CONFIG`) < env vars
(`FS_MCP_<KEY>`, e.g. `FS_MCP_ROOT_DIR`, `FS_MCP_BATCH_MAX_WORKERS=8`, `FS_MCP_ALLOW_WRITE=false`) < CLI flags.
Unknown keys and invalid values fail fast. The effective config can be read at `config://server`.

## Part B: agent refactoring

| Milestone 3 (before) | Milestone 4 (after) |
|---|---|
| `@tool` functions calling `open()`, `os.listdir`, pypdf, python-docx inside the agent | No file I/O in the agent; `mcp.call("batch_process", …)` etc. |
| Tool list hard-coded in the agent | Tools **discovered at runtime** via `tools/list` (`connect_and_discover` node) |
| One file at a time | One `batch_process` call, 4 workers, progress events |
| Re-run the script to see new resumes | `--watch`: `watch_directory` loop inside the graph |
| Results only on disk | Report via `write_file` **and** persisted run via a second MCP server |

The scoring logic moved unchanged into `matching_engine.py`, a pure module with no I/O.
`test_agent_does_no_direct_file_io` disables `open()` and all `pathlib` I/O inside the agent process and the full run still passes.

**Multi-MCP (bonus):** servers are declared in `mcp_servers.json` (same shape as Claude Desktop's config).
`MultiMCPClient` routes each call to the server that owns the tool. `required` servers must start.
Optional ones are skipped with a warning, so the agent falls back to reading `jobs/JOB-xxx.json` through the filesystem
server when the DB server is absent. A third-party web-search server is pre-configured but disabled. Set `enabled: true`
and `BRAVE_API_KEY` to try it.

**LLM (optional):** set `LLM_MODEL` (any `init_chat_model` string) and install the provider package. The agent then
uses the LLM for match explanations, and `ask` mode lets the LLM choose MCP tools itself through `as_langchain_tools()`.
Without an LLM everything still runs, with rule-based explanations.

## Use the server from Claude Desktop
Copy `claude_desktop_config.example.json` into Claude Desktop's config, fix the paths, and restart. Claude then sees
the same 9 tools and the `resume://` resources.

## Docker (HTTP transport)
```bash
docker build -t resume-fs-mcp .
docker run -p 8765:8765 -v "$PWD/data:/app/data" resume-fs-mcp
curl localhost:8765/health
```

## Project layout
```
filesystem_mcp_server.py   MCP server: M1 tools + watch_directory + batch_process + resources + config
jobs_db_mcp_server.py      Bonus MCP server: SQLite jobs + match history
mcp_protocol.py            JSON-RPC 2.0 / MCP protocol core + stdio & HTTP transports
mcp_client.py              MCP client, multi-server router, LangChain tool adapter
matching_agent.py          LangGraph workflow (MCP-only I/O) + CLI
matching_engine.py         Pure scoring (skills, TF-IDF, experience)
mcp_servers.json           Which MCP servers the agent connects to
config/                    Server configuration
scripts/                   Sample-data generator
tests/                     59 pytest scenarios
docs/                      Diagrams, test scenarios, demo script
demo.py                    Scripted walkthrough for the video
app.py                     Streamlit dashboard (MCP client UI)
```
#   - M C P - i n t e g r a t i o n - r e s u m e - m a t c h e r  
 