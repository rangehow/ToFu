---
name: web-search-batch-display-shows-all-candidates
description: Batch web_search display: all candidate queries shown (one per line); results tagged with `_q` and grouped under per-query subheaders in UI; vertical results render in separate purple card
enabled: true
tags: [ui, tool-display, web_search, fetch_url, batch-mode]
created: 2026-04-20T09:25:58Z
updated: 2026-06-04T06:11:38Z
---

# Batch web_search / fetch_url display: candidates + per-query attribution

**Files**: `lib/tasks_pkg/tool_display.py`, `lib/tasks_pkg/handlers/search.py`,
`lib/tasks_pkg/streaming_tool_executor.py`, `static/js/ui/tool_rounds.js`,
`static/styles.css`

## Candidate-term display (tool_start line)
`_tool_display_web_search` / `_tool_display_fetch_url` batch mode renders
EVERY candidate query/URL on its own line as `• <full text>` (no
truncation). Frontend `_renderUnifiedToolLine` turns `\n → <br>`.
`_batchQueries` / `_batchUrls` also exposed on round + tool_start event.
Persisted-read line (`_persisted_read_labels`) likewise renders one
`• label` per line, no `+N more` elision.

## Per-query result attribution (2026-06)
Batch web_search flattens all queries' results into ONE `results` list.
To let users see which result came from which query:
- Backend tags each display result dict with `dr['_q'] = <source query>`
  in BOTH paths:
  - serial: `_handle_web_search_batch` in handlers/search.py
  - streaming prefetch: `_execute_one` batch branch in
    streaming_tool_executor.py (the `results_per_q` loop)
- `_q` rides through the dedup cache unchanged (`_finalize_tool_round`
  passes `results` verbatim; cache-hit branch in tool_dispatch.py replays
  `cached_display`).
- Frontend `tool_rounds.js` results branch groups by `r._q`: when >1
  distinct non-empty `_q` present, renders `.search-query-group` blocks
  each with a `.search-query-group-header` (🔍 + query text + count).
  Single-query / untagged → flat list (unchanged).
- CSS: `.search-query-group-header/-q/-count` amber, light-theme overrides.

## Vertical results
Already visually distinct: render in a separate purple `.vertical-card`
BEFORE the web items inside `.ptool-results-content`. Header badge now
reads `vertical: <domain>` (was bare domain). See
vertical-search-domain-parameter-and-card memory.

## Guardrail
Any new web_search/fetch_url per-result field must be set in BOTH the
serial handler AND the streaming `_execute_one` batch branch, or it's
silently dropped on the common (streaming pre-exec) path. Tests:
`tests/test_tool_changes.py` + `tests/test_streaming_and_prefetch.py` (62).
