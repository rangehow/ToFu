"""Codex logout/relogin must preserve explicit owner denials, not remint them."""
import copy

import pytest

from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary
from lib.model_routing.revocation_history import record_explicit_revocations
from lib.oauth import outbound
from lib.oauth.manager import _exchange

pytestmark = pytest.mark.unit


def _grants(credential):
    return {(row['creator_id'], row['model_id']): row.get('excluded_wire_model_ids', [])
            for row in credential['authorization']['models']}


@pytest.fixture
def oauth_authority(monkeypatch):
    repo = InMemoryModelRoutingRepository()
    owner = OwnerBoundary.create(41)
    monkeypatch.setattr('lib.model_routing.ModelRoutingRepository', lambda: repo)
    monkeypatch.setattr('lib.llm_dispatch.reset_dispatcher', lambda: None)
    monkeypatch.setattr('lib.oauth.token_store.load_token', lambda _provider: {})
    monkeypatch.setattr('lib.oauth.token_store.delete_token', lambda _provider: True)
    monkeypatch.setattr('lib.oauth.manager._device.stop_device_flow', lambda _provider: None)
    monkeypatch.setattr('lib.oauth.codex_catalog.stop_codex_catalog_refresher', lambda: None)
    monkeypatch.setattr('lib.oauth.codex_catalog.forget_pending_codex_catalog', lambda: None)
    monkeypatch.setattr('lib.subscription_quota.clear_subscription_quota', lambda *a, **kw: None)
    monkeypatch.setattr(_exchange, 'audit_log', lambda *a, **kw: None)
    monkeypatch.setattr(_exchange, '_active_flows', {})
    monkeypatch.setattr(_exchange, '_active_servers', {})
    return repo, owner


def _tombstone(credential_id, creator_id, model_id):
    return {'credential_id': credential_id, 'creator_id': creator_id, 'model_id': model_id,
            'model_revoked': True, 'excluded_wire_model_ids': []}


def test_codex_relogin_filters_only_exact_revocations(oauth_authority, monkeypatch):
    repo, owner = oauth_authority
    revoked, narrowed, unaffected = 'gpt-5.5', 'gpt-6-astra', 'gpt-6.1-sol'
    models = [{'model_id': model, 'capabilities': ['text']}
              for model in (revoked, narrowed, unaffected)]
    monkeypatch.setattr('lib.oauth.codex_catalog.cached_codex_provider_models',
                        lambda: copy.deepcopy(models))
    args = {'owner_user_id': owner.owner_user_id}

    # First login still initializes the legacy-import grants.
    assert outbound.provision_oauth_provider('codex', **args)
    initial = repo.get(owner)
    credential = initial.document['credentials'][0]
    credential_id = credential['credential_id']
    assert _grants(credential) == {('openai', model): [] for model in (revoked, narrowed, unaffected)}
    connections = credential['authorization']['connection_ids']
    assert connections

    candidate = copy.deepcopy(initial.document)
    grants = candidate['credentials'][0]['authorization']['models']
    grants[:] = [row for row in grants if row['model_id'] != revoked]
    next(row for row in grants if row['model_id'] == narrowed)['excluded_wire_model_ids'] = [
        narrowed, 'previous-provider-wire']
    record_explicit_revocations(initial.document, candidate)
    # Another credential, another creator, or a similarly named model must not match.
    candidate['revocation_history'].extend([
        _tombstone('different-credential', 'openai', unaffected),
        _tombstone(credential_id, 'different-creator', unaffected),
        _tombstone(credential_id, 'openai', f'{unaffected}-preview'),
    ])
    before = repo.compare_and_swap(owner, candidate, expected_revision=initial.revision).document
    expected = {('openai', narrowed): [narrowed, 'previous-provider-wire'], ('openai', unaffected): []}
    assert not outbound.provision_oauth_provider('codex', **args)
    assert _grants(repo.get(owner).document['credentials'][0]) == expected

    assert _exchange.logout_oauth('codex', **args)['ok']
    logged_out = repo.get(owner).document
    assert not logged_out['credentials']
    assert logged_out['revocation_history'] == before['revocation_history']

    assert outbound.provision_oauth_provider('codex', **args)
    recreated = repo.get(owner).document
    credential = recreated['credentials'][0]
    assert credential['credential_id'] == credential_id
    assert credential['authorization']['connection_ids'] == connections
    assert _grants(credential) == expected
    assert recreated['revocation_history'] == before['revocation_history']
    assert not outbound.provision_oauth_provider('codex', **args)


def test_non_codex_recreation_keeps_existing_import_policy(oauth_authority):
    repo, owner = oauth_authority
    args = {'owner_user_id': owner.owner_user_id}
    assert outbound.provision_oauth_provider('claude', **args)
    initial = repo.get(owner)
    candidate = copy.deepcopy(initial.document)
    credential = candidate['credentials'][0]
    expected = _grants(credential)
    assert expected
    credential['authorization']['models'].pop()
    record_explicit_revocations(initial.document, candidate)
    changed = repo.compare_and_swap(owner, candidate, expected_revision=initial.revision)
    assert changed.document['revocation_history'][0]['model_revoked']

    assert _exchange.logout_oauth('claude', **args)['ok']
    assert outbound.provision_oauth_provider('claude', **args)
    recreated = repo.get(owner).document
    assert _grants(recreated['credentials'][0]) == expected
    assert recreated['revocation_history'] == changed.document['revocation_history']
