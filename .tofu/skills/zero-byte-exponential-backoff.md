---
name: zero-byte-exponential-backoff
description: Zero-byte gateway retries now use exponential backoff + jitter (corrects "essentially free" assumption)
enabled: true
tags: [llm, retry, stream, gateway, convention]
created: 2026-05-12T07:19:18Z
updated: 2026-05-12T07:19:18Z
---


# Zero-byte retry pacing (lib/tasks_pkg/stream_handler.py, 2026-05-12)

## Why
The earlier "each zero-byte retry is essentially free" claim was wrong for
prompt-cache billing.  Verified on conv `mp2a0tz1h45d8p`: 17 retries × 28,718
`cache_read_tokens` = ~488 K cache reads = ~$0.73 at Opus list price for a
single failed turn.  Bedrock zero-byte windows also cluster — hammering the
same poisoned pool every ~3s rarely recovers.  Anthropic SDK / Claude Code
themselves use exponential backoff (`CLAUDE_CODE_MAX_RETRIES`).

## What changed
- Added `_zero_byte_backoff_seconds(attempt)` and `_interruptible_sleep` helpers.
- After deciding `'continue'` for a zero-byte abnormal stop, the analyser
  sleeps `0.5 * 2**(attempt-1)` seconds (capped at 8s) plus uniform [0, 0.5s)
  jitter.  Schedule: 0.5, 1, 2, 4, 8, 8, 8, ...
- Sleep polls `task['aborted']` every 100 ms so user abort interrupts promptly.
- Phase event carries `backoff_s` (frontend ignores extra fields; dedup is
  still on `attempt`).  Detail string surfaces "退避 X.Xs 后重试" to the user.
- Classic premature-close bucket does NOT sleep (already capped at 2; full
  per-attempt token cost dominates wall time anyway).
- Cap unchanged: `_PREMATURE_RETRY_MAX_ZERO_BYTE = 16`.  Total wall-time
  budget rises from ~46s to ~75s.

## Why not infinite retries (rejected proposal)
1. Zero-byte does not currently rotate slots → infinite means burning
   cache-read tokens on the same dead key forever.
2. Outage windows can persist for minutes; user sees a hung session with no
   feedback.
3. Asymmetric to 429: 429 retries succeed because slot rotation finds a free
   pool; zero-byte without rotation is just spinning.
   → If we ever add slot rotation here (mirroring `gateway-5xx-treated-as-429`),
   revisit and consider raising the cap.

## Tests
- `tests/test_zero_byte_round0_retry.py` (6 tests, unchanged) — still green.
- `tests/test_zero_byte_backoff.py` (6 new tests):
  - `test_backoff_schedule_doubles_until_cap` — schedule 0.5/1/2/4/8/8/8
  - `test_backoff_includes_jitter` — jitter ∈ [base, base+0.5)
  - `test_zero_byte_retry_calls_sleep_with_backoff` — sleep invoked once
  - `test_phase_event_carries_backoff_s` — UI metadata exposed
  - `test_classic_premature_retry_does_not_sleep` — no backoff in classic bucket
  - `test_interruptible_sleep_returns_promptly_on_abort` — abort < 500 ms

Both run as plain `python tests/test_zero_byte_*.py` (no pytest dep);
the new file ships its own minimal `_MonkeyPatch` shim.

## Cost evidence (conv mp2a0tz1h45d8p, all 17 rounds)
- `cache_read_tokens`: 488,206 (28,718 × 17)
- `prompt_tokens` uncached: 102
- `completion_tokens`: 17
- Approx Anthropic public list price: $0.73 per failed turn

## Future direction (not implemented)
Highest-leverage next step would be to force slot rotation on each
zero-byte retry (analogous to `gateway-5xx-treated-as-429`) so each
retry hits a different upstream pool.  Failures cluster per-pool —
in our logs all 34 anomalies in 2 minutes were on `sankuai_key_0`.
Add `force_rotate=True` flag to the dispatch call when `_is_zero_byte`.

