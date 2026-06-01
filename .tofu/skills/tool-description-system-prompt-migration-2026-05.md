---
name: tool-description-system-prompt-migration-2026-05
description: Migrated per-tool usage prose FROM system prompt (indexer tools_section + _TOOL_USAGE_GUIDANCE) INTO each tool's API description field. Net: -4.5KB system cache prefix, +richer tool docs, no dup
enabled: true
tags: ["prompt-engineering", "tool-schemas", "system-prompt", "cache", "claude-code-alignment"]
created: 2026-05-07T06:56:18Z
updated: 2026-05-07T06:56:18Z
---

# Tool-Description ↔ System-Prompt Migration (2026-05-07)

## Motivation
CC's architecture: per-tool `description` field is rich (1-21 KB each);
cross-cutting tool policy lives in a separate `getUsingYourToolsSection`
(~1 KB). We had the worst of both worlds: thin schema descriptions +
3-4 KB of duplicated per-tool prose dumped into the system prompt by
`lib/project_mod/indexer.py::tools_section` AND a verbose
`_TOOL_USAGE_GUIDANCE` with per-tool bullets. Cache-weighty system
prefix, conflicting signal source, hard to evolve.

## What moved

| Item | From (system prompt) | To (tool schema) |
|---|---|---|
| list_dir signature + "use before reading" | indexer.tools_section | list_dir.description |
| grep_search "5x faster than grep, batch mode" | tools_section + _TOOL_USAGE_GUIDANCE | grep_search.description |
| find_files "auto-filters ignored dirs" | tools_section + _TOOL_USAGE_GUIDANCE | find_files.description |
| write_file vs apply_diff vs insert_content routing | tools_section + _TOOL_USAGE_GUIDANCE | each tool's description |
| apply_diff batching ("edits[]") | tools_section | apply_diff.description |
| insert_content "prefer when additive" + anchor semantics | tools_section + _TOOL_USAGE_GUIDANCE | insert_content.description |
| run_command WHEN-TO-USE matrix (don't use for cat/grep/find) | tools_section | run_command.description (full rewrite) |
| read_files "read WIDE, not narrow" | tools_section | read_files.description |
| read_files batch-reads + absolute-paths | tools_section | read_files.description |
| web_search summary-first strategy | _TOOL_USAGE_GUIDANCE | web_search.description |
| emit_to_user terminal semantics | tools_section | (already in emit_to_user.description, dup removed) |

## What stayed in system prompt (legitimately cross-cutting)
- `_TOOL_USAGE_GUIDANCE` now ~1.4 KB (was ~3.1 KB), containing only:
  - "Do not propose changes to code you haven't read"
  - "Call multiple tools in parallel when independent"
  - Cross-tool routing meta: prefer dedicated tools over shell (mirrors CC's `getUsingYourToolsSection`)
  - "Diagnose before switching tactics"
  - "Each tool's description is authoritative for args & batching"
- `_FUNCTION_RESULT_CLEARING_SECTION`, `_SUMMARIZE_TOOL_RESULTS_SECTION`, `_OUTPUT_EFFICIENCY_GUIDANCE` — unchanged (already cross-cutting)
- Project header, multi-root topology, CLAUDE.md auto-detection — unchanged in `indexer.py`

## Size delta
| Layer | Before | After |
|---|---:|---:|
| System project context | ~3900c (incl. tools_section dump) | 183c (header only when no extras) |
| System static guidance | ~3100c | 2337c |
| **Total system prefix** | **~7000c** | **~2520c (-4.5 KB)** |
| Tool descriptions (tools[]) | thin (~3-6 KB) | 10201c (richer) |

## Files changed
- `lib/tools/project.py` — enriched all 10 project-tool descriptions
- `lib/tools/search.py` — enriched web_search with strategy block
- `lib/project_mod/indexer.py` — deleted ~3 KB `tools_section`; docstring updated
- `lib/tasks_pkg/system_context.py` — `_TOOL_USAGE_GUIDANCE` trimmed to cross-cutting meta

## Verification
- `debug/test_project_state_race.py` — all tests still pass
- Dedupe audit: 0 of 7 migrated markers remain in system prompt
- Both servers healthy (must restart to load new lib/)

## Expected impact
1. **Cache prefix shrinks ~4.5 KB** → smaller BP1/BP2 segments, faster
   R1 cache write, better inter-conversation hit rate
2. **Richer tool descriptions** → model has authoritative per-tool info
   near each tool's schema, not floating in system
3. **Single source of truth per tool** → editing a tool's usage is now
   one place (its description) not two
4. **Closer to CC architecture** (`claude-code-context-construction-alignment`
   memo): per-tool `prompt()` method + thin `getUsingYourToolsSection`

## Follow-up ideas (not yet done)
- Add CC's `getSimpleDoingTasksSection` code-style bullets to system
  prompt (the "minimal edit, don't add error handling, don't invent
  abstractions" block from `claude-code/src/constants/prompts.ts`)
- Further enrich bash-like tools with worked examples (CC's BashTool is
  21 KB; our run_command is now 1.3 KB — there's headroom)
- Per-tool cache-breakpoint within tools[] array (Anthropic supports it)

## Rollback
All changes are in git. To revert:
  project_history | grep 'tool-description migration'
  project_diff --from-ref <sha> --to-ref HEAD

