---
name: translate-send-notranslate-leak
description: notranslate handling: in-place ⟦NT_N⟧ placeholder + safety fallback (2026-05 redesign)
enabled: true
tags: [translation, bug, chat, notranslate]
created: 2026-04-29T13:48:13Z
updated: 2026-05-06T03:05:11Z
---

# `<notranslate>` block handling in translation pipeline

## Current design (2026-05) — in-place placeholder, LLM may reposition
`_extract_notranslate_blocks(text)` in `routes/translate.py` replaces each
`<notranslate>...</notranslate>` / `<nt>...</nt>` block with an
`⟦NT_N⟧` placeholder (full-width brackets, order-numbered). Placeholder
is initially emitted at the block's source-text position so the LLM has
positional context. Returns `(text_with_placeholders, blocks)` where
`blocks = [{'placeholder', 'content'}, ...]`.

**Key rule (2026-05 refinement)**: the translation prompt explicitly ALLOWS
the LLM to REPOSITION `⟦NT_N⟧` markers within the translated text for
target-language fluency (e.g. SVO→SOV word order, adjective placement).
The marker must only appear **exactly once and intact**; order is NOT
enforced, because the `⟦NT_N⟧ → content` mapping is held in Python. This
is important: forcing word order from the source language into the target
makes translations sound robotic.

`_reattach_notranslate_blocks(translated, blocks)` does
`str.replace(ph, content, 1)` per block. **Critical safety net**: if the
LLM dropped a placeholder, the orphaned content is appended at the end with
`logger.warning('...dropped by LLM, appending at end as fallback')` — content
is NEVER silently lost. Also strips mangled placeholder fragments
(`⟦NT_\d+⟧` and a tolerant `[\u27e6\[\(]\s*N\s*T\s*_\s*\d+\s*[\u27e7\]\)]` regex).

Prompt rule 5 in `_build_translate_prompt`:
"保留 ⟦NT_N⟧ 占位符完整不变 — 不要翻译/删除/拆分/加空格；但**可以**根据目标语言的语法、语序把它移到译文中最自然的位置（顺序不强制）"

## Why placeholder over prefix/suffix split (the OLD design)
The old `_extract_notranslate_blocks` classified each block as 'prefix' (no
text before it) or 'suffix' (text before it), then rebuilt as
`prefixes + translated + suffixes`. This caused any block in the **middle**
of the user input to snap to the bottom of the translated output — a usability
bug when users wrap a URL/code in the middle of a sentence.

## Why this is safe (vs the disabled code-block placeholder scheme)
The 2025-06 code-block extraction was disabled because cheap LLMs dropped
placeholders and `str.replace` silently lost 85% of content. The
notranslate placeholder scheme avoids that fate via:
1. Fallback append on missing placeholder (with warning).
2. Loose-pattern stripping of mangled markers.
3. Order-numbered (not random) IDs so a partial drop is still recoverable.
4. Full-width `⟦⟧` brackets preserve better than `[]` or `<>` on cheap MT.

## Both entry points must mirror the lifecycle
- `routes/translate.py:_do_translate` — async path
- `routes/chat.py:_auto_translate_user` — sync path used by `/api/chat/send`
  (imports `_extract_notranslate_blocks` / `_reattach_notranslate_blocks` /
  `_strip_notranslate_tags` from translate.py)

UI: all `msg.content` rendering in `static/js/ui.js` should call
`stripNoTranslateTags()` as defense in depth.

`lib/tasks_pkg/conv_message_builder.py` already strips notranslate tags
before sending to the main LLM (so the main agent never sees them — only
the inner content is preserved).

## Lesson
Any new translation entry point MUST mirror the notranslate lifecycle
(extract → translate inner → reattach). Grep for `_translate_one_chunk`
call sites when auditing.
