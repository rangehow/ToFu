---
name: cjk-friendly-emphasis-letter-guard
description: CJK-friendly markdown emphasis ZWS injection must require letter on opposite side
enabled: true
tags: [frontend, markdown, cjk, renderer]
created: 2026-05-20T10:16:26Z
updated: 2026-05-20T10:16:26Z
---

# CJK-friendly emphasis preprocessor — letter-on-opposite-side guard

`static/js/core.js` has `_cjkFriendlyPreprocess()` which inserts U+200B
between emphasis runs (`*+`, `_+`, `~+`) and adjacent CJK punctuation, so
patterns like `**这是中文。**接下来` and `从**「重点」**开始` can be
parsed by marked.js's stock CommonMark flanking rules.

**The trap**: applying ZWS unconditionally breaks cases where BOTH sides
of the closing `**` are punctuation, e.g. `**(Table 3)**：` or
`**[图 4]**，`. Without ZWS, marked closes the run via CM rule 2b
(closing `**` is preceded by punct AND followed by punct → still
right-flanking + not after a left-flanking → closes). Inserting ZWS
between the run and the trailing `：` makes the run end in `**ZWS`, where
the character right after `**` is now a non-punct/non-ws (Cf format) —
which causes the run to be classified differently and the `<strong>`
never opens.

**Fix**: only insert ZWS when the OPPOSITE side of the run is a letter
or number (`\p{L}\p{N}` with the `u` flag). This matches the cases the
upstream markdown-cjk-friendly spec actually targets (`」**开` /
`点**：`) and skips the punct-on-both-sides cases where stock
CommonMark already handles correctly.

```js
const _LETTER_OR_NUM = '[\\p{L}\\p{N}]';
const _CJK_FRIENDLY_BEFORE_RE = new RegExp(
  '(' + _CJK_PUNCT_CLASS + ')(' + _EMPH_RUN_CLASS + ')(?=' + _LETTER_OR_NUM + ')', 'gu');
const _CJK_FRIENDLY_AFTER_RE = new RegExp(
  '(' + _LETTER_OR_NUM + ')(' + _EMPH_RUN_CLASS + ')(' + _CJK_PUNCT_CLASS + ')', 'gu');
```

Use `'gu'` (Unicode flag) so `\p{L}\p{N}` works. Avoid lookbehind for
Safari <16.4 — capture the letter in the AFTER rule and re-emit it.

**Test command**: drive marked.js with the project's vendor copy via
node and the `Function` constructor:
```
node -e 'const c = require("fs").readFileSync("static/vendor/marked.min.js","utf-8");
const m = new Function("globalThis","window","self","document",
  c+"; return globalThis.marked || marked;")({},{},{},{});
console.log(m.parse("* **架构对比 (Table 3)**：YOCO-U"));'
```

