---
name: backend-paper-decomposition
description: routes/paper.py decomposed: thin route layer (1520 LOC) + lib/paper/ package (12 modules)
enabled: true
tags: [refactor, backend, routes, paper, convention]
created: 2026-05-28T06:35:40Z
updated: 2026-05-28T06:35:40Z
---

# `routes/paper.py` Decomposition (2026-05-28)

Third backend hot-file decomposition. The pattern locked in by
`backend-translate-decomposition` and validated by
`backend-daily-report-decomposition` applies cleanly here.

## Before

Single 3089-LOC `routes/paper.py` with:
- 22 route handlers (Blueprint + endpoints)
- 388-line system prompts (EN + ZH)
- LLM streaming generator (worker thread + queue bridge)
- 143-line tool execution dispatcher (web_search / fetch_url)
- 261-line report tool-calling engine (with image injection on completion)
- 129-line translate engine (paragraph-aware chunking)
- Two TaskRuntimes (report + translate), each with their own
  (paper_hash, lang) → task_id dedup index
- Figure extraction + manifest I/O + deterministic image injection
- Two SQL-table schemas (paper_library, paper_reports, paper_translations)
- 241-line SSE handler (fetch_arxiv-stream with embedded thread + queue)
- 229-line export handler (MD/HTML/PDF, with KaTeX, base64-embedded images)

22 cross-module callers across `routes.api_v1.agents` (4 functions),
`tests/test_paper_migration.py` (14 private symbols),
`static/js/paper-reader.js` (comment reference).

## After

```
routes/paper.py          1520 LOC — Blueprint + 22 route handlers ONLY
                                   + back-compat re-exports (45+ symbols)
lib/paper/
  __init__.py             115 LOC — package facade
  hashing.py               35 LOC — _paper_hash, _safe_hash_dir, BASE_DIR/PAPER_DIR/PAPER_IMG_DIR
  prompts.py              387 LOC — _REPORT_PROMPT_EN/ZH, _REPORT_TOOLS, _MAX_REPORT_TOOL_ROUNDS
  llm_stream.py            63 LOC — _stream_llm_sse (Q&A SSE generator)
  tools.py                160 LOC — _execute_report_tool (web_search / fetch_url dispatcher)
  images.py               392 LOC — _FIG_EXTRACT_VERSION, _load/_extract/_ensure manifests,
                                   _inject_images_into_report,
                                   _lookup_paper_title, _ensure_title_heading,
                                   _build_image_manifest
  arxiv.py                 40 LOC — _extract_arxiv_id (modern + legacy IDs)
  library.py               55 LOC — _PAPER_LIB_COLUMNS, _LIB_*_CAP, _lib_row_to_dict
  report_runtime.py       115 LOC — _report_runtime, _report_dedup_index,
                                   _new_report_task, _append_report_event,
                                   _cleanup_stale_report_tasks
  report_engine.py        286 LOC — _run_report_task (tool loop + image injection + DB persist)
  translate_runtime.py     95 LOC — _translate_runtime, _translate_dedup_index,
                                   _new_translate_task, _LANG_NAMES, _TRANSLATE_CHUNK_SIZE
  translate_engine.py     155 LOC — _run_translate_task (paragraph-aware chunking)
```

Total LOC: 3418 (vs 3089 — increase from per-module docstrings + facade).

## Why the route file is still ~1500 LOC

Two HTTP-response handlers are very large but ARE genuinely route-layer
concerns and don't belong in `lib`:

- `export_report` (≈220 LOC) — Markdown→HTML rendering with KaTeX math
  protection, base64 image embedding, three response variants (MD/HTML/PDF
  with auto-print bootstrap). Pure response shaping; no reusable engine.
- `fetch_arxiv_stream` (≈240 LOC) — SSE generator with embedded thread +
  queue bridge that streams parse progress events. Not callable from
  another route or background task; it IS the endpoint.

These could be extracted but the abstraction wouldn't help: nothing else
calls them, and putting them behind a function in `lib/paper/exports.py`
would just move ~450 LOC for no win.

## Shared-state subtleties

Three pieces of mutable module-level state with verified identity through
the re-export chain:

- `_report_runtime` (TaskRuntime) — owner `report_runtime.py`,
  also `routes.paper._report_runtime` and `lib.paper._report_runtime` —
  same Python object across all three.
- `_translate_runtime` (TaskRuntime) — same pattern.
- Dedup indexes (`_report_dedup_index`, `_translate_dedup_index`) and
  the legacy-name shims (`_report_tasks`, `_report_tasks_lock`,
  `_translate_tasks`, `_translate_tasks_lock`) — all pass identity check.

`hashing.py` computes `BASE_DIR` via `os.path.dirname(...)` 3 levels up
from `lib/paper/hashing.py` (matching the original 2-level computation
from `routes/paper.py`). Verified at import time: `PAPER_DIR ==
<repo_root>/uploads/papers`.

## Back-compat strategy

`routes/paper.py` re-exports 45+ legacy symbols. External callers
need no changes:
- `routes/api_v1/agents.py` (4 functions: `start_report_task`,
  `start_translate_task`, `poll_report_task`, `poll_translate_task`)
- `tests/test_paper_migration.py` (14 private symbols, all 14 tests pass)

## Verification

- All 14 standalone migration tests pass
  (`python tests/test_paper_migration.py`).
- All 10 translate migration tests still pass (no regression).
- `tests/test_frontend_api_isolation.py` 4/4, `test_json_store.py`,
  `test_request_parser.py` all pass (95/96 — same pre-existing
  `test_api_bad_request` flake from test ordering).
- 22 endpoints registered when Blueprint is mounted on a Quart app.
- `lib.paper._report_runtime is routes.paper._report_runtime` — True.
- `lib.paper._translate_runtime is routes.paper._translate_runtime` — True.

## Pattern divergences from translate / daily_report

- **Two TaskRuntimes in one file** (paper has BOTH report and translate
  workflows with separate dedup indexes). Translate had one. Daily-report
  had its own simpler `_active_jobs` dict. Splitting into
  `report_runtime.py` + `translate_runtime.py` keeps each cleanly bounded.
- **No DB-CAS commit layer needed** — paper writes go to dedicated tables
  (`paper_reports`, `paper_translations`, `paper_library`) keyed by
  content-addressable hashes, not concurrent message arrays. Standard
  `INSERT OR REPLACE` is race-safe enough.
- **Image injection is its own module** — biggest non-trivial pure-Python
  block (392 LOC), worth its own home in `images.py` so future image work
  doesn't churn engine code.
- **Prompts are huge** (387 LOC) — kept in their own module so prompt
  iteration doesn't show up as engine churn in diffs.
- **CSS / KaTeX / base64 embedding stays in routes** — that's response
  shaping, not engine logic.

## Backend hot-files: ALL DONE

| File | Before | After (route) | lib/ package | Tests |
|---|---|---|---|---|
| `routes/translate.py` | 1590 | 335 | 10 modules / 1839 LOC | 10/10 |
| `routes/daily_report.py` | 2405 | 705 | 9 modules / 2020 LOC | manual |
| `routes/paper.py` | 3089 | 1520 | 12 modules / 1898 LOC | 14/14 |

The "thin route + lib package" pattern is now applied consistently across
all three giant route files.

