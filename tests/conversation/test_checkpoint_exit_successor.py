"""No-server SQLite child exit followed by one authorized synthetic successor.

TransactionClient is the existing SQL transaction test seam, not a transport
qualification. Its local test starts no server; the private service-restart
spec imports the same assertions with a real storage.v1 transport. Neither
starts a model, real tool command or native helper.
"""
from copy import deepcopy
import json
import multiprocessing
import os
from types import SimpleNamespace

import pytest

from tests.conversation.test_coordinator_session_control import tool_checkpoint_session as tool_checkpoint_session
from tests.storage_authority_sql.test_native_checkpoint_handoff import TransactionClient
from tests.storage_authority_sql.test_sqlite_read_pool import _config

pytestmark = pytest.mark.unit


@pytest.fixture
def storage(tmp_path, monkeypatch):
    from lib import storage as storage_module, turn_lifecycle as turns
    from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend

    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    config = _config(tmp_path)
    backend = SQLiteBackend(config)
    backend.start()
    slot = SimpleNamespace(client=TransactionClient(backend), backend=backend, config=config)
    monkeypatch.setattr(turns, '_turn_client', lambda **kw: slot.client)
    monkeypatch.setattr(storage_module, 'get_storage_client', lambda **kw: slot.client)
    try:
        yield slot
    finally:
        slot.backend.close()


