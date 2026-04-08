"""lib/memory/injection.py — System prompt injection and memory context building.

Only a compact XML index of memory names + descriptions is injected into
the system prompt. The agent uses read_file to load full memory content
on-demand.

Budget management (aligned with Claude Code):
  • Memories get 1% of the context window (in characters).
  • Progressive degradation when over budget:
      1. Full descriptions (capped at 250 chars each)
      2. Truncated descriptions to fit budget
      3. Names-only (extreme case)
  • Default budget: 8000 chars (~2000 tokens) — 1% of 200K × 4 chars/token.
  • Path templates in header → per-entry IDs only (saves ~50 chars/memory).
"""

import os

from lib.memory.storage import GLOBAL_MEMORY_SUBDIR, get_eligible_memories

__all__ = [
    'MEMORY_ACCUMULATION_INSTRUCTIONS',
    'MEMORY_ACCUMULATION_INSTRUCTIONS_COMPACT',
    'build_memory_context',
]

# ═══════════════════════════════════════════════════════
#  Budget constants (mirroring Claude Code's MemoryTool/prompt.ts)
# ═══════════════════════════════════════════════════════

MEMORY_BUDGET_CONTEXT_PERCENT = 0.01   # 1% of context window
CHARS_PER_TOKEN = 4
DEFAULT_CHAR_BUDGET = 8_000           # Fallback: 1% of 200K × 4
MAX_LISTING_DESC_CHARS = 250          # Per-entry hard cap
MIN_DESC_LENGTH = 20                  # Below this → names-only fallback


MEMORY_ACCUMULATION_INSTRUCTIONS = """<memory_accumulation>
You have memory management tools to maintain a reusable knowledge base across sessions:
- `create_memory` — Save a NEW memory (accumulated experience).
- `update_memory` — Update an EXISTING memory's content, description, or tags.
- `delete_memory` — Remove an outdated or incorrect memory.
- `merge_memories` — Combine multiple overlapping/related memories into one consolidated memory.

You should PROACTIVELY manage memories when any of these situations occur:

**When to CREATE a memory:**
1. **Bug pattern discovered** — You helped debug a tricky issue. Save the root cause and fix pattern.
2. **Project convention learned** — You noticed a coding style, naming convention, directory layout, or architectural pattern.
3. **User preference revealed** — The user corrected you or expressed a preference for how things should be done.
4. **Complex workflow completed** — You executed a multi-step task (build, deploy, refactor). Save the steps as a repeatable recipe.
5. **Tool/API quirk found** — You discovered an undocumented behavior, version-specific workaround, or configuration gotcha.

**When to UPDATE a memory:**
- You discover new information that extends or corrects an existing memory.
- A memory's description is too vague and needs improvement.
- A memory's content is partially outdated but still useful.

**When to DELETE a memory:**
- A memory is completely outdated (e.g. library upgraded, API changed).
- A memory contains incorrect information that could be harmful.
- A memory is a duplicate of another, better memory.

**When to MERGE memories:**
- Two or more memories cover overlapping topics and would be better as one consolidated memory.
- Multiple small memories could be combined into a comprehensive guide.

Guidelines:
- Keep each memory focused on ONE topic — don't bundle unrelated lessons.
- Use `scope='project'` for project-specific knowledge, `scope='global'` for general patterns.
- Write the body as clear, actionable Markdown instructions (not a conversation recap).
- Don't duplicate — if you've already saved a similar memory, skip it (or merge it).
- Don't ask the user for permission — just manage memories quietly when relevant.
- Tag memories for easier filtering (e.g. `['python', 'debugging']`, `['react', 'convention']`).

⚠️ **Description quality is critical** — the description is the ONLY thing injected into the
system prompt (memory bodies are loaded on-demand). A vague description means the memory will
never be found. Write descriptions that are:
  - At least 20 characters, ideally 40-80 characters
  - Specific about WHEN the memory applies (e.g. "Fix for Flask SQLAlchemy circular import when using blueprints")
  - NOT generic (avoid "useful tips", "some notes about X")
  - Include key trigger words that would appear in a relevant user query
</memory_accumulation>"""


