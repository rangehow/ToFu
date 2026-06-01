---
name: overleaf-mcp-api-quirks
description: Overleaf web API endpoints & body shapes confirmed against overleaf/overleaf tests
enabled: true
tags: [overleaf, mcp, api, web-api]
created: 2026-04-19T09:33:14Z
updated: 2026-04-19T09:33:14Z
---

# Overleaf web API quirks (verified 2026-04)

All confirmed against upstream `overleaf/overleaf` repo tests.

## Create project
- **URL**: `POST /project/new` (NOT `/project/new/blank` — that returns 404)
- **Body**: `application/x-www-form-urlencoded`, fields:
  - `projectName=<name>` (required)
  - `template=example` (optional; omit for blank)
- **Response**: JSON `{"project_id": "<24hex>"}`
- **Headers**: session cookie + `x-csrf-token` from `<meta name="ol-csrfToken">`
- Reference: `services/web/test/acceptance/src/ProjectDuplicateNameTests.mjs`
  and `.../helpers/User.mjs` (`createProject` helper).

## Download PDF / log
- `/project/<id>/output/output.pdf` — 404 on current prod site.
- Use the per-build `url` field in `compile` API's `outputFiles[]` instead.

## List projects
- Parse `<meta name="ol-prefetchedProjectsBlob">` JSON from `/` dashboard.
- Entries: `{id, name, trashed, archived, ...}` — filter out trashed/archived.

## Natural project name
- `git_client` / `ProjectConfig` does NOT have the real title — only a
  placeholder derived from `project_id[:8]`. To show the human name
  (e.g. in `status_summary`), resolve it via `list_projects_web()` and
  match by `project_id`.

## status_summary output
- Shows ALL sections (no `[:15]` truncation). Callers rely on it as
  a complete bird's-eye view. If the list is long, that's fine — the
  user explicitly asked for full content.

