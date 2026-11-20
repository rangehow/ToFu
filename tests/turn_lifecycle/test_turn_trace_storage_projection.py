"""Selective replay body projection on an isolated database and backend SQL seam."""
import json
import sqlite3
from contextlib import closing

import pytest
from lib.storage_authority_sql.adapters.sqlite import SQLiteSession
from lib.storage_authority_sql.operations_pkg._records import _event_list, _event_latest
from lib.storage_authority_sql.operations_pkg._records import _append_event_row
from lib.storage import StorageError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('backend', ['sqlite', 'postgres'])
def test_execution_timestamp_survives_delayed_storage_and_selective_replay(backend, monkeypatch):
    from lib.storage_authority_sql.operations_pkg import _records
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.execute('CREATE TABLE storage_events(task_id, sequence, stream_kind, '
                       'event_type, event_kind, event_json, created_at_ms, UNIQUE(task_id, sequence))')
    connection.execute('CREATE TABLE storage_event_read_guards(task_id PRIMARY KEY, '
                       'source_id, append_version, prune_version)')
    # Appends resolve durable-descendant ancestry from task birth records.
    connection.execute('CREATE TABLE storage_records(namespace, record_key, value_json, '
                       'version, updated_at_ms, UNIQUE(namespace, record_key))')
    class Session:
        def execute(self, sql, params):
            return connection.execute(sql, params).rowcount
        def fetch_all(self, sql, params):
            return connection.execute(sql, params).fetchall()
        def fetch_one(self, sql, params):
            return connection.execute(sql, params).fetchone()
    session = Session()
    session.backend = backend
    monkeypatch.setattr(_records.time, 'time', lambda: 9000.0)
    event = {'type': 'delta', 'content': 'bounded synthetic token',
             'executionAtMs': 1_000_123, 'executionOriginAtMs': 1_000_000,
             'executionClockId': 'producer-clock'}
    try:
        assert _append_event_row(session, {'task_id': 'ours', 'sequence': 0,
                                          'event': event})['inserted']
        guard = connection.execute('SELECT * FROM storage_event_read_guards WHERE task_id=?', ('ours',)).fetchone()
        assert guard['append_version'] == 1 and guard['prune_version'] == 0 and guard['source_id']
        replay = _event_list(session, {'task_id': 'ours', 'payload_types': []})
        assert replay == [{'sequence': 0, 'event': {'type': 'delta'}, 'created_at_ms': 1_000_123}]
        assert _event_latest(session, {'task_id': 'ours', 'metadata_only': True}) == replay[0]
        assert not _append_event_row(session, {'task_id': 'ours', 'sequence': 0,
                                              'event': event})['inserted']
        for timestamp in (True, None, -1):
            with pytest.raises(StorageError):
                _append_event_row(session, {'task_id': 'ours', 'sequence': 1,
                                           'event': {**event, 'executionAtMs': timestamp}})
        assert len(_event_list(session, {'task_id': 'ours'})) == 1
    finally:
        connection.close()


@pytest.mark.parametrize('backend', ['sqlite', 'postgres'])
def test_projection_omits_unselected_even_invalid_payloads(backend):
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.execute('CREATE TABLE storage_events(task_id, sequence, event_type, event_json, created_at_ms)')
    connection.executemany('INSERT INTO storage_events VALUES(?,?,?,?,?)', [
        ('ours', 0, 'delta', 'intentionally not JSON', 10),
        ('ours', 1, 'round_start', '{"type":"round_start","roundNum":1}', 11),
        ('other', 0, 'round_start', '{"private":true}', 12),
    ])
    class Session:
        def fetch_all(self, sql, params):
            return connection.execute(sql, params).fetchall()
        def fetch_one(self, sql, params):
            return connection.execute(sql, params).fetchone()
    session = Session()
    session.backend = backend
    try:
        result = _event_list(session, {'task_id': 'ours', 'payload_types': ['round_start']})
        assert result == [
            {'sequence': 0, 'event': {'type': 'delta'}, 'created_at_ms': 10},
            {'sequence': 1, 'event': {'type': 'round_start', 'roundNum': 1}, 'created_at_ms': 11},
        ]
        assert _event_list(session, {'task_id': 'ours', 'payload_types': [], 'limit': 1}) == result[:1]
        assert _event_latest(session, {'task_id': 'ours', 'metadata_only': True}) == {
            'sequence': 1, 'event': {'type': 'round_start'}, 'created_at_ms': 11}
        with pytest.raises(StorageError):
            _event_list(session, {'task_id': 'ours', 'payload_types': ['x'] * 65})
    finally:
        connection.close()


def test_sqlite_filtered_replay_applies_global_limit_before_decoding(tmp_path):
    with closing(sqlite3.connect(tmp_path / 'events.sqlite3')) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('CREATE TABLE storage_events(task_id TEXT, sequence INTEGER, '
                           'event_type TEXT, event_json TEXT, created_at_ms INTEGER, '
                           'PRIMARY KEY(task_id, sequence))')
        connection.executemany('INSERT INTO storage_events VALUES (?, ?, ?, ?, ?)', [
            ('ours', 0, 'round_end_extra', 'not JSON', 100),
            ('ours', 1, 'round_end', '{"type":"round_end","roundNum":1}', 101),
            ('ours', 2, 'round_end', '{"type":"round_end","roundNum":2}', 102),
            ('ours', 3, 'round_end', 'not JSON', 103),
            ('ours', 4, 'toolXprogress', 'not JSON', 104),
            ('ours', 10, 'tool_progress', '{"type":"tool_progress","body":"hidden"}', 110),
            ('ours', 11, 'tool_progress', 'not JSON', 111),
            ('other', 1, 'round_end', 'not JSON', 201),
        ])
        session = SQLiteSession(connection)
        request = {'task_id': 'ours', 'types': ['round_end'], 'type_prefixes': ['tool_'],
                   'payload_types': ['round_end'], 'limit': 2}

        # The first page belongs entirely to the earlier type. Sequence 3 is
        # selected for payload decoding, but is not yet on the page.
        first = _event_list(session, request)
        assert first == [
            {'sequence': 1, 'event': {'type': 'round_end', 'roundNum': 1}, 'created_at_ms': 101},
            {'sequence': 2, 'event': {'type': 'round_end', 'roundNum': 2}, 'created_at_ms': 102},
        ]
        continuation = {**request, 'after_sequence': first[-1]['sequence']}
        with pytest.raises(json.JSONDecodeError):
            _event_list(session, continuation)

        metadata = {**continuation, 'payload_types': []}
        second = _event_list(session, metadata)
        assert second == [
            {'sequence': 3, 'event': {'type': 'round_end'}, 'created_at_ms': 103},
            {'sequence': 10, 'event': {'type': 'tool_progress'}, 'created_at_ms': 110},
        ]
        third = _event_list(session, {**request, 'after_sequence': second[-1]['sequence']})
        assert third == [
            {'sequence': 11, 'event': {'type': 'tool_progress'}, 'created_at_ms': 111},
        ]
        assert _event_list(session, {**request, 'after_sequence': third[-1]['sequence']}) == []
