"""Freeze the entire SQL checkpoint cut, not just its session metadata."""
from copy import deepcopy
from uuid import uuid4

import pytest

from lib.storage.errors import StorageError
from lib.swarm import persistence
from lib.swarm.message_contract import NAMESPACE
from lib.swarm.persistence_contract import SWARM_DELETE_EXACT_OPERATION, swarm_mailbox_key
from tests.storage_authority_sql.test_swarm_lifecycle_receipt import client as client
from tests.storage_authority_sql.test_swarm_runtime_lifecycle import runtime as runtime

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.unit

RUN = 'checkpoint-run'
SAVE = 'swarm.agent.save'
APPEND = 'swarm.agent.append'
MARK = 'swarm.agent.checkpoint.mark'
ACK = 'swarm.agents.acknowledge_results'
LEGACY_ACK = 'swarm.agents.mark_delivered'
TERMINATE = 'swarm.session.terminate'


def write(client, operation, key, **payload):
    return client.command(operation, {'swarm_key': key, **payload}, uuid4().hex)


def saved(checkpoint_id, agent_id='child'):
    return dict(run_id=RUN, agent_id=agent_id, checkpoint_id=checkpoint_id,
                messages=[{'role': 'user', 'content': 'objective'}], result={},
                status='running', now_ms=1)


def snapshot(key):
    return persistence.load_session(key, required=True)


def setup_run(runtime):
    client, task, _ = runtime
    key = task['convId']
    turn, attempt = task['config']['_turnId'], task['config']['_attemptId']
    accepted = client.query('turn.attempt.get', {
        'attempt_id': attempt, 'user_id': 7, 'include_execution_config': True})
    source = accepted['config']['_resumeSource']
    config = {'user_id': 7, 'parent_cfg': {'_swarmParentTurnId': turn,
        '_swarmParentAttemptId': source['attemptId'], '_swarmParentTaskId': source['taskId']}}
    write(client, 'swarm.session.save', key, task_id=RUN, conv_id=key, save_mode='launch',
          config=config, specs=[{'id': 'child'}, {'id': 'second'}], now_ms=100)
    assert write(client, SAVE, key, **saved('base'))['saved']
    successor = deepcopy(config)
    successor['parent_cfg'].update(_swarmParentAttemptId=attempt, _swarmParentTaskId=task['id'])
    identity = persistence.run_identity(key, snapshot(key))
    identity.pop('swarm_key')
    return client, key, dict(identity,
        caller_task_id=task['id'], turn_id=turn, attempt_id=attempt, config=successor,
        save_mode='continue_rebind')


