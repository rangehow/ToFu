---
name: sankuai-gateway-sse-format
description: Sankuai AIGC gateway returns SSE data lines as 'data:{json}' (no space after colon), not 'data: {json}' like OpenAI
enabled: true
tags: [bug, sse, sankuai, gateway, streaming]
created: 2026-05-08T07:17:45Z
updated: 2026-05-08T07:17:45Z
---

# Sankuai Gateway SSE Format Bug

## Bug
The Sankuai AIGC gateway (`https://aigc.sankuai.com/v1/openai/native`) returns SSE data lines
in the format `data:{json}` (no space after the colon), while the standard OpenAI SSE format
is `data: {json}` (with a space). The `[DONE]` sentinel uses `data: [DONE]` (with space).

## Root Cause
In `lib/llm_client.py:2077`, the SSE parser used `line.startswith('data: ')` (6 chars) and
`line[6:]` to extract the JSON payload. This caused ALL data lines from the Sankuai gateway
to be silently skipped, resulting in empty `content` and `thinking_text`, which was then
misclassified as `content_filter` by `llm_fallback.py`'s empty-content-on-first-round detection.

## Fix
Changed to `line.startswith('data:')` (5 chars) and `line[5:].strip()` — this accepts both
formats and `.strip()` removes any leading space from the standard format.

## Impact
ALL streaming requests to AWS Claude models via the Sankuai gateway were affected — every
response appeared as "content filtered" because the content was always empty after SSE parsing.

## Date
2026-05-08
