"""Offline CLI parity for one read-only file round on Chat and Responses."""
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from tests.support.loopback import LoopbackThreadingHTTPServer
from tests.support.normal_provider_streams import normal_stream
from tests.support.standalone_model_routing import standalone_model_routing_envelope

pytestmark = pytest.mark.api
ROOT = Path(__file__).resolve().parents[2]
CALL_ID = 'call-file-parity'
FINAL = 'Verified the three-line local fixture.'
LINES = ['first: "quoted"', 'second: 雪 and spaces', 'third: exact end']


@pytest.mark.parametrize('protocol', ['openai', 'openai_responses'])
def test_cli_file_read_preserves_arguments_result_and_history_order(tmp_path, protocol):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    fixture = workspace / 'fixture 雪 "quoted".txt'
    fixture.write_text('\n'.join(LINES) + '\n', encoding='utf-8')
    arguments = json.dumps({'path': str(fixture)}, ensure_ascii=False)
    split = arguments.index('雪')
    fragments = [arguments[:split], arguments[split:split + 1], arguments[split + 1:]]
    requests = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append((self.path, body))
            first = len(requests) == 1
            payload = normal_stream(protocol, call_id=CALL_ID,
                response_id=f'resp-{len(requests)}', name='read_files' if first else None,
                fragments=fragments if first else None, text=FINAL if not first else '')
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = LoopbackThreadingHTTPServer(('127.0.0.1', 0), Provider)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    envelope = standalone_model_routing_envelope(
        base_url=f'http://127.0.0.1:{server.server_port}/v1',
        secret='synthetic-file-parity', model_id='file-parity')
    routing = envelope['model_routing']
    routing['connections'][0]['protocol'] = protocol
    for entity in routing['models'] + routing['offerings']:
        entity['capabilities'].append('tools')
    environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'SYSTEMROOT')
                   if key in os.environ}
    environment.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE='1',
        HOME=str(tmp_path / 'home'), TOFU_DATA_DIR=str(tmp_path / 'data'),
        TMPDIR=str(tmp_path), TOFU_AGENT_MODEL_ROUTING=json.dumps(envelope),
        NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
    result_path = tmp_path / 'result.json'
    try:
        completed = subprocess.run([sys.executable, '-m', 'tofu_agent',
            '--env-file', str(tmp_path / 'absent.env'), 'run',
            '--task', 'Read the declared local fixture, then report its verification.',
            '--cwd', str(workspace), '--output', str(result_path), '--timeout-s', '90',
            '--tools', 'none', '--tool-specs', 'read_files', '--trajectory', ''], cwd=workspace,
            env=environment, capture_output=True, text=True, timeout=150)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)
    assert not worker.is_alive()
    assert result_path.is_file(), ('CLI harness did not publish result', completed.stderr[-4000:])
    result = json.loads(result_path.read_text(encoding='utf-8'))
    assert completed.returncode == 0, (result.get('error'), completed.stderr[-4000:])
    assert result['ok'] is True
    assert (result['status'], result['content'], result['n_tool_rounds']) == ('done', FINAL, 1)
    assert len(requests) == 2, ('unexpected provider round count', requests)
    assert [path for path, _ in requests] == [
        '/v1/chat/completions' if protocol == 'openai' else '/v1/responses'] * 2
    retained = requests[1][1]['messages' if protocol == 'openai' else 'input']
    if protocol == 'openai':
        call_index, call_message = next((i, item) for i, item in enumerate(retained)
                                       if item.get('tool_calls'))
        call = call_message['tool_calls'][0]
        assert call['id'] == CALL_ID and call['function']['name'] == 'read_files'
        decoded = json.loads(call['function']['arguments'])
        outputs = [(i, item['content']) for i, item in enumerate(retained)
                   if item.get('role') == 'tool' and item.get('tool_call_id') == CALL_ID]
    else:
        call_index, call = next((i, item) for i, item in enumerate(retained)
                               if item.get('type') == 'function_call')
        assert call['call_id'] == CALL_ID and call['name'] == 'read_files'
        decoded = json.loads(call['arguments'])
        outputs = [(i, item['output']) for i, item in enumerate(retained)
                   if item.get('type') == 'function_call_output' and item.get('call_id') == CALL_ID]
    assert decoded == {'path': str(fixture)}
    user_indices = [i for i, item in enumerate(retained) if item.get('role') == 'user']
    assert user_indices and max(user_indices) < call_index
    assert len(outputs) == 1 and outputs[0][0] > call_index
    output = outputs[0][1]
    assert all(output.count(line) == 1 for line in LINES), output
    assert [output.index(line) for line in LINES] == sorted(output.index(line) for line in LINES)
    assert 'error' not in output.lower(), output
    assert fixture.read_text(encoding='utf-8') == '\n'.join(LINES) + '\n'
