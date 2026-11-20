"""Wire-level failure injection: Rust vs Python SQL authority (P3b).

Raw ``storage.v1`` frames, including malformed, unauthenticated, expired,
oversized and torn ones, are sent to both authorities (isolated SQLite data
dirs under ``tmp_path``); each response must be byte-for-byte the same JSON
value, and both authorities must keep serving afterwards. Lost-ACK replay
and SIGKILL durability compare a committed command across a hard crash.
"""

from __future__ import annotations

from contextlib import closing
import json
import os
import signal
import socket
import sqlite3
import struct
import time

import pytest

from tests.storage_authority_sql.test_rust_sql_authority_differential import (
    Authority,
    PYTHON_AUTHORITY_COMMAND,
    pair_identity as pair_identity,
    rust_authority_binary,
    unported_operations,
)

pytestmark = [pytest.mark.unit, pytest.mark.serial]


@pytest.fixture()
def pair(tmp_path, pair_identity):
    binary = rust_authority_binary()
    python = Authority(tmp_path / 'python', PYTHON_AUTHORITY_COMMAND)
    try:
        rust = Authority(tmp_path / 'rust', (str(binary), 'serve'))
    except BaseException:
        python.stop()
        raise
    try:
        pair_identity((python, rust))
        yield python, rust
        pair_identity((python, rust), phase='finished')
    finally:
        rust.stop()
        python.stop()


def _endpoint(authority: Authority) -> tuple[int, str]:
    return authority.supervisor._endpoint_port, authority.supervisor._token


def _exchange(port: int, frame: bytes, *, half_close=True) -> dict | None:
    with socket.create_connection(('127.0.0.1', port), timeout=15) as connection:
        connection.sendall(frame)
        if half_close:
            connection.shutdown(socket.SHUT_WR)
        header = b''
        while len(header) < 4:
            chunk = connection.recv(4 - len(header))
            if not chunk:
                return None
            header += chunk
        (size,) = struct.unpack('!I', header)
        body = b''
        while len(body) < size:
            chunk = connection.recv(size - len(body))
            if not chunk:
                return None
            body += chunk
        return json.loads(body)


def _frame(message: dict | bytes) -> bytes:
    body = message if isinstance(message, bytes) else json.dumps(message).encode()
    return struct.pack('!I', len(body)) + body


def _request(token: str, **fields) -> dict:
    request = {
        'protocol': 'storage.v1', 'request_id': 'wire-case', 'auth': token,
        'deadline_unix_ms': int(time.time() * 1000) + 15_000,
        'kind': 'query', 'operation': 'system.schema_version', 'payload': {},
    }
    for key, value in fields.items():
        if value is _ABSENT:
            request.pop(key, None)
        else:
            request[key] = value
    return request


_ABSENT = object()

CASES = {
    'ok query': {},
    'protocol mismatch': {'protocol': 'storage.v0'},
    'bad auth': {'auth': 'x' * 64},
    'missing auth': {'auth': _ABSENT},
    'missing deadline': {'deadline_unix_ms': _ABSENT},
    'float deadline': {'deadline_unix_ms': 1.5e13},
    'expired deadline': {'deadline_unix_ms': 1},
    'missing request id': {'request_id': _ABSENT},
    'long request id': {'request_id': 'r' * 65},
    'invalid operation name': {'operation': 'System.Bad'},
    'unknown operation': {'operation': 'nothing.here'},
    'kind mismatch': {'kind': 'command', 'command_id': 'c1'},
    'invalid kind': {'kind': 'mystery'},
    'payload not object': {'payload': [1, 2]},
    'payload missing': {'payload': _ABSENT},
    'invalid priority': {'operation': 'system.reclaim', 'kind': 'command', 'priority': 'urgent'},
    'reclaim command': {'operation': 'system.reclaim', 'kind': 'command', 'payload': {'min_free_pages': 0}},
    'reclaim invalid integer': {'operation': 'system.reclaim', 'kind': 'command',
                                'payload': {'max_pages': True}},
    'watch invalid payload': {'operation': 'worker_control.watch', 'payload': {'cursor': 1, 'x': 2}},
    'watch with receipt': {'operation': 'worker_control.watch', 'command_id': 'c'},
    'watch invalid cursor': {'operation': 'conversation_control.watch', 'payload': {'cursor': {'epoch': 1}}},
    'native checkpoint receipt': {'operation': 'native_checkpoint.capabilities', 'command_id': 'c'},
    'native checkpoint bad admission': {'operation': 'native_checkpoint.capabilities', 'payload': {}},
    'native checkpoint control': {'operation': 'native_checkpoint.control', 'kind': 'command'},
    'integrity check': {'operation': 'system.integrity_check', 'kind': 'maintenance'},
    'maintenance unknown': {'operation': 'nothing.here', 'kind': 'maintenance'},
}


