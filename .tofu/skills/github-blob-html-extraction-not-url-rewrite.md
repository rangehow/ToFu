---
name: github-blob-html-extraction-not-url-rewrite
description: Fix for GitHub code file fetching: extract rawLines from embedded JSON in HTML (react-app.embeddedData) instead of URL rewriting to /raw/ endpoint which may be blocked by proxies; GitLab/Bitbucket still need URL rewriting since they use full JS rendering
enabled: true
tags: [python, github, fetch, html-extraction, code-hosting, blob, bug-fix, proxy]
created: 2026-03-30T23:26:40Z
updated: 2026-03-30T23:26:40Z
---

# GitHub Blob HTML Extraction (Not URL Rewriting)

## Problem
When fetching GitHub `/blob/` URLs (e.g. `github.com/owner/repo/blob/main/file.py`),
a plain HTTP GET returns a 914KB HTML page with navigation chrome and no visible code.
Trafilatura extracts garbage ("feedback", "notifications", line numbers).

## Root Cause
GitHub renders code via React (JavaScript). The HTML is a shell — but it **embeds the
full source code** as a JSON array `rawLines` inside:
```html
<script type="application/json" data-target="react-app.embeddedData">
  { ... "rawLines": ["line1", "line2", ...], "path": "file.py", "language": "Python" ... }
</script>
```

## Fix: Extract from HTML, Don't Rewrite URL
In `lib/fetch/html_extract.py`, added Phase 0 (`_try_extract_code_blob`) before trafilatura:

1. Check if URL matches `github.com/.../blob/...` pattern
2. Regex-extract the `react-app.embeddedData` JSON script block
3. Parse JSON, recursively find `rawLines` array
4. Join into source code string with metadata header

## Why Not URL Rewriting?
- `raw.githubusercontent.com` is **blocked** in corporate/proxy networks
- `github.com/.../raw/...` works but adds an unnecessary redirect
- HTML extraction is more robust — gets the same data the browser renders
- GitLab/Bitbucket still need URL rewriting (`/-/raw/`, `/raw/`) since they
  use full JS rendering (no embedded code in HTML)

## Key Details
- `_GITHUB_BLOB_URL_RE`: matches `/owner/repo/blob/ref/path.ext` (requires file extension)
- `_GITHUB_EMBEDDED_RE`: extracts the JSON from the `<script>` tag
- `_find_nested_key()`: recursive key search since `rawLines` is deeply nested
- Also extracts `path` and `language` metadata for header
- Non-blob GitHub URLs (repos, issues, PRs) fall through to normal extraction

