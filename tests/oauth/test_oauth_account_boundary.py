"""Synthetic direct OAuth account selection and credential-publication fences."""
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('replacement', [False, True])
def test_late_codex_exchange_cannot_publish(monkeypatch, tmp_path, replacement):
    from lib.oauth import codex, token_store
    from lib.oauth.manager import exchange_code, logout_oauth
    from lib.oauth.manager._state import _active_flows

    monkeypatch.setattr(token_store, '_config_path', lambda path: str(tmp_path / path))
    monkeypatch.setattr('lib.oauth.outbound.deprovision_oauth_provider', lambda *a, **k: None)
    monkeypatch.setattr('lib.oauth.codex_catalog.stop_codex_catalog_refresher', lambda: None)
    monkeypatch.setattr('lib.oauth.codex_catalog.forget_pending_codex_catalog', lambda: None)
    monkeypatch.setattr('lib.subscription_quota.clear_subscription_quota', lambda *a, **k: None)
    assert token_store.save_token('codex', {'access_token': 'existing-fixture'})
    monkeypatch.setitem(_active_flows, 'codex', {
        'owner_user_id': 1, 'flow_id': 'old', 'state': 'fixture-state',
        'pkce': {'code_verifier': 'fixture-verifier'}, 'status': 'started',
    })

    def exchange(*_args, **_kwargs):
        if replacement:
            _active_flows['codex'] = {'owner_user_id': 2, 'flow_id': 'new', 'status': 'started'}
        else:
            assert logout_oauth('codex', owner_user_id=1)['ok']
        return SimpleNamespace(status_code=200, json=lambda: {'access_token': 'late-fixture'})

    monkeypatch.setattr(codex, '_oauth_http_post', exchange)
    result = exchange_code('codex', 'fixture-code', 'fixture-state', owner_user_id=1)
    assert 'flow has changed' in result['error']
    stored = token_store.load_stored_token('codex')
    if replacement:
        assert stored['access_token'] == 'existing-fixture'
        assert _active_flows['codex']['status'] == 'started'
    else:
        assert stored is None


@pytest.mark.parametrize('bearer, reason', [('B', 'account_changed'), ('', 'credential_source_binding')])
def test_explicit_codex_account_requires_matching_bearer(monkeypatch, bearer, reason):
    from lib.llm.llm_errors import SubscriptionCredentialUnavailableError
    from lib.oauth import codex
    from lib.oauth.outbound import resolve_oauth_request

    monkeypatch.setattr(codex, 'codex_get_valid_token_snapshot', lambda **_: SimpleNamespace(
        access_token='synthetic', account_id='A', bearer_account_id=bearer))
    with pytest.raises(SubscriptionCredentialUnavailableError) as raised:
        resolve_oauth_request('codex', {}, {}, user_id='1', expected_account_id='A')
    assert raised.value.reason == reason