def _comparable(response: dict | None) -> dict | None:
    if response is None:
        return None
    value = dict(response)
    result = value.get('result')
    if isinstance(result, dict) and 'runtime_id' in result:
        value['result'] = {**result, 'runtime_id': '<runtime>'}
    return value


@pytest.mark.parametrize('case', sorted(CASES), ids=lambda name: name)
def test_frame_cases_answer_identically(pair, case):
    responses = []
    for authority in pair:
        port, token = _endpoint(authority)
        responses.append(_comparable(_exchange(port, _frame(_request(token, **CASES[case])))))
    assert responses[0] == responses[1], json.dumps(responses, indent=1)


def _operation_reply(authority: Authority, request_id: str, operation: str, payload: dict) -> dict:
    port, token = _endpoint(authority)
    reply = _exchange(port, _frame(_request(
        token, request_id=request_id, operation=operation, payload=payload)))
    assert reply is not None, (authority.root.name, request_id)
    return reply


def _success_reply(request_id: str, result: dict) -> dict:
    return {'protocol': 'storage.v1', 'request_id': request_id, 'ok': True, 'result': result}


def _failure_reply(request_id: str, code: str, message: str) -> dict:
    return {
        'protocol': 'storage.v1', 'request_id': request_id, 'ok': False,
        'error': {'code': code, 'message': message, 'operation_id': request_id,
                  'retryable': False, 'retry_after_ms': None},
    }


def _durable_snapshot(authority: Authority) -> tuple[str, ...]:
    # Compare exact logical SQLite state within each authority, without masking
    # generated IDs, clocks, receipt fields, or BLOBs as a cross-authority diff would.
    with closing(sqlite3.connect(f'{authority.database.as_uri()}?mode=ro', uri=True)) as connection:
        connection.execute('BEGIN')
        return tuple(connection.iterdump())


def _wait_for_seed_projection(authority: Authority) -> None:
    # Seed commands enqueue asynchronous search work. Let it finish before
    # snapshotting so that cleanup of their outbox rows cannot look like a query write.
    deadline = time.monotonic() + 15
    with closing(sqlite3.connect(f'{authority.database.as_uri()}?mode=ro', uri=True)) as connection:
        while connection.execute('SELECT 1 FROM storage_projection_outbox LIMIT 1').fetchone():
            assert time.monotonic() < deadline, (authority.root.name, 'seed projection did not drain')
            time.sleep(0.01)


def test_tofusql_query_refuses_native_only_operation(pair, record_property):
    record_property('sql_authority_wire_operation', 'tofusql.query')
    request_id = 'tofusql-native-only'
    expected = _failure_reply(request_id, 'database_unavailable',
                              'tofusql.query is served only by the Tofu-DB engine')
    for authority in pair:
        before = _durable_snapshot(authority)
        reply = _operation_reply(authority, request_id, 'tofusql.query',
                                 {'query': 'SELECT count(*) AS n FROM storage_records'})
        assert reply == expected, authority.root.name
        assert _durable_snapshot(authority) == before, authority.root.name


