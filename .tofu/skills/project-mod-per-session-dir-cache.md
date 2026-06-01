---
name: project-mod-per-session-dir-cache
description: Modification history uses per-session-dir cache (not a single global list) to prevent data loss on UI project switches during active tasks
enabled: true
tags: [python, project-mod, modifications, undo, concurrency, bug-fix, multi-project]
created: 2026-04-22T11:16:44Z
updated: 2026-04-22T11:16:44Z
---

# Modifications module — per-session-dir cache (2026-04-22 refactor)

## Background — the bug

`lib/project_mod/modifications.py` used to keep a **single** global list
`_state['modifications']` for the currently-active project.  Every UI
project switch called `_start_new_session()` which **wiped that list**
and repopulated it from the newly-active project's file.  But
`_record_modification()` then wrote the (swapped-in) list back into the
*original* task's session file — silently clobbering that project's
history.

Real-world symptom: a task modified `install.sh`, `install.py`, and
`bootstrap.py` successfully, but the UI's "file changes" bar showed only
`bootstrap.py` because the UI had briefly switched to another project
mid-task, then back.  (Conv `mo9wvbk5ae4kpf`.)

## The robust fix

**Disk is the source of truth per session_dir**; memory is a per-
session-dir cache keyed by absolute `session_dir` path.

Key pieces in `lib/project_mod/modifications.py`:

- `_mods_cache: dict[session_dir, list]` — independent list per project.
- `_loaded_dirs: set[session_dir]` — avoid redundant cold reads.
- `_locked_rmw(session_dir, mutator)` — single atomic
  read-modify-write primitive holding `_lock` through cache fetch +
  mutation + disk flush.  All public mutations funnel through this.
- `_cache_get(session_dir)` / `_flush_to_disk(session_dir, mods)` —
  small helpers, caller must hold `_lock`.
- `_sync_primary_view(session_dir)` — mirrors the cache into the legacy
  `_state['modifications']` / `_state['sessionId']` **as a read-only
  projection** for `get_state()` / UI badges.  Nothing outside this
  module should ever write to `_state['modifications']` again.
- `_start_new_session(base_path)` is now **non-destructive**: it warms
  the cache for the requested session_dir and repoints the primary
  mirror.  Other session_dirs' caches (and running tasks using them)
  are untouched.

All of `_record_modification`, `get_modifications`,
`get_conv_ids_with_modifications`, `undo_conv_modifications`,
`undo_task_modifications`, `undo_all_modifications` are now keyed on
the task's own `base_path`, so a UI project switch can never leak into
another project's records.

## Secondary fix — stale-tmp race

The pre-existing `_clean_stale_tmp()` deleted **all** `.modifications_*.tmp`
files unconditionally on `_start_new_session`, which raced against a
concurrent `_atomic_json_write` whose freshly created tmp file could
get deleted before `os.replace`.  Fixed with two safety rails:

- **Once-per-session_dir-per-process** via `_stale_cleaned: set`
  (guarded by `_lock`).
- **Age-gated** via `_STALE_TMP_AGE_SECONDS = 60.0` — only files older
  than 60s (genuine crash artifacts) are deleted.

## Concurrency test

Stress test in the fix verifies: 4 writer threads × 200 writes against
project A, interleaved with a flipper thread doing 100 A↔B project
switches.  Before fix: many records lost.  After fix: **0 lost, disk
matches**.

## Invariant to preserve

**Never mutate `_state['modifications']` outside `lib/project_mod/modifications.py`.**
It is a read-only mirror maintained only by `_sync_primary_view` and
`_locked_rmw`.  All other code paths must use the public API
(`get_modifications`, `_record_modification`, etc.) which are keyed
on the caller's `base_path`.

