"""Public v4 account commands with synthetic upstream grants only."""
import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.oauth.test_codex_account_flow import bearer

pytestmark = pytest.mark.unit


def test_codex_account_management_wire(monkeypatch):
    from quart import g, request
    from lib.api_keys import local_admin_context
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    from lib.app.app_factory import create_base_app
    from lib.http_transport.error_handlers import register_http_error_handlers
    from lib.identity import PrincipalContext
    from lib.model_routing.repository import InMemoryModelRoutingRepository
    from lib.oauth import codex
    from routes.api_v1.oauth import api_v1_oauth_bp
    from routes.api_v1 import oauth_accounts

    repo = InMemoryModelRoutingRepository()
    monkeypatch.setattr(oauth_accounts, 'ModelRoutingRepository', lambda: repo)
    monkeypatch.setattr(codex, '_save_login_token', lambda *_: pytest.fail('singleton write'))
    monkeypatch.setattr(codex, '_oauth_http_post', lambda _url, payload, **_: SimpleNamespace(
        status_code=200, json=lambda: {'access_token': bearer(payload['code']),
                                      'id_token': bearer(payload['code']), 'refresh_token': 'never-return-this'}))
    app = create_base_app(__name__, {'TESTING': True})
    register_http_error_handlers(app)
    app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(api_v1_oauth_bp, name='oauth_test_v4')))

    @app.before_request
    async def identity_fixture():
        if request.headers.get('X-Fixture-Anonymous'):
            return
        tenant = request.headers.get('X-Fixture-Tenant', 'account-api-fixture')
        g.auth_ctx = local_admin_context()
        g.principal_context = PrincipalContext.user(subject_id='oauth-fixture', owner_user_id=31, tenant_id=tenant)

    async def exercise():
        client = app.test_client()
        base = '/api/v4/oauth/accounts'

        async def command(action, expected=200, headers=None, **fields):
            resp = await client.post(base + '/commands', headers=headers or {},
                                     json={'provider': 'codex', 'action': action, **fields})
            assert resp.status_code == expected, await resp.get_data(as_text=True)
            text = await resp.get_data(as_text=True)
            assert all(value not in text for value in ('never-return-this', 'secret_reference', 'code_verifier', 'subject_fingerprint'))
            return (await resp.get_json()).get('data')

        async def flow(action, revision, **fields):
            result = await command(action, expected_revision=revision, method='browser', **fields)
            state = parse_qs(urlsplit(result['auth_url']).query)['state'][0]
            return result, state

        assert (await client.get(base + '?provider=codex', headers={'X-Fixture-Anonymous': '1'})).status_code == 401
        for invalid in ('0', True, 0.0):
            await command('add', 400, expected_revision=invalid, method='browser')
        one, state = await flow('add', 0)
        assert (await client.get(base + '/flows/' + one['flow_id'], headers={'X-Fixture-Tenant': 'other'})).status_code == 404
        await command('complete', 404, headers={'X-Fixture-Tenant': 'other'}, flow_id=one['flow_id'], code='A', state=state)
        a = (await command('complete', flow_id=one['flow_id'], code='A', state=state))['account_id']
        second, state = await flow('add', 1)
        b = (await command('complete', flow_id=second['flow_id'], code='B', state=state))['account_id']
        assert a != b
        await command('default', account_id=b, expected_revision=2)
        await command('rename', 409, account_id=a, alias='work', expected_revision=2)
        await command('rename', account_id=a, alias='work', expected_revision=3)
        stale, stale_state = await flow('reauth', 4, account_id=a)
        active, state = await flow('reauth', 4, account_id=a)
        await command('complete', 404, flow_id=stale['flow_id'], code='A', state=stale_state)
        await command('complete', 404, flow_id=active['flow_id'], code='B', state=state)
        active, state = await flow('reauth', 4, account_id=a)
        assert (await command('complete', flow_id=active['flow_id'], code='A', state=state))['account_id'] == a
        await command('remove', account_id=b, expected_revision=5)
        result = (await (await client.get(base + '?provider=codex')).get_json())['data']
        assert result == {
            'provider': 'codex', 'revision': 6, 'default_account_id': None,
            'accounts': [{'account_id': a, 'alias': 'work', 'status': 'authorized'}],
            'usage': {'source': 'local_record', 'provider': 'codex', 'status': 'unavailable'},
        }

    asyncio.run(exercise())
