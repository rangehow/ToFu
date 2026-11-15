"""Security regression tests for the /api/push WebSocket trust boundary."""

from __future__ import annotations

import threading

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _stable_auth(monkeypatch):
    monkeypatch.setattr('routes.api_v1.auth._OPEN_MODE_ALLOW_REMOTE', False)


def _resolve(monkeypatch, *, private=False, peer=('127.0.0.1', 1),
             headers=None, cookies=None, ctx=None):
    monkeypatch.setattr('lib.auth_mode.requires_credential', lambda: private)
    monkeypatch.setattr('lib.api_keys.validate_token', lambda _token: ctx)
    from routes.push import _resolve_push_ws_auth
    return _resolve_push_ws_auth(headers or {}, cookies or {}, peer)


def test_open_mode_keeps_zero_setup_loopback_socket(monkeypatch):
    ctx, reason = _resolve(monkeypatch)
    assert ctx is not None and ctx.via_open_mode is True
    assert reason == 'open_local'


@pytest.mark.parametrize('peer', [
    ('203.0.113.7', 5555), ('10.0.0.8', 5555), ('2001:db8::1', 5555),
])
def test_open_mode_remote_socket_is_rejected_without_opt_in(monkeypatch, peer):
    ctx, reason = _resolve(monkeypatch, peer=peer)
    assert ctx is None and reason == 'credential_required'


def test_open_mode_explicit_remote_opt_in_is_preserved(monkeypatch):
    monkeypatch.setattr('routes.api_v1.auth._OPEN_MODE_ALLOW_REMOTE', True)
    ctx, reason = _resolve(monkeypatch, peer=('203.0.113.7', 5555))
    assert ctx is not None and ctx.via_open_mode is True
    assert reason == 'open_local'


def test_private_mode_requires_a_socket_credential(monkeypatch):
    ctx, reason = _resolve(monkeypatch, private=True)
    assert ctx is None and reason == 'credential_required'


def test_private_mode_carries_valid_token_owner(monkeypatch):
    from lib.api_keys import AuthContext
    expected = AuthContext(key_id='k1', owner_user_id=17,
                           account_user_id='alice',
                           scopes=frozenset({'chat'}))
    ctx, reason = _resolve(
        monkeypatch, private=True, ctx=expected,
        headers={'Authorization': 'Bearer tofu_live_test'})
    assert ctx is expected and ctx.owner_user_id == 17
    assert reason == 'token'


def test_private_mode_rejects_a_bad_socket_token(monkeypatch):
    ctx, reason = _resolve(
        monkeypatch, private=True, ctx=None,
        headers={'Authorization': 'Bearer tofu_live_bad'})
    assert ctx is None and reason == 'invalid_token'


@pytest.mark.parametrize('headers', [
    {'Host': '127.0.0.1:15000', 'Origin': 'https://evil.example',
     'Sec-Fetch-Site': 'cross-site'},
    {'Host': 'localhost:15000', 'Origin': 'http://localhost:3000',
     'Sec-Fetch-Site': 'same-site'},
    # Browsers always send Origin on a socket handshake; CORS never applies.
    {'Host': '127.0.0.1:15000', 'Origin': 'https://evil.example'},
    {'Host': '127.0.0.1:15000', 'Origin': 'null'},
])
def test_open_mode_refuses_cross_site_browser_socket(monkeypatch, headers):
    """Another site's page cannot read the owner's push stream or call RPCs."""
    ctx, reason = _resolve(monkeypatch, headers=headers)
    assert ctx is None and reason == 'cross_site'
    ctx, reason = _resolve(
        monkeypatch, ctx=None,
        headers={**headers, 'Authorization': 'Bearer tofu_live_stale'})
    assert ctx is None


