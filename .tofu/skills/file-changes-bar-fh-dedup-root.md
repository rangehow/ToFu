---
name: file-changes-bar-fh-dedup-root
description: file_history side-channel must dedup by (fh_root, path) to match mod['root'] tagging or rows duplicate
enabled: true
tags: [orchestrator, file_history, modifications, ui-bug]
created: 2026-05-12T05:07:52Z
updated: 2026-05-12T05:07:52Z
---

# File-changes bar — duplicate rows after fh side-channel merge

## Symptom
The "N files changed" bar shows the same file twice — once with a
`rootname:` prefix (PATCHED), once without (MODIFIED).  Conv example:
`mp0sggcln5pruo` showed `chatui:static/js/ui.js PATCHED` plus
`static/js/ui.js MODIFIED` for the same edit.

## Root cause
Two writers contribute to `task['modifiedFileList']`:

1. **`lib/project_mod/modifications.py:_record_modification`** — tags
   each mod with `root=<name>` by reverse-looking-up `base_path` in
   the global `_roots` registry.  Carried through into the
   `modifiedFileList` items emitted by `_emit_done_event`.

2. **`lib/tasks_pkg/orchestrator.py:_run_commit_round_async`** — runs
   `fh.diff_name_status` post-snapshot to catch files that
   modifications.py missed (run_command / code_exec / MCP side
   effects).  fh stores only relative paths — it knows nothing about
   the workspace-root NAME the UI uses.

The original dedup keyed `seen_paths` from `existing` by
`(f.get('root',''), f.get('path',''))` but built the new key as
`('', entry['path'])`.  Whenever (1) ran first and tagged a non-empty
root, (2) failed to dedup and re-added the file as an unrooted
duplicate.

## Fix
In `_run_commit_round_async`, resolve the project_path → root NAME
once via `lib.project_mod.config._roots` (under `_lock`).  Build the
seen-set with BOTH `(root, path)` AND `('', path)` aliases so:
  - new fh entries with a known root collapse against existing rooted entries;
  - and vice-versa for any path that arrived with no root tag.
Tag new fh items with the resolved root name when available.

## Invariants
- `mod['root']` is the workspace-root NAME (basename-derived, e.g.
  `chatui`), not an absolute path.
- fh layer is path-only; any UI-facing rooting must be applied at the
  consumer (here, the orchestrator merge).
- Dedup must always include the unrooted-alias fallback because the
  registered name is keyed on `_roots` which may be empty for transient
  / unregistered base paths.

