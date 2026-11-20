"""Exercise the stdio MCP → ACP connector against a fake installed CLI."""

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


pytestmark = [pytest.mark.unit, pytest.mark.skipif(os.name != 'posix', reason='POSIX process ownership')]
_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def agy(tmp_path):
    script = tmp_path / 'agy'
    script.write_text(f'#!{sys.executable}\n' + textwrap.dedent('''
        import json
        import os
        from pathlib import Path
        import sys

        def receive():
            return json.loads(sys.stdin.readline())

        def send(**fields):
            print(json.dumps(dict(jsonrpc='2.0', **fields)), flush=True)

        def response(request, **result):
            send(id=request['id'], result=result)

        assert sys.argv[1:] == ['acp', '--sandbox']
        assert 'GEMINI_API_KEY' not in os.environ
        assert 'GOOGLE_API_KEY' not in os.environ
        assert 'TOFU_STORAGE_TOKEN' not in os.environ
        assert 'MY_OTHER_CREDENTIAL' not in os.environ
        init = receive()
        assert init['method'] == 'initialize'
        assert init['params']['protocolVersion'] == 1
        assert init['params']['clientCapabilities'] == {}
        scenario = Path(__file__).with_name('scenario').read_text()
        Path(__file__).with_name('pid').write_text(str(os.getpid()))
        browser = scenario.startswith('login_')
        methods = [{'id': 'oauth-personal', 'name': 'Personal Google account'}] if browser else []
        if scenario == 'login_terminal':
            methods[0]['type'] = 'terminal'
        response(init, protocolVersion=2 if scenario == 'version' else 1, authMethods=methods)
        new = receive()
        assert new['method'] == 'session/new'
        assert new['params'] == {'cwd': os.getcwd(), 'mcpServers': []}
        assert list(Path.cwd().iterdir()) == []
        if scenario == 'auth':
            print('fake oauth token in stderr', file=sys.stderr)
            send(id=new['id'], error={'code': -32000, 'message': 'fake oauth token'})
            sys.exit(0)
        if browser:
            send(id=new['id'], error={'code': -32000, 'message': 'fake oauth token'})
            login = sys.stdin.readline()
            if not login:
                sys.exit(0)
            Path(__file__).with_name('login-attempted').touch()
            assert scenario not in ('login_offer', 'login_terminal')
            login = json.loads(login)
            assert login['method'] == 'authenticate'
            assert login['params'] == {'methodId': 'oauth-personal'}
            if scenario == 'login_hold':
                sys.stdin.readline()
                sys.exit(0)
            response(login)
            again = receive()
            assert again['method'] == 'session/new' and again['params'] == new['params']
            if scenario == 'login_still_required':
                send(id=again['id'], error={'code': -32000, 'message': 'fake oauth token'})
                sys.stdin.readline()
                sys.exit(0)
            new = again
        response(new, sessionId='this-turn')
        if scenario == 'status' or browser:
            if sys.stdin.readline():
                Path(__file__).with_name('unexpected-prompt').touch()
            sys.exit(0)
        prompt = receive()
        assert prompt['method'] == 'session/prompt'
        assert prompt['params'] == {'sessionId': 'this-turn',
            'prompt': [{'type': 'text', 'text': 'Please answer: \"hi\"'}]}
        if scenario == 'hold':
            Path(__file__).with_name('ready').touch()
            sys.stdin.readline()
            sys.exit(0)
        send(id=10, method='session/request_permission', params={'sessionId': 'this-turn',
            'options': [{'kind': 'allow_once', 'optionId': 'yes'},
                        {'kind': 'reject_once', 'optionId': 'no'}]})
        assert receive() == {'jsonrpc': '2.0', 'id': 10,
            'result': {'outcome': {'outcome': 'selected', 'optionId': 'no'}}}
        send(id=11, method='terminal/create', params={})
        assert receive()['error']['code'] == -32601
        for session, kind, value in [('other', 'agent_message_chunk', 'wrong session'),
                ('this-turn', 'agent_thought_chunk', 'internal thought'),
                ('this-turn', 'agent_message_chunk', 'Hello '),
                ('this-turn', 'agent_message_chunk', 'there.')]:
            send(method='session/update', params={'sessionId': session, 'update': {
                'sessionUpdate': kind, 'content': {'type': 'text', 'text': value}}})
        if scenario == 'eof':
            sys.exit(0)
        response(prompt, stopReason='max_tokens' if scenario == 'partial' else 'end_turn')
        # Don't exit on a terminal reply: the connector must reap its child.
        sys.stdin.readline()
    '''), encoding='utf-8')
    script.chmod(0o700)
    return script


