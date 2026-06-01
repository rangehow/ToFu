---
name: chatinner-targeted-endpoints-2026-04
description: Targeted per-action endpoints for chatInner controls: PATCH message, DELETE branch, POST /api/chat/continue — replace full-conv PUT
enabled: true
tags: [architecture, frontend, backend, endpoints, patch, continue, branch]
created: 2026-04-21T11:12:14Z
updated: 2026-04-21T11:12:14Z
---

# chatInner — Targeted per-action endpoints (2026-04)

## Goal
Every user-facing action button inside `#chatInner` now hits a dedicated,
scoped backend route — no more full-conversation PUT for edit-only,
translation-visibility toggle, branch-delete, or continue.

## New routes

| Action | Endpoint | File |
|---|---|---|
| Edit-only / translate-toggle / translate-complete metadata | `PATCH /api/conversations/<id>/messages/<idx>` | `routes/conversations.py` |
| Branch delete | `DELETE /api/conversations/<id>/messages/<idx>/branches/<bidx>` | `routes/conversations.py` |
| Continue from last-complete-tool-batch checkpoint | `POST /api/chat/continue` | `routes/chat.py` |

## PATCH message — whitelist
`_PATCH_MSG_WHITELIST` in `routes/conversations.py` restricts mutable keys to:
`content, originalContent, images, pdfTexts, replyQuotes,
_showingTranslation, translatedContent, _translateModel, _translateDone,
_translateTaskId, _translateField, _translateError, _translatedCache,
_originalContent, timestamp`. Any key outside → HTTP 400. Literal `null`
value deletes that key from the message.

## Continue server flow
`/api/chat/continue`:
1. Load conv from DB, take last assistant msg.
2. `_scan_continue_checkpoint()` — port of `continueAssistant()` JS scan.
   Returns `None` if no recoverable checkpoint → 200 with
   `{fallback: "regenerate", reason: ...}` (frontend pops and resends).
3. Roll back `toolRounds` / `content` / `thinking` on the message.
4. **Persist rolled-back state to DB BEFORE starting the task** (mirrors
   `chat_regenerate` order — prevents `_sync_result_to_conversation`
   overwriting the rollback).
5. Build cfg_payload with `toolHistory`, `contentPrefix`,
   `checkpointToolRounds`, `checkpointUsage`, `checkpointApiRounds`,
   `checkpointModifiedFiles`, `checkpointModifiedFileList`, `excludeLast=True`.
6. `_start_task_for_conv(conv_id, cfg_payload)`; return
   `{taskId, convId, checkpoint:{...summary...}}`.

## Frontend delta (static/js)
- `ui.js`: new helper `_patchMessageOnServer(convId, idx, patch, opts)`.
  - `saveEditOnly` and `translateMessage` toggle/complete paths call it
    instead of `syncConversationToServerDebounced`.
- `main.js::continueAssistant`:
  - Still scans locally for optimistic UI rollback + sets `_continueXxx`
    fields consumed by SSE merge handlers.
  - POSTs `{convId, config:_buildConvConfig(conv)}` — **no
    `toolHistory` / `contentPrefix` / `checkpointXxx` on the wire**.
  - Honors `{fallback: "regenerate"}` by undoing local rollback and
    calling `startAssistantResponse`.
- `branch.js::branchCloseOrDelete`:
  - Optimistic splice + DELETE. On error, restore `msg.branches = _prevBranches`
    and reload conv from server.

## Backward compat
`/api/chat/start` still reads `checkpointToolRounds`/`contentPrefix` for
SWE-bench harness and other external callers. Only the frontend
`continueAssistant` stopped emitting them.

## `_start_task_for_conv` must respect `excludeLast`
`routes/chat.py::_start_task_for_conv` now reads
`config.get('excludeLast', False)` and passes it to
`build_api_messages_from_db(..., exclude_last=_exclude_last)`.

## Audit events
- `audit_log('msg_patch', conv_id=..., idx=..., keys=...)`
- `audit_log('branch_delete', conv_id=..., msg_idx=..., branch_idx=..., remaining=...)`
- `audit_log('continue_checkpoint', conv_id=..., kept=..., discarded=...,
  preservedContentLen=..., discardedContentLen=...,
  preservedThinking=..., discardedThinking=...)`

## Tests
`debug/test_chatinner_endpoints.py` — 5 cases:
- patch_message (edit, null-delete, non-whitelist reject, out-of-range,
  unknown conv, empty patch)
- delete_branch (middle, out-of-range, delete-all→branches key removed)
- continue fallback (no tool rounds)
- continue fallback (empty assistant)
- continue rollback scan (direct unit test of checkpoint logic)

Shim note: the SWE-bench workdir ships an editable Flask dev install whose
bundled werkzeug lacks `__version__`; the test monkey-patches
`werkzeug.__version__ = '0.0.0'` so `app.test_client()` doesn't crash.

## CLAUDE.md compliance
- §2 logging: every except logs; `audit_log` for significant state changes;
  %-style formatting; truncated previews (`%.50s`).
- §10.3: no DB schema changes — messages column structure unchanged.
- §11.3: no new secrets/endpoints/internal domains → `export.py` untouched.

