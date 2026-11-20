"""Replay a recorded MCP session over stdio (JSON-RPC 2.0, one object per line).

Responsibility: a deterministic MCP server for parity tests of the Python
bridge (lib/mcp/client) and the Rust client (packages/tofu-mcp). It answers
``initialize`` by echoing the client's protocol version, ``tools/list`` with
the recorded catalog, and ``tools/call`` from the recorded per-tool script:

- ``{"result": {...}}``: the recorded CallToolResult, returned verbatim;
- ``{"error": {"code", "message"}}``: a JSON-RPC error response;
- ``{"echo": true}``: the received arguments and ``_meta`` as structured content;
- ``{"hang": true}``: never answer (deadline and cancellation cases);
- ``{"exit": true}``: exit without answering (dead transport);
- ``{"exit_once": true}``: exit the first time across restarts (the STATE file
  records it), then answer ``{"after": "restart"}``-style ``result``;
- ``{"text_of_length": N}``: one text block of N digits (display bounds);
- ``{"by": ARG, "cases": {VALUE: script}}``: the recorded script named by the
  call's ``ARG`` argument (one tool, several recorded result shapes);
- ``{"reflect": ARG}``: the CallToolResult carried in the ``ARG`` argument,
  returned verbatim (a generated family of result shapes);
- ``{"raw_result": TEXT}``: TEXT sent byte for byte as the response's
  ``result`` (JSON a strict parser rejects: NaN, lone surrogates);
- ``{"list_changed": KEY, "result": {...}}``: replace the catalog with the
  session's ``KEY`` list, send ``notifications/tools/list_changed``, answer;
- ``{"sleep": SECONDS, "result": {...}}``: answer after SECONDS.

A session's ``"listChanged": true`` declares ``tools.listChanged``. With a
STATE file, every start appends its pid to ``STATE.pids`` (launch count and
the live child for crash cases).

A ``calls`` value that is a list plays its entries in order, then repeats the
last. Every received request is appended to the LOG file when given, so a
test can count what reached the server.
Usage: ``replay_server.py SESSION.json [LOG] [STATE]``.
Standard library only.
"""
from __future__ import annotations

import json
import os
import sys
import time


def _already_exited(name: str) -> bool:
    """Record an ``exit_once`` in the STATE file; True when it already happened."""
    state = sys.argv[3] if len(sys.argv) > 3 else ''
    if not state:
        return False
    seen = set()
    if os.path.exists(state):
        with open(state, encoding='utf-8') as handle:
            seen = set(handle.read().split())
    if name in seen:
        return True
    with open(state, 'a', encoding='utf-8') as handle:
        handle.write(name + '\n')
    return False


def main() -> int:
    with open(sys.argv[1], encoding='utf-8') as handle:
        session = json.load(handle)
    plays: dict[str, int] = {}
    if len(sys.argv) > 3 and sys.argv[3]:
        with open(sys.argv[3] + '.pids', 'a', encoding='utf-8') as pids:
            pids.write(f'{os.getpid()}\n')
    log_path = sys.argv[2] if len(sys.argv) > 2 else ''
    out = sys.stdout

    def send(message):
        out.write(json.dumps(message, ensure_ascii=False) + '\n')
        out.flush()

    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        method = message.get('method')
        if log_path and method:
            with open(log_path, 'a', encoding='utf-8') as log:
                log.write(json.dumps({'method': method, 'params': message.get('params')}) + '\n')
        if 'id' not in message or method is None:
            continue
        ident = message['id']
        params = message.get('params') or {}
        if method == 'initialize':
            send({'jsonrpc': '2.0', 'id': ident, 'result': {
                'protocolVersion': params.get('protocolVersion') or '2025-06-18',
                'capabilities': {'tools': {'listChanged': bool(session.get('listChanged'))}},
                'serverInfo': session.get('serverInfo', {'name': 'tofu-replay', 'version': '1.0.0'}),
            }})
        elif method == 'ping':
            send({'jsonrpc': '2.0', 'id': ident, 'result': {}})
        elif method == 'tools/list':
            result = {'tools': session['tools']}
            if session.get('catalogVersion'):
                result['_meta'] = {'catalogVersion': session['catalogVersion']}
            send({'jsonrpc': '2.0', 'id': ident, 'result': result})
        elif method == 'tools/call':
            name = params.get('name')
            arguments = params.get('arguments') or {}
            script = session.get('calls', {}).get(name)
            if isinstance(script, dict) and 'by' in script:
                chosen = script.get('cases', {}).get(str(arguments.get(script['by'])))
                script = chosen if chosen is not None or 'reflect' not in script else {'reflect': script['reflect']}
            if isinstance(script, list):
                index = plays.get(name, 0)
                plays[name] = index + 1
                script = script[min(index, len(script) - 1)]
            if script is None:
                send({'jsonrpc': '2.0', 'id': ident,
                      'error': {'code': -32602, 'message': f'Unknown tool: {name}'}})
            elif script.get('exit'):
                return 3
            elif script.get('exit_once') and not _already_exited(name):
                return 3
            elif script.get('text_of_length'):
                text = ''.join('0123456789'[i % 10] for i in range(script['text_of_length']))
                send({'jsonrpc': '2.0', 'id': ident, 'result': {
                    'content': [{'type': 'text', 'text': text}]}})
            elif script.get('hang'):
                continue
            elif script.get('list_changed'):
                session['tools'] = session[script['list_changed']]
                send({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'})
                send({'jsonrpc': '2.0', 'id': ident, 'result': script['result']})
            elif script.get('sleep'):
                time.sleep(script['sleep'])
                send({'jsonrpc': '2.0', 'id': ident, 'result': script['result']})
            elif script.get('reflect'):
                send({'jsonrpc': '2.0', 'id': ident, 'result': arguments.get(script['reflect'])})
            elif 'raw_result' in script:
                out.write('{"jsonrpc": "2.0", "id": %s, "result": %s}\n' % (json.dumps(ident), script['raw_result']))
                out.flush()
            elif script.get('echo'):
                received = {'arguments': params.get('arguments'), 'meta': params.get('_meta')}
                send({'jsonrpc': '2.0', 'id': ident, 'result': {
                    'content': [{'type': 'text', 'text': 'echo'}], 'structuredContent': received}})
            elif script.get('exit_once'):
                send({'jsonrpc': '2.0', 'id': ident, 'result': script['result']})
            elif 'error' in script:
                send({'jsonrpc': '2.0', 'id': ident, 'error': script['error']})
            else:
                send({'jsonrpc': '2.0', 'id': ident, 'result': script['result']})
        else:
            send({'jsonrpc': '2.0', 'id': ident,
                  'error': {'code': -32601, 'message': 'Method not found'}})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
