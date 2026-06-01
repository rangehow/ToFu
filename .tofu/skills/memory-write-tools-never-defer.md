---
name: memory-write-tools-never-defer
description: create_memory + merge_memories must be in _NEVER_DEFER (not just CORE_TOOL_NAMES) so token-pressure Phase-2 deferral can't demote them
enabled: true
tags: ["tool-deferral", "memory-tools", "architecture", "bug-fix"]
created: 2026-05-07T10:12:29Z
updated: 2026-05-07T10:12:29Z
---

# Memory write tools are in _NEVER_DEFER (not just CORE_TOOL_NAMES)

## Background

`lib/tools/deferral.py` has three relevant concepts:

1. `CORE_TOOL_NAMES` — **informational only**, not used for enforcement.
   Listing a tool here does nothing on its own.
2. `DEFERRED_TOOL_HINTS` — keyword hints for `tool_search` discovery. A
   tool in this dict CAN be deferred (either by Phase 2 dynamic logic
   or manually) and will need `tool_search` to resurface it.
3. `_NEVER_DEFER` frozenset (inside `partition_tools()`) — the ONLY
   mechanism that actually protects a tool from Phase 2 token-pressure
   deferral.

## Why this matters for memory tools

Before 2026-05-07 fix: `create_memory` and `merge_memories` were in
`DEFERRED_TOOL_HINTS` but NOT in `_NEVER_DEFER`. Under token pressure
the dynamic threshold logic demoted them. Symptom: the model would
either (a) fall back to `write_file` against `.chatui/skills/<slug>.md`,
or (b) call `tool_search("create memory")` before every save —
extra round-trip that the model often skipped.

## Fix (2026-05-07)

- Added `create_memory` + `merge_memories` to `_NEVER_DEFER` frozenset
  inside `partition_tools()` in `lib/tools/deferral.py`.
- Removed them from `DEFERRED_TOOL_HINTS` (no longer deferrable, so no
  need for search-hint keywords).
- `update_memory`, `delete_memory`, `search_memories` remain in
  `DEFERRED_TOOL_HINTS` — they're more rarely needed and can be
  surfaced via `tool_search` when required.

## Takeaway for future edits

If you want a tool to be truly always-on, putting it in
`CORE_TOOL_NAMES` is NOT enough. Must add it to `_NEVER_DEFER`
inside `partition_tools()` too.

