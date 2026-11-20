"""Compose retained history, a held SQLite writer, process exit and public retry.

The executor is synthetic and model-free. The selected SQLite transactions and
TCP transport are real; no server restart or production authority participates.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from tests.conversation.test_coordinator_session_control import tool_checkpoint_session as tool_checkpoint_session
from tests.storage_authority_sql.test_sqlite_read_pool import _config

pytestmark = pytest.mark.api


@pytest.fixture
def storage(tmp_path, monkeypatch):
    from lib import storage as storage_module, turn_lifecycle as turns
    from lib.storage.client import StorageClient
    from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend
    from lib.storage_authority_sql.server import create_server

    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    config = _config(tmp_path)
    backend = SQLiteBackend(config)
    backend.start()
    server = create_server(backend, config.token)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    client = StorageClient('127.0.0.1', server.server_address[1], config.token, timeout=30)
    monkeypatch.setattr(turns, '_turn_client', lambda **kw: client)
    monkeypatch.setattr(storage_module, 'get_storage_client', lambda **kw: client)
    try:
        yield SimpleNamespace(client=client, backend=backend, server=server,
                              endpoint=('127.0.0.1', server.server_address[1], config.token))
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(30)
        backend.close()
        assert not thread.is_alive()


def _execute(endpoint, task, claim_document, effect_path, channel):
    from lib import storage as storage_module, turn_lifecycle as turns
    from lib.process_owner import process_owner
    from lib.durable_worker import WorkerExecutionContext, WorkerJobClaim
    from lib.storage.client import StorageClient
    from lib.storage.errors import StorageError

    client = StorageClient(*endpoint, timeout=30)
    turns._turn_client = lambda **kw: client
    storage_module.get_storage_client = lambda **kw: client
    witness = process_owner()
    context = WorkerExecutionContext(WorkerJobClaim.from_document(
        claim_document, expected_worker_id='fixture-worker'))
    task['_conversationWorkerContext'] = context
    try:
        started = turns.mark_task_started(task['_attemptId'], task['id'], user_id=1,
            worker_claim=context.publication_claim(), worker_process=witness)
        assert started['status'] == 'running'
        saved = turns.get_turn(task['convId'], task['_turnId'], user_id=1)
        task['toolRounds'] = saved['projection']['toolRounds']
        channel.send(('started', witness))
        with Path(effect_path).open('a') as effect:
            effect.write('synthetic execution once\n')
        task.update(status='done', finishReason='stop', content='Synthetic retained work complete')
        try:
            turns.record_task_event(task, {'type': 'done', 'finishReason': 'stop'})
        except StorageError as error:
            channel.send(('ack-lost', error.code))
        else:
            raise AssertionError('execution received a success ACK')
    finally:
        client.close()
        channel.close()


def test_retained_checkpoint_writer_commit_exit_and_public_same_attempt_retry(
        tool_checkpoint_session, monkeypatch, tmp_path, record_property):
    s = tool_checkpoint_session
    from lib.conversation_sync.command_service import ConversationTurnCommandService
    from lib.conversation_sync import runtime
    from lib.storage.errors import StorageError
    from lib.storage_authority_sql import server as transport

    before = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
    retained = [deepcopy(row) for row in before['projection']['toolRounds']
                if not row.get('_providerAttemptDiscarded')]
    assert [row['toolCallId'] for row in retained] == [f'durable-call-{n}' for n in range(3)]
    assert [row['llmRound'] for row in retained] == [1, 2, 3]
    effect_path = tmp_path / 'effect.txt'
    entered, release, committed, dropped, reached = (threading.Event() for _ in range(5))
    stages, launches, responses, operations = [], [], [], []
    actual_command = s.storage.backend.command
    actual_response = transport._StorageHandler._send_response
    armed = threading.Event()

    def hold_writer(*args, **kwargs):
        operations.append(args[0])
        if args[0] != 'turn.event.record' or not armed.is_set():
            result = actual_command(*args, **kwargs)
            if args[0] == 'turn.attempt.start' and armed.is_set():
                stages.append('execution-start-committed')
            return result
        def transaction(session):
            stages.append('writer-entered-before-commit')
            entered.set()
            reached.set()
            assert release.wait(30), 'owner did not release scratch writer'
            return args[4](session)
        result = actual_command(*args[:4], transaction, *args[5:], **kwargs)
        stages.append('durable-commit')
        committed.set()
        return result

    def drop_ack(handler, response, request_id, operation):
        if handler.server is s.storage.server and operation == 'turn.event.record' and armed.is_set():
            assert committed.is_set()
            assert response['ok'], response
            responses.append(response['result'])
            stages.append('tcp-success-ack-dropped')
            dropped.set()
            return False
        return actual_response(handler, response, request_id, operation)

    monkeypatch.setattr(s.storage.backend, 'command', hold_writer)
    monkeypatch.setattr(transport._StorageHandler, '_send_response', drop_ack)
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    processes = []

    def launch(result):
        attempt_id = result['attempt']['attemptId']
        task_id = 'conversation-attempt:' + attempt_id
        accepted = s.turns.get_attempt(attempt_id, user_id=1, include_execution_config=True)
        config = accepted['config']
        task = dict(id=task_id, convId='session', _turnId=s.args['turn_id'],
                    _attemptId=attempt_id, _userId=1, config=config,
                    model='target-model', content='', thinking='')
        claim = s.storage.client.command('worker_job.claim_next', {
            'worker_id': 'fixture-worker', 'task_kinds': ['conversation-attempt'],
            'now_ms': 1, 'lease_ms': 300_000}, 'fixture-claim')
        assert claim['taskId'] == task_id
        launches.append(attempt_id)
        process = context.Process(target=_execute,
            args=(s.storage.endpoint, task, claim, str(effect_path), child))
        processes.append(process)
        armed.set()
        process.start()
        child.close()
        assert parent.poll(30), 'execution did not publish start'
        kind, witness = parent.recv()
        assert kind == 'started' and witness['pid'] == process.pid
        stages.append('execution-process-observed')
        from lib.storage.generated_worker_job_contract import WORKER_EXECUTION_CONFIG_KEY
        entered_attempt = s.turns.get_attempt(attempt_id, user_id=1, include_execution_config=True)
        assert entered_attempt['config'][WORKER_EXECUTION_CONFIG_KEY]['process'] == witness
        assert parent.poll(30), 'execution did not observe ACK loss'
        kind, _code = parent.recv()
        assert kind == 'ack-lost'
        process.join(30)
        assert process.exitcode == 0, 'fixture execution failed to drain'
        stages.append('execution-exited')

    commands = ConversationTurnCommandService(build_user_message=lambda *a: {},
        was_aborted_after=lambda *a: False)
    monkeypatch.setattr(runtime, 'conversation_turn_commands', commands)
    create = commands.create_attempt

    def lose_public_ack(*args, **kwargs):
        result = create(*args, **kwargs)
        if not launches:
            launch(result.value)
            assert processes[0].exitcode == 0
            stages.append('public-ack-lost')
            raise StorageError('database_unavailable', 'fixture public admission ACK lost')
        return result

    monkeypatch.setattr(commands, 'create_attempt', lose_public_ack)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(s.call, 'control_conversation', s.args)
            pending.add_done_callback(lambda future: reached.set())
            try:
                assert reached.wait(30), stages
                assert entered.is_set(), pending.result(timeout=1)
                held = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
                assert held['status'] == 'running'
                assert held['projection']['toolRounds'] == retained
                assert not committed.is_set() and not dropped.is_set() and not pending.done()
                assert processes[0].is_alive()
            finally:
                release.set()
            uncertain = pending.result(timeout=30)
        assert uncertain['execution_started'] is None, uncertain
        assert uncertain['code'] == 'database_unavailable', uncertain
        assert committed.is_set() and dropped.is_set() and len(responses) == 1
        assert len(launches) == 1
        stored = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
        assert stored['status'] == 'completed'
        assert stored['projection']['toolRounds'] == retained
        assert stored['projection']['content'] == 'Synthetic retained work complete'
        assert stored['currentAttemptId'] == launches[0] != before['currentAttemptId']
        assert effect_path.read_text() == 'synthetic execution once\n'
        job = s.storage.client.query('worker_job.get', {
            'task_id': 'conversation-attempt:' + launches[0], 'user_id': 1})
        assert job['taskId'] == 'conversation-attempt:' + launches[0]
        stages.append('public-retry')
        replay = s.call('control_conversation', s.args)
        assert 'error' not in replay, replay
        assert replay['attempt']['attemptId'] == launches[0]
        assert s.call('control_conversation', s.args)['attempt']['attemptId'] == launches[0]
        assert launches == [stored['currentAttemptId']]
        assert s.storage.client.query('worker_job.get', {
            'task_id': job['taskId'], 'user_id': 1}) == job
        assert operations.count('turn.attempt.dispatch_worker') == 1
        assert operations.count('worker_job.claim_next') == 1
        assert operations.count('turn.attempt.start') == 1
        assert operations.count('turn.event.record') == 1
        assert s.turns.get_turn('session', s.args['turn_id'], user_id=1) == stored
        assert effect_path.read_text() == 'synthetic execution once\n'
        assert stages.index('execution-start-committed') < stages.index('writer-entered-before-commit')
        assert stages.index('writer-entered-before-commit') < stages.index('durable-commit')
        assert stages.index('durable-commit') < stages.index('tcp-success-ack-dropped')
        assert stages.index('tcp-success-ack-dropped') < stages.index('execution-exited')
        assert stages.index('execution-exited') < stages.index('public-ack-lost') < stages.index('public-retry')
        record_property('recovery_boundaries', json.dumps(stages))
        record_property('supported_backend', 'scratch SQLite via storage.v1 TCP')
        record_property('same_attempt_id', launches[0])
        record_property('retained_tool_call_ids', json.dumps([row['toolCallId'] for row in retained]))
    finally:
        release.set()
        try:
            for process in processes:
                process.join(30)
                assert not process.is_alive(), 'fixture execution did not exit; no process was killed'
                process.close()
        finally:
            parent.close()
            child.close()


def _execute_until_killed(endpoint, task, claim_document, effect_path, channel):
    import signal
    from lib import storage as storage_module, turn_lifecycle as turns
    from lib.process_owner import process_owner
    from lib.durable_worker import WorkerExecutionContext, WorkerJobClaim
    from lib.storage.client import StorageClient

    client = StorageClient(*endpoint, timeout=30)
    turns._turn_client = lambda **kw: client
    storage_module.get_storage_client = lambda **kw: client
    context = WorkerExecutionContext(WorkerJobClaim.from_document(
        claim_document, expected_worker_id='fixture-killed-worker'))
    task['_conversationWorkerContext'] = context
    witness = process_owner()
    started = turns.mark_task_started(task['_attemptId'], task['id'], user_id=1,
        worker_claim=context.publication_claim(), worker_process=witness)
    assert started['status'] == 'running'
    saved = turns.get_turn(task['convId'], task['_turnId'], user_id=1)
    task['toolRounds'] = saved['projection']['toolRounds']
    with Path(effect_path).open('a') as effect:
        effect.write('synthetic execution once\n')
    task.update(status='running', content='Synthetic effect returned before abrupt exit')
    turns.record_task_event(task, {'type': 'delta', 'content': task['content']})
    channel.send(('checkpoint-committed', witness, task['toolRounds']))
    signal.pause()
    raise AssertionError('fixture child must be killed, not released')


def test_killed_checkpoint_child_recovers_once_without_replaying_retained_effects(
        tool_checkpoint_session, monkeypatch, tmp_path, record_property):
    import signal
    import time
    from lib.conversation_sync.command_service import ConversationTurnCommandService
    from lib.conversation_sync import runtime
    from lib.conversation_sync.worker_execution import execute_claimed_attempt, conversation_registration
    from lib.durable_worker import (
        StorageWorkerJobStore, SynchronousWorkerRunner, WorkerDomainSettlementUnknown,
        WorkerExecutionContext, WorkerJobClaim,
    )
    from tests.support.worker_job_time import wait_until_ms
    from lib.storage.client import StorageClient
    from lib.storage.generated_worker_job_contract import (
        WORKER_EXECUTION_CONFIG_KEY, WORKER_EXECUTION_EXIT_KEY,
    )
    from tests.conversation.test_worker_exit_recovery import _watch_recovered_resume

    s = tool_checkpoint_session
    before = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
    retained = [deepcopy(row) for row in before['projection']['toolRounds']
                if not row.get('_providerAttemptDiscarded')]
    assert [row['toolCallId'] for row in retained] == [f'durable-call-{n}' for n in range(3)]
    assert [row['llmRound'] for row in retained] == [1, 2, 3]
    commands = ConversationTurnCommandService(build_user_message=lambda *a: {},
        was_aborted_after=lambda *a: False)
    monkeypatch.setattr(runtime, 'conversation_turn_commands', commands)
    accepted = s.call('control_conversation', s.args)
    assert 'error' not in accepted, accepted
    attempt_id = accepted['attempt']['attemptId']
    task_id = 'conversation-attempt:' + attempt_id
    original = s.turns.get_attempt(before['currentAttemptId'], user_id=1)
    store = StorageWorkerJobStore(client_factory=lambda **kw: s.storage.client)
    document = store.claim_next(worker_id='fixture-killed-worker',
        task_kinds=('conversation-attempt',), now_ms=time.time_ns() // 1_000_000,
        lease_ms=10_000)
    context = WorkerExecutionContext(WorkerJobClaim.from_document(
        document, expected_worker_id='fixture-killed-worker'))
    assert context.claim.task_id == task_id
    task = dict(id=task_id, convId='session', _turnId=s.args['turn_id'],
                _attemptId=attempt_id, _userId=1, config={}, model='target-model',
                content='', thinking='')
    effect_path = tmp_path / 'killed-effect.txt'
    mp = multiprocessing.get_context('spawn')
    parent, child = mp.Pipe()
    process = mp.Process(target=_execute_until_killed,
        args=(s.storage.endpoint, task, document, str(effect_path), child))
    resumed, resumes = _watch_recovered_resume(monkeypatch)
    monkeypatch.setattr('lib.conversation_sync.task_start.start_conversation_attempt_executor',
                        lambda *a, **kw: pytest.fail('killed execution effects were replayed'))
    process.start()
    child.close()
    try:
        assert parent.poll(30), 'fixture child did not commit its checkpoint'
        kind, witness, child_rounds = parent.recv()
        assert kind == 'checkpoint-committed' and witness['pid'] == process.pid
        assert child_rounds == retained
        entered = s.turns.get_attempt(attempt_id, user_id=1, include_execution_config=True)
        assert entered['config'][WORKER_EXECUTION_CONFIG_KEY]['process'] == witness
        with pytest.raises(WorkerDomainSettlementUnknown, match='physical-exit'):
            execute_claimed_attempt(context, context.claim.payload)
        assert process.is_alive()
        process.kill()
        process.join(30)
        assert process.exitcode == -signal.SIGKILL
        # A fresh transport reads storage, not the dead child's task dictionary.
        reread = StorageClient(*s.storage.endpoint, timeout=30)
        try:
            with monkeypatch.context() as reads:
                reads.setattr(s.turns, '_turn_client', lambda **kw: reread)
                killed = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
                assert killed['status'] == 'running'
                assert killed['projection']['toolRounds'] == retained
                assert killed['projection']['content'] == 'Synthetic effect returned before abrupt exit'
                assert killed['currentAttemptId'] == attempt_id
                replay = s.call('control_conversation', s.args)
                assert replay['attempt']['attemptId'] == attempt_id
                wait_until_ms(document['leaseDeadlineMs'] + 1)
                runner = SynchronousWorkerRunner(worker_id='fixture-successor', store=store,
                    registrations=[conversation_registration()])
                outcome = runner.run_once()
                assert outcome.state == 'failed', outcome
                assert outcome.task_id == task_id
                job = reread.query('worker_job.get', {'task_id': task_id, 'user_id': 1})
                assert job['fencingToken'] > context.fencing_token
                assert job['error']['code'] == 'worker_effect_outcome_unknown'
                settled = s.turns.get_turn('session', s.args['turn_id'], user_id=1)
                assert (settled['status'], settled['settlement']['cause']) == (
                    'interrupted', 'server_restart')
                assert settled['projection']['toolRounds'] == retained
                assert settled['projection']['content'] == killed['projection']['content']
                assert settled['parentTurnId'] == before['parentTurnId']
                assert runner.run_once().state == 'idle'
                assert s.turns.get_turn('session', s.args['turn_id'], user_id=1) == settled
            result = reread.query('task_results.replay_get', {
                'key': task_id, 'user_id': 1, 'include_metadata': True})
            assert result['metadata']['usageComplete'] is False
            receipt = result['metadata'][WORKER_EXECUTION_EXIT_KEY]
            assert (receipt['proof'], receipt['settlement']) == ('process_dead', 'unknown')
            assert resumed.wait(30)
            assert resumes == [(1, 'session', s.args['turn_id'], attempt_id)]
            state = store.claim_state(context.claim, now_ms=time.time_ns() // 1_000_000)
            assert state == {'ok': False, 'error': 'stale_fence'}
            from lib.storage.errors import StorageError
            stale = dict(task, _conversationWorkerContext=context, status='running',
                         toolRounds=retained, content='stale publication must not land')
            s.turns.record_task_event(stale, {'type': 'delta', 'content': stale['content']})
            assert stale['aborted'] is True
            from lib.task_replay.task_result_checkpoint_contract import TASK_RESULT_CHECKPOINT_GUARD_CONTRACT
            record = reread.query('record.get', {'namespace': 'task_results', 'key': task_id})
            with pytest.raises(StorageError) as refusal:
                reread.command('task_results.checkpoint', {
                    'key': task_id, 'expected_version': record['version'],
                    'guard_contract': TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
                    'require_parent': True, 'worker_claim': context.publication_claim(),
                    'value': {**record['value'], 'content': 'stale result must not land'},
                }, 'fixture-stale-result')
            assert refusal.value.code == 'database_conflict'
            assert s.turns.get_turn('session', s.args['turn_id'], user_id=1) == settled
            assert store.claim_next(worker_id='fixture-successor',
                task_kinds=('conversation-attempt',), now_ms=time.time_ns() // 1_000_000,
                lease_ms=300_000) is None
            assert effect_path.read_text() == 'synthetic execution once\n'
            assert reread.query('task_results.replay_get', {
                'key': task_id, 'user_id': 1, 'include_metadata': True}) == result
            assert s.turns.get_attempt(before['currentAttemptId'], user_id=1) == original
            assert s.call('control_conversation', s.args)['attempt']['attemptId'] == attempt_id
            assert reread.query('worker_job.get', {'task_id': task_id, 'user_id': 1}) == job
            assert not s.started
            record_property('exit_proof', json.dumps({
                'pid': witness['pid'], 'exitcode': process.exitcode, 'receipt': receipt,
                'attemptId': attempt_id, 'retainedToolCallIds': [r['toolCallId'] for r in retained],
                'backend': 'scratch SQLite via storage.v1 TCP',
                'recovery': 'SynchronousWorkerRunner.run_once/conversation_registration', 'effectCount': 1}))
        finally:
            reread.close()
    finally:
        if process.is_alive():
            process.kill()
        process.join(30)
        assert not process.is_alive()
        process.close()
        parent.close()
        child.close()
