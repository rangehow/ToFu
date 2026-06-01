---
name: continue-checkpoint-based-resumption
description: Continue checkpoint preserves toolContent/assistantContent/thinking/thoughtSignature across page refresh with per-provider capability gating
enabled: true
tags: [python, javascript, continue, checkpoint, resumption, tool-history, architecture, claude, gemini, thinking, thought-signature]
created: 2026-03-30T05:56:24Z
updated: 2026-04-21T00:00:00Z
---

# Continue Checkpoint-Based Resumption — Architecture & Fixes

## How Continue Works

1. **Frontend** (`continueAssistant` in `main.js`):
   - Scans `assistantMsg.toolRounds` for complete rounds (`status==="done"` AND `toolContent != null`)
   - Builds `keptRounds` (checkpoint), discards incomplete rounds
   - Reconstructs `preservedContent` from `assistantContent` fields on kept rounds
   - Reconstructs `preservedThinking` from per-round `thinking` fields on kept rounds
   - Builds `toolHistory[]` via `_buildToolHistoryRound(batch)` carrying:
     `{assistantContent, thinking?, thinkingSignature?, toolCalls:[{id,name,arguments,extraContent?}], toolResults}`
   - Sends `toolHistory`, `contentPrefix`, and checkpoint metadata to backend
   - Checkpoint metadata: `checkpointToolRounds`, `checkpointUsage`, `checkpointApiRounds`, `checkpointModifiedFiles`, `checkpointModifiedFileList`

2. **Backend** (`orchestrator.py`):
   - Stashes checkpoint metadata on `task['_checkpointToolRounds']` etc.
   - Applies `contentPrefix` to `task['content']` ONLY — never as a trailing
     assistant prefill message (Anthropic Messages API rejects that shape)
   - Injects `toolHistory` into messages via `inject_tool_history()`
   - On completion, merges checkpoint data into done event

3. **Backend persistence** (`manager.py`): unchanged — `_sync_result_to_conversation` + `checkpoint_task_partial` merge `_checkpointToolRounds` + `task['toolRounds']` when writing to DB.

## Per-Provider Capability Matrix (2026-04-21)

What each API actually accepts on a replayed assistant turn (tool calls already made):

| Provider          | tool_use replay | thinking replay                           | Prefill (trailing assistant) |
|-------------------|-----------------|-------------------------------------------|------------------------------|
| Anthropic Claude  | required        | `thinking{}` + opaque `signature` is MANDATORY when tools were used with extended thinking, else HTTP 400 | **NO** — API rejects         |
| Gemini (OAI-compat) | required      | `extra_content.google.thought_signature` on each tool_call or HTTP 400 | tolerated (best-effort)    |
| OpenAI / DeepSeek / Qwen / GLM / Kimi / Doubao / MiniMax / ERNIE / LongCat | standard | **NOT re-accepted** (`reasoning_content` stripped server-side) | tolerated (best-effort)    |

Capability probes live in `lib/model_info.py`:
- `model_requires_thinking_signature_replay(m)` → True for Claude
- `model_requires_thought_signature_on_tool_calls(m)` → True for Gemini
- `model_supports_assistant_prefill(m)` → False only for Claude

## Key Fields on toolRound Entries

- `toolCallId`, `toolName`, `toolArgs`, `toolContent`, `status`, `llmRound` — basic identity & tool result
- `assistantContent` — text LLM wrote alongside tool calls (first round of batch)
- `thinking` — reasoning trace captured from `reasoning_content` for that LLM round (NEW 2026-04-21)
- `thinkingSignature` — opaque Claude thinking-block signature (NEW 2026-04-21)
- `extraContent` — Gemini's `{google: {thought_signature}}` envelope, captured off the tool_call delta (NEW 2026-04-21)

All five new/old fields are persisted to DB via the standard toolRounds JSON column.

## `inject_tool_history` Gating (lib/tasks_pkg/message_builder.py)

- Always emits standard `assistant(tool_calls)` + `tool(result)` sequence.
- If `model_requires_thinking_signature_replay(model)` AND both `thinking` + `thinkingSignature` present → attach `reasoning_content` + `thinking_signature` on the assistant message.
- If `model_requires_thought_signature_on_tool_calls(model)` AND `extraContent` present → attach `extra_content` on each tool_call entry.
- Unsupported providers: silent no-op (`logger.debug(...)` records the drop).
- `thinking_signature` is whitelisted in `lib.llm_client._API_MESSAGE_FIELDS` so it survives `_strip_non_api_fields`.

## `conv_message_builder._reconstruct_tool_call_messages` parity

