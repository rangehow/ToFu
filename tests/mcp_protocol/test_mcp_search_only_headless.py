"""Headless runs keep MCP search-only end to end (owner decision 2026-11-13).

A real storage-free ``tofu_agent`` runtime bridges a loopback stdio MCP server
and talks to a scripted loopback model over HTTP, so every assertion reads the
exact provider request. With ``tools: "*"`` and with the lean prompt profile:

* no request declares an MCP tool, and the first request (the benchmark's
  clean context: system prompt, user turn, every tool schema) carries no MCP
  text at all;
* the model still finds the loopback tool with ``search_tools`` by what it
  does, and ``execute_tools`` runs it against the server, so MCP stays
  available. Policy owner: ``lib/tools/mcp_exposure.py``.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import textwrap
import threading

import pytest

pytestmark = pytest.mark.api

OWNER = 7
SERVER = 'loop'
TOOL = f'mcp__{SERVER}__weather_forecast'
ANSWER = 'Paris will be sunny.'

# A minimal MCP 2025 stdio peer: one read-only tool that reports a forecast.
LOOPBACK_MCP_SERVER = textwrap.dedent(
    r'''
    import json
    import sys

    def send(request_id, *, result=None, error=None):
        message = {'jsonrpc': '2.0', 'id': request_id}
        message['error' if error is not None else 'result'] = (
            error if error is not None else result)
        sys.stdout.write(json.dumps(message, separators=(',', ':')) + '\n')
        sys.stdout.flush()

    for line in sys.stdin:
        request = json.loads(line)
        method, request_id = request.get('method', ''), request.get('id')
        if request_id is None:
            continue
        if method == 'initialize':
            send(request_id, result={
                'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}},
                'serverInfo': {'name': 'loopback-weather', 'version': '1.0'}})
        elif method == 'tools/list':
            send(request_id, result={'tools': [{
                'name': 'weather_forecast',
                'description': 'Return the weather forecast for a city.',
                'inputSchema': {'type': 'object', 'required': ['city'],
                                'properties': {'city': {'type': 'string'}}},
                'annotations': {'readOnlyHint': True}}]})
        elif method == 'tools/call':
            city = request['params']['arguments']['city']
            send(request_id, result={'isError': False, 'content': [
                {'type': 'text', 'text': f'sunny in {city}'}]})
        else:
            send(request_id, error={'code': -32601, 'message': 'Method not found'})
    '''
)


def _tool_call(call_id, name, arguments):
    return {'tool_calls': [{'index': 0, 'id': call_id, 'type': 'function',
                            'function': {'name': name, 'arguments': json.dumps(arguments)}}]}


SCRIPT = (
    (_tool_call('search-1', 'search_tools', {'query': 'weather forecast for a city'}),
     'tool_calls'),
    (_tool_call('execute-1', 'execute_tools',
                {'calls': [{'name': TOOL, 'arguments': {'city': 'Paris'}}]}), 'tool_calls'),
    ({'content': ANSWER}, 'stop'),
)


def _scripted_provider(requests_seen):
    class Provider(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests_seen.append(request)
            delta, finish = SCRIPT[min(len(requests_seen), len(SCRIPT)) - 1]
            frames = [
                {'choices': [{'index': 0, 'delta': delta}]},
                {'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}],
                 'usage': {'prompt_tokens': 100, 'completion_tokens': 10}},
            ]
            wire = ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write((wire + 'data: [DONE]\n\n').encode())
            self.wfile.flush()
            self.close_connection = True

    return ThreadingHTTPServer(('127.0.0.1', 0), Provider)


@pytest.fixture
def loopback_mcp(tmp_path_factory):
    """Owner 7's bridge connected to the loopback server, as Settings -> MCP would."""
    from lib.mcp import get_bridge
    root = tmp_path_factory.mktemp('loopback-server')
    script = root / 'weather_server.py'
    script.write_text(LOOPBACK_MCP_SERVER, encoding='utf-8')
    bridge = get_bridge(OWNER)
    tools = bridge.connect_server(SERVER, {
        'transport': 'stdio', 'command': sys.executable, 'args': [str(script)],
        'enabled': True})
    assert [tool.name for tool in tools] == ['weather_forecast']
    try:
        yield bridge
    finally:
        bridge.disconnect_all()


def _names(request):
    return [str((tool.get('function') or {}).get('name') or '')
            for tool in request.get('tools') or ()]


def _tool_results(request):
    return '\n'.join(str(message.get('content') or '') for message in request['messages']
                     if message.get('role') == 'tool')


@pytest.mark.parametrize('config', [
    {'tools': '*'},
    {'tools': '*', 'responses': {'promptProfile': 'lean'}},
], ids=['tools-star', 'lean-profile'])
def test_headless_request_is_mcp_clean_and_reaches_mcp_through_search(
        loopback_mcp, monkeypatch, tmp_path_factory, config):
    from tests.support.standalone_model_routing import standalone_model_routing_envelope
    from tofu_agent import AgentRuntime, ModelRoutingConfig
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.setenv('no_proxy', '127.0.0.1,localhost')
    requests_seen: list[dict] = []
    server = _scripted_provider(requests_seen)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    envelope = standalone_model_routing_envelope(
        base_url=f'http://127.0.0.1:{server.server_port}/v1', secret='test-only',
        model_id='headless-clean-test')
    for row in (*envelope['model_routing']['models'], *envelope['model_routing']['offerings']):
        row['capabilities'].append('tools')
    workspace = tmp_path_factory.mktemp('benchmark-task')
    try:
        with AgentRuntime.local(model_routing=ModelRoutingConfig.from_mapping(envelope),
                                owner_user_id=OWNER, max_inflight=1) as runtime:
            result = runtime.run(
                [{'role': 'user', 'content': 'What is the weather in Paris?'}],
                config={**config, 'projectPath': str(workspace)}, timeout_s=120)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)

    assert result.status == 'done' and result.content == ANSWER, result.error
    assert len(requests_seen) == len(SCRIPT)
    first = requests_seen[0]
    assert {'search_tools', 'execute_tools'} <= set(_names(first))
    assert 'mcp' not in json.dumps(first, ensure_ascii=False).lower()
    for request in requests_seen:
        assert not [name for name in _names(request) if name.startswith('mcp__')]
    # Search found the tool by what it does; execute_tools ran it on the server.
    assert TOOL in _tool_results(requests_seen[1])
    assert 'sunny in Paris' in _tool_results(requests_seen[2])
