---
name: artifacts-scanner-prose-false-positive
description: Bug pattern: scanner spliced HTML tags from python fences + inline backticks in prose into a fake artifact
enabled: true
tags: [artifacts, scanner, bug-pattern, false-positive]
created: 2026-05-14T04:15:46Z
updated: 2026-05-14T04:15:46Z
---

# Bug — inline scanner spliced an artifact out of explanatory prose

## Symptom
After an assistant turn that *talks about* HTML wrapping (no actual artifact),
the chip showed up with title `inline.html` and the panel rendered nonsense
text taken from between two unrelated occurrences of `<html>` / `</html>`
in the assistant's own writing.

## Cause
`lib/artifacts/scanner.py:scan_message` ran two detectors on the assistant
message body:

1. `_detect_fenced_blocks` — only kept fences whose language tag is
   renderable (`html`/`markdown`/`md`/`svg`).
2. `_detect_bare_html_docs` — found `<!doctype html>` / `<html>...</html>`
   regions in prose, with `_mask_fence_regions` first wiping the spans of
   detector 1's *kept* fences.

Both detectors were too narrow:

- The mask only covered renderable fences. A `\`\`\`python` block
  containing `<html>...</html>` (e.g. an example showing how injection
  works) survived the mask and stayed in the masked text.
- Inline backtick spans (`` `<html>` ``, `` `</html>` ``) were not masked
  at all. Technical writing routinely mentions tags this way.

When detector 2 ran on the masked text, it greedily matched from the
`<html>` inside the python fence (or in prose backticks) all the way to
a later `</html>` mention elsewhere in the assistant's reply, then
persisted that spliced span as an `inline_doc` artifact.

## Fix
`scan_message` now calls `_mask_all_code_regions(text)` instead of
`_mask_fence_regions`. The new helper masks:

- Every fenced block (`_FENCE_RE.finditer`), regardless of language tag.
- Every inline ``` `code` ``` span (`_INLINE_CODE_RE`).

Length-preserving (replaces with spaces), so any `source_ref.start` /
`source_ref.end` offsets stored on a renderable fence stay valid.

Renderable fences are still detected by `_detect_fenced_blocks` first
(unchanged). The wider mask only affects the bare-HTML detector.

## Tests (regression guards)
- `test_html_inside_python_fence_not_detected` — prose with
  `\`\`\`python` containing `<html>...</html>` and a stray inline
  `` `</html>` `` mention → no artifact.
- `test_inline_backtick_html_tags_not_detected` — `` `<html>` `` /
  `` `</html>` `` in prose → no artifact.
- `test_explanatory_text_with_tag_mentions_no_artifact` — the actual
  shape of the assistant turn that triggered this bug → no artifact.

All 18 scanner tests, 81 total artifact tests pass.

## Generic lesson
Whenever you build a *content scanner* that runs on raw text, **mask
ALL code regions before any other regex pass, regardless of whether
those regions are themselves "interesting" to a different detector.**
The detectors compose; the masking step must be inclusive of every
region a downstream detector might mistake for prose.

Test fixture habit: the scanner tests already had a
``test_fenced_html_not_double_detected`` for renderable fences but
lacked the symmetric ``inside python fence`` and ``inline backtick``
cases. **For every detector that masks one input class, write a
regression covering each adjacent class** (other fence languages, no
language, inline code, single backticks, double backticks).

