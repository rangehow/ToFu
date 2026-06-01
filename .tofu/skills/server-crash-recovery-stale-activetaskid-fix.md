---
name: server-crash-recovery-stale-activeTaskId-fix
description: Fix for slow conversation recovery after server crash: backend startup cleanup + non-blocking frontend recovery
enabled: true
tags: [bug-fix, crash-recovery, server-restart, activeTaskId, performance, frontend, backend, startup]
created: 2026-04-17T04:23:00Z
updated: 2026-04-17T04:23:00Z
---

# Server Crash Recovery — Stale activeTaskId Fix

## Problem
When server crashes mid-generation:
1. `activeTaskId` stays set in conversation settings (never cleared since `_sync_result_to_conversation` didn't run)
2. `task_results` has entries with `status='running'` from partial checkpoints
3. On restart, frontend's `initActiveTasks` blocks on Case B recovery for ALL conversations with stale `activeTaskId` before rendering anything
4. User sees "Loading..." for a long time before their conversation appears

## Root Causes
1. **Accumulated stale data**: Every crash adds conversations with stuck `activeTaskId` — they accumulate over time
2. **Serial blocking**: `initActiveTasks` awaits Case B (poll each stale task) + Case F (fetch each offline conv) before rendering
3. **Prefetch blocked**: `loadConversationsFromServer` skipped prefetch for convs with `activeTaskId` set (`!pc.activeTaskId` guard)

## Fix (3-part)

### Backend: `recover_stale_tasks_on_startup()` in `lib/tasks_pkg/manager.py`
- Called from `server.py` after `warmup_db()`
- Marks all `task_results` with `status='running'` as `'interrupted'`
- Clears `activeTaskId` from ALL conversation settings
- Merges interrupted task content into conversation messages (with `finishReason='interrupted'`)
- Uses `CAST(settings AS TEXT) LIKE '%activeTaskId%'` for PostgreSQL jsonb compatibility

### Frontend: Non-blocking background recovery in `main.js`
- Case A (running task reconnect) stays synchronous
- `renderConversationList()` + `_ensureNewest()` called BEFORE Case B/F/E
- Case B + F + E wrapped in `_bgRecovery()` async function, fired as `.then()` (not awaited)
- Background recovery re-renders after completion

### Frontend: Prefetch allowed for activeTaskId convs in `core.js`
- Removed `!pc.activeTaskId` guard from prefetch apply block
- Since backend clears `activeTaskId` on startup, this is safe
- Eliminates an extra round-trip for the active conversation after crash recovery

## PostgreSQL Note
`settings` column is `jsonb` in PG — `LIKE` doesn't work directly. Use `CAST(settings AS TEXT) LIKE '...'`.