@pytest.mark.parametrize(('scenario', 'error', 'expected'), [
    ('success', False, 'Hello there.'),
    ('auth', True, 'agy acp --login'),
    ('version', True, 'ACP version'),
    ('eof', True, 'closed'),
    ('partial', True, 'max_tokens'),
])
def test_antigravity_tools_call(agy, scenario, error, expected):
    asyncio.run(_exercise_tool(agy, scenario, error, expected))


async def _exercise_tool(agy, scenario, error, expected):
    agy.with_name('scenario').write_text(scenario)
    launch = StdioServerParameters(
        command=sys.executable,
        args=['-m', 'lib.mcp.antigravity_local', '--agy', str(agy)],
        cwd=str(_ROOT),
        env=dict(os.environ, PYTHONPATH=str(_ROOT), TOFU_AUTH_MODE='private',
                 TOFU_MCP_OWNER_ID='tofu:1', GEMINI_API_KEY='do-not-forward',
                 GOOGLE_API_KEY='do-not-forward', TOFU_STORAGE_TOKEN='do-not-forward',
                 MY_OTHER_CREDENTIAL='do-not-forward'),
    )
    async with stdio_client(launch) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            (tool,) = (await session.list_tools()).tools
            assert tool.name == 'ask_antigravity'
            assert tool.annotations.read_only_hint is False
            assert set(tool.input_schema['properties']) == {'prompt'}
            result = await session.call_tool(tool.name, {'prompt': 'Please answer: "hi"'})
    text = '\n'.join(block.text for block in result.content)
    assert bool(result.is_error) is error
    assert expected in text
    assert 'oauth token' not in text
    assert 'internal thought' not in text
    assert 'wrong session' not in text
    if not error:
        assert text == expected
    _assert_retired(agy)


def _assert_retired(agy):
    with pytest.raises(ProcessLookupError):
        os.kill(int(agy.with_name('pid').read_text()), 0)


@pytest.mark.parametrize(('mode', 'owner'), [('open', 'tofu:1'), ('multi-user', 'tofu:1'), ('private', 'tofu:2')])
def test_connector_refuses_unowned_or_nonprivate_use(agy, mode, owner):
    completed = subprocess.run(
        [sys.executable, '-m', 'lib.mcp.antigravity_local', '--agy', str(agy)],
        cwd=_ROOT, env=dict(os.environ, PYTHONPATH=str(_ROOT), TOFU_AUTH_MODE=mode, TOFU_MCP_OWNER_ID=owner),
        capture_output=True, text=True, check=False, timeout=10,
    )
    assert completed.returncode == 2
    assert 'private single-user Tofu instance and its owner' in completed.stderr
    assert not agy.with_name('pid').exists()


def test_cancellation_retires_the_cli(agy):
    import anyio

    from lib.mcp.antigravity_local import _prompt

    async def exercise():
        async with anyio.create_task_group() as group:
            group.start_soon(_prompt, agy, 'Please answer: "hi"')
            with anyio.fail_after(10):
                while not agy.with_name('ready').exists():
                    await anyio.sleep(0.01)
            group.cancel_scope.cancel()
        _assert_retired(agy)

    agy.with_name('scenario').write_text('hold')
    anyio.run(exercise)


