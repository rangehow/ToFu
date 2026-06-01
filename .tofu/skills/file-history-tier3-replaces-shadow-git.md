---
name: file-history-tier3-replaces-shadow-git
description: Tier-3 redesign 2026-05-08: lib/file_history/ replaces lib/project_mod/git_shim.py — copy-backups instead of shadow git, no FUSE timeouts, no index.lock
enabled: true
tags: [architecture, file-history, undo-redo, fuse]
created: 2026-05-08T13:44:16Z
updated: 2026-05-08T13:44:16Z
---

# File-History Replaces Shadow-Git (Tier-3 redesign, 2026-05-08)

## Why
The old `lib/project_mod/git_shim.py` synthesised a parallel git repo
(`lib/.project_sessions/<hash>/shadow.git`) to give round-by-round undo,
diff, history, and blame.  On slow FUSE this kept timing out: `git add -A`
walked the whole worktree, leaving stale `index.lock` files that needed
self-healing.  We accumulated workarounds (per-repo RLock,
`--no-optional-locks`, kill switches, dual timeout bases) without
removing the root cause: trying to make git's worktree-scoped operations
work on cross-DC FUSE mounts.

## What changed
**Replaced** with `lib/file_history/` — a Claude-Code-style copy-backup
store modeled on `claude-code:src/utils/fileHistory.ts`:

* Disk layout: `<base_path>/.chatui/file-history/`
  * `snapshots.jsonl` — append-only round log
  * `tracked.json` — index of `{rel_path → {latest_version, mtime, size}}`
  * `backups/<sha256(rel)[:2]>/<sha256(rel)>@v<n>` — copy backups
* Bounded work — every operation is O(files this session touched), not
  O(worktree).  No subprocess spawns.
* Inside the project (`<base>/.chatui/`) so history follows project moves.

## Public API (lib/file_history)
```python
fh.is_enabled() / fh.probe_enabled()
fh.track_edit(base, rel_path, *, message_id=None, pre_content=None) -> int|None
fh.make_snapshot(base, *, task_id, conv_id, tool_names, summary, rel_paths, external=False, redo_of=None) -> snapshot_id
fh.list_history(base, *, path=None, limit=20) -> list
fh.diff_name_status(base, from_id, to_id) -> list[{path, action}]
fh.rewind_to(base, snapshot_id) -> {ok, files, failed, ...}     # undo
fh.restore_from(base, snapshot_id) -> {ok, files, newSnapshotId} # redo
fh.detect_external_edits(base) -> {committed, snapshotId, files}
fh.get_last_snapshot_id(base) -> str|None
```

## Pre-write hook semantics
`_record_modification()` runs AFTER the write tool has overwritten the
file, so we MUST pass `original_content` explicitly via
`fh.track_edit(... pre_content=...)`.  Reading from disk would just
capture the post-image.  This is wired in:
* `lib/project_mod/modifications.py::_record_modification` calls
  `fh.track_edit(... pre_content=original_content)` when `existed=True`.
* `lib/project_mod/write_tools.py::_apply_one_diff` and `_insert_one`
  now pass the pre-image as `original_content` so apply_diff/insert_content
  also use the deterministic fh-restore undo path.

## Orchestrator integration
* `_run_commit_round_async` calls `fh.make_snapshot(...)`, captures
  `prev_snap = fh.get_last_snapshot_id(...)` before the call, then uses
  `fh.diff_name_status(prev_snap, _snap_id)` to enrich the
  `modifiedFileList` SSE payload.
* External-edit probe uses `fh.detect_external_edits(...)` (mtime-based,
  bounded by tracked-file set).
* SSE `round_committed` event now carries `snapshotId` (canonical) plus
  `gitSha` (legacy alias = same value, kept for FE backward-compat).

## What was removed
* `lib/project_mod/git_shim.py` (deleted, ~1207 lines)
* `debug/test_git_shim.py` (deleted)
* `routes/project.py::project_history`, `project_diff` and the
  `_parse_git_route_args` helper (unused by frontend)
* LLM-facing tools `project_history`, `project_diff`, `project_blame`
  (the model rarely invoked them; for line-level attribution in a real
  git repo, users can `run_command git blame`)
* `tool_project_history`, `tool_project_diff`, `tool_project_blame`
  handlers in `lib/project_mod/tools.py`
* Env vars `CHATUI_PROJECT_GIT*` (no longer consulted)
* `lib/project_mod/__init__.py` no longer re-exports `git_shim`

## What was kept
* `routes/project.py::project_redo` — backed by `redo_task_modifications`
  which now uses `fh.restore_from`.
* `lib/project_mod/modifications.py::redo_task_modifications` — finds
  the snapshot by taskId in `fh.list_history(...)` and calls
  `fh.restore_from`.
* `gitSha` field on SSE events and persisted `_gitSha`/`_snapshotId`
  on assistant messages — frontend assigns but doesn't yet read them.
* The undo flow in `_undo_modifications_list` — the new
  `fh_restore` short-circuit (using `fh.store.read_blob`) replaces the
  previous `blob_restore` short-circuit.  Legacy `originalContent` /
  `reversePatch` paths remain as fallbacks.

## Env knobs
* `CHATUI_FILE_HISTORY=0` — disable the store entirely (everything
  short-circuits to no-op / empty).
* `CHATUI_FILE_HISTORY_PROBE=0` — disable the per-round external-edit
  probe but keep snapshots.

## Testing
* `python debug/test_file_history.py` — 34 scenarios pass: bootstrap,
  diff, rewind, redo, side-channel, binary, UTF-8, drift, dedup,
  concurrency, disabled mode.
* `python debug/test_project_prompts.py` — confirms
  `PROJECT_TOOLS` is now 8 entries (down from 11 — no project_*).

## Tunables (lib/file_history/store.py)
* `MAX_VERSIONS_PER_FILE = 20` — older versions GC'd.
* `MAX_BACKUP_SIZE_BYTES = 16 MB` — files larger are tracked but not
  backed up; rewind through such a version is a no-op with warning.
* `SOFT_DISK_BUDGET_BYTES = 256 MB` — informational, no auto-compaction
  yet.

## Migration / fallout
Existing shadow repos in `lib/.project_sessions/<hash>/shadow.git/` are
now orphaned but harmless.  In-flight conversations lose their git-shim
history when the server restarts but new file-history starts cleanly
on first write.  Frontend continues to assign `_gitSha` from SSE; the
value is now a snapshot UUID instead of a git sha (both are opaque
identifiers).