# Compact version (~400 chars) — stays in the system message for cache stability.
# The full version above is only used when injected alongside the memory listing
# (which now goes into the user message, not the system message).
MEMORY_ACCUMULATION_INSTRUCTIONS_COMPACT = """<memory_accumulation>
You have memory CRUD tools: create_memory, update_memory, delete_memory, merge_memories.
Proactively save memories when you discover: bug patterns, project conventions,
user preferences, complex workflows, or tool/API quirks.
Keep memories focused, well-described (40-80 chars), scope='project' or 'global'.
Description quality is critical — be specific about WHEN the memory applies.
</memory_accumulation>"""


def _get_char_budget(context_window_tokens=None):
    """Compute character budget for memory listing.

    Mirrors Claude Code's getCharBudget(): 1% of context window in characters.
    """
    if context_window_tokens and context_window_tokens > 0:
        return int(context_window_tokens * CHARS_PER_TOKEN * MEMORY_BUDGET_CONTEXT_PERCENT)
    return DEFAULT_CHAR_BUDGET


def _truncate_desc(desc, max_len):
    """Truncate description to max_len characters with ellipsis."""
    if len(desc) <= max_len:
        return desc
    return desc[:max_len - 1] + '\u2026'


def _abbreviate_home(path):
    """Replace home dir prefix with ~."""
    if not path:
        return ''
    home = os.path.expanduser('~')
    if path.startswith(home):
        return '~' + path[len(home):]
    return path


def build_memory_context(project_path=None, context_window_tokens=None,
                         query=None):
    """Build the memory context string for injection.

    Uses budget-aware injection aligned with Claude Code:
      1. Compact XML index with path templates (not per-entry paths).
      2. Budget = 1% of context window (default 8K chars).
      3. Progressive degradation: full desc → truncated desc → names-only.

    Args:
        project_path: Path to project for project-scoped memories.
        context_window_tokens: Model's context window size in tokens.
            Used to compute the character budget (1% of window × 4 chars/token).
            Falls back to DEFAULT_CHAR_BUDGET (8000 chars) if not provided.
        query: Optional user message text for BM25 relevance filtering.
            When provided and there are more than DEFAULT_TOP_K memories,
            only the most relevant memories are included.
            When None, all eligible memories are included (backward compat).

    Returns None if no eligible memories exist.
    """
    memories = get_eligible_memories(project_path)
    if not memories:
        return None

    # BM25 relevance filtering when query is provided
    if query:
        from lib.memory.relevance import filter_relevant_memories
        memories = filter_relevant_memories(memories, query)

    budget = _get_char_budget(context_window_tokens)
    return _build_index_inject(memories, budget, project_path)


# ═══════════════════════════════════════════════════════
#  Entry formatters — use memory ID (= filename stem), not full path.
#  Path templates in the header tell the model how to reconstruct.
# ═══════════════════════════════════════════════════════

def _fmt_full(mem, desc):
    """Name + description (no tags — tags are in the full memory file)."""
    return f'<memory name="{mem["name"]}" description="{desc}"/>'


def _fmt_name_only(mem):
    """Just the name — no description."""
    return f'<memory name="{mem["name"]}"/>'


# ═══════════════════════════════════════════════════════
#  Header / footer builders
# ═══════════════════════════════════════════════════════

def _build_header(memories, project_path):
    """Build header with path templates for memory resolution.

    Instead of repeating full paths per entry, we put templates in the
    header. Each entry uses `name` which doubles as the filename stem
    (name ≈ id, file is {id}.md).
    """
    lines = [
        '\n<available_memories>',
        f'You have {len(memories)} accumulated memory(s) from previous sessions.',
        'To load a memory, use `read_file` on its path. Paths:',
    ]
    if project_path:
        global_dir = _abbreviate_home(os.path.join(project_path, GLOBAL_MEMORY_SUBDIR))
        proj_dir = _abbreviate_home(os.path.join(project_path, '.chatui', 'skills'))
        lines.append(f'  Global: {global_dir}/{{name}}.md')
        lines.append(f'  Project: {proj_dir}/{{name}}.md')
    else:
        lines.append('  Path: ~/.chatui/skills/{name}.md')
    lines.append('')
    return lines


