---
name: github-raw-url-corporate-proxy-fix
description: SUPERSEDED by github-blob-html-extraction-not-url-rewrite: GitHub /raw/ URLs stay on github.com domain (not raw.githubusercontent.com which is blocked by corporate proxies), but HTML extraction approach is now preferred — extracts rawLines from embedded JSON in react-app.embeddedData script block"
enabled: true
tags: [github, proxy, corporate-network, raw-url, fetch, bug-fix]
created: 2026-03-30T23:18:39Z
updated: 2026-03-30T23:26:49Z
---

# GitHub Raw URL — Corporate Proxy Fix

## Problem
`raw.githubusercontent.com` is blocked/unreachable in many corporate proxy environments.
The initial fix rewrote `github.com/.../blob/ref/path` → `raw.githubusercontent.com/.../ref/path`,
but this fails silently (connection timeout or refused).

## Solution
Rewrite to GitHub's own raw endpoint instead:
```
github.com/owner/repo/blob/ref/path → github.com/owner/repo/raw/ref/path
```

This stays on `github.com` (which IS accessible) and returns `text/plain` content directly.

Note: GitHub also has a longer form `github.com/.../raw/refs/heads/ref/path` which works too,
but the short form `/raw/ref/path` is equivalent and GitHub redirects internally.

## Code location
`lib/fetch/utils.py` → `_normalize_code_hosting_url()` → GitHub blob branch
