"""A PostgreSQL model-context reply describes one committed snapshot."""
from dataclasses import replace
import uuid

import pytest

from lib.storage.errors import StorageError

from tests.storage_authority_sql.test_native_checkpoint_handoff import TransactionClient
from tests.storage_authority_sql.test_sqlite_read_pool import _config

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.unit


@pytest.fixture
def context_client(postgres_cluster, tmp_path):
    from lib.storage_authority_sql.adapters.postgres import PostgresBackend

    backend = PostgresBackend(replace(_config(tmp_path), backend='postgres',
                                      postgres_dsn=postgres_cluster))
    backend.start()
    try:
        yield TransactionClient(backend)
    finally:
        backend.close()


@pytest.mark.parametrize('planning', [False, True])
def test_context_proof_and_turns_share_snapshot(context_client, monkeypatch, planning):
    from lib.storage_authority_sql.adapters.postgres import PostgresSession

    def command(operation, payload):
        return context_client.command(operation, payload, uuid.uuid4().hex)

    identity = {'conversation_id': 'context-snapshot', 'user_id': 11}
    command('conversation.create', {'conv_id': identity['conversation_id'], 'user_id': 11})
    for turn_id, actor in [('prior', 'human'), ('target', 'assistant')]:
        command('turn.append_settled', identity | {
            'turn_id': turn_id, 'actor': actor, 'command_id': uuid.uuid4().hex,
            'projection': {'content': turn_id},
            **({'status': 'failed', 'settlement': {'outcome': 'failed', 'resumeOptions': ['continue']}}
               if planning and turn_id == 'target' else {}),
        })
    request = identity | {'turn_id': 'target', 'replay': False}
    before = context_client.query('turn.context.read', request)
    prior = before['turns'][0]
    if planning:
        target = before['turns'][-1]
        request['continue_plan'] = {
            'attempt_id': target['currentAttemptId'],
            'projection_revision': target['projectionRevision'],
            'history_digest': before['historyProof']['digest'],
        }
        before = context_client.query('turn.context.read', request)
        assert before['continuePlan']['historyDigest'] == before['historyProof']['digest']
    fetch_all = PostgresSession.fetch_all
    interleaved = False

    def edit_after_headers(session, sql, params=()):
        nonlocal interleaved
        rows = fetch_all(session, sql, params)
        if (not interleaved and 'model_context_display_offset,ordinal' in sql
                and params[:2] == (identity['conversation_id'], identity['user_id'])):
            interleaved = True
            # Commit via the authority's independent writer before the reader
            # can hydrate any Turn from the already selected header batch.
            command('turn.projection.update', identity | {
                'turn_id': 'prior', 'expected_projection_revision': prior['projectionRevision'],
                'projection': {'content': 'edited'},
            })
        return rows

    with monkeypatch.context() as patch:
        patch.setattr(PostgresSession, 'fetch_all', edit_after_headers)
        during = context_client.query('turn.context.read', request)
    assert interleaved
    if planning:
        with pytest.raises(StorageError) as stale:
            context_client.query('turn.context.read', request)
        assert stale.value.code == 'turn_projection_stale'
    after = context_client.query('turn.context.read', {k: v for k, v in request.items() if k != 'continue_plan'})
    assert after['turns'][0]['projection']['content'] == 'edited'
    assert after['historyProof'] != before['historyProof']
    assert during == before


def test_continue_selection_fences_interleaved_post_messages_with_same_receipt(context_client, monkeypatch):
    from lib.conversation_sync.resume_request_plan import read_cold_resume_candidate
    from lib.storage_authority_sql.adapters.postgres import PostgresSession
    from lib.tasks_pkg.compaction._resume_projection import compaction_resume_witness

    scope = {'conversation_id': 'archive-snapshot', 'user_id': 11}

    def command(operation, **payload):
        return context_client.command(operation, scope | payload, uuid.uuid4().hex)

    command('conversation.create', conv_id=scope['conversation_id'])
    command('turn.append_settled', turn_id='archive-question', actor='human',
            command_id='question', projection={'content': 'question'})
    target = command('turn.append_settled', turn_id='archive-target', actor='assistant',
                     command_id='target', status='failed', projection={'content': 'partial'},
                     settlement={'outcome': 'failed', 'resumeOptions': ['continue']})['turn']
    query = scope | {'turn_id': target['turnId']}
    history = context_client.query('turn.context.read', query | {'replay': False})['historyProof']
    config = {'model': 'fixture'}
    receipt = {'status': 'completed', 'implementation': 'suspended_working_context',
               'resumeWitness': compaction_resume_witness({'_turnId': target['turnId'],
                   'config': config | {'_modelReplayHistoryProof': history}}, rounds=[])}
    archive = {'archive_id': 'snapshot-archive'}
    command('compaction_archive.create', **archive, messages=[])

    def publish(content, **kwargs):
        command('compaction_archive.update_summary', **archive, summary='', tokens_after=0, msgs_after=1,
                post_messages=[{'role': 'assistant', 'content': content}], **kwargs)

    publish('before', receipt=receipt)
    snapshot = context_client.query('turn.sync.snapshot', scope)

    class ReadOnly:
        def query(self, operation, payload, **kwargs):
            return context_client.query(operation, payload)

    monkeypatch.setattr('lib.storage.get_storage_client', lambda: ReadOnly())

    def plan():
        return read_cold_resume_candidate(**query, attempt_id=target['currentAttemptId'],
            projection_revision=target['projectionRevision'], history_digest=history['digest'],
            selection_config=config)

    before = plan()
    assert before.archive_witness.status == 'selected'
    fetched = PostgresSession.fetch_all
    interleaved = False

    def after_archive_metadata(session, sql, params=()):
        nonlocal interleaved
        rows = fetched(session, sql, params)
        if not interleaved and 'FROM storage_compaction_archives' in sql and 'ORDER BY' in sql:
            interleaved = True
            publish('after')  # Commit after selection metadata, before post hydration.
        return rows

    with monkeypatch.context() as patch:
        patch.setattr(PostgresSession, 'fetch_all', after_archive_metadata)
        during = plan()
    after = plan()
    assert interleaved
    assert during == before
    assert after.archive_witness.status == 'selected'
    assert after.archive_witness.candidate_digest == before.archive_witness.candidate_digest
    assert after.archive_witness.content_digest != before.archive_witness.content_digest
    saved = context_client.query('compaction_archive.get', scope | archive | {'include_post_messages': True})
    assert saved['archive']['receipt'] == receipt
    assert saved['postMessages'] == [{'role': 'assistant', 'content': 'after'}]
    assert context_client.query('turn.sync.snapshot', scope) == snapshot
