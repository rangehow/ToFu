---
name: server-side-auto-translate-safety-net
description: Server-side auto-translate (sole auto path); _do_translate now pushes running/started/partial frames so active view shows live streaming indicator; translation.js '*' subscriber handles running frames; redesigned .translate-loading CSS
enabled: true
tags: [python, translation, server-side, safety-net, auto-translate, manager, sync-result]
created: 2026-03-25T07:11:03Z
updated: 2026-06-15T11:33:18Z
---

## 2026-06 — Live indicator + streaming polish for server auto-translate
**Symptom user reported:** auto-translate gives no feedback in the ACTIVE view — you had to switch conversations to even see translation started, and the streaming preview was an ugly gray pre-wrap blob.

**Root cause:** `lib/translate/runtime.py::_do_translate` only pushed `done`/`error` frames. The frontend `_wireServerPushTranslate` subscriber ignored everything except `status==='done'`. `finishStream` deliberately skips client-side scheduling (backend safety net is sole auto path), so the active view had NO signal until the final translation landed. The `_translateDone===false` spinner is only set by the client poll path, which never runs for auto-translate.

**Fix (3 files + CSS):**
1. `lib/translate/runtime.py::_do_translate` — added `_push_running(partial/status_message/status_kind)` helper. Fires: (a) a `started` frame the instant the worker picks up (before first SSE delta); (b) on every throttled `_on_progress` partial (250ms throttle already existed); (c) on every `_on_status` retry event. Only fires when `conv_id` known. Frame shape: `{type:'running',status:'running',convId,msgIdx,msgId,field,partial?,statusMessage?,statusKind?}`.
2. `static/js/translation.js` — `_wireServerPushTranslate` now handles running frames: flips `msg._translateDone=false` (+ `_renderMsgInPlace`) to show indicator, then routes partial/status through `_applyTranslationStatus`. Guards: only `translatedContent` field, skip if already translated/done, skip if client poll loop owns the task (`_translateTaskId && !_translateDone`). `_patchTranslateLoadingDom` updated for new DOM: renders partial as MARKDOWN into `.translate-preview .md-content` (cached via `previewEl._lastPartial`), status moved under `.translate-loading-head`.
3. `static/js/ui/chat_render.js` — restructured `.translate-loading`: `.translate-loading-head` (spinner+label) + `.translate-status-sub` + `.translate-preview` (markdown body + blinking `.translate-caret`). Partial rendered via `renderMarkdown(stripNoTranslateTags(...))` so it morphs smoothly into the final bilingual 译文.
4. `static/styles.css` — `.translate-loading` now a bordered card with top sheen bar (`translate-sheen`), shimmer label (`translate-shimmer`), fade-in preview with bottom mask-image gradient, blinking caret.

**Guardrail:** must rebuild bundle (`build_bundle()`) + hard refresh after JS/CSS edits. `started`/`in_progress` are in `_TRANSLATE_BENIGN_STATUS_KINDS` (don't render as ⚠ warning). renderMarkdown wrapped in try/catch → escapeHtml fallback (partial markdown can be malformed mid-stream).

