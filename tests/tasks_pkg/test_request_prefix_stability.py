"""The assembled request prefix is byte-stable across the rounds of a session.

Prompt caching (OpenAI automatic prefix caching, Anthropic ``cache_control``
breakpoints) only pays when every byte a round already sent is sent again,
unchanged and in the same position, by the next round. This drives a real
storage-free ``tofu_agent`` session (assembly, context composition, tool
dispatch through the gateway, result settlement) against a scripted loopback
provider and diffs consecutive rounds' requests as the provider received them:

* the tool array and the system prompt are byte-identical in every round
  (no reordered tools, timestamps or volatile ids);
* every message of round N reappears byte-identically, at the same index, in
  round N+1, so the history only ever grows at its tail;
* every other top-level request field is identical, except the output
  budget (``max_tokens``), which follows the context left each round and is
  no part of any provider's prompt-cache key.

On the Anthropic wire the conversation-tail breakpoint moves to the newest
message each round by design, so messages are compared with ``cache_control``
removed (a marker is cache metadata, not prompt content); the system and tool
breakpoints never move, so those fields are compared with their markers.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

pytestmark = pytest.mark.api

OWNER = 7
ROUNDS = 6


def _openai_frames(index):
    if index < ROUNDS:
        arguments = json.dumps({'path': f'note-{index}.txt'})
        delta = {'tool_calls': [{'index': 0, 'id': f'call-{index}', 'type': 'function',
                                 'function': {'name': 'read_files', 'arguments': arguments}}]}
        finish = 'tool_calls'
    else:
        delta, finish = {'content': 'Read them all.'}, 'stop'
    frames = [{'choices': [{'index': 0, 'delta': delta}]},
              {'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}],
               'usage': {'prompt_tokens': 100, 'completion_tokens': 10}}]
    return ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames) + 'data: [DONE]\n\n'


def _anthropic_frames(index):
    events = [('message_start', {'type': 'message_start', 'message': {
        'id': f'msg_{index}', 'type': 'message', 'role': 'assistant', 'model': 'wire',
        'content': [], 'stop_reason': None,
        'usage': {'input_tokens': 100, 'output_tokens': 0}}})]
    if index < ROUNDS:
        events += [
            ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {
                'type': 'tool_use', 'id': f'toolu_{index}', 'name': 'read_files', 'input': {}}}),
            ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {
                'type': 'input_json_delta',
                'partial_json': json.dumps({'path': f'note-{index}.txt'})}}),
        ]
        stop = 'tool_use'
    else:
        events += [
            ('content_block_start', {'type': 'content_block_start', 'index': 0,
                                     'content_block': {'type': 'text', 'text': ''}}),
            ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {
                'type': 'text_delta', 'text': 'Read them all.'}}),
        ]
        stop = 'end_turn'
    events += [
        ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
        ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': stop},
                           'usage': {'output_tokens': 10}}),
        ('message_stop', {'type': 'message_stop'}),
    ]
    return ''.join(f'event: {name}\ndata: {json.dumps(data)}\n\n' for name, data in events)


def _provider(requests_seen, frames):
    class Provider(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests_seen.append(request)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(frames(len(requests_seen) - 1).encode())
            self.wfile.flush()
            self.close_connection = True

    return ThreadingHTTPServer(('127.0.0.1', 0), Provider)


def _without_cache_control(value):
    if isinstance(value, dict):
        return {key: _without_cache_control(item) for key, item in value.items()
                if key != 'cache_control'}
    if isinstance(value, list):
        return [_without_cache_control(item) for item in value]
    return value


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _run_session(monkeypatch, tmp_path_factory, protocol, model_id):
    from tests.support.standalone_model_routing import standalone_model_routing_envelope
    from tofu_agent import AgentRuntime, ModelRoutingConfig
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.setenv('no_proxy', '127.0.0.1,localhost')
    requests_seen: list[dict] = []
    server = _provider(requests_seen,
                       _anthropic_frames if protocol == 'anthropic' else _openai_frames)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    envelope = standalone_model_routing_envelope(
        base_url=f'http://127.0.0.1:{server.server_port}/v1', secret='test-only',
        model_id=model_id)
    routing = envelope['model_routing']
    for row in (*routing['models'], *routing['offerings']):
        row['capabilities'].append('tools')
    for connection in routing['connections']:
        connection['protocol'] = protocol
    workspace = tmp_path_factory.mktemp('prefix-stability')
    for index in range(ROUNDS):
        (workspace / f'note-{index}.txt').write_text(f'{index}\n', encoding='utf-8')
    try:
        with AgentRuntime.local(model_routing=ModelRoutingConfig.from_mapping(envelope),
                                owner_user_id=OWNER, max_inflight=1) as runtime:
            result = runtime.run(
                [{'role': 'user', 'content': 'Read every note-N.txt file.'}],
                config={'tools': '*', 'projectPath': str(workspace)}, timeout_s=120)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)
    assert result.status == 'done', result.error
    assert len(requests_seen) == ROUNDS + 1
    return requests_seen


@pytest.mark.parametrize('protocol, model_id', [
    ('openai', 'prefix-stability-test'),
    ('anthropic', 'claude-prefix-stability-test'),
], ids=['openai', 'anthropic'])
def test_consecutive_rounds_resend_the_identical_prefix(
        monkeypatch, tmp_path_factory, protocol, model_id):
    requests = _run_session(monkeypatch, tmp_path_factory, protocol, model_id)
    first = requests[0]
    assert first.get('tools'), 'the session must send a tool surface'
    for number, (earlier, later) in enumerate(zip(requests, requests[1:]), start=1):
        where = f'round {number} -> {number + 1}'
        assert _bytes(later.get('tools')) == _bytes(earlier.get('tools')), where
        assert _bytes(later.get('system')) == _bytes(earlier.get('system')), where
        assert ({key: value for key, value in later.items() if key != 'messages'}.keys()
                == {key: value for key, value in earlier.items() if key != 'messages'}.keys())
        for key in later:
            # The output budget is clamped to the context left each round; it
            # is a generation parameter, outside every provider's cache key.
            if key not in ('messages', 'tools', 'system', 'max_tokens'):
                assert _bytes(later[key]) == _bytes(earlier[key]), (where, key)
        before = _without_cache_control(earlier['messages'])
        after = _without_cache_control(later['messages'])
        assert len(after) > len(before), where
        for index, message in enumerate(before):
            assert _bytes(after[index]) == _bytes(message), (
                f'{where}: message {index} ({message.get("role")}) changed after it was sent')
    if protocol == 'anthropic':
        # The system/tool breakpoints sit in the stable prefix every round.
        assert any('cache_control' in tool for tool in first['tools'])
