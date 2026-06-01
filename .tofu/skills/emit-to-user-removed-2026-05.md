---
name: emit-to-user-removed-2026-05
description: emit_to_user tool was removed 2026-05-11; do not reintroduce. content_ref on write_file remains
enabled: true
tags: [tools, removed, history]
created: 2026-05-11T04:30:38Z
updated: 2026-05-11T04:30:38Z
---

# emit_to_user has been REMOVED (2026-05-11)

The `emit_to_user` terminal tool was removed because it was rarely useful
in practice (the model would emit an empty comment and the inline tool
result block was redundant with the tool round already shown above).

## Files affected

- DELETED: `lib/tools/emit.py`
- `lib/tools/__init__.py` — removed emit imports + facade entry
- `lib/tasks_pkg/model_config.py` — removed `EMIT_TO_USER_TOOL` import + append
- `lib/tasks_pkg/handlers/misc.py` — removed `_handle_emit_to_user` + import
- `lib/tasks_pkg/orchestrator.py` — removed Phase 4a emit detection block
- `lib/tools/deferral.py` — removed from `CORE_TOOL_NAMES` + `_NEVER_DEFER`
- `lib/tasks_pkg/tool_dispatch.py` — removed label entry
- `lib/tasks_pkg/tool_display.py` — removed `_tool_display_emit_to_user`
- `lib/tasks_pkg/manager.py` — removed `_emitContent` / `_emitToolName` persistence
- `routes/chat.py` — removed emit metadata in poll response
- `static/js/ui.js` — removed `_emitContent` rendering + `emit_ref` SSE handler
- `static/js/main.js` — removed poll-response recovery code
- `static/js/branch.js` — removed `emit_ref` handler
- `static/styles.css` — removed `.emit-content-block/-label/-output` rules
- `tests/test_cc_alignment.py` — removed emit_to_user assertions
- `CLAUDE.md`, `README.md`, `README_CN.md`, `docs/ARCHITECTURE.md`,
  `docs/agentic-development-experience.md`, `docs/architecture_harness.html`
  — references removed/updated

## Intentionally NOT touched

- `debug/test_cache_*.py` — frozen fixtures for prompt-cache benchmarks;
  modifying them would invalidate historical cache-hit numbers.
- `docs/refactor_decomposition_proposal.md` — planning doc that references
  a hypothetical `_emit_ref.py` file; left as historical record.
- `lib/tasks_pkg/orchestrator.py::_emit_tool_round_phase` — UNRELATED helper
  that emits SSE events at the start of a tool round. Despite the name
  match, do NOT remove this.

## What remains

- `content_ref` on `write_file` — the OTHER token-saving mechanism — is
  intact. It writes a previous tool result's content to a file without
  the model re-generating it. Lives in
  `lib/tasks_pkg/executor.py::_resolve_content_ref` and is still
  documented in CLAUDE.md §4.5.

