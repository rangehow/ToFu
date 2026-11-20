import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.model_routing.domain import ModelRoutingError
from lib.model_routing.repository import InMemoryModelRoutingRepository, OwnerBoundary
from lib.oauth.accounts import SubscriptionAccounts
from lib.oauth.scoped_catalog import admit_initial_codex_supply
from tests.oauth.test_codex_account_flow import bearer

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('mode', ['relay', 'direct', 'revision_race', 'foreign_tenant'])
def test_scoped_catalog_transport_and_publication_fences(monkeypatch, mode):
    repo = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(7, 'owned')
    accounts = SubscriptionAccounts(repo)
    account = accounts.complete(accounts.begin(boundary, 'codex', 0), verified_subject='A',
                                 token={'access_token': bearer('A'), 'account_id': 'A'},
                                 expires_at=time.time() + 300)
    route = Mock(return_value='fixture-agent' if mode == 'relay' else 'direct')
    monkeypatch.setattr('lib.oauth.codex_catalog.codex_client_version', lambda: '0.0.1')
    monkeypatch.setattr('lib.oauth.codex_version.codex_client_version', lambda: '0.0.1')
    resolve = Mock(wraps=repo.resolve_secret)
    monkeypatch.setattr(repo, 'resolve_secret', resolve)
    relay = Mock()
    monkeypatch.setattr('lib.desktop.egress.route_request', route)
    monkeypatch.setattr('lib.desktop.egress.egress_http', relay)
    def response(url, **options):
        assert url.startswith('https://chatgpt.com/backend-api/codex/models?')
        assert options['allow_redirects'] is False
        assert options['headers']['Authorization'] == 'Bearer ' + bearer('A')
        if mode == 'revision_race':
            accounts.update(boundary, 'codex', account, 1, action='rename', alias='racing edit')
        return SimpleNamespace(status_code=200, headers={}, json=lambda: {'models': [
            {'slug': 'gpt-5.4', 'visibility': 'list', 'context_window': 100000}]})
    direct = Mock(side_effect=response)
    monkeypatch.setattr('lib.oauth.http.http_get', direct)
    target = OwnerBoundary.create(7, 'foreign') if mode == 'foreign_tenant' else boundary
    if mode == 'direct':
        assert admit_initial_codex_supply(repo, target, account, 1) == 2
        assert repo.get(boundary).document['providers']
    else:
        with pytest.raises(ModelRoutingError):
            admit_initial_codex_supply(repo, target, account, 0 if mode == 'foreign_tenant' else 1)
        assert not repo.get(boundary).document['providers']
    relay.assert_not_called()
    assert resolve.call_count == int(mode in {'direct', 'revision_race'})
    assert direct.call_count == int(mode in {'direct', 'revision_race'})
    assert route.call_count == int(mode != 'foreign_tenant')
