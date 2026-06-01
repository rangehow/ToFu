---
name: artifacts-view-endpoint-trusted-katex
description: Artifact /view endpoint: trusted KaTeX wrapper renders math without enabling model scripts
enabled: true
tags: [artifacts, katex, csp, iframe, security]
created: 2026-05-14T03:43:21Z
updated: 2026-05-14T03:43:21Z
---

# Artifact /view endpoint — trusted KaTeX wrapper

## Why
The default no-scripts iframe path was correct for security but bad for UX:
HTML artifacts with `$..$` / `$$..$$` / KaTeX/MathJax `<script>` tags
showed raw TeX because all scripts were blocked.  Forcing the user to
click "Run scripts" to see math runs the FULL model JS — a heavy
security cost for what should be a default rendering primitive.

## Architecture
Two endpoints now coexist for HTML/SVG artifacts:

- `GET /api/artifacts/<id>/raw` (unchanged) — exact bytes, CSP `sandbox`
  (no scripts at all).  Used by the download button and by the
  user-opt-in `srcdoc + sandbox=allow-scripts` path.
- `GET /api/artifacts/<id>/view` (NEW) — wraps the artifact with our
  trusted KaTeX bundle and ships:
  ```
  Content-Security-Policy: sandbox allow-scripts;
    default-src 'self'; script-src 'self';
    style-src 'self' 'unsafe-inline';
    img-src data: https: 'self';
    font-src 'self' data:;
    media-src data: https:;
    frame-ancestors 'self'
  ```
  ONLY same-origin scripts run → our KaTeX is fine, model `<script>`
  blocks are forbidden by `script-src 'self'` (no `'unsafe-inline'`).
  Plus we strip `<script>` blocks at the source as defense in depth.

## Frontend default
`static/js/artifacts.js:_renderHtml` now sets
`iframe.src = /api/artifacts/<id>/view` with
`sandbox="allow-scripts"` (no `allow-same-origin` → opaque origin).
Math renders, TOC anchors work, model JS does not run.

The "Run document scripts" button is reworded to make clear that math
and TOC links work without it; only interactive JS docs need to opt in.

## Files
- `lib/artifacts/__init__.py` / `lib/artifacts/core.py` — unchanged.
- `routes/artifacts.py:_VIEW_CSP_HEADER`, `_build_artifact_view_html`,
  `_strip_model_scripts`, route `api_get_artifact_view`.
- `static/vendor/katex/tofu-auto-render.js` — minimal text-walker that
  finds `$..$ / $$..$$ / \(..\) / \[..\]` and calls
  `katex.renderToString` on each.  ~170 lines, no dependencies, runs
  inside the iframe.
- `static/js/artifacts.js:_renderHtml` — switched default to /view.
- Tests in `tests/test_artifacts_api.py`:
  - `test_view_route_injects_katex` — CSP shape + model script strip.
  - `test_view_rejects_markdown` — 400 on unsupported format.
  - `test_view_404_on_missing` — 404 on missing id.
  - `test_view_full_doc_keeps_structure` — full `<!doctype html>`
    documents get KaTeX injected before `</head>` without losing
    original head/body.

## Threat model
1. Model HTML may contain `<script>`, `on*=`, `javascript:` href.
2. Iframe sandbox `allow-scripts` (NO `allow-same-origin`) → scripts
   that DO run are in opaque origin, can't reach parent
   localStorage/cookies/DOM.
3. Response CSP `script-src 'self'` (no `'unsafe-inline'`) → forbids
   inline scripts AND remote scripts.  Both layers must fail for model
   JS to execute.
4. Defense in depth: `_strip_model_scripts` removes `<script>` tags
   before injecting the model body.
5. KaTeX `trust: false` in `tofu-auto-render.js` blocks `\href` /
   `\url` from emitting `javascript:` URIs.

## Don't reuse for arbitrary user-uploaded HTML
This endpoint is for ARTIFACT content (model output we already store).
For a future "user uploads HTML" feature, audit `_strip_model_scripts`
+ CSP combo against the OWASP list — same-origin scripts blocked is
sufficient HERE because we control all `'self'` JS, but a stricter
fragment renderer that NEVER ships `'self'` scripts may be safer for
that case.

## Out of scope
- Mermaid / Plotly / D3 — these require model `<script>` to run and so
  still need the user-opt-in `Run scripts` path.  Acceptable.

