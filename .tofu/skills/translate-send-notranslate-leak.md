---
name: translate-send-notranslate-leak
description: notranslate ⟦NT_N⟧ placeholder + reattach lifecycle; EVERY frontend translated-content render site (incl. seg-narration in tool_rounds.js) MUST call stripNoTranslateTags() before renderMarkdown as defense-in-depth
enabled: true
tags: [translation, bug, chat, notranslate]
created: 2026-04-29T13:48:13Z
updated: 2026-07-08T11:34:32Z
---

# `<notranslate>` block handling in translation pipeline

## Current design (2026-05) — in-place placeholder, LLM may reposition
`_extract_notranslate_blocks(text)` (now in `lib/translate/notranslate.py`) replaces each
`<notranslate>...</notranslate>` / `<nt>...</nt>` block with an
`⟦NT_N⟧` placeholder (full-width brackets, order-numbered). Placeholder
is initially emitted at the block's source-text position so the LLM has
positional context. Returns `(text_with_placeholders, blocks)` where
`blocks = [{'placeholder', 'content'}, ...]`.

**Key rule (2026-05 refinement)**: the translation prompt explicitly ALLOWS
the LLM to REPOSITION `⟦NT_N⟧` markers within the translated text for
target-language fluency. The marker must only appear **exactly once and intact**;
order is NOT enforced, because the `⟦NT_N⟧ → content` mapping is held in Python.

`_reattach_notranslate_blocks(translated, blocks)` does
`str.replace(ph, content, 1)` per block. **Critical safety net**: if the
LLM dropped a placeholder, the orphaned content is appended at the end with
`logger.warning('...dropped by LLM, appending at end as fallback')` — content
is NEVER silently lost. Also strips mangled placeholder fragments via
`_NT_PLACEHOLDER_RE` (`⟦NT_\d+⟧`) and the tolerant `_NT_PLACEHOLDER_LOOSE_RE`
(bracket class covers CJK-localized 【】/〔〕/《》/「」 forms + full-width digits).

## FRONTEND defense-in-depth — EVERY translated-content render site must strip
The backend reattach is best-effort; cheap LLMs mangle/localize the markers, so
the frontend is the last line of defense. `stripNoTranslateTags()` is defined in
`static/js/ui/conversation_list.js` (strips `<notranslate>`/`<nt>` tags + the
`⟦NT_N⟧` / localized 【NT_N】 placeholder class). Call it BEFORE `renderMarkdown()`
at every site that renders translated content:
- Streaming preview — `translation.js` (`_renderStreamingTranslatePreview`, seg narration).
- Settled bilingual view — `chat_render.js` (~1165/1231/1261).
- Partial preview / sidebar — `chat_render.js` (~1300), `conversation_list.js` (~541).
- **Settled interleaved tool-log narration — `ui/tool_rounds.js` `_renderTimelineBatch`
  (`stripNoTranslateTags(_segText)`, ~L2658).** ← FIXED 2026-07-08; this was the ONE
  site missing the strip, so a mangled marker rendered CLEAN during streaming (that
  path strips) then SNAPPED DIRTY at finalize (this path didn't). Guarded by
  `tests/test_frontend_segment_timeline.py` (positive strip test + neuter proving
  `⟦NT_0⟧` leaks when the call is removed).

**Rule**: when adding a NEW translated-content render path, mirror the
`strip → renderMarkdown` idiom. A clean streaming preview that goes dirty at
finalize is the fingerprint of a render site that forgot the strip.

## NOT an artifact: balanced `*…*` emphasis surviving translation
Balanced emphasis carried across translation (e.g. source `*styling*` →
translated `*样式*`) renders as `<em>` CORRECTLY — CommonMark renders `*`-emphasis
intraword by design (`_` is the one with the intraword restriction), and
`_cjkFriendlyPreprocess` is NOT involved (RAW vs preprocessed output is
byte-identical). Do NOT "fix" this by suppressing intraword `*` for CJK — it
would break legit emphasis like `这是*重点*内容`. Verify with the vendored
`marked` before assuming a `*` is an artifact. (Han has no true italic → synthetic
oblique slant on a lone CJK term looks odd, but that's a typographic design call,
not a bug.)

## Both TEXT entry points mirror the lifecycle
- async path + sync `/api/chat/send` auto-translate — both extract → translate inner → reattach.
- `lib/tasks_pkg/conv_message_builder.py` strips notranslate tags before the main LLM sees them.

## Lesson
Any new translation entry point MUST mirror the notranslate lifecycle
(extract → translate inner → reattach), and any new FRONTEND render of
translated content MUST call `stripNoTranslateTags()` before `renderMarkdown()`.
