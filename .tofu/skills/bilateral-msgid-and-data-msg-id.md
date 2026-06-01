---
name: bilateral-msgid-and-data-msg-id
description: Stable per-message id convention: server UUID via _assign_message_ids, client tmp_<uuid> via _ensureMsgId; DOM exposes data-msg-id alongside msg-${idx}
enabled: true
tags: [frontend, convention, chatInner, rendering, ids]
created: 2026-05-15T04:33:23Z
updated: 2026-05-15T04:33:23Z
---

# Bilateral `_msgId` and `data-msg-id` convention

Step 1+2 of the unified chatInner rendering refactor (2026-05-15).
**Strictly additive — no callers were migrated to id-based lookup yet.**
All existing `getElementById('msg-' + idx)` paths still work and are
still the primary lookup mechanism.

## The convention

Every chat message — whether persisted, streaming, or only client-side —
carries a stable `_msgId` string. Two parallel mints:

- **Server** — `lib/tasks_pkg/manager.py:_assign_message_ids(messages)`
  backfills `_msgId` with `uuid.uuid4()` on persist. Idempotent.
  Also referenced by `lib/tasks_pkg/manager.py:find_message_by_id`,
  `lib/artifacts/scanner.py`, `lib/tasks_pkg/autopilot.py`.
- **Client** — `static/js/core.js`:
  - `_newClientMsgId()` → `'tmp_' + crypto.randomUUID()`
    (falls back to base36 when crypto.randomUUID is missing).
  - `_ensureMsgId(msg)` → idempotent stamper. Only sets `_msgId` if
    missing, so a server UUID arriving via a Phase-2 reload always
    overrides the temporary id.

The `tmp_` prefix is the **only** way to distinguish "never persisted
yet" from "persisted with a real UUID". A `tmp_` id should be
considered ephemeral — once `loadConversationMessages` round-trips
through the server, the id will become a real UUID.

## DOM handle

`static/js/ui.js:renderMessage` emits BOTH:

```html
<div class="message …" id="msg-${idx}" data-msg-id="${msg._msgId}" data-mfp="…">
```

`static/js/ui.js:_streamingBubbleHTML(role, status, timeStr, msgId)`
gained an optional 4th param; emits `data-msg-id` when supplied.
**Existing 3-arg callers stay valid** — none of them pass msgId yet.

Lookup helpers that should be added when we migrate (step 3+):

```js
inner.querySelector(`[data-msg-id="${CSS.escape(id)}"]`)
```

## Where stamping happens (Step 1+2 sites)

Every `conv.messages.push(msg)` in chatInner-flow files is preceded
by `_ensureMsgId(msg)`:

- `static/js/main.js` — 9 sites (sendMessage, regenerate, autopilot,
  queued-dispatch, initActiveTasks Case A & C orphan recovery,
  error fallback)
- `static/js/ui.js` — 11 sites (regenerateFromUser branch,
  connectToTask 3 defensive recovery paths, SSE state
  planner/critic/worker, endpoint_iteration critic + worker,
  endpoint_critic_msg replan planner, endpoint_new_turn worker)
- `static/js/image-gen.js` — 6 sites (single-shot, batch, error
  paths)

**Skipped intentionally:**
- `branch.js` — separate branch model, own DOM regions
  (`branch-streaming-${msgIdx}-${bi}`); will follow same pattern
  but in its own pass.
- `paper-reader.js` — `_paperQAHistory`, not chatInner.

## What NOT to do yet

- Do NOT migrate any caller from `getElementById('msg-' + idx)` to
  `data-msg-id` — that is step 3 (ConvView controller).
- Do NOT remove `id="msg-${idx}"` — both attributes must coexist
  during the transition.
- Do NOT trust `_msgId` for cross-conv uniqueness; uniqueness is
  per-conversation only.

## Verification

```js
// In browser console after restart + reload:
document.querySelectorAll('#chatInner [data-msg-id]').length
// Should equal getActiveConv().messages.length
```

`node --check` on core.js / ui.js / main.js / image-gen.js passes.

