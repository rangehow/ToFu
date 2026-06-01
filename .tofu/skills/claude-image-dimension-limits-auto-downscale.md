---
name: claude-image-dimension-limits-auto-downscale
description: Claude API image limits: 8000px single / 2000px many-image (5+) — auto-downscale in build_body + InvalidImageError for non-retryable 400s
enabled: true
tags: [python, claude, image, http-400, dispatch, auto-fix]
created: 2026-04-05T05:05:39Z
updated: 2026-04-05T05:05:39Z
---

# Claude Image Dimension Limits & Auto-Downscale

## Problem
Claude API rejects images exceeding pixel dimension limits with HTTP 400:
- **Single image**: max 8000px on longest dimension
- **Many-image requests (5+ images)**: max 2000px on longest dimension

Error message pattern:
```
messages.1.content.2.image.source.base64.data: At least one of the image dimensions exceed max allowed size: 8000 pixels
```

## Bug: Infinite 429 Retry Loop
Before the fix, this 400 error was caught by the generic `except Exception` in `dispatch_stream()`, which:
1. Excluded only the (key, model) pair (with strict_model=True)
2. Retried with OTHER keys of the same model → got 429s → looped forever
3. 429 retries don't count toward hard_attempts → infinite loop

## Fix (3 layers)

### 1. Proactive: Auto-downscale in `build_body()`
`_downscale_oversized_images(messages, model)` in `lib/llm_client.py`:
- Only for Claude models (`is_claude()`)
- Counts total images across all messages
- Uses 8000px limit for <5 images, 2000px for ≥5 images
- Resizes using PIL/Pillow with LANCZOS resampling
- Re-encodes as JPEG (or PNG if RGBA) at quality=85

Called in:
- `build_body()` after `_validate_image_blocks()`
- `dispatch_stream()` pre-built body path when dispatch swaps to Claude

### 2. Detection: `InvalidImageError` exception
New exception class in `lib/llm_client.py`. Raised when HTTP 400 matches `_is_image_error()` patterns.

### 3. Dispatch: Don't retry
`InvalidImageError` handled like `ContentFilterError` — raises immediately without retrying on other keys/models (same payload = same rejection).

In `lib/tasks_pkg/llm_fallback.py`: returns user-friendly error message, no model fallback.

