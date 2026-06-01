---
name: embeddings-dispatch-routing-fix
description: Bug fix: lib/embeddings.py used hardcoded LLM_BASE_URL (resolved at import time to first provider) for all embedding requests — when first provider DNS fails, ALL embeddings fail; fix: use dispatch system _pick_embedding_slot() for per-request provider routing with correct api_key/base_url
enabled: true
tags: [python, embeddings, dispatch, multi-provider, bug-fix, dns-resolution, routing]
created: 2026-03-31T04:29:52Z
updated: 2026-03-31T04:29:52Z
---

# Embeddings Dispatch Routing Fix

## Bug
`lib/embeddings.py` built `_EMBED_URL` at module import time from `LLM_BASE_URL`:
```python
_EMBED_URL = f'{LLM_BASE_URL.rstrip("/")}/embeddings'
```

When multi-provider config exists in `~/.chatui/server_config.json`, `_resolve_base_url()` in
`lib/__init__.py` picks the **first enabled provider's** base_url. If that provider is
unreachable (e.g. DNS resolution failure for `yeysai.com`), ALL embedding requests fail —
even though another provider (Meituan) has the embedding model available and working.

Additionally, the old key round-robin cycled through ALL `LLM_API_KEYS` (flat list from
all providers), sending the wrong provider's key to the wrong base URL.

## Root Cause
- `_EMBED_URL` was a module-level constant, frozen at import time
- Key round-robin (`itertools.cycle(LLM_API_KEYS)`) mixed keys from different providers
- No provider-aware routing — unlike `llm_client.py` which uses dispatch slots with `base_url`

## Fix
Replaced the hardcoded URL + key cycling with `_pick_embedding_slot(model)`:

```python
def _pick_embedding_slot(model: str):
    """Use dispatch system to find correct (api_key, base_url) for embeddings."""
    from lib.llm_dispatch.factory import get_dispatcher
    dispatcher = get_dispatcher()
    slot = dispatcher.pick_and_reserve(capability='embedding', prefer_model=model)
    if slot:
        base = slot.base_url.rstrip('/') if slot.base_url else LLM_BASE_URL.rstrip('/')
        return slot.api_key, base, slot.key_name, slot
    # Fallback to global config
    return LLM_API_KEYS[0], LLM_BASE_URL.rstrip('/'), 'key_0', None
```

Each batch call now:
1. Picks the best available embedding slot via dispatch (correct provider)
2. Uses that slot's `base_url` (not the global one)
3. Uses that slot's `api_key` (matching the provider)
4. Reports success/error back to the slot for proper load balancing

## Impact
- 98+ embedding errors per day → 0
- Search reranking (semantic) works correctly again
- Skills semantic search works