@pytest.mark.parametrize('headers', [
    {'Host': '127.0.0.1:15000', 'Origin': 'http://127.0.0.1:15000',
     'Sec-Fetch-Site': 'same-origin'},
    {'Host': '127.0.0.1:15000', 'Origin': 'https://ide.example',
     'Sec-Fetch-Site': 'same-origin'},
    {'Host': 'localhost:15000', 'Origin': 'http://localhost:15000'},
    {'Host': '127.0.0.1:15000'},
])
def test_open_mode_keeps_same_origin_and_non_browser_sockets(monkeypatch, headers):
    ctx, reason = _resolve(monkeypatch, headers=headers)
    assert ctx is not None and ctx.via_open_mode is True
    assert reason == 'open_local'


def test_http_and_ws_share_loopback_address_parser():
    from routes.api_v1.auth import address_is_loopback
    assert address_is_loopback(('127.0.0.1', 5000))
    assert address_is_loopback(('::1', 5000))
    assert address_is_loopback(('::ffff:127.0.0.1', 5000))
    assert not address_is_loopback(('203.0.113.7', 5000))
    assert not address_is_loopback(None)


@pytest.fixture
def _task_registry():
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
    with tasks_lock:
        old = dict(tasks)
        tasks.clear()
    try:
        yield tasks, tasks_lock
    finally:
        with tasks_lock:
            tasks.clear()
            tasks.update(old)


def test_scoped_socket_cannot_abort_another_users_task(_task_registry):
    tasks, lock = _task_registry
    evt = threading.Event()
    task = {'id': 'foreign-task', '_userId': 18, 'aborted': False,
            'abort_event': evt}
    with lock:
        tasks[task['id']] = task

    from routes.push import _handle_abort
    _handle_abort(task['id'], req_id='owner-17-ws', user_id=17)
    assert task['aborted'] is False
    assert not evt.is_set()


def test_owner_can_abort_and_ownerless_socket_is_denied(_task_registry):
    tasks, lock = _task_registry
    from routes.push import _handle_abort

    owner_evt = threading.Event()
    owner_task = {'id': 'owner-task', '_userId': 17, 'aborted': False,
                  'abort_event': owner_evt}
    denied_evt = threading.Event()
    denied_task = {'id': 'denied-task', '_userId': 19, 'aborted': False,
                   'abort_event': denied_evt}
    with lock:
        tasks[owner_task['id']] = owner_task
        tasks[denied_task['id']] = denied_task

    _handle_abort(owner_task['id'], req_id='owner-17-ws', user_id=17)
    _handle_abort(denied_task['id'], req_id='ownerless-ws', user_id='')
    assert owner_task['aborted'] is True and owner_evt.is_set()
    assert denied_task['aborted'] is False and not denied_evt.is_set()


def test_push_handler_fails_closed_before_registering_socket():
    """Static order guard: rejection must precede PushClient/hub.register."""
    import ast
    import inspect
    from routes import push

    src = inspect.getsource(push.push_ws)
    tree = ast.parse(src)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else '')
            if name in {'abort', 'PushClient', 'register'}:
                calls.append((node.lineno, name))
    first = {name: min(line for line, n in calls if n == name)
             for name in {'abort', 'PushClient', 'register'}}
    assert first['abort'] < first['PushClient'] < first['register']


# What reaches the server through a path-prefixed IDE gateway (for example
# https://<id>-vscode-….mlp.internal.example.com/proxy/15000): the gateway rewrites
# Host to its upstream, the browser's Origin stays the public page, and
# Chrome sends no Fetch Metadata on a WebSocket handshake.
_GATEWAY_ORIGIN = 'https://67dc97fd-vscode-shxstraining.mlp.internal.example.com'
_PROXIED_PAGE_SOCKET = {'Host': '127.0.0.1:15000', 'Origin': _GATEWAY_ORIGIN}
_SECRET = 'push-proof-test-secret'
_NONCE_A = 'a' * 43
_NONCE_B = 'b' * 43


def test_proxied_page_socket_needs_the_same_origin_proof(monkeypatch):
    ctx, reason = _resolve(monkeypatch, headers=_PROXIED_PAGE_SOCKET)
    assert ctx is None and reason == 'cross_site'

    monkeypatch.setattr('lib.auth_mode.requires_credential', lambda: False)
    from routes.push import _resolve_push_ws_auth
    ctx, reason = _resolve_push_ws_auth(
        _PROXIED_PAGE_SOCKET, {}, ('127.0.0.1', 1), True)
    assert ctx is not None and ctx.via_open_mode is True
    assert reason == 'open_local'


