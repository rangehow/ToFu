---
name: markdown-cjk-missing-space-heading-list-bug
description: Bug: CJK text after markdown markers (###, -, 1.) without space renders as plain text in marked.js — fixed with regex normalizer
enabled: true
tags: [javascript, markdown, cjk, bug-fix, rendering]
created: 2026-04-11T04:08:29Z
updated: 2026-04-11T04:08:29Z
---

# Markdown CJK Missing Space Bug

## Problem
CommonMark requires a space after heading markers (`###`) and list markers (`-`, `*`, `+`, `1.`).
LLMs and machine translation APIs often omit this space when generating CJK (Chinese/Japanese/Korean) text:
- `###标题` → renders as plain text `<p>###标题</p>` instead of `<h3>标题</h3>`
- `-项目` → renders as plain text instead of `<li>项目</li>`
- `1.步骤` → renders as plain text instead of ordered list

This is especially common with:
1. NiuTrans MT provider stripping spaces during translation
2. LLMs generating Chinese/Japanese headings without the space
3. English text works better because spaces are natural between words

## Fix
Added a 3-line regex normalizer in `renderMarkdown()` (core.js), right before `_fixTableExtraPipes()`:

```javascript
p = p.replace(/^(\s{0,3}#{1,6})([^\s#])/gm, '$1 $2');      // headings
p = p.replace(/^(\s*[-*+])([^\s\-*+\d])/gm, '$1 $2');       // unordered lists
p = p.replace(/^(\s*\d+\.)([^\s])/gm, '$1 $2');               // ordered lists
```

Key design:
- Uses `^` with `gm` flag → only matches line-start, no mid-line false positives
- `\s{0,3}` allows 0-3 leading spaces (CommonMark spec)
- `[^\s#]`, `[^\s\-*+\d]`, `[^\s]` prevents matching already-spaced markers
- `---` / `***` horizontal rules are NOT affected (matched by exclusion patterns)

## Also Fixed
Changed streaming `md-stream-tail` from `<span>` to `<div>` — block elements (`<h3>`, `<ul>`)
inside `<span>` (inline element) is invalid HTML and browsers auto-correct it, breaking rendering.

