"""The Rust node's ``codex_login_read`` against the Python Home.

Real processes: the Python Home (``live_server``, in this process) and the Rust
node (``tofu node --allow-codex-login``) with a temporary ``HOME`` and
``CODEX_HOME`` fixture (never the real ``~/.codex``). The installed Codex is the
synthetic app-server of tests/oauth/test_codex_command_login.py, found through
the account shell's startup file exactly as the node finds a real one; the
node's own ``PATH`` holds no ``codex``. Covers the native read, native renewal
of an expired access token, the file seam once the command disappears, the
param validation and the account check. Results never carry a refresh token.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import time

import pytest

from tests.desktop.test_rust_node_interop import OWNER, _api, _nodes, _stop, _wait
from tests.support.tofu_node import node_command

pytestmark = [pytest.mark.slow, pytest.mark.home_ceiling,
              pytest.mark.skipif(os.name == 'nt', reason='POSIX native-shell adapter')]

HOST = ('codex-machine', 'codex-host', 'c' * 64)

FAKE_CODEX = r'''
import json, os, pathlib, sys, time, base64
root = pathlib.Path(os.environ['CODEX_HOME'])
trace = root.parent / 'trace.jsonl'
trace.open('a').write(json.dumps({'pid': os.getpid(), 'event': 'start'}) + '\n')
for line in sys.stdin:
    msg = json.loads(line); method = msg.get('method')
    trace.open('a').write(json.dumps({'pid': os.getpid(), 'event': method}) + '\n')
    if not msg.get('id'): continue
    if method == 'initialize': result = {'codexHome': str(root)}
    elif method == 'config/read': result = {'config': {}}
    elif method == 'configRequirements/read': result = {'requirements': None}
    elif method == 'account/read':
        path = root / 'auth.json'; data = json.loads(path.read_text())
        claims = {'exp': time.time() + 7200,
                  'https://api.openai.com/auth': {'chatgpt_account_id': data['tokens']['account_id']}}
        data['tokens']['access_token'] = 'header.' + base64.urlsafe_b64encode(
            json.dumps(claims).encode()).decode().rstrip('=') + '.renewed'
        following = path.with_suffix('.next')
        following.write_text(json.dumps(data)); following.chmod(0o600); os.replace(following, path)
        result = {'account': {'type': 'chatgpt'}}
    else: result = {}
    print(json.dumps({'id': msg['id'], 'result': result}), flush=True)
'''


def _token(account='account-a', expired=False):
    claims = {'exp': time.time() + (-10 if expired else 3600),
              'https://api.openai.com/auth': {'chatgpt_account_id': account}}
    jwt = 'header.' + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=') + '.signature'
    return {'auth_mode': 'chatgpt', 'tokens': {'access_token': jwt,
            'refresh_token': 'never-copy-refresh', 'account_id': account}}


def _write_auth(root: Path, **kwargs) -> None:
    path = root / 'auth.json'
    path.write_text(json.dumps(_token(**kwargs)))
    path.chmod(0o600)


def _events(trace: Path) -> list[str]:
    if not trace.exists():
        return []
    return [json.loads(line)['event'] for line in trace.read_text().splitlines()]


def test_codex_login_read_through_the_rust_node(live_server, tmp_path):
    from lib.desktop import bridge as db
    from lib.desktop.bridge import send_desktop_command
    from lib.desktop_wire.node_protocol import build_pairing_link, parse_pairing_link

    shell = pwd.getpwuid(os.getuid()).pw_shell
    if Path(shell).name not in ('bash', 'zsh'):
        pytest.skip(f'account shell {shell} reads neither .bashrc nor .zshrc')
    home = tmp_path / 'home'
    home.mkdir()
    root = tmp_path / 'codex-home'
    root.mkdir()
    _write_auth(root)
    (root / 'config.toml').write_text('')
    (root / 'config.toml').chmod(0o600)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    program = bin_dir / 'codex'
    program.write_text(f'#!{sys.executable} -S\n{FAKE_CODEX}')
    program.chmod(0o700)
    startup = f'export PATH="{bin_dir}:$PATH"\n'
    for name in ('.bashrc', '.zshrc'):
        (home / name).write_text(startup)
    trace = tmp_path / 'trace.jsonl'
    config = home / 'desktop_agent.json'
    log = tmp_path / 'node.log'
    proc = None
    try:
        status, minted = _api(live_server, 'POST', f'/api/v4/desktop/pair-code?base={live_server}/', {})
        assert status == 201, minted
        link = build_pairing_link(live_server, parse_pairing_link(minted['link']).secret)
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'SHELL': shell, 'CODEX_HOME': str(root),
               'TOFU_DESKTOP_CONFIG': str(config), 'NO_PROXY': '*', 'no_proxy': '*',
               'OPENAI_API_KEY': 'never-forwarded'}
        with log.open('ab') as handle:
            proc = subprocess.Popen(node_command('--simulate-host', *HOST, '--join', link,
                                                 '--allow-codex-login', '--poll-interval', '0.1'),
                                    env=env, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
        _wait(lambda: 'fingerprint' in log.read_text() or proc.poll() is not None, 'joined')
        assert proc.poll() is None, log.read_text()[-2000:]
        key = subprocess.run(node_command('--simulate-host', *HOST, '--host-key'), capture_output=True,
                             text=True, check=True).stdout.strip()
        node_id = json.loads(config.read_text())['agent_ids'][key]
        status, body = _api(live_server, 'POST', f'/api/v4/desktop/nodes/{node_id}/confirm', {})
        assert status == 200, body
        status, snapshot = _api(live_server, 'GET', '/api/v4/devices/permissions')
        assert status == 200, snapshot
        status, body = _api(live_server, 'PUT', f'/api/v4/devices/{node_id}/ceiling',
                            {'expected_revision': snapshot['rev'], 'tiers': {'read': True, 'codex_login': True}})
        assert status == 200, body
        _wait(lambda: _nodes(live_server)[node_id]['online'] and _nodes(live_server)[node_id]['tiers']['codex_login'],
              'codex_login effective')

        def read(params):
            return send_desktop_command('codex_login_read', params, timeout=30,
                                        target_agent_id=node_id, user_id=OWNER, ttl=30)

        # Native: the installed Codex names the home; access-only snapshot.
        result, error = read({'account_id': 'account-a'})
        assert error is None and result['account_id'] == 'account-a', (result, error, log.read_text()[-2000:])
        assert result['access_token'].endswith('.signature') and result['credential_source'] == 'local_codex'
        assert result['shared_login_issue'] == '' and 'refresh_token' not in json.dumps(result)
        assert _events(trace)[:2] == ['start', 'initialize'] and 'account/read' not in _events(trace)

        # Expired: native Codex renews (account/read), the node re-reads the renewed token.
        _write_auth(root, expired=True)
        result, error = read({})
        assert error is None and result['access_token'].endswith('.renewed'), (result, error)
        assert 'account/read' in _events(trace)

        # A different expected account is refused; params are validated on the node.
        result, error = read({'account_id': 'account-b'})
        assert result == {'shared_login_issue': 'account_changed'}, (result, error)
        result, error = read({'path': '/etc/passwd'})
        assert 'invalid_codex_login_request' in str(error or result), (result, error)

        # The command disappears (startup file changed): the file seam reads $CODEX_HOME.
        for name in ('.bashrc', '.zshrc'):
            (home / name).write_text('')
        starts = _events(trace).count('start')
        _write_auth(root, account='account-a')
        result, error = read({})
        assert error is None and result['access_token'].endswith('.signature'), (result, error)
        assert _events(trace).count('start') == starts
        assert 'never-copy-refresh' not in log.read_text() and 'never-forwarded' not in log.read_text()
        # Credential reads and failures deliver normally, but neither secrets
        # nor diagnostic bodies survive in the shared node journal.
        records = [json.loads(path.read_text()) for path in config.with_suffix('.calls').glob('*.json')]
        assert records and all(row['state'] == 'sensitive_done' for row in records)
        assert all(set(row) == {'id', 'type', 'state', 'at'} for row in records)
        from lib.desktop_agent._journal import CallJournal
        python_journal = CallJournal(str(config.with_suffix('.calls')))
        for row in records:
            repeated = python_journal.begin({'id': row['id'], 'type': 'codex_login_read', 'params': {}})
            assert repeated['result'] is None and 'not run again' in repeated['error']
    finally:
        _stop(proc)
        for listed in list(_nodes(live_server)):
            _api(live_server, 'DELETE', f'/api/v4/desktop/nodes/{listed}')
        with db.command_queue_lock:
            db.command_queue.clear()
            db._agents.clear()
