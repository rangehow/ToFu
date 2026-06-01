---
name: auto-learn-model-context-limits
description: Auto-learn per-(provider, model) context window in BOTH directions: shrink on PromptTooLongError, expand when prompt_tokens exceeds presumed ceiling. Persisted to server_config.json under model_context_limits.
enabled: true
tags: [llm, context-limits, auto-learning, self-tuning]
created: 2026-05-12T02:22:33Z
updated: 2026-05-12T02:22:33Z
---

# Auto-Learn Model CONTEXT (input) Limits

Companion to memory `auto-learn-model-token-limits` (which auto-learns
the OUTPUT `max_tokens` ceiling). This one learns the INPUT context
window, per `(provider_id, model)` pair.

## Problem
A logical model id (e.g. `deepseek-v3.2`) may be served by several
providers (Tencent, Baidu, Huawei, Doubao gateways) — each may advertise
a different real context length. Our preset table in
`lib/tasks_pkg/compaction.py::_get_static_context_limit` inevitably gets
some wrong: a 1M-context provider may be flagged as 128k (compaction
fires too early; user UX worse than necessary), or a 128k provider may
be flagged as 1M (we send 600k of context, get HTTP 400).

## Solution Architecture

### Module
`lib/context_limits.py` — small standalone module, three public funcs:
- `lookup_learned_context_limit(provider_id, model)` — returns int or None.
  Tries `<provider>::<model>` key first, falls back to bare `<model>`.
- `learn_shrink_from_error(provider_id, model, reported_tokens, preset_limit)`
  — called when `PromptTooLongError` fires. Persists `int(reported_tokens * 0.95)`,
  but never below `preset_limit * 0.10` (`_MIN_SHRINK_FACTOR`) — protects
  against a single anomalous error collapsing the limit.
- `learn_expand_from_success(provider_id, model, observed_tokens, preset_limit)`
  — called after a successful response. If `observed_tokens > preset_limit`,
  raises ceiling to `int(observed_tokens * 1.05)` (`_EXPAND_HEADROOM=5%`).

### Storage
`data/config/server_config.json` → `model_context_limits` →
`{"<provider_id>::<model>": int, ...}`. Atomic via tmp+rename.
Sanity: only int values in `[4_000, 50_000_000]` are loaded.

### Read path
`lib/tasks_pkg/compaction.py::_get_context_limit(task)` (renamed
original to `_get_static_context_limit`). New wrapper consults learned
table FIRST via `lookup_learned_context_limit(task['provider_id'], model)`,
falls back to static. All compaction triggers, force-compact, and
reactive_compact get the corrected ceiling automatically.

### Shrink hook
`lib/tasks_pkg/llm_fallback.py::_llm_call_with_fallback` — inside the
`PromptTooLongError` branch, BEFORE calling `reactive_compact`:
parses `_parse_reported_token_count(str(e))`, calls `learn_shrink_from_error`,
emits SSE phase event "Auto-detected smaller context window for X: N tokens".

### Expand hook
`lib/tasks_pkg/manager.py::stream_llm_response` — right after
`record_usage(...)` writes to the usage cache. Computes `total_prompt_tokens`
including Anthropic cache tokens (cache_creation + cache_read), then
calls `learn_expand_from_success`. Emits same-shape SSE phase event.

### Surface
`routes/config.py` `/api/server-config` exposes `model_context_limits`
alongside `model_limits` so Settings UI can display learned values.

## Audit
`audit_log('context_limit_learned', direction='shrink'|'expand', ...)`
fires on every persisted change.

## Why provider-namespaced
Multiple providers serving the same model id is the common case — using
a bare model key would let one provider's smaller window incorrectly
trim what another provider can accept. Bare-key fallback in
`lookup_learned_context_limit` keeps single-provider deployments working.

