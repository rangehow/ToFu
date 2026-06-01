---
name: batch-tool-split-persist-audit
description: Audit of all batch tools for oversized-result visibility; fetch_url + find_files now split-persist per-item
enabled: true
tags: [compaction, persistence, fetch_url, batch-mode, split-persist]
created: 2026-04-29T13:44:27Z
updated: 2026-04-29T13:48:26Z
---

# Batch Tool Split-Persist — Audit & Fixes

## Problem pattern
When a batch tool (e.g. `fetch_url(urls=[...])`, `find_files(searches=[...])`)
concatenates per-item sections and the total exceeds `TOOL_RESULT_MAX_CHARS`,
`_persist_to_disk` in `lib/tasks_pkg/compaction.py` dumps everything to one
file and the 2000-char preview only shows item #1 — items #2..N become
invisible to the LLM, which can't know which file to `read_files`.

## Audit of batch tools

| Tool | Batch param | Budget | Multi-item? | Split-persist? |
|---|---|---|---|---|
| `web_search` | `queries[]` | 30K | yes | ✅ `_persist_web_search_split` |
| `fetch_url` | `urls[]` | 50K | yes | ✅ `_persist_fetch_url_split` (added) |
| `grep_search` | `searches[]` | 30K | yes | ✅ `_persist_grep_search_split` (works by-file) |
| `find_files` | `searches[]` | 20K | yes | ✅ `_persist_find_files_split` (added) |
| `read_files` | `reads[]` | exempt | n/a | n/a (never truncated) |
| `apply_diff` | `edits[]` | 60K default | no (short confirms) | n/a |
| `insert_content` | `edits[]` | 60K default | no (short confirms) | n/a |

## Section header regexes used
- web_search: `^\[N\]` + `════════════════════` separator
- fetch_url:  `^Content from <url> \(N chars\):` or `^Failed to fetch <url>\.`
- grep_search: rg/grep line format `filepath:num:content`
- find_files: `^Files matching "<pattern>"(?: in <path>)? \(N found\):`

## Invariant for future batch tools
Any new batch tool that:
1. concatenates per-item output with a parseable section delimiter, AND
2. is subject to `budget_tool_result` (not in `_BUDGET_EXEMPT_TOOLS`)

MUST implement `_persist_<tool>_split(content, persist_dir, safe_id)` and
wire it in `_persist_to_disk` (right after the other split branches),
otherwise oversized results lose visibility of items 2..N.

Fallback contract: return `None` when fewer than 2 sections detected so
single-item calls use default single-file persist unchanged.

## Tests
`tests/test_compaction_improvements.py` — 5 tests for fetch_url split
(+fallthrough +mixed OK/FAILED) and find_files split (+fallthrough).
Full suite: 104 tests passing.

