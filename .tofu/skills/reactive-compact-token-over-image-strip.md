---
name: reactive-compact-token-over-image-strip
description: reactive_compact Phase 0 trigger must OR token-over-limit with wire-byte-over (big base64 images blow 1M tokens while fitting under 4MB)
enabled: true
tags: [compaction, images, bugfix, 2026-05]
created: 2026-05-04T16:11:06Z
updated: 2026-05-04T16:11:06Z
---

# reactive_compact Phase 0: trigger on tokens OR wire-bytes (2026-05-04)

## Bug
`lib/tasks_pkg/compaction.py::reactive_compact` originally gated Phase 0
(`_strip_images_aggressive`) only on `wire_before > _WIRE_BYTE_SOFT_LIMIT`
(4 MB). Large Claude/Vertex base64 images can inflate to 1.3M tokens
while the raw body is still ~2.7 MB — under the wire soft limit. Result:
token-over HTTP 400s caused by images never triggered image stripping,
so the retry dropped only ~5% (head truncate refuses to drop the last
4 messages) and the conversation wedged FATAL.

Concrete case: task=26204914 / conv=mo4fr5xe on 2026-05-04 23:46 — hit
`Prompt too long: 1310784 tokens > 1000000` with wire_bytes=2.7MB.

## Fix
Compute `token_over = _estimate_total_tokens(messages) > int(context_limit * 0.95)`
BEFORE Phase 0 and OR it with `over_wire`. Log `trigger=wire|tokens|wire+tokens`
for post-mortem clarity. 0.95 threshold chosen so we only strip when the
request is demonstrably over — not merely large.

## Relevant constants
- `_WIRE_BYTE_SOFT_LIMIT = 4 MB`
- `_WIRE_IMAGE_KEEP_TAIL = 2` (tail images preserved)
- `_get_context_limit(task)` honors model (qwen=128k, claude-4.6+=1M, …)
- `_estimate_msg_tokens` charges images a fixed `_IMAGE_TOKENS_HIGH=800`
  per image — NOT base64 length.

## Test
`debug/test_compaction_reactive_image_strip.py` — synthetic messages
with 500k chars text (~125k tokens) + 3 images on a qwen task. Asserts
tokens > threshold AND wire < soft limit, calls `reactive_compact`,
verifies oldest image got placeholder-replaced while tail images
survived (index-independent walk — force_compact may drop cold turns).

