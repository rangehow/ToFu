---
name: paper-translate-server-task
description: Babel translation is a server-owned task (start/poll/abort/lookup/cache) keyed by (paper_hash, lang)
enabled: true
tags: [paper, translation, task-orchestration]
created: 2026-05-19T16:18:41Z
updated: 2026-05-19T16:18:41Z
---

# Paper Reading Mode — Babel Translation Task

## Architecture
Mirrors `paper_reports` task design: keyed by ``(paper_hash, lang)``, one
running task at a time per pair, append-only events list, persisted to a
``paper_translations`` table on completion. Same lifecycle the report
generator uses.

## Endpoints (all in `routes/paper.py`)
- ``POST /api/paper/translate/start`` — body ``{paper_text, lang, paper_hash?, model?, force?}``.
  Returns ``{ok, task_id, paper_hash, running, existed}`` or, on cache hit,
  ``{ok, cached: true, text}``.
- ``GET /api/paper/translate/poll?task_id=…&cursor=N`` — append-only events
  ``{type: 'status' | 'chunk' | 'done' | 'error'}``.
- ``POST /api/paper/translate/abort`` — sets the abort event.
- ``POST /api/paper/translate/lookup`` — find an existing running task by
  ``(paper_hash, lang)``.
- ``POST /api/paper/translate/cache`` — DB cache only, no task spawn.

## Chunking (server-side)
`_run_translate_task` splits paper_text on **paragraph boundaries** (``\n\n+``)
greedily up to ``_TRANSLATE_CHUNK_SIZE`` (2400 chars) — never breaks
sentences/equations mid-way. Long paragraphs over the cap are sliced.
Each chunk → ``dispatch_stream`` → emit ``{type: 'chunk', index, total, text}``.
Aborted runs persist nothing.

## Frontend (`static/js/paper-reader.js`)
- `_babelTranslateAllPages(lang)` polls the server task and updates the
  progress bar from ``ev.index`` / ``ev.total``.
- If the user switches to a different target language mid-translate, the
  client POSTs ``/api/paper/translate/abort`` to free the worker.
- The old client-side chunking + per-chunk SSE-stream parsing is gone.

## Schema
```sql
CREATE TABLE paper_translations (
    paper_hash TEXT NOT NULL, lang TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    PRIMARY KEY (paper_hash, lang)
);
```
PG translation requires `_PK_MAP['paper_translations'] = ['paper_hash', 'lang']`
in `lib/database/_sql_translate.py` so `INSERT OR REPLACE` rewrites
correctly.
