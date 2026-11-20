"""Continue planning and acceptance agree on the selected source and later-tree fences."""
from dataclasses import replace
import uuid

import pytest

from lib.storage.errors import StorageError
from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend
from tests.storage_authority_sql.test_native_checkpoint_handoff import TransactionClient
from tests.storage_authority_sql.test_sqlite_read_pool import _config

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.unit


@pytest.fixture(params=['sqlite', 'postgres'])
def authority(request, tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    config = _config(tmp_path)
    if request.param == 'postgres':
        from lib.storage_authority_sql.adapters.postgres import PostgresBackend

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
def seeded(authority):
    base = {'conversation_id': 'continue-' + uuid.uuid4().hex, 'user_id': 11}

    def command(name, **payload):
        return authority.command(name, base | payload, uuid.uuid4().hex)

    def append(actor='assistant', status='completed', **payload):
        return command('turn.append_settled', actor=actor, status=status,
                       command_id=uuid.uuid4().hex, **payload)['turn']

    command('conversation.create', conv_id=base['conversation_id'])
    prior = append('human', projection={'content': 'question'})
    anchor = {'type': 'prefix', 'contentChars': 6}
    target = append(status='failed', projection={'content': 'partial', 'toolRounds': [
        {'toolCallId': 'call-1', 'toolName': 'read_files', 'toolArgs': {},
         'toolContent': 'result', 'status': 'done'},
    ]}, settlement={'outcome': 'failed', 'resumeOptions': [{'operation': 'continue', 'anchor': anchor}]})
    query = base | {'turn_id': target['turnId'], 'replay': False}
    context = authority.query('turn.context.read', query)
    pin = {'attempt_id': target['currentAttemptId'], 'projection_revision': target['projectionRevision'],
           'history_digest': context['historyProof']['digest']}
    return command, append, prior, target, anchor, query, pin, context


def test_continue_plan_is_bound_to_prefix_and_read_only(authority, seeded):
    command, append, prior, target, anchor, query, pin, context = seeded
    tail = append(projection={'content': 'later reply'})
    reply = authority.query('turn.context.read', query | {'continue_plan': pin})
    plan = reply.pop('continuePlan')
    assert reply == context
    assert plan['historyDigest'] == context['historyProof']['digest']
    assert plan['anchor'] == anchor
    assert plan['projection']['toolRounds'][0]['attemptId'] == target['currentAttemptId']
    assert authority.query('turn.get', query) == target
    assert authority.query('turn.context.read', query | {'user_id': 12, 'continue_plan': pin}) is None

    for changed in ({'attempt_id': 'wrong-attempt'}, {'projection_revision': 99},
                    {'history_digest': '0' * 64}):
        with pytest.raises(StorageError) as stale:
            authority.query('turn.context.read', query | {'continue_plan': pin | changed})
        assert stale.value.code == 'turn_projection_stale'

    wrong_anchor = dict(anchor, contentChars=999)
    attempt = {'turn_id': target['turnId'], 'expected_projection_revision': target['projectionRevision'],
               'operation': 'continue', 'command_id': 'resume'}
    with pytest.raises(StorageError, match='The requested resume anchor is not current'):
        authority.query('turn.context.read', query | {'continue_plan': pin | {'resume_anchor': wrong_anchor}})
    with pytest.raises(StorageError, match='The requested resume anchor is not current'):
        command('turn.attempt.create', **attempt, resume_anchor=wrong_anchor)

    command('turn.projection.update', turn_id=prior['turnId'],
            expected_projection_revision=prior['projectionRevision'], projection={'content': 'edited'})
    with pytest.raises(StorageError) as stale:
        authority.query('turn.context.read', query | {'continue_plan': pin})
    assert stale.value.code == 'turn_projection_stale'
    assert authority.query('turn.get', query) == target
    accepted = command('turn.attempt.create', **attempt, resume_anchor=anchor)
    assert accepted['attempt']['resumeAnchor'] == anchor
    assert accepted['turn']['projection']['toolRounds'] == plan['projection']['toolRounds']
    assert accepted['deletedTurnIds'] == []
    assert authority.query('turn.get', query | {'turn_id': tail['turnId']}) == tail


def test_continue_plan_checks_live_nested_later_branches(authority, seeded):
    command, append, _, target, _, query, pin, _ = seeded

    def branch(parent):
        return command('turn.branch.create', parent_turn_id=parent['turnId'],
                       expected_projection_revision=parent['projectionRevision'])['lane']['laneId']

    child = append(lane_id=branch(append()))
    grandchild = append(status='failed', lane_id=branch(child),
                        settlement={'outcome': 'failed', 'resumeOptions': ['continue']})
    assert authority.query('turn.context.read', query | {
        'turn_id': grandchild['turnId'], 'continue_plan': pin,
    }) == {'supported': False}
    assert 'continuePlan' in authority.query('turn.context.read', query | {'continue_plan': pin})
    command('turn.attempt.create', turn_id=grandchild['turnId'], command_id='resume-nested',
            operation='continue', expected_projection_revision=grandchild['projectionRevision'])
    with pytest.raises(StorageError, match='running turn or child branch') as read:
        authority.query('turn.context.read', query | {'continue_plan': pin})
    with pytest.raises(StorageError, match='running turn or child branch') as write:
        command('turn.attempt.create', turn_id=target['turnId'], command_id='resume-main',
                operation='continue', expected_projection_revision=target['projectionRevision'])
    assert read.value.code == write.value.code == 'database_conflict'
    assert authority.query('turn.get', query) == target


@pytest.mark.parametrize('extra', [{'replay': True}, {'after_turn_id': 'any'}, {'continue_plan': None}])
def test_continue_plan_requires_cold_head(authority, seeded, extra):
    *_, query, pin, _ = seeded
    with pytest.raises(StorageError) as invalid:
        authority.query('turn.context.read', query | {'continue_plan': pin} | extra)
    assert invalid.value.code == 'database_protocol_error'
