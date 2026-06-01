---
name: js-bundler-missing-compaction-viewer
description: Bug: compaction-viewer.js was registered in index.html but missing from _BUNDLE_FILES — silent no-op in production
enabled: true
tags: [frontend, bundler, bug]
created: 2026-05-15T10:25:13Z
updated: 2026-05-15T10:25:13Z
---

# Bundler omission — compaction-viewer.js silent no-op (fixed 2026-05-15)

## Symptom
- Compaction chips disappear from assistant messages after page reload.
- Clicking the context-bar compaction button logs `compaction-viewer not loaded` (the `else` branch in ui.js:1518).
- `window.openCompactionViewer` and `window.attachCompactionMarkersToConversation` undefined in browser console even though the file existed at `static/js/compaction-viewer.js` and was referenced in `index.html`.

## Root cause
`routes/common.py:_APP_SCRIPTS_RE` strips ALL `<script defer src="static/js/*.js">` tags from served HTML and replaces them with the single bundle tag. The bundler only concatenates files in `_BUNDLE_FILES` (lib/js_bundler.py) — anything not listed is stripped but never re-added. `compaction-viewer.js` was missing from the list.

## Fix
Added `'compaction-viewer.js'` to `_BUNDLE_FILES` in `lib/js_bundler.py`, placed right BEFORE `context-bar.js` (after `main.js`). It only depends on `window.*` runtime globals (escapeHtml, showToast, updateContextBar) and is consumed by context-bar.js (`window.openCompactionViewer`) and core.js (`attachCompactionMarkersToConversation`) — both via `typeof === 'function'` checks at runtime, so post-main.js placement is safe.

## How to audit (per CLAUDE.md §3.2.1)
```bash
bash -c "diff <(grep -oE 'static/js/[a-z_-]+\.js' index.html | sed 's|static/js/||' | sort -u) <(python3 -c \"from lib.js_bundler import _BUNDLE_FILES; [print(f) for f in _BUNDLE_FILES]\" | sort)"
```
Note the audit regex `[a-z_-]+` does NOT match digits, so `i18n.js` always shows as a "missing" false positive. Anything else flagged is a real bug.

