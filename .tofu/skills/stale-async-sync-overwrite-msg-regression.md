---
name: stale-async-sync-overwrite-msg-regression
description: syncConversationToServer allowTruncate guard system (3 layers) + the 2026-07-03 bug where the _serverMsgCount count-drop guard ignored allowTruncate so swept buried ghosts RESURRECTED on reload; also Case-D→Case-E fall-through auto-firing an unrequested turn. Original: stale async translate PUT overwrote fresher msgs.
enabled: true
tags: [frontend, race-condition, data-loss, sync, translate, allowTruncate, buried-ghost, initActiveTasks]
created: 2026-04-04T13:20:16Z
updated: 2026-07-03T17:40:00Z
---

# Stale Async Sync Overwrite — Message Regression Bug

## Symptom
User message disappears from conversation. Backend log shows it was received,
but the conversation in DB has two consecutive `assistant` messages with no `user`
between them.

## Root Cause (TOCTOU race in syncConversationToServer)

1. **Translate poll callback** starts `syncConversationToServer(conv)` which captures
   `lightMsgs` from `conv.messages` (e.g. 51 messages)
2. **User sends new message** → `conv.messages.push(userMsg, assistantMsg)` → 53 messages
3. **User's sync** PUTs 52 messages → server has 52
4. **Translate callback's sync** PUTs 51 messages (stale `lightMsgs`) → **OVERWRITES 52 → 51**
5. User message is permanently lost

## Trigger Conditions
- Server restart during active task (page reloads)
- `_resumePendingTranslations` fires after reload, starts translate poll
- User sends message while translate poll is in flight
- Translate poll callback fires, captures stale conv.messages

## Fix (Dual-Layer Defense)

### Layer 1: Frontend pre-send staleness check (core.js)
Before the `await fetch(PUT)`, check if `conv.messages.length > lightMsgs.length`:
```javascript
if (!allowTruncate && conv.messages.length > lightMsgs.length) {
  console.warn(`[syncToServer] CANCELLED stale sync`);
  return;
}
```

### Layer 2: Backend message count regression guard (routes/conversations.py)
Reject PUTs where `msg_count < existing_count` unless `allowTruncate=true`:
```python
if msg_count > 0 and msg_count < existing_count and not allow_truncate:
    return jsonify({'error': 'blocked_msg_regression'}), 409
```

### Intentional truncation callers pass `allowTruncate: true`:
- `regenerateFromUser()` — truncates to regen point
- `continueAssistant()` — pops empty assistant
- `saveEditAndResend()` — truncates to edit point
- `initActiveTasks Case D` — pops ghost empty assistant / sweeps buried ghosts

## ⚠️ Layer-0 gotcha (2026-07-03): a THIRD guard ignored `allowTruncate`
`syncConversationToServer` has an EARLIER count-drop guard than the Layer-1
staleness check — the `_serverMsgCount` data-loss guard near the top:
```javascript
// BEFORE (bug): fires for a DELIBERATE truncation too
if (conv._serverMsgCount && conv.messages.length < conv._serverMsgCount) return;
// AFTER (fix): honour the flag, like Layer-1 + the backend
if (!allowTruncate && conv._serverMsgCount && conv.messages.length < conv._serverMsgCount) return;
```
It ran BEFORE (and without consulting) `allowTruncate`, so every truncation
caller above (the buried-ghost SWEEP, Case-D delete, edit/regen) BAILED here
and never reached the PUT. **Symptom:** buried empty-ghost bubbles are swept
from the DOM on each load but NEVER persisted server-side → they RESURRECT on
every reload — the "chatInner shows stale/ghost elements out of sync with the
backend" class. Fix: gate on `!allowTruncate` so all THREE layers (this guard,
Layer-1 staleness, backend regression guard) agree. Test:
`tests/test_frontend_sync_allowtruncate_guard.py` (drives REAL sync + double-neuter).

## ⚠️ Related control-flow leak (same day): Case-D → Case-E fall-through
`initActiveTasks` Case D's `_ghost==='delete'` branch does `conv.messages.pop()`
but had NO `continue` (Cases A/B/C all do). After the pop the new tail can be
the preceding recent `user` msg → execution falls into the Case-E block, which
auto-fires `startAssistantResponse` — an UNREQUESTED, billed LLM turn (possibly
duplicating a completed answer). Fix: `continue;` after the delete-branch sync.
Only the delete pop exposes a user tail (sweep is mid-list; interrupted leaves
an assistant tail). Test: `tests/test_frontend_casee_no_autostart_after_ghost_delete.py`
(drives REAL initActiveTasks end-to-end + double-neuter). GOTCHA: stub
`sessionStorage` + `_editingMsgIdx`/`showStreamingUIForConv` or initActiveTasks'
outer try/catch swallows the throw and the loop silently never runs.

## ⚠️ Same bug class in the SSE `state` handler (2026-07-03): empty snapshot wiped checkpointed content
`sse_pipeline.js`'s plain-assistant `state` branch did
`assistantMsg.content = ev.content || ""` UNCONDITIONALLY — so an EMPTY or
SHORTER `state` snapshot (lagging server content-lock cycle, or reconnect
before content accumulated) clobbered already-checkpointed content back to
blank. Identical class to the `_twFlush` raw-buffer wipe (see
`force-refresh-streaming-stuck-waiting-bug` skill) and the `_pollFallback`
`data.content` regression. **Reset-safety proof (why keep-longer is safe):**
`state` / `retry_reset` / `delta_reset` are mutually-exclusive `else if` arms
on `ev.type`. RESETS ride their OWN events (`retry_reset`/`delta_reset`) which
clear BOTH message AND buffer to `""`; a `state` snapshot is a full RESYNC and
NEVER a reset. After a reset the msg is `""` and the next `state` also carries
`""` → stays empty. Stale snapshots from an aborted/superseded task are already
dropped by the SyncFix guard before this branch. So NO legitimate empty-reset
path flows through `state`. **Fix:** shared helper
`_snapshotLonger(msg, ev, field)` (returns `incoming.length >= current.length
? incoming : current`) routed into every state-snapshot write site (plain
assistant, critic, planner). Test: `tests/test_frontend_state_snapshot_no_wipe.py`
(drives REAL state handler via `window.__sse_test__` seam + double-neuter that
reverts `_snapshotLonger` to raw `return incoming` → empty-state wipe returns).
GUARDRAIL: any `ev.field || ""` overwrite on a resync/reconnect/poll snapshot is
a wipe hazard — use keep-longer, and confirm real resets ride a separate event.

## Forensic Pattern in Logs
```
PUT saves 52 msgs  ← user sends message
PUT saves 51 msgs  ← STALE translate sync (lower count = regression!)
PUT saves 52 msgs  ← re-sync but user msg already lost
```

