---
name: gateway-5xx-treated-as-429
description: HTTP 502/503/504 now raise RateLimitError (infinite slot rotation), not RetryableAPIError
enabled: true
tags: [llm_client, dispatch, error-handling, convention]
created: 2026-04-20T06:31:08Z
updated: 2026-04-20T06:31:08Z
---

# Gateway 5xx (502/503/504) is classified as RateLimitError

## Change (2026-04-20, approved by user)

In `lib/llm_client.py` `_classify_http_error()` and the SSE error
branch in `_stream_chat_once()`:

- **502, 503, 504** → raise `RateLimitError` (NOT `RetryableAPIError`).
  This causes the dispatch layer to apply a 0.5s slot cooldown and
  rotate to another slot, retrying **indefinitely** (same as HTTP 429).
- **500, 529** → still raise `RetryableAPIError` (5 same-key retries with
  exponential backoff 3s → 6s → 12s → 24s). Plus: 500 with `"No matching
  constant for [429|529]"` body detected by `_is_wrapped_overload()`
  escalates to `RateLimitError`.
- **429** → unchanged (`RateLimitError`, indefinite rotation).

## Rationale

This project's gateway (`aigc.sankuai.com`) is operationally stable.
A 5xx almost always means upstream pool overload, not gateway outage.
Retrying on the SAME key for ~45s wall-time was a waste; rotating slots
immediately is far more likely to succeed because different keys hit
different backend pools.

## Key constants

```python
# lib/llm_client.py
_RETRYABLE_STATUS_CODES = {500, 529}          # retry same key
_GATEWAY_THROTTLE_STATUS = {502, 503, 504}    # rotate slots (like 429)
```

## Where handling happens

- Classification: `_classify_http_error()` at entry path (`lib/llm_client.py:308`)
- Classification: SSE error branch in `_stream_chat_once()` (look for
  `_GATEWAY_THROTTLE_STATUS` in the SSE error dispatch, ~line 2080+)
- Rotation logic: `dispatch_stream` and `dispatch_chat` in
  `lib/llm_dispatch/api.py` — `except RateLimitError` blocks already
  handle `_429_count`-style indefinite retry with 0.3s sleep between
  cycles and 0.5s slot cooldown.

## When NOT to follow this pattern

If the gateway becomes unstable (real outages), add a cycle cap
(e.g. `_MAX_GATEWAY_CYCLES = 20`) so we don't spin forever during
a real multi-hour outage. Currently no cap — user confirmed gateway
will not go down.

## Related audit log entry

`audit_log('config_change', param='gateway_5xx_handling', ...)`
emitted on 2026-04-20 with `approved_by='user'`.

