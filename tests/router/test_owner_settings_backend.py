"""owner_settings runs the Settings page's own Save and read (docs/ROUTER.md R11).

The real ``routes.config`` handlers, through the ``SettingsBackend`` the composition root
registers, over the isolated test application and an isolated server_config.json. The R9 gate
is specified in test_delegated_action.py and test_owner_settings.py; here it passes the effect
straight through, so these specs judge only backend parity: a Router change reads back
identically in the Settings page, also from the other managed process, and an invalid value
gets the Settings Save's own validation error.
"""
from __future__ import annotations

import json

import pytest

import lib
from lib.lifecycle_approval import DelegationRefused
from lib.router import owner_settings
from lib.search.saved_domain_lists import SKIP_DOMAINS_SCHEMA, SKIP_DOMAINS_SCHEMA_KEY

pytestmark = pytest.mark.unit

TRIGGER = {'reason': 'owner asked', 'source_turn_id': 'owner-turn'}
GATEWAY = {'enabled': False, 'id': 'alpha', 'sources': ['one-search']}


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    import routes.config as rc

    path = tmp_path / 'server_config.json'
    path.write_text(json.dumps({
        'model_defaults': {'default_model': 'claude-opus-5', 'fallback_model': 'gpt-5.6-terra'},
        'search': {'skip_domains': ['old.example'], SKIP_DOMAINS_SCHEMA_KEY: SKIP_DOMAINS_SCHEMA,
                   'gateway': GATEWAY},
    }))
    monkeypatch.setattr(rc, '_SERVER_CONFIG_PATH', str(path))
    return path


@pytest.fixture
def router(flask_app, monkeypatch):
    """The Router's scope with the R9 gate passing straight through to the effect."""
    from routes.config import SettingsBackend
    assert isinstance(owner_settings._SETTINGS_BACKEND, SettingsBackend)
    monkeypatch.setattr(owner_settings, '_SETTINGS_BACKEND', SettingsBackend(flask_app))
    monkeypatch.setattr('lib.router.owner_delegation.delegated_action',
                        lambda task, scope, arguments, *, scope_name, target, action: (action(), 'action-1'))
    return {'user_id': 1, 'project_path': '/router-home', 'conversation_id': 'router', 'binding_revision': 1}


def _settings_page(flask_client) -> dict:
    response = flask_client.get('/api/v4/server-config')
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def test_the_composition_root_registers_the_settings_pages_own_backend(flask_app):
    from routes.config import SettingsBackend
    assert isinstance(owner_settings._SETTINGS_BACKEND, SettingsBackend)


def test_a_router_change_reads_back_identically_in_the_settings_page(router, flask_client, config_file,
                                                                     monkeypatch):
    result = owner_settings.call({}, router, 'set',
                                 {'path': 'search.skip_domains', 'value': ['b.example', 'a.example'], **TRIGGER})
    assert result['value'] == ['a.example', 'b.example']
    assert _settings_page(flask_client)['search']['skip_domains'] == ['a.example', 'b.example']
    # In the managed split the Router saves in the execution host, so the API host's module
    # copy is stale; the Settings page still reads the persisted document back.
    monkeypatch.setattr(lib, 'SKIP_DOMAINS', {'stale.example'})
    assert _settings_page(flask_client)['search']['skip_domains'] == ['a.example', 'b.example']
    # The Save body carried no gateway, so the configured gateway is kept.
    assert json.loads(config_file.read_text())['search']['gateway'] == GATEWAY


def test_a_router_default_model_change_keeps_the_fallback(router, flask_client, config_file):
    owner_settings.call({}, router, 'set',
                        {'path': 'model_defaults.default_model', 'value': 'claude-opus-5.5', **TRIGGER})
    defaults = _settings_page(flask_client)['model_defaults']
    assert (defaults['default_model'], defaults['fallback_model']) == ('claude-opus-5.5', 'gpt-5.6-terra')


def test_an_invalid_value_gets_the_settings_saves_own_validation_error(router, flask_client, config_file):
    before = config_file.read_text()
    response = flask_client.post('/api/v4/server-config', json={'proxy_bypass_domains': 'corp.example'})
    assert response.status_code == 400
    message = response.get_json()['error']['message']
    assert 'must be an array' in message
    with pytest.raises(DelegationRefused) as refused:
        owner_settings.call({}, router, 'set',
                            {'path': 'proxy_bypass_domains', 'value': 'corp.example', **TRIGGER})
    assert refused.value.code == 'owner_settings_rejected'
    assert message in str(refused.value)
    assert config_file.read_text() == before
