---
name: unified-translation-pipeline-frontend
description: All three frontend translation paths (manual click / auto / resume retry) funnel through _runTranslationPipeline in static/js/translation.js — never duplicate the poll loop or persistence path
enabled: true
tags: [frontend, translation, convention, ui]
created: 2026-05-09T05:56:56Z
updated: 2026-05-09T05:56:56Z
---

# Unified Frontend Translation Pipeline

## Single entry point: `_runTranslationPipeline(conv, idx, msg, opts)`

Lives in `static/js/translation.js`. All three call sites delegate:

| Call site | File:line | Mode |
|---|---|---|
| Manual click (`translateMessage`) | `static/js/ui.js` | `mode: 'manual'` |
| Auto-translate (`_startAutoTranslateForMsg`) | `static/js/ui.js` (thin wrapper) | `mode: 'auto'` |
| Edit-translate in `saveEditOnly` | `static/js/ui.js` | `mode: 'auto'`, `field: 'content'` |
| Resume after page load (`_resumePendingTranslations`) | `static/js/translation.js` | `mode: 'auto'`, with `existingTaskId` |

## Invariants

1. **Server persistence**: targeted `_patchMessageOnServer` ONLY. Never call `syncConversationToServer` (full-conv PUT) — it races with the queue-dispatcher in `finishStream`.
2. **Re-render**: surgical `outerHTML` replacement of `#msg-N` via `_renderMsgInPlace`. Never `renderChat(conv)` — it destroys `#streaming-msg`.
3. **Poll cadence**: 2s × 5 → 4s × 35 (~150s total). Constants `_TRANSLATE_POLL_FAST_DELAY` / `_TRANSLATE_POLL_SLOW_DELAY` / `_TRANSLATE_POLL_MAX_ATTEMPTS`.
4. **Cache field**: write `translatedContent`. Also write `_translatedCache` for legacy readers; reads check both.
5. **Stale-15% detection**: `_isStalePartialTranslation` runs at pipeline entry — re-translates if `translatedContent.length < content.length * 0.15` (mid-stream-partial detection).
6. **Retry budget**: `mode='auto'` retries up to 2× via `msg._translateRetryCount`. `mode='manual'` surfaces error immediately.

## When adding a new translation trigger

DO:
- Call `_runTranslationPipeline(conv, idx, msg, { sourceLang, targetLang, field, mode })`
- Reuse helpers `_applyTranslationDone` / `_applyTranslationError` if you need terminal handling without start.

DON'T:
- Inline a `setTimeout`/`_pollTranslateTask` loop — duplicates 100+ lines that diverge on every change.
- Call `_startTranslateTask` directly without going through the pipeline.
- Use `syncConversationToServer` for translation persistence.

## PATCH whitelist

`_PATCH_MSG_WHITELIST` in `routes/conversations.py` accepts:
`translatedContent`, `_translateModel`, `_translateDone`, `_translateTaskId`,
`_translateField`, `_translateError`, `_translatedCache`, `_originalContent`,
`_showingTranslation`, `content`, `originalContent`.

Any new `_translate*` field added to messages must be appended here OR it
silently drops at the PATCH server route.

