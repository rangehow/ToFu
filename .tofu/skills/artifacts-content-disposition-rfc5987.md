---
name: artifacts-content-disposition-rfc5987
description: Production bug: HTTP headers are ISO-8859-1, non-ASCII titles need RFC 5987 encoding
enabled: true
tags: [artifacts, http, i18n, bug-pattern]
created: 2026-05-14T01:49:51Z
updated: 2026-05-14T01:49:51Z
---

# Bug pattern — Content-Disposition with non-ASCII filenames crashes werkzeug

## Symptom
Frontend shows "Failed to load artifact: Error: HTTP 500" on `/api/artifacts/<id>/raw`.
`logs/error.log` shows:
```
UnicodeEncodeError: 'latin-1' codec can't encode characters in position 39-40
File "...http/server.py", line 530, in send_header
```

## Cause
HTTP/1.1 headers are ISO-8859-1 by spec.  When we built
`Content-Disposition: inline; filename="<CJK>"` directly from a model-
supplied artifact title, the response writer crashed at the socket layer.
Affected both `/api/artifacts/<id>/raw` (always set) and
`/api/artifacts/<id>/export` (PDF download).

## Fix — RFC 5987 encoding (already applied in routes/artifacts.py)
Helper `_content_disposition(disposition, filename)` emits both:
- `filename="ascii_fallback"` — non-ASCII chars replaced with `_`
- `filename*=UTF-8''<percent-encoded>` — the real Unicode title

Modern browsers prefer `filename*`; legacy ones fall back.  The crash
goes away because every byte of the header value is now in `\x20-\x7e`
plus `%XX` escapes for the `filename*` part.

## Test
`tests/test_artifacts_api.py::TestRoutes::test_raw_disposition_handles_unicode_title`
- Creates an artifact with title "周报告_v1.md".
- GETs /raw, asserts 200 (not 500).
- Asserts `Content-Disposition` header is latin-1-encodable.
- Asserts `filename*=UTF-8''` extension is present.
- Asserts body still contains the CJK content.

## Generic lesson
**Any `Response.headers[...] = value` where `value` may contain
model-supplied Unicode is a latent 500.**  Audit list (whenever adding
new download / export routes):
- `Content-Disposition: filename=...`
- `Location: ...` redirects (URL encode)
- Cookie names/values
- Custom `X-...` headers built from user / model strings

Always wrap with `_content_disposition` for filenames, or
`urllib.parse.quote(...)` for arbitrary header values.

