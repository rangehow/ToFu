"""Optional Continue evidence fails closed on corrupt persisted SQL records."""
from dataclasses import replace
import time
import uuid

import pytest

from lib.conversation_sync.resume_request_plan import read_cold_resume_candidate
from lib.storage.errors import StorageError
from lib.storage.model_continuation import settled_witness
from lib.storage_authority_sql.adapters.postgres import PostgresBackend
from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend
from lib.storage_authority_sql.operations_pkg._common import _dump
from lib.storage_authority_sql.projection_codec import STORAGE_PROJECTION_CODEC_KEY
from lib.tasks_pkg.compaction._resume_projection import compaction_resume_witness
from lib.tasks_pkg.model_replay import policy_witness
from tests.storage_authority_sql.test_native_checkpoint_handoff import TransactionClient
from tests.storage_authority_sql.test_sqlite_read_pool import _config

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.unit


@pytest.fixture(params=['sqlite', 'postgres'])
def sql_client(request, tmp_path):
    config = _config(tmp_path)
    if request.param == 'postgres':
        backend = PostgresBackend(replace(config, backend='postgres',
                                          postgres_dsn=request.getfixturevalue('postgres_cluster')))
    else:
        backend = SQLiteBackend(config)
    backend.start()
    try:
        yield TransactionClient(backend)
    finally:
        backend.close()


@pytest.fixture
def selection(sql_client, monkeypatch):
    scope = dict(conversation_id='corruption-selection', user_id=41)

    def command(op, **payload):
        return sql_client.command(op, scope | payload, uuid.uuid4().hex)

    def query(op, **payload):
        return sql_client.query(op, scope | payload)

    command('conversation.create', conv_id=scope['conversation_id'])
    command('turn.append_settled', turn_id='input', command_id='input', actor='human',
            projection={'content': 'question'})
    command('turn.append_settled', turn_id='publisher', command_id='publisher', actor='assistant',
            projection={'content': 'answer'})
    publisher = query('turn.context.read', turn_id='publisher', replay=False)
    target = command('turn.append_settled', turn_id='source', command_id='source',
                     actor='assistant', status='failed', projection={'content': 'partial'},
                     settlement={'outcome': 'failed', 'resumeOptions': ['continue']})['turn']
    history = query('turn.context.read', turn_id='source', replay=False)['historyProof']
    pin = dict(attempt_id=target['currentAttemptId'], projection_revision=target['projectionRevision'],
               history_digest=history['digest'])
    config = {'model': 'fixture'}
    checkpoint = {'turns': [settled_witness(publisher['turns'][-1])],
                  'historyProof': publisher['historyProof'], 'policy': policy_witness(config),
                  'messages': [{'role': 'assistant', 'content': 'retained prefix'}]}
    assert command('model_continuation.put', lane_id='main', projection=checkpoint)
    receipt = {'status': 'completed', 'implementation': 'suspended_working_context',
               'resumeWitness': compaction_resume_witness({'_turnId': 'source', 'config':
                   config | {'_modelReplayHistoryProof': history}}, rounds=[])}
    command('compaction_archive.create', archive_id='archive', messages=[])
    command('compaction_archive.update_summary', archive_id='archive', summary='',
            tokens_after=0, msgs_after=1, receipt=receipt,
            post_messages=[{'role': 'assistant', 'content': 'retained archive'}])
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **_: sql_client)

    def plan(**opts):
        return read_cold_resume_candidate(**scope, turn_id='source', **pin, **opts)

    return query, pin, config, plan


@pytest.mark.parametrize('record', ['receipt', 'post_messages', 'continuation'])
def test_persisted_corruption_only_invalidates_its_own_witness(sql_client, selection, record):
    _assert_independent_witness(sql_client, selection, record)


@pytest.mark.parametrize('sql_client', ['sqlite'], indirect=True)
@pytest.mark.parametrize(('column', 'value', 'storage_class'), [
    ('created_at_ms', 0.5, 'real'),
    ('round_num', '', 'text'),
    ('tokens_before', 'nonnumeric', 'text'),
    ('tokens_after', 1.5, 'real'),
    ('msgs_before', b'1', 'blob'),
    ('payload_size', float('inf'), 'real'),
])
def test_persisted_malformed_numeric_metadata(sql_client, selection, column, value, storage_class):
    _assert_independent_witness(sql_client, selection, 'numeric', (column, value, storage_class))


def test_persisted_negative_count(sql_client, selection):
    _assert_independent_witness(sql_client, selection, 'numeric', ('msgs_after', -1, None))


