---
name: vscode-proxy-apiUrl-required-for-all-fetch
description: Bug pattern: settings.js OAuth fetch used raw '/api/...' paths instead of apiUrl(), causing 404 through VSCode proxy at /proxy/PORT/
enabled: true
tags: [javascript, bug-pattern, vscode-proxy, oauth]
created: 2026-04-05T05:51:30Z
updated: 2026-04-05T05:51:30Z
---

# VSCode Proxy Path Prefix — All fetch() Must Use apiUrl()

## The Bug
OAuth fetch calls in `settings.js` used raw absolute paths like `fetch('/api/oauth/login')`.
When accessed through VSCode port-forwarding proxy at `https://host/proxy/15000/`, the 
browser resolves `/api/oauth/login` to `https://host/api/oauth/login` (missing proxy prefix), 
which the proxy doesn't know about → returns **404: Not found.**

## Root Cause
- `core.js` defines `BASE_PATH` from `window.location.pathname` and `apiUrl(path)` = `BASE_PATH + path`
- Most fetch calls in the codebase correctly use `apiUrl('/api/...')`
- OAuth calls were added without using `apiUrl()` wrapper

## Symptom
- Error: `OAuth 登录请求失败: HTTP 404: Not found.` (note: period at end)
- Flask's 404 says `"Not Found: /path"` (no period) — the 404 with period is from VSCode proxy
- Backend logs show no OAuth request was received at all

## Fix
Wrap ALL `fetch('/api/...')` calls with `apiUrl()`: `fetch(apiUrl('/api/...'))`

## Rule
**Every new `fetch()` call to a backend API MUST use `apiUrl('/api/...')`**, never a raw path.
This applies to all JS files. The only exception is relative paths like `'api/health'` (no leading slash)
which are resolved relative to the current page URL and work correctly.

## Diagnosis Checklist
When a fetch returns 404 that the backend doesn't log:
1. Check if the fetch uses `apiUrl()` wrapper
2. Check if the 404 body matches Flask format (`"Not Found: /path"`) or proxy format (`"Not found."`)
3. Test with `curl http://127.0.0.1:PORT/api/path` to confirm backend is reachable directly