def _seed_resume_sources(authority: Authority) -> dict[str, dict]:
    client = authority.supervisor.client
    scope = {'conversation_id': 'wire-resume-plan', 'user_id': 7}
    client.command('conversation.create', {
        'conv_id': scope['conversation_id'], 'user_id': scope['user_id'],
        'title': 'Wire resume plan', 'settings': {},
    }, 'resume-create')
    sources = {}
    for status in ('failed', 'interrupted', 'truncated', 'completed'):
        turn_id = f'resume-{status}'
        settled = client.command('turn.append_settled', {
            **scope, 'turn_id': turn_id, 'command_id': turn_id, 'actor': 'assistant',
            'status': status, 'projection': {'content': 'private projection sentinel'},
            'config': {'privateSentinel': 'accepted configuration'},
        }, turn_id)
        sources[status] = {
            **scope, 'turn_id': turn_id, 'attempt_id': settled['attempt']['attemptId'],
            'projection_revision': settled['turn']['projectionRevision'],
        }
    return sources


def _resume_plan_wire_cases(sources: dict[str, dict]):
    for status in ('failed', 'interrupted', 'truncated'):
        request_id = f'pin-{status}'
        source = sources[status]
        yield request_id, source, _success_reply(request_id, {
            'status': 'source_pinned', 'sourceStatus': status,
            'conversationId': source['conversation_id'], 'turnId': source['turn_id'],
            'attemptId': source['attempt_id'], 'projectionRevision': source['projection_revision'],
        })

    failed = sources['failed']
    stale = {'status': 'stale_source'}
    unavailable = {'status': 'source_unavailable'}
    for request_id, payload, expected in (
        ('stale-revision', {**failed, 'projection_revision': failed['projection_revision'] + 1}, stale),
        ('stale-attempt', {**failed, 'attempt_id': sources['truncated']['attempt_id']}, stale),
        ('foreign-owner', {**failed, 'user_id': failed['user_id'] + 1}, unavailable),
        ('missing-conversation', {**failed, 'conversation_id': 'missing-conversation'}, unavailable),
        ('missing-turn', {**failed, 'turn_id': 'missing-turn'}, unavailable),
        ('not-resumable', sources['completed'], unavailable),
    ):
        yield request_id, payload, _success_reply(request_id, expected)

    for request_id, field, value in (
        ('invalid-revision-negative', 'projection_revision', -1),
        ('invalid-revision-bool', 'projection_revision', True),
        ('invalid-revision-overflow', 'projection_revision', 2**63),
        ('invalid-attempt-empty', 'attempt_id', ''),
    ):
        yield request_id, {**failed, field: value}, _failure_reply(
            request_id, 'database_protocol_error', f'Invalid {field} in storage request')


def test_turn_resume_plan_read_wire_contract(pair, record_property):
    record_property('sql_authority_wire_operation', 'turn.resume.plan.read')
    for authority in pair:
        sources = _seed_resume_sources(authority)
        _wait_for_seed_projection(authority)
        before = _durable_snapshot(authority)
        for request_id, payload, expected in _resume_plan_wire_cases(sources):
            reply = _operation_reply(authority, request_id, 'turn.resume.plan.read', payload)
            assert reply == expected, (authority.root.name, request_id)
            assert _durable_snapshot(authority) == before, (authority.root.name, request_id)


RAW_FRAMES = {
    'malformed json': _frame(b'{"protocol": '),
    'non-object frame': _frame(b'[1,2,3]'),
    'zero length': struct.pack('!I', 0),
    'oversized length': struct.pack('!I', 64 * 1024 * 1024 + 1),
}


@pytest.mark.parametrize('case', sorted(RAW_FRAMES), ids=lambda name: name)
def test_malformed_frames_answer_identically_and_keep_serving(pair, case):
    responses = []
    for authority in pair:
        port, token = _endpoint(authority)
        responses.append(_exchange(port, RAW_FRAMES[case]))
        after = _exchange(port, _frame(_request(token)))
        assert after is not None and after['ok'] is True, after
    assert responses[0] == responses[1], json.dumps(responses, indent=1)


