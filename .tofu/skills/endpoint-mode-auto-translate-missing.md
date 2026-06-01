---
name: endpoint-mode-auto-translate-missing
description: Endpoint mode auto-translate — 4 stacked fixes; Fix 3 protects translatedContent against save_conv INSERT OR REPLACE overwrite, Fix 4 extends the pipeline to critic messages (role=user + _isEndpointReview) end-to-end
enabled: true
tags: [javascript, python, translation, endpoint, bug-fix, auto-translate, race-condition, cas, parallel-writes, finishStream, safety-net, critic, save_conv, overwrite]
created: 2026-04-21T04:42:42Z
updated: 2026-04-22T00:00:00Z
---

# Endpoint Mode Auto-Translate Missing — Multi-Layer Bug

## Symptom
In endpoint mode (planner → worker → critic loop) with `autoTranslate=ON`,
none (or only one) of the endpoint turns are auto-translated — planner, most
worker iterations, and **critic** stay in English forever. Only the LAST-to-
finish translation (typically the final worker turn) survives in the DB.

## Fix 1 (2026-04-21) — dual-skip bypass

### Skip 1: Frontend `finishStream(convId)` (static/js/ui.js)
Translation block only inspected `conv.messages[conv.messages.length - 1]`.
In endpoint mode the last message is usually a critic review or an
approved-worker turn, so earlier planner + worker turns were never considered.

### Skip 2: Backend `persist_task_result` (lib/tasks_pkg/manager.py)
Endpoint tasks deliberately skip `_sync_result_to_conversation`, taking with
it `_maybe_auto_translate_assistant`. Dedicated
`_sync_endpoint_turns_to_conversation` in `endpoint.py` had no translate hook.

Fix v1 added:
- Frontend loop over all `_isEndpointPlanner || _epIteration` assistant turns.
- Backend `_trigger_endpoint_auto_translate(task, endpoint_turns)` in
  `endpoint.py`, called from `_finalize()` and the fatal-error path.

## Fix 2 (2026-04-21 follow-up) — race in `_commit_translation_to_db`

After Fix 1, logs showed `[Endpoint:AutoTranslate] scheduled=N` with N
correct and each `[Translate] Task X done` line completed. But the DB
still showed `translatedContent` missing for some turns.

### Root cause
`_commit_translation_to_db` (routes/translate.py) did a **naive
read-modify-write** on the full `conversation.messages` JSON with **no CAS
and no per-conv lock**. N parallel translate threads (one per turn) all
read the same snapshot, each injected its own `translatedContent` into a
different index, and the last UPDATE wipes the earlier threads' writes.

### Fix
1. Per-conversation `threading.Lock` (module-level `_commit_locks` dict).
2. CAS loop on `updated_at` inside the lock (5 retries, exponential backoff).
3. Raw `db.execute` for the UPDATE (need `cur.rowcount` to detect CAS miss —
   `db_execute_with_retry` returns None).
4. Logs `[Translate] Committed …attempt=N`, `[Translate] commit CAS miss …
   retrying`, `[Translate] commit gave up after N attempts`.

## Fix 3 (2026-04-22) — `save_conv` INSERT OR REPLACE overwrites backend-committed translations

After Fix 2, logs confirmed every translate thread completed AND committed
successfully. But DB inspection still showed PLANNER translations missing
while worker translations survived — an **asymmetric** data loss that Fix 2's
CAS/lock couldn't explain.

### Root cause
Finish-sequence in endpoint mode is:
1. Backend `_trigger_endpoint_auto_translate` spawns N translate threads.
2. SSE `done` arrives on frontend → `finishStream(convId)` →
   `syncConversationToServer(conv)` → PUT `/api/conversations/<id>`.
