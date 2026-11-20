"""Per-Turn deliverable facts of a lead conversation, kept exact by the authority's Turn delta.

The deliverables projection (``lead_deliverables.py``) needs only a few facts of
each main-lane Turn: the canonical report it is, the ``report_to_user`` receipts
its reply carries, the artifact ids its rounds name and the relay notices its
texts carry. Deriving them on every read walked every Turn's whole projection:
the tofu lead (158 Turns) shipped 49 MB through one ``turn.sync.page`` per Turn,
and scanned every string of it, for each board summary and each expanded task
(measured 1-18 s per read on 2026-11-18, docs/PROJECT_COORDINATION.md, "Lead
deliverables").

This module derives each Turn's facts once and keeps them current with exact
invalidation by the authority: a later read sends the projection revision of
every Turn it holds to ``turn.list_delta`` (``turn_lifecycle.turns_changed_since``),
which returns only the Turns updated since the previous read's watermark whose
revision advanced, and the Turns deleted since (tombstones). Those Turns are
derived again; nothing else is read. An entry is rebuilt by the lane walk when
there is none, when the authority refuses the delta at its row or response
bound, and when it was last validated longer ago than the authority keeps
tombstones (``TURN_TOMBSTONE_RETENTION_SECONDS``): a deletion older than that
can no longer be seen. Entries are reconstructible; memory-pressure relief
drops them with every ``lib.ttl_cache`` cache.

Entry point: LeadTurnFacts.read. Dependencies: lib.turn_lifecycle (lane walk,
Turn delta), lib.ttl_cache, lib.weak_lock_pool. Owner identity is explicit.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import threading
import time
from typing import Any

from lib.storage.errors import StorageError, is_resource_bound_refusal
from lib.ttl_cache import TTLCache
from lib.weak_lock_pool import WeakLockPool

# Both authorities prune Turn tombstones after seven days (SQL
# ``_turns_read._TOMBSTONE_RETENTION_MS``, native ``turn.rs`` TOMBSTONE_RETENTION_MS).
TURN_TOMBSTONE_RETENTION_SECONDS = 7 * 24 * 60 * 60

Facts = Mapping[str, Any]


@dataclass(frozen=True)
class _Entry:
    watermark_ms: int
    facts: Mapping[str, Facts]  # turnId -> facts, main lane only


def _walk(conversation_id: str, owner: int) -> Iterable[Mapping[str, Any]]:
    from lib.turn_lifecycle import lane_turns_newest_first
    return (row for row, _attempt in lane_turns_newest_first(conversation_id, user_id=owner))


def _delta(conversation_id: str, owner: int, since_ms: int, known: Mapping[str, int]) -> Mapping[str, Any]:
    from lib.turn_lifecycle import turns_changed_since
    return turns_changed_since(conversation_id, user_id=owner, since_ms=since_ms, known_revisions=known)


def _main_lane(turn: Mapping[str, Any]) -> bool:
    return (turn.get('laneId') or 'main') == 'main'


class LeadTurnFacts:
    """Facts of each main-lane Turn of a conversation, newest first, derived by ``derive``.

    ``derive(turn)`` must return a mapping carrying the Turn's ``turnId``,
    ``ordinal`` and ``revision`` (its ``projectionRevision``).
    """

    def __init__(self, derive: Callable[[Mapping[str, Any]], Facts], *,
                 walk: Callable[[str, int], Iterable[Mapping[str, Any]]] = _walk,
                 delta: Callable[[str, int, int, Mapping[str, int]], Mapping[str, Any]] = _delta,
                 now_ms: Callable[[], int] = lambda: int(time.time() * 1000)) -> None:
        self._derive = derive
        self._walk = walk
        self._delta = delta
        self._now_ms = now_ms
        self._entries = TTLCache(TURN_TOMBSTONE_RETENTION_SECONDS, name='lead_turn_facts')
        self._locks: WeakLockPool[tuple[int, str], Any] = WeakLockPool(threading.Lock)

    def read(self, owner: int, conversation_id: str) -> list[Facts]:
        key = (owner, conversation_id)
        # One reader per conversation: a board's summary and task reads arrive together.
        with self._locks.lock_for(key):
            entry = self._entries.get(key)
            entry = self._advance(owner, conversation_id, entry) if entry is not None else None
            if entry is None:
                entry = self._build(owner, conversation_id)
            self._entries.set(key, entry)
        return sorted(entry.facts.values(), key=lambda facts: -(facts.get('ordinal') or 0))

    def _advance(self, owner: int, conversation_id: str, entry: _Entry) -> _Entry | None:
        known = {turn_id: facts['revision'] for turn_id, facts in entry.facts.items()
                 if type(facts.get('revision')) is int}
        try:
            delta = self._delta(conversation_id, owner, entry.watermark_ms, known)
        except StorageError as exc:
            if not is_resource_bound_refusal(exc):
                raise
            return None
        facts = dict(entry.facts)
        for turn_id in delta['deletedTurnIds']:
            facts.pop(turn_id, None)
        for turn in delta['turns']:
            if _main_lane(turn):
                facts[turn['turnId']] = self._derive(turn)
            else:
                facts.pop(turn['turnId'], None)
        return _Entry(delta['serverNowMs'], facts)

    def _build(self, owner: int, conversation_id: str) -> _Entry:
        # The watermark precedes the walk, so a Turn changed while it runs is in the next delta.
        watermark = self._delta(conversation_id, owner, self._now_ms(), {})['serverNowMs']
        facts: dict[str, Facts] = {}
        for turn in self._walk(conversation_id, owner):
            if _main_lane(turn):
                facts.setdefault(turn['turnId'], self._derive(turn))
        return _Entry(watermark, facts)

