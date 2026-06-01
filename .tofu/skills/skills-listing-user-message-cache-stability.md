---
name: skills-listing-user-message-cache-stability
description: Skills listing moved from system message to user message for prompt cache stability; BM25 relevance filtering reduces 100+ skills to top-30 per turn
enabled: true
tags: [architecture, performance, caching, skills]
created: 2026-04-02T16:58:20Z
updated: 2026-04-02T16:58:20Z
---

# Skills Listing: User Message Injection + BM25 Filtering

## Architecture After Refactor

The skills system uses a **split injection** pattern:

1. **System message** (static, cache-stable):
   - `SKILL_ACCUMULATION_INSTRUCTIONS_COMPACT` (~416 chars) — HOW to use skill tools
   - Injected by `_inject_system_contexts()` in `system_context.py`

2. **User message** (dynamic, per-turn):
   - `<available_skills>` XML index — WHICH skills exist
   - Injected by `inject_skills_to_user()` in `system_context.py`
   - Called LAST in orchestrator main loop (after attachments, search addendum)
   - Uses BM25 relevance filtering (top-30 skills based on user message text)

## System Message Block Ordering (cache-optimized)

```
[User system prompt]     — rarely changes
[Project CLAUDE.md]      — changes on file edits (prepended)
[Static guidance]        — NEVER changes (separate block for cache breakpoint)
[Compact skill instrs]   — NEVER changes (~416 chars)
[Swarm prompt]           — static when enabled
[Session memory]         — most dynamic, last
```

## Key Files
- `lib/skills/relevance.py` — BM25 scorer (no external deps)
- `lib/skills/injection.py` — `build_skills_context(query=...)`, `SKILL_ACCUMULATION_INSTRUCTIONS_COMPACT`
- `lib/tasks_pkg/system_context.py` — `inject_skills_to_user()`, reordered `_inject_system_contexts()`
- `lib/tasks_pkg/orchestrator.py` — Wires `inject_skills_to_user()` after search addendum

## Why This Matters for Cache
- Skill CRUD (create/update/delete) no longer invalidates the prompt cache
- The system message only changes when: CLAUDE.md changes, project tree changes, or session memory changes
- ~2.4K chars saved per turn in system message (2810→416 chars for skill instructions)
- ~75% fewer skills listed per turn when BM25 filtering kicks in (127→30)

