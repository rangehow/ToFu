"""Production shutdown errors and idle expiry preserve Push connection ownership."""

import asyncio

import pytest
from quart import abort
from quart.testing import WebsocketResponseError

from lib.agent_core.push import PushHub
from lib.api_keys import AuthContext
from lib.app.app_factory import create_base_app
from lib.http_transport.error_handlers import register_http_error_handlers


pytestmark = pytest.mark.api


@pytest.fixture
def push_app(monkeypatch):
    import routes.push as push

    hub = PushHub()
    monkeypatch.setattr(push, 'hub', hub)
    context = AuthContext(key_id='idle-test', owner_user_id=17,
                          scopes=frozenset({'chat'}))
    monkeypatch.setattr(push, '_resolve_push_ws_auth',
                        lambda *_args: (context, 'token'))
    app = create_base_app('push-error-idle-test', {
        'TESTING': True, 'PROPAGATE_EXCEPTIONS': False,
        'MAX_CONTENT_LENGTH': 1234,
    })
    register_http_error_handlers(app)
    app.register_blueprint(push.push_bp)
    return app, hub


def test_multiple_owner_connections_and_shutdown_refusal_use_production_handlers(push_app):
    app, hub = push_app

    async def exercise():
        client = app.test_client()
        async with client.websocket('/api/push') as first:
            await first.send_json({'action': 'ping', 't': 1})
            assert (await first.receive_json())['type'] == 'pong'
            async with client.websocket('/api/push') as second:
                await second.send_json({'action': 'ping', 't': 2})
                assert (await second.receive_json())['type'] == 'pong'
                assert hub.client_count == 2
                hub.close_clients_for_shutdown()
                with pytest.raises(WebsocketResponseError) as rejected:
                    async with client.websocket('/api/push') as third:
                        await third.receive()
                assert rejected.value.response.status_code == 503
        assert hub.client_count == 0

    asyncio.run(exercise())


@pytest.mark.parametrize('path', ['/api/handshake', '/api/v4/handshake'])
@pytest.mark.parametrize('status', [401, 404, 405, 413, 429, 503])
def test_websocket_handshake_errors_keep_status_without_http_context(
        push_app, path, status):
    app, _ = push_app

    @app.websocket(path)
    async def handshake():
        abort(status)

    async def exercise():
        with pytest.raises(WebsocketResponseError) as rejected:
            async with app.test_client().websocket(path) as connection:
                await connection.receive()
        response = rejected.value.response
        assert response.status_code == status
        if path.startswith('/api/v4/'):
            assert response.content_type == 'application/problem+json'
            assert (await response.get_json())['instance'] == path

    asyncio.run(exercise())


def test_idle_client_releases_slot_while_heartbeats_renew_it(push_app, monkeypatch):
    import routes.push as push

    app, hub = push_app
    monkeypatch.setattr(push, 'PUSH_RECEIVE_IDLE_TIMEOUT_SECONDS', 0.15)

    async def exercise():
        client = app.test_client()
        async with client.websocket('/api/push') as connection:
            # Stay alive longer than the deadline by renewing proof-of-life.
            for i in range(8):
                await connection.send_json({'action': 'ping', 't': i})
                assert (await connection.receive_json())['t'] == i
                assert hub.client_count == 1
                await asyncio.sleep(0.04)
            # Keep the peer open but silent: the server must own eviction.
            async with asyncio.timeout(1):
                while hub.client_count:
                    await asyncio.sleep(0.01)
            async with client.websocket('/api/push') as replacement:
                await replacement.send_json({'action': 'ping', 't': 100})
                assert (await replacement.receive_json())['t'] == 100
                assert hub.client_count == 1
        assert hub.client_count == 0

    asyncio.run(exercise())


async def _server_close_code(connection):
    from quart.testing.connections import WebsocketDisconnectError

    async with asyncio.timeout(5):
        while True:
            try:
                await connection.receive()
            except WebsocketDisconnectError as closed:
                return closed.args[0]


def test_idle_release_closes_with_a_code_the_browser_recovers_from(push_app, monkeypatch):
    """push.js treats a 1000 close as intentional and terminal (see
    tests/frontend/test_push_recovery_lifecycle.py). A page that is still alive
    when the server releases its silent socket must reconnect by itself."""
    import routes.push as push

    app, _hub = push_app
    monkeypatch.setattr(push, 'PUSH_RECEIVE_IDLE_TIMEOUT_SECONDS', 0.1)

    async def exercise():
        async with app.test_client().websocket('/api/push') as connection:
            return await _server_close_code(connection)

    assert asyncio.run(exercise()) == 1001


def test_backlog_overflow_closes_with_a_code_the_browser_recovers_from(push_app, monkeypatch):
    """The overflow disconnect exists so the page reconciles from durable
    state; a terminal close would leave it without a socket instead."""
    import lib.agent_core.push_policy as push_policy

    app, hub = push_app
    monkeypatch.setattr(push_policy, 'resolve_push_event_queue_capacity',
                        lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(hub, '_register_subscription', lambda *args: None)
    monkeypatch.setattr(hub, '_deregister_subscription', lambda *args: None)

    async def exercise():
        try:
            async with app.test_client().websocket('/api/push') as connection:
                await connection.send_json({'action': 'ping', 't': 1})
                assert (await connection.receive_json())['type'] == 'pong'
                server_client = next(iter(hub._clients))
                hub.subscribe(server_client, 'chat', 'flood')
                for n in range(3):
                    hub._deliver_frame({'channel': 'chat', 'taskId': 'flood',
                                        '_ownerUserId': '17', 'n': n})
                return await _server_close_code(connection)
        finally:
            await asyncio.to_thread(hub.stop)

    assert asyncio.run(exercise()) == 1001


def test_real_socket_receives_large_ordered_backlog_without_pressure_disconnect(push_app, monkeypatch):
    app, hub = push_app
    monkeypatch.setattr(hub, '_register_subscription', lambda *args: None)
    monkeypatch.setattr(hub, '_deregister_subscription', lambda *args: None)
    body = '译' * (3 * 1024 * 1024)
    async def exercise():
        try:
            async with app.test_client().websocket('/api/push') as socket:
                await socket.send_json({'action': 'ping', 't': 1})
                assert (await socket.receive_json())['type'] == 'pong'
                server_client = next(iter(hub._clients))
                hub.subscribe(server_client, 'chat', 'large-task')
                for n in range(1025):
                    hub._deliver_frame({'channel': 'chat', 'taskId': 'large-task',
                                        '_ownerUserId': '17', 'n': n})
                hub._deliver_frame({'channel': 'chat', 'taskId': 'large-task',
                                    '_ownerUserId': '17', 'n': 1025, 'body': body})
                async with asyncio.timeout(20):
                    for n in range(1026):
                        frame = await socket.receive_json()
                        assert frame['n'] == n
                        assert '_ownerUserId' not in frame
                        if n == 1025:
                            assert frame['body'] == body
                assert hub.client_count == 1
            assert hub.client_count == 0
        finally:
            await asyncio.to_thread(hub.stop)
    asyncio.run(exercise())
