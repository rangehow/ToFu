"""Scripted local provider for fault injection (Anthropic by default).

Responsibility: answer the isolated instance's model requests from a per-turn
script so a test decides, request by request, whether the provider streams a
reply, a tool call, an HTTP error, malformed SSE, an invalid tool argument
document, a stream that drops mid-response, or a stream that holds open until
the test releases it. Every request is recorded for assertions.

A turn is addressed by the marker ``[chaos turn=<nonce>]`` in its newest user
message; each model request of that turn consumes the next step (the last step
repeats). Requests without a marker (titles, summaries) get one short answer.

Entry: ``ScriptedProvider().start()`` -> ``.base_url``; ``.script(nonce, steps)``;
``.requests(nonce)``; ``.stop()``. Steps are built with the ``text``, ``tool``,
``http_error``, ``malformed``, ``invalid_tool_json``, ``drop`` and ``hold``
helpers below. Standard library only.
"""
from __future__ import annotations

import http.server
import json
import re
import threading
import uuid
from copy import deepcopy

from tests.support.normal_provider_streams import normal_stream

MARKER = re.compile(r'\[chaos turn=([0-9a-f]+)\]')
MODEL_ID = 'chaos-model'


def text(body: str) -> dict:
    return {'kind': 'text', 'text': body}


def tool(name: str, arguments, *, preface: str = '') -> dict:
    """A tool call. ``arguments`` may be a callable of the texts of every tool result in the
    request's history (oldest first), for arguments naming what earlier calls returned."""
    return {'kind': 'tool', 'name': name, 'input': arguments, 'text': preface}


def result_texts(results: list[dict]) -> list[str]:
    """The texts of a recorded request's ``tool_results`` blocks, in order."""
    texts = []
    for block in results:
        content = block.get('content')
        texts.append(content if isinstance(content, str) else ''.join(
            part.get('text', '') for part in content or [] if isinstance(part, dict)))
    return texts


def http_error(status: int, *, error_type: str = 'overloaded_error', retry_after: str | None = None) -> dict:
    return {'kind': 'http_error', 'status': status, 'error_type': error_type, 'retry_after': retry_after}


def malformed(raw: bytes = b'event: content_block_delta\ndata: {"type": "content_block_delta", "index": 0, '
              b'"delta": {"type": "text_delta", "text": \n\n') -> dict:
    return {'kind': 'malformed', 'raw': raw}


def invalid_tool_json(name: str, partial_json: str = '{"command": "echo hi", ') -> dict:
    return {'kind': 'invalid_tool_json', 'name': name, 'partial_json': partial_json}


def drop(prefix: str) -> dict:
    """Stream ``prefix`` as text, then reset the connection before message_stop."""
    return {'kind': 'drop', 'text': prefix}


def hold(prefix: str, release: threading.Event, then: dict) -> dict:
    """Stream ``prefix``, block until ``release`` is set, then play ``then``'s tail."""
    return {'kind': 'hold', 'text': prefix, 'release': release, 'then': then}


def _sse(event: str, payload: dict) -> bytes:
    return f'event: {event}\ndata: {json.dumps(payload, separators=(",", ":"))}\n\n'.encode()


