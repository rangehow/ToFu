"""Advance an opted-in Codex account's history and exact wire grants under CAS.

Inputs are runtime-only live evidence and an already admitted candidate for one
managed ProviderAccess. No token-store reads, locks, or cache are authoritative.
"""
from lib.model_routing.codex_grant_history import (
    GrantHistory, ModelIdentity, Observation, plan_codex_grants,
)


def has_complete_codex_history(document, fingerprint):
    return any(row['complete'] and row['account_fingerprint'] == fingerprint
               for row in document.get('codex_seen_history', ()))


def _identity(row):
    return ModelIdentity(row['creator_id'], row['model_id'])


def _observations(document, access, credential, evidence, reserved):
    """Distinguish reservable unknowns from ambiguous or denied supply."""
    from lib.model_catalog._creator_identity import creator_family, strip_routing_decoration

    offerings = {row['offering_id']: row for row in document['offerings']
                 if row['provider_access_id'] == access['provider_access_id']}
    deployments = {row['wire_model_id']: row for row in document['deployments']
                   if row['offering_id'] in offerings}
    connections = {row['connection_id'] for row in document['connections'] if row['enabled']}
    connections.intersection_update(credential['authorization']['connection_ids'])
    models_by_id = {}
    for row in document['models']:
        models_by_id.setdefault(row['model_id'], []).append(row)
    # Retained tombstones from removed/replaced credentials are conservative
    # denials too: their original provider account is no longer knowable.
    denied_models = {_identity(row) for row in document.get('revocation_history', ())
                     if row['model_revoked']}
    denied_wires = {wire for row in document.get('revocation_history', ())
                    for wire in row['excluded_wire_model_ids']}
    observations = []
    eligible = {}
    for wire in sorted(evidence.wires):
        deployment = deployments.get(wire)
        offering = offerings[deployment['offering_id']] if deployment else {}
        available = bool(deployment and access['enabled'] and credential['enabled']
                         and offering['enabled'] and not offering['stale']
                         and deployment['connection_id'] in connections
                         and wire in evidence.visible_wires and wire not in denied_wires
                         and ModelIdentity('openai', wire) not in denied_models)
        ref = _identity(offering['model']) if offering.get('model') else None
        exact = ModelIdentity('openai', wire)
        matches = models_by_id.get(wire, ())
        if ref == exact and len(matches) == 1:
            ready = (matches[0].get('registration_state') == 'ready'
                     and deployment['identity_confidence'] == 'high'
                     and not deployment.get('registration_issues'))
            if ready and available:
                eligible[ref] = wire
            if not ready and available and wire in reserved:
                # Binding may finish before provider inference validation. Do
                # not consume existing consent until the deployment is ready.
                observations.append(Observation(wire, None, reservable_pending=True))
                continue
        if ref is None:
            pending = (available and offering.get('pending_model_id') == wire and not matches
                       and creator_family(wire) == 'openai' and strip_routing_decoration(wire) == wire)
            observations.append(Observation(wire, None, reservable_pending=pending))
        else:
            observations.append(Observation(wire, ref))
    return tuple(observations), eligible


def apply_codex_history(previous, candidate, evidence):
    """Update the caller-owned admitted CAS candidate; never widen unscoped grants."""
    fingerprint = evidence.account_fingerprint
    accesses = {row['provider_access_id']: row for row in candidate['provider_accesses']
                if row['provider_id'] == 'oauth_codex'}
    credentials = {row['credential_id']: row for row in candidate['credentials']
                   if row['provider_access_id'] in accesses and row['kind'] == 'oauth'}
    previous_credentials = {(row['credential_id'], row['provider_access_id'])
                            for row in previous['credentials']}
    for history in candidate.get('codex_seen_history', ()):
        if not history['complete'] or history['account_fingerprint'] != fingerprint:
            continue
        credential_id, access_id = history['credential_id'], history['provider_access_id']
        credential = credentials.get(credential_id)
        if (not credential or credential['provider_access_id'] != access_id
                or (credential_id, access_id) not in previous_credentials):
            # A replaced/deleted credential cannot carry forward pending consent.
            history['pending_wire_model_ids'] = []
            continue
        authorization = credential['authorization']
        scoped = authorization.get('codex_account_grants', {}).get(fingerprint, ())
        grants = frozenset(_identity(row) for row in (*authorization['models'], *scoped))
        reserved = frozenset(history['pending_wire_model_ids']) & evidence.wires
        observations, eligible = _observations(candidate, accesses[access_id], credential, evidence, reserved)
        seen = GrantHistory(True, frozenset(history['wire_model_ids']),
                            frozenset(_identity(row) for row in history['models']), reserved)
        plan = plan_codex_grants(seen, grants, observations)
        for model in sorted(plan.grants - grants):
            wire = eligible.get(model)
            if wire:
                authorization.setdefault('codex_account_grants', {}).setdefault(fingerprint, []).append(
                    {'creator_id': model.creator_id, 'model_id': model.model_id, 'wire_model_id': wire})
        history['wire_model_ids'] = sorted(plan.history.wires)
        history['models'] = [{'creator_id': m.creator_id, 'model_id': m.model_id}
                             for m in sorted(plan.history.models)]
        history['pending_wire_model_ids'] = sorted(plan.history.pending_wires)
