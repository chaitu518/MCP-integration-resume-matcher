# Architecture & workflow diagrams

GitHub renders these Mermaid diagrams inline. For slides or the demo video, open
[`diagrams.html`](diagrams.html) in a browser. It renders the same diagrams full-size.

## 1. Component view

**Milestone 3:** the agent called Python functions that touched the disk directly.
**Milestone 4:** the agent is an MCP *client*. Every byte of resume data crosses a JSON-RPC boundary.

```mermaid
flowchart LR
    subgraph AGENT["matching_agent.py · LangGraph"]
        G["StateGraph nodes"] --> ENG["matching_engine.py<br/>pure scoring, no I/O"]
        G --> MC["MultiMCPClient<br/>mcp_client.py"]
    end

    MC -- "JSON-RPC 2.0 · stdio" --> FS
    MC -- "JSON-RPC 2.0 · stdio" --> DB
    MC -. "optional, disabled by default" .-> WS["web-search MCP server<br/>(third party)"]

    subgraph FS["filesystem_mcp_server.py"]
        T1["Milestone 1 tools<br/>list_files · read_file · write_file<br/>search_files · get_file_metadata"]
        T2["New capabilities<br/>watch_directory · stop_watch · list_watches<br/>batch_process"]
        R1["Resources<br/>resume://{filename} · fs://files/{+path}<br/>watch://{watch_id} · config://server · server://capabilities"]
    end

    subgraph DB["jobs_db_mcp_server.py (bonus)"]
        T3["get_job · list_jobs · add_job<br/>save_match_results · get_match_history · get_top_candidates"]
        R2["Resources<br/>jobs://{job_id} · db://schema"]
    end

    FS --> DISK[("data/ sandbox<br/>resumes · jobs · output")]
    DB --> SQL[("SQLite<br/>data/jobs.db")]
    HOST["Claude Desktop / any MCP host"] -. "same server, no code changes" .-> FS

    PROTO["mcp_protocol.py<br/>JSON-RPC 2.0 + MCP lifecycle<br/>stdio & Streamable HTTP"]
    PROTO -.-> FS
    PROTO -.-> DB
```

## 2. Agent state machine (LangGraph)

Solid arrows are fixed edges. Labelled arrows are the conditional routers in `matching_agent.py`.
(`python matching_agent.py graph` prints the auto-generated version of this graph.)

```mermaid
stateDiagram-v2
    [*] --> connect_and_discover
    connect_and_discover --> load_job : initialize + tools/list on every server
    load_job --> discover_resumes : job found
    load_job --> finalize : unknown job_id
    discover_resumes --> extract_resumes : resumes found
    discover_resumes --> wait_for_new_resumes : none yet, watch on
    discover_resumes --> finalize : none, watch off
    extract_resumes --> analyze_candidates : batch_process()
    analyze_candidates --> score_and_rank
    score_and_rank --> explain_matches
    explain_matches --> publish_results
    publish_results --> wait_for_new_resumes : watch on, cycles left
    publish_results --> finalize : otherwise
    wait_for_new_resumes --> extract_resumes : new or modified resumes
    wait_for_new_resumes --> score_and_rank : a resume was deleted
    wait_for_new_resumes --> wait_for_new_resumes : nothing yet, cycles left
    wait_for_new_resumes --> finalize : cycles used up
    finalize --> [*] : stop_watch, status
```

| Node | MCP calls | Purpose |
|---|---|---|
| `connect_and_discover` | `initialize`, `tools/list` (each server) | Capability discovery; fail fast if required tools are missing |
| `load_job` | `jobs_db.get_job` → fallback `filesystem.read_file` | Job requirements |
| `discover_resumes` | `watch_directory(timeout=0)`, `list_files` | Start watch **before** reading so no upload is missed |
| `extract_resumes` | `batch_process(operation=extract_text)` + `notifications/progress` | Concurrent PDF/DOCX/TXT extraction; bad files isolated |
| `analyze_candidates` | — | Skills, years, contact (pure Python) |
| `score_and_rank` | — | Hybrid score: skills + TF-IDF + experience |
| `explain_matches` | — (optional LLM) | One-line rationale per candidate |
| `publish_results` | `filesystem.write_file`, `jobs_db.save_match_results` | Markdown report + persisted run |
| `wait_for_new_resumes` | `watch_directory(watch_id, timeout)` | Blocks until a resume arrives / changes / is deleted |
| `finalize` | `stop_watch` | Clean shutdown |

## 3. Agent ↔ MCP interaction (sequence)

```mermaid
sequenceDiagram
    autonumber
    participant A as LangGraph agent
    participant C as MCP client
    participant FS as filesystem MCP server
    participant DB as jobs-db MCP server

    A->>C: MultiMCPClient.connect()
    C->>FS: initialize {protocolVersion: 2025-11-25}
    FS-->>C: capabilities {tools, resources(subscribe, listChanged), logging}
    C->>FS: notifications/initialized
    C->>FS: tools/list
    C->>DB: initialize, notifications/initialized, tools/list

    A->>C: get_job("JOB-001")
    C->>DB: tools/call get_job
    DB-->>C: required / preferred skills, min years

    A->>C: watch_directory(timeout 0)
    C->>FS: tools/call watch_directory
    FS-->>C: watch_id

    A->>C: batch_process(paths)
    C->>FS: tools/call batch_process (_meta.progressToken)
    loop each file, 4 worker threads
        FS--)C: notifications/progress
    end
    FS-->>C: 5 ok, 1 error (isolated, not fatal)

    Note over A: analyze, score, explain (no I/O)

    A->>C: write_file(report.md)
    C->>FS: tools/call write_file
    A->>C: save_match_results(ranked)
    C->>DB: tools/call save_match_results

    A->>C: watch_directory(watch_id, timeout 10)
    C->>FS: tools/call watch_directory (blocks)
    Note over FS: recruiter drops karthik_nair.txt
    FS--)C: notifications/resources/list_changed
    FS-->>C: new_files: [resumes/karthik_nair.txt]
    A->>C: batch_process([new file]), re-rank, publish
    A->>C: stop_watch(watch_id)
    C->>FS: close stdin (stdio shutdown)
```

## 4. Error model

| Situation | Where it surfaces | Code |
|---|---|---|
| Malformed JSON | JSON-RPC error | `-32700` Parse error |
| Not a JSON-RPC 2.0 object, bad `id`, empty batch | JSON-RPC error | `-32600` Invalid Request |
| Unknown method (e.g. `prompts/list`) | JSON-RPC error | `-32601` Method not found |
| Unknown tool, bad cursor, params not an object | JSON-RPC error | `-32602` Invalid params |
| Request before `initialize` | JSON-RPC error | `-32003` Server not initialized |
| Resource URI that doesn't exist | JSON-RPC error | `-32002` Resource not found |
| Unexpected server bug | JSON-RPC error | `-32603` Internal error |
| Bad tool arguments, path outside sandbox, corrupt PDF, file too large, read-only mode | **Tool result** with `isError: true` and a machine-readable `error` code | `INVALID_ARGUMENTS`, `ACCESS_DENIED`, `PARSE_ERROR`, `FILE_TOO_LARGE`, `READ_ONLY`, … |

Tool-level failures are deliberately *results*, not protocol errors, as the MCP spec recommends,
so an LLM can read the message and correct its next call.
