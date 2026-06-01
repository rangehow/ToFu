---
name: multi-root-cross-project-path-misrouting-fix
description: Multi-root workspace fixes: cross-root auto-route safety, extra roots persist across conv switches, projectPaths in chat config, ensure_project_state with extras, system prompt warns about new file creation
enabled: true
tags: [python, javascript, multi-root, project-tools, path-routing, bug-fix, cross-project, conv-switch, state-persistence]
created: 2026-03-31T09:55:17Z
updated: 2026-04-17T04:25:03Z
---

# Multi-Root Cross-Project Path Misrouting Fix

## Problem (Original)
When multiple project roots are configured (e.g. frontend + backend), the model frequently
writes files intended for root B into root A because:

1. **Silent fallback** — `_resolve_base()` falls through to primary root when model omits `rootname:` prefix
2. **No `run_command` multi-root support** — always uses primary root as `cwd`
3. **Weak system prompt** — vague "use rootname:path syntax" instruction easily forgotten by model
4. **No cross-check** — file existence never verified against other roots

## Problem (2026-04-17 — Extra Roots Lost on Conv Switch)
Extra roots were silently lost when switching conversations:

1. **Frontend only sent `projectPath` (singular)** in chat config → backend never knew about extras
2. **`ensure_project_state()` only accepted primary path** → called `set_project()` which **clears all roots**
3. **`loadProjectStatus()` only restored primary** via `/api/project/set` instead of `/api/project/set_paths`
4. **System prompt didn't warn about new file creation** in non-primary roots (no auto-detection for new files)

## Fixes

### Phase 1 (Original — path routing)
- `_resolve_base()` cross-root safety net for existing files
- `working_dir` param for run_command  
- Stronger system prompt with mandatory prefix table

### Phase 2 (2026-04-17 — conv switch root persistence)

#### 1. `static/js/main.js` — `_buildConvConfig()` 
Added `projectPaths: conv.projectPaths || []` to chat config alongside `projectPath`.

#### 2. `lib/project_mod/scanner.py` — `ensure_project_state(path_str, extra_paths=None)`
New `extra_paths` parameter. Compares both primary AND extras against current state.
If extras differ, calls `set_project_paths()` instead of `set_project()`.

#### 3. `lib/tasks_pkg/orchestrator.py` — Extract extras from cfg
Reads `cfg.get('projectPaths')` and passes `_extra_paths` to `ensure_project_state()`.

#### 4. `static/js/project.js` — `loadProjectStatus()` 
Uses multi-path API when conversation has extras. Also checks if primary matches but
extras don't, and re-applies with all paths.

#### 5. `lib/project_mod/indexer.py` — System prompt enhancement
Added explicit warning about new file creation in non-primary roots:
- "There is no auto-detection for new files (the file doesn't exist yet to check)"
- Added `write_file(path='root:src/new_file.py')` and `apply_diff(edits=[{path: 'root:...'}])` examples

## Architecture: Multi-Root Data Flow
```
Frontend conv.projectPaths = ['/primary', '/extra1', '/extra2']
  ↓ _buildConvConfig → config.projectPaths = ['/primary', '/extra1', '/extra2']
  ↓ POST /api/chat/start → cfg['projectPaths']
  ↓ orchestrator: _extra_paths = cfg['projectPaths'][1:]
  ↓ ensure_project_state(project_path, extra_paths=_extra_paths)
  ↓ compares primary + extras vs _state + _roots
  ↓ if different → set_project_paths([primary] + extras)
```

