---
name: tofu-context-bar-live-round-usage
description: Per-round usage SSE event drives the gauge live during tool rounds; reader priority chain
enabled: true
tags: [frontend, context-bar, sse]
created: 2026-05-11T06:45:04Z
updated: 2026-05-11T06:45:04Z
---

# Context Bar — live per-round usage (2026-05-11)

## Problem
The gauge needs to reflect "size of the prompt about to be sent" on
EVERY API hit — including all tool rounds inside a single user→assistant
turn, not just at task end.

Two prior bugs:
1. `assistantMsg.usage` is **accumulated** across rounds (see
   `lib/tasks_pkg/llm_fallback.py:144`: `accumulated_usage[k] += v`).
   After 36 rounds × ~117k tokens, it's 4.2M — billed, not
   currently-in-prompt.  4.2M displayed as 100% on a 1M model is wrong.
2. `assistantMsg.apiRounds` is only delivered with the final `done`
   event (`orchestrator.py:336-337`).  Mid-task, between tool rounds,
   the frontend has no per-round breakdown to read.

## Solution: per-round SSE event
Backend emits a `round_usage` event the moment each LLM round lands.

### Backend
- `lib/tasks_pkg/llm_fallback.py:_emit_round_usage(task, round, model,
  usage, *, tag)` — appends `{type:'round_usage', round, model, tag,
  tokensIn, tokensOut, usage}` to the task event stream.  Computes
  `tokensIn` server-side using the same Anthropic vs OpenAI cache
  convention as `ui.js:1853`.
- Called from all three sites in `_llm_call_with_fallback` (primary,
  reactive, fallback) and from `orchestrator.py:185` (post-loop
  fallback).  Each site is right after the corresponding
  `api_rounds.append(...)`.
- Endpoint mode (planner/critic) is automatically covered because
  `_run_planner_turn` / `_run_critic_turn` go through
  `_run_single_turn` → `_llm_call_with_fallback`.

### Frontend
- `static/js/ui.js` — new SSE branch `ev.type === 'round_usage'`
  stashes `assistantMsg._liveLastRoundUsage = {round, model, tag,
  tokensIn, tokensOut, usage}` and calls `updateContextBar()`.
- `static/js/context-bar.js:_lastUsageTokens(conv)` reader priority:
  1. `m._liveLastRoundUsage.tokensIn` — live in-flight reading.
  2. `m.apiRounds[-1].usage` (cache-corrected via
     `_promptTokensFromUsage`) — post-done.
  3. `m.usage / N` — legacy fallback for old conversations without
     `apiRounds`.
- Walks newest-first; skips zero readings so an in-flight bubble with
  no usage yet doesn't shadow the previous turn's number.

## In-flight `assistantMsg` reference
`_processSSELine` runs inside the SSE poll fn that does
`let assistantMsg = conv.messages[conv.messages.length - 1]` (~line
5524).  In endpoint mode this gets reassigned across planner→worker→
critic phase transitions, so by the time `round_usage` arrives,
`assistantMsg` is whichever bubble is currently being streamed —
correctly attaching the live reading to the active phase's message.

## Tooltip wording
"4.0k / 1.0M tokens (0%) — last round prompt".  The "last round
prompt" suffix tells the user this is next-prompt-size, not
accumulated.  The number CAN go down between rounds when tool
results get pruned — that's correct behaviour, not a bug.

