---
name: search-memories-tool-based-discovery
description: Memory discovery via search_memories tool instead of injected listing — saves ~2K tokens/turn, model searches with own keywords
enabled: true
tags: [memory, architecture, tool-design, bm25]
created: 2026-04-11T16:19:44Z
updated: 2026-04-11T16:19:44Z
---

# Memory System: Tool-Based Discovery (search_memories)

## Architecture Change
Memories are no longer injected as an `<available_memories>` XML listing in the user message.
Instead, the model uses the `search_memories(query, top_k=5)` tool to find relevant memories on demand.

## What Changed
1. **`lib/memory/tools.py`** — Added `SEARCH_MEMORIES_TOOL` definition
2. **`lib/memory/relevance.py`** — Added `search_memories()` function with body-inclusive BM25
3. **`lib/tasks_pkg/handlers/memory.py`** — Added `_memory_search` handler
4. **`lib/memory/injection.py`** — `build_memory_context()` returns just a count hint, not a full listing
5. **`lib/tasks_pkg/system_context.py`** — `inject_memory_to_user()` injects minimal hint only

## Key Design Decisions
- **No category/index** — model doesn't need to see all memory names; it searches when needed
- **Body content indexed** — `search_memories` BM25 includes memory body (first 2000 chars), unlike the old `filter_relevant_memories` which only scored on name/desc/tags
- **Multiple searches** — model can call search_memories multiple times with different keywords
- **Compact system instructions** mention search_memories as primary discovery mechanism
- **Backward compat** — `build_memory_context()` still accepts query/context_window_tokens params (ignored)
- **MEMORY_TOOL_NAMES** now has 5 entries (was 4): added 'search_memories'

