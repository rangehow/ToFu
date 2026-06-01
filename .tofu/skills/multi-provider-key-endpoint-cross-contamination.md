---
name: multi-provider-key-endpoint-cross-contamination
description: Critical bug pattern: multi-provider LLM dispatch sends API key from provider A to provider B's endpoint — every code path that receives (api_key, base_url) from dispatch MUST forward BOTH to the HTTP call; hardcoded base URLs in image_gen/embeddings must derive from slot.base_url
enabled: true
tags: [python, multi-provider, api-key, cross-contamination, dispatch, critical-bug, image-gen, embeddings]
created: 2026-03-31T05:44:03Z
updated: 2026-03-31T05:44:03Z
---

# Multi-Provider Key/Endpoint Cross-Contamination

## Bug Pattern

When a project has multiple LLM providers (e.g., yeysai.com + aigc.sankuai.com),
each with its own API keys, the dispatch system picks the best `(api_key, base_url)`
slot per request. **Every downstream call MUST forward BOTH** `api_key` and `base_url`
from the slot — if either is omitted, the code falls back to module-level defaults
(`LLM_API_KEY` / `LLM_BASE_URL`) which belong to a DIFFERENT provider.

## Bugs Found (2026-03-31)

### 1. `dispatch_fastest._race_worker()` — missing `base_url`
```python
# ❌ BUG: passes api_key from sankuai slot, but base_url falls back to yeysai
content, usage = chat(
    api_key=slot.api_key,
    # base_url NOT passed → _chat_url() → LLM_BASE_URL (yeysai)
)

# ✅ FIX
content, usage = chat(
    api_key=slot.api_key,
    base_url=slot.base_url or None,  # ← MUST pair with api_key
)
```

### 2. `image_gen.py` — hardcoded `_FRIDAY_BASE = 'https://aigc.sankuai.com'`
```python
# ❌ BUG: dispatch picks yeysai slot, but URL is hardcoded to sankuai
url = f'{_FRIDAY_BASE}/v1/openai/native/images/generations'
headers = {'Authorization': f'Bearer {yeysai_api_key}'}  # key goes to wrong host!

# ✅ FIX: derive FRIDAY base from slot's base_url
from urllib.parse import urlparse
def _friday_base_from_slot(slot):
    if slot and slot.base_url:
        p = urlparse(slot.base_url)
        return f'{p.scheme}://{p.netloc}'
    return _FRIDAY_BASE_DEFAULT
```

### 3. `embeddings.py` — fallback to `LLM_BASE_URL` when slot.base_url is empty
```python
# ⚠️ RISK: if slot has empty base_url, sankuai key goes to yeysai endpoint
if not base:
    from lib import LLM_BASE_URL
    base = LLM_BASE_URL.rstrip('/')
    logger.warning('Slot %s has no base_url — may cause key/endpoint mismatch!', ...)
```

## Audit Checklist

When reviewing multi-provider dispatch code:
- [ ] Every `chat()` / `stream_chat()` call with `api_key=slot.api_key` ALSO passes `base_url=slot.base_url`
- [ ] No hardcoded base URLs that override the slot's provider
- [ ] Fallback paths use consistent (key, url) pairs from the SAME provider
- [ ] Image gen / embedding / translation helpers derive their base URL from the slot
- [ ] `_headers()` default key matches `_chat_url()` default URL (same provider)