@pytest.fixture
def settings_api(agy, monkeypatch):
    from quart import Quart, g, request

    from lib.api_keys import AuthContext
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    from lib.http_transport.error_handlers import register_http_error_handlers
    from lib.oauth import antigravity
    from routes.api_v1.oauth import api_v1_oauth_bp

    monkeypatch.setenv('TOFU_AUTH_MODE', 'private')
    monkeypatch.setattr(antigravity, '_find_cli', lambda: agy)
    app = Quart(__name__)
    register_http_error_handlers(app)
    app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(api_v1_oauth_bp, name='oauth_v4')))

    @app.before_request
    async def test_auth():
        scopes = {'admin'} if request.headers.get('X-Test-NoAdmin') is None else {'chat'}
        g.auth_ctx = AuthContext(key_id='test', scopes=frozenset(scopes),
                                 owner_user_id=int(request.headers.get('X-Test-Owner', '1')))

    client = app.test_client()
    client.set_cookie('localhost', 'tofu_session', 'tofu_admin_synthetic')
    return client


def test_settings_connects_without_an_mcp_server(settings_api, agy):
    async def exercise():
        path = '/api/v4/oauth/antigravity'
        for scenario, state in [('auth', 'login_required'), ('version', 'unavailable'), ('status', 'ready')]:
            agy.with_name('scenario').write_text(scenario)
            response = await settings_api.get(path)
            assert response.status_code == 200
            assert (await response.get_json())['data'] == {'state': state}
            _assert_retired(agy)
        assert not agy.with_name('unexpected-prompt').exists()
        agy.with_name('scenario').write_text('success')
        response = await settings_api.post(path, headers={'Sec-Fetch-Site': 'same-origin'},
                                           json={'prompt': 'Please answer: "hi"'})
        assert response.status_code == 200
        assert (await response.get_json())['data'] == {'state': 'ready', 'text': 'Hello there.'}
        _assert_retired(agy)

    asyncio.run(exercise())


def test_settings_refuses_other_owners_headless_and_bad_inputs(settings_api, agy):
    async def exercise():
        path = '/api/v4/oauth/antigravity'
        ui = {'Sec-Fetch-Site': 'same-origin'}
        prompt = {'prompt': 'hello'}
        response = await settings_api.get(path, headers={'X-Test-Owner': '2'})
        assert (await response.get_json())['data'] == {'state': 'unsupported'}
        assert (await settings_api.get(path, headers={'X-Test-NoAdmin': '1'})).status_code == 403
        assert (await settings_api.post(path, json=prompt)).status_code == 403
        assert (await settings_api.post(path, headers={**ui, 'Authorization': 'Bearer synthetic'},
                                        json=prompt)).status_code == 403
        for bad in ({'prompt': '   '}, {'prompt': 3}, {'prompt': 'x', 'cwd': '/tmp'}, {'prompt': 'x' * 16385}):
            assert (await settings_api.post(path, headers=ui, json=bad)).status_code == 400
        assert not agy.with_name('pid').exists()

    asyncio.run(exercise())


@pytest.mark.parametrize(('scenario', 'operation', 'expected', 'started'), [
    ('login_offer', 'status', {'state': 'login_required', 'browser_login_available': True}, False),
    ('login_terminal', 'login', {'state': 'login_required'}, False),
    ('login_success', 'login', {'state': 'ready'}, True),
    ('login_still_required', 'login', {'state': 'login_required', 'browser_login_available': True}, True),
])
def test_settings_browser_signin_negotiates_and_verifies(settings_api, agy, scenario, operation, expected, started):
    async def exercise():
        path = '/api/v4/oauth/antigravity'
        agy.with_name('scenario').write_text(scenario)
        if operation == 'status':
            response = await settings_api.get(path)
        else:
            response = await settings_api.post(path + '/login', headers={'Sec-Fetch-Site': 'same-origin'})
        assert response.status_code == 200
        assert (await response.get_json())['data'] == expected
        assert agy.with_name('login-attempted').exists() is started
        assert not agy.with_name('unexpected-prompt').exists()
        _assert_retired(agy)

    asyncio.run(exercise())


