"""Prefetch corpus listing: the eligible metadata corpus the prefetch ranks.

Responsibility: ``list_prefetch_corpus`` is the one definition of the corpus
the high-confidence prefetch ranks: eligible, metadata-only retrieval rows of
the attempt's memory roots. It reads them from the process's resident memory
listing (``lib.memory.storage.resident_eligible_memories``), so obtaining the
corpus never walks a store directory on the startup path once the process holds
or has persisted its snapshot.

Freshness: ``lib/memory/storage/_resident_listing.py`` (docs/DEFENSIVE_PATTERNS.md,
"Startup reads a resident memory listing").

Entry points: ``list_prefetch_corpus``.
Dependencies: ``lib.memory.storage`` only.
"""

from __future__ import annotations


def list_prefetch_corpus(project_path, extra_paths, details=None):
    """Eligible metadata-only retrieval rows the prefetch ranks (read-only).

    ``details`` receives how the listing was obtained (``listing``,
    ``snapshotAgeMs``) for the ``memory_list`` startup anchor.
    """
    from lib.memory.storage import resident_eligible_memories

    return resident_eligible_memories(
        project_path, extra_paths=extra_paths, details=details)


__all__ = ['list_prefetch_corpus']
