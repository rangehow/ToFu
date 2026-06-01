---
name: send-timeout-auto-translate-blocking
description: Bug: /api/chat/send 60s timeout caused by synchronous auto-translate LLM call in HTTP handler — fixed with 20s cap
enabled: true
tags: [bug-fix, frontend, timeout, auto-translate, chat-send, rate-limit]
created: 2026-04-13T08:24:28Z
updated: 2026-04-13T08:24:28Z
---

# /api/chat/send Timeout — Auto-Translate Blocking

## Bug
Frontend shows "Request timed out — server may be overloaded" even though the request finishes normally on the server.

## Root Cause
`/api/chat/send` handler in `routes/chat.py` calls `_auto_translate_user()` **synchronously** — this makes an LLM API call that can take 100+ seconds when the translate model hits 429 rate limits or 502 errors.

Frontend has a 60s safety timeout on the AbortController (`setTimeout(() => _sendAbortCtrl.abort(), 60000)` in `sendMessage()`).

When the translate LLM retries exceed 60s, the frontend aborts and shows the error — but the server continues processing and the task actually starts successfully. The user sees an error but the response also appears, creating confusion.

## Evidence (2026-04-13)
- 15:58:11 → POST /api/chat/send starts
- 15:58:48 → Translate 429 rate-limited on gemini-3.1-flash-lite-preview  
- 15:59:11 → Frontend 60s timeout fires
- 15:59:58 → Server responds 200 after 107s (task created successfully)

## Fix
Added `_TRANSLATE_SEND_TIMEOUT = 20` — wraps the translate call in a ThreadPoolExecutor with `future.result(timeout=20)`. If translation takes >20s, gracefully skip it and send original text.

## Key Locations
- Frontend timeout: `static/js/main.js` line ~1930 — `setTimeout(() => _sendAbortCtrl.abort(), 60000)`
- Server translate: `routes/chat.py` `_auto_translate_user()`
- `/api/chat/start` also has `AbortSignal.timeout(30000)` but doesn't do translation

