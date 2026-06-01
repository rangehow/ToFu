---
name: subpath-deploys-markdown-img-fix
description: Markdown img/a root-anchored URLs need BASE_PATH prefix for cloud-IDE sub-path deploys
enabled: true
tags: [frontend, markdown, deploy, paper, bug]
created: 2026-05-15T04:25:11Z
updated: 2026-05-15T04:25:11Z
---

## Symptom
Images embedded in markdown (e.g. paper-report figures `![图3](/api/paper/images/.../fig_03_p3.jpg)`)
show as broken-image icon. The image file exists on disk and `curl` against
`/api/paper/images/...` returns 200, but `logs/access.log` shows zero GETs
from the browser.

## Cause
`apiUrl()` (`static/js/core.js:9`) prepends `BASE_PATH` (derived from
`window.location.pathname`) to API calls so they work behind a reverse proxy
or cloud-IDE prefix like `/proxy/15000/`. But markdown-rendered `<img>` /
`<a>` tags bypass `apiUrl()` — `marked.parse()` emits the raw root-anchored
URL (`src="/api/..."`), which loses the proxy prefix and 404s upstream
(never reaching Flask, hence no entry in access.log).

## Fix
After `marked.parse()` in `renderMarkdown` (core.js around line 3104), if
`BASE_PATH` is non-empty, rewrite root-anchored URLs in `<img>`/`<a>` tags
that point at server resources:

```js
if (BASE_PATH) {
  html = html.replace(
    /(<(?:img|a)\b[^>]*?\s(?:src|href)=["'])(\/(?:api|static|uploads)\/)/g,
    '$1' + BASE_PATH + '$2'
  );
}
```

The allow-list `(api|static|uploads)` keeps the rewrite scoped to our own
backend paths and avoids touching `//example.com`, `http://...`, fragment
links, etc.

## Out of scope (intentionally)
Don't try to fix this server-side in `_inject_images_into_report` — the
report is cached in `paper_reports` (PG) and may be rendered from many
different proxy contexts. Frontend rewrite is the right layer.

