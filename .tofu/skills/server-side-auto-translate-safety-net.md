---
name: server-side-auto-translate-safety-net
description: Server-side auto-translate is sole auto path (2026-05-27); push-channel listener (2026-05-29) renders results live without conversation switch
enabled: true
tags: [python, translation, server-side, safety-net, auto-translate, manager, sync-result]
created: 2026-03-25T07:11:03Z
updated: 2026-05-29T00:39:34Z
---

# Server-Side Auto-Translation (Sole Auto Path)

## History
Originally translation was a frontend-only concern (`finishStream()` in `ui.js`). A server-side safety net (`_maybe_auto_translate_assistant` in `lib/tasks_pkg/manager.py`) was added to cover offline / switched-away clients.

**Update 2026-05-27** — the dual-trigger design caused a real bug (`mpnf92ixj85uv5`): two tasks for the same msg ran in parallel, the slower one (`deepseek-v4-flash-huawei`) returned the input verbatim, and last-writer-wins clobbered the good Chinese translation. The frontend `_maybeTranslateMsg` schedule in `finishStream` was therefore **removed** — the backend safety net is now the sole automatic trigger.

**Update 2026-05-29** — the 2026-05-27 change had a follow-on regression: removing the client trigger left the active-conversation view with no listener for backend-driven translations. The server committed the translation correctly, but the bilingual block only surfaced after a conversation switch (because `loadConversationMessages` re-reads from DB). Fixed by:
1. `lib/translate/runtime.py::_do_translate` now includes `convId / msgIdx / msgId / field` in every `push_event('translate', ...)` payload (done / error / skip-done branches).
2. `static/js/translation.js` (bottom of file) registers a one-time `pushSubscribe('translate', '*', fn)` wildcard listener that resolves the target message by `_msgId` (preferred) or `msgIdx` and applies the result in place. The listener intentionally does NOT call `_patchMessageOnServer` — the server has already committed, so it just mutates in-memory state and surgically re-renders `#msg-N`. It also skips when `_translateTaskId && !_translateDone` so it doesn't race with a client-initiated poll loop.

## Current architecture
- **Auto path (the only one)**: `lib/tasks_pkg/manager.py::_maybe_auto_translate_assistant`, called from `_sync_result_to_conversation` after DB persist. Endpoint mode goes through `_trigger_endpoint_auto_translate` + `_maybe_auto_translate_critic`.
- **Server → client live render**: `lib/translate/runtime.py::_do_translate` fires `push_event('translate', task_id, {convId, msgIdx, msgId, field, translated, model, ...})` on every terminal state. `static/js/translation.js` wildcard subscriber applies it.
- **Manual click**: `translateMessage()` → `_runTranslationPipeline(mode='manual')` → `POST /api/translate/start`. Untouched. The poll loop is authoritative for client-initiated tasks; the push listener defers to it via the `_translateTaskId && !_translateDone` skip.
- **Page-load resume**: `_resumePendingTranslations` in `translation.js`. Untouched.
- **`finishStream`**: now just logs `Auto-translate handled by backend safety net — skipping client-side scheduling`. Frontend still polls `/api/chat/send-translate-status/<convId>` to display the *input* translation progress (separate concern).

## Backend dedup (`_maybe_auto_translate_assistant`)
1. `autoTranslate` setting in conversation `settings` (default true).
2. Existing `translatedContent` (skip; re-translate only if stale partial < 15%).
3. `routes.translate._translate_tasks` for a running frontend task on same conv+msgIdx (skip).

The stale-partial branch clears `translatedContent` / `_translateDone` / `_translateTaskId` with CAS, then proceeds.

## No-op detection (added 2026-05-27)
`_translate_one_chunk` in `routes/translate.py` rejects:
* Output identical to input (cheap models echoing the source verbatim — root cause of `mpnf92ixj85uv5`).
* For `target=Chinese`: source has cjk_ratio < 10% but output cjk_ratio < 5% → treat as no-op.
Both penalize the model, add it to `_excluded_models`, and retry. After `_MAX_CONTENT_RETRIES` failures the chunk's `c` is forced to `''` so the trailing emptiness check raises.

## Prompt fix (2026-05-27)
Removed rule 7 ("如果原文已经是目标语言，原样输出") from `_build_translate_prompt` — it tempted cheap models into echoing when the source had heavy code/identifier density. Already-target-language detection happens server-side via `lib.text_lang.is_predominantly_chinese` *before* the LLM is called.

## Files
- `lib/tasks_pkg/manager.py:818` — `_maybe_auto_translate_assistant`
- `lib/tasks_pkg/manager.py:983` — `_maybe_auto_translate_critic`
- `lib/tasks_pkg/endpoint.py:519` — `_trigger_endpoint_auto_translate`
- `lib/translate/runtime.py::_do_translate` — fires `push_event('translate', ...)` with full routing payload
- `routes/translate.py:219` — `_build_translate_prompt`
- `routes/translate.py:434` — `_translate_one_chunk` (no-op detector)
- `static/js/ui/streaming_ui.js` (~1071) — `finishStream` block (logs only, no scheduling)
- `static/js/translation.js` (bottom IIFE, `_wireServerPushTranslate`) — wildcard `pushSubscribe('translate', '*', ...)` listener

