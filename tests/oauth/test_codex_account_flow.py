"""Exercise exact account intent through synthetic OAuth exchange and outbound resolution."""
import base64
import json
import time
from types import SimpleNamespace

import pytest

from lib.model_routing.domain import ModelRoutingError
from lib.model_routing.repository import InMemoryModelRoutingRepository, OwnerBoundary
from lib.oauth import codex
from lib.oauth.accounts import SubscriptionAccounts
from lib.oauth.manager import account_flow

pytestmark = pytest.mark.unit


def bearer(subject):
    payload = json.dumps({'https://api.openai.com/auth': {'chatgpt_account_id': subject}}).encode()
    return 'fixture.' + base64.urlsafe_b64encode(payload).decode().rstrip('=') + '.fixture'


@pytest.fixture
def setup(monkeypatch):
    from lib.oauth.manager._state import _active_flows
    boundary = OwnerBoundary.create(7, 'fixture')
    monkeypatch.setitem(_active_flows, account_flow._key(boundary), {'expires_at': 0})
    repo = InMemoryModelRoutingRepository()
    service = SubscriptionAccounts(repo)
    monkeypatch.setattr(codex, '_save_login_token', lambda *_: pytest.fail('singleton write'))
    monkeypatch.setattr(codex, '_oauth_http_post', lambda _url, payload, **_: SimpleNamespace(
        status_code=200, json=lambda: {'access_token': bearer(payload['code']),
                                      'id_token': bearer(payload['code'])}))
    return service, boundary