def test_rebind_rejects_stale_cuts_and_all_predecessor_checkpoint_writers(runtime):
    client, key, rebind = setup_run(runtime)
    append = dict(run_id=RUN, agent_id='child', base_checkpoint_id='new-base',
                  base_message_count=1, checkpoint_id='appended', messages=[], result={}, now_ms=1)
    marker = dict(run_id=RUN, agent_id='child', rounds_used=1, now_ms=1,
                  result={'checkpoint_phase': 'ready'})
    delivery = dict(task_id=RUN, agent_ids=['child'])
    mutations = [(SAVE, saved('new-base'), 'saved'), (APPEND, append, 'appended'),
                 (MARK, marker, 'marked'), (ACK, delivery, 'acknowledged')]
    for operation, payload, ack in mutations:
        cut = snapshot(key)
        assert write(client, operation, key, **payload)[ack]
        assert snapshot(key)['updated_at'] == cut['updated_at'] + 1
        with pytest.raises(StorageError, match='frozen revision changed'):
            write(client, 'swarm.session.save', key,
                  expected_updated_at=cut['updated_at'], **rebind)

    archive = dict(owner_id=7, tenant_id='', conversation_id=key, swarm_key=key,
                   swarm_run_id=RUN, agent_id='child', role='coder', objective='objective',
                   status='completed')
    archive_address = {field: archive[field] for field in ('owner_id', 'swarm_run_id', 'agent_id')}
    assert persistence.save_run_result(**archive, result={'final_answer': 'original archive'})
    original_archive = persistence.load_run_result(**archive_address)
    address = dict(namespace=NAMESPACE, key=swarm_mailbox_key(7, '', key, RUN, 'child'))
    mailbox = {'binding': dict(owner=7, tenant='', conversationId=key, projectPath='',
                               swarmKey=key, swarmRunId=RUN, agentId='child'),
               'closed': False, 'messages': []}
    client.command('record.put', {**address, 'value': mailbox, 'expected_version': 0}, uuid4().hex)
    original_mailbox = client.query('record.get', address)
    before = snapshot(key)
    rebound = write(client, 'swarm.session.save', key,
                    expected_updated_at=before['updated_at'], **rebind)
    epoch = rebound['checkpoint_writer_epoch']
    assert epoch == before['checkpoint_writer_epoch'] + 1
    after = snapshot(key)
    assert after['checkpoint_writer_epoch'] == epoch
    assert after['agents'] == before['agents']
    for epoch_fields in ({}, {'checkpoint_writer_epoch': epoch - 1}):
        for operation, payload, _ in mutations[:3]:
            with pytest.raises(StorageError, match='exact current swarm writer'):
                write(client, operation, key, **payload, **epoch_fields)
        assert write(client, ACK, key, **delivery, **epoch_fields) == {'acknowledged': False, 'changed': 0}
        assert write(client, LEGACY_ACK, key, **delivery, **epoch_fields) == {'changed': 0}
        assert write(client, TERMINATE, key, run_id=RUN, now_ms=1, **epoch_fields) == {'changed': False}
    assert not persistence.save_run_result(**archive, result={'final_answer': 'stale overwrite'})
    assert persistence.load_run_result(**archive_address) == original_archive
    close = {**address, 'value': {**mailbox, 'closed': True},
             'expected_version': original_mailbox['version']}
    with pytest.raises(StorageError, match='Mailbox writer epoch changed'):
        client.command('record.put', close, uuid4().hex)
    assert client.query('record.get', address) == original_mailbox
    client.command('record.put', {**close, 'checkpoint_writer_epoch': epoch}, uuid4().hex)
    assert client.query('record.get', address)['value']['closed']
    identity = persistence.run_identity(key, after)
    # Both host-side and authority-side retirement must refuse old residency.
    assert persistence.dispose_run(identity, 'ordinary_retirement', checkpoint_writer_epoch=0) is False
    with pytest.raises(StorageError, match='retirement requires its exact current swarm writer'):
        client.command(SWARM_DELETE_EXACT_OPERATION,
                       {**identity, 'disposition': 'replacement'}, uuid4().hex)
    roster = dict(task_id=RUN, conv_id=key, config=after['config'], save_mode='roster',
                  specs=[{'id': 'new-agent'}], now_ms=1)
    with pytest.raises(StorageError, match='update requires its exact current swarm writer'):
        write(client, 'swarm.session.save', key, **roster)
    assert snapshot(key) == after
    assert write(client, 'swarm.session.save', key, **roster, checkpoint_writer_epoch=epoch)['saved']
    assert persistence.save_run_result(**archive, result={'final_answer': 'successor archive'},
                                       checkpoint_writer_epoch=epoch)
    for operation, payload, ack in mutations:
        assert write(client, operation, key, **payload, checkpoint_writer_epoch=epoch)[ack]
    assert write(client, TERMINATE, key, run_id=RUN, now_ms=1,
                 checkpoint_writer_epoch=epoch) == {'changed': True}
    assert persistence.dispose_run(identity, 'stop')  # Owner Stop stays run-scoped.


