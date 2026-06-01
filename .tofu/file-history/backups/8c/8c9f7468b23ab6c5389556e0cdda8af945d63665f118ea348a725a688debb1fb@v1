"""System context injection — append/prepend helpers and context layering.

Extracted from orchestrator.py to isolate the system-message manipulation
logic (project context, memory, swarm prompt, search addendum).

Includes delta attachment tracking (inspired by Claude Code): context strings
are hashed, and when the content is unchanged between successive tasks in the
same conversation, we **skip the expensive load** (FUSE I/O) but still inject
the text.  This is necessary because each task receives a *fresh* message list
from the frontend — the system message does NOT carry over project/memory
context from the previous task.

**Claude-Code-style layout** (the only layout — no kill switch).

Static prompt sections (``# System``, ``# Doing tasks``, ``# Executing actions
with care``, ``# Using your tools``, ``# Tone and style``, ``# Output
efficiency``, ``# Environment``, etc.) are assembled by
``lib.tasks_pkg.system_prompt_cc.build_static_prompt`` as ONE cache-stable
block in the system message.  CLAUDE.md / project-intelligence content is
**NOT** placed in the system message — it goes into a prepended user message
with ``_isMeta: True`` wrapped in ``<system-reminder>`` tags (mirroring
Claude Code's ``prependUserContext`` in ``utils/api.ts:449``).  A/B-validated
to save 18% cost / +49% cache hit — see
``.chatui/skills/claudemd-placement-ab-test-results.md``.

Historical note: a ``CHATUI_CC_SYSPROMPT`` env-var kill switch used to toggle
a legacy layout where project context was prepended into the system message.
It was removed on 2026-05-07 after an empty-string env-var value silently
flipped the layout in production — see the commit that touched this line.
"""

import hashlib

from lib.log import get_logger

logger = get_logger(__name__)

from lib.tasks_pkg import system_prompt_cc

# ── Delta attachment tracking ──
# Cache of (hash, text) per category per conv_id.
# Purpose: skip the expensive FUSE load (get_context_for_prompt /
# build_memory_context) when the content hasn't changed.
# IMPORTANT: we ALWAYS inject into the system message — we only skip
# the *computation*.  Each task gets fresh messages from the frontend,
# so the text is NOT already present.
_last_context_cache: dict[tuple[str, str], tuple[str, str]] = {}


def _context_hash(text: str) -> str:
    """Compute a fast hash for a context string."""
    return hashlib.md5(text.encode('utf-8', errors='replace')).hexdigest()[:16]


def _get_cached_or_compute(conv_id: str, category: str,
                            compute_fn) -> str:
    """Return cached context text if hash matches, else re-compute.

    Unlike the previous ``_should_inject`` (which skipped injection),
    this function always returns text — it only skips the *computation*.

    Args:
        conv_id:    Conversation ID for cache scoping.
        category:   'project' or 'memory'.
        compute_fn: Zero-arg callable that produces the context string.

    Returns:
        The context string (either from cache or freshly computed).
        Empty string if compute_fn returns falsy.
    """
    key = (conv_id, category)
    text = compute_fn()
    if not text:
        return ''
    h = _context_hash(text)
    prev = _last_context_cache.get(key)
    if prev and prev[0] == h:
        logger.debug('[DeltaCtx] Reusing cached %s context (hash=%s) conv=%s',
                     category, h[:8], conv_id[:8])
        return prev[1]  # cached text (should be identical)
    _last_context_cache[key] = (h, text)
    return text


_TIMESTAMP_PREFIX = 'Current date and time: '


def inject_search_addendum_to_user(messages: list, search_enabled: bool,
                                    round_num: int = 0):
    """Legacy no-op — timestamp moved to system prompt as date-only.

    Previously injected "Current date and time: ..." into the last user
    message.  A/B testing showed this killed cache (Arm A: 77.9% cache,
    $0.49 vs Arm C date-only in system: 85.7%, $0.36).

    The date is now injected in _inject_system_contexts() step 4.5 as
    date-only format (changes once per UTC day → cache-stable).

    This function is kept for backward compatibility but does nothing.
    It still strips old timestamps from user messages to clean up
    conversations that had them injected previously.

    Args:
        messages: The messages list (may be cleaned in-place).
        search_enabled: Ignored (was: whether search/tools are enabled).
        round_num: Ignored (was: current round within the task).
    """
    # Strip old timestamps from user messages for clean cache prefix
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') == 'user':
            content = messages[i].get('content', '')
            if isinstance(content, str) and _TIMESTAMP_PREFIX in content:
                messages[i]['content'] = _strip_old_timestamp(content)
            elif isinstance(content, list):
                _new = [b for b in content
                        if not (isinstance(b, dict) and b.get('type') == 'text'
                                and b.get('text', '').strip().startswith(_TIMESTAMP_PREFIX))]
                if len(_new) != len(content):
                    messages[i]['content'] = _new
            break  # only check the last user message


