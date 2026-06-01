---
name: gateway-413-image-wire-size-fix
description: Gateway HTTP 413 is a BYTE limit (not token limit); fix with upload-time shrink + reactive wire-byte compaction
enabled: true
tags: [bug-fix, 413, compaction, images, upload, wire-size]
created: 2026-04-29T10:40:51Z
updated: 2026-04-29T10:40:51Z
---

# Gateway HTTP 413 vs Upstream Token Limit — Two Different Dimensions

## Key insight (conv=mofu0tfayzvuv1, 2026-04-29)

An HTTP 413 from openresty/aigc.sankuai.com is a **wire-byte limit** at the gateway,
NOT an upstream token-count limit. They're orthogonal:

- **Upstream image cost**: `tokens ≈ (W × H) / 750` — pixel-dimension-based,
  independent of byte size. Verified via `debug/probe_image_tokens.py`:
  - 1024×510 PNG (442 KB) → 706 upstream tokens
  - 1024×1024 PNG (5 KB) → 1,372 upstream tokens (bigger tokens, smaller file!)
- **Gateway wire limit**: measured in bytes of the serialized JSON body.
  openresty's `client_max_body_size` rejects before upstream ever tokenizes.

**Token estimate can say "170K tokens, we're fine" while the wire body is 5+ MB
and the gateway rejects.** Our `_IMAGE_TOKENS_DEFAULT = 800` is correct for
upstream billing but blind to wire size.

## Fix architecture (two layers)

### Layer A — upload-time shrink (routes/upload.py)
`_shrink_upload_image()` re-encodes uploads:
- Max 2048 px long side (Claude internally downsamples to ~1568 px anyway)
- JPEG q=90 for opaque content, PNG for true alpha
- Detects degenerate RGBA (alpha=255,255) from screenshots → drops to JPEG
- Skips if already ≤1600 px AND ≤400 KB
- Never touches GIF (animated) / BMP
- Real 442 KB offender → 191 KB (2.3× shrink, zero visible quality loss)

Constants:
- `MAX_UPLOAD_LONG_SIDE_PX = 2048`
- `JPEG_REENCODE_QUALITY = 90`
- `SHRINK_SKIP_LONG_SIDE_PX = 1600`
- `SHRINK_SKIP_MAX_BYTES = 400 * 1024`

### Layer C — wire-aware reactive_compact (lib/tasks_pkg/compaction.py)

Before Phase 1 (micro_compact), check if wire bytes > 4 MB soft limit.
If so: call `_strip_images_aggressive(keep_tail=2)` to remove base64 images
from all but the 2 most-recent message positions (ignores hot-tail protection
because gateway has proven the payload too big).

After all phases, if wire bytes STILL over limit, `_head_truncate(byte_target=...)`
drops oldest messages by wire bytes not tokens.

Constants:
- `_WIRE_BYTE_SOFT_LIMIT = 4 * 1024 * 1024`
- `_WIRE_IMAGE_KEEP_TAIL = 2`

New helpers:
- `_estimate_wire_bytes(messages)` — `len(json.dumps(messages).encode('utf-8'))`
- `_strip_images_aggressive(messages, keep_tail)` — replaces image_url blocks
  with a `[image removed during emergency compaction]` text placeholder
- `_head_truncate(messages, task, byte_target=None)` — extended with byte mode

## Why reactive_compact was broken before

1. `_should_force_compact` checks tokens, estimated ~170K (fine).
2. Image blocks count as 800 tokens each (correct for billing) but 100s of KB
   on the wire.
3. `force_compact_if_needed` returned False → fell through to `_head_truncate`.
4. `_head_truncate` also checked tokens (170K < 600K target) → dropped nothing.
5. Retry was byte-for-byte identical to first attempt → same 413.

## Debugging script

`debug/probe_image_tokens.py` — empirical probe that sends a baseline text
request + images of various sizes to aws.claude-opus-4.7 and prints the
`usage.prompt_tokens` from each response. Proves the pixel-based token formula
and the wire-vs-token divergence.