3. `lightMsgs` passed in the PUT body is mapped from in-memory `conv.messages`
   which has **no `translatedContent`** (backend just committed, frontend
   hasn't reloaded).
4. `save_conv` did a blind `INSERT OR REPLACE INTO conversations ... messages=?`
   — unconditional full-row overwrite.
5. Planner translate threads typically finish BEFORE step 4 (planner was
   produced first) → save_conv wipes planner's `translatedContent`.
6. Worker translate threads typically finish AFTER step 4 → the Fix 2 CAS
   loop retries against the new `updated_at` and the worker's translation
   survives.

CAS inside the translate path defends against other translate threads; it
does **not** defend against the frontend-initiated full-row overwrite.

### Fix
`save_conv` (routes/conversations.py) now:
1. Reads the existing `messages` from the DB **before** the INSERT OR REPLACE.
2. Merges a fixed set of preserved keys — `translatedContent`,
   `_showingTranslation`, `_translateDone`, `_translateModel`, `_translateField`,
   `_translatedCache`, `originalContent` — from the DB row into the incoming
   payload for any matching message where the incoming snapshot lacks a
   translation but the DB has one.
3. **Strict identity check** before merge:
   - same `role`
   - same `_isEndpointPlanner` / `_isEndpointReview` / `_epIteration` markers
   - **byte-for-byte content equality** (rejects merge onto edited messages
     so we don't resurrect stale translations)
   - skip image-gen outputs (`_igResult`, `_isImageGen`)
4. Skipped entirely when `allowTruncate=true` (edit / regen intentionally
   rewrites the tail).
5. Emits `[save_conv] 🈯 Preserved N translatedContent entries ... (by role=...)`
   on success and `[save_conv] ⚠️ translatedContent loss ...` when the server
   had a translation that could not be preserved (content mismatch or
   non-truncate tail drop).
6. Also relaxed the frontend merge gate in `core.js::loadConvMsgs` — new
   helper `_mergeServerTranslations(server, local)` is called in every
   reconciliation branch (localHasUnsynced, activeTaskId, cache-fresh,
   cache-stale is natural because it assigns `serverMsgs` wholesale).

## Fix 4 (2026-04-22) — critic messages outside the pipeline

The plan explicitly required critic auto-translation (previously scoped out
by the Fix 1 docstring: "Critic review messages … intentionally skipped —
content in this project is English-authored.").

### Backend
- New `_maybe_auto_translate_critic(conv_id, content, msg_idx, db)` in
  `lib/tasks_pkg/manager.py` — thin wrapper that delegates to
  `_maybe_auto_translate_assistant` (the commit layer is role-agnostic:
  it writes to `messages[msg_idx]` by index) with a `[AutoTranslate:Critic]`
  log prefix for observability.
- `_trigger_endpoint_auto_translate` in `endpoint.py` iterates
  planner + worker + critic now and counts per-role:
  `[Endpoint:AutoTranslate] conv=X Done — scheduled=N (planner=P worker=W
   critic=C) skipped=S (messages=M)`.

### Frontend — `renderMessage` (static/js/ui.js)
- `_isCritic = isUser && msg._isEndpointReview`; `showTrans` now covers both
  `!isUser` and `_isCritic`.
- Added a critic bilingual 原文/译文 block symmetric with the assistant one
  (`copyBilingualOriginal(this, 'critic', idx)`).
- Translate-loading indicator also fires for critic.
- Translate action button allowed for `!isUser || _isCritic`.
- `translateMessage(idx)` guard relaxed:
  `if (msg.role === "user" && !msg._isEndpointReview) return;`

### Frontend — `finishStream` endpoint loop (static/js/ui.js)
- `_isEndpoint` now also triggers on `_isEndpointReview`.
- New `_maybeTranslateCritic(msg, idx)` closure mirrors `_maybeTranslateMsg`
  but targets `role==='user' && _isEndpointReview`.
- Per-turn iterator calls the right helper depending on role; log line now
  reads `Scheduling N across M assistant turn(s) + K/C critic turn(s)`.

## Verification

### Reproduction query
```python
from lib.database import get_thread_db, DOMAIN_CHAT
import json
db = get_thread_db(DOMAIN_CHAT)
row = db.execute(
    'SELECT id, messages FROM conversations WHERE id LIKE ? AND user_id=1',
    ('<conv-prefix>%',)
).fetchone()
msgs = json.loads(row[0])
for i, m in enumerate(msgs):
    tag = ('planner' if m.get('_isEndpointPlanner')
           else f"worker#{m.get('_epIteration')}" if (m.get('_epIteration') and not m.get('_isEndpointReview'))
           else 'critic' if m.get('_isEndpointReview')
           else m.get('role'))
    tc = len(m.get('translatedContent') or '')
    print(i, tag, 'translatedLen=%d' % tc, 'showingTranslation=%s' % m.get('_showingTranslation'))
```

All 4 fixes applied: every planner, every worker iteration, and every critic
row must show `translatedLen > 0` and `showingTranslation=True`.

### Log grep tags (fresh endpoint run, autoTranslate=ON)
- `[Endpoint:AutoTranslate] conv=X Done — scheduled=N (planner=P worker=W critic=C)`
- `[AutoTranslate:Critic] conv=X msg=Y ... — delegating ...`
- `[Translate] Committed translatedContent to conv=X msg=Y (…, attempt=N)`
- `[save_conv] 🈯 Preserved N translatedContent entries … (by role={'planner':1,'worker#1':1,'critic':1})`
- Must NOT see: `[save_conv] ⚠️ translatedContent loss` (fires only on edit
  truncation), `[Translate] commit gave up after N attempts`.

## Files Modified (cumulative across all 4 fixes)

### Python
- `lib/tasks_pkg/endpoint.py` — `_trigger_endpoint_auto_translate()`:
  iterates planner + worker + critic; per-role counter log; entry log +
  empty-turns guard.
- `lib/tasks_pkg/manager.py` — `_maybe_auto_translate_assistant()` upgraded
  skip-log to INFO with actual value; new `_maybe_auto_translate_critic`
  wrapper (Fix 4).
- `routes/translate.py` — per-conv `threading.Lock` + CAS loop in
  `_commit_translation_to_db` (Fix 2); split into public shim +
  `_commit_translation_inner`.
- `routes/conversations.py` — `save_conv` pre-INSERT merge of preserved
  translation keys with strict identity check (Fix 3).

### JavaScript
- `static/js/ui.js`
  - `renderMessage()`: critic-aware `showTrans` + critic bilingual block +
    translate button enabled for critic + translate-loading for critic.
  - `translateMessage()`: guard relaxed for `_isEndpointReview`.
  - `finishStream()`: endpoint loop includes critic via
    `_maybeTranslateCritic` + per-skip diagnostics + outer-gate OFF log.
- `static/js/core.js`
  - `loadConvMsgs()`: new shared `_mergeServerTranslations(src, dst)` helper
    called in all reconciliation branches so server translations merge in
    regardless of `activeTaskId`/cacheHit state.
- `index.html` — bumped `?v=` on `core.js` and `ui.js`.

## What NOT to do
- Do NOT remove the per-conv `threading.Lock` in `_commit_translation_to_db`
  (Fix 2) — CAS alone can live-lock under many identical-time retries.
- Do NOT remove the strict content-equality check in the `save_conv` merge
  (Fix 3) — merging without it resurrects stale translations on edited messages.
- Do NOT skip the `save_conv` merge when `allowTruncate=true` is set —
  that is the edit / regen flow's explicit "rewrite the tail" signal.
- Do NOT treat critic messages as generic `role=user` in `renderMessage` /
  `translateMessage` — they need both the translated-display path AND the
  user-role visual styling.  Check `_isEndpointReview` explicitly.
- Do NOT call `db_execute_with_retry` when you need `rowcount` — it returns
  None.  Use raw `db.execute` + explicit `db.commit()` inside a retry loop.
- Do NOT remove the `if not task.get('endpoint_mode')...` skip in
  `persist_task_result` — it protects the single-turn sync path; the
  endpoint-mode translate trigger lives in `_finalize` instead.

## Grep tags for debugging
- Backend:
  - `[Endpoint:AutoTranslate]` — per-run scheduling summary
  - `[AutoTranslate]` — per-message dispatch (assistant + critic)
  - `[AutoTranslate:Critic]` — critic-path delegation
  - `[Translate] Committed translatedContent` — successful commit
  - `[Translate] commit CAS miss … retrying` — Fix 2 CAS loop
  - `[save_conv] 🈯 Preserved N translatedContent entries` — Fix 3 merge
  - `[save_conv] ⚠️ translatedContent loss` — Fix 3 failed merge (investigate)
- Frontend (DevTools console):
  - `[finishStream:endpoint] Scheduling N … + K/C critic turn(s)`
  - `[finishStream] skip idx=N: <reason>` — per-skip diagnostics
  - `[loadConvMsgs] 🈯 Merged N server translation(s)` — merge helper hits