def test_browser_and_device_add_exact_reauth_and_outbound_revocation(setup, monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    from lib.llm.llm_errors import SubscriptionCredentialUnavailableError
    from lib.oauth.manager._device import _CodexDevice
    from lib.oauth.outbound import resolve_oauth_request

    service, boundary = setup
    flow = account_flow.start(service, boundary, 'codex', 0, method='browser')
    state = parse_qs(urlsplit(flow['auth_url']).query)['state'][0]
    assert not {'pkce', 'intent', 'exchange'} & flow.keys()
    with pytest.raises(ModelRoutingError):
        account_flow.complete(service, boundary, flow['flow_id'], code='A', state='wrong')
    with pytest.raises(ModelRoutingError):
        account_flow.complete(service, OwnerBoundary.create(7, 'other'), flow['flow_id'], code='A', state=state)
    a = account_flow.complete(service, boundary, flow['flow_id'], code='A', state=state)['account_id']
    monkeypatch.setattr(_CodexDevice, 'request', staticmethod(lambda **_: {
        'device_auth_id': 'private-device-id', 'user_code': 'user-fixture', 'interval': 5,
        'expires_in': 60, 'verification_url': 'https://example.invalid/device'}))
    monkeypatch.setattr(_CodexDevice, 'poll', staticmethod(lambda *a, **k: {
        'authorization_code': 'B', 'code_verifier': 'private-verifier'}))
    flow = account_flow.start(service, boundary, 'codex', 1, method='device')
    assert 'device_auth_id' not in flow
    b = account_flow.complete(service, boundary, flow['flow_id'])['account_id']
    assert a != b
    binding = service.bind(boundary, 'codex', a)
    service.update(boundary, 'codex', b, 2, action='default')
    def resolve():
        return resolve_oauth_request('codex', {}, {}, user_id='7', account_binding=binding,
                                     account_repository=service.repository)
    assert resolve()[0] == bearer('A')
    flow = account_flow.start(service, boundary, 'codex', 3, method='browser', account_id=a)
    state = parse_qs(urlsplit(flow['auth_url']).query)['state'][0]
    with pytest.raises(ModelRoutingError):
        account_flow.complete(service, boundary, flow['flow_id'], code='B', state=state)
    assert resolve()[0] == bearer('A')
    flow = account_flow.start(service, boundary, 'codex', 3, method='browser', account_id=a)
    state = parse_qs(urlsplit(flow['auth_url']).query)['state'][0]
    assert account_flow.complete(service, boundary, flow['flow_id'], code='A', state=state)['account_id'] == a
    with pytest.raises(SubscriptionCredentialUnavailableError):
        resolve()


def test_superseded_callback_cannot_publish(setup, monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    service, boundary = setup
    flow = account_flow.start(service, boundary, 'codex', 0, method='browser')
    state = parse_qs(urlsplit(flow['auth_url']).query)['state'][0]
    def exchange(*args, **kwargs):
        account_flow.start(service, boundary, 'codex', 0, method='browser')
        return {'access_token': bearer('A'), 'account_id': 'A', 'expire': time.time() + 300}
    monkeypatch.setattr(codex, 'codex_exchange_code', exchange)
    with pytest.raises(ModelRoutingError):
        account_flow.complete(service, boundary, flow['flow_id'], code='A', state=state)
    assert service.inventory(boundary, 'codex')['accounts'] == []


def test_issued_python_preparation_fences_actual_send_after_removal(setup):
    from lib.llm._sse_core import prepare_request
    from lib.llm.llm_errors import SubscriptionCredentialUnavailableError
    from lib.llm.request import dispatch_model_request
    from lib.oauth.account_dispatch import scoped_account_dispatch

    service, boundary = setup
    for revision, subject in enumerate(('A', 'B')):
        account = service.complete(service.begin(boundary, 'codex', revision), verified_subject=subject,
                                   token={'access_token': bearer(subject), 'account_id': subject},
                                   expires_at=time.time() + 300)
        if subject == 'A':
            a = account
    service.update(boundary, 'codex', a, 2, action='default')
    binding_a = service.issued_default(service.repository.get(boundary), 'codex')
    assert binding_a.account_id == a
    service.update(boundary, 'codex', account, 3, action='default')
    binding_b = service.issued_default(service.repository.get(boundary), 'codex')
    assert binding_b.account_id == account

    def prepare(binding):
        with scoped_account_dispatch(SimpleNamespace(subscription_account=(binding, service.repository))):
            return prepare_request({'model': 'fixture', 'messages': [{'role': 'user', 'content': 'hello'}]},
                                   oauth='codex', api_protocol='responses', owner_user_id=7,
                                   base_url='https://chatgpt.com/backend-api/codex')

    plan_a, plan_b = prepare(binding_a), prepare(binding_b)
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread
    import http.client

    sent = []

    class FakeProvider(BaseHTTPRequestHandler):
        def do_POST(self):
            sent.append(self.headers['Authorization'])
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    with HTTPServer(('127.0.0.1', 0), FakeProvider) as fake:
        worker = Thread(target=fake.serve_forever)
        worker.start()
        try:
            def send(plan):
                # Replace only the final socket destination in the fixture;
                # production preparation still requires the real Codex origin.
                connection = http.client.HTTPConnection('127.0.0.1', fake.server_port, timeout=3)
                try:
                    connection.request('POST', '/responses', headers=plan.hdrs)
                    assert connection.getresponse().status == 200
                finally:
                    connection.close()

            for plan in (plan_a, plan_b):
                dispatch_model_request(plan, send, plan)
            assert sent == ['Bearer ' + bearer('A'), 'Bearer ' + bearer('B')]
            service.update(boundary, 'codex', a, 4, action='remove')
            with pytest.raises(SubscriptionCredentialUnavailableError):
                dispatch_model_request(plan_a, send, plan_a)
            assert len(sent) == 2
        finally:
            fake.shutdown()
            worker.join()


def test_bound_dispatch_401_never_calls_legacy_refresh(setup, monkeypatch):
    from lib.llm.llm_errors import ModelAdmissionRefused, PermissionError_
    from lib.llm_dispatch import _api_failures

    def forbidden(*args):
        pytest.fail('scoped credential fell through to provider-global refresh')

    monkeypatch.setattr(_api_failures, '_force_oauth_token_refresh', forbidden)
    released = []
    service, boundary = setup
    account = service.complete(service.begin(boundary, 'codex', 0), verified_subject='A',
                               token={'access_token': bearer('A')}, expires_at=time.time() + 300)
    scoped = service.bind(boundary, 'codex', account), service.repository
    slot = SimpleNamespace(subscription_account=scoped, release=lambda: released.append(True))
    error = PermissionError_('fixture rejected', status_code=401)
    with pytest.raises(ModelAdmissionRefused, match='needs reauthorization'):
        _api_failures.DispatchFailurePolicy.handle(None, error, slot, tag='fixture')
    assert service.inventory(boundary, 'codex')['accounts'][0]['status'] == 'needs_reauth'
    assert released == [True]


def test_native_application_issuance_pins_default_and_refuses_revoked_generation(monkeypatch):
    import threading
    from lib.llm.llm_errors import SubscriptionCredentialUnavailableError
    from lib.model_routing.native_route import NativeCredentialRequest, route_binding_sha256
    from lib.tasks_pkg.native_execution import compile_native_application_route
    from tests.model_routing.test_native_route import Repository, principal, selection

    original = Repository()
    original.document['connections'][0].update(protocol='openai_responses', responses_profile='codex',
                                                base_url='https://chatgpt.com/backend-api/codex')
    original.document['credentials'][0]['kind'] = 'oauth'
    owner = original.boundary
    repo = InMemoryModelRoutingRepository()
    from dataclasses import replace
    get = repo.get

    def authority(boundary):
        row = get(boundary)
        return replace(row, document={**original.document, **{
            key: value for key, value in row.document.items() if key == 'subscription_accounts'}})

    monkeypatch.setattr(repo, 'get', authority)

    def read(boundary, reference, **kwargs):
        if reference == 'secret-a':
            return 'oauth:codex'
        return resolve(boundary, reference, **kwargs)
    resolve = repo.resolve_secret
    monkeypatch.setattr(repo, 'resolve_secret', read)
    service = SubscriptionAccounts(repo)
    a, b = [service.complete(service.begin(owner, 'codex', revision), verified_subject=subject,
                             token={'access_token': bearer(subject), 'account_id': subject},
                             expires_at=time.time() + 300)
            for revision, subject in enumerate(('A', 'B'))]
    service.update(owner, 'codex', a, 2, action='default')
    route = compile_native_application_route(repo, principal(), selection())
    metadata, wire = route.consume(principal())
    service.update(owner, 'codex', b, 3, action='default')
    request = NativeCredentialRequest(principal=principal(), task_id='fixture', attempt_ordinal=1,
        credential_id=metadata['credential_id'], provider_id=metadata['provider_id'],
        offering_id=metadata['offering_id'], connection_id=metadata['connection_id'],
        deployment_id=metadata['deployment_id'], secret_reference=wire['secret_reference'],
        subscription='codex', endpoint=wire['endpoint'], wire_model=metadata['wire_model'],
        cache_affinity_key='fixture', extra_headers_json=json.dumps(wire['headers']).encode(),
        route_binding_sha256=route_binding_sha256(metadata), cancelled=threading.Event())
    resolver = wire['_credential_resolver']
    assert resolver(request)['Authorization'] == 'Bearer ' + bearer('A')
    service.update(owner, 'codex', a, 4, action='remove')
    with pytest.raises(SubscriptionCredentialUnavailableError):
        resolver(request)
