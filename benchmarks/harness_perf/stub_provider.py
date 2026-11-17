#!/usr/bin/env python3
"""Deterministic zero-latency model provider for harness overhead measurement.

Responsibility: answer every model request of a scripted scenario instantly,
on all three wire faces the measured harnesses speak, and record what the
harness sent and when, so harness time is the only time in a round.

Faces (POST; with or without a ``/v1`` prefix; ``stream`` true or false):
    /v1/chat/completions   OpenAI Chat Completions  (Tofu openai route, opencode, pi)
    /v1/responses          OpenAI Responses         (codex)
    /v1/messages           Anthropic Messages       (Tofu anthropic route, claude-code)
    GET /v1/models         model list
Control (GET):
    /control/records       every recorded request (JSON list)
    /control/reset         clear records

Scenario: the newest user text containing ``[harness-perf scenario=S rounds=N
session=ID]`` selects the script. Round k = assistant tool calls already in the
history after that message. Rounds 0..N-1 call one tool; round N answers
``done``. Scenarios:
    chat   -- N is ignored; answer ``done`` at once (time to first request,
              settle latency)
    noop   -- the harness's plan tool, or a one-line file read where none is
              offered (``NOOP_TOOLS``); arguments vary per round so no loop
              breaker sees a repeated call
    shell  -- the harness's shell tool running ``echo r<k>`` (``SHELL_TOOLS``)
    true   -- the harness's shell tool running ``true``; identical every
              round, so run it with rounds=1 (Tofu's loop breaker stops a
              repeated no-op command, by design)
Requests without a marker (titles, summaries, probes) get ``done`` and are
recorded as ``auxiliary``.

Record per request: monotonic arrival/finish (``time.monotonic_ns``, a
host-wide clock comparable across processes), wall arrival, face, session,
round, body bytes, rendered prompt bytes and the common prefix with the same
session's previous request (prompt-cache prefix stability), tool count.

Entry: ``python3 stub_provider.py --port-file F [--records F.jsonl]``.
Dependencies: standard library only (it also serves the reference harnesses).
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import signal
import threading
import time
import uuid
from pathlib import Path

MODEL_ID = 'harness-perf-stub'
MARKER = re.compile(r'\[harness-perf scenario=(\w+) rounds=(\d+) session=([\w-]+)\]')

# First tool name present in the request wins. Arguments are each harness's
# own minimal valid call; ``{k}`` is the round index and ``{cmd}`` the
# scenario's shell command, so consecutive calls differ.
NOOP_TOOLS = (
    ('todo_write', {'todos': [{'content': 'step {k}', 'status': 'pending'}]}),           # Tofu (Tool-Search deferred)
    ('TodoWrite', {'todos': [{'content': 'step {k}', 'status': 'pending',
                              'activeForm': 'measuring'}]}),                              # claude-code
    ('TaskCreate', {'subject': 'step {k}', 'description': 'measure',
                    'activeForm': 'measuring'}),                                          # claude-code >= 2.1 (no TodoWrite)
    ('todowrite', {'todos': [{'content': 'step {k}', 'status': 'pending',
                              'priority': 'low', 'id': '1'}]}),                           # opencode
    ('update_plan', {'plan': [{'step': 'step {k}', 'status': 'pending'}]}),                # codex
    # No plan tool offered: read one distinct one-line file per round
    # (``f<k>.txt``; the driver creates them). Repeating one read-only call
    # trips Tofu's loop breaker (redundant/no-op reads), by design.
    ('read_files', {'path': 'f{k}.txt'}),                                                  # Tofu
    ('read', {'path': 'f{k}.txt'}),                                                        # pi
)
SHELL_TOOLS = (
    ('run_command', {'command': '{cmd}'}),                                                 # Tofu
    ('Bash', {'command': '{cmd}', 'description': 'no-op'}),                                # claude-code
    ('bash', {'command': '{cmd}', 'description': 'no-op'}),                                # opencode, pi
    ('exec_command', {'cmd': '{cmd}'}),                                                    # codex (unified exec)
    ('shell', {'command': ['{cmd}']}),                                                     # codex (classic)
    ('shell_command', {'command': '{cmd}'}),                                               # codex (string shell)
)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict):
            parts.append(str(block.get('text') or block.get('input_text') or ''))
            if isinstance(block.get('content'), (list, str)):
                parts.append(_text_of(block['content']))
    return '\n'.join(parts)


def conversation_items(face: str, body: dict) -> list[tuple[str, str]]:
    """(kind, text) per history item: kind is user, assistant_tool or other."""
    items = []
    if face == 'responses':
        raw = body.get('input')
        if isinstance(raw, str):
            return [('user', raw)]
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            kind = item.get('type') or 'message'
            if kind in ('function_call', 'custom_tool_call', 'local_shell_call'):
                items.append(('assistant_tool', ''))
            elif kind == 'message' and item.get('role') == 'user':
                items.append(('user', _text_of(item.get('content'))))
            else:
                items.append(('other', ''))
        return items
    for message in body.get('messages') or []:
        if not isinstance(message, dict):
            continue
        role, content = message.get('role'), message.get('content')
        if role == 'user':
            items.append(('user', _text_of(content)))
        elif role == 'assistant':
            calls = len(message.get('tool_calls') or [])
            if isinstance(content, list):
                calls += sum(1 for block in content if isinstance(block, dict) and block.get('type') == 'tool_use')
            items.extend([('assistant_tool', '')] * calls or [('other', '')])
        else:
            items.append(('other', ''))
    return items


def scenario_of(face: str, body: dict) -> tuple[str, int, str, int] | None:
    """(scenario, rounds, session, round index) or None for auxiliary calls."""
    items = conversation_items(face, body)
    found = None
    for index, (kind, text) in enumerate(items):
        if kind == 'user':
            match = None
            for match in MARKER.finditer(text):
                pass
            if match:
                found = (match.group(1), int(match.group(2)), match.group(3), index)
    if found is None:
        return None
    scenario, rounds, session, index = found
    done = sum(1 for kind, _ in items[index + 1:] if kind == 'assistant_tool')
    return scenario, rounds, session, done


def tool_names(face: str, body: dict) -> list[str]:
    names = []
    for tool in body.get('tools') or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get('name') or (tool.get('function') or {}).get('name')
        if name:
            names.append(name)
    return names


def plan_reply(face: str, body: dict) -> dict:
    """The deterministic answer: {'tool': (name, args) | None, 'text': str, ...}."""
    scenario = scenario_of(face, body)
    if scenario is None:
        return {'tool': None, 'text': 'done', 'session': '', 'round': -1, 'scenario': 'auxiliary'}
    name, rounds, session, done = scenario
    reply = {'tool': None, 'text': 'done', 'session': session, 'round': done, 'scenario': name}
    offered = tool_names(face, body)
    if done == 0:
        reply['offered'] = offered
    if name in ('noop', 'shell', 'true') and done < rounds:
        command = 'true' if name == 'true' else f'echo r{done}'
        for tool, arguments in (NOOP_TOOLS if name == 'noop' else SHELL_TOOLS):
            if tool in offered:
                text = json.dumps(arguments).replace('{cmd}', command).replace('{k}', str(done))
                reply['tool'] = (tool, json.loads(text))
                break
        else:
            reply['text'] = f'no {name} tool offered'
    return reply


def rendered_prompt(face: str, body: dict) -> str:
    """The cacheable prompt as the provider renders it: system, tools, history."""
    if face == 'responses':
        parts = [body.get('instructions'), body.get('tools'), body.get('input')]
    elif face == 'anthropic':
        parts = [body.get('system'), body.get('tools'), body.get('messages')]
    else:
        parts = [body.get('tools'), body.get('messages')]
    return '\x1e'.join(json.dumps(part, ensure_ascii=False, separators=(',', ':')) for part in parts)


def _common_prefix(a: str, b: str) -> int:
    low, high = 0, min(len(a), len(b))
    while low < high:  # binary search on slice equality: C-speed compares
        mid = (low + high + 1) // 2
        if a[:mid] == b[:mid]:
            low = mid
        else:
            high = mid - 1
    return low


class Recorder:
    def __init__(self, path: str | None):
        self.lock = threading.Lock()
        self.records: list[dict] = []
        self.previous: dict[str, str] = {}
        self.path = Path(path) if path else None

    def add(self, record: dict, prompt: str):
        with self.lock:
            session = record['session']
            if session:
                previous = self.previous.get(session)
                if previous is not None:
                    record['prefix_chars'] = _common_prefix(previous, prompt)
                    record['previous_prompt_chars'] = len(previous)
                self.previous[session] = prompt
            self.records.append(record)
            if self.path:
                with self.path.open('a') as handle:
                    handle.write(json.dumps(record) + '\n')

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.records)

    def reset(self):
        with self.lock:
            self.records.clear()
            self.previous.clear()


def _sse(payload: dict, event: str | None = None) -> bytes:
    head = f'event: {event}\n' if event else ''
    return f'{head}data: {json.dumps(payload, separators=(",", ":"))}\n\n'.encode()


def _usage_tokens(prompt: str) -> int:
    return max(1, len(prompt) // 4)


def make_handler(recorder: Recorder):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        # Provider front ends send with TCP_NODELAY. With Nagle on, the body
        # write waits for the client's delayed ACK of the header segment (up
        # to 40 ms on a reused connection) after ``finished_ns`` is recorded,
        # and that stall would be charged to the harness as round overhead.
        disable_nagle_algorithm = True

        def log_message(self, *args):
            pass

        def _send(self, status: int, payload, content_type='application/json'):
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith('/control/records'):
                return self._send(200, recorder.snapshot())
            if self.path.startswith('/control/reset'):
                recorder.reset()
                return self._send(200, {'ok': True})
            return self._send(200, {'object': 'list', 'data': [
                {'id': MODEL_ID, 'object': 'model', 'type': 'model', 'owned_by': 'harness-perf',
                 'display_name': MODEL_ID, 'created_at': '2026-11-17T00:00:00Z'}]})

        def do_HEAD(self):
            self._send(200, b'')

        def do_POST(self):
            arrived_ns, arrived_wall = time.monotonic_ns(), time.time()
            raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
            path = self.path.split('?', 1)[0].rstrip('/')
            if path.endswith('/chat/completions'):
                face = 'chat'
            elif path.endswith('/responses'):
                face = 'responses'
            elif path.endswith('/messages'):
                face = 'anthropic'
            elif path.endswith('/count_tokens'):
                return self._send(200, {'input_tokens': max(1, len(raw) // 4)})
            else:
                return self._send(404, {'error': {'type': 'not_found', 'message': path}})
            try:
                body = json.loads(raw or b'{}')
            except ValueError:
                return self._send(400, {'error': {'type': 'invalid_request_error', 'message': 'bad json'}})
            reply = plan_reply(face, body)
            prompt = rendered_prompt(face, body)
            tokens = _usage_tokens(prompt)
            stream = bool(body.get('stream'))
            try:
                getattr(self, f'_{face}')(reply, tokens, stream)
            except (BrokenPipeError, ConnectionResetError):
                return
            recorder.add({
                'face': face, 'session': reply['session'], 'scenario': reply['scenario'],
                'round': reply['round'], 'tool': reply['tool'][0] if reply['tool'] else None,
                'arrived_ns': arrived_ns, 'finished_ns': time.monotonic_ns(), 'arrived_wall': arrived_wall,
                'body_bytes': len(raw), 'prompt_chars': len(prompt), 'tools': len(body.get('tools') or []),
                'stream': stream, 'offered_tools': reply.get('offered'), 'user_agent': self.headers.get('User-Agent', '')[:80],
            }, prompt if reply['session'] else '')

        # ── Anthropic Messages ──────────────────────────────────────────
        def _anthropic(self, reply, tokens, stream):
            message_id = f'msg_{uuid.uuid4().hex[:24]}'
            tool = reply['tool']
            content = [{'type': 'text', 'text': 'ok' if tool else reply['text']}]
            if tool:
                content.append({'type': 'tool_use', 'id': f'toolu_{uuid.uuid4().hex[:24]}',
                                'name': tool[0], 'input': tool[1]})
            stop = 'tool_use' if tool else 'end_turn'
            usage = {'input_tokens': tokens, 'output_tokens': 8,
                     'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}
            if not stream:
                return self._send(200, {'id': message_id, 'type': 'message', 'role': 'assistant', 'model': MODEL_ID,
                                        'content': content, 'stop_reason': stop, 'stop_sequence': None,
                                        'usage': usage})
            out = [_sse({'type': 'message_start', 'message': {
                'id': message_id, 'type': 'message', 'role': 'assistant', 'model': MODEL_ID, 'content': [],
                'stop_reason': None, 'stop_sequence': None, 'usage': {**usage, 'output_tokens': 1}}},
                'message_start')]
            for index, block in enumerate(content):
                if block['type'] == 'text':
                    out.append(_sse({'type': 'content_block_start', 'index': index,
                                     'content_block': {'type': 'text', 'text': ''}}, 'content_block_start'))
                    out.append(_sse({'type': 'content_block_delta', 'index': index,
                                     'delta': {'type': 'text_delta', 'text': block['text']}}, 'content_block_delta'))
                else:
                    out.append(_sse({'type': 'content_block_start', 'index': index, 'content_block': {
                        'type': 'tool_use', 'id': block['id'], 'name': block['name'], 'input': {}}},
                        'content_block_start'))
                    out.append(_sse({'type': 'content_block_delta', 'index': index, 'delta': {
                        'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])}},
                        'content_block_delta'))
                out.append(_sse({'type': 'content_block_stop', 'index': index}, 'content_block_stop'))
            out.append(_sse({'type': 'message_delta', 'delta': {'stop_reason': stop, 'stop_sequence': None},
                             'usage': {'output_tokens': 8}}, 'message_delta'))
            out.append(_sse({'type': 'message_stop'}, 'message_stop'))
            self._send(200, b''.join(out), 'text/event-stream')

        # ── OpenAI Chat Completions ─────────────────────────────────────
        def _chat(self, reply, tokens, stream):
            completion_id = f'chatcmpl-{uuid.uuid4().hex[:24]}'
            created = int(time.time())
            tool = reply['tool']
            usage = {'prompt_tokens': tokens, 'completion_tokens': 8, 'total_tokens': tokens + 8}
            call = None
            if tool:
                call = {'id': f'call_{uuid.uuid4().hex[:24]}', 'type': 'function',
                        'function': {'name': tool[0], 'arguments': json.dumps(tool[1])}}
            finish = 'tool_calls' if tool else 'stop'
            text = 'ok' if tool else reply['text']
            if not stream:
                message = {'role': 'assistant', 'content': text}
                if call:
                    message['tool_calls'] = [call]
                return self._send(200, {'id': completion_id, 'object': 'chat.completion', 'created': created,
                                        'model': MODEL_ID, 'usage': usage, 'choices': [
                                            {'index': 0, 'message': message, 'finish_reason': finish}]})

            def chunk(delta, finish_reason=None, **extra):
                return _sse({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created,
                             'model': MODEL_ID, 'choices': [{'index': 0, 'delta': delta,
                                                             'finish_reason': finish_reason}], **extra})
            out = [chunk({'role': 'assistant', 'content': text})]
            if call:
                out.append(chunk({'tool_calls': [{'index': 0, **call}]}))
            out.append(chunk({}, finish))
            out.append(_sse({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created,
                             'model': MODEL_ID, 'choices': [], 'usage': usage}))
            out.append(b'data: [DONE]\n\n')
            self._send(200, b''.join(out), 'text/event-stream')

        # ── OpenAI Responses ────────────────────────────────────────────
        def _responses(self, reply, tokens, stream):
            response_id = f'resp_{uuid.uuid4().hex[:24]}'
            tool = reply['tool']
            output = [{'type': 'message', 'id': f'msg_{uuid.uuid4().hex[:24]}', 'status': 'completed',
                       'role': 'assistant', 'content': [{'type': 'output_text', 'annotations': [],
                                                         'text': 'ok' if tool else reply['text']}]}]
            if tool:
                output.append({'type': 'function_call', 'id': f'fc_{uuid.uuid4().hex[:24]}', 'status': 'completed',
                               'call_id': f'call_{uuid.uuid4().hex[:24]}', 'name': tool[0],
                               'arguments': json.dumps(tool[1])})
            usage = {'input_tokens': tokens, 'input_tokens_details': {'cached_tokens': 0}, 'output_tokens': 8,
                     'output_tokens_details': {'reasoning_tokens': 0}, 'total_tokens': tokens + 8}
            response = {'id': response_id, 'object': 'response', 'created_at': int(time.time()),
                        'status': 'completed', 'model': MODEL_ID, 'output': output, 'usage': usage}
            if not stream:
                return self._send(200, response)
            out = [_sse({'type': 'response.created', 'response': {**response, 'status': 'in_progress',
                                                                  'output': []}}, 'response.created')]
            for index, item in enumerate(output):
                out.append(_sse({'type': 'response.output_item.added', 'output_index': index, 'item': item},
                                'response.output_item.added'))
                if item['type'] == 'message':
                    out.append(_sse({'type': 'response.output_text.delta', 'output_index': index,
                                     'item_id': item['id'], 'content_index': 0,
                                     'delta': item['content'][0]['text']}, 'response.output_text.delta'))
                out.append(_sse({'type': 'response.output_item.done', 'output_index': index, 'item': item},
                                'response.output_item.done'))
            out.append(_sse({'type': 'response.completed', 'response': response}, 'response.completed'))
            self._send(200, b''.join(out), 'text/event-stream')
    return Handler


class StubProvider:
    """In-process server (the suite's driver owns one)."""

    def __init__(self, records_path: str | None = None):
        self.recorder = Recorder(records_path)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.recorder))
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, name='harness-perf-stub', daemon=True)

    def start(self) -> 'StubProvider':
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def base_url(self) -> str:
        return f'http://127.0.0.1:{self.port}'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--port-file', required=True)
    parser.add_argument('--records')
    args = parser.parse_args(argv)
    stub = StubProvider(args.records)
    Path(args.port_file).write_text(str(stub.port))
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=stub.server.shutdown, daemon=True).start())
    print(f'stub provider pid={os.getpid()} port={stub.port}', flush=True)
    stub.server.serve_forever()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
