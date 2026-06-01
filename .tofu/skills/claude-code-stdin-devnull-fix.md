---
name: claude-code-subprocess-fixes
description: Bug fixes: Claude Code CLI subprocess — stdin /dev/null, api_retry phase display, JSONL event logging
enabled: true
tags: [bug-fix, subprocess, claude-code, codex, stdin, phase, sse]
created: 2026-04-08T13:36:14Z
updated: 2026-04-08T15:13:48Z
---

# Claude Code CLI Subprocess Fixes

## Bug 1: stdin must be /dev/null
When spawning CLI agent backends (Claude Code, Codex) via `subprocess.Popen`,
stdin was not explicitly set. It defaulted to inheriting the parent's stdin
which was a real terminal (`/dev/pts/N`). Claude Code CLI detects a TTY on
stdin and enters interactive mode, hanging indefinitely.

**Fix**: `stdin=subprocess.DEVNULL` in Popen call.

## Bug 2: api_retry events showed "Waiting…" instead of retry status
Claude Code CLI outputs `{"type":"system","subtype":"api_retry",...}` events
when hitting rate limits (429). These have fields: `attempt`, `max_retries`,
`retry_delay_ms`, `error_status`, `error`.

The original code used `event.get('message', 'Retrying API call...')` but
the event has no `message` field. The SSE bridge sent `phase: 'working'`,
but the frontend only recognized `tool_exec`, `llm_thinking`, `retrying`,
`compacting`, `thinking_active` — so `'working'` fell through to "Waiting…".

**Fix**:
1. Added `phase_type` field to `NormalizedEvent` (protocol.py)
2. SSE bridge uses `event.phase_type or 'working'` (sse_bridge.py)
3. Claude code sets `phase_type='retrying'` for api_retry (claude_code.py)
4. Frontend handles `phase === 'working'` with detail text (ui.js)
5. Stream timer recognizes `'working'` as expected silence (core.js)

## Files
- `lib/agent_backends/protocol.py` — NormalizedEvent.phase_type field
- `lib/agent_backends/sse_bridge.py` — phase_type mapping
- `lib/agent_backends/claude_code.py` — Popen stdin, api_retry parsing, JSONL logging
- `lib/agent_backends/codex.py` — Popen stdin
- `static/js/ui.js` — 'working' phase rendering
- `static/js/core.js` — 'working' in stream timer silence check

## Key Lesson
Always set `stdin=subprocess.DEVNULL` for non-interactive CLI subprocesses.
Always test that phase types sent by backend are handled by frontend phase renderer.

