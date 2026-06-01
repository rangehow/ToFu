---
name: multi-backend-agent-architecture
description: Multi-backend agent arch: SSEBridgeState, Claude Code tool lifecycle mapping, searchRounds tracking, display string generation
enabled: true
tags: [architecture, agent-backends, claude-code, codex, multi-backend]
created: 2026-04-04T06:52:26Z
updated: 2026-04-08T15:28:16Z
---

# Multi-Backend Agent Architecture

## Overview
`lib/agent_backends/` provides a pluggable backend system where external CLI agents
(Claude Code, Codex) can replace the built-in Tofu orchestrator as the coding agent backend.

## Key Components

| File | Purpose |
|---|---|
| `protocol.py` | `AgentBackend` ABC, `NormalizedEvent` dataclass, `BackendCapabilities` |
| `__init__.py` | Registry: `get_backend()`, `list_backends()`, auto-registration |
| `builtin.py` | Wraps existing `run_task()` — default backend, all features |
| `claude_code.py` | Spawns `claude -p --output-format stream-json`, normalizes JSONL |
| `codex.py` | Spawns `codex exec --json`, normalizes JSONL |
| `sse_bridge.py` | `SSEBridgeState` (stateful) translates NormalizedEvent → frontend SSE with roundNum tracking |
| `detection.py` | `detect_claude_code()`, `detect_codex()` — check CLI availability |
| `session_store.py` | Maps conv_id → backend session_id (PostgreSQL) |

## Event Flow
```
External CLI → JSONL stdout → Backend._normalize() → NormalizedEvent
→ SSEBridgeState.translate() → append_event(task, sse_dict) → Frontend SSE
```

## Claude Code Tool Lifecycle (critical mapping)
```
CC: content_block_start(tool_use) → PHASE (preparing) — no TOOL_START yet
CC: input_json_delta             → silent accumulation
CC: content_block_stop           → TOOL_START (with full input, execution begins)
CC: user(tool_result)            → TOOL_COMPLETE (with result, execution done)
```
- `tool_id_to_name` dict tracks mapping for resolving names in tool results
- `user` type events contain `tool_result` blocks — MUST be handled

## SSE Bridge State
- `SSEBridgeState` class tracks roundNum counter and tool_id→roundNum/name mappings
- Created once per task in `_run_external()`
- `_build_tool_query()` maps external tool names (Read, Write, Bash, Glob, Grep, etc.) to human-friendly display strings
- `_build_tool_results_meta()` builds the results array format frontend expects

## _run_external() searchRounds tracking
- On TOOL_START: translate → build round_entry → append to task['searchRounds']
- On TOOL_COMPLETE: translate → find matching round by roundNum → update results/status
- searchRounds are persisted to DB and included in SSE state snapshots

## Frontend Expectations for tool_start/tool_result events
```js
tool_start: { roundNum, query, toolName, toolCallId, toolArgs }
tool_result: { roundNum, results: [{toolName, title, snippet, source}], toolCallId }
```

## DB Schema
- `agent_sessions` table: `(conv_id, backend, session_id)` PK, schema version 7

