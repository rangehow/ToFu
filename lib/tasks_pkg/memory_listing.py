"""Load an accepted attempt's resident memory listing before startup reads it.

Responsibility: memory prefetch ranks the eligible metadata corpus of the
attempt's roots inside ``startup_memory`` and reads it from the process's
resident memory listing (``lib.memory.prefetch.list_prefetch_corpus``). A store
directory the process holds no snapshot of yet (the first start after a
restart, or roots this process never listed) is loaded by its first read: the
persisted snapshot, or a walk when there is none. An accepted attempt whose
configuration already opens the prefetch gate therefore reads its roots at
acceptance, next to its input translation, on the shared context-provider
demand executor: that load overlaps context assembly, registration, executor
start and worker entry instead of ``startup_memory``, and the read requests
the refresh that keeps the listing current.

Entry point: ``prepare_memory_listing`` (``lib.conversation_sync.task_start``).
The listing is process-owned, so the attempt holds nothing to release: a
stopped attempt leaves the listing loaded for the next reader. Flow roots never
prepare (a Flow root never prefetches). A project-dispatch worker does: its
memory roots are the canonical project its admission names, which worker-entry
checkout binding never moves.

Dependencies: the gate is ``_memory_prefetch.accepted_memory_prefetch_roots``,
the executor ``context_composer._provider_executor``. Decision record:
docs/DEFENSIVE_PATTERNS.md, "Startup reads a resident memory listing".
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


def prepare_memory_listing(config: dict[str, Any]) -> None:
    """Load the attempt's prefetch corpus listing off the startup path.

    Never raises: the preparation is an optimization, so a failure leaves
    ``startup_memory`` to load the listing itself.
    """
    try:
        roots = _prefetch_roots(config)
        if roots is None:
            return
        from lib.tasks_pkg.context_composer._provider_executor import (
            context_provider_executor,
        )
        context_provider_executor.submit(_load, *roots)
    except Exception:
        logger.warning('[MemoryListing] listing not prepared at acceptance; '
                       'startup loads it', exc_info=True)


def _prefetch_roots(config):
    from lib.orchestration.chat_flow_selection import (
        CHAT_FLOW_ENTRY_NONE,
        select_chat_flow_entry,
    )

    if select_chat_flow_entry(config) != CHAT_FLOW_ENTRY_NONE:
        return None
    from lib.tasks_pkg.orchestrator._memory_prefetch import (
        accepted_memory_prefetch_roots,
    )
    return accepted_memory_prefetch_roots(config)


def _load(project_path, extra_paths) -> None:
    from lib.memory.prefetch import list_prefetch_corpus

    try:
        list_prefetch_corpus(project_path, extra_paths)
    except Exception:
        logger.warning('[MemoryListing] preparing the listing of %s failed; '
                       'startup loads it', project_path, exc_info=True)


__all__ = ['prepare_memory_listing']
