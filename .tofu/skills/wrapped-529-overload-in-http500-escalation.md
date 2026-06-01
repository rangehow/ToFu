---
name: wrapped-529-overload-in-http500-escalation
description: Bug fix: Gateway wrapping 529 overload as HTTP 500 — now detected and escalated to dispatch layer as RateLimitError instead of futile same-key retries
enabled: true
tags: [python, llm-client, bug-fix, 529, overload, rate-limit, dispatch, gateway]
created: 2026-04-16T08:48:40Z
updated: 2026-04-16T08:48:40Z
---

# Wrapped 529 Overload in HTTP 500 — Escalation Fix

## Problem
Some API gateways (e.g., MiniMax via sankuai proxy) receive HTTP 529 (overloaded) from 
the model server but can't map it, so they return HTTP 500 with body like:
```json
{"status":500,"message":"Request exception","data":"No matching constant for [529]"}
```

Previously, our code treated this as a regular `RetryableAPIError` and retried up to 
`MAX_STREAM_RETRIES` times **on the same key** with exponential backoff (~30+ seconds wasted).
Since the model is genuinely overloaded, retrying the same endpoint is futile.

**Impact**: 1,124 retry attempts in error.log, 81 total failures after exhausting all retries.

## Fix
In `_classify_http_error()` (`lib/llm_client.py`), added `_is_wrapped_overload()` check 
before the generic `RetryableAPIError` raise. When HTTP 500 body contains evidence of 
embedded 429/529, raises `RateLimitError` instead — this escalates to the dispatch layer 
which tries a different key/model slot immediately.

Detection regex patterns:
- `No matching constant for [429]` / `No matching constant for [529]`
- `"status": 529` / `"status": 429` (JSON body)
- `status_code: 429` / `status_code: 529`

## Key Insight
`RateLimitError` → dispatch layer (try different key/model immediately)
`RetryableAPIError` → retry same key with exponential backoff (futile for overload)