class ScriptedProvider:
    def __init__(self, *, protocol: str = 'anthropic'):
        assert protocol in ('anthropic', 'openai', 'openai_responses')
        self.protocol = protocol
        self._lock = threading.Lock()
        self._scripts: dict[str, list[dict]] = {}
        self._requests: dict[str, list[dict]] = {}
        self.held = threading.Event()  # set while a ``hold`` step blocks
        self._server: http.server.ThreadingHTTPServer | None = None

    # control ---------------------------------------------------------------
    def start(self) -> 'ScriptedProvider':
        self._server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _handler(self))
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, name='chaos-provider', daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @property
    def base_url(self) -> str:
        return f'http://127.0.0.1:{self._server.server_address[1]}'

    def script(self, steps: list[dict]) -> str:
        nonce = uuid.uuid4().hex[:16]
        with self._lock:
            self._scripts[nonce] = list(steps)
            self._requests[nonce] = []
        return nonce

    def requests(self, nonce: str) -> list[dict]:
        with self._lock:
            return deepcopy(self._requests.get(nonce, []))

    # request handling ------------------------------------------------------
    def _next_step(self, body: dict) -> tuple[str | None, dict]:
        messages = body.get('messages') or body.get('input') or []
        nonce = None
        for message in messages:
            if message.get('role') != 'user':
                continue
            content = message.get('content')
            texts = [content] if isinstance(content, str) else [
                block.get('text', '') for block in content or [] if isinstance(block, dict)]
            for value in texts:
                match = MARKER.search(value or '')
                if match:
                    nonce = match.group(1)
        with self._lock:
            if nonce is None or nonce not in self._scripts:
                return None, text('Chaos auxiliary answer.')
            seen = self._requests[nonce]
            steps = self._scripts[nonce]
            step = steps[min(len(seen), len(steps) - 1)]
            results = [block for block in (messages[-1].get('content') if messages else None) or []
                       if isinstance(block, dict) and block.get('type') == 'tool_result']
            if self.protocol == 'openai':
                results = [{'type': 'tool_result', 'tool_use_id': message['tool_call_id'],
                            'content': message['content']} for message in messages
                           if message.get('role') == 'tool']
            elif self.protocol == 'openai_responses':
                results = [{'type': 'tool_result', 'tool_use_id': message['call_id'],
                            'content': message['output']} for message in messages
                           if message.get('type') == 'function_call_output']
            if step['kind'] == 'tool' and callable(step['input']):
                history = [block for message in messages if message.get('role') == 'user'
                           and isinstance(message.get('content'), list)
                           for block in message['content']
                           if isinstance(block, dict) and block.get('type') == 'tool_result']
                step = {**step, 'input': step['input'](result_texts(history))}
            seen.append({'step': step['kind'], 'tool_results': results, 'messages': len(messages),
                         'protocol': self.protocol, 'body': deepcopy(body),
                         'last': json.dumps(messages[-1] if messages else None)[:2000],
                         'tools': sorted(t.get('name', '') for t in body.get('tools') or [] if isinstance(t, dict))})
            return nonce, step