def test_settings_signin_refuses_headless_and_other_owners(settings_api, agy):
    async def exercise():
        path = '/api/v4/oauth/antigravity/login'
        ui = {'Sec-Fetch-Site': 'same-origin'}
        assert (await settings_api.post(path)).status_code == 403
        assert (await settings_api.post(path, headers={**ui, 'Authorization': 'Bearer fake'})).status_code == 403
        response = await settings_api.post(path, headers={**ui, 'X-Test-Owner': '2'})
        assert (await response.get_json())['data'] == {'state': 'unsupported'}
        assert not agy.with_name('pid').exists()

    asyncio.run(exercise())


def test_settings_signin_disconnect_reaps_process(settings_api, agy):
    async def exercise():
        agy.with_name('scenario').write_text('login_hold')
        path = '/api/v4/oauth/antigravity/login'
        async with settings_api.request(path, method='POST', headers={'Sec-Fetch-Site': 'same-origin'}) as conn:
            await conn.send_complete()
            async with asyncio.timeout(10):
                while not agy.with_name('login-attempted').exists():
                    await asyncio.sleep(0.01)
            await conn.disconnect()
        _assert_retired(agy)

    asyncio.run(exercise())


def test_settings_deadline_reaps_the_cli(agy, monkeypatch):
    from lib.identity import PrincipalContext
    from lib.oauth import antigravity

    agy.with_name('scenario').write_text('hold')
    monkeypatch.setenv('TOFU_AUTH_MODE', 'private')
    monkeypatch.setattr(antigravity, '_find_cli', lambda: agy)
    monkeypatch.setattr(antigravity, '_SETTINGS_PROMPT_DEADLINE_SECONDS', 1)
    owner = PrincipalContext.user(subject_id='owner', owner_user_id=1, scopes=('admin',))
    assert asyncio.run(antigravity.antigravity_for_owner(owner, prompt='Please answer: "hi"')) == {
        'state': 'unavailable',
    }
    _assert_retired(agy)


@pytest.mark.parametrize('mode', ['open', 'private'])
def test_settings_personal_modes_can_check_missing_cli(settings_api, monkeypatch, mode):
    from lib.oauth import antigravity

    monkeypatch.setenv('TOFU_AUTH_MODE', mode)
    monkeypatch.setattr(antigravity, '_find_cli', lambda: None)

    async def exercise():
        response = await settings_api.get('/api/v4/oauth/antigravity')
        assert (await response.get_json())['data'] == {'state': 'missing_cli'}
    asyncio.run(exercise())


def test_install_requires_same_origin_owner_and_has_no_command_input(settings_api, monkeypatch):
    from lib.oauth import subscription_cli_install

    calls = []

    async def install(provider, principal):
        calls.append((provider, principal.owner_user_id))
        return {'provider': provider, 'status': 'installed', 'version': '1.2.3'}

    monkeypatch.setattr(subscription_cli_install, 'install_subscription_cli', install)

    async def exercise():
        path = '/api/v4/oauth/antigravity/install'
        ui = {'Sec-Fetch-Site': 'same-origin'}
        for headers in ({}, {'Sec-Fetch-Site': 'cross-site'}, {**ui, 'Authorization': 'Bearer synthetic'},
                        {**ui, 'X-Test-NoAdmin': '1'}):
            assert (await settings_api.post(path, headers=headers)).status_code == 403
        assert (await settings_api.post(path, headers=ui, json={'url': 'https://evil.invalid'})).status_code == 400
        assert not calls
        response = await settings_api.post(path, headers=ui)
        assert response.status_code == 200
        assert (await response.get_json())['data']['status'] == 'installed'
        assert calls == [('antigravity', 1)]
    asyncio.run(exercise())
