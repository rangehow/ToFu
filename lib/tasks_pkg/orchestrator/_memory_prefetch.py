# HOT_PATH — invoked once per user turn from run_task.
# Prefer logger.debug() over logger.info(). logger.info() is reserved
# for rare, high-signal events.
"""Memory-prefetch gate — extracted from run_task (pt_03f4cdf1 slice 9).

Section 3.5 of run_task's body: the proactive, per-user-turn memory
prefetch that surfaces past lessons via BM25 → cheap-LLM precision →
``<relevant_memories>`` injection. Also stashes the sibling
``_profileConsolidateEligible`` flag the post-done spawner in
``_finalize.py`` reads — those two locals ARE the seam that used to
force this block to live inside run_task.

Never raises: a prefetch failure is advisory and logs a warning.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


def maybe_run_memory_prefetch(
    *,
    task: dict[str, Any],
    cfg: dict[str, Any],
    messages: list[dict[str, Any]],
    tool_list: list[dict[str, Any]] | None,
    project_path: str | None,
    project_enabled: bool,
    memory_enabled: bool,
    has_real_tools: bool,
    injected_tool_calls: int,
) -> None:
    """Run BM25 + cheap-LLM memory prefetch, gated on eligibility.

    Skipped if any of:
      * memory toggle disabled (``memory_enabled=False``)
      * no real tools (memory tools unavailable anyway)
      * continue/resume turn (tool_history was replayed →
        ``injected_tool_calls > 0`` → not a fresh turn)

    Always stashes ``task['_profileConsolidateEligible']`` for the
    post-done profile-consolidation spawner in ``_finalize.py``.
    """
    task['_profileConsolidateEligible'] = bool(memory_enabled and has_real_tools)

    if not (memory_enabled and has_real_tools and not injected_tool_calls):
        return

    try:
        from lib.memory.prefetch import run_memory_prefetch
        # Active-tools list lets the cheap-LLM filter drop memories
        # about subsystems the user can't currently use (e.g. browser
        # memories when browser is off).
        _active_tools: list[str] = []
        for _t in (tool_list or []):
            try:
                _active_tools.append(_t['function']['name'])
            except (KeyError, TypeError) as _e_audit:
                logger.debug(
                    '[orchestrator] memory_prefetch caught %s: %s',
                    type(_e_audit).__name__, _e_audit)
                continue
        # ★ Extra workspace roots for memory scoping — recomputed
        #   locally at the (single) call site since pt_03f4cdf1 slice 3
        #   moved the prefetch pool init out of run_task. The same
        #   derivation is done inside start_prefetches for the
        #   background memory prefetch; they're deliberately
        #   independent so a future consumer can be added without
        #   threading a shared list through the call stack. Same rule:
        #   extras are projectPaths[1:] minus the primary, empty when
        #   disabled.
        _pp = project_path if project_enabled else None
        _mem_extra_paths: list[str] = []
        if project_enabled and _pp:
            _all_mem_paths = cfg.get('projectPaths') or []
            _mem_extra_paths = (
                [p for p in _all_mem_paths[1:] if p and p != _pp]
                if len(_all_mem_paths) > 1 else [])
        run_memory_prefetch(
            messages,
            project_path=project_path if project_enabled else None,
            task=task,
            emit_event=lambda ev: append_event(task, ev),
            active_tools=_active_tools,
            extra_paths=_mem_extra_paths,
        )
    except Exception as _e:
        # Advisory path — never block the task on prefetch failure.
        logger.warning('[Task %s] memory prefetch failed: %s',
                       task['id'][:8], _e, exc_info=True)
