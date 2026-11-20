"""Build an owner-consented Codex baseline in a caller-owned CAS candidate.

The application command fetches a fresh catalog for the exact bearer account
and commits an account-scoped opt-in at the supplied owner revision. Baseline
and opt-out never change credential grants.
"""
from lib.model_routing.codex_grant_history import (
    GrantHistory, ModelIdentity, Observation, establish_codex_baseline,
)
from lib.model_routing.domain import ModelRoutingError


def _at_revision(repository, boundary, expected_revision):
    current = repository.get(boundary)
    if type(expected_revision) is not int or expected_revision != current.revision:
        raise ModelRoutingError('model-routing revision has changed', kind='model_routing_revision_conflict')
    return current


def establish_owner_codex_baseline(repository, boundary, *, expected_revision: int):
    """Opt in explicitly to future discoveries; never grant models at baseline."""
    from copy import deepcopy

    from lib.llm.llm_errors import SubscriptionCredentialUnavailableError, SubscriptionLoginRequiredError
    from lib.oauth.codex_catalog import fetch_codex_registration_evidence

    current = _at_revision(repository, boundary, expected_revision)
    try:
        evidence = fetch_codex_registration_evidence(boundary, expected_revision=expected_revision)
    except (RuntimeError, ValueError, OSError, SubscriptionCredentialUnavailableError,
            SubscriptionLoginRequiredError) as exc:
        raise ModelRoutingError('Authenticated Codex catalog is unavailable', kind='codex_baseline_unavailable') from exc
    if (evidence is None or evidence.boundary != boundary
            or evidence.observed_revision != expected_revision):
        raise ModelRoutingError('Authenticated Codex catalog is unavailable', kind='codex_baseline_unavailable')
    # Network wait must not make an idempotent opt-in answer for a stale revision.
    current = _at_revision(repository, boundary, expected_revision)
    candidate = deepcopy(current.document)
    baseline_codex_history(candidate, account_fingerprint=evidence.account_fingerprint,
                           catalog_rows=[{'slug': wire} for wire in sorted(evidence.wires)],
                           preserve_complete=True)
    if candidate == current.document:
        return current
    return repository.compare_and_swap(boundary, candidate, expected_revision=expected_revision)


def disable_owner_codex_registration(repository, boundary, *, expected_revision: int):
    """Stop future automatic grants for every account without rewriting past decisions."""
    from copy import deepcopy

    current = _at_revision(repository, boundary, expected_revision)
    candidate = deepcopy(current.document)
    for history in candidate.get('codex_seen_history', ()):
        history.update(complete=False, pending_wire_model_ids=[])
    if candidate == current.document:
        return current
    return repository.compare_and_swap(boundary, candidate, expected_revision=expected_revision)


def baseline_codex_history(document, *, account_fingerprint, catalog_rows,
                           preserve_complete=False):
    """Record all available wires, mapped identities and retained tombstones.

    ``catalog_rows`` must be the nonempty, authenticated same-account catalog;
    a static fallback or an account-less/stale source is not evidence. Existing
    histories for all accounts remain intact. A rebaseline only unions evidence.
    """
    if (not isinstance(account_fingerprint, str) or len(account_fingerprint) != 64
            or any(c not in '0123456789abcdef' for c in account_fingerprint)):
        raise ModelRoutingError('Codex account identity is unavailable', kind='codex_baseline_unavailable')
    if not isinstance(catalog_rows, (list, tuple)) or not catalog_rows:
        raise ModelRoutingError('Codex catalog is unavailable', kind='codex_baseline_unavailable')
    wires = []
    for row in catalog_rows:
        wire = row.get('slug') if isinstance(row, dict) else None
        if not isinstance(wire, str) or not wire.strip() or wire != wire.strip():
            raise ModelRoutingError('Codex catalog is invalid', kind='codex_baseline_unavailable')
        wires.append(wire)
    accesses = {r['provider_access_id'] for r in document['provider_accesses']
                if r['provider_id'] == 'oauth_codex'}
    credentials = [r for r in document['credentials'] if r['provider_access_id'] in accesses and r['kind'] == 'oauth']
    if not credentials:
        raise ModelRoutingError('Codex credential is unavailable', kind='codex_baseline_unavailable')
    histories = document.setdefault('codex_seen_history', [])
    for credential in credentials:
        access = credential['provider_access_id']
        credential_id = credential['credential_id']
        matching = [h for h in histories if h['provider_access_id'] == access
                    and h['credential_id'] == credential_id
                    and h['account_fingerprint'] == account_fingerprint]
        if len(matching) > 1:
            raise ModelRoutingError('duplicate Codex history', kind='codex_baseline_unavailable')
        history = matching[0] if matching else None
        if preserve_complete and history and history['complete']:
            continue
        seen = GrantHistory(
            history['complete'] if history else False,
            frozenset(history['wire_model_ids']) if history else frozenset(),
            frozenset(ModelIdentity(m['creator_id'], m['model_id']) for m in history['models']) if history else frozenset(),
            frozenset(history['pending_wire_model_ids']) if history else frozenset(),
        )
        observations = [Observation(w, None) for w in wires]
        offerings = {r['offering_id']: r for r in document['offerings'] if r['provider_access_id'] == access}
        for deployment in document['deployments']:
            offering = offerings.get(deployment['offering_id'])
            if not offering:
                continue
            model = offering.get('model')
            identity = ModelIdentity(model['creator_id'], model['model_id']) if model else None
            observations.append(Observation(deployment['wire_model_id'], identity))
        # A deleted credential's denials also survive credential replacement.
        # Their original account is unknowable, so conservatively include all.
        for tombstone in document.get('revocation_history', []):
            identity = ModelIdentity(tombstone['creator_id'], tombstone['model_id'])
            # Model tombstones can outlive every deployment and wire.
            seen = GrantHistory(seen.complete, seen.wires, seen.models | {identity}, seen.pending_wires)
            observations.extend(Observation(w, identity) for w in tombstone['excluded_wire_model_ids'])
        baseline = establish_codex_baseline(seen, tuple(observations))
        if history is None:
            history = {'provider_access_id': access, 'credential_id': credential_id,
                       'account_fingerprint': account_fingerprint}
            histories.append(history)
        history.update(complete=True, wire_model_ids=sorted(baseline.wires),
                       models=[{'creator_id': m.creator_id, 'model_id': m.model_id}
                               for m in sorted(baseline.models)], pending_wire_model_ids=[])
    return document
