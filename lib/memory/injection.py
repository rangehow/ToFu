"""lib/memory/injection.py — System prompt injection and memory context building.

Memory injection strategy (tool-based, on-demand):
  • NO listing of memories is injected into the prompt.
  • The system message tells the model it has `search_memories` tool and
    how many memories are available.
  • The model calls `search_memories(query)` when it needs past experience,
    using its own keywords for BM25 matching (including body content).
  • This saves ~2K tokens/turn that were previously used for the index,
    and lets the model search with better keywords than the raw user message.
"""


from lib.memory.storage import get_eligible_memories

__all__ = [
    'MEMORY_ACCUMULATION_INSTRUCTIONS',
    'MEMORY_ACCUMULATION_INSTRUCTIONS_COMPACT',
    'build_memory_context',
]


MEMORY_ACCUMULATION_INSTRUCTIONS = """<memory_accumulation>
You have memory management tools to maintain a reusable knowledge base across sessions:
- `search_memories` — Search your memories by keyword (BM25). Call this when past experience might be relevant.
- `create_memory` — Save a NEW memory (accumulated experience).
- `update_memory` — Update an EXISTING memory's content, description, or tags.
- `delete_memory` — Remove an outdated or incorrect memory.
- `merge_memories` — Combine multiple overlapping/related memories into one consolidated memory.

**When to SEARCH memories:**
- Before starting a complex task — check if you've done something similar before.
- When debugging a tricky issue — you may have encountered this pattern previously.
- When working with a specific library/API/framework — search for past quirks and conventions.
- When unsure about project conventions — search for style rules, patterns, preferences.
- You can search multiple times with different keywords to find what you need.

**When to CREATE a memory:**
1. Bug pattern discovered — save the root cause and fix pattern.
2. Project convention learned — coding style, naming, architecture.
3. User preference revealed — the user corrected you or expressed a preference.
4. Complex workflow completed — save repeatable steps.
5. Tool/API quirk found — undocumented behavior, version-specific workaround.

Guidelines:
- Keep each memory focused on ONE topic.
- Use `scope='project'` for project-specific, `scope='global'` for general patterns.
- Write body as clear, actionable Markdown (not a conversation recap).
- Don't duplicate — search first, then create/update/merge as needed.
- Don't ask permission — manage memories quietly when relevant.

⚠️ **Description quality is critical** — search_memories uses BM25 over name+description+body.
  Write descriptions that are specific and include key trigger words (40-80 chars).
</memory_accumulation>"""


# Compact version — stays in the system message for cache stability.
# Includes search_memories as the primary discovery mechanism.
MEMORY_ACCUMULATION_INSTRUCTIONS_COMPACT = """<memory_accumulation>
You have memory tools: search_memories (keyword search), create/update/delete/merge_memories.
Use search_memories PROACTIVELY when past experience might help — debugging, conventions, APIs.
Proactively save memories when you discover: bug patterns, project conventions,
user preferences, complex workflows, or tool/API quirks.
Keep memories focused, well-described (40-80 chars), scope='project' or 'global'.
</memory_accumulation>"""



def build_memory_context(project_path=None, context_window_tokens=None,
                         query=None):
    """Build a minimal memory hint for injection.

    Since memories are now discovered via the `search_memories` tool,
    this function only returns a short count hint (NOT a full listing).
    The model uses search_memories(query) to find relevant memories on demand.

    Args:
        project_path: Path to project for project-scoped memories.
        context_window_tokens: Unused (kept for backward compat).
        query: Unused (kept for backward compat).

    Returns None if no eligible memories exist, otherwise a short hint string.
    """
    memories = get_eligible_memories(project_path)
    if not memories:
        return None

    n = len(memories)
    return (
        f'You have {n} accumulated memories from previous sessions. '
        f'Use search_memories(query) to find relevant past experience.'
    )
