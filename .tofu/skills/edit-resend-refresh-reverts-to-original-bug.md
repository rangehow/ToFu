---
name: edit-resend-refresh-reverts-to-original-bug
description: Bug fix: refresh during in-flight /api/chat/regenerate window reverts edited message to original — DB+IDB still had pre-edit state because truncation only lived in memory until the atomic server call completed
enabled: true
tags: [javascript, frontend, bug-fix, edit, regenerate, refresh, race-condition, sync, IDB-cache, data-loss]
created: 2026-04-22T12:34:34Z
updated: 2026-04-22T12:34:34Z
---

# Edit→Resend Refresh Reverts to Original Question

## Symptom
1. User sends message A, generation starts.
2. User clicks Stop, then Edit → types B → Save & Resend.
3. While "Waiting…" is shown (before regenerate task streams), user refreshes.
4. Page loads showing the ORIGINAL question A and starts generating for A, not B.

## Root Cause
`saveEditAndResend` (ui.js) and `regenerateFromUser` (main.js) did:
1. `_hardCancelActiveStream(conv)` — aborts old task cooperatively, clears `conv.activeTaskId` **in memory only**
2. `conv.messages = conv.messages.slice(0, idx + 1)` — truncates in memory
3. Edits `msg.content` in memory
4. Surgical DOM truncation
5. `fetch('/api/chat/regenerate', ...)` — the FIRST point where DB learns about the truncation/edit

During the gap between step 1 and step 5 returning:
- **Server DB** still has the original messages, plus the old task's `activeTaskId` in settings
- **IndexedDB cache** still has the original messages
- Old task may still be mid-sync (abort is cooperative)

On refresh in that window, `loadConversationsFromServer` + `loadConversationMessages` load the original messages. `initActiveTasks` then either:
- Case A reconnects to the old (aborted) task, OR
- Case E sees last msg is `role=user` with original content → auto-starts assistant response for the ORIGINAL question

## Fix
Persist truncated+edited state BEFORE the `/api/chat/regenerate` POST fires, in both places:
- `static/js/ui.js` → `saveEditAndResend()` after surgical DOM truncation
- `static/js/main.js` → `regenerateFromUser()` after surgical DOM truncation

```js
// ★ SyncFix: persist truncated/edited state BEFORE /api/chat/regenerate fires
try { if (typeof ConvCache !== 'undefined') ConvCache.put(conv); }
catch (e) { console.warn('[SyncFix] ConvCache.put failed:', e); }
try {
  await syncConversationToServer(conv, { allowTruncate: true });
} catch (e) {
  console.warn(`[SyncFix] pre-regenerate sync failed: ${e.message}`);
}
```

## Why this works
- `syncConversationToServer` with `allowTruncate: true` updates the server row with the truncated messages AND writes `activeTaskId: null` into settings (from `conv.activeTaskId` which `_hardCancelActiveStream` already set to null in memory).
- `ConvCache.put(conv)` updates IDB cache so Phase 1 render on refresh shows the edited state immediately.
- On a refresh during the in-flight `/api/chat/regenerate` window:
  - DB has edited/truncated messages + no activeTaskId
  - Case E sees the EDITED user message at the tail, and orphan recovery auto-regenerates the edited message (intended behavior) — OR, if `/api/chat/regenerate` already started the new task server-side, Case C detects the running task via `convIdToRunningTask` and reconnects to it.

## Related prior fixes
- `interrupt-then-edit-stale-ui-sync-fix` — `_hardCancelActiveStream`, DOM truncation, stale-task SSE guards
- `regen-needsLoad-stale-message-resurrection-bug` — clear `_needsLoad` after truncation
- `translateThenRespond-missing-allowTruncate-edit-regen-bug` — `allowTruncate=true` on the server PUT
- `stale-task-overwrites-regeneration-fix` — backend freshness guard via `_conv_latest_task`

The missing piece these earlier fixes didn't cover: the atomic `/api/chat/regenerate` window itself. The new pre-sync closes that window.

## Files Changed
- `static/js/ui.js` — `saveEditAndResend` adds pre-sync
- `static/js/main.js` — `regenerateFromUser` adds pre-sync
