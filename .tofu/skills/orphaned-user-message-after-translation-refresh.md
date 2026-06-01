---
name: orphaned-user-message-after-translation-refresh
description: Bug fix: orphaned user message after page refresh during translation — Case E skipped _needsLoad shell convs; fix adds lastMsgRole/lastMsgTimestamp to settings metadata for shell-based orphan detection, plus message loading guard in startAssistantResponse
enabled: true
tags: [javascript, python, bug-fix, translation, page-refresh, orphaned-message, initActiveTasks, sendMessage, recovery, _needsLoad, metadata, shell-conv, Case-E]
created: 2026-03-25T05:14:55Z
updated: 2026-03-30T07:24:34Z
---

# Orphaned User Message After Page Refresh — Case E Shell Conv Bug

## Root Cause (v2 — metadata-based fix)

When user sends a message with `autoTranslate` enabled:
1. `sendMessage()` pushes user message to conv, saves to DB, starts translation
2. Translation blocks `sendMessage()` (awaits completion before `startAssistantResponse()`)
3. **User refreshes page** during translation wait
4. JS context killed — translation may complete on server side but `startAssistantResponse()` never fires
5. Page reloads → `initActiveTasks()` runs
6. **BUG**: Conversation is a `_needsLoad=true` shell (metadata only, messages not loaded)
7. Case E guard `!conv._needsLoad && conv.messages.length > 0` **excludes** shell convs
8. Orphan is never detected → conversation stuck with unresponded user message

## Fix (4 files)

### 1. `static/js/core.js` — syncConversationToServer
Add `lastMsgRole` and `lastMsgTimestamp` to settings payload:
```js
settings = {
  ...existing...,
  lastMsgRole: lastMsg?.role || null,
  lastMsgTimestamp: lastMsg?.timestamp || null,
};
```

### 2. `static/js/core.js` — _applySettingsToConv
Map the new fields from settings to conv properties:
```js
if (settings.lastMsgRole) conv.lastMsgRole = settings.lastMsgRole;
if (settings.lastMsgTimestamp) conv.lastMsgTimestamp = settings.lastMsgTimestamp;
```

### 3. `static/js/main.js` — initActiveTasks Case E
Replace the guard to also check `_needsLoad` shells using metadata:
```js
if (!conv._needsLoad && conv.messages.length > 0) {
  // loaded conv path (existing)
} else if (conv._needsLoad && conv.lastMsgRole) {
  // ★ Shell conv: use metadata from settings
  _caseELastRole = conv.lastMsgRole;
  _caseELastTimestamp = conv.lastMsgTimestamp;
}
```

### 4. `static/js/main.js` — startAssistantResponse
Add message-loading guard for shell convs dispatched by Case E:
```js
if (conv._needsLoad) {
  await loadConversationMessages(convId);
  if (conv.messages.length === 0) return;
}
```

### 5. `routes/conversations.py` — save_conv
Backend auto-injects `lastMsgRole`/`lastMsgTimestamp` into settings from messages:
```python
if msg_count > 0:
    settings_dict['lastMsgRole'] = raw_messages[-1].get('role')
    settings_dict['lastMsgTimestamp'] = raw_messages[-1].get('timestamp')
```

### 6. `lib/tasks_pkg/manager.py` — _sync_result_to_conversation
Updates `lastMsgRole`/`lastMsgTimestamp` in settings when task completes:
```python
if messages:
    lm = messages[-1]
    s['lastMsgRole'] = lm.get('role')
    s['lastMsgTimestamp'] = lm.get('timestamp')
```

## Key Insight
The metadata (settings JSON) is the **only** data available for `_needsLoad` shell convs.
By persisting last-message info in settings, orphan detection works without loading full messages.

