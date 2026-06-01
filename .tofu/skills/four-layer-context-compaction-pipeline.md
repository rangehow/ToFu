---
name: three-layer-context-compaction-pipeline
description: 3-layer progressive context compression: L1 micro-compaction (hot-tail tool results), L2 structural tiered truncation (thinking/args/screenshots/drop, NOT tool text), L3 LLM smart summary with path-only restoration — each layer has non-overlapping responsibilities
enabled: true
tags: [python, compaction, context-management, architecture, concurrent, llm, layer-separation]
created: 2026-03-23T06:55:12Z
updated: 2026-03-23T07:29:34Z
---

# Three-Layer Context Compaction Pipeline

## Architecture

```
Every LLM call:
  L1: micro_compact()          → compress cold tool results (hot tail of 4)
  L2: compact_messages_fast()  → compress assistant thinking/args, strip screenshots, drop ancient rounds
  L3: smart_summary_compact()  → LLM summarization when tokens > 82% of context limit
```

## Critical Design Rules

### Each layer has NON-OVERLAPPING responsibilities:

| Content Type | Handled By | NOT Handled By |
|---|---|---|
| Tool result text | **L1 only** (hot-tail) | L2 (would be redundant) |
| Assistant thinking | **L2 only** (Tier 1 keeps, Tier 2 deletes) | L1 |
| write_file/apply_diff args | **L2 only** (head+tail truncation) | L1 |
| Base64 screenshots | **L2 only** (L1 can't detect image_url blocks) | L1 |
| Ancient full rounds | **L2 Tier 3** (drop → one-line summary) | L1 |
| Global context overflow | **L3** (LLM structured summary) | L1, L2 |

### Why Layer 0 (tool output externalization at source) was removed:
- `read_files` is the model's ONLY way to read files — truncating it at source creates infinite retry loops
- Model reads file → gets truncated → reads again → truncated again → infinite loop

### Why Layer 2 does NOT truncate tool result text:
- By the time a round enters L2's scope (21+ rounds old), its tool results are already outside L1's hot tail (4 most recent) and have been compacted to ~150-char placeholders
- L2 truncating tool text would be redundant (operating on already-compacted placeholders)

### Why restoration injects file PATHS only (not content):
- File content injected as user messages is NEVER compactable by L1 (not role=tool) or L2 (not a tool round)
- Creates permanent un-compactable blobs that grow proportionally with each compression
- Model can call read_files itself — those results enter normal L1 lifecycle

## Key Files
- `lib/tasks_pkg/compaction.py` — all 3 layers + pipeline orchestration
- `lib/tasks_pkg/orchestrator.py` — calls `run_compaction_pipeline()` before each LLM call
- `lib/tasks_pkg/tool_dispatch.py` — tool results enter context at full fidelity (no preprocessing)
- `lib/database.py` — `transcript_archive` table for L3 archival
- `routes/common.py` — cleans up `transcript_archive` on conversation delete

## Persistence
- All state keyed by `conv_id` for concurrent conversation safety
- `transcript_archive` table: full message history before each L3 compression
- `_summary_cooldowns` dict: 30s cooldown per conv_id to prevent rapid re-summarization

