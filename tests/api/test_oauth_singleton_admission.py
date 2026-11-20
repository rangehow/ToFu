"""An unsupported account-targeted command must never act on the singleton."""
import asyncio
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('path, command, parameters, methods', [
    ('/api/oauth/login', 'start_oauth_flow', {}, ('GET', 'POST')),
    ('/api/oauth/callback', 'exchange_code', {'code': 'fixture'}, ('GET', 'POST')),
    ('/api/oauth/store-token', 'store_token', {'token': {}}, ('POST',)),
    ('/api/oauth/logout', 'logout_oauth', {}, ('GET', 'POST')),
    ('/api/v1/oauth/device-login', 'start_device_flow', {}, ('GET', 'POST')),
])
def test_singleton_mutations_refuse_account_selector(monkeypatch, path, command, parameters, methods):
    from quart import g
    from lib.api_keys import local_admin_context
    from lib.app.app_factory import create_base_app
    from lib.http_transport.error_handlers import register_http_error_handlers
    from lib.oauth import manager
    from routes.api_v1.oauth import api_v1_oauth_bp
    from routes.oauth import oauth_bp

    app = create_base_app(__name__, {'TESTING': True})
    register_http_error_handlers(app)
    app.register_blueprint(oauth_bp)
    app.register_blueprint(api_v1_oauth_bp)

    @app.before_request
    async def grant():
        g.auth_ctx = local_admin_context()
        g.rate_decision = None

    handler = Mock(side_effect=AssertionError('account-targeted command reached OAuth mutation'))
    monkeypatch.setattr(manager, command, handler)
    data = {'provider': 'codex', **parameters, 'account_id': 'synthetic-other-account'}

    async def exercise():
        client = app.test_client()
        for method in methods:
            response = await client.open(path, method=method, **(
                {'query_string': data} if method == 'GET' else {'json': data}))
            assert response.status_code == 400
            assert data['account_id'] not in await response.get_data(as_text=True)
        response = await client.post(path, json={'provider': 'codex', **parameters},
                                     query_string={'account_id': data['account_id']})
        assert response.status_code == 400

    asyncio.run(exercise())
    handler.assert_not_called()
