---
name: recent-projects-save-all-folders-no-limit
description: Recent projects: LIMIT removed server-side; frontend mpApplyFolders saves every folder; MODEL-added roots saved backend-side in write_tools._save_model_added_root_to_recent
enabled: true
tags: [project-tool, recent-projects]
created: 2026-05-01T17:35:30Z
updated: 2026-07-08T05:16:40Z
---

# Recent projects list — completeness fixes

## Bugs that caused "new project doesn't show up in recent list"

### 1. Server SQL had `LIMIT 20`
`lib/project_mod/config.py` `get_recent_projects()` hardcoded
`ORDER BY last_used DESC LIMIT 20`. Fix: removed the `LIMIT` entirely,
kept the sort. `save_recent_project` UPSERTs (`ON CONFLICT(path) DO UPDATE`).

### 2. Frontend only saved the PRIMARY path
`static/js/project.js` `mpApplyFolders` only did `saveRecentProject(data.path)`.
Fix: iterate `_mpFolders` and save every non-empty path.

### 3. MODEL-added roots were never saved (2026-07-08)
A root the ASSISTANT registers — `tool_create_project` OR the absolute-path-write
auto-register (`_resolve_write_path` §2) — was added to `_roots`/conv registry but
never written to `recent_projects`. The frontend `workspace_root_added`/`create_project`
SSE handlers refresh projectState but (a) gate on `activeConvId`, (b) never call
`saveRecentProject` — so a background/non-active conv's registration was dropped.

**Fix (backend, the RIGHT boundary):** `lib/project_mod/write_tools.py`
`_save_model_added_root_to_recent(abs_path)` — best-effort `save_recent_project`,
skips temp-dir scratch (`_is_temp_path`), debug-level fallback, imports
`save_recent_project` at call time (so tests stub `cfg.save_recent_project`).
Wired into BOTH: `tool_create_project` (after registration) + the abs-path
auto-register `if not _existing` branch. A write under an existing root saves nothing new.
Test: `tests/test_temp_write_and_root_signal.py::RecentProjectPersistenceTest` (4).

## Read path (no server-side filter beyond the sort)
`save_recent_project` → `get_recent_projects()` (config.py, ORDER BY last_used DESC,
no LIMIT/WHERE) → `GET /api/v1/project/recent` {projects:[...]} →
`renderRecentProjects()` (project.js, re-fetched every modal open) →
`_renderRecentList()` (only filters on the user search box). Newest-first.

## Guardrail
Anything the MODEL registers that should surface in the UI must be persisted at the
BACKEND registration boundary, not a frontend SSE handler that gates on the active conv.

## Related
- Endpoint: `routes/api_v1/project.py` `/api/v1/project/recent` (GET/POST/DELETE)
- Render: `static/js/project.js` `renderRecentProjects()` / `_renderRecentList()`
- DB schema table: `recent_projects(path PK, count, last_used)`
