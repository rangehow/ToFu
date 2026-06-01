---
name: tool-compacted-sse-cross-message-fix
description: tool_compacted SSE handler must search ALL assistant messages, not just current bubble — L1 compacts cold rounds in older messages
enabled: true
tags: [frontend, sse, compaction, bug]
created: 2026-05-12T06:06:51Z
updated: 2026-05-12T06:06:51Z
---

# tool_compacted SSE: must search ALL assistant messages

## The bug (2026-05-12)
`static/js/ui.js` had:
```js
} else if (assistantMsg && assistantMsg.toolRounds) {
  _applyCompacted(assistantMsg.toolRounds.find(r => r.toolCallId === ev.toolCallId));
}
```

**`assistantMsg` is the in-flight bubble** — i.e. the most recent
assistant message being streamed. But L1 micro-compaction by definition
operates on COLD rounds — tool calls from earlier assistant messages
that have aged out of the hot tail. Those rounds live in
`conv.messages[i].toolRounds` for some `i < latest`, NOT in
`assistantMsg.toolRounds`.

Result: `find` returned `undefined`, `_applyCompacted(undefined)`
no-op'd, the COMPACTED pill never rendered, and DB persistence
silently dropped the stamp.

Diagnosis: query the live DB and check if `compactionLayer` is
present on any round. If logs say "[L1] compacted=N" but DB has
`compactionLayer: null` everywhere → this bug.

## The fix
Walk every assistant message in `conversations.find(c=>c.id===convId).messages`,
finding the round by `toolCallId` (UUID-style, conversation-unique).
After stamping, if the matched message ISN'T `assistantMsg`,
trigger `renderChat(_conv, false)` — `twUpdate` only re-renders the
streaming bubble; older messages need a full conv re-render to pick
up the new `compactionLayer`.

`_msgFingerprint` already includes `compactedCount` and
`compactedToSum`, so `renderChat`'s fingerprint guard correctly
detects which message changed and re-renders only that one.

## Persistence path (works correctly once stamping works)
`_round_index` in `compaction.py:_stamp_l1` mutates `task['toolRounds']`
in-place. `checkpoint_task_partial` (orchestrator.py, every ~5s)
writes `task['toolRounds']` to `conversations.messages` via
`_sync_result_to_conversation` → `last_msg['toolRounds'] = task['toolRounds']`.
So the stamps survive into DB as long as the in-memory mutation
succeeds — which is exactly what was failing on the frontend before
this fix (frontend writes via `save_conv` would overwrite with
unstamped rounds, but the backend checkpoint path is correct).

## Symptom for users
"I see the COMPACTED pill design but no row in any conversation
shows it" — even though `lib/tasks_pkg/compaction.py` logs
"[L1] cold=143 compacted=48".

## Tested with
`mp0sggcln5pruo` had 222 tool rounds with 48 reportedly compacted
but `compactionLayer: null` on all of them in the DB. After this
fix + a new compaction event firing, the stamp should land.

