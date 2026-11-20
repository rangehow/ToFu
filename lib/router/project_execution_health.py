"""Bounded, metadata-only execution evidence for the Router project directory.

Entry points: execution_health(owner, conversation_id, *, client),
execution_health_wire(facts). Uses the storage authority's bound-attempt
catalogue and owned Turn metadata. This is display evidence, not a repair gate.
"""
from __future__ import annotations

from collections.abc import Mapping

from lib.storage.errors import StorageError
from lib.turn_verdict import DISPATCH_AUTHORITY_ABORT_REASONS

from .contract import validate

_WIRE_NAMES = {'state': 'state', 'turn_id': 'turnId', 'status': 'status',
               'authority_withdrawn': 'authorityWithdrawn'}


def execution_health_wire(facts: dict) -> dict:
    return {_WIRE_NAMES[key]: value for key, value in facts.items()}


def _observed_health(storage, identity: dict, records: list) -> dict | None:
    for row in records:
        turn = storage.query('turn.get', {**identity, 'turn_id': row['turn_id'],
                                         'include_projection': False})
        if (not isinstance(turn, Mapping) or turn.get('turnId') != row['turn_id']
                or turn.get('conversationId') != identity['conversation_id']):
            raise ValueError('Attempt catalogue Turn no longer readable')
        if turn['laneId'] != 'main' or turn.get('currentAttemptId') != row['attempt_id']:
            continue
        cause = (turn.get('settlement') or {}).get('cause')
        facts = {'state': 'observed', 'turn_id': turn['turnId'], 'status': turn['status'],
                 'authority_withdrawn': isinstance(cause, str) and cause in DISPATCH_AUTHORITY_ABORT_REASONS}
        validate(execution_health_wire(facts), 'projectExecutionHealth')
        return facts
    return None


def execution_health(owner: int, conversation_id: str, *, client) -> dict:
    """Inspect one default catalogue page, never chase its history cursor."""
    identity = {'user_id': owner, 'conversation_id': conversation_id}
    try:
        storage = client()
        page = storage.query('turn.timing_trace.list', identity)
        if not isinstance(page['records'], list) or type(page['has_more']) is not bool:
            return {'state': 'unknown'}
        observed = _observed_health(storage, identity, page['records'])
        if observed is not None:
            return observed
        return {'state': 'unknown' if page['has_more'] else 'no_bound_attempt'}
    except (StorageError, PermissionError, ValueError, KeyError, TypeError, AttributeError):
        # Do not expose exception text, settlement detail, settings or transcript.
        return {'state': 'unknown'}
