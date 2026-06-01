---
name: js-bundler-extraction-pattern
description: Pattern for splitting static/js/*.js files: declare in _BUNDLE_FILES BEFORE main.js, also add to index.html script list
enabled: true
tags: [frontend, js-bundler, refactor]
created: 2026-05-09T08:44:48Z
updated: 2026-05-09T08:44:48Z
---

# Extracting Code From a Big static/js/*.js File

The bundler in `lib/js_bundler.py` is a pure file-concatenator (no module
system). Split-files share `window` scope after concatenation.

## Procedure

1. Identify a self-contained block in main.js / ui.js / settings.js
   (functions + their `let` module vars).
2. Move the block to a new file, e.g. `static/js/agent-backend.js`.
3. Add the new file to `_BUNDLE_FILES` in `lib/js_bundler.py`.
4. Add the new file to `index.html` (it has both bundle + fallback `<script>`
   tags) so dev mode without the bundler works.

## Ordering rules

- **Files referenced AT LOAD TIME by another file** (top-level expressions,
  not inside functions) MUST come before that file in `_BUNDLE_FILES`.
- **Files referenced only at RUNTIME** (inside functions called later) can
  appear anywhere — temporal dead zone for `let` only triggers at use.
- main.js MUST be last (it boots the app).
- i18n.js MUST be first (`t()` is used everywhere).

## Example: agent-backend.js extracted from main.js (2026-05)

The agent-backend selection block (~215 lines) was self-contained except
for runtime references to `_lastAppliedModelId` and `_saveConvToolState`
that live in main.js. Since those references are inside function bodies
called only after both files are concatenated, ordering doesn't matter
beyond "before main.js".

Bundler verified by:

```python
from lib.js_bundler import build_bundle
build_bundle()
```

Then grep the resulting `bundle-*.js` for expected symbols.