def _assert_independent_witness(sql_client, selection, record, numeric=None):
    query, pin, config, plan = selection
    request = dict(turn_id='source', replay=False)
    cold = query('turn.context.read', **request, continue_plan=pin)
    before = plan(selection_config=config)
    assert before.archive_witness.status == before.continuation_witness.status == 'selected'
    snapshot = query('turn.sync.snapshot')

    def corrupt(session):
        if numeric is not None:
            column, value, storage_class = numeric
            count = session.execute(f'UPDATE storage_compaction_archives SET {column}=? WHERE archive_id=?',
                                    (value, 'archive'))
            if storage_class is not None:
                stored = session.fetch_one(
                    f'SELECT typeof({column}) AS kind FROM storage_compaction_archives WHERE archive_id=?',
                    ('archive',),
                )
                assert stored['kind'] == storage_class
            return count
        # Valid JSON on both engines, invalid at the persisted record decoder.
        corrupt_value = _dump({STORAGE_PROJECTION_CODEC_KEY: {'version': -1}})
        if record == 'continuation':
            return session.execute('UPDATE storage_records SET value_json=? WHERE namespace=?',
                                   (corrupt_value, 'model.continuation.v1'))
        if record == 'post_messages':
            corrupt_value = _dump([None])  # Invalid archived-message sequence member.
        return session.execute(f'UPDATE storage_compaction_archives SET {record}_json=? WHERE archive_id=?',
                               (corrupt_value, 'archive'))

    assert sql_client.backend.command('scratch.corrupt', 'corrupt', uuid.uuid4().hex, 'user',
                                      corrupt, time.monotonic() + 20, receipt_required=False) == 1
    ordinary_op, ordinary_args = {
        'receipt': ('compaction_archive.list', {}),
        'numeric': ('compaction_archive.list', {}),
        'post_messages': ('compaction_archive.get', {'archive_id': 'archive', 'include_post_messages': True}),
        'continuation': ('turn.context.read', {'turn_id': 'source'}),
    }[record]
    with pytest.raises(StorageError) as error:
        query(ordinary_op, **ordinary_args)
    assert error.value.code == 'database_integrity'
    if numeric is not None:
        with pytest.raises(StorageError) as detail_error:
            query('compaction_archive.get', archive_id='archive', include_messages=False)
        assert detail_error.value.code == 'database_integrity'

    response = query('turn.context.read', **request, continue_plan=pin | {'selection': True})
    inputs = response.pop('continueSelectionInputs')
    assert response == cold == query('turn.context.read', **request, continue_plan=pin)
    after = plan(selection_config=config)
    assert after.messages_json == before.messages_json == plan().messages_json
    if record == 'continuation':
        assert (inputs['continuationStatus'], inputs['covered'], inputs['continuation']) == (
            'invalid_checkpoint', 0, None)
        assert after.continuation_witness.status == 'indeterminate'
        assert after.archive_witness == before.archive_witness
    else:
        assert inputs['archives'] is None
        assert after.archive_witness.status == 'indeterminate'
        assert after.continuation_witness == before.continuation_witness
    assert query('turn.sync.snapshot') == snapshot
    with pytest.raises(StorageError) as still_corrupt:
        query(ordinary_op, **ordinary_args)
    assert still_corrupt.value.code == 'database_integrity'


@pytest.mark.parametrize('read_source', ['archive_list', 'archive_post', 'continuation'])
@pytest.mark.parametrize('code', ['database_integrity', 'database_unavailable'])
def test_optional_evidence_does_not_swallow_failed_sql_read(sql_client, selection, monkeypatch, read_source, code):
    from lib.storage_authority_sql.adapters.postgres import PostgresSession
    from lib.storage_authority_sql.adapters.sqlite import SQLiteSession

    query, pin, _, _ = selection
    session_class = PostgresSession if isinstance(sql_client.backend, PostgresBackend) else SQLiteSession
    method, marker = {
        'archive_list': ('fetch_all', 'FROM storage_compaction_archives'),
        'archive_post': ('fetch_one', 'FROM storage_compaction_archives'),
        'continuation': ('fetch_one', 'SELECT value_json FROM storage_records'),
    }[read_source]
    fetch = getattr(session_class, method)

    def fail(session, sql, params=()):
        if marker in sql:
            # Even the same code from a SQL read is not record decode evidence.
            raise StorageError(code, 'Scratch read failure')
        return fetch(session, sql, params)

    monkeypatch.setattr(session_class, method, fail)
    with pytest.raises(StorageError, match='Scratch read failure') as error:
        query('turn.context.read', turn_id='source', replay=False, continue_plan=pin | {'selection': True})
    assert error.value.code == code
