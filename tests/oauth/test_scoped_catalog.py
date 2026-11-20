"""Clean owner supply only from explicit first-party account evidence."""
import time
from types import SimpleNamespace

import pytest

from lib.model_routing.domain import ModelRef, NativeModelSelection
from lib.model_routing.candidate_compiler import compile_candidates
from lib.model_routing.repository import InMemoryModelRoutingRepository, OwnerBoundary
from lib.oauth.accounts import SubscriptionAccounts
from lib.oauth.codex import account_fingerprint
from lib.oauth.codex_catalog import CodexRegistrationEvidence, _normalise_models
from lib.oauth.scoped_catalog import admit_initial_codex_supply

pytestmark = pytest.mark.unit


def test_clean_owner_admits_observed_maintained_model(monkeypatch):
    repo = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(7, 'fixture')
    service = SubscriptionAccounts(repo)
    account = service.complete(service.begin(boundary, 'codex', 0), verified_subject='A',
                               token={'fixture': True}, expires_at=time.time() + 300)
    service.update(boundary, 'codex', account, 1, action='default')
    model = 'gpt-5.4'
    rows = _normalise_models({'models': [{'slug': model, 'visibility': 'list', 'context_window': 100000},
                                        {'slug': 'unmaintained-fixture', 'visibility': 'list'}]})
    def fetch(cache, **kwargs):
        assert not cache and kwargs['account_binding'].account_id == account
        assert kwargs['boundary'] == boundary and kwargs['observed_revision'] == 2
        wires = frozenset(row['slug'] for row in rows)
        return SimpleNamespace(rows=rows, evidence=CodexRegistrationEvidence(
            boundary, 2, account_fingerprint('A'), wires, wires))
    monkeypatch.setattr('lib.oauth.codex_catalog._fetch_catalog', fetch)
    assert admit_initial_codex_supply(repo, boundary, account, 2) == 3
    document = repo.get(boundary).document
    candidates = compile_candidates(document, NativeModelSelection(ModelRef('openai', model), None))
    assert candidates and candidates[0].codex_account_fingerprints == (account_fingerprint('A'),)
    assert not any(row['wire_model_id'] == 'unmaintained-fixture' for row in document['deployments'])
    from lib.identity import PrincipalContext
    from lib.tasks_pkg.native_execution import compile_native_application_route
    principal = PrincipalContext.user(subject_id='fixture', owner_user_id=7, tenant_id='fixture')
    route = compile_native_application_route(repo, principal, NativeModelSelection(ModelRef('openai', model), None))
    try:
        assert route.consume(principal)[0]['wire_model'] == model
    finally:
        route.close()
