---
name: marked-strict-del-tokenizer-fix
description: marked.js v12 GFM allows single-tilde strikethrough; override del tokenizer to require ~~
enabled: true
tags: [markdown, marked.js, frontend, bug-fix]
created: 2026-05-20T04:51:20Z
updated: 2026-05-20T04:51:20Z
---

# marked.js v12 — Strict double-tilde strikethrough

## Problem
The bundled `static/vendor/marked.min.js` v12 ships a GFM `del` tokenizer
whose regex is `/^(~~?)(?=[^\s~])([\s\S]*?[^\s~])\1(?=[^~]|$)/` — note
`~~?`, which lets a **single** tilde activate strikethrough. Spec GFM
(and GitHub itself) require `~~`. LLM output frequently emits single-tilde
spans around CJK text or hyphenated tokens (e.g. `~WMT 2014 EN-FR~`,
`~约 5 分钟~`), which then renders as unintended `<del>...</del>`.

Users reading these reports often misdiagnose the cause as
"consecutive hyphens being misread as strikethrough", but the trigger
is actually the lone `~` characters; the dashes are coincidence.

## Fix (static/js/core.js, just below `marked.setOptions({breaks:true})`)

```js
marked.use({
  tokenizer: {
    del(src) {
      const m = /^(~~+)(?=[^\s~])([\s\S]*?[^\s~])\1(?=[^~]|$)/.exec(src);
      if (!m) return false;
      return {
        type: 'del',
        raw: m[0],
        text: m[2],
        tokens: this.lexer.inlineTokens(m[2]),
      };
    },
  },
});
```

`marked.use({tokenizer:{...}})` wraps each tokenizer: returning `false`
falls through to the original; returning a token short-circuits.

## Verification
- `~~struck~~` still renders `<del>struck</del>`.
- `~single~` no longer becomes `<del>single</del>`.
- Hyphens and `--` are unaffected (not part of any del rule).

