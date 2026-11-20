"""Exact account-resolution boundary proof, not a provider execution receipt."""
import json
import threading
import time
from dataclasses import replace

import pytest

from lib.llm.llm_errors import SubscriptionCredentialUnavailableError
from lib.model_routing.domain import ModelRoutingError
from lib.model_routing.native_route import NativeCredentialRequest, route_binding_sha256
from lib.model_routing.repository import InMemoryModelRoutingRepository, OwnerBoundary
from lib.oauth.accounts import SubscriptionAccounts
from lib.tasks_pkg.native_execution import compile_native_application_route
from tests.model_routing.test_native_route import Repository, principal, selection
from tests.oauth.test_codex_account_flow import bearer

pytestmark = pytest.mark.unit


@pytest.fixture
def accounts(monkeypatch):
    original = Repository()
    original.document['connections'][0].update(
        protocol='openai_responses', responses_profile='codex',
        base_url='https://chatgpt.com/backend-api/codex')
    original.document['credentials'][0]['kind'] = 'oauth'
    repository = InMemoryModelRoutingRepository()
    get, resolve = repository.get, repository.resolve_secret

    def authority(boundary):
        row = get(boundary)
        return replace(row, document={**original.document, **{
            key: value for key, value in row.document.items()
            if key == 'subscription_accounts'}})

    def secret(boundary, reference, **kwargs):
        if reference == 'secret-a':
            return 'oauth:codex'
        return resolve(boundary, reference, **kwargs)

    monkeypatch.setattr(repository, 'get', authority)
    monkeypatch.setattr(repository, 'resolve_secret', secret)
    from lib.oauth import codex_version, native_credentials, token_store
    monkeypatch.setattr(codex_version, 'codex_client_version', lambda: '0.0.0-fixture')
    monkeypatch.setattr(codex_version, 'codex_user_agent', lambda: 'synthetic-binding-proof')
    monkeypatch.setattr(codex_version, '_schedule_refresh',
                        lambda *_: pytest.fail('must not schedule external metadata lookup'))
    monkeypatch.setattr(native_credentials.NativeCodexCredentialResolver, '_source',
                        lambda *_: pytest.fail('scoped binding must not use a legacy source'))
    monkeypatch.setattr(token_store, 'load_stored_token',
                        lambda *_: pytest.fail('must not read live credentials'))
    service = SubscriptionAccounts(repository)
    owner = original.boundary
    ids = [service.complete(service.begin(owner, 'codex', revision),
                            verified_subject=subject,
                            token={'access_token': bearer(subject), 'account_id': subject},
                            expires_at=time.time() + 300)
           for revision, subject in enumerate(('synthetic-A', 'synthetic-B'))]
    return service, owner, ids


def issue(service):
    route = compile_native_application_route(service.repository, principal(), selection())
    try:
        metadata, wire = route.consume(principal())
    finally:
        route.close()
    request = NativeCredentialRequest(
        principal=principal(), task_id='synthetic-binding-proof', attempt_ordinal=1,
        credential_id=metadata['credential_id'], provider_id=metadata['provider_id'],
        offering_id=metadata['offering_id'], connection_id=metadata['connection_id'],
        deployment_id=metadata['deployment_id'], secret_reference=wire['secret_reference'],
        subscription='codex', endpoint=wire['endpoint'], wire_model=metadata['wire_model'],
        cache_affinity_key='synthetic', extra_headers_json=json.dumps(wire['headers']).encode(),
        route_binding_sha256=route_binding_sha256(metadata), cancelled=threading.Event())
    return metadata, request, wire['_credential_resolver']


def test_same_route_evidence_cannot_distinguish_two_resolved_accounts(accounts):
    service, owner, (a, b) = accounts
    service.update(owner, 'codex', a, 2, action='default')
    metadata_a, request_a, resolve_a = issue(service)
    service.update(owner, 'codex', b, 3, action='default')
    metadata_b, request_b, resolve_b = issue(service)

    assert service.inventory(owner, 'codex')['default_account_id'] == b
    assert route_binding_sha256(metadata_a) == route_binding_sha256(metadata_b)
    assert {k: v for k, v in metadata_a.items() if k != 'revision'} == {
        k: v for k, v in metadata_b.items() if k != 'revision'}
    assert request_a == replace(request_b, cancelled=request_a.cancelled)
    # Credential resolution is the last trusted Python boundary, not proof
    # that Rust sent the request or received a provider response.
    headers_a, headers_b = resolve_a(request_a), resolve_b(request_b)
    assert headers_a['Authorization'] == 'Bearer ' + bearer('synthetic-A')
    assert headers_b['Authorization'] == 'Bearer ' + bearer('synthetic-B')
    assert headers_a['chatgpt-account-id'] == 'synthetic-A'
    assert headers_b['chatgpt-account-id'] == 'synthetic-B'
    public = json.dumps([metadata_a, metadata_b])
    for private in (a, b, 'synthetic-A', 'synthetic-B', 'Authorization', 'access_token'):
        assert private not in public


