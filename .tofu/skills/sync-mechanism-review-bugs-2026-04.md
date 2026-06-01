---
name: sync-mechanism-review-bugs-2026-04
description: Frontend↔backend sync review: DB poll missing metadata fields, _continue* cleanup asymmetry, partial sync no CAS
enabled: true
tags: [sync, sse, polling, bug-fix, architecture, frontend, backend]
created: 2026-04-14T14:35:15Z
updated: 2026-04-14T14:35:15Z
---

# Synchronization Mechanism Review (2026-04)

## Bug 1: DB poll path missing metadata fields (FIXED)
`routes/chat.py` — the DB poll path only iterated over `('finishReason', 'usage', 'preset', 'toolSummary')` while the in-memory path included `model`, `thinkingDepth`, `apiRounds`, `modifiedFiles`, `modifiedFileList`. The SSE stream DB path had them. Fixed by adding the missing keys.

## Bug 2: _continue* cleanup asymmetry (FIXED)
SSE done handler was missing `delete _continueApiRounds` and `delete _continueUsage`.
Poll fallback was missing `delete _continueContentPrefix`.
Both now clean up all 6 `_continue*` markers identically.

## Known Issue: _sync_partial_to_conversation lacks CAS guard
`lib/tasks_pkg/manager.py` — `_sync_partial_to_conversation` does unconditional UPDATE without `WHERE updated_at=?` optimistic lock. Could overwrite fresher data during concurrent streaming. Mitigated by content-length guard but still overwrites `updated_at`/`msg_count`/`search_text` unconditionally.

## Dead Code (harmless, low priority)
- `searchResults`/`searchQuery` backward compat in `ui.js:4667` and `core.js:1100` — all messages long since migrated to `toolRounds`
- `_mergeFromStorage()` and `_writeToLocalStorage()` no-op functions in `core.js`

## Architecture Summary
- **SSE primary → poll fallback**: `_trySSE()` attempts streaming; on premature close/timeout → `_pollFallback()` polls `/api/chat/poll/<taskId>`
- **3 response paths**: in-memory task, DB task_results, 404
- **Crash recovery**: `checkpoint_task_partial()` every 5s during streaming + after each tool round
- **Optimistic locking**: `_sync_result_to_conversation` uses CAS guard (`WHERE updated_at=?`), but `_sync_partial_to_conversation` does not
- **Frontend guards**: content-length regression detection, cross-talk detection, dangling ref detection

