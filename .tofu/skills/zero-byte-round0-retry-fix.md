---
name: zero-byte-round0-retry-fix
description: Round-0 zero-byte gateway anomaly now auto-retries (was blocked by round_num>0 guard); CONTENT_FILTER classifier defers when _stream_anomaly=True
enabled: true
tags: [streaming, retry, gateway, bugfix]
created: 2026-05-12T06:19:55Z
updated: 2026-05-12T06:19:55Z
---

# Round-0 Zero-Byte Anomaly Retry (2026-05-12)

## Bug
The `aws.claude-opus-4.7` via sankuai gateway zero-byte anomaly
(documented in `stream-retry-cap-split-by-signature`) was NOT retried
when it happened on round 0. Two defects:

1. `lib/tasks_pkg/stream_handler.py` — `_is_anomaly_empty` had a
   `round_num > 0` guard, so round-0 anomalies fell through to the
   unconditional `if _stream_anomaly: break` block and surfaced as
   "API流异常终止" with no retry.
2. `lib/tasks_pkg/llm_fallback.py` — when round 0 returned
   `finish_reason=stop` with empty content, the code labeled it
   `content_filter` (safety block) **before** checking
   `_stream_anomaly`. The user log misleadingly showed
   "🚫 CONTENT_FILTER detected" for what was really a gateway hiccup.

## Fix
In `stream_handler.py`, the zero-byte signature is now computed
**first** as its own predicate (no `round_num` guard) and added to
`_is_abnormal`. Looser anomaly-empty still keeps the round-0 guard.
Predicate order:
```
_is_zero_byte = (no content) AND _stream_anomaly
                AND len(thinking)<100 AND elapsed_ms<15000
_is_classic_premature = (no content) AND len(thinking)>1000
_is_anomaly_empty     = (no content) AND _stream_anomaly
                        AND round_num>0 AND NOT _is_zero_byte
```

In `llm_fallback.py`, both the primary and fallback CONTENT_FILTER
detectors now skip when `usage._stream_anomaly` is True; an INFO
log line announces "deferring to zero-byte retry path". This applies
to all models, not just Claude — any provider that sets
`_stream_anomaly` benefits.

## Test
`tests/test_zero_byte_round0_retry.py` (6 tests, no pytest dep):
- `test_zero_byte_round0_retries_now` — primary regression guard
- `test_zero_byte_uses_large_cap` — confirms cap=16
- `test_zero_byte_eventually_breaks_after_cap` — error message preserved
- `test_classic_premature_uses_low_cap` — classic still cap=2
- `test_normal_empty_round0_does_not_retry` — no anomaly = no loop
- `test_phase_event_fields_for_zero_byte_round0` — UI dedup fields

Run: `python tests/test_zero_byte_round0_retry.py`

## Evidence
`logs/raw_sse_anomaly.log` line 43 (M-TraceId
`92f47a7f3ecb4086b19da552ea9be846`) — round 0,
`ring_lines=0 ring_bytes=0`, elapsed 4.51s. Previously fatal,
now retried up to 16x.

