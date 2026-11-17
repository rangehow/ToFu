"""A minimal stdio MCP server whose failures the chaos suite controls.

Newline-delimited JSON-RPC 2.0 (the MCP stdio transport), standard library
only. Two tools:

- ``chaos_record(note)`` appends ``note`` to ``<state>/calls.log`` and answers;
  its input schema names its argument ``note`` while ``<state>/schema`` is
  ``v1`` and ``message`` once it is ``v2`` (a server upgraded across a
  restart), so a client that kept the old declaration sends a stale argument.
- ``chaos_crash()`` records the call, then exits the process mid-call without
  answering, as a crashing server does.

Usage: ``python3 mcp_server.py <state dir>``; the state dir also receives the
server's PID per launch in ``launches.log``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

STATE = Path(sys.argv[1])


def schema_version() -> str:
    try:
        return (STATE / 'schema').read_text().strip() or 'v1'
    except OSError:
        return 'v1'


def tools() -> list[dict]:
    argument = 'note' if schema_version() == 'v1' else 'message'
    return [
        {'name': 'chaos_record', 'description': 'Record one note in the chaos call log.',
         'inputSchema': {'type': 'object', 'properties': {argument: {'type': 'string'}},
                         'required': [argument], 'additionalProperties': False}},
        {'name': 'chaos_crash', 'description': 'Crash the chaos MCP server mid-call.',
         'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    ]


def answer(message_id, result=None, error=None) -> None:
    payload = {'jsonrpc': '2.0', 'id': message_id}
    payload.update({'error': error} if error else {'result': result})
    sys.stdout.write(json.dumps(payload) + '\n')
    sys.stdout.flush()


def record(line: str) -> None:
    with open(STATE / 'calls.log', 'a') as handle:
        handle.write(line + '\n')


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / 'launches.log', 'a') as handle:
        handle.write(f'{os.getpid()} {schema_version()}\n')
    for raw in sys.stdin:
        if not raw.strip():
            continue
        message = json.loads(raw)
        method, message_id = message.get('method'), message.get('id')
        if message_id is None:
            continue  # notifications (initialized, cancelled)
        if method == 'initialize':
            answer(message_id, {'protocolVersion': message['params'].get('protocolVersion', '2025-06-18'),
                                'capabilities': {'tools': {'listChanged': False}},
                                'serverInfo': {'name': 'chaos', 'version': schema_version()}})
        elif method == 'tools/list':
            answer(message_id, {'tools': tools()})
        elif method == 'tools/call':
            name = message['params'].get('name')
            arguments = message['params'].get('arguments') or {}
            if name == 'chaos_crash':
                record('crash')
                os._exit(17)
            if name == 'chaos_record':
                argument = 'note' if schema_version() == 'v1' else 'message'
                if set(arguments) != {argument}:
                    answer(message_id, {'isError': True, 'content': [{'type': 'text', 'text':
                           f'invalid arguments {sorted(arguments)}; expected [{argument!r}]'}]})
                    continue
                record(f'record {arguments[argument]}')
                answer(message_id, {'content': [{'type': 'text', 'text': f'recorded {arguments[argument]}'}]})
            else:
                answer(message_id, error={'code': -32601, 'message': f'unknown tool {name}'})
        elif method == 'ping':
            answer(message_id, {})
        else:
            answer(message_id, error={'code': -32601, 'message': f'unknown method {method}'})
    return 0


if __name__ == '__main__':
    sys.exit(main())
