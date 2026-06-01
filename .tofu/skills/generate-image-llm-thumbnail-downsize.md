---
name: generate-image-llm-thumbnail-downsize
description: generate_image tool downsizes a separate thumbnail for the LLM wire; full-res stays on disk + in meta.imageDataUri
enabled: true
tags: [image-gen, gateway-413, executor_image]
created: 2026-05-05T05:21:11Z
updated: 2026-05-05T05:21:11Z
---

# generate_image — LLM-wire Thumbnail vs Full-Res On Disk

## Problem (conv=mos43adg4sg3es, 2026-05-05)

A single 2K PNG from `generate_image` (gemini-3-pro-image-preview) was ~12 MB
raw / ~16 MB base64 / **~15.7 MB after JSON serialization** → instant HTTP 413
at the sankuai openresty gateway (4 MB cap). `ReactiveCompact` couldn't save
it because head-truncate can't drop the *latest* tool result (which is why we
called the LLM in the first place). Same payload on retry → same 413. Task
ended with `finishReason=error`, content=0 chars.

Key insight: this is NOT a token-count problem (compactor reported ~2104
tokens after truncate) — it's a **wire-byte** problem. See memory
`gateway-413-image-wire-size-fix`.

## Solution (lib/tasks_pkg/executor_image.py)

**Two copies of the image with different lifetimes:**

| Copy | Used by | Size |
|---|---|---|
| Full-res PNG | Disk (`uploads/images/`), project path, `round_entry.results[0].imageDataUri` (frontend render + intra-turn history) | ~12 MB |
| Downsized JPEG/PNG thumbnail | Chat-LLM tool_content (`dataUrl`) — goes into request body | ~200 KB |

**Why it's safe**: Claude's vision tower internally downsamples anything to
≤1568 px before tokenization — a 2K image is pure wire waste.

## Implementation

`_downsize_for_llm(image_b64, mime_type)` in `lib/tasks_pkg/executor_image.py`:
- `_LLM_THUMB_MAX_PX = 1024` (long side)
- `_LLM_THUMB_JPEG_QUALITY = 85`
- LANCZOS resize
- Opaque → JPEG q=85 progressive; real-alpha RGBA → PNG
- Real-alpha probe: `img.getchannel('A').getextrema()[0] < 255` (ignores
  degenerate RGBA where alpha is all 255, same trick as upload.py
  `_shrink_upload_image`)
- Returns `(b64, mime)` unchanged on any failure (Pillow missing / decode
  error / no savings)

In `_handle_generate_image` on success path, before building `tool_content`:

```python
thumb_b64, thumb_mime = _downsize_for_llm(image_b64, mime_type) if image_b64 else (image_b64, mime_type)
thumb_data_uri = f'data:{thumb_mime};base64,{thumb_b64}' if thumb_b64 else data_uri

tool_content = {
    '__screenshot__': True,
    'dataUrl': thumb_data_uri,         # ← thumbnail (LLM wire)
    ...
    'compressionApplied': (thumb_b64 != image_b64),
    '_text_fallback': '\n'.join(fallback_parts),
}
```

**Critical**: `meta['imageDataUri']` in `round_entry.results[0]` keeps the
FULL-res `data_uri`, NOT the thumbnail. This is what `_extract_image_gen_history`
reads (phase 2) for intra-turn multi-turn editing → image-gen API gets full
fidelity for edits. The chat-LLM gets the thumbnail.

## Cross-turn history caveat

`_extract_image_gen_history` phase 1 still reads the conversation message
(which now contains the thumbnail). For now that's acceptable — editing
fidelity on the *same* turn uses phase 2 (full-res); fidelity on a *later*
turn ("make the cat orange next time") degrades to thumbnail quality.
If that ever matters, phase 1 could be rewritten to look up the full-res
via `imageSavedUrl` from disk instead of from the `image_url` block.

## Smoke test

```python
from lib.tasks_pkg.executor_image import _downsize_for_llm
import base64, io
from PIL import Image
# 2048x1536 noisy PNG → ~220 KB JPEG (9× smaller for noise; ~70× for real photo).
```

## Files touched
- `lib/tasks_pkg/executor_image.py` — `_downsize_for_llm` helper + wire it into
  the tool_content build in `_handle_generate_image`.

