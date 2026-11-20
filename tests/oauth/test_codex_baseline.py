"""The explicit baseline preserves denials and does not change grants."""
import copy
import hashlib

import pytest

from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary
from lib.model_routing.domain import ModelRoutingError
from lib.oauth import outbound
from lib.oauth.codex_baseline import baseline_codex_history

pytestmark = pytest.mark.unit


def test_baseline_unions_catalog_deployments_and_tombstones_without_grants(monkeypatch):
    monkeypatch.setattr('lib.llm_dispatch.reset_dispatcher', lambda: None)
    repo = InMemoryModelRoutingRepository()
    owner = OwnerBoundary.create(41)
    outbound.provision_oauth_provider('codex', owner_user_id=41, repository=repo)
    authority = repo.get(owner)
    doc = authority.document
    cred = doc['credentials'][0]
    cid = cred['credential_id']
    doc['revocation_history'] = [{'credential_id': cid, 'creator_id': 'openai',
        'model_id': 'removed', 'model_revoked': True, 'excluded_wire_model_ids': ['old-wire']},
        {'credential_id': 'deleted-credential', 'creator_id': 'openai',
         'model_id': 'old-model', 'model_revoked': True, 'excluded_wire_model_ids': ['deleted-wire']}]
    auth = copy.deepcopy(cred['authorization'])
    fingerprint = hashlib.sha256(b'account').hexdigest()
    baseline_codex_history(doc, account_fingerprint=fingerprint,
                           catalog_rows=[{'slug': 'unmapped-wire'}])
    stored = repo.compare_and_swap(owner, doc, expected_revision=authority.revision).document
    history = stored['codex_seen_history'][0]
    assert history['complete']
    assert {'unmapped-wire', 'old-wire', 'deleted-wire'} <= set(history['wire_model_ids'])
    assert {d['wire_model_id'] for d in doc['deployments']} <= set(history['wire_model_ids'])
    assert {'creator_id': 'openai', 'model_id': 'removed'} in history['models']
    assert {'creator_id': 'openai', 'model_id': 'old-model'} in history['models']
    assert stored['credentials'][0]['authorization'] == auth
    before = copy.deepcopy(history)
    baseline_codex_history(stored, account_fingerprint=fingerprint, catalog_rows=[{'slug': 'later'}])
    after = stored['codex_seen_history'][0]
    assert set(before['wire_model_ids']) <= set(after['wire_model_ids'])
    assert all(m in after['models'] for m in before['models'])
    assert stored['credentials'][0]['authorization'] == auth


@pytest.mark.parametrize('fingerprint,rows', [('', [{'slug': 'a'}]), ('a' * 64, []), ('a' * 64, [{'slug': ''}])])
def test_baseline_rejects_missing_evidence(fingerprint, rows):
    with pytest.raises(ModelRoutingError):
        baseline_codex_history({}, account_fingerprint=fingerprint, catalog_rows=rows)
