---
name: skill-package-install-architecture
description: Drag-and-drop skill package install: directory-style memories with SKILL.md, AgentSkills/OpenClaw frontmatter, BM25 over SKILL.md only, references/ via Progressive Disclosure
enabled: true
tags: [memory, skills, openclaw, anthropic, installer, architecture]
created: 2026-05-06T17:28:14Z
updated: 2026-05-07T02:01:02Z
---

# Skill-Package Install — Architecture (Memory + Skills Store)

## Two surfaces, one substrate
There are **two UI entry points**, both backed by the same memory storage:

1. **Memory modal** (Enhancements → Memory) — flat `.md` notes + drag-drop
   skill packages. Quick add for individual notes, drag a `.zip` to install.
2. **Settings → Skills tab** — App-Store-style browser (mirrors the MCP
   tab): search bar, scope toggle (Catalog / Installed), category pills,
   curated catalog grid, drag-drop zone, and a per-package file browser.

Both write to `<project>/.chatui/skills/`. The model never sees the
distinction — `search_memories` returns flat `.md` and packaged
`<id>/SKILL.md` identically.

## On-disk shapes (under `<.chatui/skills>/`)
- **Flat** — `<id>.md` (created by `create_memory`).
- **Package** — `<id>/SKILL.md` plus `references/`, `scripts/`,
  `knowledge/`, `assets/`. Sub-files NOT indexed by BM25; the model
  reaches them via `read_files(<package_dir>/references/...)` once
  SKILL.md is in scope (Anthropic / OpenClaw Progressive Disclosure).

## Backend files
- `lib/memory/storage.py`
  - `_parse_frontmatter` accepts JSON metadata + folded YAML scalars.
  - `_extract_package_metadata` reads `metadata.openclaw` / legacy
    `metadata.clawdbot` → `requires_bins`, `requires_any_bins`,
    `requires_env`, `requires_os`, `always`, `homepage`, `primary_env`,
    `install_specs`.
  - `_check_memory_eligible` honors all of the above plus `requires_os`
    via `sys.platform`.
  - `_list_memories_in_dir` discovers BOTH `<id>.md` and `<id>/SKILL.md`.
  - `delete_memory` recursively removes `package_dir` (with realpath
    containment check inside `project_path`).
  - `resolve_target_dir(scope, project_path)` shared helper.
- `lib/memory/installer.py` — `install_skill_package(source, scope,
  project_path, overwrite)` accepts `.zip` path / directory path /
  `bytes`. Auto-walks 1–3 wrapper levels to find SKILL.md. Hard caps:
  25 MB / 2000 entries. Rejects path traversal, symlinks, archives
  without SKILL.md. **Never executes `install.sh`** → surfaces it as an
  `install_hints` entry only.
- `lib/memory/catalog.py` — `get_catalog()` returns curated
  `SkillCatalogEntry` list with `id`, `name`, `description`, `icon`,
  `category`, `download_url` (https zip), `homepage`, `tags`,
  `featured`, `author`, `requires`. Default catalog includes the
  Anthropic Skills bundle (docx/xlsx/pdf/pptx/artifacts/webapp-testing/
  skill-creator) and an OpenClaw starter. Internal Meituan skills
  (citadel, mlp-skills) are NOT in the default catalog — users drag-drop
  the zips, or set `CHATUI_SKILL_CATALOG_URL` for a private registry.

## REST API (`routes/memory.py`)
- `GET /api/memory` — list all memories (flat + packages, with
  `is_package`, `package_dir`, `eligible`, `ineligible_reasons`).
- `POST /api/memory/install` — multipart `file` (zip) + form fields
  `scope`, `overwrite`. Also accepts JSON `{path}` for local paths.
- `POST /api/memory/catalog/install` — JSON `{skill_id, scope?,
  overwrite?}`. Backend streams the `download_url` (cap 50 MB,
  60 s timeout) into the installer.
- `GET /api/memory/catalog` — returns the catalog with each entry
  annotated with `installed: bool`.
- `GET /api/memory/<id>/files` — for package memories, returns
  `{root, files: [{path, size, kind}]}`. Used by the Skills tab file
  browser for human inspection (the LLM still uses `read_files`).

## Frontend
### Settings → Skills tab (primary UX)
- `index.html` — `#settingsTab_skills` panel with `mcp-store-header`
  (mirrors MCP), `.skills-scope-tabs` toggle, `#skillsCategoryBar`,
  `.skills-drop-zone` (clickable + drag target), `#skillsCatalogGrid`,
  and `#skillsFilesOverlay` modal for the file browser.
- `static/js/skills.js` — `_populateSkillsTab()` (called from
  `openSettings` after `_populateMcpTab`). Fetches `/api/memory/catalog`
  + `/api/memory?scope=all`, renders cards with the same
  `mcp-app-card` shell. `_skillsCatalogInstall(id, btn)` POSTs to
  `/catalog/install`. `_skillsViewFiles(id)` opens the file browser.
  Drag-drop zone covers the whole tab panel; uses depth counter and
  `dataTransfer.types.includes('Files')` to ignore intra-app drags.
- `static/styles.css` — `.skills-scope-tabs`, `.skills-drop-zone`,
  `.skill-card-footer`, `.skill-badge-official`, `.skill-badge-warn`,
  `.skills-files-modal`, `.skills-toast`. Reuses `.mcp-app-card` /
  `.mcp-cat-pill`.

### Memory modal (kept, slimmed)
- Footer button "技能市场 / Skills Store" → `_openSkillsStoreFromMemory()`
  closes the memory modal and opens Settings → Skills.
- Drag-drop overlay still works inside the memory modal for users on
  the old workflow (`memory.js: _attachMemoryDropZone`).

### i18n
- New keys in `i18n.js`: `settings.tabSkills`, `skills.title`,
  `skills.tabCatalog`, `skills.tabInstalled`, `skills.searchPh`,
  `skills.intro`, `skills.dropZone`, `skills.installBtn`,
  `skills.viewFiles`, `memory.openSkillsStore`, `common.close`.

## Why a separate Skills tab (not just inside Memory modal)
The MCP tab established a strong UX pattern for "an installable
ecosystem with credentials, search, categories, repo links". Skills
share all of that (curated catalog, install → connect → status,
inspectable contents). Forcing this into a 600-px modal squeezed
out search, category filters, and the file browser. The Memory
modal stays for what it does well: quick CRUD on flat notes.

## What we deliberately did NOT do
- **No new tool**. Discovery is `search_memories` (BM25 already
  truncates body to 2000 chars).
- **No router/exploder.** mlp-skills installs as ONE package
  containing many `skills/<sub>/SKILL.md` files.
- **No `install.sh` execution** — surfaced as `install_hints` only.
- **No rename** of `.chatui/skills/` directory.
- **No bundling of internal-only skills** in the default catalog —
  that would leak through to the opensource export.

## Test recipe
```python
from lib.memory.installer import install_skill_package
from lib.memory.catalog import get_catalog
import tempfile
with tempfile.TemporaryDirectory() as tmp:
    res = install_skill_package('/path/to/citadel.zip',
                                scope='project', project_path=tmp)
    print(res['memory']['package_dir'])
print('catalog entries:', len(get_catalog()))
```
Verified against `citadel.zip` (12 files, 1 SKILL.md + references/),
`mlp-skills.zip` (135 files, 3 install hints surfaced), and the
catalog endpoint annotation logic.

