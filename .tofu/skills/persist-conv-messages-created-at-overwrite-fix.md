---
name: persist-conv-messages-created-at-overwrite-fix
description: Bug fix: _persist_conv_messages was overwriting created_at with now_ms on every call via INSERT OR REPLACE
enabled: true
tags: [bug-fix, database, created_at, conversations, chat]
created: 2026-04-15T08:13:17Z
updated: 2026-04-15T08:13:17Z
---

# _persist_conv_messages created_at overwrite bug

## Bug
`routes/chat.py` `_persist_conv_messages()` used `INSERT OR REPLACE` with `created_at=now_ms`,
which overwrote the original conversation creation timestamp every time a message was sent,
regenerated, or edited. This also happened twice per send (once for messages, once for activeTaskId update).

## Root Cause
The function used the same `now_ms` for both `created_at` and `updated_at` in the SQL.
The existing query only fetched `settings` — it didn't preserve the original `created_at`.

## Fix (2026-04-15)
Changed the SELECT to also fetch `created_at`:
```python
existing = db.execute(
    'SELECT settings, created_at FROM conversations WHERE id=? AND user_id=?',
    (conv_id, DEFAULT_USER_ID)
).fetchone()
```
And preserve it when the row exists:
```python
created_at = existing['created_at'] or now_ms
```
Only use `now_ms` for `created_at` when the conversation doesn't exist yet (new conversation).

## Impact
All conversations were losing their original creation timestamps on every interaction.
The `createdAt` field in the frontend and any time-based sorting was affected.

