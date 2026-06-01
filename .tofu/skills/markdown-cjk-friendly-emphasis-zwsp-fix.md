---
name: markdown-cjk-friendly-emphasis-zwsp-fix
description: CJK emphasis bug fixed via U+200B preprocessor in renderMarkdown — no marked.js extension needed
enabled: true
tags: [javascript, markdown, cjk, emphasis, marked, rendering, bug-fix]
created: 2026-04-21T09:44:29Z
updated: 2026-04-21T09:44:29Z
---

# CJK-Friendly Emphasis (markdown-cjk-friendly) in Tofu

## Problem
CommonMark's emphasis flanking rules treat CJK punctuation (。，！？（）「」：etc.)
the same as ASCII punct, so these fail to render as emphasis:

- `**这是中文。**接下来` — closing `**` not right-flanking (preceded by `。`, followed by CJK char that isn't ws/punct)
- `从**「重点」**开始` — opening `**` not left-flanking (followed by `「`)
- `**重点：**说明` — closing `**` fails

Upstream fix is the `markdown-cjk-friendly` spec (tats-u/markdown-cjk-friendly on GitHub)
which ships ports for markdown-it / remark / micromark / goldmark / Markdig — but NOT marked.js.

## Fix (static/js/core.js, inside renderMarkdown)
Since marked.js has no extension API for the inline emphasis tokenizer, we insert
U+200B (ZERO WIDTH SPACE, category Cf — neither ws nor punct per CommonMark) between
delimiter runs and adjacent CJK punctuation. Stock flanking rules then fire correctly.

```javascript
const _CJK_PUNCT_CLASS =
  '[\u3000-\u303F\uFE30-\uFE4F\uFE50-\uFE6B\uFF01-\uFF0F\uFF1A-\uFF20\uFF3B-\uFF40\uFF5B-\uFF65]';
const _EMPH_RUN_CLASS = '(?:\\*+|_+|~+)';  // covers **bold**, _italic_, ~~strike~~

function _cjkFriendlyPreprocess(text) {
  if (!/[\u3000-\uFFEF]/.test(text)) return text;  // fast reject
  if (!/[*_~]/.test(text)) return text;
  return text
    .replace(/(<cjk-punct>)(<delim>)/g, '$1\u200B$2')
    .replace(/(<delim>)(<cjk-punct>)/g, '$1\u200B$2');
}
```

## Critical placement
Call `_cjkFriendlyPreprocess(p)` AFTER fenced/inline code AND math are extracted into
`\x02CODEn\x03` / `\x02MATHn\x03` placeholders, BEFORE placeholders are restored. This
guarantees `*`/`_`/`~` inside code spans/math are never modified.

## What this does NOT cover
- `你**好**吗` (CJK char adjacent to `*`) — actually already works in plain CommonMark
  because CJK chars aren't punctuation. No fix needed.
- Full spec port (CJK character detection via East Asian Width W/F/H, Hangul script,
  ideographic variation selectors) — would require a proper marked extension. The
  ZWSP preprocessor covers the common punctuation-adjacent bug that LLM output hits.

## Related existing fix
`markdown-cjk-missing-space-heading-list-bug` — `###标题` → `### 标题` normalizer,
also in `renderMarkdown`. Both fixes coexist.

## Entry point
`static/js/core.js` → `renderMarkdown(text)` (~line 2587). All callers benefit
(ui.js streaming, branch.js, memory.js, trading/state.js).

The bundle `bundle-*.js` is auto-rebuilt by `lib/js_bundler.py` on next server start.