def _strip_old_timestamp(text: str) -> str:
    """Remove a previously injected timestamp line from user message text."""
    lines = text.split('\n')
    cleaned = [ln for ln in lines if not ln.strip().startswith(_TIMESTAMP_PREFIX)]
    # Also strip trailing blank lines left behind
    result = '\n'.join(cleaned).rstrip()
    return result


def _wrap_system_reminder(text: str) -> str:
    """Wrap text in <system-reminder> tags.

    Claude Code wraps all mid-conversation system-level injections in these
    tags to distinguish them from user-authored content.  The model is trained
    to treat <system-reminder> content as authoritative system instructions.

    We use the same convention for dynamic injected context (project, memory,
    search addendum, swarm) so that:
      1. The model clearly distinguishes system instructions from user text.
      2. Compaction can identify and preserve system-reminder blocks.
      3. Context is consistent with Claude Code's convention.
    """
    return f'<system-reminder>\n{text}\n</system-reminder>'


def _append_to_system_message(messages, text, *, as_separate_block=False):
    """Append text to the first system message, or create one if absent.

    Helper used by _inject_system_contexts to avoid repeating the
    str-vs-list content detection pattern.

    Args:
        messages: The messages list (mutated in-place).
        text: The text to append.
        as_separate_block: If True and content is already a list,
            append as a separate text block (for cache segmentation).
            If content is a string, convert to list-of-blocks first.
    """
    if messages and messages[0].get('role') == 'system':
        sc = messages[0].get('content', '')
        if as_separate_block:
            # Force list-of-blocks format for cache segmentation
            if isinstance(sc, str):
                messages[0]['content'] = [
                    {'type': 'text', 'text': sc},
                    {'type': 'text', 'text': text},
                ]
            elif isinstance(sc, list):
                messages[0]['content'].append({'type': 'text', 'text': text})
            else:
                messages[0]['content'] = [{'type': 'text', 'text': text}]
        else:
            if isinstance(sc, str):
                messages[0]['content'] = sc + '\n\n' + text
            elif isinstance(sc, list):
                # Merge into last text block to avoid block proliferation
                if sc and isinstance(sc[-1], dict) and sc[-1].get('type') == 'text':
                    sc[-1] = {**sc[-1], 'text': sc[-1]['text'] + '\n\n' + text}
                else:
                    messages[0]['content'].append({'type': 'text', 'text': text})
    else:
        # No system message yet — create one.
        # Respect as_separate_block so callers that want downstream cache
        # segmentation don't get stuck with a string content.
        if as_separate_block:
            messages.insert(0, {'role': 'system',
                                'content': [{'type': 'text', 'text': text}]})
        else:
            messages.insert(0, {'role': 'system', 'content': text.strip()})


def _insert_user_context_message(messages, body: str) -> None:
    """Insert a Claude-Code-style ``<system-reminder>`` user message.

    Inserted RIGHT AFTER the last system message (or at index 0 if no
    system message), BEFORE the first real user message.  Matches Claude
    Code's ``prependUserContext`` behavior — see ``utils/api.ts:449``.

    Marked with ``_isMeta: True`` so chatui's debug panel / token
    counter / persistence layers can recognize it as synthetic.

    Idempotency: skip if any existing user message already contains the
    ``<system-reminder>`` claudeMd marker — Critic mode reuses worker
    messages and re-injecting would duplicate.
    """
    # Find the first non-system slot
    insert_idx = 0
    for i, m in enumerate(messages):
        if m.get('role') != 'system':
            insert_idx = i
            break
    else:
        # All system → append
        insert_idx = len(messages)

    # Idempotency: if a previous _isMeta user message with the same
    # marker already exists, don't double-inject.
    for m in messages:
        if m.get('role') != 'user' or not m.get('_isMeta'):
            continue
        c = m.get('content', '')
        if isinstance(c, str) and '[PROJECT CO-PILOT MODE]' in c:
            logger.debug('[Inject] CC user-context already present, skipping')
            return
        if isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict) and blk.get('type') == 'text' \
                        and '[PROJECT CO-PILOT MODE]' in blk.get('text', ''):
                    logger.debug('[Inject] CC user-context already present, skipping')
                    return

    messages.insert(insert_idx, {
        'role': 'user',
        'content': body,
        '_isMeta': True,  # synthetic marker — see Claude Code's isMeta flag
    })


