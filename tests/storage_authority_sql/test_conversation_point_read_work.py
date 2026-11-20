"""Executable work-count receipt for ordinary SQLite conversation reads."""

import sqlite3
from unittest.mock import patch

import pytest

from lib.storage.errors import StorageError
from lib.storage_authority_sql.adapters.sqlite import SQLiteSession
from lib.storage_authority_sql.operations_pkg import _conversations as conversations
from lib.storage_authority_sql import turn_projection_head as projection_head
from lib.storage_authority_sql.schema import initialize_schema
from tests.storage_authority_sql.test_turns_delta_sync import _seed_conv, _seed_turn


pytestmark = pytest.mark.unit


@pytest.fixture
def database(tmp_path):
    connection = sqlite3.connect(tmp_path / 'receipt.sqlite', isolation_level=None)
    connection.row_factory = sqlite3.Row
    assert connection.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    connection.execute('PRAGMA synchronous=FULL')
    session = SQLiteSession(connection)
    initialize_schema(session)
    _seed_conv(session)
    try:
        yield session
    finally:
        connection.close()


def measured_get(session, **payload):
    statements = []
    session.connection.set_trace_callback(statements.append)
    session.connection.execute('BEGIN')
    try:
        with patch.object(projection_head, '_load', wraps=projection_head._load) as decode:
            result = conversations._conversation_get(
                session, {'conv_id': 'conv-d', 'user_id': 1, **payload})
        assert session.connection.in_transaction
    finally:
        session.connection.rollback()
        session.connection.set_trace_callback(None)
    selects = [sql for sql in statements if sql.startswith('SELECT')]
    return result, selects, decode.call_count


def test_inline_turn_read_has_no_archive_reread_or_projection_redecode(database):
    _seed_turn(database, 'turn-1', 1, 2000, projection={'content': 'hello'})
    # Frozen bytes must not be consulted when main-lane Turn authority exists.
    database.execute("UPDATE storage_conversations SET messages_json='not-json'")
    result, selects, decodes = measured_get(database)
    assert len(selects) == 2
    assert 'settings_json' in selects[0] and 'messages_json' not in selects[0]
    assert 'storage_conversation_turns' in selects[1]
    assert decodes == 1
    assert result == {
        'metadata': {'id': 'conv-d', 'user_id': 1, 'title': 'delta',
                     'created_at': 1000, 'updated_at': 1000, 'settings': {},
                     'msg_count': 1, 'search_text': '', 'rev': 3},
        'messages': [{'content': 'hello', 'role': 'assistant', '_turnId': 'turn-1',
                      '_attemptId': None, '_turnActor': 'assistant', '_turnKind': 'reply',
                      '_turnLaneId': 'main', '_turnStatus': 'completed',
                      '_turnSettlement': {'outcome': 'completed', 'cause': 'ingested',
                                          'providerFinishReason': None, 'error': None,
                                          'resumeOptions': [], 'streamState': None,
                                          'evidence': 'external_authority'},
                      '_commandPending': None,
                      '_projectionRevision': 1, 'timestamp': 2000}],
        'source': 'sidecar',
    }
    print('inline conversation: 2 SELECTs, 1 projection decode, exact document matched')


def test_archive_read_serves_frozen_compatibility_not_duplicate_columns(database):
    database.execute(
        'UPDATE storage_conversations SET messages_json=?,msg_count=1',
        ('[{"role":"user","content":"frozen"}]',))
    result, selects, decodes = measured_get(database)
    assert len(selects) == 3 and decodes == 0
    assert 'messages_json' not in selects[0]
    assert selects[2].startswith('SELECT messages_json FROM storage_conversations')
    assert result['messages'] == [{'role': 'user', 'content': 'frozen'}]
    database.execute("UPDATE storage_conversations SET messages_json='[1]'")
    with pytest.raises(StorageError) as raised:
        measured_get(database)
    assert raised.value.code == 'database_integrity'
    print('archive conversation: 3 SELECTs; extra read hydrates distinct frozen bytes; malformed bytes refuse')


def test_owner_filter_stops_before_transcript_read(database):
    result, selects, decodes = measured_get(database, user_id=2)
    assert result is None and len(selects) == 1 and decodes == 0


def test_header_and_transcript_share_snapshot_during_writer_commit(database):
    _seed_turn(database, 'turn-1', 1, 2000, projection={'content': 'before'})
    path = database.connection.execute('PRAGMA database_list').fetchone()[2]
    writer = sqlite3.connect(path, isolation_level=None)
    original_fetch = database.fetch_one

    def fetch_and_commit(sql, params=()):
        row = original_fetch(sql, params)
        if 'settings_json' in sql:
            writer.execute(
                'UPDATE storage_conversation_turns SET projection_json=?,projection_revision=2',
                ('{"content":"after"}',))
        return row

    try:
        with patch.object(database, 'fetch_one', side_effect=fetch_and_commit):
            result, selects, decodes = measured_get(database)
        assert result['messages'][0]['content'] == 'before'
        assert result['messages'][0]['_projectionRevision'] == 1
        assert len(selects) == 2 and decodes == 1
        current, _, _ = measured_get(database)
        assert current['messages'][0]['content'] == 'after'
        assert current['messages'][0]['_projectionRevision'] == 2
        assert database.connection.execute('PRAGMA synchronous').fetchone()[0] == 2
    finally:
        writer.close()
