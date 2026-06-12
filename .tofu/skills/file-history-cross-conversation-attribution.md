---
name: file-history-cross-conversation-attribution
description: fh side-channel: filter by last_writer_task_id, hold project lock across snapshot, AND drop unattributed drift on read-only rounds (Fix 4)
enabled: true
tags: [file-history, concurrency, orchestrator]
created: 2026-05-13T04:52:04Z
updated: 2026-06-10T09:35:00Z
---

# File-history side-channel cross-conversation misattribution

## Symptom
A round that didn't actually edit any project file shows a long
"N files changed" bar in the UI when ANOTHER conversation, pointing
at the same project root, committed an editing round at roughly the
same time. Example conv: `mor8epb7c82olc`. Also presents as: the bar
appears on a no-op round and a hard refresh clears it only
*sometimes* (the bad list was in the live SSE event but the persisted
DB copy raced differently).

## Root cause
`lib/tasks_pkg/orchestrator.py:_run_commit_round_async` runs three
fh calls back-to-back on a daemon thread:
```python
prev_snap = fh.get_last_snapshot_id(project_path)
_snap_id  = fh.make_snapshot(...)
fh_changes = fh.diff_name_status(project_path, prev_snap, _snap_id)
```
`make_snapshot` walks the project-wide `tracked.json` index, so a
concurrent conv's writer that updates `tracked.json` between our
prev_snap capture and our make_snapshot leaks its edits into our diff.
They flow into `task['modifiedFileList']` and the persisted assistant
message via `_patch_assistant_message_with_git`.

## Fix (defense in depth)

### Fix 3 — atomic commit region
Wrap prev_snap → make_snapshot → diff → load_tracked in
`lib.file_history.store._project_lock(project_path)` (RLock; inner
`@with_project_lock` calls re-acquire as no-ops).

### Fix 2 — per-task attribution on tracked entries
Every `stage_backup`/`_stage_explicit` write stamps
`last_writer_task_id` on the `tracked.json` entry (plumbed through
store.py / api.py:track_edit / make_snapshot /
modifications.py:_record_modification). In the orchestrator, after
`diff_name_status`, drop any path whose `last_writer_task_id` is set
and is NOT the current task's id.

### Fix 4 — drop unattributed drift on read-only rounds (2026-06)
Fix 2 kept paths with EMPTY `last_writer_task_id` for back-compat —
but that escape hatch is the *residual leak*: external/IDE drift and
un-stamped concurrent writes have `last_writer_task_id == ''` and get
attributed to whatever round snapshots next. Gate it: compute
`_round_can_write` = did this round run any tool NOT in a read-only
WHITELIST (`list_dir, read_files, grep_search, find_files,
web_search, fetch_url`) — probe `task['toolRounds']` by `toolName`.
WHITELIST (not blacklist) so unknown MCP tools count as write-capable
and never over-suppress. Then in the filter:
  - writer set & != own  → drop (Fix 2)
  - writer empty & NOT round_can_write → drop (Fix 4, drift)
  - else keep.
This preserves the legitimate side-channel for run_command /
code_exec / MCP edits (which DO leave empty last_writer_task_id but
run on a write-capable round). Probe fails OPEN (`_round_can_write=True`)
on exception so we never over-suppress.

## Testing
- `python debug/test_file_history.py` — 34 pass.
- `python debug/test_concurrent_fh_attribution.py` — Task-A/B + C/D
  concurrency PLUS new Task-E (read-only round drops unattributed
  drift) / Task-F (write-capable round still surfaces it). The test's
  `_commit_round_simulating_orchestrator` mirror must be kept in sync
  with the orchestrator filter — it now takes `tool_rounds=` and
  applies the same whitelist gate. NOTE: each drift edit appears in
  only ONE snapshot diff, so use a distinct file per round in tests.

## Invariants
- NEVER drop entries with `last_writer_task_id == ''` on a
  WRITE-CAPABLE round (legacy entries + run_command/code_exec/MCP).
- `_project_lock` is an RLock — re-entrancy required.
- `last_writer_task_id` lives on the tracked entry (per-file), set at
  stage time; misattribution happens at diff time which keys on the
  tracked index.
- Read-only whitelist is the safe side: unknown tool name ⇒ treated
  as write-capable.