def test_torn_frame_disconnect_never_dispatches_and_authority_survives(pair):
    for authority in pair:
        port, token = _endpoint(authority)
        body = json.dumps(_request(token)).encode()
        with socket.create_connection(('127.0.0.1', port), timeout=5) as connection:
            connection.sendall(struct.pack('!I', len(body)) + body[: len(body) // 2])
        after = _exchange(port, _frame(_request(token)))
        assert after is not None and after['ok'] is True


def test_keep_alive_serves_consecutive_frames_identically(pair):
    transcripts = []
    for authority in pair:
        port, token = _endpoint(authority)
        replies = []
        with socket.create_connection(('127.0.0.1', port), timeout=15) as connection:
            for index in range(3):
                connection.sendall(_frame(_request(token, request_id=f'k{index}', keep_alive=True)))
                header = connection.recv(4, socket.MSG_WAITALL)
                (size,) = struct.unpack('!I', header)
                replies.append(json.loads(connection.recv(size, socket.MSG_WAITALL)))
        transcripts.append(replies)
    assert transcripts[0] == transcripts[1]
    assert all(reply['keep_alive']['idle_timeout_ms'] == 5000 for reply in transcripts[1])


def test_unauthenticated_keep_alive_is_not_honored(pair):
    for authority in pair:
        port, _token = _endpoint(authority)
        reply = _exchange(port, _frame(_request('bad-token' * 8, keep_alive=True)))
        assert reply['ok'] is False and 'keep_alive' not in reply


def test_health_and_watch_shapes(pair):
    shapes = []
    for authority in pair:
        port, token = _endpoint(authority)
        health = _exchange(port, _frame(_request(token, kind='health', operation='system.health')))
        assert health['ok'] and health['result']['ready'] is True
        watch = _exchange(port, _frame(_request(
            token, operation='worker_control.watch', payload={'cursor': None},
            deadline_unix_ms=int(time.time() * 1000) + 300)))
        assert watch['ok'], watch
        shapes.append((sorted(health['result']), sorted(watch['result']), sorted(watch['result']['cursor'])))
    assert shapes[0][1:] == shapes[1][1:]
    assert {'ready', 'backend', 'protocol', 'preflight'} <= set(shapes[1][0])


def _require(operations: set[str]):
    missing = sorted(operations & unported_operations())
    assert not missing, 'not yet ported: ' + ', '.join(missing)


def test_receipt_replay_and_reuse_conflict_match(pair):
    _require({'record.put', 'record.get'})
    answers = []
    for authority in pair:
        port, token = _endpoint(authority)
        put = dict(kind='command', operation='record.put', command_id='same-command',
                   payload={'namespace': 'wire', 'key': 'k', 'value': {'n': 1}})
        first = _exchange(port, _frame(_request(token, **put)))
        replay = _exchange(port, _frame(_request(token, **put)))
        reused = _exchange(port, _frame(_request(token, **{**put, 'payload': {**put['payload'], 'value': 2}})))
        missing_id = _exchange(port, _frame(_request(token, **{**put, 'command_id': _ABSENT})))
        assert first['result'] == replay['result']
        answers.append([
            {**first['result'], 'updated_at_ms': 0}, reused['error'], missing_id['error']])
    assert answers[0] == answers[1]


def test_committed_command_survives_sigkill_on_both(pair, pair_identity):
    _require({'record.put', 'record.get'})
    observed = []
    for authority in pair:
        port, token = _endpoint(authority)
        put = dict(kind='command', operation='record.put', command_id='durable',
                   payload={'namespace': 'wire', 'key': 'durable', 'value': {'kept': True}})
        acked = _exchange(port, _frame(_request(token, **put)))
        assert acked['ok'], acked
        role = authority.root.name
        pair_identity(pair, phase='before_crash_' + role)
        process = authority.supervisor._process
        os.kill(process.pid, signal.SIGKILL)
        assert process.wait(timeout=30) == -signal.SIGKILL
        authority.supervisor.stop()
        authority.supervisor.start()
        pair_identity(pair, phase='restarted_' + role, restarted_role=role)
        port, token = _endpoint(authority)
        replay = _exchange(port, _frame(_request(token, **put)))
        read = _exchange(port, _frame(_request(
            token, operation='record.get', payload={'namespace': 'wire', 'key': 'durable'})))
        pair_identity(pair, phase='replayed_' + role)
        assert replay['result'] == acked['result']
        observed.append((read['result']['value'], read['result']['version']))
    assert observed[0] == observed[1] == ({'kept': True}, 1)
