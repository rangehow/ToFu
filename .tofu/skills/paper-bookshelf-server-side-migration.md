---
name: paper-bookshelf-server-side-migration
description: Paper bookshelf moved from localStorage to SQL (paper_library table); shared across browsers
enabled: true
tags: [paper-mode, database, schema-migration, bookshelf, architecture]
created: 2026-04-18T02:40:40Z
updated: 2026-04-18T02:40:40Z
---

# Paper Bookshelf: Server-side Storage

## Problem
Paper library (bookshelf) used to live in `localStorage` under keys
`paper_library` and `paper_active_id`. This meant two computers viewing
the same server showed different bookshelves — inconsistent with
conversations (which are in SQLite) and contrary to user expectation
since PDFs/reports/images are all server-side.

## Solution (schema v11)
Added `paper_library` table:
```sql
CREATE TABLE paper_library (
  id TEXT NOT NULL,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title TEXT, pdf_url TEXT, pdf_filename TEXT,
  arxiv_id TEXT, paper_hash TEXT, parsed_text TEXT,
  qa_history TEXT DEFAULT '[]',     -- JSON array
  images TEXT DEFAULT '[]',         -- JSON array
  babel_cache TEXT DEFAULT '{}',    -- JSON object
  page_count INTEGER, created_at BIGINT, updated_at BIGINT,
  PRIMARY KEY (id, user_id)
)
```
Also added PK mapping in `lib/database/_sql_translate.py` for PG.

## API (routes/paper.py)
- `GET /api/paper/library` — list all papers. Includes `hasReport` computed by joining against `paper_reports(paper_hash)`.
- `PUT /api/paper/library/<id>` — upsert single entry. Per-paper PUT so one save never clobbers another paper's save.
- `DELETE /api/paper/library/<id>` — remove from bookshelf. PDF file on disk is intentionally left alone (may be referenced by other entries / report cache).

## Frontend (static/js/paper-reader.js)
- `_loadPaperLibrary()` is now async — fetches from server. Called from `enterPaperMode()` with `await`.
- `_persistPaperEntry(entry)` — single source of truth for PUT.
- `_saveActivePaperState()` → calls `_persistPaperEntry()`.
- `_migrateLegacyLibrary()` — one-time migration of old `localStorage['paper_library']` to server. Guarded by flag `paper_library_migrated_v1`.
- `_activePaperId` still in localStorage (it's a per-browser "last viewed" pointer, not shared state).

## Removed as dead code
- `_savePaperLibrary()` — replaced by per-entry `_persistPaperEntry()`.
- `_savePaperState()` / `_restorePaperState()` — legacy conversation-based restore, no longer used.
- `conv.paperMode` handling in `main.js` `_applyConvUIState()`.
- `hasReport` flag stored on entry (now computed server-side from `paper_reports`).
- `entry.report` field (full report always lives in `paper_reports` table).

## Key invariants
- PDF bytes: `uploads/papers/<filename>` — shared by all entries referencing the same file.
- Report: `paper_reports` table, keyed by `paper_hash + lang`.
- Bookshelf entries: `paper_library` table, keyed by `id + user_id`.
- Images: `uploads/papers/images/<paper_hash>/` + manifest.json.

