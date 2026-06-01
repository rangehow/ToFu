---
name: tool-deferral-subsystem-removed-2026-05
description: Removed lib/tools/deferral.py + tool_search pseudo-tool; frontend toggles already gate per-feature inclusion
enabled: true
tags: [architecture, tools, removal, claude-code-alignment]
created: 2026-05-17T02:25:42Z
updated: 2026-05-17T02:25:42Z
---

# Tool Deferral Subsystem — Removed (2026-05)

## What was removed
- `lib/tools/deferral.py` (whole module: `partition_tools`,
  `search_deferred_tools`, `format_search_results`, `_estimate_tool_tokens`,
  `DEFERRED_TOOL_HINTS`, `CORE_TOOL_NAMES`, `TOOL_SEARCH_TOOL`).
- `_handle_tool_search` handler in `lib/tasks_pkg/handlers/search.py`.
- `_tool_display_tool_search` + `table['tool_search']` registration in
  `lib/tasks_pkg/tool_display.py`.
- `_get_tool_discovery_delta` branch + `discovered_tools` state in
  `lib/tasks_pkg/attachments.py`.
- `task['_deferred_tools']` / `task['_discovered_tool_names']` plumbing in
  `lib/tasks_pkg/orchestrator.py` and `_assemble_tool_list` 4-tuple return.
- Test classes `TestToolDeferral` / `TestToolSearchHandler` /
  `TestDynamicDeferral` and the `'tool_search'` entry in
  `tests/test_refactored_utils.py::test_tool_registry_has_expected_tools`.
- Doc references in `CLAUDE.md`, `docs/ARCHITECTURE.md`,
  `docs/CLAUDE_CODE_ALIGNMENT.md`.

## Why
- ChatUI's frontend toggles already gate browser / desktop / scheduler /
  image-gen / swarm / MCP per-conversation. Phase 1 static deferral was
  already disabled by policy (user-selected tools must NEVER be silently
  deferred).
- Phase 2 dynamic deferral could only fire on MCP tool-blowup, but ran
  with a hardcoded 200k context default and a `_NEVER_DEFER` set that
  duplicated `CORE_TOOL_NAMES` with drift.
- The hint-table pattern was a "side dictionary" — keyed by tool name but
  living separately from the tool definition, so renames silently
  orphaned hints.
- Without API-level support (Anthropic's `tool_reference` blocks +
  `defer_loading: true`), discovered schemas had to be re-sent on every
  subsequent request anyway, eroding the tokens-saved argument.

## Where MCP-specific bounding belongs (NOT done in this removal)
If MCP tool-list explosion ever needs a runtime bound, add it inside
`lib/mcp/` directly — e.g. an `MCP_MAX_TOOL_TOGGLE_TOKENS` cap in
`lib.mcp.bridge.get_openai_tool_defs`, with a UI warning in
`Settings → MCP`. Don't bring back a cross-cutting framework for a
problem that lives in one package.

## Return-value change (consumers)
`_assemble_tool_list` now returns `(tool_list, has_real_tools, max_tool_rounds)`
— a 3-tuple, was 4-tuple. The `deferred_tools` slot is gone.

## Memory cross-reference
The earlier `claude-code-copilot-full-alignment` memory listed
"tool deferral" as one of 15 alignment items — that line is now
obsolete and should be ignored.