@pytest.mark.parametrize('site', ['cross-site', 'same-site'])
def test_same_origin_proof_never_overrides_fetch_metadata(monkeypatch, site):
    monkeypatch.setattr('lib.auth_mode.requires_credential', lambda: False)
    from routes.push import _resolve_push_ws_auth
    ctx, reason = _resolve_push_ws_auth(
        {**_PROXIED_PAGE_SOCKET, 'Sec-Fetch-Site': site}, {},
        ('127.0.0.1', 1), True)
    assert ctx is None and reason == 'cross_site'


def test_proof_is_bound_to_one_browser_session():
    from routes.api_v1.auth import (
        PUSH_SESSION_COOKIE, SESSION_COOKIE, push_origin_proof,
        push_origin_proof_matches)
    session_a = {PUSH_SESSION_COOKIE: _NONCE_A}
    proof_a = push_origin_proof(_SECRET, '', _NONCE_A)
    assert push_origin_proof_matches(_SECRET, session_a, proof_a)
    # Another browser session (its own nonce cookie) cannot reuse it.
    assert not push_origin_proof_matches(
        _SECRET, {PUSH_SESSION_COOKIE: _NONCE_B}, proof_a)
    # Login and logout change the session credential, so the proof changes.
    logged_in = {**session_a, SESSION_COOKIE: 'tofu_live_alice'}
    assert not push_origin_proof_matches(_SECRET, logged_in, proof_a)
    proof_logged_in = push_origin_proof(_SECRET, 'tofu_live_alice', _NONCE_A)
    assert push_origin_proof_matches(_SECRET, logged_in, proof_logged_in)
    assert not push_origin_proof_matches(_SECRET, session_a, proof_logged_in)
    assert not push_origin_proof_matches(
        _SECRET, {**session_a, SESSION_COOKIE: 'tofu_live_bob'}, proof_logged_in)
    # No session cookie, a malformed one, another install or a truncated
    # proof never verify.
    assert not push_origin_proof_matches(_SECRET, {}, proof_a)
    assert not push_origin_proof_matches(
        _SECRET, {PUSH_SESSION_COOKIE: 'short'}, push_origin_proof(_SECRET, '', 'short'))
    assert not push_origin_proof_matches('another-install', session_a, proof_a)
    assert not push_origin_proof_matches(_SECRET, session_a, proof_a[:-1])
    assert push_origin_proof(_SECRET, '', '') == ''


def test_shell_mints_a_session_nonce_and_publishes_its_proof():
    import asyncio
    import json
    from lib.app.app_factory import create_base_app
    from routes.api_v1.auth import (
        PUSH_SESSION_COOKIE, SESSION_COOKIE, push_origin_proof)
    from routes.common import (
        _boot_config_tag, _push_session_for_request, _with_push_session)

    app = create_base_app('boot-proof-test', {'TESTING': True})
    app.secret_key = _SECRET

    async def shell(cookie_header=None):
        headers = {'Cookie': cookie_header} if cookie_header else {}
        async with app.test_request_context('/', headers=headers):
            proof, minted = _push_session_for_request()
            tag = _boot_config_tag('main', proof)
            response = _with_push_session(app.response_class('x'), minted)
            return proof, minted, tag, response.headers.getlist('Set-Cookie')

    proof, minted, tag, cookies = asyncio.run(shell())
    assert minted and proof == push_origin_proof(_SECRET, '', minted)
    payload = json.loads(tag.split('>', 1)[1].rsplit('</script>', 1)[0])
    assert payload['pushOriginProof'] == proof
    (cookie,) = cookies
    assert cookie.startswith(f'{PUSH_SESSION_COOKIE}={minted};')
    assert 'HttpOnly' in cookie and 'SameSite=Strict' in cookie

    again, reminted, _tag, cookies = asyncio.run(
        shell(f'{PUSH_SESSION_COOKIE}={minted}'))
    assert again == proof and reminted == '' and cookies == []
    logged_in, _m, _t, _c = asyncio.run(
        shell(f'{PUSH_SESSION_COOKIE}={minted}; {SESSION_COOKIE}=tofu_live_alice'))
    assert logged_in != proof


