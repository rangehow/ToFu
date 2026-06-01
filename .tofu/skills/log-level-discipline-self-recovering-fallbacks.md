---
name: log-level-discipline-self-recovering-fallbacks
description: Self-recovering fallbacks should log at debug, not warning — warning/error routes to error.log (noise)
enabled: true
tags: [logging, convention, log-level, error.log, noise-reduction]
created: 2026-04-24T05:51:21Z
updated: 2026-04-24T05:51:21Z
---

# Log-Level Discipline: Self-Recovering Fallbacks

## Rule

Per CLAUDE.md §2.2: if an exception is **expected, self-recovering, and the system continues normally**, log at `debug`, NOT `warning`.

Warning/error levels are routed to `logs/error.log` via the error handler. Using them for routine fallbacks creates noise that drowns out actual problems needing attention.

## User preference (explicit)

> "If it doesn't need to be handled and doesn't affect anything, should we still set it to error level? Only set it to error if it needs my attention to fix."

## Examples where debug is correct

- **`lib/fetch/playwright_pool.py::_do_fetch` outer except** — Playwright `Target crashed` / render timeouts. Caller treats None as "fallback failed" and uses HTTP/trafilatura. Per-page, routine.
- **`lib/tasks_pkg/tool_dispatch.py` successful `_repair_json`** — LLM streamed slightly-malformed JSON, `_repair_json` recovered it, tool proceeds. The **unrecovered** branch below keeps warning because the tool call actually fails and returns error to the model.

## Counter-examples where warning/error IS correct

- Retry loop final failure (all retries exhausted)
- Unexpected degraded behavior that blocks the pipeline
- DB write failures (data loss risk)
- `_repair_json` itself failing — model will get an error back

## Decision heuristic

Ask: **"Does this log line demand my attention to fix something?"**
- Yes → warning/error with exc_info
- No (system recovered, fallback worked) → debug

