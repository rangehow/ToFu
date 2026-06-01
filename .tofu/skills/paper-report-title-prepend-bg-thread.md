---
name: paper-report-title-prepend-bg-thread
description: Paper report title — bg-thread DB lookup AND racing with paper_library upsert; client_title fallback
enabled: true
tags: [paper-mode, background-task, flask, database, bug-fix]
created: 2026-05-24T01:30:09Z
updated: 2026-05-24T07:23:59Z
---

# Paper report — title prepend silently failing

## Symptoms over time
- Reports rendered without the top-level `# Title` heading.
- Title prepend log line `Prepended title:` never appeared in app.log.

## Root cause #1: bg-thread DB lookup
`_lookup_paper_title` originally called `lib.database.get_db()` which is
**request-scoped** (binds to `flask.g`). Called from `_run_report_task`
on a TaskRuntime worker thread → `RuntimeError: Working outside of
application context` → caught silently → empty title.

**Fix**: try `get_db()` then fall back to `get_thread_db()`:
```python
try:
    db = get_db()
except Exception:
    db = get_thread_db()
```
Bumped log level on the failure path from `debug` to `warning`.

## Root cause #2: race with paper_library upsert
Even with the DB fallback, the `paper_library` row may not yet exist
when the report worker queries it. Frontend calls
`_persistPaperEntry()` (PUT /api/paper/library/<id>) **without await**;
the user can click Report → fire `/api/paper/report/start` before that
PUT finishes. `_lookup_paper_title` then returns `''`.

**Fix**: frontend sends the active entry's title in the report start
request body (`title: clientTitle`); backend stores it on the task
(`task['client_title']`) and uses it as the fallback when
`_lookup_paper_title` is empty:
```python
title = _lookup_paper_title(phash) or task.get('client_title') or ''
```
Also strips trailing `.pdf` from the client title.

## General lesson
- Any helper called from BOTH a Flask route AND a background worker must
  not assume `flask.g`.
- Don't depend on a fire-and-forget PUT having landed before another
  request reads the same row. Pass critical data in the second request's
  body, or `await` the PUT. Same lesson applies to any racing pair.

