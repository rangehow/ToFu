---
name: claude-code-context-construction-alignment
description: Claude Code vs ChatUI context construction: system prompt as array-of-blocks with per-block cache_control, system-reminder tags, delta attachments via message-history scanning (vs compute-caching), multi-block cache segmentation"
enabled: true
tags: [claude-code, context, system-prompt, cache, system-reminder, delta-attachments, architecture]
created: 2026-04-01T04:29:18Z
updated: 2026-04-01T04:29:18Z
---

# Claude Code Context Construction — Architecture Alignment

## System Prompt: Array of Text Blocks

Claude Code: Anthropic `system` param = array of `TextBlockParam`, each with independent `cache_control`
Our system: Single system message, now split into multiple text blocks via `as_separate_block=True`

## `<system-reminder>` Tags

Claude Code wraps mid-conversation injections in `<system-reminder>` tags as user messages.
Our system wraps project/skills/swarm context via `_wrap_system_reminder()` in the system message.

## Delta Attachments — Architectural Mismatch

Claude Code: Long-lived in-memory array → scan history for prior deltas → only inject changes
Our system: Stateless per-task → must always inject → cache computation only (hash-based skip of FUSE I/O)

## Files Changed
- `lib/tasks_pkg/system_context.py` — `_wrap_system_reminder()`, `as_separate_block`, multi-block injection
- `lib/llm_client.py` — Per-block `cache_control` in `add_cache_breakpoints()`

