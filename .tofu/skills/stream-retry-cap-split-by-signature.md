---
name: stream-retry-cap-split-by-signature
description: Retry budget split: zero-byte gateway anomaly (16) vs classic premature close (2)
enabled: true
tags: [streaming, retry, stream-anomaly, gateway, orchestrator, stream_handler]
created: 2026-04-19T16:12:17Z
updated: 2026-04-19T16:12:17Z
---

# Stream-retry budget split (lib/tasks_pkg/stream_handler.py)

As of 2026-04-19, `analyse_stream_result` distinguishes TWO abnormal-stop
signatures with different retry caps:

| Signature | Detection | Cap constant | Value | Rationale |
|---|---|---|---|---|
| **zero_byte** | `_is_anomaly_empty AND len(round_thinking)<100 AND stream_elapsed_ms<15000` | `_PREMATURE_RETRY_MAX_ZERO_BYTE` | **16** | Gateway cut SSE before any token. Each retry ~free (no tokens spent). Drops "all fail" prob from 2.7% to ~0 at 30% per-call failure. |
| **classic** | `_is_classic_premature` (no content + thinking>1000) OR anomaly_empty with big thinking | `_PREMATURE_RETRY_MAX_CLASSIC` | **2** | Substantial thinking already produced. Retry costs 20-60s of tokens. Don't loop on a model genuinely struggling. |

## Frontend UX
Phase event fields emitted: `phase='retrying', attempt=N, max=cap, bucket=zero_byte|classic, detail=<i18n msg>`.
Renderer at `static/js/ui.js:3906` uses `phase.attempt` as dedup key and renders `phase.detail` with `⟳` icon via `.stream-phase-retrying` class.

## Invariants to preserve
- Both caps live at module top of `lib/tasks_pkg/stream_handler.py` — easy to locate.
- Never reduce `PREMATURE_RETRY_MAX_CLASSIC` below 2 without discussion (that was the historical min).
- Any change to either constant is a §10.1 hyperparameter change → requires user approval + `audit_log('config_change', ...)`.
- Zero-byte detection thresholds (100 chars thinking, 15000ms elapsed) are heuristic; if failure signatures shift, tune them with supporting logs.

## Triggering conditions observed in the wild
- `aws.claude-opus-4.7` via sankuai gateway: sporadic 0-byte closes, 6-8s elapsed, `chunks_received=0`, `finish_reason=stop`, `_stream_anomaly=True`.
- Pattern: ~30% per-call failure rate during bad minutes, succeeds between failures — proves it's upstream transient, not payload/context size.

## Debug/history
- Prior cap was `2` uniformly (hardcoded `< 2` / `>= 2` literals).
- Conv `mo59j6h7vp3ayv` task `2ff8ace8` exhausted retries at rounds 6/8/11 — motivation for this split.

