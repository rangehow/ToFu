---
name: zero-byte-round0-retry-fix
description: Round-0 empty stop is unconditionally routed to the empty_stop/zero_byte retry bucket via _flag_empty_stop_for_retry; the old content_filter label was DELETED. Genuine block = HTTP-450 only.
enabled: true
tags: [streaming, retry, gateway, bugfix]
created: 2026-05-12T06:19:55Z
updated: 2026-07-03T04:58:33Z
---

# Round-0 Empty/Zero-Byte Stop Retry — llm_fallback.py + stream_handler.py

> **HISTORY / SUPERSEDED (read this first).** The 2026-05-12 version of this
> skill said "both the primary and fallback CONTENT_FILTER detectors now SKIP
> when `usage._stream_anomaly` is True". **That code no longer exists.** As of
> 2026-07-03 the `content_filter` LABEL for an empty round-0 stop was DELETED
> entirely from `lib/tasks_pkg/llm_fallback.py` and replaced by
> `_flag_empty_stop_for_retry`. Do NOT go looking for a `content_filter`-defer
> branch — there isn't one, and don't reintroduce the terminal label.

## The problem (original 2026-05-12 + the residual it left)
An `aws.claude-opus-4.x` via sankuai gateway TRANSIENT empty response on
round 0 (`finish_reason=stop`, no content) was mislabeled a terminal
`content_filter` (safety block) — surfaced "🚫 CONTENT_FILTER detected", NO
retry, blank bubble, "can't continue". The 2026-05-12 fix made the label
DEFER when `_stream_anomaly` was already set. But `lib/llm/_sse_core.py:813`
only sets `_empty_stop`/`_stream_anomaly` when `finish=stop AND not content
AND chunk_count > 0`. So an UNFLAGGED empty stop still hit `content_filter`:
  - **zero-chunk clean `[DONE]`** (`_chunks_received==0`, the production case),
    which the stream layer does NOT flag, AND
  - **whitespace-only body** (`content` truthy so `not content` is False, but
    it `.strip()`s to empty).
Proven transient by `debug/repro_conv_empty_stop.py` (replays the exact
~737k-token request that empty-stopped in prod → clean content 6/6).

## Current mechanism (2026-07-03 — root fix)
`lib/tasks_pkg/llm_fallback.py` — `_flag_empty_stop_for_retry(assistant_msg,
finish_reason, task, round_num, usage)` is a pure helper called at BOTH the
primary and fallback-model sites (replacing the old content_filter heuristic
at both). It returns True and MUTATES `usage` (sets `_empty_stop=True` +
`_stream_anomaly=True`) when ALL hold:
  - `finish_reason == 'stop'`
  - `round_num == 0` (empty content after tool calls on later rounds is legit)
  - `assistant_msg.content` strips to empty
  - `task['content']` AND `task['thinking']` strip to empty (a Continue
    contentPrefix seed is NOT an empty round → left alone)
  - `usage['_stream_anomaly']` is NOT already set (if the stream layer flagged
    it, the existing machinery handles it → helper is a no-op)
Setting those flags routes the round through `analyse_stream_result`'s
retry buckets instead of a terminal break. Only after the retries exhaust
does it surface as **`abnormal_stop`** (honest transient label) — NEVER a
fabricated `content_filter`.

## A GENUINE policy block is HTTP-450 ONLY
`ContentFilterError` (HTTP 450) is raised by the transport and caught
separately in `llm_fallback.py` (its own `if isinstance(e, ContentFilterError)`
branch) → returns `finish_reason='content_filter'`, terminal, no retry. That
is the ONLY path that should ever yield `content_filter`. A 200-OK empty
`stop` is never a policy block.

## WHICH retry bucket (confirmed empirically 2026-07-03)
`analyse_stream_result` checks `_is_zero_byte` FIRST, and the `empty_stop`
branch explicitly requires `not _is_zero_byte`, so:
  - **Production zero-chunk case** (`_chunks_received==0`, no content, no
    thinking) → **`zero_byte` bucket, cap 16** (`_PREMATURE_RETRY_MAX_ZERO_BYTE`).
    Correct: no tokens were generated, retry is ~free.
  - Model emitted chunks/thinking but empty/whitespace body (`_empty_stop`
    without zero-byte) → **`empty_stop` bucket, cap 2** (`_EMPTY_STOP_RETRY_MAX`).
  - `stub response` (`_empty_stop && _chunks_received<=5 && <60s`) also →
    `zero_byte` (cap 16). See `stream-anomaly-detection-fingerprints`.
`stream_handler.py` predicate order is unchanged from 2026-05-12 (zero-byte
computed first, no round guard; anomaly_empty keeps `round_num>0`).

## Tests
- `tests/test_empty_stop_not_content_filter.py` (2026-07-03, 9 cases):
  helper unit branches + END-TO-END (flagged usage → `analyse_stream_result`
  returns `action='continue'`, NOT terminal; surfaces `abnormal_stop` not
  `content_filter` when the budget is exhausted). Double-neutered: reverting
  the flag-set → the 4 asserting tests fail; restored byte-identical.
- `tests/test_retry_budget_envelope.py`, `tests/test_stream_anomaly_retry_widening.py`,
  `tests/test_sse_core_parity.py` — the pre-existing bucket/anomaly-flag suites
  (still green; the fix did not change `analyse_stream_result` or `_sse_core`).
- The legacy `tests/test_zero_byte_round0_retry.py` predates the 2026-07-03
  change and asserts the OLD stream_handler predicates (still valid there).

## Evidence
Prod: conv `mr3jfcw10pianj`, tasks e.g. `48aee449` — round-0 `finish=stop
content=0chars` logged "🚫 CONTENT_FILTER detected", finalized empty, user
could not continue. Replay probe served the identical request cleanly 6/6.

