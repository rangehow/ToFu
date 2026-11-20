"""Record owner-authored grant edits independently of live provider resources.

Entry point: record_explicit_revocations. Call only for explicit owner grant
replacement, never for discovery or provider deletion.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from .domain import ModelRoutingError


_SCOPED_GRANTS = 'codex_account_grants'


def _scoped_grant_keys(accounts):
    return {(account, row['creator_id'], row['model_id'], row['wire_model_id'])
            for account, rows in accounts.items() for row in rows}


def _preserve_scoped_grants(previous, candidate):
    """Allow explicit narrowing, not client creation or reassignment.

    Older clients can omit the field without removing existing grants. A
    supplied subset restricts the exact wire on the credential across accounts;
    dropping a credential also persists its existing scoped wire denials.
    """
    old = {row['credential_id']: row for row in previous.get('credentials', [])}
    new = {row['credential_id']: row for row in candidate.get('credentials', [])}
    removals = set()
    for credential_id in old.keys() | new.keys():
        before, after = old.get(credential_id), new.get(credential_id)
        stored = before['authorization'].get(_SCOPED_GRANTS, {}) if before else {}
        requested = after['authorization'].get(_SCOPED_GRANTS) if after else {}
        same_access = before and after and before.get('provider_access_id') == after.get('provider_access_id')
        if requested is None and same_access:
            if stored:
                after['authorization'][_SCOPED_GRANTS] = copy.deepcopy(stored)
            requested = stored
        old_keys = _scoped_grant_keys(stored)
        new_keys = _scoped_grant_keys(requested or {})
        if new_keys - (old_keys if same_access else set()):
            raise ModelRoutingError('Account-bound Codex grants may only be narrowed',
                                    field=f'credentials.authorization.{_SCOPED_GRANTS}')
        removals.update((credential_id, creator, model, wire)
                        for _, creator, model, wire in old_keys - new_keys)
    return removals


def preserve_seen_history(previous: Mapping[str, Any], candidate: dict[str, Any]) -> None:
    """Do not accept client history; cancel reservations no longer reachable."""
    if 'codex_seen_history' not in previous:
        candidate.pop('codex_seen_history', None)
        return
    histories = copy.deepcopy(previous['codex_seen_history'])
    credentials = {row['credential_id']: row for row in candidate.get('credentials', [])}
    offerings = {row['offering_id']: row for row in candidate.get('offerings', [])}
    for row in histories:
        credential = credentials.get(row['credential_id'])
        connections = set()
        if credential and credential.get('provider_access_id') == row['provider_access_id']:
            connections = set(credential['authorization']['connection_ids'])
        reachable = {
            dep['wire_model_id'] for dep in candidate.get('deployments', [])
            if dep.get('connection_id') in connections
            and offerings.get(dep.get('offering_id'), {}).get('provider_access_id') == row['provider_access_id']
        }
        row['pending_wire_model_ids'] = sorted(set(row['pending_wire_model_ids']) & reachable)
    candidate['codex_seen_history'] = histories


def record_explicit_revocations(previous: Mapping[str, Any], candidate: dict[str, Any]) -> None:
    """Derive history from normalized owner replacements, never client history."""
    preserve_seen_history(previous, candidate)
    scoped_removals = _preserve_scoped_grants(previous, candidate)

    def grants(document):
        return {
            (credential['credential_id'], grant['creator_id'], grant['model_id']):
                set(grant.get('excluded_wire_model_ids', []))
            for credential in document.get('credentials', [])
            for grant in credential['authorization']['models']
        }

    old, new = grants(previous), grants(candidate)
    history = {
        (row['credential_id'], row['creator_id'], row['model_id']): {
            'model_revoked': row['model_revoked'],
            'excluded_wire_model_ids': set(row['excluded_wire_model_ids']),
        }
        for row in previous.get('revocation_history', [])
    }
    for key in old.keys() | new.keys():
        before, after = old.get(key), new.get(key)
        if before == after:
            continue
        row = history.setdefault(key, {'model_revoked': False, 'excluded_wire_model_ids': set()})
        if after is None:
            row['model_revoked'] = True
            row['excluded_wire_model_ids'].update(before)
        elif before is None:
            row['model_revoked'] = False
            row['excluded_wire_model_ids'] = set(after)
        else:
            row['excluded_wire_model_ids'].difference_update(before - after)
            row['excluded_wire_model_ids'].update(after - before)
        if not row['model_revoked'] and not row['excluded_wire_model_ids']:
            del history[key]
    for credential, creator, model, wire in scoped_removals:
        key = credential, creator, model
        row = history.setdefault(key, {'model_revoked': False, 'excluded_wire_model_ids': set()})
        row['excluded_wire_model_ids'].add(wire)
    candidate['revocation_history'] = [
        {'credential_id': key[0], 'creator_id': key[1], 'model_id': key[2],
         'model_revoked': row['model_revoked'],
         'excluded_wire_model_ids': sorted(row['excluded_wire_model_ids'])}
        for key, row in sorted(history.items())
    ]
