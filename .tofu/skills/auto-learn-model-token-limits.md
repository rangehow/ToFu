---
name: auto-learn-model-token-limits
description: Auto-learn and persist model max_tokens limits from HTTP 400 errors: _parse_token_limit_from_error() extracts limit from various API error formats, _learn_model_limit() persists to server_config.json, ModelLimitError triggers auto-retry in stream_chat/chat with corrected value, user notified via SSE delta
enabled: true
tags: [python, llm-client, auto-learn, model-limits, max-tokens, http-400, config-persistence, sse-notification]
created: 2026-03-29T15:28:13Z
updated: 2026-03-29T15:28:13Z
---

# Auto-Learning Model Token Limits

## Problem
Different LLM models have varying `max_tokens` limits (e.g., gpt-4.1-mini=65536, Claude=128000).
When `build_body()` sends `max_tokens=128000` to a model that only supports 65536, the API returns
HTTP 400 with an error like:
```
<400> InternalError.Algo.InvalidParameter: Range of max_tokens should be [1, 65536]
```
Previously, this caused a fallback to Opus or a hard failure.

## Solution Architecture

### 1. Error Detection (`_parse_token_limit_from_error`)
- Located in `lib/llm_client.py`
- Regex patterns match common API error formats from multiple providers:
  - `[1, 65536]` range style
  - `at most / cannot exceed / maximum of` style  
  - `between 1 and N` style
  - Both `max_tokens` and `max_output_tokens` variants
- Sanity check: extracted value must be 1..1,000,000

### 2. Persistence (`_learn_model_limit`)
- Updates in-memory `_LEARNED_MODEL_LIMITS` dict (thread-safe with `_limits_lock`)
- Persists to `~/.chatui/server_config.json` under `model_limits` key
- Generates `audit_log('model_limit_learned', ...)` audit trail
- Survives server restarts via `_load_learned_limits()` at module import

### 3. Clamping (`_clamp_max_tokens`)
- Enhanced to check BOTH family-level limits (`_MODEL_MAX_OUTPUT`) AND learned limits
- Takes the minimum of all applicable limits

### 4. Auto-Retry
- **Streaming (`stream_chat`)**: `ModelLimitError` caught in retry loop, body corrected, retried immediately (no backoff, doesn't count as transient error)
- **Non-streaming (`chat`)**: Recursive call with `_limit_retry=True` guard (one level only)
- In `_stream_chat_once()`: HTTP 400 → `_parse_token_limit_from_error()` → `raise ModelLimitError`

### 5. User Notification
- In `stream_llm_response()` (manager.py): checks `usage._model_limit_learned`
- Emits SSE delta with notice: `⚙️ Auto-detected model limit: gpt-4.1-mini max output tokens = 65,536`
- Also exposed in GET `/api/server-config` response as `model_limits` dict

### Key Files
- `lib/llm_client.py` — Core detection, learning, clamping, retry logic
- `lib/tasks_pkg/manager.py` — SSE notification to user
- `routes/common.py` — `model_limits` in server config API response
- `~/.chatui/server_config.json` — Persistent storage

