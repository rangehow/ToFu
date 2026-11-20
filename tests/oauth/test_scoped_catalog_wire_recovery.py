from types import SimpleNamespace
import time

import pytest
import requests

from lib.http_transport.outbound import RequestNetworkPolicy, send_routed_request
from lib.model_routing.domain import ModelRoutingError
from lib.model_routing.repository import InMemoryModelRoutingRepository, OwnerBoundary
from lib.oauth.accounts import SubscriptionAccounts
from lib.oauth.scoped_catalog import admit_initial_codex_supply
from tests.oauth.test_codex_account_flow import bearer

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('change', ['remove', 'reauth', None])
def test_every_catalog_recovery_send_revalidates_generation(monkeypatch, change):
    repo = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(7, 'fixture')
    service = SubscriptionAccounts(repo)

    def link(revision, account_id=None):
        return service.complete(service.begin(boundary, 'codex', revision, account_id=account_id),
                                verified_subject='A', token={'access_token': bearer('A'), 'account_id': 'A'},
                                expires_at=time.time() + 300)

    account = link(0)
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda *a, **k: 'direct')
    monkeypatch.setattr('lib.oauth.codex_catalog.codex_client_version', lambda: '0.0.1')
    monkeypatch.setattr('lib.oauth.codex_version.codex_client_version', lambda: '0.0.1')
    monkeypatch.setattr('lib.proxy.report_network_route', lambda *a, **k: None)
    routes = [SimpleNamespace(route_id=str(n), mode='direct', pool_id=None, requests_proxies=lambda: {}) for n in range(2)]
    monkeypatch.setattr('lib.proxy.network_route_candidates', lambda *a, **k: routes)
    sent = []

    def transport(url, **options):
        assert options['allow_redirects'] is False
        request = requests.Request('GET', url, headers=options['headers']).prepare()

        def physical(request, **kwargs):
            sent.append(request.headers['Authorization'])
            if len(sent) == 1:
                if change == 'remove':
                    service.update(boundary, 'codex', account, 1, action='remove')
                elif change == 'reauth':
                    link(1, account)
                raise requests.exceptions.ConnectTimeout()
            response = requests.Response()
            response.status_code = 200
            response._content = b'{"models":[{"slug":"gpt-5.4","visibility":"list","context_window":100000}]}'
            return response

        return send_routed_request(physical, request, policy=RequestNetworkPolicy(scope='subscription'))

    monkeypatch.setattr('lib.oauth.http.http_get', transport)
    if change is None:
        assert admit_initial_codex_supply(repo, boundary, account, 1) == 2
    else:
        with pytest.raises(ModelRoutingError):
            admit_initial_codex_supply(repo, boundary, account, 1)
        assert not repo.get(boundary).document['providers']
    assert sent == ['Bearer ' + bearer('A')] * (2 if change is None else 1)
