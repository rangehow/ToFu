---
name: project-undo-per-round-and-vscode-timeline
description: Bug fix: /api/project/undo route was missing (undo had no effect), undo now per-round via task_id tagging, Undo All as separate /api/project/undo_all; VS Code timeline fix via os.fsync+os.utime mtime bump after external writes
enabled: true
tags: [python, javascript, undo, project-tools, vscode, bug-fix, task-id, per-round]
created: 2026-03-30T09:20:19Z
updated: 2026-03-30T09:20:19Z
---

# Project Undo Per-Round + VS Code Timeline Fix

## Bug #1: Undo Had No Effect
The frontend called `POST /api/project/undo` but no backend route existed in `routes/project.py`.
The functions `undo_conv_modifications` and `undo_all_modifications` existed in `lib/project_mod/modifications.py`
but were never exposed via Flask routes.

**Fix**: Added `/api/project/undo` (per-round via taskId, fallback to convId), `/api/project/undo_all`, and `/api/project/rescan` routes.

## Bug #2: Undo Reverted ALL Conversation Changes
Undo was per-conversation (convId), meaning clicking "Undo 3" on one round's changes
would revert ALL modifications from the entire conversation.

**Fix**: Added `task_id` tagging to `_record_modification()`. Each modification now records both
`convId` and `taskId`. The new `undo_task_modifications(base_path, task_id)` only reverts that round.

Flow: `executor.py` passes `task['id']` → `execute_tool(task_id=)` → `tool_write_file(task_id=)` → `_record_modification(task_id=)`.

The `done` event now includes `taskId`, stored as `msg._taskId` on the assistant message in the frontend.

## Bug #3: VS Code Timeline Not Updated
VS Code's "Local History" only creates snapshots when the editor itself saves.
External writes (from our tools) are detected via inotify but don't trigger history entries.

**Mitigation**: After every file write, call `os.fsync(fd)` + `os.utime(filepath, (atime, mtime+1µs))`.
This guarantees VS Code's file watcher fires a change event, which:
- Triggers "file changed on disk — reload?" if the file is open
- Updates the SCM diff view
- Note: still won't create local-history entries (VS Code limitation) but at least the user sees the change

Helper function `_touch_for_vscode(filepath)` added to `lib/project_mod/tools.py`.
Same logic duplicated as `_nudge_vscode(filepath)` in `modifications.py` for undo operations.

## UI Changes
- Undo button moved from message action buttons into the `file-changes-bar` (per-message)
- "Undo All" button added as separate interaction point in file-changes-bar
- Both buttons styled with JetBrains Mono, subtle borders, hover glow effects
- CSS classes: `.fc-undo-btn` (blue), `.fc-undo-all-btn` (red)
- Buttons only appear on finalized (non-streaming) messages