_FOOTER_LINES = [
    '',
    'When a memory looks relevant, read its file to get full instructions.',
    'Do NOT guess memory content from the description alone.',
    '</available_memories>',
]


# ═══════════════════════════════════════════════════════
#  Main builder
# ═══════════════════════════════════════════════════════

def _build_index_inject(memories, budget, project_path=None):
    """Budget-aware compact XML index with progressive degradation.

    Strategy (inspired by Claude Code's formatCommandsWithinBudget):
      1. Try full descriptions (capped at MAX_LISTING_DESC_CHARS each).
      2. If over budget → uniformly truncate ALL descriptions to fit.
      3. If max desc < MIN_DESC_LENGTH → names-only.
      4. If still over → cap the count.

    Note: Claude Code partitions "bundled" (always full) vs "rest" (trimmable),
    but that only works with ~5-10 bundled memories. With 100+ project memories,
    we treat all entries uniformly.
    """
    header_lines = _build_header(memories, project_path)
    footer_lines = list(_FOOTER_LINES)

    chrome = '\n'.join(header_lines) + '\n'.join(footer_lines)
    chrome_chars = len(chrome) + 2
    remaining = budget - chrome_chars
    if remaining < 100:
        remaining = 100

    # ── Pass 1: Full descriptions (capped per-entry at 250 chars) ──
    entries = []
    for mem in memories:
        desc = mem.get('description', '') or mem.get('name', '')
        desc = _truncate_desc(desc, MAX_LISTING_DESC_CHARS)
        entries.append((mem, desc))

    full_lines = [_fmt_full(s, d) for s, d in entries]
    full_total = sum(len(l) + 1 for l in full_lines)

    if full_total <= remaining:
        return '\n'.join(header_lines + full_lines + footer_lines)

    # ── Pass 2: Uniformly truncate descriptions to fit ──
    return _build_truncated_uniform(memories, remaining, header_lines, footer_lines)


def _build_truncated_uniform(memories, entry_budget, header_lines, footer_lines):
    """Uniform truncation across ALL memories when full descriptions exceed budget.

    Args:
        memories: List of memory dicts.
        entry_budget: Character budget for memory entries ONLY (chrome excluded).
        header_lines: Header lines (prepended to output).
        footer_lines: Footer lines (appended to output).

    Progressive degradation:
      1. Try truncated descriptions for all
      2. Names-only for all
      3. Cap the count if even names don't fit
    """
    n = len(memories)
    if n == 0:
        return '\n'.join(header_lines + footer_lines)

    # ── Step 1: Names-only baseline ──
    name_lines = [_fmt_name_only(s) for s in memories]
    name_chars = sum(len(l) + 1 for l in name_lines)

    parts = list(header_lines)

    if name_chars > entry_budget:
        # Names-only exceeds budget — cap the count
        used = 0
        shown = 0
        for line in name_lines:
            cost = len(line) + 1
            if used + cost > entry_budget:
                break
            parts.append(line)
            used += cost
            shown += 1
        if shown < n:
            parts.append(
                f'<!-- {n - shown} more memories omitted '
                f'(use list_all_memories to see all) -->'
            )
        parts += footer_lines
        return '\n'.join(parts)

    # ── Step 2: Names fit. Try adding descriptions ──
    # Going from name-only → full entry adds ' description="<desc>"' overhead
    # per entry (~16 chars of XML chrome beyond just the description text).
    DESC_XML_OVERHEAD = 16  # len(' description=""') = 15, +1 for safety

    avail = entry_budget - name_chars
    max_possible = (avail // n) - DESC_XML_OVERHEAD if n else 0

    if max_possible < MIN_DESC_LENGTH:
        # Not enough room for meaningful descriptions — names-only
        parts.extend(name_lines)
        parts += footer_lines
        return '\n'.join(parts)

    # Truncate descriptions to max_possible
    max_desc = min(max_possible, MAX_LISTING_DESC_CHARS)
    for mem in memories:
        desc = mem.get('description', '') or mem.get('name', '')
        parts.append(_fmt_full(mem, _truncate_desc(desc, max_desc)))

    parts += footer_lines
    return '\n'.join(parts)
