---
name: auto-translate-skip-already-chinese
description: Auto-translate now skips messages already predominantly CJK and patches loading DOM in place
enabled: true
tags: [translation, frontend, ux]
created: 2026-05-10T07:46:08Z
updated: 2026-05-10T07:46:08Z
---

# Auto-translate (assistant → Chinese) skip + spinner DOM fix

## Spec
- Hardcoded direction: assistant content is always translated **to Chinese**
  (`_startAutoTranslateForMsg` in `static/js/ui.js`, `_resumePendingTranslations`
  in `static/js/translation.js`).  We don't pick a direction per-message.
- System prompt at `routes/translate.py:202` `_build_translate_prompt(target,
  source)` — **rule 0** now hard-pins the output language to `target`
  ("输出语言必须是 {target}").  Prevents the model from rewriting Chinese
  source as English when target=Chinese.

## Skip already-Chinese (new, 2026-05-10)
`static/js/translation.js` `_isAlreadyChinese(text)`:
- Counts CJK ideographs / non-whitespace chars.
- If ratio ≥ `_TRANSLATE_SKIP_CJK_RATIO = 0.30`, mark `_translateDone=true`
  with `_translateSkippedReason='already_target_language'` and PATCH the
  server, never starting an LLM round.
- Saves money + eliminates the bug "原文中文 · 译文是英文".

## Spinner DOM fix
`_applyTranslationStatus` used to do full-message `outerHTML` every poll
tick (every 2-4 s).  Two visible bugs:
1. `.translate-spinner` DOM was destroyed/recreated → CSS keyframe
   restarted → user perceived a frozen spinner.
2. `content-visibility:auto` on `.message` invalidated intrinsic-size on
   every replacement → `scrollTop` drifted upward until the chat looked
   like it was jumping to the top.

Now `_patchTranslateLoadingDom` patches just the `.translate-status-sub`
and `.translate-preview-sub` children of the existing
`#translate-loading-N` element.  Falls back to full re-render only when
the loading element isn't in the DOM (e.g. first render after reload).

