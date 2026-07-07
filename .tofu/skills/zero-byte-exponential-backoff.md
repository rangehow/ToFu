---
name: zero-byte-exponential-backoff
description: Zero-byte AND classic-premature retries both use exponential backoff (classic cap raised 2→16); only late-round stream_anomaly bucket stays no-backoff
enabled: true
tags: [llm, retry, stream, gateway, convention]
created: 2026-05-12T07:19:18Z
updated: 2026-06-17T01:22:37Z
---

# Abnormal-stop retry pacing (lib/tasks_pkg/stream_handler.py)

## Current design (supersedes the 2026-05-12 "classic does NOT sleep" note)
`analyse_stream_result` paces abnormal-stop retries. Backoff applies to:
- **zero_byte** bucket: always.
- **classic premature-close** bucket: NOW ALSO backed off (changed when
  `_PREMATURE_RETRY_MAX_CLASSIC` was raised 2 → 16). A cap of 16 with no
  backoff would hammer a dropped connection in milliseconds, so backoff is
  the necessary companion to the higher cap.
- **late-round stream_anomaly** bucket (`_is_anomaly_empty`, round>0, not
  zero-byte): KEEPS the historical NO-backoff behaviour (sleeps 0.0s).

Code: `_backoff_s = _zero_byte_backoff_seconds(n) if (_is_zero_byte or _is_classic_premature) else 0.0`.
Schedule (`_zero_byte_backoff_seconds`): `0.5*2**(n-1)` capped at 8.0 + uniform[0,0.5) jitter → 0.5,1,2,4,8,8...
`_interruptible_sleep(seconds, task)` polls `task['aborted']` every 100ms.
Caps: `_PREMATURE_RETRY_MAX_CLASSIC = _PREMATURE_RETRY_MAX_ZERO_BYTE = 16`; `_EMPTY_STOP_RETRY_MAX = 2`.
Zero-byte also sets `task['_force_rotate_pair']=(key,model)` to rotate slots next dispatch.

## Tests (tests/test_zero_byte_backoff.py — 7, pytest OR `python3 tests/test_zero_byte_backoff.py`)
- backoff schedule doubles to cap; jitter present; zero-byte retry sleeps once; phase event carries backoff_s.
- `test_classic_premature_retry_uses_backoff` — classic NOW sleeps once in [0.5,1.0) (RENAMED from the old `_does_not_sleep`; that old assertion encoded the obsolete cap=2/no-backoff design and was the source of a stale-test failure fixed 2026-06).
- `test_late_round_stream_anomaly_does_not_sleep` — late-round anomaly bucket still 0.0s (chunks_received=12, elapsed=90s, round=2 → classic bucket label but `_is_anomaly_empty`, so no backoff).
- interruptible sleep returns <0.5s on abort.

## Why backoff (cost evidence, conv mp2a0tz1h45d8p)
17 zero-byte retries × 28,718 cache_read_tokens = ~488K cache reads ≈ $0.73 for ONE failed turn. Bedrock zero-byte windows cluster per-pool (34/34 anomalies on sankuai_key_0 in 2 min) → hammering every ~3s rarely recovers. Hence backoff + slot rotation.
