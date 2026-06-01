---
name: frontend-metadata-bloats-llm-request-body
description: Bug fix: Frontend message fields (searchRounds 1.45MB, thinking 108KB, translatedContent 63KB, apiRounds 45KB) sent to LLM API as non-API JSON bloat, causing BrokenPipe gateway errors on large conversations — fix: _strip_non_api_fields whitelist in build_body()
enabled: true
tags: [python, llm-client, build-body, request-size, BrokenPipe, gateway, frontend-metadata, bug-fix, performance]
created: 2026-03-31T15:51:03Z
updated: 2026-03-31T15:51:03Z
---

# Frontend Metadata Bloats LLM Request Body → BrokenPipe

## Symptom
- Specific conversations repeatedly fail with `BrokenPipeError(32, 'Broken pipe')` during SSE stream read
- Error happens at high round numbers (R11-R13) after context accumulates
- Only affects conversations with long history and many tool-call rounds
- Gateway kills the TCP connection during/after request body upload

## Root Cause
Frontend messages stored in the conversation contain display-only metadata fields:
- `searchRounds`: **1.45 MB** — complete search result data for UI rendering
- `thinking`: **108 KB** — thinking/reasoning display content (NOT `reasoning_content`)
- `translatedContent`: **63 KB** — translated text for bilingual display
- `apiRounds`: **45 KB** — API call metadata for UI panels
- `toolSummary`: **14 KB** — tool result summaries for UI
- Plus: `originalContent`, `usage`, `timestamp`, `model`, `images`, `pdfTexts`, `_taskId`, `_translateDone`, etc.

These fields are passed verbatim through `task['messages']` → `build_body()` → `json.dumps(body)` → HTTP POST, inflating the request body from **68 KB → 1.72 MB** (96% bloat).

The LLM gateway/proxy has a body size limit (often 1-2 MB for nginx defaults), and at high round numbers with accumulated context, the body exceeds this limit, causing the gateway to reset the TCP connection (BrokenPipe).

## Fix
Add `_strip_non_api_fields()` in `build_body()` that whitelists only API-valid fields:

```python
_API_MESSAGE_FIELDS = frozenset({
    'role', 'content', 'name',              # standard OpenAI
    'tool_calls', 'tool_call_id',           # tool use
    'reasoning_content',                    # thinking models (vendor extension)
    'cache_control',                        # Anthropic prompt caching
})

def _strip_non_api_fields(messages: list) -> list:
    cleaned = []
    for msg in messages:
        clean = {k: v for k, v in msg.items() if k in _API_MESSAGE_FIELDS}
        cleaned.append(clean)
    return cleaned
```

## Key Insight
The `thinking` field (frontend display) is different from `reasoning_content` (API field). Historical messages only have `thinking` which was never API-valid anyway. The orchestrator correctly uses `reasoning_content` for current-task rounds, so stripping `thinking` changes nothing semantically.

## Secondary Issue
The compaction token estimator (`_estimate_msg_tokens`) also doesn't count `thinking`, so it underestimates context size for conversations with large thinking content. This is less critical since the API never sees it after the fix.

