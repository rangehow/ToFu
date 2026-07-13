---
name: claude-image-dimension-limits-auto-downscale
description: Claude vision tower processes at ~1568px; _downscale_oversized_images now uses ONE uniform 1568px cap (not two-tier 8000/2000) to kill the ≥5-image cache-cliff; hard 400s at 8000 single / 2000 many still avoided
enabled: true
tags: [python, claude, image, http-400, dispatch, auto-fix]
created: 2026-04-05T05:05:39Z
updated: 2026-07-10T15:14:59Z
---

# Claude Image Dimension Limits & Auto-Downscale

## Hard API limits (why the downscale exists at all)
Claude rejects oversized images with HTTP 400:
- **Single image**: max 8000px longest dimension
- **Many-image (5+ images)**: max 2000px longest dimension
```
...image dimensions exceed max allowed size for many-images requests: 2000 pixels
```

## KEY FACT: the model only ever sees ~1568px
Claude's vision tower internally downscales EVERY image to a ~1568px long edge
(~1.15 MP) before tokenization (Anthropic vision docs). So an image sent at
7999px conveys the SAME information as one at 1568px — the larger size is pure
wire waste. Token cost ≈ (w×h)/750.

## Current design (2026-07): ONE uniform 1568px cap
`_downscale_oversized_images(messages, model)` in **`lib/llm/body.py`**:
- Only for Claude (`is_claude()`); requires Pillow.
- `_CLAUDE_IMAGE_MAX_PX = 1568`, **count-independent** (single constant now —
  the old `_CLAUDE_SINGLE_IMAGE_MAX_PX`=7999 / `_CLAUDE_MANY_IMAGE_MAX_PX`=1999
  / `_CLAUDE_MANY_IMAGE_THRESHOLD`=5 are GONE).
- Resize LANCZOS; re-encode JPEG q=85 (PNG if real alpha). `int()` floor →
  result lands on cap OR cap-1; invariant is `<= cap`.
- Idempotent: `max(w,h) <= max_px` → skip, so an at-cap image is byte-identical
  across rounds.
- Called from `build_body()` after `_validate_image_blocks()`, and on the
  `dispatch_stream()` pre-built-body path when dispatch swaps to Claude.

## Why the old two-tier design was replaced (cache-cliff RCA, 2026-07)
The ≥5-image threshold dropped max_px 7999→1999 the round the 5th image
arrived, RETROACTIVELY re-encoding images 1–4 that were already prompt-cached →
guaranteed cache miss + a per-round WARNING, for ZERO quality gain (model caps
at 1568 anyway; uploads already shrunk to 2048px on ingest so it was a 49px
destructive shrink). A single 1568px cap removes the cliff (one-shot idempotent
shrink), loses no quality, and stays under BOTH hard limits so neither 400 can
fire. Guarded by `tests/test_image_downscale_uniform_cap.py` (the cache-cliff
NC: 4 capped images + a 5th oversized one → first four byte-identical).

## Related paths (unchanged)
- `executor_image.py::_downsize_for_llm` — generated images go to the chat LLM
  as a 1024px thumbnail; full-res stays on disk / in `meta.imageDataUri`.
- Detection: `InvalidImageError` (raised on HTTP 400 image-dimension patterns)
  is handled like `ContentFilterError` — no retry on other keys/models (same
  payload = same rejection); `lib/tasks_pkg/llm_fallback.py` returns a
  user-friendly message, no model fallback.

