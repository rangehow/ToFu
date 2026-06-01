---
name: compaction-drop-marker-merge
description: Bug fix: _merge_drop_markers now just deletes drop markers and legacy Context Summaries instead of merging them into a summary message; also merges consecutive user messages after removal
enabled: true
tags: [python, compaction, context, bug-fix, drop-markers, message-alternation]
created: 2026-03-20T03:18:15Z
updated: 2026-03-20T11:28:03Z
---

# Compaction: _merge_drop_markers cleanup-only design

## History
- Original: merged drop markers into a `role: 'user'` Context Summary → caused consecutive user messages
- First fix: changed to `role: 'system'` + consecutive user merge
- Final fix: **removed Context Summary entirely** — drop markers are just deleted

## Current Behavior
`_merge_drop_markers()` does two things:
1. **Deletes** all `🗑️ [Dropped]` assistant messages AND any legacy `📝 [Context Summary]` messages (both `system` and `user` role variants)
2. **Merges consecutive user messages** that become adjacent after deletion (joins with `\n\n`)

## Rationale
The Context Summary was just a bullet list of old tool calls (e.g. `read_files(foo.py) → 200 lines`). 
This carried negligible value — the model doesn't benefit from knowing it read a file 40 rounds ago 
when the file content is no longer in context. Removing it saves tokens and eliminates the risk of 
role alternation violations.

## Key Constants
- `COMPACT_DROP_MARKER = '🗑️ [Dropped]'` — prefix for dropped round summaries
- `COMPACT_CONTEXT_MARKER = '📝 [Context Summary]'` — legacy, now only used for cleanup detection

