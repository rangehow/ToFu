---
name: Claude-Code system-prompt port (2026-05)
description: Ported Claude Code's full static system-prompt section list into lib/tasks_pkg/system_prompt_cc.py; single layout (no CHATUI_CC_SYSPROMPT kill switch), CLAUDE.md injected as _isMeta user msg, compaction reinject keys off _CC_STATIC_MARKER
enabled: true
tags: [system-prompt, claude-code, prompt-cache, compaction, cc-alignment]
created: 2026-05-07T16:04:19Z
updated: 2026-05-07T16:04:19Z
---

# Claude-Code System Prompt Port — 2026-05-07

## Summary
Ported Claude Code's full static system-prompt section list into chatui to
close the SWE-bench gap.  Main module: `lib/tasks_pkg/system_prompt_cc.py`.

## Layout (no kill switch — single layout as of 2026-05-07)

**Only layout: Claude-Code-style.**
- System message: `intro / # System / # Doing tasks / # Executing actions
  with care / # Using your tools / # Tone and style / # Output efficiency /
  # Function Result Clearing / SUMMARIZE / system-reminder note /
  # Environment / Notes: / Current date` assembled by
  `system_prompt_cc.build_static_prompt()` as ONE cache-stable text block.
  A separate second block carries the dynamic memory count +
  `<memory_accumulation>` reminder so memory CRUD doesn't invalidate the
  static prefix's BP.
- CLAUDE.md / project-intelligence → prepended user msg with `_isMeta: True`
  wrapped in `<system-reminder>...</system-reminder>` (matches Claude Code's
  `prependUserContext` in `utils/api.ts:449`). Contains `[PROJECT CO-PILOT
  MODE]` marker for idempotency.

### Why a single layout
The original port had a `CHATUI_CC_SYSPROMPT` env-var kill switch to
fallback to a legacy Layout B (CLAUDE.md prepended into system msg).
It was REMOVED on 2026-05-07 because:
  1. An empty-string env value (`export CHATUI_CC_SYSPROMPT=` with no RHS,
     easy to do in shell init or .env files) silently flipped production
     to Layout B for weeks without anyone noticing — the debug panel
     showed CLAUDE.md in the system message instead of a user msg.
  2. `os.environ.get(key, default)` returns the ACTUAL value if the key
     exists — even empty string — and only returns the default when the
     key is absent. The `is_enabled()` check had `'' in ('0','false',...)`
     which evaluated to True, flipping the layout.
  3. The kill switch was never intentionally used; it existed only as a
     hot-rollback path that outlived its purpose.

Dead code removed: `_FUNCTION_RESULT_CLEARING_SECTION`,
`_SUMMARIZE_TOOL_RESULTS_SECTION`, `_TOOL_USAGE_GUIDANCE`,
`_OUTPUT_EFFICIENCY_GUIDANCE` constants + `_prepend_to_system_message`
helper + `is_enabled()` + Layout B branch in `_inject_system_contexts`.

### Compaction re-inject trigger fix
`compaction._reinject_system_contexts_after_compact` used
`'[PROJECT CO-PILOT MODE]' not in sys_text` as the "rebuild needed"
trigger. Under Layout A that marker is NEVER in system text (it's in the
user _isMeta msg), so the trigger fired after every compaction. Fixed
by switching to `_CC_STATIC_MARKER` (the "IMPORTANT: You must NEVER
generate or guess URLs" string from the CC intro section), which DOES
live in system text.

## How CLAUDE.md is handled in Claude Code (multi-turn)
1. `getUserContext()` (`src/context.ts:142`) builds `{claudeMd, currentDate}` once,
   cached for the conversation lifetime via `memoize`.
2. `prependUserContext(messages, userContext)` (`src/utils/api.ts:449`) wraps it
   in `<system-reminder>…</system-reminder>` and inserts it as `isMeta: true`
   user message at index 0 EVERY TURN inside `query.ts:660`'s loop.
3. Cache: this user-message lives under BP4 (tail), not the system BPs. Since
   content rarely changes, tail cache survives across turns. A/B confirmed
   on chatui: -18% cost / +49% cache hit vs system-msg placement
   (`.chatui/skills/claudemd-placement-ab-test-results.md`).

## Files
- `lib/tasks_pkg/system_prompt_cc.py`  (main, no `is_enabled()` any more)
- `lib/tasks_pkg/system_context.py`    (single-layout `_inject_system_contexts`,
                                        `_CC_STATIC_MARKER` exported)
- `lib/tasks_pkg/compaction.py`        (compact-reinject uses `_CC_STATIC_MARKER`)
- `lib/tasks_pkg/orchestrator.py`      (passes `model=` to inject)
- `lib/project_mod/indexer.py`         (docstring pointer updated to
                                        `system_prompt_cc.section_using_tools`)
- `tests/test_cc_alignment.py`         (sections imported from system_prompt_cc)
- `tests/test_compaction_improvements.py` (uses `_CC_STATIC_MARKER`)
- `tests/test_streaming_and_prefetch.py`  (checks all messages, not just sys)

## Tool-name substitutions vs Claude Code
- Read → read_files
- Edit → apply_diff / insert_content
- Write → write_file
- Glob → find_files
- Grep → grep_search
- Bash → run_command

## Validation
- `_isMeta` flag is auto-stripped by `_strip_non_api_fields()` in
  `lib/llm_client.py` (any non-`_API_MESSAGE_FIELDS` key strips).
- Idempotency: marker `IMPORTANT: You must NEVER generate or guess URLs`
  prevents endpoint-mode re-injection of the static block; the
  `[PROJECT CO-PILOT MODE]` marker prevents duplicate CLAUDE.md.
- Unit tests: 182 green (test_cc_alignment + test_compaction_improvements
  + test_streaming_and_prefetch).

## Unported (chatui has no equivalent)
- `# Tone` ant-only `/issue` `/share` slash commands
- TodoWrite / TaskCreate references
- AskUserQuestion (chatui uses `ask_user` via human_guidance — doesn't expose
  the trained tool name, so left out of the prompt)
- Cyber-risk instruction (Anthropic-internal text)
- `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` global-scope cache marker
  (Anthropic-internal feature, single-org cache is fine for us)