def _system_text(messages) -> str:
    """Return the plain-text concatenation of the first system message.

    Used for idempotency checks in ``_inject_system_contexts`` — callers
    can look for a known marker substring (e.g. ``[PROJECT CO-PILOT MODE]``,
    ``Function Result Clearing``) to detect whether a context block has
    already been injected.  Returns empty string when there is no system
    message.
    """
    if not messages or messages[0].get('role') != 'system':
        return ''
    sc = messages[0].get('content', '')
    if isinstance(sc, str):
        return sc
    if isinstance(sc, list):
        parts = []
        for b in sc:
            if isinstance(b, dict) and b.get('type') == 'text':
                parts.append(b.get('text', '') or '')
        return '\n\n'.join(parts)
    return ''


# Marker embedded in the Claude-Code static block. Used as the idempotency
# probe that says "the CC static prompt is already in the system message".
_CC_STATIC_MARKER = "IMPORTANT: You must NEVER generate or guess URLs"


def _inject_system_contexts(messages, project_path, project_enabled,
                             memory_enabled, search_enabled, swarm_enabled,
                             has_real_tools, conv_id: str = '',
                             task: dict = None, model: str = ''):
    """Inject the Claude-Code-style system + user contexts into *messages*.

    Modifies the messages list directly. Final shape:

      System message (one cache-stable block per entry):
        1. Static Claude-Code-style prompt
           (intro / # System / # Doing tasks / # Executing actions with care
            / # Using your tools / # Tone and style / # Output efficiency
            / # Function Result Clearing / SUMMARIZE / system-reminder note
            / # Environment / Notes: / Current date)
        2. (optional) Memory accumulation instructions + count hint
        3. (optional) Swarm / parallel-execution prompt

      User message at index 1 (prepended before real user turn):
        <system-reminder> CLAUDE.md / project-intelligence </system-reminder>
        with ``_isMeta: True`` — matches Claude Code's ``prependUserContext``.

    Memory prefetch support: if ``task`` is provided and contains
    ``_prefetch_project`` / ``_prefetch_memory`` futures, their already-
    completed results are consumed instead of re-computing (saving FUSE I/O
    latency).

    **Idempotency.**  Each section checks for its own marker via
    ``_system_text(messages)`` and skips if already present — required for
    endpoint-mode (Planner / Worker / Critic share the same messages) and
    for post-compaction re-injection.

    Args:
        model: Model ID for the ``# Environment`` section.
    """
    _cid = conv_id or ''

    # ── Idempotency probe: detect an already-injected system message ──
    _existing = _system_text(messages)

    # ── Helper: try to get prefetched result, else compute synchronously ──
    def _get_prefetched(key, fallback_fn):
        """Get result from prefetch future if available, else call fallback."""
        if task and task.get(key):
            future = task[key]
            if future.done():
                try:
                    result = future.result(timeout=0)
                    logger.debug('[MemPrefetch] Using prefetched %s', key)
                    return result
                except Exception as e:
                    logger.debug('[MemPrefetch] %s failed, falling back: %s',
                                 key, e)
            else:
                logger.debug('[MemPrefetch] %s not done yet, falling back', key)
        return fallback_fn()

    # ── Load project context (CLAUDE.md) once ──
    proj_ctx = ''
    if project_enabled:
        def _load_project():
            from lib.project_mod import get_context_for_prompt
            return get_context_for_prompt(project_path)

        if _cid:
            proj_ctx = _get_cached_or_compute(
                _cid, 'project',
                lambda: _get_prefetched('_prefetch_project', _load_project),
            ) or ''
        else:
            proj_ctx = _get_prefetched('_prefetch_project', _load_project) or ''

    logger.info('[Inject] conv=%s proj_enabled=%s proj_ctx_len=%d '
                'has_real_tools=%s',
                (_cid or '?')[:8], project_enabled, len(proj_ctx or ''),
                has_real_tools)

    # ★ 1. Static Claude-Code block — append as separate cache-stable block.
    #      Injected ONCE; marker guards against endpoint-mode re-entry.
    if _CC_STATIC_MARKER not in _existing:
        _cwd = project_path or ''
        try:
            import os as _os
            _is_git = bool(_cwd and _os.path.isdir(_os.path.join(_cwd, '.git')))
        except Exception as e:
            logger.debug('[SysPrompt] is_git probe failed: %s', e)
            _is_git = False

        # Extra-roots (multi-root workspace) — reuse project_mod snapshot.
        _extra_roots = []
        try:
            from lib.project_mod.config import _roots, _lock
            with _lock:
                for _rn, _rs in _roots.items():
                    if _rs.get('path') and _rs['path'] != _cwd:
                        _extra_roots.append(f"{_rn} → {_rs['path']}")
        except Exception as e:
            logger.debug('[SysPrompt] extra-roots probe failed: %s', e)

        _static_block = system_prompt_cc.build_static_prompt(
            cwd=_cwd, is_git=_is_git, model=model,
            extra_roots=_extra_roots or None,
            has_real_tools=has_real_tools,
        )
        _append_to_system_message(messages, _static_block,
                                   as_separate_block=True)
        _existing = _system_text(messages)

    # ★ 2. Project CLAUDE.md → prepended user _isMeta message (cache-friendly).
    if proj_ctx and '[PROJECT CO-PILOT MODE]' not in _existing:
        _reminder = system_prompt_cc.build_user_context_reminder(
            claude_md=proj_ctx, current_date=None,
        )
        if _reminder:
            _insert_user_context_message(messages, _reminder)
            logger.info('[Inject] conv=%s CLAUDE.md inserted as user '
                        '_isMeta msg (len=%d)',
                        (_cid or '?')[:8], len(_reminder))
    elif proj_ctx:
        # CLAUDE.md is already IN the system message — shouldn't happen under
        # the single-layout design.  Left as a warning so stale snapshots /
        # external injections are surfaced.
        logger.warning('[Inject] conv=%s CLAUDE.md marker found in system '
                       'text — something is placing it in system instead of '
                       'as a user _isMeta msg. Check endpoint re-entry / '
                       'stale legacy code paths.',
                       (_cid or '?')[:8])

    # ★ 3. Compact memory accumulation instructions + memory count hint
    #   Both the HOW-TO-USE instructions and the dynamic count hint
    #   ("You have N accumulated memories...") go into the system message.
    if has_real_tools:
        if '<memory_accumulation>' in _existing:
            logger.debug('[Inject] Memory instructions already present, skipping '
                         'append (conv=%s)', _cid[:8] if _cid else '?')
        else:
            from lib.memory import MEMORY_ACCUMULATION_INSTRUCTIONS_COMPACT

            # Build memory count hint (dynamic, changes on CRUD)
            _pp = project_path if project_enabled else None
            def _load_memory_hint():
                from lib.memory import build_memory_context
                return build_memory_context(project_path=_pp)

            if _cid:
                _mem_hint = _get_cached_or_compute(
                    _cid, 'memory_hint', _load_memory_hint)
            else:
                _mem_hint = _load_memory_hint() or ''

            _mem_block = MEMORY_ACCUMULATION_INSTRUCTIONS_COMPACT
            if _mem_hint:
                _mem_block = _mem_hint + '\n\n' + _mem_block

            # Separate cache-block: the memory count in _mem_hint changes
            # whenever memories are CRUD'd, so we want its BP independent
            # from the static CC block's BP.
            _append_to_system_message(
                messages,
                _wrap_system_reminder(_mem_block),
                as_separate_block=True)
            _existing = _system_text(messages)

    # ★ 4. Swarm system prompt injection (only when swarm is enabled)
    if swarm_enabled and project_enabled and '<parallel_execution>' not in _existing:
        swarm_prompt = """
<parallel_execution>
You have a **parallel execution** system via `spawn_agents`. It dramatically speeds up complex tasks by running multiple sub-tasks simultaneously.

**ALWAYS use `spawn_agents` when:**
- A task has 2+ parts that can be worked on independently
- You need to research/analyze/modify multiple files or topics
- Any decomposition would speed things up vs doing everything sequentially

**Do NOT use it for:**
- Trivial single-step questions
- Tasks that are inherently sequential with no parallelizable parts

**How:**
Call `spawn_agents` with a list of sub-tasks. Each sub-task only needs an `objective` (what to do) and optional `context`. Don't overthink it — just split and ship.

```json
{"agents": [
  {"objective": "Find all usages of deprecated API X in lib/ and routes/", "context": "Looking for function calls like X.do_thing()"},
  {"objective": "Research the new API Y replacement patterns from the docs", "context": "See https://docs.example.com/migration"},
  {"objective": "Write unit tests for the migration in tests/test_migration.py", "context": "Test both old→new conversion and edge cases"}
]}
```

Sub-tasks run in parallel with full tool access. Results come back together. You then synthesize a final answer.
Use `depends_on: [0]` only when a task truly needs another's output (rare — prefer maximum parallelism).
</parallel_execution>
"""
        _append_to_system_message(messages,
                                   _wrap_system_reminder(swarm_prompt),
                                   as_separate_block=True)

    # Current date is already inlined by build_static_prompt()'s
    # section_current_date — do NOT append it here or it duplicates.




# ═══════════════════════════════════════════════════════════════════════════════
#  Last-user-text extraction (shared helper)
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_last_user_text(messages: list) -> str:
    """Extract the text content of the last user message.

    Used by memory-prefetch to build a BM25 query from the conversational
    surface of the last turn.
    """
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') == 'user':
            content = messages[i].get('content', '')
            if isinstance(content, str):
                return content
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'text':
                        parts.append(block.get('text', ''))
                return ' '.join(parts)
    return ''
