"""Python reference implementation of every parity boundary.

``python3 -m tests.rust_parity.python_impl`` speaks the harness line protocol
(``protocol.py``) on stdin/stdout. Each boundary calls the production owner
that produces those bytes today, never a re-implementation:

  provider_wire    lib.llm.body.build_body + lib.llm._sse_core.prepare_request
  sse_frame        routes.conversation_sync_v3._multiplexed_sse_frame
  sync_snapshot    lib.api_response.api_prevalidated_payload
  push_frame       tofu_workers.json_frames._json_text (data lane) and Quart's
                   DefaultJSONProvider (control lane, ``websocket.send_json``)
  storage_request  tofu_db.wire.StorageV2Session.request on a recording socket

When an owner moves to Rust, its boundary here keeps calling the Python code
until the phase deletes it; the Rust side must then answer the same cases.
"""
from __future__ import annotations

import base64
import json
import sys


def _part(name: str, data: bytes | str) -> dict:
    if isinstance(data, str):
        data = data.encode('utf-8')
    return {'name': name, 'b64': base64.b64encode(data).decode('ascii')}


def provider_wire(case_input: dict) -> list[dict]:
    from lib.llm._sse_core import prepare_request
    from lib.llm.body import build_body
    from lib.llm.request import WIRE_MESSAGE_ROWS_KEY
    from lib.llm_sanitize.context_sections import next_text_memo_round, text_memo_scope
    parts = []
    for protocol in case_input['protocols']:
        with text_memo_scope():
            next_text_memo_round()
            body = build_body(case_input['model'], json.loads(json.dumps(case_input['messages'])),
                              max_tokens=case_input['max_tokens'], temperature=case_input['temperature'],
                              tools=json.loads(json.dumps(case_input['tools'])) if case_input['tools'] else None,
                              provider_id='rust-parity', precomputed_input_tokens=100,
                              context_window=200000)
            body[WIRE_MESSAGE_ROWS_KEY] = {}
            plan = prepare_request(body, api_key=case_input['api_key'], base_url=case_input['base_url'],
                                   api_protocol=protocol)
        parts.append(_part(f'{protocol}:url', plan.url))
        for key, value in plan.hdrs.items():
            parts.append(_part(f'{protocol}:header:{key}', str(value)))
        parts.append(_part(f'{protocol}:payload', bytes(plan.body.payload)))
    return parts


def _frame_fields(frame: str) -> tuple[str | None, list[str]]:
    cursor, data = None, []
    for line in frame.split('\n'):
        if line.startswith('id: '):
            cursor = line[len('id: '):]
        elif line.startswith('data: '):
            data.append(line[len('data: '):])
    return cursor, data


def sse_frame(case_input: dict) -> list[dict]:
    from routes.conversation_sync_v3 import _multiplexed_sse_frame
    cursor, data = _frame_fields(case_input['frame'])
    return [_part('frame', _multiplexed_sse_frame(int(data[0]), json.loads(data[1]), cursor=cursor))]


def sync_snapshot(case_input: dict) -> list[dict]:
    from lib.api_response import api_prevalidated_payload
    response, status = api_prevalidated_payload(json.loads(case_input['body']))
    return [_part('status', str(status)), _part('body', bytes(response.response.data))]


_CONTROL_PROVIDER = None


def push_frame(case_input: dict) -> list[dict]:
    global _CONTROL_PROVIDER
    frame = json.loads(case_input['text'])
    if frame.get('channel') == 'system':
        # routes/push.py sends control-lane dicts with websocket.send_json.
        if _CONTROL_PROVIDER is None:
            from quart import Quart
            from quart.json.provider import DefaultJSONProvider
            _CONTROL_PROVIDER = DefaultJSONProvider(Quart('tofu-rust-parity'))
        return [_part('text', _CONTROL_PROVIDER.dumps(frame))]
    from tofu_workers.json_frames import _json_text
    return [_part('text', _json_text(frame))]


class _RecordingSocket:
    def __init__(self):
        self.sent = bytearray()

    def sendall(self, data) -> None:
        self.sent += data


def storage_request(case_input: dict) -> list[dict]:
    from tofu_db.wire import StorageV2Session
    session = object.__new__(StorageV2Session)
    socket = _RecordingSocket()
    correlation = bytes.fromhex(case_input['correlation_id'])
    session._sock = socket
    session._owner_id = case_input['owner_id']
    session._tenant_id = case_input['tenant_id']
    session.schema_id = case_input['schema_id']
    session._next_correlation = lambda: correlation
    session._receive_response = lambda *args, **kwargs: None
    session.request(case_input['operation'], case_input['payload'],
                    command_id=case_input['command_id'], deadline_unix_ms=case_input['deadline_unix_ms'])
    return [_part('frames', bytes(socket.sent))]


def engine_session(case_input: dict) -> list[dict]:
    from tests.rust_parity_engine.reference import engine_session as run_engine_session
    return run_engine_session(case_input)


BOUNDARIES = {
    'engine_session': engine_session,
    'provider_wire': provider_wire,
    'sse_frame': sse_frame,
    'sync_snapshot': sync_snapshot,
    'push_frame': push_frame,
    'storage_request': storage_request,
}


def main() -> int:
    out = sys.stdout
    # Owners that print or log to stdout must not corrupt the protocol stream.
    sys.stdout = sys.stderr
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        answer = {'id': request['id']}
        handler = BOUNDARIES.get(request['boundary'])
        if handler is None:
            answer['unsupported'] = f"no Python owner for boundary {request['boundary']!r}"
        else:
            try:
                answer['parts'] = handler(request['input'])
            except Exception as error:  # reported per case; the run continues
                answer['error'] = f'{type(error).__name__}: {error}'
        out.write(json.dumps(answer) + '\n')
        out.flush()
    return 0


if __name__ == '__main__':
    sys.exit(main())
