"""Children Tofu starts never inherit the running server's storage handles.

With ``TOFU_STORAGE_CONNECTION_FILE`` inherited, a script that imports
``lib.storage`` attaches to the live engine instead of selecting its own
(docs/contracts/TOOL_EXECUTION_POLICY.md). Each spawn path below once copied or
inherited this process's environment wholesale. A real child spawned through
each one must see none of the storage host's transport keys and must keep the
storage configuration the policy allows.
"""
from __future__ import annotations

import json
import os
import sys
import textwrap

import pytest

from serverctl_pkg.native_storage_host import _TRANSPORT_KEYS

pytestmark = [pytest.mark.unit, pytest.mark.skipif(os.name != 'posix', reason='POSIX children')]

CONFIGURATION = {
    'TOFU_PERSONAL_STORAGE_BACKEND': 'tofudb',
    'TOFUDB_BIN': '/opt/engine/tofu-db',
    'TOFU_STORAGE_FASTPATH': 'auto',
    'TOFU_STORAGE_RPC_CAPACITY': '64',
    'PORT': '15999',
}
REPORTED = sorted({*_TRANSPORT_KEYS, *CONFIGURATION})
MARKER = 'CHILD_ENVIRONMENT='
REPORT = ('import json, os; '
          f'print({MARKER!r} + json.dumps({{name: os.environ.get(name) for name in {REPORTED!r}}}))')

_ENVIRONMENT_MCP_SERVER = textwrap.dedent(r'''
    import json
    import os
    import sys

    names = json.loads(sys.argv[1])

    def send(request_id, **body):
        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': request_id, **body}) + '\n')
        sys.stdout.flush()

    for line in sys.stdin:
        request = json.loads(line)
        method, request_id = request.get('method', ''), request.get('id')
        if request_id is None:
            continue
        if method == 'initialize':
            send(request_id, result={'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}},
                                     'serverInfo': {'name': 'environment-probe', 'version': '1.0'}})
        elif method == 'tools/list':
            send(request_id, result={'tools': [{
                'name': 'environment', 'description': 'Report this server process environment.',
                'inputSchema': {'type': 'object', 'properties': {}}}]})
        elif method == 'tools/call':
            seen = {name: os.environ.get(name) for name in names}
            send(request_id, result={'content': [{'type': 'text', 'text': 'CHILD_ENVIRONMENT='
                                                  + json.dumps(seen)}], 'isError': False})
        else:
            send(request_id, error={'code': -32601, 'message': 'Method not found: ' + method})
''')


@pytest.fixture
def server_environment(monkeypatch):
    """This process as a running server sees it: live handles beside configuration."""
    for name in _TRANSPORT_KEYS:
        monkeypatch.setenv(name, f'live-handle-{name}')
    for name, value in CONFIGURATION.items():
        monkeypatch.setenv(name, value)


def _child_environment(text: str) -> dict:
    line = next((line for line in str(text).splitlines() if line.startswith(MARKER)), None)
    assert line is not None, text
    return json.loads(line[len(MARKER):])


def _assert_withholds_handles_and_keeps_configuration(seen: dict) -> None:
    leaked = sorted(name for name in _TRANSPORT_KEYS if seen.get(name) is not None)
    assert not leaked, f'the child inherited live storage handles: {leaked}'
    assert {name: seen.get(name) for name in CONFIGURATION} == CONFIGURATION


def test_stdio_mcp_server(server_environment, tmp_path, monkeypatch):
    pytest.importorskip('mcp')
    from lib.mcp.client import MCPBridge
    (tmp_path / 'home').mkdir()
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('TOFU_MCP_CACHE_DIR', str(tmp_path / 'cache'))
    # Never consult the deployment's proxy pool during a hermetic MCP test.
    monkeypatch.setattr('lib.mcp.client._vendor._propagate_pool_proxy_env', lambda env: None)
    server = tmp_path / 'environment_mcp.py'
    server.write_text(_ENVIRONMENT_MCP_SERVER, encoding='utf-8')
    bridge = MCPBridge()
    try:
        bridge.connect_server('envprobe', {'transport': 'stdio', 'command': sys.executable,
                                           'args': [str(server), json.dumps(REPORTED)],
                                           'enabled': True})
        seen = _child_environment(bridge.call_tool('mcp__envprobe__environment', {}))
    finally:
        bridge.disconnect_all()
    _assert_withholds_handles_and_keeps_configuration(seen)


def test_scheduled_job_predicate_and_criterion_runner(server_environment, tmp_path):
    """Scheduled jobs and readiness predicates share this runner."""
    from lib.identity import PrincipalContext
    from lib.scheduler.process_runner import run_bounded_process
    result = run_bounded_process(
        [sys.executable, '-c', REPORT], max_runtime=60, job_id='storage-environment-spec',
        job_type='test', cwd=str(tmp_path),
        principal=PrincipalContext.user(subject_id='storage-environment-spec', owner_user_id=1,
                                        scopes={'agents:scheduler'}))
    assert result.ok, result
    _assert_withholds_handles_and_keeps_configuration(_child_environment(result.stdout))


def test_project_brain_checker(server_environment, tmp_path, monkeypatch):
    from lib.conversations import project_brain
    monkeypatch.setattr(project_brain, '_checker_definition', lambda *args, **kwargs: {
        'argv': [sys.executable, '-c', REPORT], 'cwd': '.', 'timeoutMs': 60_000,
        'label': 'environment'})
    monkeypatch.setattr(project_brain, '_command', lambda *args, **kwargs: None)
    result = project_brain.run_checker(str(tmp_path), 'environment', 1, user_id=1)
    assert result['ok'], result
    _assert_withholds_handles_and_keeps_configuration(_child_environment(result['output']))
