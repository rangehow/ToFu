---
name: finishStream-global-autoTranslate-bug
description: Bug fix: autoTranslate cross-talk between conversations — toggling translation OFF in conv B overwrites conv A's autoTranslate while task runs, causing finishStream to skip translation; fix: freeze autoTranslate at send-time + server-side auto-translate safety net
enabled: true
tags: [javascript, python, debugging, autoTranslate, cross-talk, race-condition, finishStream, sendMessage, _saveConvToolState, server-side-translate]
created: 2026-03-23T06:34:55Z
updated: 2026-03-25T07:57:39Z
---

# autoTranslate Cross-Talk Between Conversations

## Bug Pattern
Toggling autoTranslate OFF in one conversation contaminates another conversation's setting, causing its assistant response to never be translated.

## Root Cause — 3 Contamination Paths

### Path 1: `_saveConvToolState()` (main.js)
Called on every toggle click. Writes `conv.autoTranslate = !!autoTranslate` (global) to the active conv — even if that conv has an active task whose `finishStream` will later read this value.

### Path 2: `loadConversation()` (main.js)
When switching convs: `prevConv.autoTranslate = !!autoTranslate` — writes the global to the previous conv. If the user toggled autoTranslate OFF in the current conv, then switches, the prev conv gets `false`.

### Path 3: `_dispatchQueuedMessage()` (main.js)
Uses global `autoTranslate` instead of per-conv `conv.autoTranslate` for user message translation.

## Scenario
1. User sends message in conv A (`autoTranslate: true`) → task starts
2. User switches to conv A and toggles autoTranslate OFF → `_saveConvToolState()` writes `convA.autoTranslate = false` even though task is running
3. Task finishes → `finishStream(convA.id)` reads `conv.autoTranslate = false` → translation skipped
4. Backend `_sync_result_to_conversation` reads `settings.autoTranslate = false` from DB → also skips

## Fix — Freeze at Send-Time

### Frontend: Guard in `_saveConvToolState()` and `loadConversation()`
```javascript
// _saveConvToolState():
const _taskActive = !!(conv.activeTaskId || activeStreams.has(conv.id));
if (!_taskActive) {
  conv.autoTranslate = !!autoTranslate;
}

// loadConversation() prevConv save:
const _prevTaskActive = !!(prevConv.activeTaskId || activeStreams.has(prevConv.id));
if (!_prevTaskActive) {
  prevConv.autoTranslate = !!autoTranslate;
}
```

### Frontend: Per-conv reads in `_dispatchQueuedMessage()` and `sendMessage()`
```javascript
// _dispatchQueuedMessage:
const _convAutoTranslate = conv.autoTranslate !== undefined ? !!conv.autoTranslate : true;

// sendMessage:
const _sendAutoTranslate = conv.autoTranslate !== undefined ? !!conv.autoTranslate : !!autoTranslate;
```

### Backend: Server-side auto-translate safety net
`_maybe_auto_translate_assistant()` in `lib/tasks_pkg/manager.py` — called from `_sync_result_to_conversation()` after persisting the assistant content. Spawns a background thread to translate if:
- `settings.autoTranslate` is `true` (default)
- Content needs translation (heuristic)
- No existing translation in DB
- No running frontend translate task (dedup)

## Files Modified
- `static/js/main.js` — `_saveConvToolState()`, `loadConversation()`, `sendMessage()`, `_dispatchQueuedMessage()`
- `static/js/ui.js` — `finishStream()` (already reads per-conv)
- `lib/tasks_pkg/manager.py` — `_maybe_auto_translate_assistant()`, `_needs_translation()`
- `routes/common.py` — `_commit_translation_to_db()` sets `_translateDone = True`

