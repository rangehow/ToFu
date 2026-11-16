"""The SQL authority acknowledges an Attempt event only within the event document bound.

The native authority reads each Attempt event document within
``maximumAttemptEventDocumentBytes`` (contracts/tofudb_ir_v1.json), and the
SQL-to-native import admits a stored row within the same bound. Every SQL
writer therefore refuses a larger envelope as ``storage_payload_too_large``
before its row exists: the terminal settlement and the checkpoint bootstrap,
which the 4 MiB non-terminal transport cap exempts, included. An envelope of
exactly the bound is written and reads back unchanged. A checkpoint resume
whose bootstrap would exceed the bound answers its caller 413; regenerate,
which restarts the lane empty, still works.
"""
from __future__ import annotations

import sqlite3

import pytest

from lib.storage.errors import StorageError, http_status_for_storage_error
from lib.storage_authority_sql.adapters.sqlite import SQLiteSession
from lib.storage_authority_sql.migration.generated_tofudb_migration import (
    MAX_ATTEMPT_EVENT_DOCUMENT_BYTES,
)
from lib.storage_authority_sql.operations_pkg._common import _dump
from lib.storage_authority_sql.operations_pkg._turns_events import (
    _insert_attempt_event,
    _turn_event_append,
)
from lib.storage_authority_sql.operations_pkg._turns_read import _turn_events
from lib.storage_authority_sql.operations_pkg._turns_write import (
    _turn_append_settled,
    _turn_attempt_create,
    _turn_create_pair,
)
from lib.storage_authority_sql.schema import initialize_schema

pytestmark = pytest.mark.unit

CONVERSATION = 'conv-event-bound'


@pytest.fixture()
def attempt():
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection, event_read_epoch='event-bound-open')
    initialize_schema(session)
    session.execute(
        'INSERT INTO storage_conversations '
        '(id,user_id,title,messages_json,created_at_ms,updated_at_ms,'
        'settings_json,msg_count,search_text,rev) VALUES (?,?,?,?,?,?,?,?,?,?)',
        (CONVERSATION, 1, 'bound', _dump([]), 1000, 1000, _dump({}), 0, '', 1),
    )
    created = _turn_create_pair(session, {
        'conversation_id': CONVERSATION, 'user_id': 1, 'command_id': 'event-bound-create',
        'input_projection': {'content': 'question'}, 'config': {},
    })
    try:
        yield session, created['attempt']['attemptId'], created['turn']['turnId']
    finally:
        connection.close()


def _rows(session, attempt_id) -> int:
    return session.fetch_one(
        'SELECT COUNT(*) AS n FROM storage_attempt_events WHERE attempt_id=?',
        (attempt_id,))['n']


def _next_sequence(session, attempt_id) -> int:
    return session.fetch_one(
        'SELECT COALESCE(MAX(sequence), 0) AS n FROM storage_attempt_events '
        'WHERE attempt_id=?', (attempt_id,))['n'] + 1


def _envelope(session, attempt_id, turn_id, event_type, document_bytes):
    """The stored envelope of the next event, exactly ``document_bytes`` long."""
    def envelope(padding):
        return {
            'conversationId': CONVERSATION, 'turnId': turn_id, 'attemptId': attempt_id,
            'seq': _next_sequence(session, attempt_id), 'projectionRevision': 1,
            'type': event_type, 'payload': {'padding': padding},
        }
    padded = envelope('x' * (document_bytes - len(_dump(envelope('')))))
    assert len(_dump(padded)) == document_bytes
    return padded


def _append_terminal(session, attempt_id, turn_id, document_bytes):
    envelope = _envelope(session, attempt_id, turn_id, 'terminal_settlement', document_bytes)
    _turn_event_append(session, {
        'attempt_id': attempt_id, 'conversation_id': CONVERSATION, 'turn_id': turn_id,
        'projection_revision': 1, 'type': 'terminal_settlement', 'event': envelope['payload'],
    })
    return envelope


def _insert_bootstrap(session, attempt_id, turn_id, document_bytes):
    envelope = _envelope(session, attempt_id, turn_id, 'status_changed', document_bytes)
    _insert_attempt_event(
        session, attempt_id=attempt_id, sequence=envelope['seq'],
        conversation_id=CONVERSATION, turn_id=turn_id, projection_revision=1,
        event_type='status_changed', envelope=envelope, created_at=2000,
        allow_oversize=True)
    return envelope


@pytest.mark.parametrize('write', [_append_terminal, _insert_bootstrap],
                         ids=['terminal-settlement', 'checkpoint-bootstrap'])
def test_an_attempt_event_past_the_document_bound_is_refused_and_one_at_the_bound_reads_back(
        attempt, write):
    session, attempt_id, turn_id = attempt
    rows = _rows(session, attempt_id)

    with pytest.raises(StorageError) as refused:
        write(session, attempt_id, turn_id, MAX_ATTEMPT_EVENT_DOCUMENT_BYTES + 1)

    assert refused.value.code == 'storage_payload_too_large'
    assert _rows(session, attempt_id) == rows

    written = write(session, attempt_id, turn_id, MAX_ATTEMPT_EVENT_DOCUMENT_BYTES)

    events = _turn_events(session, {
        'attempt_id': attempt_id, 'user_id': 1, 'projection_mode': 'patch'})
    assert len(events) == rows + 1
    assert events[-1] == written
    assert len(_dump(events[-1])) == MAX_ATTEMPT_EVENT_DOCUMENT_BYTES


def _committed(session, operation, payload):
    """One operation in its own transaction, rolled back on failure like the writer's."""
    try:
        result = operation(session, payload)
    except BaseException:
        session.connection.rollback()
        raise
    session.connection.commit()
    return result


def test_a_checkpoint_resume_past_the_document_bound_answers_413_and_regenerate_still_works(attempt):
    # The resume bootstrap moves the interrupted text lane into ``rolledBack``
    # within its one status event; regenerate restarts the lane empty.
    session = attempt[0]
    session.connection.commit()
    settled = _committed(session, _turn_append_settled, {
        'conversation_id': CONVERSATION, 'user_id': 1, 'turn_id': 'interrupted',
        'command_id': 'ingest-interrupted', 'actor': 'assistant', 'lane_id': 'main',
        'projection': {'content': 'x' * MAX_ATTEMPT_EVENT_DOCUMENT_BYTES, 'thinking': '',
                       'segments': [], 'toolRounds': []},
        'settlement': {'outcome': 'truncated', 'resumeOptions': [
            {'operation': 'checkpoint_resume', 'anchor': {
                'content': '', 'thinking': '', 'segments': [], 'keptToolRounds': 0}},
        ]},
    })
    request = {
        'conversation_id': CONVERSATION, 'user_id': 1, 'turn_id': 'interrupted',
        'expected_projection_revision': settled['turn']['projectionRevision'], 'config': {},
    }

    with pytest.raises(StorageError) as refused:
        _committed(session, _turn_attempt_create, {
            **request, 'command_id': 'resume', 'operation': 'checkpoint_resume'})

    assert refused.value.code == 'storage_payload_too_large'
    assert http_status_for_storage_error(refused.value) == 413
    regenerated = _committed(session, _turn_attempt_create, {
        **request, 'command_id': 'regenerate', 'operation': 'regenerate',
        'target_actor': 'assistant', 'target_kind': 'reply'})
    assert regenerated['attempt']['status'] == 'pending'
    assert regenerated['turn']['currentAttemptId'] == regenerated['attempt']['attemptId']