def test_rename_and_removal_do_not_rewrite_or_rebind_issued_identity(accounts):
    service, owner, (a, b) = accounts
    service.update(owner, 'codex', a, 2, action='default')
    metadata, request, resolve = issue(service)
    service.update(owner, 'codex', a, 3, action='rename', alias='Synthetic Alpha')
    service.update(owner, 'codex', b, 4, action='default')
    assert resolve(request)['Authorization'] == 'Bearer ' + bearer('synthetic-A')
    inventory = {row['account_id']: row for row in service.inventory(owner, 'codex')['accounts']}
    assert inventory[a]['alias'] == 'Synthetic Alpha'
    assert 'Synthetic Alpha' not in json.dumps(metadata)
    service.update(owner, 'codex', a, 5, action='remove')
    with pytest.raises(SubscriptionCredentialUnavailableError) as error:
        resolve(request)
    assert error.value.reason == 'credential_source_binding'
    assert service.inventory(owner, 'codex')['default_account_id'] == b
    assert [row['account_id'] for row in service.inventory(owner, 'codex')['accounts']] == [b]
    _, new_request, new_resolve = issue(service)
    assert new_resolve(new_request)['Authorization'] == 'Bearer ' + bearer('synthetic-B')


def test_missing_default_refuses_instead_of_using_available_account(accounts):
    service, owner, (a, b) = accounts
    with pytest.raises(ModelRoutingError) as error:
        issue(service)
    assert error.value.kind == 'subscription_account_unavailable'
    service.update(owner, 'codex', a, 2, action='default')
    _, request, resolve = issue(service)
    service.update(owner, 'codex', a, 3, action='remove')
    with pytest.raises(SubscriptionCredentialUnavailableError):
        resolve(request)
    with pytest.raises(ModelRoutingError) as error:
        issue(service)
    assert error.value.kind == 'subscription_account_unavailable'
    assert service.inventory(owner, 'codex')['default_account_id'] is None
    assert [row['account_id'] for row in service.inventory(owner, 'codex')['accounts']] == [b]


def test_bound_resolution_refuses_foreign_owner_and_cancelled_attempt(accounts):
    service, owner, (a, _) = accounts
    service.update(owner, 'codex', a, 2, action='default')
    _, request, resolve = issue(service)
    from lib.model_routing.native_route import NativeRouteError
    with pytest.raises(NativeRouteError) as error:
        resolve(replace(request, principal=principal(8)))
    assert error.value.kind == 'native_route_owner'
    with pytest.raises(ModelRoutingError):
        service.resolve(replace(service.bind(owner, 'codex', a),
                                boundary=OwnerBoundary.create(8)))
    request.cancelled.set()
    import asyncio
    with pytest.raises(asyncio.CancelledError):
        resolve(request)


@pytest.mark.parametrize('error', [RuntimeError('synthetic transport failure'),
                                    SubscriptionCredentialUnavailableError(
                                        reason='credential_source_binding',
                                        credential_source='tofu_oauth', transient=False)])
def test_bound_failure_is_terminal_not_account_or_provider_fallback(accounts, error):
    from types import SimpleNamespace
    from lib.llm_dispatch._api_failures import DispatchFailurePolicy

    service, owner, (a, b) = accounts
    service.update(owner, 'codex', a, 2, action='default')
    binding = service.bind(owner, 'codex', a)
    service.update(owner, 'codex', b, 3, action='default')
    releases = []
    slot = SimpleNamespace(subscription_account=(binding, service.repository),
                           release=lambda: releases.append(True))

    def forbidden(*_args, **_kwargs):
        pytest.fail('bound failure must not restart, retry, or select fallback')

    policy = DispatchFailurePolicy(None, SimpleNamespace(pick_and_reserve=forbidden),
        capability='text', strict_model=False, max_429_attempts=None,
        owner_user_id=owner.owner_user_id, log_prefix='synthetic',
        on_retry=forbidden, restart=forbidden)
    with pytest.raises(type(error)) as raised:
        policy.handle(error, slot, tag='synthetic')
    assert raised.value is error
    assert releases == [True]
    assert service.inventory(owner, 'codex')['default_account_id'] == b


def test_legacy_metadata_absence_does_not_supply_account_evidence():
    repository = InMemoryModelRoutingRepository()
    owner = OwnerBoundary.create(7)
    authority = repository.get(owner)
    assert SubscriptionAccounts.issued_default(authority, 'codex') is None
    assert SubscriptionAccounts(repository).inventory(owner, 'codex') == {
        'provider': 'codex', 'revision': 0, 'default_account_id': None, 'accounts': []}