def _handler(provider: ScriptedProvider):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def _json(self, status: int, payload: dict, headers: dict | None = None):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._json(200, {'data': [{'id': MODEL_ID, 'type': 'model'}]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
            endpoint = {'anthropic': '/messages', 'openai': '/chat/completions',
                        'openai_responses': '/responses'}[provider.protocol]
            if not self.path.endswith(endpoint):
                return self._json(404, {'type': 'error', 'error': {'type': 'not_found_error', 'message': self.path}})
            _nonce, step = provider._next_step(body)
            if step['kind'] == 'http_error':
                headers = {'retry-after': step['retry_after']} if step['retry_after'] else {}
                return self._json(step['status'], {'type': 'error', 'error': {
                    'type': step['error_type'], 'message': f'chaos injected {step["status"]}'}}, headers)
            if provider.protocol != 'anthropic':
                assert body.get('stream'), 'ordinary Chat/Responses fixture supports streaming only'
                return self._ordinary_stream(step)
            if not body.get('stream'):
                return self._json(200, self._message(step))
            self._stream(step)

        def _ordinary_stream(self, step):
            assert step['kind'] in ('hold', 'text', 'tool')
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            try:
                if step['kind'] == 'hold':
                    self._write(b': controlled pre-tool hold\n\n')
                    provider.held.set()
                    step['release'].wait()
                    provider.held.clear()
                    step = step['then']
                assert step['kind'] in ('text', 'tool')
                arguments = json.dumps(step['input']) if step['kind'] == 'tool' else ''
                self._write(normal_stream(provider.protocol,
                    call_id='call-' + uuid.uuid4().hex, response_id='resp-' + uuid.uuid4().hex,
                    name=step.get('name'), fragments=[arguments] if arguments else None,
                    text=step.get('text', '')))
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def _message(self, step):
            content = [{'type': 'text', 'text': step.get('text') or 'Done.'}]
            if step['kind'] == 'tool':
                content.append({'type': 'tool_use', 'id': f'toolu_{uuid.uuid4().hex[:20]}',
                                'name': step['name'], 'input': step['input']})
            return {'id': f'msg_{uuid.uuid4().hex[:20]}', 'type': 'message', 'role': 'assistant',
                    'model': MODEL_ID, 'content': content,
                    'stop_reason': 'tool_use' if step['kind'] == 'tool' else 'end_turn',
                    'stop_sequence': None, 'usage': {'input_tokens': 10, 'output_tokens': 10}}

        def _start(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            self._write(_sse('message_start', {'type': 'message_start', 'message': {
                'id': f'msg_{uuid.uuid4().hex[:20]}', 'type': 'message', 'role': 'assistant', 'model': MODEL_ID,
                'content': [], 'stop_reason': None, 'stop_sequence': None,
                'usage': {'input_tokens': 10, 'output_tokens': 1}}}))

        def _write(self, data: bytes):
            self.wfile.write(data)
            self.wfile.flush()

        def _text_block(self, index: int, value: str, *, stop: bool = True):
            self._write(_sse('content_block_start', {'type': 'content_block_start', 'index': index,
                                                     'content_block': {'type': 'text', 'text': ''}}))
            for offset in range(0, len(value), 8):
                self._write(_sse('content_block_delta', {'type': 'content_block_delta', 'index': index,
                                                         'delta': {'type': 'text_delta',
                                                                   'text': value[offset:offset + 8]}}))
            if stop:
                self._write(_sse('content_block_stop', {'type': 'content_block_stop', 'index': index}))

        def _finish(self, stop_reason: str):
            self._write(_sse('message_delta', {'type': 'message_delta',
                                               'delta': {'stop_reason': stop_reason, 'stop_sequence': None},
                                               'usage': {'output_tokens': 10}}))
            self._write(_sse('message_stop', {'type': 'message_stop'}))

        def _tail(self, step, index: int):
            if step['kind'] == 'text':
                self._text_block(index, step['text'])
                return self._finish('end_turn')
            if step['kind'] in ('tool', 'invalid_tool_json'):
                if step.get('text'):
                    self._text_block(index, step['text'])
                    index += 1
                arguments = step['partial_json'] if step['kind'] == 'invalid_tool_json' else json.dumps(step['input'])
                self._write(_sse('content_block_start', {'type': 'content_block_start', 'index': index,
                                                         'content_block': {'type': 'tool_use',
                                                                           'id': f'toolu_{uuid.uuid4().hex[:20]}',
                                                                           'name': step['name'], 'input': {}}}))
                self._write(_sse('content_block_delta', {'type': 'content_block_delta', 'index': index,
                                                         'delta': {'type': 'input_json_delta',
                                                                   'partial_json': arguments}}))
                self._write(_sse('content_block_stop', {'type': 'content_block_stop', 'index': index}))
                return self._finish('tool_use')
            raise AssertionError(f'no stream tail for {step["kind"]}')

        def _abort(self):
            # An RST, not a clean FIN: the provider vanished mid-response.
            import socket
            import struct
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            self.connection.close()

        def _stream(self, step):
            try:
                self._start()
                kind = step['kind']
                if kind == 'malformed':
                    self._write(step['raw'])
                    return self._abort()
                if kind == 'drop':
                    self._text_block(0, step['text'], stop=False)
                    return self._abort()
                if kind == 'hold':
                    self._text_block(0, step['text'])
                    provider.held.set()
                    step['release'].wait()
                    provider.held.clear()
                    return self._tail(step['then'], 1)
                self._tail(step, 0)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
    return Handler