Historical turns (prior, completed messages loaded from DB) carry the same new fields when present. This matters because Claude with extended thinking + tools requires the signature on any prior assistant turn in the conversation, not just the one being continued.

## Anthropic Limitation (Accepted, NOT worked around)

- Messages API: "This model does not support assistant message prefill. The conversation must end with a user message."
- Consequence: the free-form text the model wrote BETWEEN tool batches can never be re-injected as a prefill against Claude.
- What we DO preserve against Claude: tool_call IDs, args, results, thinking block + signature, and each round's `assistantContent` (which becomes the `content` field of the replayed assistant turn).
- What we CANNOT preserve against Claude: free text written after the last completed tool batch (if any) — it is rolled back and the model regenerates from the tool-result checkpoint.

## `priorThinking` Display-Only Field (2026-05-16)

The trailing **message-level** thinking that exists *beyond* the per-round
thinking on completed tool batches cannot be replayed on the wire (Anthropic
rejects orphan thinking blocks; OpenAI-compat strips `reasoning_content`).
On Continue rollback we capture it as `assistant_msg['priorThinking']` so the
UI can render it as a collapsed "Earlier Thinking" block. Hard rules:

- **Source**: `routes/chat.py::_scan_continue_checkpoint` returns
  `discarded_thinking_text = original_thinking if discarded_thinking > 0 else ''`
  alongside the existing `discarded_thinking` count.
- **Set point**: `routes/chat.py::chat_continue` writes
  `assistant_msg['priorThinking'] = scan['discarded_thinking_text']` only when
  non-empty. When empty, it does **not** clear an existing value — a previous
  Continue cycle's prior thinking is the freshest signal we have.
- **Frontend mirror**: `static/js/main.js::continueAssistant` does the same
  before clearing `assistantMsg.thinking`. Restore path (regenerate fallback)
  explicitly `delete`s `priorThinking`.
- **Wire safety**: `priorThinking` is **NOT** in `lib/llm_sanitize.py::_API_MESSAGE_FIELDS`,
  so `_strip_non_api_fields` drops it before any LLM call. It also is not read
  by `lib/tasks_pkg/conv_message_builder.py::_build_assistant_messages` —
  cannot leak into messages built from DB.
- **Renderer**: `static/js/ui.js::renderMessage` emits a second
  `<div class="thinking-block thinking-prior">` block (dashed border via
  `static/styles.css`). `_togglePriorThinking(el, msgIdx)` is the lazy-load
  twin of `_toggleThinking` — sources from `msg.priorThinking`. Both functions
  live in `ui.js` which is in `lib/js_bundler.py::_BUNDLE_FILES` so top-level
  `function` declarations are global.
- **Persistence**: just another key on the message dict; round-trips through
  the `messages` JSON column. Not indexed in `build_search_text`.

## Bugs Fixed

### 2026-04-21 — Thinking + thought_signature loss on Continue
**Root cause**: `inject_tool_history` only carried `assistantContent` + `tool_calls` + `tool_results`. Against Claude extended-thinking models, this caused HTTP 400 on the follow-up call ("Expected thinking block with signature"); against Gemini it caused HTTP 400 ("missing thought_signature"); against both, semantic reasoning continuity was lost.
**Fix**: (1) `tool_dispatch.parse_tool_calls` now stores `thinking`, `thinkingSignature`, `extraContent` per round. (2) Frontend `_buildToolHistoryRound` propagates them. (3) Backend `inject_tool_history` re-attaches them only for providers whose API consumes them, gated by `lib/model_info.py` capability probes. (4) Whitelisted `thinking_signature` in `_API_MESSAGE_FIELDS` so it survives the strip pass. Tests: `tests/test_continue_lossless.py` (21 cases).

### 2026-04-12 — toolContent / preservedContent / incomplete-rounds
- toolContent lost after refresh → backend now updates toolRounds even when skipping content update.
- preservedContent empty after refresh → fall back to originalContent.
- Rounds marked incomplete → reconstruct toolContent from results metadata.

## Auto-Resume Patterns (Unaffected)

- **Premature close retry** (`stream_handler.py`): in-task retry within orchestrator's while-loop. Same task dict, no Continue flow.
- **Page-load reconnection** (`initActiveTasks`): SSE reconnection gets backend state snapshot with full toolRounds.
- **Queue dispatch** (`_dispatch_queued_message`): separate new task for next queued message.

## Related Memories

- `gemini-thought-signature-openai-compat` — capture-time handling for Gemini signatures
- `conv-message-builder-structured-tool-history` — parity with historical-turn reconstruction
- `sync-mechanism-review-bugs-2026-04` — frontend↔backend sync asymmetry
