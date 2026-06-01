---
name: llm-package-split
description: lib/llm_client.py fully deleted; all code moved to lib/llm/ package (no compat shim)
enabled: true
tags: [refactoring, architecture, llm]
created: 2026-05-21T16:48:30Z
updated: 2026-05-21T16:58:03Z
---

# lib/llm/ Package Structure (replaces deleted lib/llm_client.py)

Completed 2026-05-21. The monolithic 2101-line `lib/llm_client.py` was decomposed and **deleted** (no shim):

```
lib/llm/
  __init__.py       — Package facade, re-exports all 45 public symbols
  _transport.py     — Retry config, headers(), chat_url(), abortable_sleep()
  body.py           — build_body(), _validate_image_blocks(), _downscale_oversized_images(), _strip_trailing_assistant_for_claude()
  cache.py          — add_cache_breakpoints(), _gateway_honors_cache_markers()
  chat.py           — chat() non-streaming completion
  stream.py         — stream_chat(), _stream_chat_once()
  diagnostics.py    — RawSSEDumper (ring buffer + opt-in transcript)
```

## Import Convention (ALL code)
```python
from lib.llm import build_body, stream_chat, chat, add_cache_breakpoints
from lib.llm import AbortedError, RateLimitError, ContentFilterError
from lib.llm import is_claude, _clamp_max_tokens
```

## `lib/llm_client.py` is DELETED
- `import lib.llm_client` raises `ImportError`
- No backward-compat shim exists
- All callers (lib/, routes/, tests/, debug/, benchmarks/) have been migrated

## Peer modules (unchanged, still standalone)
- `lib/llm_errors.py` — Exception classes + HTTP error classifier
- `lib/llm_sanitize.py` — Message sanitization helpers
- `lib/model_info.py` — Model detection + token limits
- All three are re-exported from `lib.llm.__init__` for convenience

