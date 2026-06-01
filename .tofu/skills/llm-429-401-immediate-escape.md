---
name: llm-429-401-immediate-escape
description: 429 retry: fast 0.3s polling (shared-key contention) + periodic exclusion reset every 60s to recover from transient 502s
enabled: true
tags: [python, debugging, llm-dispatch, retry, 429, rate-limit, cooldown, cycling]
created: 2026-03-16T16:53:17Z
updated: 2026-04-08T09:23:12Z
---

# 429 Retry Strategy — Fast Polling with Exclusion Reset

## Context
HTTP 429 in shared-key environments means **contention** with other users, not self-inflicted overload.
Users need aggressive fast polling to grab slots the instant they become available.
Backoff is counterproductive — it lets competing users grab slots while we sleep.

## Root Cause (discovered 2026-04-08)
Task 12bb98d7 did 10,000+ 429 retries over 80+ minutes without succeeding, while other tasks on the
same slots succeeded with just 1-2 retries. Root cause was **two compounding bugs**:

1. **Permanent `exclude_pairs`**: After hitting 502 gateway errors on 2 of 6 key_0 slots, those
   pairs were added to `exclude_pairs` and **never removed** during the 429 retry loop. The 502
   was transient (gateway restart) and recovered within minutes, but the task permanently lost
   those 2 slots — reducing its pool from 6 to 4.

2. **502→429 transition in `stream_chat`**: The 3rd key_0 slot got 4×502, then the 5th retry
   returned 429 instead. `stream_chat` raised `RateLimitError` (not `RetryableAPIError`), so
   `dispatch_stream` didn't increment `hard_attempts` (stayed at 2 < max_retries=3), and the
   loop continued forever.

Meanwhile, task a9069e7d succeeded immediately on `key_0:aws.claude-opus-4.6` — the exact slot
that 12bb98d7 had permanently excluded.

## Fix (v3)

### 1. Fast 0.3s sleep (no backoff)
- Both `dispatch_stream` and `dispatch_chat` use `time.sleep(0.3)` for 429 retries
- Fast polling = grab slots the instant they become available in contention scenario
- Slot cooldown (0.5s) naturally rotates between slots

### 2. Periodic exclusion reset every 60s
- During 429 cycling, `exclude_pairs`, `exclude_keys`, and `exclude` (models) are cleared every 60s
- This gives recovered slots (e.g. after 502 gateway restart) another chance
- If still broken, they'll get re-excluded quickly on the next attempt
- `hard_attempts` is NOT reset — genuinely broken slots still count toward retry limit
- Tracked via `_last_exclusion_reset = time.monotonic()`

### 3. 429 response body logging
- Log error body on first 3 cycles and every 100th for diagnostics
- Previously, 429 was logged without the response body, making diagnosis impossible

### 4. Stream read timeout
- Reduced from `timeout=(1200, 3000)` to `timeout=(60, 300)` in `lib/llm_client.py`
- Prevents 50-minute hangs when API accepts connection but never sends data

### 5. Key behavior (unchanged)
- `hard_attempts` counts only non-429 errors
- `_429_count` tracks 429 cycles for logging
- `_MAX_429_CYCLES = 0` (disabled) — no upper cap, user cancels via abort_check
- Slot cooldown `0.5s` from `record_error(is_rate_limit=True)` steers picker naturally

