"""Codex refresh never auto-grants or advances retained history."""
import copy
import hashlib

import pytest

from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary
from lib.oauth import outbound

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('mode', ['complete', 'legacy', 'different_account', 'deny'])
def test_upsert_preserves_authorization_and_history_on_catalog_change(monkeypatch, mode):
    models = [{'model_id': 'gpt-5.5', 'capabilities': ['text']}]
    fingerprint = hashlib.sha256(b'account').hexdigest()
    monkeypatch.setattr('lib.oauth.token_store.load_token', lambda *a: {'account_id': 'account', 'access_token': 'token'})
    monkeypatch.setattr('lib.oauth.codex_catalog.cached_codex_provider_models', lambda: copy.deepcopy(models))
    monkeypatch.setattr('lib.oauth.codex_catalog._read_cache', lambda: {'account_fingerprint': fingerprint[:16], 'models': [{'slug': m['model_id']} for m in models]})
    monkeypatch.setattr('lib.llm_dispatch.reset_dispatcher', lambda: None)
    repo = InMemoryModelRoutingRepository()
    owner = OwnerBoundary.create(41)
    def provision():
        return outbound.provision_oauth_provider('codex', owner_user_id=41, repository=repo)
    provision()
    current = repo.get(owner)
    doc = current.document
    credential = doc['credentials'][0]
    access = credential['provider_access_id']
    initial_model = credential['authorization']['models'][0]
    wire = doc['deployments'][0]['wire_model_id']
    credential['authorization']['models'] = []
    doc['codex_seen_history'] = [{'provider_access_id': access, 'credential_id': credential['credential_id'],
        'account_fingerprint': fingerprint, 'complete': True, 'wire_model_ids': [wire],
        'models': [initial_model], 'pending_wire_model_ids': []}]
    if mode == 'legacy':
        doc['codex_seen_history'][0]['complete'] = False
    if mode == 'different_account':
        doc['codex_seen_history'][0]['account_fingerprint'] = 'a' * 64
    if mode == 'deny':
        doc['revocation_history'] = [{'credential_id': credential['credential_id'],
            'creator_id': 'openai', 'model_id': 'gpt-6-astra', 'model_revoked': True,
            'excluded_wire_model_ids': []}]
    repo.compare_and_swap(owner, doc, expected_revision=current.revision)
    before = repo.get(owner).document
    models.append({'model_id': 'gpt-6-astra', 'capabilities': ['text']})
    provision()
    result = repo.get(owner).document
    assert result['credentials'][0]['authorization'] == before['credentials'][0]['authorization']
    assert result['codex_seen_history'] == before['codex_seen_history']
    assert result.get('revocation_history', []) == before.get('revocation_history', [])
    assert not provision()
