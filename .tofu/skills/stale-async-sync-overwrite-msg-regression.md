---
name: stale-async-sync-overwrite-msg-regression
description: Bug fix: async translate poll callback captured stale conv.messages snapshot → PUT with fewer messages overwrote fresher data, losing user messages
enabled: true
tags: [frontend, race-condition, data-loss, sync, translate]
created: 2026-04-04T13:20:16Z
updated: 2026-04-04T13:20:16Z
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
- `initActiveTasks Case D` — pops ghost empty assistant

## Forensic Pattern in Logs
```
PUT saves 52 msgs  ← user sends message
PUT saves 51 msgs  ← STALE translate sync (lower count = regression!)
PUT saves 52 msgs  ← re-sync but user msg already lost
```

