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
- `search_memories` — Search your memories by keyword (BM25). Call this when you suspect a project-specific convention or past lesson applies.
- `create_memory` — Save a NEW memory (accumulated experience).
- `update_memory` — Update an EXISTING memory's content, description, or tags.
- `delete_memory` — Remove an outdated or incorrect memory.
- `merge_memories` — Combine multiple overlapping/related memories into one consolidated memory.

**When to SEARCH memories (narrow triggers):**
- You suspect this exact project has an established convention you don't remember (style, naming, architecture).
- You're about to redo something the user has previously corrected you on in this project.
- You're hitting a tricky bug AND it feels like one you might have logged before.

**When NOT to search memories (use other tools instead):**
- The user mentions a local file path or directory → use `read_files` / `list_dir` directly. Don't search memory for it.
- The user asks about an external project, library, product, or service (e.g. claude-code, openclaw, citadel) → use `web_search` and/or read the local copy with `read_files` / `list_dir`. Memory is unlikely to have it.
- General coding / API knowledge questions → answer from training, or `web_search` for fresh info.
- A relevant `<relevant_memories>` block was already prefetched and injected this turn → it's already done; don't re-search the same topic.

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
Use search_memories when you suspect this project has an established convention or past lesson that applies.
Don't use it as a generic discovery step: if the user mentions a local path use read_files/list_dir;
if they ask about an external project/library use web_search. A `<relevant_memories>` block, when present,
already surfaces likely-relevant memories — don't re-search the same topic.
Proactively save memories when you discover: bug patterns, project conventions,
user preferences, complex workflows, or tool/API quirks.
Keep memories focused, well-described (40-80 chars), scope='project' or 'global'.
</memory_accumulation>"""



def build_memory_context(project_path=None):
    """Build a minimal memory hint for injection.

    Since memories are now discovered via the `search_memories` tool,
    this function only returns a short count hint (NOT a full listing).
    The model uses search_memories(query) to find relevant memories on demand.

    Args:
        project_path: Path to project for project-scoped memories.

    Returns None if no eligible memories exist, otherwise a short hint string.
    """
    memories = get_eligible_memories(project_path)
    if not memories:
        return None

    n = len(memories)
    return (
        f'You have {n} accumulated memories available. '
        f'A `<relevant_memories>` block is auto-injected when prefetch finds matches; '
        f'call search_memories(query) only when you specifically suspect a past project '
        f'convention or logged lesson applies (not as a generic discovery step).'
    )
