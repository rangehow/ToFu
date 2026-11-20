"""Reconcile an owner's current-day usage projections against storage metadata.

Only changed conversations are hydrated. A second metadata read fences newly
read projections; PostgreSQL usage reads can span multiple READ COMMITTED
statements. An unstable read still contributes to the current scan (as on the
ordinary live scan), but cannot seed a reusable projection or total.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .repository import ConversationRepository, ConversationTurnUsage

_ATTRIBUTION = ('model', 'preset', 'effort')
_HEADER = ('title', 'created_at', 'updated_at')


def _header(metadata: Mapping[str, Any]) -> tuple:
    settings = metadata.get('settings') or {}
    return (*(metadata.get(key) for key in _HEADER), *(settings.get(key) for key in _ATTRIBUTION))


def _witness(metadata: Mapping[str, Any]) -> tuple:
    # Header-only title/model updates intentionally do not advance transcript rev.
    return (metadata.get('rev'), metadata.get('msg_count'), *_header(metadata))


@dataclass(frozen=True, slots=True)
class DayUsageSnapshot:
    client: Any
    scope: tuple[int, int, int, int, int]
    candidate_count: int
    reusable: dict[str, tuple[tuple, ConversationTurnUsage]]
    projections: tuple[ConversationTurnUsage, ...]


def reconcile_day_usage(*, owner_user_id: int, start_ms: int, end_ms: int,
                        limit: int, title_characters: int,
                        previous: DayUsageSnapshot | None) -> DayUsageSnapshot:
    """Read current candidates and return the same snapshot if none changed."""
    from lib.storage import get_storage_client
    client = get_storage_client()
    repository = ConversationRepository(client_factory=lambda **_: client)
    scope = (owner_user_id, start_ms, end_ms, limit, title_characters)
    retained = (previous.reusable if previous and previous.client is client
                and previous.scope == scope else {})

    def catalog():
        rows = repository.list(user_id=owner_user_id, updated_at_gte=start_ms,
                               created_at_lt=end_ms, include_messages=False,
                               settings_keys=_ATTRIBUTION, limit=limit)
        return {row['id']: _witness(row.metadata) for row in rows}

    before = catalog()
    gathered: dict[str, ConversationTurnUsage] = {}
    reusable = {}
    for conv_id, witness in before.items():
        cached = retained.get(conv_id)
        if cached is not None and cached[0] == witness:
            reusable[conv_id] = cached
            gathered[conv_id] = cached[1]
            continue
        projection = repository.turn_usage(conv_id, user_id=owner_user_id,
                                           timestamp_gte=start_ms, timestamp_lt=end_ms,
                                           title_fallback_characters=title_characters)
        if projection is not None:
            gathered[conv_id] = projection

    if (previous is not None and retained is previous.reusable
            and len(reusable) == len(before) == previous.candidate_count):
        return previous
    if gathered.keys() - reusable.keys():
        after = catalog()
        for conv_id, projection in gathered.items():
            witness = before[conv_id]
            if (conv_id not in reusable and type(witness[0]) is int
                    and after.get(conv_id) == witness and _header(projection.metadata) == witness[2:]
                    and projection.message_count == witness[1]):
                reusable[conv_id] = (witness, projection)
    return DayUsageSnapshot(client, scope, len(before), reusable, tuple(gathered.values()))
