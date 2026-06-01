---
name: translate-retry-status-surfacing
description: Surface translate retry/started/in_progress/timed_out status; fix silent-stuck send-path AbortError handling
enabled: true
tags: translation, retry, frontend, ux, 429, rate-limit, status
created: 2026-04-30T02:14:30Z
updated: 2026-05-26T12:05:46Z
---

# Translation Retry Status Surfacing (and Silent‑Stuck Fix)

## Problem A — Async path
The async translation task `_translate_one_chunk()` retries forever on 429s
(up to 10 min deadline) and retries up to 5x on empty/truncated output. While
retrying, the frontend just showed "Translating…" with no indication.

## Problem B — Synchronous send/regenerate path (added 2026‑05‑26)
`/api/chat/send` and `/api/chat/regenerate` translate Chinese user text
synchronously before starting the agent. Two latent bugs made the chat go
silent (only "Translating…" rendered, no reply, no error):

1. `_auto_translate_user` used `with concurrent.futures.ThreadPoolExecutor() as pool:` — when the outer 20 s `future.result(timeout=…)` fired, leaving the `with` block called `pool.shutdown(wait=True)` and **blocked until the worker actually finished** (10 min internal deadline). The HTTP request never returned.
2. The frontend safety timer (`_sendTimeout`) called `AbortController.abort()` indistinguishable from a user click‑Stop, so the `catch` branch silently removed the bubble and left the chat with no reply and no error message.

## Fix (routes/translate.py)
- `_translate_one_chunk(..., overall_deadline=None)` — caller can pin a tight per‑call wall‑clock budget. Defaults to 600 s (matches old behaviour).
- Emits a `started` status event up front so the very first poll surfaces a label even when the LLM is just slow (no retries).
- New status kinds: `started`, `in_progress`, `timed_out`.

## Fix (routes/chat.py)
- `_TRANSLATE_SEND_TIMEOUT` raised 20 → 45 s (still well under frontend's 90 s).
- `_auto_translate_user` builds the `ThreadPoolExecutor` manually and calls `pool.shutdown(wait=False, cancel_futures=True)` on timeout so the HTTP request returns immediately.
- A daemon heartbeat thread publishes `kind='in_progress'` every 4 s while the worker is in flight — so even a non‑retrying slow LLM call shows progress.
- On timeout, publishes `kind='timed_out'` to `_send_translate_status[conv_id]` BEFORE `_clear_send_translate_status`, so the next frontend poll picks it up.
- Passes `overall_deadline=_TRANSLATE_SEND_TIMEOUT - 5` to `_translate_one_chunk` so the inner loop self‑terminates before the outer `future.result` deadline.

## Fix (static/js/main.js + static/js/ui.js)
Three call sites: `sendMessage`, `regenerateFromUser` (main.js), `saveEditAndResend` (ui.js). All were patched the same way:
- Safety timer 60 s → 90 s (must exceed backend's 45 s + buffer).
- A per‑call `_sendAbortReason` / `_regenAbortReason` / `_editAbortReason` flag is set to `'timeout'` from inside the safety setTimeout BEFORE calling `abort()`, so the catch handler can distinguish timer aborts from user‑stop aborts.
- The user‑stop branch is now gated on `conv._translateAborted` (which `updateSendButton`'s stop‑click handler sets *before* aborting) — without this, ALL aborts (timer OR user) fell into the silent "keeping message for editing" path.
- The non‑user branch builds a concrete error message ("Translation took too long and was cancelled. The server may be overloaded — try again, or disable auto‑translate in Settings.") and pushes a visible assistant error bubble.

## Fix (static/js/i18n.js)
Added: `translate.retry.started`, `translate.retry.in_progress`, `translate.retry.timed_out`.

## Key conventions
- Transient retry state is rerender‑only (frontend memory + DOM); never write it to DB or localStorage. Clear it on success/error.
- The status callback must never break the translation flow — wrap in try/except and log at debug level.
- When wrapping a long‑running call in a `concurrent.futures.ThreadPoolExecutor` for timeout enforcement, NEVER use `with executor:` — that calls `shutdown(wait=True)` on exit. Build it manually and call `shutdown(wait=False, cancel_futures=True)` on timeout.
- A frontend `AbortController` aborted by a safety timer must be told apart from a user‑click‑Stop. Use a side‑channel flag (set inside the setTimeout AND by the stop‑click handler), not just `signal.aborted`.