def _free_port():
    import socket
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _serve_push_and_connect(monkeypatch, attempts):
    """Real Hypercorn + real `websockets` client and the real access log."""
    import asyncio
    import logging
    websockets = pytest.importorskip('websockets')
    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    from lib.agent_core.push import PushHub
    from lib.app.app_factory import create_base_app
    import routes.push as push

    monkeypatch.setattr('lib.auth_mode.requires_credential', lambda: False)
    monkeypatch.setattr(push, 'hub', PushHub())
    app = create_base_app('push-proxy-origin-test', {'TESTING': True})
    app.secret_key = _SECRET
    app.register_blueprint(push.push_bp)

    access_lines = []

    class _Capture(logging.Handler):
        def emit(self, record):
            access_lines.append(record.getMessage())

    access_logger = logging.getLogger('test.push.access')
    access_logger.addHandler(_Capture())
    access_logger.setLevel(logging.INFO)
    port = _free_port()
    config = Config()
    config.bind = [f'127.0.0.1:{port}']
    config.accesslog = access_logger

    async def scenario():
        stop = asyncio.Event()
        server = asyncio.create_task(serve(app, config, shutdown_trigger=stop.wait))
        for _ in range(200):
            try:
                _r, writer = await asyncio.open_connection('127.0.0.1', port)
                writer.close()
                break
            except OSError:
                await asyncio.sleep(0.02)
        results = []
        try:
            for query, protocols, cookie in attempts:
                headers = {'Cookie': cookie} if cookie else {}
                try:
                    async with websockets.connect(
                            f'ws://127.0.0.1:{port}/api/push{query}',
                            origin=_GATEWAY_ORIGIN, subprotocols=protocols,
                            additional_headers=headers) as connection:
                        await connection.send('{"action":"ping","t":7}')
                        frame = await asyncio.wait_for(connection.recv(), 3)
                        results.append(('open', connection.subprotocol, frame))
                except websockets.exceptions.InvalidStatus as rejected:
                    results.append(('rejected', rejected.response.status_code, None))
        finally:
            stop.set()
            await server
        return results

    return asyncio.run(scenario()), access_lines


def test_push_socket_stays_up_behind_a_host_rewriting_prefix_proxy(monkeypatch):
    """The owner's page behind /proxy/15000 opens its push socket, and the
    proof appears in no access-log line."""
    import json
    from routes.api_v1.auth import PUSH_SESSION_COOKIE, push_origin_proof
    proof_a = push_origin_proof(_SECRET, '', _NONCE_A)
    offered = ['tofu.push.v1', 'tofu.origin-proof.' + proof_a]
    results, access_lines = _serve_push_and_connect(monkeypatch, [
        ('?_rid=proxy-ws1', offered, f'{PUSH_SESSION_COOKIE}={_NONCE_A}'),
        # A proof minted for another browser session is refused.
        ('?_rid=proxy-ws2', offered, f'{PUSH_SESSION_COOKIE}={_NONCE_B}'),
        # Without the session cookie (another site cannot send a Strict one).
        ('?_rid=proxy-ws3', offered, None),
        # The retired URL transport is not honoured.
        ('?_rid=proxy-ws4&originProof=' + proof_a, ['tofu.push.v1'],
         f'{PUSH_SESSION_COOKIE}={_NONCE_A}'),
    ])
    status, subprotocol, frame = results[0]
    assert status == 'open' and subprotocol == 'tofu.push.v1'
    assert json.loads(frame)['type'] == 'pong'
    assert results[1:] == [('rejected', 401, None)] * 3
    assert len(access_lines) >= 4
    assert not [line for line in access_lines if proof_a in line]
