---
name: stream-anomaly-detection-fingerprints
description: SSE anomaly fingerprints &amp; retry buckets in lib/tasks_pkg/stream_handler.py — zero_byte / classic_premature / empty_stop, plus _chunks_received signal
enabled: true
tags: [streaming, llm_client, stream_handler, retry, anomaly]
created: 2026-05-18T16:01:37Z
updated: 2026-05-21T15:59:19Z
---

# SSE Stream Anomaly Detection — Reference

`lib/tasks_pkg/stream_handler.py` `analyse_stream_result()` distinguishes
THREE qualitatively different upstream-failure fingerprints. Each has its
own retry budget and detection predicate. Don't conflate them.

## Buckets

| Bucket | Cap | When | Cost of retry |
|---|---|---|---|
| `zero_byte` | 16 | `_chunks_received == 0` OR stub response (chunks ≤ 5, empty_stop, no content/thinking, <60s) | Free — no tokens generated |
| `empty_stop` | 2 | `_empty_stop` flag + content empty + NOT zero-byte (model emitted thinking, then finish=stop with no body) | Moderate — cache reads + new thinking |
| `classic` (premature_close) | 2 | content empty + thinking > 1000 chars (or stream-anomaly empty on round > 0) | Expensive — full thinking-token cost |

All three increment the SAME `_premature_retry_count` counter. The bucket
chosen for an event determines which cap is compared against the counter.

## Stub response pattern (2026-05-21 fix)

Gateway (sankuai → Vertex) sometimes returns structurally valid but empty
completions: 2 SSE chunks (role + stop), `finish_reason=stop`,
`prompt_tokens=1` (impossible for 175K-token request), `completion_tokens=2`,
3-5s elapsed. This was misclassified as `classic` (2 retries) because
`_chunks_received=2` failed the `== 0` check. Fixed to classify as
`zero_byte` when: `_empty_stop && _chunks_received <= 5 && elapsed < 60s
&& no content && no thinking`. Gets 16 retries with exponential backoff
+ slot rotation.

## Required usage[] fields propagated by lib/llm_client.py

- `_chunks_received` — real SSE chunk count (0 = true zero-byte gateway hang)
- `_stream_anomaly` — set when ANY of `_missing_done`, `_missing_finish_reason`, `_empty_stop`
- `_missing_done`, `_missing_finish_reason`, `_empty_stop` — granular flags
- `stream_elapsed_ms` — wall-clock time before stream closed
- `trace_id`, `resp_trace_id` — for upstream coordination (M-TraceId)

If `_chunks_received` is missing (older client), the analyser falls back to
the legacy `len(thinking) < 100 + elapsed < 60s` heuristic.

## Production fingerprints (logs/raw_sse_anomaly.log)

- **vertex.claude-opus-4.6 via Sankuai gateway**: stub response pattern —
  2 chunks, empty_stop, prompt_tokens=1, 3-5s elapsed. Gateway returns
  valid-looking completion but model never ran inference.
- **aws.claude-opus-4.7 via Sankuai gateway**: dominant offender — zero-byte
  + missing_done + empty_stop. Often 5-40 s elapsed. The 5-min Bedrock
  budget seems to interact badly with Sankuai's gateway proxy.
- **MiniMax-M2.7**: classic premature-close after 200-300 KB of thinking,
  ~5-15 minutes elapsed. Model genuinely struggles with large prompts.
- **GLM-5.1**: empty_stop after thinking-only response. Retrying once
  usually works.

## Diagnostic tools

- `python3 debug/repro_stream_anomaly.py --n 8 --model aws.claude-opus-4.7`
  exercises the gateway directly with a raw key.
- `logs/raw_sse_anomaly.log` — server-side dumps with ring buffer of last
  ~32 SSE lines; grep on `M-TraceId` from user-visible error to find it.

## Frontend display

`finishReason` values rendered as user-visible badges (static/js/ui.js):
- `premature_close` → "网关中断" (low cap exhausted)
- `abnormal_stop` → "异常中断" (zero-byte / empty-stop / partial anomaly)
