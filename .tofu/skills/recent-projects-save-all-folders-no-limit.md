---
name: recent-projects-save-all-folders-no-limit
description: Recent projects: LIMIT removed server-side, frontend mpApplyFolders saves every folder (primary + extras)
enabled: true
tags: [project-tool, recent-projects]
created: 2026-05-01T17:35:30Z
updated: 2026-05-01T17:35:30Z
---

# Recent projects list — completeness fixes

## Two bugs that caused "new project doesn't show up in recent list"

### 1. Server SQL had `LIMIT 20`
`lib/project_mod/config.py` `get_recent_projects()` hardcoded
`ORDER BY last_used DESC LIMIT 20`. If the user had 20+ projects,
older-but-recently-reopened ones could be pushed below the window.

Fix: removed the `LIMIT` entirely. Frontend decides how many to show.
Keep the sort (`ORDER BY last_used DESC`) so the newest always comes first.

### 2. Frontend only saved the PRIMARY path
`static/js/project.js` `mpApplyFolders` only did `saveRecentProject(data.path)`
after /api/project/set_paths, so extra workspace roots (extras beyond the
primary) were never persisted to `recent_projects` and never appeared in
the recent list dropdown.

Fix: iterate `_mpFolders` and save every non-empty path. The backend
UPSERT in `save_recent_project` (`ON CONFLICT(path) DO UPDATE`) handles
de-dup correctly — each path gets its own row with incremented count.

## Related
- Endpoint: `routes/project.py` `/api/project/recent` (GET/POST/DELETE)
- Render: `static/js/project.js` `renderRecentProjects()`
- DB schema table: `recent_projects(path PK, count, last_used)`

