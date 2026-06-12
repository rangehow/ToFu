---
name: server-side-auto-translate-safety-net
description: Server-side auto-translate (sole auto path); 2026-06 fixes: removed _cas_succeeded gate + pass _msgId in backend push + _renderMsgInPlace full-render fallback so active-view renders live
enabled: true
tags: [python, translation, server-side, safety-net, auto-translate, manager, sync-result]
created: 2026-03-25T07:11:03Z
updated: 2026-06-03T05:18:16Z
---

# Server-Side Auto-Translation (Sole Auto Path)

## History
Originally translation was a frontend-only concern (`finishStream()`). A server-side safety net (`_maybe_auto_translate_assistant` in `lib/tasks_pkg/manager.py`) was added to cover offline / switched-away clients.

**2026-05-27** — dual-trigger caused a clobber bug (two tasks for same msg, slower returned input verbatim, last-writer-wins). Frontend `finishStream` client-side schedule was **removed** — backend safety net is now the SOLE automatic trigger. `finishStream` only logs.

**2026-05-29** — removing client trigger left active-view with no render signal. Added `push_event('translate', ...)` in `_do_translate` + a `pushSubscribe('translate','*')` wildcard listener in `translation.js`.

## 2026-06 — Three-layer fix for "active-view never auto-translates"
Symptom: agent generation finishes while you VIEW that conversation → Chinese translation does NOT appear until you switch conversation or click translate.

**Layer 1 — CAS gate skipped the trigger entirely.** `_sync_result_to_conversation` gated auto-translate `if _cas_succeeded and ...`. The live frontend writes the conv row on stream-finish, winning the optimistic-lock race → backend CAS misses → auto-translate skipped. Fix: dropped `_cas_succeeded` → `if content and not error:` (manager.py:~886). Safe: `_maybe_auto_translate_assistant` re-reads fresh DB, dedups, commits via targeted by-id write. **Verify via logs**: `grep "AutoTranslate" logs/app.log` should show `Starting server-side auto-translation` + `Committed`.

**Layer 2 — backend push frame had no msgId.** Even after Layer 1 fired, the live render missed because the backend auto-translate task called `_do_translate(..., msg_idx, 'translatedContent')` with NO `msg_id`. Its push frame carried `msgId:''`, forcing the frontend `'*'` listener onto the fragile `msgIdx` fallback — which drifts for multi-turn agent/endpoint convs + post-stream reconciliation. Fix: resolve `_msg_id = messages[msg_idx]['_msgId']` and pass both `'msgId': _msg_id` on the task dict AND `msg_id=_msg_id or None` to `_do_translate` (manager.py `_maybe_auto_translate_assistant._run_translate`).

**Layer 3 — `_renderMsgInPlace` silently bailed.** When `document.getElementById('msg-'+idx)` was missing (index drift / not-yet-laid-out), it `return`ed silently → translation invisible until a full re-render (= switch). Fix: on missing element, fall back to scroll-preserving `renderChat(conv, false)` (`static/js/translation.js::_renderMsgInPlace`). Requires bundle rebuild (`build_bundle()`) + hard refresh.

Endpoint mode uses `_trigger_endpoint_auto_translate` at `_finalize` (no CAS gate) — Layer 1 N/A, but Layers 2/3 still relevant for its render.

## Telltale in logs that live-render is broken
Duplicate translate threads for the SAME msg: a backend `auto-translate-<conv>` thread (via=msgIdx) completes, then a frontend-initiated `translate-<taskid>` thread (via=msgId) runs ~10s later for the same msg. The 2nd only exists because the user switched/clicked → proof the push frame never applied in the active view.

## Current architecture
- **Auto path (only)**: `manager.py::_maybe_auto_translate_assistant` from `_sync_result_to_conversation` (CAS-independent). Endpoint: `_trigger_endpoint_auto_translate` + `_maybe_auto_translate_critic` at `endpoint.py::_finalize`. External CLI backends flow through `persist_task_result`→`_sync_result_to_conversation`.
- **Server→client live render**: `lib/translate/runtime.py::_do_translate` fires `push_event('translate', task_id, {convId,msgIdx,msgId,field,translated,model})`. `translation.js` wildcard subscriber applies by `_msgId` (preferred) or `msgIdx`.
- **Manual click**: `translateMessage()`→`_runTranslationPipeline(mode='manual')`. Poll loop authoritative; push listener defers via `_translateTaskId && !_translateDone`.
- **Page-load/switch resume**: `_resumePendingTranslations` (Phase 0 re-translates last untranslated assistant msg).

## Backend dedup (`_maybe_auto_translate_assistant`)
1. `settings.autoTranslate` (default true).
2. Existing `translatedContent` (skip; re-translate if stale partial <15%).
3. `routes.translate._translate_tasks` running task on same conv+msgIdx (skip).

## Files
- `lib/tasks_pkg/manager.py:~886` — trigger (CAS gate removed); `_maybe_auto_translate_assistant._run_translate` passes `_msgId`
- `lib/tasks_pkg/endpoint.py` — `_trigger_endpoint_auto_translate` (from `_finalize`)
- `lib/translate/runtime.py::_do_translate` — fires `push_event('translate', ...)`
- `lib/agent_core/push.py` — PushHub (`hub.set_loop(loop)` at server.py:1307); `'*'` taskId = all tasks on channel
- `static/js/translation.js` — `_renderMsgInPlace` (full-render fallback), bottom IIFE `_wireServerPushTranslate`
- `static/js/push.js` — WS client; `static/js/ui/streaming_ui.js:~1270` — `finishStream` (logs only)
- `lib/js_bundler.py::build_bundle()` — rebuild after JS edits
