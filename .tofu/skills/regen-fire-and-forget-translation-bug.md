---
name: regen-fire-and-forget-translation-bug
description: Bug fix: regenerateFromUser skips translation when prior translation was interrupted — originalContent exists but content===originalContent and _translateDone is falsy; fix adds _translationIncomplete detection
enabled: true
tags: [javascript, translation, regen, interrupted-translation, bug-fix, originalContent, _translateDone]
created: 2026-03-31T03:58:19Z
updated: 2026-03-31T12:41:40Z
---

# Regen Translation Bug — Interrupted Translation Not Re-attempted

## Bug (Original — Fixed Previously)
`regenerateFromUser` used fire-and-forget async translation (non-blocking `_startTranslateTask`+poll)
while `sendMessage`/`saveEditAndResend` used blocking `/api/translate`. LLM received untranslated Chinese.
**Fixed**: All three functions now use the same `_translateThenRespond` flow.

## Bug (Second — Fixed 2026-03-31)
When user interrupts during the translation phase ("翻译中" showing) and then clicks regen:

1. **Interruption state**: `msg.originalContent` = Chinese, `msg.content` = Chinese (same),
   `msg._translateDone` = undefined (translation never completed)
2. **Regen check was**: `!msg.originalContent` → false → skip translation
3. **Result**: Chinese text sent directly to LLM without translation

### Root cause
The condition `!msg.originalContent` assumes that if `originalContent` exists, translation was
already done. But when translation is aborted mid-flight, `originalContent` is set (before
translation starts) while `content` is never updated to the English result.

### Fix
```javascript
const _translationIncomplete = msg.originalContent
  && msg.content === msg.originalContent && !msg._translateDone;
if (!msg.originalContent || _translationIncomplete) {
  const hasChinese = /[\u4e00-\u9fff\u3400-\u4dbf]/.test(
    msg.originalContent || msg.content
  );
  if (hasChinese) {
    if (!msg.originalContent) msg.originalContent = msg.content;
    needsTranslation = true;
  }
}
```

**Three cases handled:**
- Case 1: `originalContent` absent → fresh Chinese, set it and translate
- Case 2: `originalContent` present, `content === originalContent`, `_translateDone` falsy
  → interrupted translation, re-translate
- Case 3: `originalContent` present, `content !== originalContent` (or `_translateDone` true)
  → translation already completed successfully, skip

### Key marker flags
- `msg.originalContent` — set before translation starts (holds Chinese text)
- `msg.content` — updated to English when translation succeeds
- `msg._translateDone` — set to `true` only on successful translation completion

### File
`static/js/main.js` → `regenerateFromUser()` → translation detection block

