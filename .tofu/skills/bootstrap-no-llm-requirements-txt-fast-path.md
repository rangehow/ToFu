---
name: bootstrap-no-llm-requirements-txt-fast-path
description: Bug fix: bootstrap.py LLM-only dependency repair fails on exported projects with no API keys — added requirements.txt fast path, _is_import_or_package_error usage, and SSE done-event _finished guard to prevent infinite page refresh loop
enabled: true
tags: [python, bootstrap, requirements.txt, dependency-repair, export, refresh-loop, bug-fix, sse]
created: 2026-03-31T22:05:52Z
updated: 2026-03-31T22:05:53Z
---

# Bootstrap: requirements.txt Fast Path & Refresh Loop Fix

## Problem
When a freshly-exported chatui project is started (`python server.py` or `python bootstrap.py`), three bugs combine to make it unusable:

### Bug 1: No requirements.txt fast path
`bootstrap.py` relies 100% on the LLM API to diagnose `ModuleNotFoundError`, but exported projects have no API keys configured. `_call_llm()` loops over `cfg['api_keys']` (empty list → 0 iterations), `last_err` stays `None`, immediately returns `{unresolvable: True}`.

### Bug 2: `_is_import_or_package_error()` defined but never called
The heuristic function exists but is never used anywhere in `main()`.

### Bug 3: Browser infinite refresh loop
The JavaScript `es.onerror` handler polls `/` every 2s and reloads on HTTP 200. But after failure, the status server stays alive (`_keep_alive_until_interrupt`). When `onerror` fires (SSE handler closes after sending `done`), the poll immediately succeeds → `window.location.reload()` → fresh page creates new EventSource → history replayed including `done` → close → `onerror` → poll → reload → ∞

## Fix

### 1. Add `_try_requirements_txt()` function
Before attempting LLM diagnosis, check if `requirements.txt` exists. If the error is an import/package error, run `pip install -r requirements.txt` first. This works without any API keys.

### 2. Use `_is_import_or_package_error()` heuristic  
Call it to decide whether to attempt requirements.txt installation.

### 3. Early exit when no LLM keys
After requirements.txt attempt, if still failing and no LLM keys, show clear instructions:
- `pip install -r requirements.txt`
- Configure `LLM_API_KEY` and `LLM_BASE_URL` in `.env`
- Re-run `python server.py`

### 4. JavaScript `_finished` flag
Add `let _finished = false` state variable. Set to `true` in `done` handler. Check in `es.onerror` — if `_finished`, return immediately (don't start poll/reload cycle).

### 5. `d.hint` support in done handler
The `done` event payload can now include a `hint` field with user-actionable instructions, displayed instead of the generic "check log output" message.

## New `main()` flow
```
1. Try server.py
2. If crashed AND is import error AND requirements.txt exists:
   a. pip install -r requirements.txt
   b. Try server.py again
   c. If still failing with import error → fall through to LLM
   d. If failing with non-import error AND no LLM → show hint, stop
3. If no LLM API key → show manual instructions, stop
4. LLM-guided repair loop (unchanged)
```

