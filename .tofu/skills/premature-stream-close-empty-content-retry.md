---
name: premature-stream-close-empty-content-retry
description: Bug fix: Stream anomaly detection covers 3 cases — (A) classic premature close with big thinking, (B) early-kill anomaly with no content, AND (C) anomaly with partial content (the mnffj4y5dez0za blind spot where 8278 chars of content caused `not round_content.strip()` to skip all anomaly checks, silently showing ✓ instead of 异常中断)
enabled: true
tags: [python, javascript, debugging, llm-client, orchestrator, streaming, proxy, timeout, abnormal-stop, stream-anomaly]
created: 2026-03-19T02:12:52Z
updated: 2026-04-01T03:33:04Z
---

# Stream Anomaly → Abnormal Stop Detection & Retry

## Problem
Proxy/gateway can sever SSE connections in three ways:
1. **Classic premature close**: Deep into thinking (>1000 chars) when cut off → no content, no tool_calls
2. **Early kill** (the `mnbvo192q8u0zo` bug): Connection severed so early that even thinking barely started (<100 chars). Only symptom: `MISSING FINISH_REASON` + `EMPTY STOP RESPONSE` warnings in llm_client, but orchestrator treated it as normal "stop".
3. **Partial content cut** (the `mnffj4y5dez0za` bug): Gateway cut connection after 671s and 969 chunks, producing 8278 chars of content but **no `[DONE]` marker and no `finish_reason`**. Since `finish_reason` defaults to `'stop'` and all three anomaly checks required `not round_content.strip()`, the response was silently treated as a normal completion, showing "✓" instead of "异常中断".

## Fix (3-layer)

### Layer 1: `llm_client.py` — Flag propagation
Inject stream anomaly flags into `usage` dict:
- `_missing_done` — Server never sent `[DONE]` marker
- `_missing_finish_reason` — `[DONE]` received but no finish_reason chunk
- `_empty_stop` — finish=stop but 0 content and 0 tool_calls
- `_stream_anomaly` — any of the above (convenience flag)

### Layer 2: `stream_handler.py` — Detection logic
Three tiers of detection:

**Retry-eligible (up to 2 retries):**
- **(A) Classic**: `not round_content.strip() and len(round_thinking) > 1000`
- **(B) Early kill**: `not round_content.strip() and _stream_anomaly and round_num > 0`

**Non-retryable (break immediately with `abnormal_stop`):**
- **(C) Partial content anomaly** (NEW): `_stream_anomaly` flag is set but content exists → don't retry (content would be lost), but DO expose the anomaly. The old code had `if _stream_anomaly and not round_content.strip()` which silently skipped when content was present, falling through to "Normal exit".

**Fix:** Changed the final `_stream_anomaly` check from requiring `not round_content.strip()` to just `if _stream_anomaly:` — catches both empty AND partial-content anomalies.

### Layer 3: `ui.js` — Visible warning
- `abnormal_stop` in `warnReasons` array → yellow warning tag
- Display label: `⚠ 异常中断` with tooltip explaining proxy/gateway interruption

## Key insight
All anomaly detection checks used `not round_content.strip()` as a precondition, creating a blind spot where truncated responses WITH content bypassed every check and fell through to "Normal exit" → `finish_reason=stop` → UI shows "✓". The `finish_reason` variable defaults to `'stop'` at initialization and is only updated IF the server sends a finish_reason chunk — when the gateway cuts the connection before that, the default `'stop'` is used, making it look normal.