@pytest.mark.parametrize('race', ['between_pages', 'after_last_page'])
def test_checkpoint_load_rejects_a_cross_revision_cut(runtime, monkeypatch, race):
    client, key, _ = setup_run(runtime)
    assert write(client, SAVE, key, **saved('other', agent_id='second'))['saved']
    monkeypatch.setattr('lib.storage_authority_sql.operations_pkg._runs._SWARM_AGENT_PAGE_BYTES', 350)
    actual = client.query
    pages = []

    def query(operation, payload, *args, **kwargs):
        result = actual(operation, payload, *args, **kwargs)
        if operation == 'swarm.agents.page':
            cursor = result['next_after_agent_id']
            pages.append(cursor)
            if (cursor is None) == (race == 'after_last_page'):
                assert write(client, MARK, key, agent_id='child', run_id=RUN, rounds_used=1,
                             result={'checkpoint_phase': 'ready'}, now_ms=1)['marked']
        return result

    with monkeypatch.context() as patch:
        patch.setattr(client, 'query', query)
        with pytest.raises(StorageError) as error:
            snapshot(key)
        assert error.value.code == 'database_conflict'
        assert pages == (['child'] if race == 'between_pages' else ['child', None])
    assert [agent['agent_id'] for agent in snapshot(key)['agents']] == ['child', 'second']


def test_epoch_zero_recovery_refuses_rotation_between_authority_reads(runtime, monkeypatch):
    from lib.swarm.integration._sql_lifecycle import require_current_run

    client, key, rebind = setup_run(runtime)
    cut = snapshot(key)
    identity = persistence.run_identity(key, cut)
    assert require_current_run(identity, managed=False, checkpoint_writer_epoch=0)
    read = persistence.read_lifecycle

    def rotate(scope):
        assert write(client, 'swarm.session.save', key, expected_updated_at=cut['updated_at'],
                     **rebind)['checkpoint_writer_epoch'] == 1
        return read(scope)

    with monkeypatch.context() as patch:
        patch.setattr(persistence, 'read_lifecycle', rotate)
        with pytest.raises(StorageError, match='not current and runnable'):
            require_current_run(identity, managed=False, checkpoint_writer_epoch=0)
    assert require_current_run(identity, managed=False, checkpoint_writer_epoch=1)


def test_late_provider_ack_cannot_borrow_rebound_session_epoch(runtime, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from lib.swarm.delivery import acknowledge_presented_results, current_result_hints, stage_tool_results

    client, key, rebind = setup_run(runtime)
    predecessor = SimpleNamespace(user_id=7, task_id=RUN, _checkpoint_writer_epoch=0)
    old_task = {'id': key, 'convId': key, '_userId': 7}
    rows = [{'agent_id': 'child', 'status': 'completed', 'final_answer': 'evidence'}]
    stage_tool_results(old_task, predecessor, rows)
    old_hint = old_task['_swarm_inject_pending'][0]
    epoch = write(client, 'swarm.session.save', key, expected_updated_at=snapshot(key)['updated_at'],
                  **rebind)['checkpoint_writer_epoch']
    successor = SimpleNamespace(user_id=7, task_id=RUN, _checkpoint_writer_epoch=epoch,
                                mark_results_delivered=Mock())
    with monkeypatch.context() as patch:
        patch.setattr('lib.swarm.integration.get_active_session', lambda _: successor)
        assert current_result_hints(key, [old_hint]) == []
        assert acknowledge_presented_results(old_task)
        assert not snapshot(key)['agents'][0]['delivered']
        successor.mark_results_delivered.assert_not_called()

        new_task = {'id': key, 'convId': key, '_userId': 7}
        stage_tool_results(new_task, successor, rows)
        new_hint = new_task['_swarm_inject_pending'][0]
        assert current_result_hints(key, [old_hint, new_hint]) == [new_hint]
        assert acknowledge_presented_results(new_task)
        assert snapshot(key)['agents'][0]['delivered']
        successor.mark_results_delivered.assert_called_once_with(['child'])
