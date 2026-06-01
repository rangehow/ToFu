---
name: artifacts-iframe-srcdoc-anchor-trap
description: iframe srcdoc base-URL trap: TOC/relative links escape to parent URL — fix is iframe src=/api/artifacts/&lt;id&gt;/raw
enabled: true
tags: [artifacts, iframe, security, bug-pattern]
created: 2026-05-14T03:11:14Z
updated: 2026-05-14T03:11:14Z
---

# Bug — `<iframe srcdoc>` and the parent-URL anchor trap

## Symptom
User clicks a TOC entry inside a rendered HTML artifact (e.g. `<a href="#mdp">`) and the **iframe navigates to the host application's URL** — in our deployment, that's the VS Code / code-server proxy entry, which then prompts for a login.

## Cause
With `<iframe srcdoc="...">` the iframe's *document URL* is `about:srcdoc`, but its **base URL defaults to the parent page's URL**.  So:
- `<a href="#mdp">` → resolves against `https://…/proxy/15000/` → loads code-server login page.
- `<a href="relative.html">` → same trap, loads parent-relative path.

This is per-spec; not a Chrome bug.  `<base href="...">` inside the model's HTML can fix it but we don't get to inject one without re-parsing the document.

## Fix (applied in static/js/artifacts.js `_renderHtml`)
For the default no-scripts path, render via **`iframe.src = /api/artifacts/<id>/raw`** instead of `iframe.srcdoc = content`.

- Iframe document URL becomes `/api/artifacts/<id>/raw`.
- `#anchor` → same-document fragment (scroll only).
- Relative URLs stay under `/api/artifacts/...` and 404 instead of escaping to the host page.
- Security unchanged: `/raw` already returns
  `Content-Security-Policy: sandbox; default-src 'none'; …` so even the URL-load case can't run scripts; the iframe's own `sandbox=""` attribute is the second layer.

## When to keep `srcdoc`
The `allow-scripts` opt-in branch MUST keep using `srcdoc` because the response CSP `sandbox` directive would otherwise still block scripts even if the iframe attribute allows them.  In `srcdoc + sandbox="allow-scripts"` (without `allow-same-origin`) the iframe is a unique opaque origin → spec-safe single-flag combo that can run JS but cannot reach `parent.localStorage` / cookies.

The anchor trap re-applies for that path, but the user explicitly opted in to "Run scripts" for that single render — acceptable.

## General lesson
Whenever rendering arbitrary author HTML in an iframe, **prefer `src=` over `srcdoc=` so the document has a sensible base URL of its own.**  `srcdoc` is for tiny snippets where you control all links; for full documents in any production embedding, `src` to a real endpoint with proper headers is the safer default.

