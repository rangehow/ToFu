---
name: file-history-cross-conversation-attribution
description: fh side-channel must filter by last_writer_task_id and hold project lock across prev_snap+make_snapshot+diff
enabled: true
tags: [file-history, concurrency, orchestrator]
created: 2026-05-13T04:52:04Z
updated: 2026-05-13T04:52:04Z
---

# File-history side-channel cross-conversation misattribution

## Symptom
A round that didn't actually edit any project file shows a long
"N files changed" bar in the UI when ANOTHER conversation, pointing
at the same project root, committed an editing round at roughly the
same time.  Example conv: `mor8epb7c82olc`.

## Root cause
`lib/tasks_pkg/orchestrator.py:_run_commit_round_async` runs three
file-history calls back-to-back on a daemon thread:

```python
prev_snap = fh.get_last_snapshot_id(project_path)
_snap_id  = fh.make_snapshot(...)
fh_changes = fh.diff_name_status(project_path, prev_snap, _snap_id)
```

Each call grabs the per-project `_project_lock` via
`@with_project_lock`, but the sequence as a whole is NOT atomic.
`fh.make_snapshot` walks the project-wide `tracked.json` index — so
if conv-B's writer thread updates `tracked.json` between conv-A's
`prev_snap` capture and conv-A's `make_snapshot`, conv-A's snapshot
ends up containing conv-B's edits, and `diff_name_status` then
reports those edits as if conv-A made them.  These flow into
`task['modifiedFileList']` and into the persisted assistant message
via `_patch_assistant_message_with_git`.

## Fix (defense in depth)

### Fix 3 — atomic commit region
Wrap the prev_snap → make_snapshot → diff → load_tracked sequence in
`lib.file_history.store._project_lock(project_path)` so concurrent
commit threads can't interleave in that critical region.  The
inner `@with_project_lock` calls re-acquire the same RLock and
become no-ops while we're holding it.

### Fix 2 — per-task attribution on tracked entries
Every `stage_backup` / `_stage_explicit` write now stamps
`last_writer_task_id` on the `tracked.json` entry.  Plumbed through:

- `lib/file_history/store.py:stage_backup(... task_id=...)`
- `lib/file_history/store.py:_stage_explicit(... task_id=...)`
- `lib/file_history/api.py:track_edit(... task_id=...)` — passes
  `task_id` (default: `message_id`) down to the store.
- `lib/file_history/api.py:make_snapshot` — passes its `task_id`
  through when re-staging declared `rel_paths`.
- `lib/project_mod/modifications.py:_record_modification` — passes
  `task_id=task_id` to `fh.track_edit(...)`.

In the orchestrator, after `diff_name_status`, drop any path whose
`last_writer_task_id` is set and is NOT the current task's id.
Paths with empty `last_writer_task_id` (legacy entries) keep the
previous reporting behaviour for back-compat.

## Testing
- `python debug/test_file_history.py` — 34 existing tests still pass.
- `python debug/test_concurrent_fh_attribution.py` — new regression
  test:
  - Task-B writes `b_only.py`, Task-A writes nothing → A's bar must
    be empty, B's bar must show `b_only.py`.
  - Threaded variant with the same expectation.

## Invariants
- The fh attribution filter must NEVER drop entries with
  `last_writer_task_id == ''` because legacy `tracked.json` files
  written before this fix carry no attribution.
- The `_project_lock` is an RLock — re-entrancy is required for
  the existing `@with_project_lock` decorators inside the critical
  region to remain correct.
- `last_writer_task_id` lives on the tracked entry (per-file), not
  on the snapshot record, because the misattribution happens at
  diff time which keys on the tracked index.

