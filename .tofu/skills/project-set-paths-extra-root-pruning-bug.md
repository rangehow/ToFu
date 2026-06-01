---
name: project-set-paths-extra-root-pruning-bug
description: Bug fix: set_project_paths didn't prune extra roots when primary unchanged — modal "remove folder" silently no-op'd
enabled: true
tags: [python, project-mod, multi-root, bug-fix, set_project_paths, idempotence]
created: 2026-04-27T23:11:18Z
updated: 2026-04-27T23:11:18Z
---

# set_project_paths — extra-root pruning bug

## Symptom
Multi-root workspace: selecting two project dirs, then removing one from the
project bar modal and clicking "Set Project" did nothing — the removed folder
kept appearing.

## Root cause
`lib/project_mod/scanner.py::set_project_paths()` claimed (in its docstring) to
auto-remove extras not in the new list, but the implementation only called
`set_project(primary)` + `add_project_root(ep)` for each extra.

`set_project()` has an idempotence guard (`same_primary=True`) that PRESERVES
existing `_roots` when the primary path is unchanged. This guard exists so that
mid-conversation `tool_create_project` roots survive repeated frontend
`/api/project/set` calls (page reload, conv restore, etc.). But it means
`set_project_paths` must explicitly prune stale extras itself.

## Fix
After `set_project(primary)`, iterate `_roots` and remove any root whose path
is neither the primary nor in the new `extras` list. Done via `remove_project_root(name)`.

## Files
- lib/project_mod/scanner.py — `set_project_paths()` now has a prune step
  between "set primary" and "add extras"

## Related
- `same_primary` idempotence guard in `set_project()` — necessary, don't remove
- `chatui_create_project_frontend_sync_bug` (referenced in the guard comment)

