"""Synchronous local memory-prefetch gate.

The former background cheap-LLM reranker added latency, billing, late-write
races, and a second context owner. Local high-confidence selection is fast
enough to finish inline and only stashes evidence for Context Composer.

The corpus it ranks does not depend on the query and comes from the resident
memory listing; an accepted attempt whose configuration already opens this
gate loads that listing for its roots at acceptance
(``lib.tasks_pkg.memory_listing``).
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger, task_log_tag
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


def memory_prefetch_corpus_roots(cfg, project_path, project_enabled):
    """The primary and extra roots whose memory corpus the prefetch ranks.

    Memories belong to the canonical project, never a task-owned checkout.
    """
    paths = cfg.get('canonicalProjectPaths') or cfg.get('projectPaths') or []
    primary = (cfg.get('canonicalProjectPath') or project_path) if project_enabled else None
    extras = [p for p in paths if p and p != primary] if primary else []
    return primary, extras


def accepted_memory_prefetch_roots(cfg):
    """Corpus roots of an accepted config that already opens the gate below.

    The gate needs memory enabled, real tools and no replayed Continue tool
    calls. An accepted configuration decides memory (after the turn prelude's
    profile and the chat-mode overlay) and the replay (an empty
    ``toolHistory`` replays none). Tool assembly decides tools in the worker;
    an attempt whose assembly ends text-only never reads the listing it loaded.
    Returns ``None`` when the configuration closes the gate.
    """
    from lib.agent_core.profiles import apply_profile
    from lib.memory import prefetch
    from lib.tasks_pkg.model_config import resolve_memory_scope

    cfg = apply_profile(cfg)
    memory_enabled, project_path, project_enabled = resolve_memory_scope(cfg)
    if not (prefetch.PREFETCH_ENABLED and memory_enabled) or cfg.get('toolHistory'):
        return None
    return memory_prefetch_corpus_roots(cfg, project_path, project_enabled)


def _held_tool_names(cfg, tool_list):
    """Tool names this request may call: its wire tools plus its deferred catalog.

    ``None`` (nothing known) leaves role fit unfiltered.
    """
    names = {(tool.get('function') or {}).get('name')
             for tool in [*(tool_list or ()), *(cfg.get('_executableToolCatalog') or ())]
             if isinstance(tool, dict)}
    names.discard(None)
    return names or None


def maybe_run_memory_prefetch(*, task: dict[str, Any], cfg: dict[str, Any],
                              messages: list[dict[str, Any]],
                              tool_list: list[dict[str, Any]] | None,
                              project_path: str | None,
                              project_enabled: bool,
                              memory_enabled: bool,
                              has_real_tools: bool,
                              injected_tool_calls: int,
                              turn_selection_retained: bool = False) -> None:
    # My Context is independent from relevance-retrieved experience memory.
    # Interactive turns learn explicit durable facts even when memory search is
    # off; headless surfaces have already stamped preferencesEnabled=False.
    from lib.agent_core.personal_scope import resolve_preferences_enabled
    from lib.tasks_pkg.execution_profile import is_third_party_text
    # Third-party text never reads the owner's memories or writes My Context.
    third_party = is_third_party_text(task)
    task['_profileConsolidateEligible'] = (
        not third_party and resolve_preferences_enabled(cfg))
    if third_party or not (memory_enabled and has_real_tools and not injected_tool_calls):
        task['_prefetchedMemories'] = []
        task['_memoryPrefetch'] = {
            'phase': 'skipped',
            'reason': ('memory_disabled' if third_party or not memory_enabled
                       else 'gate_disabled'),
        }
        return
    if turn_selection_retained:
        # A Continue restored this request's selection with its working
        # view. Not a fresh empty result: the composer keeps that block.
        task['_prefetchedMemories'] = []
        task['_memoryPrefetch'] = {'phase': 'skipped',
                                   'reason': 'turn_selection_retained'}
        return
    try:
        from lib.memory.prefetch import run_memory_prefetch
        primary, extras = memory_prefetch_corpus_roots(
            cfg, project_path, project_enabled)
        run_memory_prefetch(
            messages, project_path=primary, task=task,
            emit_event=lambda event: append_event(task, event),
            active_tools=_held_tool_names(cfg, tool_list),
            extra_paths=extras,
        )
    except Exception as exc:
        task['_prefetchedMemories'] = []
        task['_memoryPrefetch'] = {'phase': 'failed', 'reason': 'selection_failed'}
        logger.warning('[Task %s] local memory prefetch failed: %s',
                       task_log_tag(task.get('id', '?')), exc, exc_info=True)


__all__ = [
    'accepted_memory_prefetch_roots',
    'maybe_run_memory_prefetch',
    'memory_prefetch_corpus_roots',
]
