---
name: minimax-sse-load-error-retryable
description: Bug fix: MiniMax SSE load errors (2064 "负载较高") were raised as bare Exception (not retried) — now detected as RetryableAPIError with non-retryable exclusion list for permanent errors (2061 "not support model")
enabled: true
tags: [python, minimax, llm-client, streaming, retry, bug-fix]
created: 2026-04-03T08:09:10Z
updated: 2026-04-03T08:09:10Z
---

# MiniMax SSE Load Error → RetryableAPIError

## Problem
MiniMax server returns overload errors INSIDE the SSE stream (not via HTTP status):
```json
{"type":"error","error":{"type":"server_error","message":"当前服务集群负载较高，请稍后重试，感谢您的耐心等待。 (2064)","http_code":"500"}}
```

Since the HTTP response is 200 (successful SSE), the `_RETRYABLE_STATUS_CODES` check never triggers. The error was caught by `raise Exception(f'SSE error: {err_text}')` — a bare Exception NOT in the `_RETRYABLE` tuple, so it was NOT retried by `stream_chat()`.

## Fix
In `_stream_chat_once()` SSE error handling block, detect retryable server errors by:
1. `error.type == 'server_error'`
2. `error.http_code` starts with '5'
3. Pattern match: '负载较高', '稍后重试', 'server overload', etc.

Raise as `RetryableAPIError` so `stream_chat()` retries with backoff.

## Exclusions (non-retryable)
MiniMax also sends permanent errors via `"type":"server_error"`:
- `"not support model"` (2061) — plan doesn't support the model
- `"invalid api key"` — auth failure  

These must NOT be retried. An exclusion list checks for permanent error patterns BEFORE applying the retryable check.

## Key Insight
SSE-transported errors (inside the stream) need their own retryability detection, separate from HTTP status code checks (which only work for non-200 responses).

