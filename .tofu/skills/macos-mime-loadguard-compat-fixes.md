---
name: macos-mime-loadguard-compat-fixes
description: macOS compat: MIME type init order, regex lookbehind avoidance, LoadGuard slow-network retry, JS bundler (16→1 request)
enabled: true
tags: [macos, compat, mime, safari, loadguard, debugging]
created: 2026-04-03T05:23:34Z
updated: 2026-04-03T07:35:39Z
---

# macOS Compatibility Fixes for Script Loading

## Problem
Mac users see "App initialization failed" red banner even when scripts are just loading slowly.
On local Mac installs (no VS Code forwarding), HTTP/1.1 connection limit (6 per host) causes
16 JS files to download in 3-4 serial waves, taking 10-30+ seconds.

## Root Causes & Fixes

### 1. JS Bundler (lib/js_bundler.py) — MAIN FIX
At server startup, concatenates all 16 app JS files into a single `static/js/bundle-{hash}.js`.
The `routes/common.py` index route reads `index.html` and replaces the 16 individual `<script>` tags
with 1 bundle tag. Result: 4 total requests (3 vendor + 1 bundle) instead of 19.

- Bundle rebuilt automatically when source files change (mtime check)
- Content hash in filename for cache busting (immutable, 1-year cache)
- Falls back to individual files if bundling fails
- Bundle excluded from git (.gitignore) and export (export.py)
- Cached in memory (`_bundled_index_cache`) to avoid re-reading + regex on every page load

### 2. MIME Type Registration Order (server.py)
`mimetypes.add_type()` before `mimetypes.init()` sets `inited=True`, preventing lazy init.
On macOS (no `/etc/mime.types`), this means other extensions (`.woff2`, `.ttf`) return `None`.
**Fix**: Call `mimetypes.init()` BEFORE `add_type()`.

Additionally, use `@app.after_request` to force `Content-Type` on `.js` and `.css` files
as a belt-and-suspenders approach.

### 3. Regex Lookbehind (core.js)
Safari < 16.4 doesn't support regex lookbehind (`(?<!...)`).
The inline math regex `(?<!\$)\$(?!\$)...` causes SyntaxError at parse time, killing the entire script.
**Fix**: Remove lookbehind — safe because `$$` blocks are extracted first.

### 4. LoadGuard False Positive
The 8-second timeout couldn't distinguish "scripts still downloading" from "scripts loaded but init crashed".
**Fix**: Track `onload` count vs total scripts; retry with 5s intervals; capture runtime errors via
`window.addEventListener('error')` for better diagnostics.

## Key Files
- `lib/js_bundler.py` — Bundle builder (build_bundle, get_bundle_filename, get_bundle_script_tag)
- `routes/common.py` — index_page route with bundle injection + caching
- `server.py` — Bundle build at startup, cache headers for bundle-*.js
- `.gitignore` / `export.py` — Bundle file exclusions

