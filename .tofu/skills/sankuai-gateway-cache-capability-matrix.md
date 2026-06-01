---
name: sankuai-gateway-cache-capability-matrix
description: Per-model cache behavior on aigc.sankuai.com gateway: 3 modes (needs-markers, auto-caches, markers-break-it) — Doubao MUST NOT get cache_control markers
enabled: true
tags: [caching, sankuai, gateway, minimax, doubao, glm, qwen, deepseek]
created: 2026-05-03T05:16:49Z
updated: 2026-05-03T05:16:49Z
---

# Sankuai Gateway — Per-Model Cache Capability Matrix

Tested 2026-05-03 via `debug/probe_nonclaude_cache.py` — two arms (no markers vs with `cache_control: ephemeral`), 2 sequential byte-identical requests per arm, ~10K token system prompt. Observed `usage.cache_read_tokens` directly.

## Results

| Model | Auto-cache (no markers)? | Markers honored? | Action in `add_cache_breakpoints` |
|---|---|---|---|
| `aws.claude-*`                | ❌ No   | ✅ Yes | **Add markers** (current behavior) |
| `glm-5.1`                     | ❌ No   | ✅ Yes | **Add markers** |
| `qwen3.5-plus`                | ❌ No   | ✅ Yes | **Add markers** |
| `deepseek-v4-flash`           | ❌ No   | ✅ Yes | **Add markers** |
| `MiniMax-M2.5`                | ✅ Yes  | ✅ Yes (no break) | **Skip** (auto-caches) |
| `MiniMax-M2.7`                | ✅ Yes  | ⚠️ inconsistent  | **Skip** (auto-caches) |
| `Doubao-Seed-2.0-pro`         | ✅ Yes  | ❌ **BREAKS** caching! | **Skip markers** |
| `LongCat-Flash-Thinking-2601` | ?       | ?       | Inconclusive (timeout) |

## Three distinct gateway behaviors

1. **Anthropic-style (claude/glm/qwen/deepseek)** — gateway caches only when `cache_control: ephemeral` is attached. Classic prompt-caching semantics.
2. **Auto-caching by prefix hash (minimax/doubao)** — gateway transparently caches the request prefix by hash on every request. No client work needed. Cache read/write show up in `usage.cache_read_tokens` without any markers.
3. **Markers break auto-caching (doubao)** — adding `cache_control` to a model in group 2 prevents the auto-cache path from firing. The gateway seems to route marker'd requests through a different code path that does NOT fall back to auto-caching. **Net result: 100% hit → 0% hit.**

## Implications for `lib/llm_client.py:add_cache_breakpoints`

Current guard `if not is_claude(model): return` leaves ~$$ on the table for GLM/Qwen/DeepSeek but correctly protects Doubao.

Correct fix is a three-way classifier:

```python
CACHE_MARKERS_HELP = ('claude', 'glm-5', 'qwen', 'deepseek')
CACHE_MARKERS_HARM = ('doubao',)  # auto-caches; markers break it
# MiniMax: auto-caches; markers are a no-op at best; skip to be safe.

if not any(k in model.lower() for k in CACHE_MARKERS_HELP):
    return
```

## SWE-bench impact estimate (MiniMax specifically)

The current 0% cache-hit rate on tofu-minimax is a **report-side ILLUSION** — the gateway IS auto-caching (the M2.5/M2.7 probe shows 99.7% auto-hit on req2). The tokens still flow at billing rates as if uncached because:

1. The gateway returns `cache_read_tokens` in the usage dict.
2. Our `swebench_runner.py` reads `inference['cache_read_tokens']` into `cache_read_tokens`, which gets priced at 0.2× in `_compute_cost`.
3. BUT: the Tofu frontend may not be extracting `cache_read_tokens` from MiniMax responses — need to verify by re-checking the per-round `usage` payload captured in results.

## Pricing note (from probe data)

All tested models charge `prompt_tokens` at full rate even when `cache_read_tokens > 0` — i.e. **sankuai gateway does NOT subtract cache_read from prompt_tokens** (unlike Anthropic native which reports uncached-only as `input_tokens`). Be careful when reconciling billing: `prompt_tokens` is the total, and `cache_read_tokens` is INCLUDED in it, not additive.

## Files
- `debug/probe_nonclaude_cache.py` — the probe harness
- `debug/nonclaude_cache_probe_20260503_131618.json` — raw data

