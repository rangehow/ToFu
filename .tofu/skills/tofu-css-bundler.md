---
name: tofu-css-bundler
description: CSS auto-cache-busting via lib/css_bundler.py — hash injected into link tag at request time
enabled: true
tags: [frontend, cache, bundler]
created: 2026-05-12T02:39:35Z
updated: 2026-05-12T02:39:35Z
---

# CSS auto-cache-busting (2026-05-12)

## Problem solved
`index.html:19` used to have a hardcoded `static/styles.css?v=20260511a`
query string that had to be bumped by hand on every CSS edit. Forgetting
to bump it left browsers serving stale cached CSS — even Ctrl+Shift+R
honors the URL-keyed cache. Hard-refresh + manual bump was the only
working invalidation path.

## Architecture

`lib/css_bundler.py` — content-hashes `static/styles.css`. Mirror of
`lib/js_bundler.py` but lighter: there's only one app stylesheet so we
don't concatenate or copy, we just compute SHA-256[:8] of the file and
exposed via `get_styles_link_tag()`.

`routes/common.py` — `index_page()` now does TWO rewrites on the served
HTML, both cached together:
1. `_APP_SCRIPTS_RE.sub(bundle_tag, ...)` — existing JS bundler.
2. `_APP_STYLES_RE.sub(styles_tag, ...)` — new CSS hash injection.
The `_bundled_index_cache` is keyed by `(bundle_tag, styles_tag,
html_mtime)` so any of those changing rebuilds the served HTML.

`index.html:19` is now `<link rel="stylesheet" href="static/styles.css">`
(no `?v=…`). The server adds the query string at request time. Match
regex is permissive: `static/styles\.css(?:\?[^"]*)?` so it accepts
either form.

## Filesystem-resolution gotcha (FUSE / NFS)
The mtime check has 1-second resolution on FUSE mounts. Two CSS edits
within the same second would both produce the same mtime, so an
mtime-only check would miss the second edit. **Fix in
`_state['mtime', 'size']`**: cache (mtime, size) and recompute the hash
whenever EITHER changes. A change that keeps both mtime AND size
identical would still be missed, but that's vanishingly rare.

## Future maintenance
- New stylesheets: extend `_APP_STYLES_RE` to match the new path AND
  add a hash function in `lib/css_bundler.py` for it.
- Vendor CSS (`static/vendor/...`) is intentionally NOT covered —
  versioned by vendor URL, rarely changes, no need.
- The `<link>` tag regex requires `static/styles.css` literal —
  changing the filename means updating the regex.

## Audit
```bash
# Confirm the link tag in served HTML carries a fresh hash:
curl -s http://localhost:5000/ | grep 'styles.css'
# Should show: ...href="static/styles.css?v=<8-char-hex>"
```

