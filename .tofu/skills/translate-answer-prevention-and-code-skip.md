---
name: translate-answer-prevention-and-code-skip
description: Translation feature: anti-answer wrapping, code block placeholder extraction DISABLED (caused 85% content loss), auto-translate kept for both user input and assistant responses, prompt instructs LLM to preserve code blocks directly
enabled: true
tags: [python, javascript, translation, bug-fix, code-blocks, placeholder, content-loss]
created: 2026-03-23T05:08:50Z
updated: 2026-03-23T09:11:23Z
---

# Translation Feature — Code Block Handling & Auto-Translate Scope

## Code Block Extraction: DISABLED (2025-06)

### The Bug
`_prepare_for_translation()` extracted code blocks/logs/URLs into `⟦TAG_N⟧` placeholders before
sending to the cheap translation LLM. But cheap models (qwen3.5-plus etc.) frequently **dropped
or mangled the placeholder markers** in their output. Then `_restore_after_translation()` did
`str.replace(ph, original, 1)` — when the placeholder wasn't found, the replace silently did
nothing and the code block was **permanently lost**.

Example: 7970-char input → 1148-char output (85% content lost).

### The Fix
- `_prepare_for_translation()` is now a **no-op** — returns `(text, [])`.
- The translation prompt rule 4 was updated to instruct the LLM to preserve code blocks directly:
  `"4. **保留代码块原样不变** — ```...``` 围栏代码块的内容不要翻译，保持原样"`
- `_restore_after_translation()` kept as-is (harmless with empty restorations list, and has
  safety fallback for dropped placeholders if ever re-enabled).
- The helper functions `_has_translatable_text`, `_restore_after_translation` etc. are kept
  but effectively unused — no restorations are ever created.

### Key Insight
Don't try to be clever with placeholder extraction for cheap LLMs. They can handle markdown
with code fences just fine when instructed properly in the prompt. The placeholder mechanism
was a premature optimization that created a catastrophic failure mode.

## Auto-Translate Scope
- **User input box**: Chinese → English (field: `content`, replaces original)
- **Assistant responses**: English → Chinese (field: `translatedContent`, bilingual display)
- Both are enabled via `autoTranslate` flag in localStorage
- Manual translate button works for any message regardless of autoTranslate setting

## Key Files
- `routes/common.py`: `_prepare_for_translation()`, `_restore_after_translation()`, `_build_translate_prompt()`, `_do_translate()`
- `static/js/ui.js`: `finishStream()` auto-translate block (~line 4310)
- `static/js/main.js`: `_callTranslateAPI()`, `_startTranslateTask()`

