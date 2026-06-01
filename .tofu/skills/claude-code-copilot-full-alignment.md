---
name: claude-code-copilot-full-alignment
description: Complete Claude Code → ChatUI alignment: 15 improvements across streaming tool exec, tool result budgeting, micro-compact, reactive compact, delta attachments, concurrency safety, memory prefetch, tool deferral, effort controls, prompt sections, 9-section summary — 134 tests passing
enabled: true
tags: [python, claude-code, streaming, prefetch, delta-attachments, concurrency, architecture, co-pilot]
created: 2026-04-01T03:13:22Z
updated: 2026-04-01T03:13:22Z
---

# Claude Code → ChatUI Co-Pilot Alignment — Full Implementation

## Streaming Tool Execution Architecture
- `StreamingToolAccumulator` in `lib/tasks_pkg/streaming_tool_executor.py`
- `on_tool_call_ready(tool_call_dict)` callback threaded through:
  `_stream_chat_once` → `stream_chat` → `dispatch_stream` → `stream_llm_response` → `_llm_call_with_fallback` → orchestrator
- Detection: when new tool_call index appears in SSE deltas, the PREVIOUS tool's args are complete → callback fires
- Final tool fires at stream end (no "next index" to trigger it)
- `_STREAMABLE_TOOLS`: read_files, grep_search, find_files, list_dir, web_search, fetch_url, check_error_logs
- Results injected into `task['_tool_result_cache']` via `_make_cache_key()` — dedup cache finds pre-computed results

## Memory Prefetch Architecture
- Before tool assembly, 2-thread pool starts `get_context_for_prompt()` and `build_skills_context()`
- Futures stored on `task['_prefetch_project']` and `task['_prefetch_skills']`
- `_inject_system_contexts(task=task)` consumes via `_get_prefetched(key, fallback_fn)`
- Graceful fallback: if future not done or failed, calls fallback synchronously

## Delta Attachment Architecture
- Per-section tracking: `_last_context_hashes[(conv_id, category)] → md5_hash[:16]`
- Categories: 'project', 'skills'
- `_should_inject(conv_id, category, text) → bool`: True if changed or first injection
- Saves tokens across successive tasks in same conversation

## Key Files
| File | Role |
|------|------|
| `lib/tasks_pkg/streaming_tool_executor.py` | StreamingToolAccumulator |
| `lib/tasks_pkg/system_context.py` | Delta tracking + prefetch + Claude Code prompt sections |
| `lib/tasks_pkg/compaction.py` | Budget, micro-compact, 9-section, reactive |
| `lib/tools/deferral.py` | Tool deferral infrastructure |

