---
name: auto-learn-model-context-limits
description: Auto-learn per-(provider,model) context window: shrink on PromptTooLongError, expand on success. 2026-06-08 hardening: shrink TTL self-heal (7d), 2-strike gate on big inferred drops, authoritative gateway-stated-max parse. Persisted to server_config.json model_context_limits + _meta.
enabled: true
tags: [llm, context-limits, auto-learning, self-tuning]
created: 2026-05-12T02:22:33Z
updated: 2026-06-08T04:09:12Z
---

# Auto-Learn Model CONTEXT (input) Limits

Companion to `auto-learn-model-token-limits` (OUTPUT max_tokens). Learns the
INPUT context window per `(provider_id, model)`. Module: `lib/context_limits.py`.

## Public funcs
- `lookup_learned_context_limit(provider_id, model)` → int|None. Per-provider key
  first, bare-model fallback. **Applies shrink TTL on read** (see below).
- `learn_shrink_from_error(provider_id, model, reported_tokens, preset_limit, stated_max=None)`
- `learn_expand_from_success(provider_id, model, observed_tokens, preset_limit)`

## Storage (`data/config/server_config.json`)
- `model_context_limits` → `{"<provider>::<model>": int}` (public; read by routes/config.py + frontend).
- `model_context_limits_meta` → `{"<key>": {ts, source, strikes}}` — sidecar driving TTL + strike gate.
  source ∈ {shrink, expand, pending}. Keys with no meta = permanent (legacy/hand-edited).

## Read path
`compaction/_tokens.py::_get_context_limit(task)` consults learned table FIRST, else static preset
(`_get_static_context_limit`). `resolve_model_context_limit(model, provider)` is the task-less sibling.

## 2026-06-08 hardening (THE deadlock fix)
**Why:** a single transient overflow learned `sankuai::deepseek-v4-pro=200278` on a genuine 1M model.
Once shrunk, `_usable_context`+force-compact cap every prompt BELOW the learned limit, so the expand
condition `observed_tokens > preset` is **structurally unsatisfiable** → expand can NEVER rescue a
wrongful shrink. Empirically verified real ceiling via `debug/probe_deepseek_v4_context.py` (647K
prompt accepted; gateway stated max 1048565). Fix lives ONLY on the shrink side + TTL:

1. **Authoritative stated-max**: `_parse_context_overflow(err)` in `_tokens.py` returns
   `(requested, stated_max)` — handles both `"N tokens > M maximum"` and `"maximum context length
   is M ... requested N"` orderings. `_parse_reported_token_count` now delegates (returns requested).
   When `stated_max` present + below preset → learn M directly + immediately (bypasses strike gate).
   Old bug: stored `requested*0.95` (the wrong number — requested, not the stated ceiling).
2. **2-strike gate** (`_REQUIRED_STRIKES=2`, `_STRIKE_WINDOW_SEC=3600`): an INFERRED shrink dropping
   below `prior*_BIG_DROP_FACTOR(0.5)` needs 2 consecutive overflows within the window before it
   persists (source='pending' meta tracks strikes). Authoritative + small drops skip the gate.
3. **TTL self-heal** (`_SHRINK_TTL_DAYS=7`, env `TOFU_CTX_SHRINK_TTL_DAYS`): `lookup_*` lazily drops
   shrink entries older than TTL → reverts to static preset. If small window is real, next overflow
   re-learns it (reactive_compact recovers that one request). **Expand entries are NEVER TTL'd**
   (corroborated by a real accepted prompt). Audit `direction='expire'`.

## Hooks
- Shrink: `llm_fallback.py::_llm_call_with_fallback` PromptTooLongError branch — calls
  `_parse_context_overflow`, passes `stated_max=` to `learn_shrink_from_error`.
- Expand: `manager.py::stream_llm_response` after `record_usage` (incl. Anthropic cache tokens).

## Audit / tests
`audit_log('context_limit_learned', direction='shrink'|'expand'|'expire', ...)`.
Tests: `tests/test_context_limits_selfheal.py` (15). NOTE: in-memory `_LEARNED`/`_META` load at import
→ **config edits need a server restart** to take effect.

