---
name: log-noise-reduction-2026-05-05
description: Routing pattern for log-noise audit: route to vendor.log / INFO level, never silence
enabled: true
tags: [logging, noise-reduction, vendor.log, routing]
created: 2026-05-05T12:56:56Z
updated: 2026-05-05T12:56:56Z
---

# Log-Noise Reduction Audit (2026-05-05)

Reduced error.log from ~6200 lines/day → target ≤ 1200 via **routing**,
never silencing. Every suppressed event still surfaces somewhere
(app.log INFO / vendor.log / audit.log). Seven classes of fixes:

## 1. Memory-prefetch + PREFIX MUTATION false positive
`lib/memory/prefetch.py::inject_relevant_memories` mutates the last
user message — this triggered `[CacheTrack] PREFIX MUTATION DETECTED`
in the next round. Fix: pass `conv_id` through, call
`lib.tasks_pkg.cache_tracking.notify_compaction(conv_id)` after
mutation.

## 2. Tolerant rerank JSON parser
`_parse_rerank_response` must accept preamble + fences:
```python
cleaned = re.sub(r'```(?:json)?\s*', '', text)
cleaned = re.sub(r'\s*```', '', cleaned).strip()
# Fallback: scan for first BALANCED {...} with string-awareness
```
Only warn when BOTH paths fail.

## 3. UnknownWorkspaceRootError — dedupe 4× log
Added `UnknownWorkspaceRootError(ValueError)` in
`lib/project_mod/config.py`. Raise site logs ONCE at WARNING;
`executor.py` / `streaming_tool_executor.py` / `tool_dispatch.py`
check `isinstance(e, UnknownWorkspaceRootError)` and log at INFO
(recoverable, LLM-facing error).

## 4. Per-cycle 429 → INFO
`lib/llm_dispatch/api.py`: per-cycle 429 is routine backpressure
(dispatcher rotates to next key). Keep WARNING only for
final exhaustion. Audited via `audit_log('config_change', …)`
one-shot at module level.

## 5. Benign 409 lifecycle demotion
`server.py::_log_response` + `routes/conversations.py::save_conv`:
the regression guards (`blocked_msg_regression`,
`blocked_empty_overwrite`, `blocked_stale_checkpoint`) are the
GUARD firing correctly, not errors. Server inspects response body
JSON via `_is_benign_409()` to decide INFO vs WARNING.

## 6. LATE-done benign extension
`routes/chat.py`: `_is_benign = _late_fr in ('aborted', 'interrupted', 'stop')`
— `'stop'` is the normal finishReason when a task completes
between the queue poll and status check.

## 7. git-shim slow-FUSE
- `_git_timeout(default=180)` for `add -A` / `write-tree` write paths.
- `_write_tree` retries ONCE after stale-lock self-heal if stderr
  contains `index.lock`.
- Audited via one-shot `audit_log('config_change', …)`.

## 8. Vendor filter on error.log
`server.py`: added `_BizAndWerkzeugOnly` filter on `_error_handler`
so trafilatura/urllib3 records do NOT duplicate into error.log —
they still appear in vendor.log (CLAUDE.md §9).

## Key principle
**Route, don't silence.** Every WARNING we demote must still appear
at INFO (app.log) or vendor.log. Every "silenced" vendor event is
still in vendor.log. CLAUDE.md §10 config-surface changes are
audited via `audit_log('config_change', …)` at the change site.

