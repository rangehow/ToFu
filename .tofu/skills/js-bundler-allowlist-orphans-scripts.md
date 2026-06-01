---
name: js-bundler-allowlist-orphans-scripts
description: JS bundler regex strips all static/js/*.js script tags but only bundles files in _BUNDLE_FILES — files missing from the list silently never load
enabled: true
tags: [frontend, bundler, trap, js]
created: 2026-05-11T04:23:36Z
updated: 2026-05-11T04:23:36Z
---

# JS bundler allowlist trap — missing files load silently as no-op

## Symptom
A feature whose JS file exists in `static/js/` and is referenced via
`<script defer src="static/js/foo.js?v=...">` in `index.html` simply never
runs in production. Click handlers wired by IIFEs in that file don't fire,
`typeof window.fooFunction` is `"undefined"`, but there's no 404 in the
Network tab and no error in the console — the script tag is just *gone*
from the served HTML.

## Root cause
`routes/common.py` swaps individual app script tags for a single bundle tag
on every `GET /`. The regex `_APP_SCRIPTS_RE` (routes/common.py:371-375)
matches **every** `<script defer src="static/js/...">` that isn't `bundle-…`
and removes it (`re.sub` at line 405).

The replacement bundle is built from a **hard-coded allowlist**
`_BUNDLE_FILES` in `lib/js_bundler.py` (~line 22). If a file isn't in
`_BUNDLE_FILES`, the regex still strips its tag from index.html, but the
bundler never adds it back → it's silently dropped from the served page.

## Fix when adding a new top-level JS module
1. Add the filename to `_BUNDLE_FILES` in `lib/js_bundler.py` in the
   correct dependency order (i18n.js MUST stay first; main.js MUST stay last).
2. Still add the `<script defer src="static/js/foo.js?v=...">` tag to
   `index.html` for dev-mode fallback (when bundling fails the original
   tags are served — see `routes/common.py:380-384`).
3. Rebuild the bundle:
   `python3 -c "from lib.js_bundler import build_bundle; print(build_bundle())"`
4. Hard-refresh the browser (the bundle filename changes via content hash,
   so a soft reload would re-use the cached old bundle).

## Real instance
Discovered 2026-05-11: OPTIMIZER badge in index.html had `cursor:pointer`
and a `_bindOptimizerBadge` IIFE in `static/js/optimizer.js`, but clicking
did nothing because `optimizer.js` was missing from `_BUNDLE_FILES`.
Fix: added `'optimizer.js'` between `'scheduler.js'` and `'timer.js'`.

## How to audit
```bash
# Files referenced in index.html but not in the bundle list:
diff <(grep -oE 'static/js/[a-z_-]+\.js' index.html | sed 's|static/js/||' | sort -u) \
     <(python3 -c "from lib.js_bundler import _BUNDLE_FILES; [print(f) for f in _BUNDLE_FILES]" | sort)
```

