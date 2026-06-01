---
name: duplicate-user-msg-blind-backend-append
description: Duplicate user message bug: root cause is blind backend append in _chat_send (+ frontend trigger)
enabled: true
tags: [python, javascript, bug-fix, race-condition, chat-send, idempotent, conversations]
created: 2026-06-01T03:52:50Z
updated: 2026-06-01T03:58:56Z
---

# Duplicate user message bug — root cause is a blind backend append

## Symptom
Two identical user messages in a conversation (e.g. mpul3n59apk26o, 2026-06-01):
row #0 = optimistic Chinese msg, `_msgId=tmp_*`, no `originalContent`; row #1 =
server-translated msg with `originalContent` + `_translateDone=true`. Both share
the SAME `timestamp`.

## TRUE root cause (backend)
Blind `messages.append(user_msg)` after loading the conv from DB, with no check
that the loaded row already ended with this same logical user message. ANY trigger
that plants the optimistic copy in the DB between create and append → duplicate.
Triggers: the front-end "rescue local-only conv" PUT, another browser tab, OR a
/chat/send network retry. A front-end-only fix is case-by-case, not root cause.

## Front-end trigger (one of several)
`loadConversationsFromServer` (`static/js/core/conversations.js:~462`) "rescue
local-only conv" branch PUTs any in-memory conv not yet on server. Fires from 60s
`setInterval` (`static/js/main.js:~985`), `visibilitychange`
(`static/js/core/cross_tab_sync.js:~79`), cross-tab `conv_saved` broadcast (`:~31`).
During the ~3s synchronous auto-translate inside `/chat/send`, the PUT plants the
optimistic Chinese row; `/chat/send` then reads `is_new=False` and appends its
translated copy → 2 rows.

## Fix (2026-06-01) — two layers, pattern closed everywhere
1. ROOT CAUSE (backend): `_append_user_msg_idempotent(messages, user_msg)` in
   `routes/chat.py` (module scope). If tail is `role=='user'` with matching
   `timestamp`, reconcile in place (server copy wins — has translation fields;
   preserve existing `_msgId`) instead of appending. Frontend payload `timestamp`
   flows through `_build_user_msg_from_payload`, so optimistic + server copies
   always share it. Verified with 4 unit cases: empty list, dup reconcile + msgId
   preserved, new turn appends, assistant tail NOT clobbered.
   APPLIED AT ALL THREE WRITE SITES:
     - `routes/chat.py` immediate-send (step 4)
     - `lib/message_queue.py:dispatch_next_queued` pre-built path (was line ~282)
     - `lib/message_queue.py:dispatch_next_queued` legacy path (was line ~370)
   The two message_queue sites use a function-local `from routes.chat import
   _append_user_msg_idempotent` — same lazy-import pattern as the pre-existing
   `from routes.chat import _resolve_conv_refs` in that function (safe vs the
   routes->lib import cycle).
2. DEFENSE IN DEPTH (frontend): `conv._sendInFlight` set in `sendMessage`
   (`static/js/main/main_send_pipeline.js`) after optimistic push, cleared in
   `finally`; `syncConversationToServer` (`static/js/core/conversations.js`)
   early-returns when set. Avoids triggering the rescue PUT at all.

## Gotcha when verifying imports
`python3 -c "import routes.chat"` OUTSIDE the running app fails with
`'Blueprint' object has no attribute 'websocket'` (routes/push.py needs Quart's
websocket Blueprint). This is an env artifact, NOT a code bug — the function-local
import only runs inside the live Quart server. Verify the helper exists via ast
parse of routes/chat.py instead.

## Deploy note
JS changes need a server restart (js_bundler builds bundle-<hash>.js at startup,
no hot-reload) + browser hard-refresh. Python changes need a server restart too.

## Lesson
When multiple code paths persist the same logical row, enforce the "atomic owner"
contract AT THE WRITE SITE (idempotent append keyed on a stable id like timestamp),
not via a convention every other writer must remember. Front-end guards reduce
trigger frequency but can't be the correctness boundary.

## Cleanup of an already-corrupted conv
`curl -X DELETE /api/v1/conversations/<id>/messages/<idx>?mode=single`
