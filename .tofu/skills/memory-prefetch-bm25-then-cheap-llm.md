---
name: memory-prefetch-bm25-then-cheap-llm
description: Per-turn memory prefetch: BM25 coarse → cheap-LLM precision filter → inject &lt;relevant_memories&gt; block, with frontend chip indicator
enabled: true
tags: [memory, prefetch, bm25, architecture, claude-code-alignment]
created: 2026-04-30T23:37:41Z
updated: 2026-05-01T17:37:39Z
---

# Memory Prefetch: proactive surfacing of relevant memories

## Problem solved
Model saves a lesson as a memory (e.g. "cuda_graph_scope='' breaks Megatron
enum"), but next turn the model doesn't think to call `search_memories` → same
trap is re-triggered. The explicit `search_memories` tool is insufficient
because it relies on the model's initiative.

## Design
Two-stage cascade runs ONCE per user turn (round 0), BEFORE the main LLM call:

1. **Query construction** — last K user+assistant text turns (K=3 default),
   stripping all tool_calls / tool_results / thinking / image blocks. Cap ~4KB.
2. **BM25 coarse** — score over name+description+tags+body → top-N (N=80).
3. **Cheap-LLM precision** — dispatch_chat with capability='cheap', temp=0.
   Prompt instructs "precision over recall — return [] if nothing is clearly
   relevant". Model output: `{"ids":[...]}` JSON.
4. **Injection** — selected memory BODIES concatenated into `<relevant_memories>`
   block, wrapped in `<system-reminder>`, appended to last user message's
   content-blocks list. Byte cap 8KB, max 5 memories.

## No-fallback policy (updated)
- **No timeout, no try/except** around the cheap-LLM rerank call. If it
  raises, the exception propagates up to `run_memory_prefetch`'s outer
  handler and we inject NOTHING.
- We deliberately do NOT fall back to BM25 top-K on failure — a noisy
  BM25 injection wastes tokens and distracts the main model more than
  it helps. Per user preference: "I'd rather not add memory than inject BM25."
- On BM25 zero hits → skip cheap-LLM entirely (saves cost on chit-chat).
- On too-few candidates (< PREFETCH_MIN_CANDIDATES=2) → skip LLM, take
  all candidates directly (trivially small pool, no filter needed).
- `_call_cheap_reranker` no longer accepts a `timeout_s` param; `dispatch_chat`
  is called without a `timeout=` kwarg.
- Retained for backward-compat: frontend ui.js still reads `fellBack` on
  persisted messages, but backend never sets `fell_back` anymore, and
  `PREFETCH_TIMEOUT_S` constant has been removed.

## Code layout
- `lib/memory/prefetch.py` — the whole pipeline
- `lib/tasks_pkg/orchestrator.py` — calls `run_memory_prefetch()` after
  `_inject_system_contexts` and `inject_tool_history`, gated on
  `has_real_tools and not _injected_tool_calls` (skip on continue-resume).
  Wraps the call in try/except so a cheap-LLM failure doesn't crash the
  task — it just logs a warning and proceeds without injection.
- `lib/tasks_pkg/manager.py` — `_sync_result_to_conversation` persists
  `task['_memoryPrefetch']` to message `_memoryPrefetch` for DB reload.
- `routes/chat.py` — poll fallback returns `memoryPrefetch` field.

## Frontend
- Event type: `memory_prefetch` with phases: `started`, `bm25_done`,
  `rerank_started`, `done`, `skipped`, `failed`.
- Rendered as `.mem-prefetch-chip` inside assistant bubble's new
  `[data-zone="memprefetch"]` zone (first zone above tool panel).
- `renderMemoryPrefetchHtml()` in ui.js builds the chip; click-to-expand
  shows which memories were picked.
- Persisted in message as `_memoryPrefetch` → survives reload via poll.

## Feature flag
`features.json → memory_prefetch` (default true); env override
`MEMORY_PREFETCH=0`. Uses `_resolve_feature_flag` helper from `lib/__init__.py`.

## Relation to `search_memories` tool
Both share the same BM25 tokenizer+doc builder in `lib/memory/relevance.py`.
Prefetch is PROACTIVE (catches things the model wouldn't have searched for);
`search_memories` is ON-DEMAND (model searches when it knows what it needs).
Keep both.

## Gotchas
- Parse JSON leniently: strip ```` ```json ```` fences, also try to extract
  first `{...}` block if raw text isn't valid JSON.
- Persist payload on ALL terminal phases (done/skipped/failed) so the chip
  survives reload even when we chose not to inject.
- The recent-turns extractor must ignore `tool_use` / `tool_result` /
  `thinking` content blocks — only `type in {'text','output_text'}`.
- The outer try/except in `run_memory_prefetch` around inject still exists
  (to protect against buggy message structures), but the cheap-LLM call
  itself is now bare.