def _exit_after_checkpoint(config, task, claim_path, effect_path, endpoint=None):
    import signal
    signal.signal(signal.SIGALRM, lambda *_: os._exit(24))
    signal.alarm(30)
    from pathlib import Path
    from lib import storage as storage_module, turn_lifecycle as turns
    from lib.durable_worker import WorkerExecutionContext, WorkerJobClaim
    from lib.process_owner import process_owner
    from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend

    if endpoint is None:
        backend = SQLiteBackend(config)
        backend.start()
        client = TransactionClient(backend)
    else:
        from lib.storage.client import StorageClient
        client = StorageClient(*endpoint, timeout=30)
    turns._turn_client = lambda **kw: client
    storage_module.get_storage_client = lambda **kw: client
    from lib.durable_worker import StorageWorkerJobStore
    import time
    document = StorageWorkerJobStore(client_factory=lambda **kw: client).claim_next(
        worker_id='exit-fixture', task_kinds=('conversation-attempt',),
        now_ms=time.time_ns() // 1_000_000, lease_ms=300_000)
    assert document['taskId'] == task['id']
    Path(claim_path).write_text(json.dumps(document))
    context = WorkerExecutionContext(WorkerJobClaim.from_document(
        document, expected_worker_id='exit-fixture'))
    task['_conversationWorkerContext'] = context
    turns.mark_task_started(task['_attemptId'], task['id'], user_id=1,
        worker_claim=context.publication_claim(), worker_process=process_owner())
    task['toolRounds'] = turns.get_turn('session', task['_turnId'], user_id=1)['projection']['toolRounds']
    with Path(effect_path).open('a') as effect:
        effect.write(task['_attemptId'] + '\n')
    task.update(status='running', content='Source synthetic effect committed')
    turns.record_task_event(task, {'type': 'delta', 'content': task['content']})
    os._exit(23)


def _assert_exited_checkpoint_successor(
        tool_checkpoint_session, monkeypatch, tmp_path, record_property):
    import time
    from lib.conversation_sync import runtime, task_start
    from lib.conversation_sync.command_service import ConversationTurnCommandService
    from lib.conversation_sync.worker_execution import conversation_registration
    from lib.durable_worker import StorageWorkerJobStore, SynchronousWorkerRunner, WorkerExecutionContext, WorkerJobClaim
    from lib.storage.errors import StorageError
    from lib.storage.generated_worker_job_contract import WORKER_EXECUTION_EXIT_KEY
    from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend
    from lib.task_replay.task_result_checkpoint_contract import TASK_RESULT_CHECKPOINT_GUARD_CONTRACT
    from lib.tasks_pkg.manager import append_event, persist_task_result, stamp_chat_task_terminal
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from tests.conversation.test_worker_exit_recovery import _watch_recovered_resume

    s = tool_checkpoint_session
    commands = ConversationTurnCommandService(build_user_message=lambda *a: {}, was_aborted_after=lambda *a: False)
    monkeypatch.setattr(runtime, 'conversation_turn_commands', commands)
    before = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
    retained = [deepcopy(row) for row in before['projection']['toolRounds'] if not row.get('_providerAttemptDiscarded')]
    accepted = s.call('control_conversation', s.args)
    assert 'error' not in accepted, accepted
    source_id = accepted['attempt']['attemptId']
    source_task_id = 'conversation-attempt:' + source_id
    store = StorageWorkerJobStore(client_factory=lambda **kw: s.storage.client)
    source_task = dict(id=source_task_id, convId='session', _turnId=s.args['turn_id'],
        _attemptId=source_id, _userId=1, config={}, model='target-model', content='', thinking='')
    effect_path = tmp_path / 'attempt-effects.txt'
    claim_path = tmp_path / 'source-claim.json'
    endpoint = getattr(s.storage, 'endpoint', None)
    if endpoint is None:
        s.storage.backend.close()
    process = multiprocessing.get_context('spawn').Process(target=_exit_after_checkpoint,
        args=(s.storage.config, source_task, str(claim_path), str(effect_path), endpoint))
    process.start()
    try:
        process.join(35)
        assert not process.is_alive(), 'fixture child exceeded its self-exit deadline'
        assert process.exitcode == 23
    finally:
        # The child self-exits on its own deadline; the parent never kills it.
        process.join()
        process.close()
    document = json.loads(claim_path.read_text())
    context = WorkerExecutionContext(WorkerJobClaim.from_document(document, expected_worker_id='exit-fixture'))
    assert context.claim.task_id == source_task_id
    if endpoint is None:
        s.storage.backend = SQLiteBackend(s.storage.config)
        s.storage.backend.start()
        s.storage.client = TransactionClient(s.storage.backend)
    else:
        s.storage.restart()
    frozen = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
    assert frozen['currentAttemptId'] == source_id
    assert frozen['projection']['toolRounds'] == retained
    assert frozen['projection']['content'] == 'Source synthetic effect committed'
    resumed, notifications = _watch_recovered_resume(monkeypatch)
    runner = SynchronousWorkerRunner(worker_id='recovery-fixture', store=store, registrations=[conversation_registration()])
    recovered = runner.run_once()
    assert (recovered.state, recovered.task_id) == ('failed', source_task_id)
    assert resumed.wait(30)
    assert notifications == [(1, 'session', s.args['turn_id'], source_id)]
    settled = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
    assert (settled['status'], settled['settlement']['cause']) == ('interrupted', 'server_restart')
    assert settled['projection']['toolRounds'] == retained
    source_result = s.storage.client.query('task_results.replay_get', {
        'key': source_task_id, 'user_id': 1, 'include_metadata': True})
    assert source_result['metadata']['usageComplete'] is False
    assert source_result['metadata'][WORKER_EXECUTION_EXIT_KEY]['proof'] == 'process_dead'
    assert source_result['metadata'][WORKER_EXECUTION_EXIT_KEY]['settlement'] == 'unknown'
    settled_attempt = s.turns.get_attempt(source_id, user_id=1, include_execution_config=True)
    options = settled['settlement']['resumeOptions']
    option = next((row for row in options if row['operation'] in ('continue', 'checkpoint_resume')), None)
    assert option is not None, settled['settlement']
    args = dict(s.args, attempt_id=source_id, action=option['operation'],
        expected_revision=settled['projectionRevision'], idempotency_key='authorized-successor',
        resume_anchor=option['anchor'])
    successor = s.call('control_conversation', args)
    assert 'error' not in successor, successor
    successor_id = successor['attempt']['attemptId']
    assert successor_id != source_id
    assert successor['turn']['turnId'] == settled['turnId']
    assert s.call('control_conversation', args)['attempt']['attemptId'] == successor_id
    assert s.turns.get_turn('session', settled['turnId'], user_id=1)['projection']['toolRounds'] == retained
    monkeypatch.setattr(task_start, '_throttled_cleanup_old_tasks', lambda: None)
    monkeypatch.setattr(s.turns, 'build_api_messages', lambda *a, **kw: [{'role': 'user', 'content': 'synthetic input'}])
    entered = []

    def complete(task):
        assert task['_attemptId'] == successor_id
        assert task['toolRounds'] == []
        assert task['config']['checkpointToolRounds'] == retained
        entered.append(task['_attemptId'])
        with effect_path.open('a') as effect:
            effect.write(task['_attemptId'] + '\n')
        task['toolRounds'].append(dict(roundNum=5, llmRound=4, toolCallId='successor-harmless',
            toolName='read_files', toolArgs={'path': 'synthetic'}, toolContent='synthetic result', status='done'))
        append_event(task, {'type': 'tool_result', 'roundNum': 5, 'toolCallId': 'successor-harmless',
                            'toolName': 'read_files', 'status': 'done', 'results': [], 'query': 'synthetic'})
        task['content'] = 'Successor completed'
        stamp_chat_task_terminal(task, status='done', finish_reason='stop')
        append_event(task, {'type': 'done', 'finishReason': 'stop'})
        assert persist_task_result(task)

    monkeypatch.setattr('lib.tasks_pkg.orchestrator.api.run_task', complete)
    try:
        outcome = runner.run_once()
        assert (outcome.state, outcome.task_id) == ('succeeded', 'conversation-attempt:' + successor_id)
        assert runner.run_once().state == 'idle'
        assert entered == [successor_id]
        final = s.turns.get_turn('session', settled['turnId'], user_id=1)
        assert (final['status'], final['currentAttemptId']) == ('completed', successor_id)
        assert final['projection']['toolRounds'][:3] == retained
        assert [row['toolCallId'] for row in final['projection']['toolRounds']] == [
            'durable-call-0', 'durable-call-1', 'durable-call-2', 'successor-harmless']
        result = s.storage.client.query('task_results.replay_get', {
            'key': 'conversation-attempt:' + successor_id, 'user_id': 1, 'include_metadata': True})
        assert final['projection']['toolRounds'][-1]['attemptId'] == successor_id
        assert final['projection']['toolRounds'][-1]['toolContent'] == 'synthetic result'
        assert result['status'] == 'done'
        assert result['metadata']['attemptId'] == successor_id
        assert result['metadata']['turnId'] == settled['turnId']
        assert result['metadata'][WORKER_EXECUTION_EXIT_KEY]['settlement'] == 'confirmed'
        assert store.claim_state(context.claim, now_ms=time.time_ns() // 1_000_000) == {'ok': False, 'error': 'stale_fence'}
        record = s.storage.client.query('record.get', {'namespace': 'task_results', 'key': source_task_id})
        with pytest.raises(StorageError) as refused:
            s.storage.client.command('task_results.checkpoint', {
                'key': source_task_id, 'expected_version': record['version'],
                'guard_contract': TASK_RESULT_CHECKPOINT_GUARD_CONTRACT, 'require_parent': True,
                'worker_claim': context.publication_claim(),
                'value': {**record['value'], 'content': 'old result must not land'},
            }, 'old-result-after-successor')
        assert refused.value.code == 'database_conflict'
        assert s.turns.get_turn('session', settled['turnId'], user_id=1) == final
        assert s.storage.client.query('task_results.replay_get', {
            'key': source_task_id, 'user_id': 1, 'include_metadata': True}) == source_result
        assert s.storage.client.query('task_results.replay_get', {
            'key': 'conversation-attempt:' + successor_id, 'user_id': 1, 'include_metadata': True}) == result
        assert s.turns.get_attempt(source_id, user_id=1, include_execution_config=True) == settled_attempt
        assert effect_path.read_text().splitlines() == [source_id, successor_id]
        assert s.call('control_conversation', args)['attempt']['attemptId'] == successor_id
        assert runner.run_once().state == 'idle'
        record_property('sqlite_successor_trace', json.dumps(dict(source=source_id, successor=successor_id,
            effectCounts={source_id: 1, successor_id: 1}, childExit=23,
            transport='storage.v1 TCP' if endpoint else 'SQL transaction test seam',
            retainedToolCallIds=[row['toolCallId'] for row in retained],
            finalToolCallIds=[row['toolCallId'] for row in final['projection']['toolRounds']],
            staleReceipt='database_conflict', recovery='conversation_registration/SynchronousWorkerRunner')))
    finally:
        chat_task_runtime.discard('conversation-attempt:' + successor_id)


def test_exited_checkpoint_admits_one_successor_and_refuses_old_result(
        tool_checkpoint_session, monkeypatch, tmp_path, record_property):
    _assert_exited_checkpoint_successor(
        tool_checkpoint_session, monkeypatch, tmp_path, record_property)
