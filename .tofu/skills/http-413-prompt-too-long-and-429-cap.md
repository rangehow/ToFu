---
name: http-413-prompt-too-long-and-429-cap
description: HTTP 413 → PromptTooLongError fix + stream read timeout reduced from 3000s to 300s to prevent 50-min hangs
enabled: true
tags: [python, debugging, llm-dispatch, 413, 429, prompt-too-long, compaction]
created: 2026-04-05T04:33:28Z
updated: 2026-04-08T09:08:36Z
---

# HTTP 413 → PromptTooLongError + Stream Timeout Fix

## Bug Pattern 1: HTTP 413
When conversation context grows huge (2.8M+ tokens), the request body exceeds the API gateway's size limit:
1. Some slots return HTTP 413 "Request Entity Too Large" 
2. Must be treated same as HTTP 400 "prompt too long" → triggers reactive compaction

### Fix
- `lib/llm_client.py` — HTTP 413 → `PromptTooLongError`
- `lib/llm_dispatch/api.py` — Don't retry `PromptTooLongError` on other slots

## Bug Pattern 2: 50-minute stream read timeout hang
Task a9069e7d was stuck for exactly 50 minutes because `requests.post()` used `timeout=(1200, 3000)` — the 3000s read timeout meant if the API accepted the connection but never started streaming (e.g. queued behind rate-limited requests), the thread blocked for 50 minutes in `resp.iter_lines()`.

### Fix
- Changed `timeout=(1200, 3000)` → `timeout=(60, 300)` in `_stream_chat_once()`
- The read timeout applies to each chunk read, not total stream duration
- 300s = 5 minutes of silence before declaring the connection dead
- `stream_chat`'s retry logic (MAX_STREAM_RETRIES=4) handles the ConnectionError

## 429 Safety Cap
- `_MAX_429_CYCLES = 0` — disabled (infinite retry, user cancels via abort_check)
- Progressive backoff prevents self-inflicted TPM exhaustion (see llm-429-401-immediate-escape memory)

