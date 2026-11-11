# Test scenarios

Run the full suite with `pytest -v`: **59 tests, ~15 s, no network or API keys needed**.
Every test runs against a fresh copy of the sample data in a temp folder.
The latest output is in [`test_results.txt`](test_results.txt).

| # | Scenario | What it proves | Test(s) |
|---|---|---|---|
| **A. JSON-RPC 2.0 + MCP protocol** ||||
| A1 | Version negotiation | Server echoes a supported `protocolVersion`, offers its latest otherwise | `test_initialize_negotiates_requested_version`, `test_initialize_unknown_version_gets_server_latest` |
| A2 | Lifecycle enforcement | Requests before `initialize` → `-32003`; `ping` always allowed | `test_requests_before_initialize_are_rejected_but_ping_allowed` |
| A3 | Standard error codes | `-32700`, `-32600`, `-32601`, `-32602`, `-32002` | `test_parse_error`, `test_invalid_requests`, `test_method_not_found`, `test_resource_not_found_code` |
| A4 | Notifications & batches | Notifications never get a response; batch arrays answered as arrays | `test_notifications_get_no_response`, `test_batch_requests` |
| A5 | Pagination | `nextCursor` paging over `tools/list`; invalid cursor rejected | `test_tools_list_pagination_and_bad_cursor` |
| A6 | Tool vs protocol errors | Unknown tool = protocol error; bad arguments = `isError` result the LLM can read | `test_unknown_tool_is_protocol_error_but_bad_args_are_tool_errors` |
| A7 | Cancellation | `notifications/cancelled` stops a blocking call; no response is sent | `test_cancelled_request_gets_no_response`, `test_watch_returns_early_when_cancelled` |
| A8 | Real stdio transport | Newline-delimited JSON on stdout only, logs on stderr, graceful EOF shutdown | `test_stdio_transport_end_to_end` |
| A9 | Interop with the official MCP SDK | The `mcp` Python SDK client can initialize, list, call and read | `test_official_sdk_client_can_use_our_server` |
| **B. Milestone 1 tools as MCP tools** ||||
| B1 | Listing, reading PDF/DOCX/TXT, truncation | Format-aware extraction on the server | `test_list_files`, `test_read_file_extracts_pdf_and_docx`, `test_read_file_truncation` |
| B2 | Sandbox security | `../`, absolute paths and traversal are blocked | `test_sandbox_blocks_path_traversal` |
| B3 | Writes & config | Write modes, extension allow-list, read-only mode, size limits | `test_write_file_modes`, `test_write_disabled_by_config`, `test_not_found_and_size_limit` |
| B4 | Search & metadata | Cross-format search, SHA-256, PDF page count | `test_search_files_spans_formats`, `test_get_file_metadata` |
| **C. New capabilities** ||||
| C1 | `batch_process` error isolation | 1 corrupt PDF out of 6 → 5 ok, 1 error, batch still succeeds | `test_batch_isolates_failures`, `test_batch_with_explicit_paths_including_bad_ones` |
| C2 | `batch_process` efficiency | Progress notifications, text cache hits on re-run, limits, fail-fast | `test_batch_sends_progress_notifications`, `test_batch_uses_text_cache`, `test_batch_limits`, `test_batch_fail_fast` |
| C3 | `watch_directory` events | Detects created → modified → deleted resumes | `test_watch_detects_new_modified_and_deleted` |
| C4 | `watch_directory` push | `resources/list_changed`, `resources/updated` (subscribed), `notifications/message` | `test_watch_pushes_notifications` |
| C5 | Watch management | Timeout, pattern filter, unknown id, limit, list, stop | `test_watch_timeout_and_pattern_filter`, `test_watch_errors_list_and_stop` |
| **D. Resource discovery** ||||
| D1 | `resources/list` + templates | Resumes, config and capability catalogue are discoverable | `test_resource_discovery` |
| D2 | `resources/read` | `resume://`, `fs://files/`, `config://server`, `server://capabilities` | `test_read_resources`, `test_resource_list_updates_when_files_change` |
| **E. HTTP transport (production deployment)** ||||
| E1 | Sessions | `Mcp-Session-Id` issued on initialize, required afterwards, `DELETE` ends it | `test_http_session_flow`, `test_http_health_and_session_delete` |
| E2 | Hardening | Origin check (DNS rebinding), protocol-version header, unknown session | `test_http_rejects_bad_requests` |
| **F. Agent workflow (LangGraph + MCP)** ||||
| F1 | End-to-end run | Correct ranking, corrupt file reported, report written, exact node path | `test_full_run_ranks_and_publishes` |
| F2 | Multi-MCP | Results persisted in the second server and read back | `test_results_persisted_in_second_mcp_server`, `test_routing_across_servers` |
| F3 | Graceful degradation | Runs with the filesystem server only; optional server crash tolerated | `test_works_with_filesystem_server_only`, `test_optional_server_failure_is_tolerated` |
| F4 | Failure paths | Unknown job → clean `failed` status; missing required server → clear error | `test_unknown_job_fails_cleanly`, `test_required_server_missing_raises` |
| F5 | Watch mode in the agent | A resume dropped mid-run is extracted, ranked and saved as a new run | `test_watch_mode_picks_up_new_resume` |
| F6 | **Refactor proof** | With `open()` / `Path` I/O disabled in the agent process, the run still succeeds | `test_agent_does_no_direct_file_io` |
| F7 | LLM tool adapter | MCP tools discovered at runtime become LangChain tools | `test_langchain_tool_adapter` |
| **G. Scoring (unchanged behaviour)** ||||
| G1 | Skills, experience, ranking | Aliases (EKS→Kubernetes), implications (PostgreSQL→SQL), scoring | `test_matching_engine.py` |
| **H. Dashboard** ||||
| H1 | Streamlit UI | Headless `AppTest`: the app starts, runs the agent over both MCP servers, writes the report, and the inspector renders a `tools/call` round trip | `test_dashboard_runs_agent_and_inspector` |
