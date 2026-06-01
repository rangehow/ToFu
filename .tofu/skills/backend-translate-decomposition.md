---
name: backend-translate-decomposition
description: routes/translate.py decomposed: thin route layer (336 LOC) + lib/translate/ package (10 modules)
enabled: true
tags: [refactor, backend, routes, translate, convention]
created: 2026-05-28T05:53:30Z
updated: 2026-05-28T05:53:30Z
---

# `routes/translate.py` Decomposition (2026-05-28)

Pattern reference for the next backend hot-file decompositions
(`routes/daily_report.py`, `routes/paper.py`, etc.).

## Before

Single 1590-LOC `routes/translate.py` mixing:
- Blueprint + 6 route handlers
- Prompt building, notranslate block handling
- Chunking + dedup
- 280-line LLM/MT retry engine
- TaskRuntime + async worker
- DB commit (per-conv lock + CAS retry)
- PPTX file translation worker

22 callers (tests, lib.message_queue, lib.tasks_pkg.manager,
routes.chat, routes.api_v1.agents, debug/, routes.translate_mt_test)
import private symbols (`_translate_runtime`, `_translate_tasks`,
`_translate_tasks_lock`, `_do_translate`, `_format_status_message`,
`_strip_notranslate_tags`, `_build_translate_prompt`,
`_translate_one_chunk`, `_commit_translation_inner`,
`_cleanup_translate_tasks`) directly.

## After

```
routes/translate.py           336 LOC — Blueprint + route handlers ONLY
                                       + back-compat re-exports for the 22 callers
lib/translate/
  __init__.py                  99 LOC — package facade re-exporting public surface
  constants.py                 16 LOC — DEFAULT_USER_ID, _CHUNK_THRESHOLD, etc.
  prompt.py                    35 LOC — _build_translate_prompt + tag wrappers
  notranslate.py              110 LOC — <notranslate>/<nt> block extract/reattach
  chunking.py                  38 LOC — _split_text_for_translation
  dedup.py                    197 LOC — _dedup_repetition_loop, _dedup_inline_loop
  status.py                    38 LOC — _format_status_message
  engine.py                   425 LOC — _translate_one_chunk (cache→MT→LLM retry loop)
  runtime.py                  225 LOC — TaskRuntime + _do_translate worker
  commit.py                   192 LOC — _commit_translation_to_db (per-conv lock + CAS)
  pptx.py                     129 LOC — _do_translate_pptx + upload-dir helpers
```

Total LOC roughly preserved (1839 vs 1590) — increase comes from
docstrings on each new submodule and the package facade.

## Back-compat strategy

`routes/translate.py` re-exports every legacy private symbol from
`lib.translate`. Existing callers (22 of them) need no changes —
`from routes.translate import _do_translate` continues to work.

New code should import from the package facade:
`from lib.translate import _do_translate`.

## Verification

- All 10 standalone migration tests pass (`python tests/test_translate_migration.py`).
- `tests/test_frontend_api_isolation.py` 4/4 still pass.
- `tests/test_api_response.py` + `tests/test_request_parser.py` +
  `tests/test_json_store.py` 86/86 pass.
- Cross-module imports verified for `lib.message_queue`,
  `lib.tasks_pkg.manager`, `routes.api_v1.agents`, `routes.chat`,
  `routes.translate_mt_test`.

## Pattern to apply to other route hot-files

1. **Identify the pure parts first** (prompts, regexes, helper math) —
   extract to a tiny submodule (`prompt.py`, `dedup.py`, `chunking.py`).
   No callers need to know.
2. **Engine = the most logic-heavy function** — its own submodule
   (`engine.py`). Imports from the pure parts.
3. **Runtime = the async TaskRuntime + worker** — owns the registry
   shims (`_xxx_tasks`, `_xxx_tasks_lock`) so the worker code is
   self-contained.
4. **Commit / DB writes = race-safe layer** — per-conv lock + CAS
   retry, factored out so unit tests can target it directly.
5. **Side-effect-bearing route handlers stay** in the routes file —
   they call into `lib.<x>.*`. Keep route handlers ≤ 50 LOC each
   where possible.
6. **Re-export the legacy private surface** at the top of the routes
   file so the 20+ external callers don't break in one PR.
   Migration of those callers to `from lib.<x> import …` is a
   follow-up sweep.

## Anti-patterns avoided

- ❌ Did NOT split into `views.py` / `services.py` / `repositories.py`
  layered abstractions — overkill for a chatui-sized project.
- ❌ Did NOT change any function signatures or return shapes.
- ❌ Did NOT introduce DI containers or factory classes.
- ❌ Did NOT migrate callers in this PR — that's its own diff.

