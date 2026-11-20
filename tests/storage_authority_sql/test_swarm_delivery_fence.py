"""Swarm acknowledgement is atomic with replacement of the durable session."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from uuid import uuid4

import pytest

from lib.storage.errors import StorageError
from lib.storage_authority_sql.operations_pkg import _runs, _swarm_teardown
from lib.swarm.persistence_contract import SWARM_RUN_RESULT_FORMAT, SWARM_RUN_RESULT_NAMESPACE, swarm_run_result_key
from tests.storage_authority_sql.test_native_checkpoint_storage import backend as backend
from tests.storage_authority_sql.test_native_checkpoint_handoff import TransactionClient

pytestmark = pytest.mark.unit


def header(task_id):
    return dict(swarm_key='swarm', task_id=task_id, specs=[], config={'user_id': 7}, now_ms=1)


def checkpoint(run_id):
    return dict(swarm_key='swarm', run_id=run_id, agent_id='worker',
        status='completed', messages=[], result={'final_answer': 'evidence'},
        delivered=False, now_ms=2)


def seed(client):
    client.command('swarm.session.save', header('old'), str(uuid4()))
    client.command('swarm.agent.save', checkpoint('old'), str(uuid4()))
    client.command('record.put', dict(namespace=SWARM_RUN_RESULT_NAMESPACE,
        key=swarm_run_result_key(7, 'old', 'worker'), value=dict(format=SWARM_RUN_RESULT_FORMAT,
            ownerId=7, tenantId='', conversationId='', swarmKey='swarm', swarmRunId='old',
            agentId='worker', role='', objective='', status='completed',
            result={'final_answer': 'evidence'})), str(uuid4()))


def replace_run(session, task_id):
    assert _swarm_teardown.delete_exact(session, dict(swarm_key='swarm', run_id='old',
        owner_id=7, tenant_id='', conversation_id='', project_path='',
        disposition='replacement'))['deleted']
    assert _runs._swarm_session_save(session, {**header(task_id), 'save_mode': 'launch'})['saved']
    _runs._swarm_agent_save(session, checkpoint(task_id))


def acknowledge(client, task_id):
    return client.command('swarm.agents.acknowledge_results',
        dict(swarm_key='swarm', task_id=task_id, agent_ids=['worker', 'worker']), str(uuid4()))


@pytest.mark.parametrize('current_task', ['new', 'n' * 600], ids=['ordinary', 'long'])
def test_replacement_refuses_stale_ack_and_current_ack_is_repeatable(backend, current_task):
    client = TransactionClient(backend)
    seed(client)
    with pytest.raises(StorageError, match='exact teardown'):
        client.command('swarm.session.save', header(current_task), str(uuid4()))
    backend.command('test.swarm.replace', '', None, 'user',
                    lambda session: replace_run(session, current_task),
                    time.monotonic()+20, receipt_required=False)
    assert acknowledge(client, 'old') == {'acknowledged': False, 'changed': 0}
    row = client.query('swarm.session.get', {'swarm_key': 'swarm'})
    assert not row['agents'][0]['delivered']
    assert acknowledge(client, current_task) == {'acknowledged': True, 'changed': 1}
    assert acknowledge(client, current_task) == {'acknowledged': True, 'changed': 1}
    assert client.query('swarm.session.get', {'swarm_key': 'swarm'})['agents'][0]['delivered']


def test_replacement_commit_fences_an_ack_already_waiting_on_storage(backend):
    client = TransactionClient(backend)
    seed(client)
    entered, release, submitted = threading.Event(), threading.Event(), threading.Event()
    def replace(session):
        replace_run(session, 'new')
        entered.set()
        assert release.wait(10)
    def ack():
        submitted.set()
        return acknowledge(client, 'old')
    with ThreadPoolExecutor(max_workers=2) as executor:
        replacing = executor.submit(backend.command, 'test.swarm.replace', '', None,
            'user', replace, time.monotonic()+20, receipt_required=False)
        assert entered.wait(5)
        acknowledging = executor.submit(ack)
        try:
            assert submitted.wait(5)
            with pytest.raises(TimeoutError):
                acknowledging.result(timeout=0.1)
        finally:
            release.set()
        replacing.result(timeout=10)
        assert acknowledging.result(timeout=10) == {'acknowledged': False, 'changed': 0}
    assert not client.query('swarm.session.get', {'swarm_key': 'swarm'})['agents'][0]['delivered']
