---
name: claude-code-caching-architecture-comparison
description: Claude Code vs ChatUI caching: single-marker + global scope + cache_editing vs our 4-BP mixed TTL — complete architecture comparison
enabled: true
tags: [claude-code, caching, architecture, ttl, prompt-cache, comparison]
created: 2026-04-04T15:18:50Z
updated: 2026-04-04T15:18:50Z
---

# Claude Code vs ChatUI — Caching Architecture Comparison

## Claude Code's Approach

### 1. Single Message-Level Breakpoint
- Places `cache_control` on **exactly one message** — `messages[-1]` (or `[-2]` for skipCacheWrite)
- Reason: Mycro's KV cache eviction frees local-attention pages at any cached prefix NOT in `cache_store_int_token_boundaries`. Two markers waste KV pages.
- Code: `src/services/api/claude.ts:addCacheBreakpoints()` (line ~3063)

### 2. Uniform TTL (Not Mixed)
- `getCacheControl()` returns `{type: 'ephemeral', ttl?: '1h', scope?: 'global'|'org'}`
- **All markers in a request use the same TTL** — no mixing 1h/5m per breakpoint
- 1h eligibility latched per-session via `should1hCacheTTL()`:
  - Anthropic employees (`USER_TYPE=ant`) always get 1h
  - Subscribers within rate limits get 1h
  - 3P Bedrock: opt-in via `ENABLE_PROMPT_CACHING_1H_BEDROCK=1`
  - Controlled by GrowthBook allowlist (query source pattern matching)
- **Session-stable latch**: Once TTL is determined, it stays for the session to prevent mid-session cache key changes

### 3. Global Cache Scope
- System prompt split by `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` marker
- Static content → `scope: 'global'` (cross-org KV sharing on Anthropic infra)
- Dynamic content → no scope (org-level)
- Beta: `prompt-caching-scope-2026-01-05`
- When MCP tools present: falls back to `scope: 'org'` (no global)

### 4. Cache Editing (Cached Microcompact)
- Instead of full rewrite on compaction, sends `cache_edits` with `cache_reference` deletions
- Tool results get `cache_reference: tool_use_id` for addressable deletion
- O(1) delta vs O(N) full prefix rewrite
- Only for 1P, main thread, supported models

### 5. Sticky Beta Header Latches
- AFK_MODE, FAST_MODE, CACHE_EDITING headers: once sent, latched for session
- Prevents toggling from changing server-side cache key (~50-70K tokens per flip)

### 6. Cache Break Detection (728 lines)
- `src/services/api/promptCacheBreakDetection.ts`
- 2-phase: `recordPromptState()` (pre-call) → `checkResponseForCacheBreak()` (post-call)
- Tracks 15+ client-side factors: system hash, tool hashes, model, betas, effort, fast mode, auto mode, overage state, cache_control hash, global cache strategy, extra body params
- Per-tool hash diffing to name which tool's schema changed
- TTL-aware: distinguishes 5min vs 1h expiry in diagnostics
- Writes `.diff` files for debugging
- Excludes haiku (different caching behavior)

### 7. Cost Calculation
- Uses Anthropic native SDK: `input_tokens` = uncached only (additive)
- `tokensToUSDCost()` in `src/utils/modelCost.ts` — separate prices for input, output, cache_read, cache_write
- `promptCacheWriteTokens` always uses 1.25x rate (5-min pricing) regardless of actual TTL used
- No detection needed — always uses Anthropic convention

## Our Approach (ChatUI)
- **4 breakpoints**: BP1-BP2 (system), BP3 (last tool), BP4 (tail)
- **Mixed TTL**: BP1-BP3 get `ttl: "1h"`, BP4 stays 5-min
- **Convention detection**: Frontend detects Anthropic vs OpenAI `prompt_tokens` semantics
- **2-phase detection**: Similar to CC but simpler (hash system+tools, check cache_read drop)

## Key Takeaways for Our System
1. CC uses 1 breakpoint (not 4) because Mycro KV management penalizes extra markers
2. CC does NOT mix TTLs — all markers use same TTL
3. CC's cache_editing eliminates full-prefix rewrites on compaction (we don't have this)
4. CC's global scope enables cross-org cache sharing (we can't use this — requires 1P)
5. CC latches TTL per-session to prevent cache busting (we should consider this)

