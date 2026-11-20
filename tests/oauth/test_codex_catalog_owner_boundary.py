"""The catalog worker must provision only its resolved routing owner."""
from unittest import mock

import pytest

from lib.model_routing import OwnerBoundary
from lib.oauth import codex_catalog

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('owner,tenant', [(101, 'tenant-a'), (202, 'tenant-b')])
@pytest.mark.parametrize('case', ['fresh', 'fetch', 'fallback', 'pending'])
def test_refresh_provisions_resolved_owner(tmp_path, monkeypatch, owner, tenant, case):
    boundary = OwnerBoundary.create(owner, tenant)
    token = {'access_token': 'token', 'account_id': 'account'}
    rows = [{'slug': 'example', 'visibility': 'list'}]
    monkeypatch.setattr(codex_catalog, '_cache_path', lambda: str(tmp_path / 'cache.json'))
    monkeypatch.setattr('lib.oauth.token_store.load_token', lambda _name: token)
    monkeypatch.setattr(codex_catalog, 'codex_client_version', lambda: '1.0')
    monkeypatch.setattr(codex_catalog, '_pending_catalog', None)
    if case == 'fresh':
        codex_catalog._write_cache(rows, 'etag', codex_catalog._account_fingerprint(token))
    if case == 'pending':
        codex_catalog._pending_catalog = {
            'rows': rows, 'etag': 'etag',
            'fingerprint': codex_catalog._account_fingerprint(token), 'client_version': '1.0',
            'cache_path': codex_catalog._cache_path(),
        }
    fetch = mock.Mock(return_value=codex_catalog._CatalogFetch(rows, 'etag', False))
    if case == 'fallback':
        fetch.side_effect = RuntimeError('offline')
    monkeypatch.setattr(codex_catalog, '_fetch_catalog', fetch)
    provision = mock.Mock(return_value=True)
    monkeypatch.setattr('lib.oauth.outbound.provision_oauth_provider', provision)
    result = codex_catalog.refresh_codex_model_catalog(boundary=boundary)
    assert result['ok'] is (case != 'fallback')
    provision.assert_called_once_with('codex', owner_user_id=owner, tenant_id=tenant)
    if fetch.called:
        assert fetch.call_args.kwargs['observed_revision'] is None


def test_worker_carries_boundary_to_every_poll(monkeypatch):
    boundary = OwnerBoundary.create(303, 'tenant-c')
    calls = []
    def refresh(**kwargs):
        calls.append(kwargs)
        return {'ok': True, 'catalog_changed': False}
    monkeypatch.setattr(codex_catalog, 'refresh_codex_model_catalog', refresh)
    monkeypatch.setattr(codex_catalog, '_research_registrations', lambda _boundary: {'status': 'ok'})
    monkeypatch.setattr(codex_catalog, '_token_refresh_poll_interval', lambda interval: interval)
    monkeypatch.setattr(codex_catalog._refresh_wake, 'wait', lambda _seconds: False)
    monkeypatch.setattr(codex_catalog._refresh_wake, 'clear', lambda: None)
    monkeypatch.setattr(codex_catalog._worker_stop, 'is_set', lambda: len(calls) >= 2)
    codex_catalog._worker_loop(force_initial=True, boundary=boundary)
    assert calls == [{'force': True, 'boundary': boundary},
                     {'force': True, 'boundary': boundary}]
