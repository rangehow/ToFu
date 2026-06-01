---
name: interrupt-then-edit-stale-ui-sync-fix
description: Composite fix for stale DOM/state after user interrupt+edit/regen: _hardCancelActiveStream helper, _surgicalTruncateDOM clears streamBufs, stale-task guards on SSE state + poll fallback, finishStream ghost-render skip
enabled: true
tags: [javascript, frontend, bug-fix, race-condition, sync, streaming, edit, regen, abort, sse, truncation]
created: 2026-04-22T02:32:46Z
updated: 2026-04-22T02:32:46Z
---

# Interrupt-then-Edit Stale UI Sync Fix (2026-04-22)

## Symptom
After user clicks Stop mid-stream and immediately Edit → Save & Resend (or
Regen), the old agent's streaming bubble / endpoint turns / tool rounds
remained visible until page refresh. Sometimes Edit silently did nothing.

## Root Causes (all frontend)
1. **Silent early-return**: `saveEditAndResend` (ui.js) and `regenerateFromUser`
   (main.js) both had `if (activeStreams.has(conv.id) || conv.activeTaskId) return`.
   When the user clicked Stop and immediately Edit, the abort was still
   propagating, so the guard fired and nothing happened.
2. **streamBufs not cleared**: `_surgicalTruncateDOM` removed DOM but left
   `streamBufs` populated — a still-alive SSE closure kept accumulating into
   a detached buffer.
3. **Late SSE `state` event resurrection**: `ev.type === 'state'` handler in
   `_trySSE` and the equivalent in `_pollFallback` rebuild `conv.messages`
   from `endpointTurns` without checking if the task was aborted/superseded.
4. **Ghost msg-N on finishStream**: `finishStream` unconditionally replaced
   `#streaming-msg` with `renderMessage(msg, lastIdx)` — after truncation
   the last msg is a user msg, creating a ghost user bubble styled as
   assistant.

## Fix Shape (all in ui.js + main.js)

### `_hardCancelActiveStream(conv)` (new helper, ui.js ~675)
- Synchronous: `s._userAbort=true; s.controller.abort(); twStop; clear activeTaskId; set _lastAbortedTaskId`
- Fire-and-forget POST `/api/chat/abort-conv/${convId}`
- Safe no-op when no active stream

### `_surgicalTruncateDOM` extended (ui.js ~613)
- Also removes `#translating-msg` + orphan `.ep-critic-msg`/`ep-worker-msg`/`ep-planner-msg` without `msg-N` id
- Calls `twStop(conv.id)` when streaming bubble removed — kills streamBufs + pending rAF
- Logs `[SyncFix] _surgicalTruncateDOM conv=… cutoffIdx=… removed=…`

### Edit/regen flows
- Replace silent early-return with `_hardCancelActiveStream(conv)`
- Add `console.assert(!document.getElementById('streaming-msg'))` after truncate
- Log before/after msg counts

### Stale-task guard (SSE `state` + poll fallback)
Discriminate old vs new task via:
```js
const _aborted = stream.controller.signal.aborted;
const _superseded = conv.activeTaskId && conv.activeTaskId !== taskId;
const _isLastAborted = conv._lastAbortedTaskId === taskId;
if (_aborted || _superseded || _isLastAborted) { discard; return; }
```
No backend change needed — `conv.activeTaskId` is set to the NEW taskId by
the /api/chat/regenerate response before SSE/poll for old task fires.

### finishStream ghost-render skip
```js
const _fsLast = conv.messages[conv.messages.length - 1];
if (sm && _fsLast && _fsLast.role !== 'assistant') {
  // trailing assistant was truncated → don't render ghost msg-N
  sm.remove();
}
```

### Stop button
Added proactive `twStop(activeConvId)` after controller.abort() so a late
delta can't accumulate into a dead buffer.

## Why the guards don't drop valid events
The NEW task's `conv.activeTaskId` is set after `/api/chat/regenerate`
returns and before `connectToTask` runs → when the NEW task's SSE fires,
`activeTaskId === taskId`, `_superseded=false`, `_aborted=false`,
`_lastAbortedTaskId` is the OLD task ≠ current → not discarded. ✅

## Files Changed
- `static/js/ui.js` — _surgicalTruncateDOM, _hardCancelActiveStream (new),
  saveEditAndResend, _trySSE state branch, _pollFallback, finishStream, stop button
- `static/js/main.js` — regenerateFromUser

## Notes
- `static/js/bundle-53a42755.js` is a minified build artifact — not edited
  here, regenerated out-of-band.
- All additions carry `[SyncFix]` log prefix for easy grep during debugging.

