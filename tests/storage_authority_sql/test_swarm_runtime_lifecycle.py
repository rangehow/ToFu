"""Swarm tool admission and teardown against scratch SQLite and PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from uuid import uuid4

import pytest

from lib import agent_inbox
from lib.storage.errors import StorageError
from lib.swarm import integration, persistence
from lib.swarm.integration import _state
from lib.swarm.scheduler import StreamingScheduler
from lib.swarm.run_messages import RunMessageRepository, binding_for
from tests.storage_authority_sql.test_swarm_lifecycle_receipt import client as client
from tests.storage_authority_sql.test_native_checkpoint_handoff import accepted
from tests.swarm.test_swarm_async import _FakeAgent, _wait_until

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.unit


@pytest.fixture
def runtime(client, monkeypatch):
    monkeypatch.setattr(persistence, '_storage', lambda **kw: client)
    monkeypatch.setattr('lib.turn_lifecycle._turn_client', lambda **kw: client)
    monkeypatch.setattr(integration, 'SWARM_AUTOCONTINUE_ENABLED', False)
    submitted = []
    submit = StreamingScheduler._submit_ready

    def observe(scheduler, ready, **kwargs):
        submitted.extend(spec.id for spec in ready)
        return submit(scheduler, ready, **kwargs)

    monkeypatch.setattr(StreamingScheduler, '_submit_ready', observe)
    monkeypatch.setattr('lib.swarm.master._build_sub_agent', lambda spec, **kw: _FakeAgent(spec))
    bound = accepted(client.backend, target_task='new')
    task = dict(id='new', convId=bound['conversation_id'], _userId=7, status='running',
                config={'_turnId': bound['turn_id'], '_attemptId': bound['target_attempt_id']})
    try:
        yield client, task, submitted
    finally:
        key = task['convId']
        session = integration.get_active_session(key)
        if session is not None:
            session.abort()
            driver = getattr(session, '_driver_thread', None)
            if driver is not None:
                driver.join(10)
                assert not driver.is_alive()
            _state._remove_session(key, preserve_durable=True, expected=session)
        agent_inbox.reset_for_test(key)


def launch(task, agent='child'):
    return json.loads(integration.execute_swarm_tool('spawn_agents', {
        'agents': [{'id': agent, 'role': 'coder', 'objective': 'work ' + agent}],
    }, task=task))


def prior(client, task):
    key = task['convId']
    payload = dict(swarm_key=key, task_id='legacy', conv_id=key, status='terminated',
                   specs=[], config={'user_id': 7}, now_ms=1)
    assert client.command('swarm.session.save', payload, uuid4().hex)['saved']
    assert persistence.save_agent(key, 'child', run_id='legacy', role='coder', objective='prior',
        status='completed', messages=[], result={'final_answer': 'legacy evidence'}, required=True)
    return persistence.run_identity(key, {'task_id': 'legacy', 'conv_id': key, 'config': payload['config']})


def lifecycle(client, task):
    key = task['convId']
    header = client.query('swarm.session.get', {'swarm_key': key, 'include_agents': False})
    return persistence.run_identity(key, header)


@pytest.mark.parametrize('fault', ['archive', 'launch'])
def test_refused_replacement_never_submits_or_writes_old_checkpoint(runtime, monkeypatch, fault):
    client, task, submitted = runtime
    identity = prior(client, task)
    actual = client.command

    def refuse(operation, payload, command_id):
        if (fault == 'archive' and operation == 'record.put') or (
                fault == 'launch' and operation == 'swarm.session.save' and payload.get('save_mode') == 'launch'):
            raise StorageError('database_conflict', 'injected permanent refusal')
        return actual(operation, payload, command_id)

    monkeypatch.setattr(client, 'command', refuse)
    response = launch(task)
    assert response['code'] == 'swarm_durable_admission_failed', response
    assert submitted == []
    assert integration.get_active_session(task['convId']) is None
    row = persistence.load_session(task['convId'], required=True)
    if fault == 'archive':
        assert row['task_id'] == 'legacy'
        assert row['agents'][0]['result']['final_answer'] == 'legacy evidence'
        assert persistence.read_lifecycle(identity) is None
    else:
        assert row is None
        assert persistence.read_lifecycle(identity)['disposition'] == 'replacement'


@pytest.mark.parametrize('after_commit', ['none', 'stop', 'ordinary_retirement', 'replacement', 'attempt_stop'])
def test_lost_launch_ack_replays_one_command_but_not_historical_admission(runtime, monkeypatch, after_commit):
    client, task, submitted = runtime
    actual = client.command
    invocations, entered = [], []
    observed = {}
    run = _FakeAgent.run

    def observe_entry(agent):
        entered.append(agent.spec.id)
        return run(agent)

    monkeypatch.setattr(_FakeAgent, 'run', observe_entry)

    def lose_ack(operation, payload, command_id):
        result = actual(operation, payload, command_id)
        if operation != 'swarm.session.save' or payload.get('save_mode') != 'launch':
            return result
        invocations.append(command_id)
        if len(invocations) != 1:
            assert result == {'saved': True}  # Store replays the original success.
            return result
        identity = observed['identity'] = lifecycle(client, task)
        if after_commit == 'attempt_stop':
            settle_turn(client, task, 'interrupted')
        elif after_commit != 'none':
            assert persistence.dispose_run(identity, after_commit)
            if after_commit == 'replacement':
                actual(operation, {**payload, 'task_id': 'successor'}, uuid4().hex)
        raise StorageError('database_busy', 'commit succeeded but ACK was lost', retryable=True)

    monkeypatch.setattr(client, 'command', lose_ack)
    response = launch(task)
    assert len(invocations) == 2 and invocations[0] == invocations[1]
    if after_commit == 'none':
        assert response['status'] == 'async_launched', response
        assert submitted == ['child']
        assert _wait_until(lambda: integration.get_active_session(task['convId']).is_terminated)
        assert entered == ['child']
        assert persistence.read_lifecycle(observed['identity'])['disposition'] is None
    else:
        assert response['code'] == 'swarm_durable_admission_failed', response
        assert submitted == entered == []
        if after_commit == 'replacement':
            assert persistence.load_session(task['convId'])['task_id'] == 'successor'
        if after_commit in ('stop', 'ordinary_retirement', 'replacement'):
            assert persistence.read_lifecycle(observed['identity'])['disposition'] == after_commit


def test_resident_stop_between_commit_and_ack_does_not_enqueue(runtime, monkeypatch):
    client, task, submitted = runtime
    actual = client.command
    committed, release = threading.Event(), threading.Event()

    def hold_ack(operation, payload, command_id):
        result = actual(operation, payload, command_id)
        if operation == 'swarm.session.save' and payload.get('save_mode') == 'launch':
            committed.set()
            assert release.wait(10)
        return result

    monkeypatch.setattr(client, 'command', hold_ack)
    with ThreadPoolExecutor() as pool:
        waiting = pool.submit(launch, task)
        try:
            assert committed.wait(10)
            identity = lifecycle(client, task)
            assert integration.abort_swarm(identity['run_id'], user_id=7)['outcome'] == 'aborted'
        finally:
            release.set()
        assert waiting.result(timeout=10)['code'] == 'swarm_durable_admission_failed'
    assert submitted == []
    assert persistence.read_lifecycle(identity)['disposition'] == 'stop'


def test_legacy_valid_roster_and_stale_checkpoint_are_exact(runtime):
    client, task, submitted = runtime
    identity = prior(client, task)
    assert persistence.dispose_run(identity, 'replacement')
    response = launch(task)
    assert response['status'] == 'async_launched', response
    assert submitted == ['child']
    with pytest.raises(StorageError, match='exact current swarm run'):
        persistence.save_agent(task['convId'], 'child', run_id='legacy', role='coder',
            objective='stale', status='completed', messages=[], required=True)
    assert not persistence.mark_delivered(task['convId'], ['child'], task_id='legacy')
    assert persistence.read_lifecycle(identity)['disposition'] == 'replacement'


def test_disposed_mailbox_reads_closed_and_keeps_original_receipt(runtime):
    client, task, _ = runtime
    identity = prior(client, task)
    from lib.swarm.types import SubTaskSpec
    mailbox = RunMessageRepository(binding_for(task, SubTaskSpec(id='child', role='coder',
        objective='prior'), task['convId'], run_id='legacy'), client=lambda **kw: client)
    receipt = mailbox.submit(content='evidence', kind='reference', key='key', sender_task_id='new')
    assert persistence.dispose_run(identity, 'stop')
    value, _ = mailbox.load()
    assert value['closed'] and value['runDisposition'] == 'stop'
    assert value['messages'][0]['state'] == 'unconfirmed'
    replay = mailbox.submit(content='evidence', kind='reference', key='key', sender_task_id='new')
    assert replay['id'] == receipt['id']


@pytest.mark.parametrize('outcome', ['ack', 'refused', 'disposed_ack'])
def test_legacy_roster_adopts_same_run_before_enqueuing(runtime, monkeypatch, outcome):
    client, task, submitted = runtime
    from lib.swarm.master import MasterOrchestrator
    key = task['convId']
    parent_config = {'user_id': 7, 'parent_cfg': {}}
    assert client.command('swarm.session.save', dict(swarm_key=key, task_id='legacy',
        conv_id=key, specs=[], config=parent_config, now_ms=1), uuid4().hex)['saved']
    session = MasterOrchestrator(task_id='legacy', conv_id=key, user_id=7, specs=[], inbox_key=key)
    session._sql_run_identity = identity = persistence.run_identity(key, {
        'task_id': 'legacy', 'conv_id': key, 'config': parent_config})
    session._scheduler = session._build_scheduler()
    _state._set_session(key, session, task_id='legacy')
    actual = client.command

    def fault(operation, payload, command_id):
        if operation == 'swarm.session.save' and payload.get('save_mode') == 'roster':
            if outcome == 'refused':
                raise StorageError('database_conflict', 'roster rejected')
            result = actual(operation, payload, command_id)
            if outcome == 'disposed_ack':
                assert persistence.dispose_run(identity, 'replacement')
            return result
        return actual(operation, payload, command_id)

    monkeypatch.setattr(client, 'command', fault)
    try:
        response = launch(task, 'added')
        receipt = persistence.read_lifecycle(identity)
        if outcome == 'ack':
            assert response['status'] == 'async_launched' and response['is_followup'], response
            assert submitted == ['added']
            assert receipt['disposition'] is None
            assert [spec['id'] for spec in receipt['roster']] == ['added']
        else:
            assert response['code'] == 'swarm_durable_admission_failed', response
            assert submitted == []
            if outcome == 'refused':
                assert receipt is None
                assert persistence.load_session(key)['specs'] == []
            else:
                assert receipt['disposition'] == 'replacement'
    finally:
        session._scheduler.shutdown(require_quiescence=True)


@pytest.mark.parametrize('fault', ['none', 'lost_ack', 'stop_first', 'stop_after', 'cancel_first',
                                   'cancel_after', 'replacement_first', 'replacement_after',
                                   'stale_ack', 'wrong_owner', 'wrong_project', 'wrong_tenant', 'native',
                                   'wrong_accepted_scope', 'later_model', 'cold_retry', 'stale_revision',
                                   'job_cancel_first', 'job_cancel_after', 'activation_before_rebind',
                                   'activation_after_rebind', 'activation_before_rebind_terminated',
                                   'activation_before_rebind_stopped', 'activation_restore_ack_lost',
                                   'activation_storage_before_commit', 'activation_policy', 'activation_checkpoint_read',
                                   'activation_page_cut', 'activation_blocked_enqueue', 'partial_executor_refusal',
                                   'nested_only_refusal', 'late_dependent_refusal', 'dedup_empty', 'driverless_refusal', 'driver_start_failure'])
def test_accepted_sql_continue_rebind(runtime, monkeypatch, fault):
    client, task, submitted = runtime
    from lib.swarm.integration._rehydrate import resume_swarm_for_continue
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kw: client)
    attempt = client.query('turn.attempt.get', {
        'attempt_id': task['config']['_attemptId'], 'user_id': 7, 'include_execution_config': True})
    source = attempt['config']['_resumeSource']
    task['config']['_turnOperation'] = 'continue'
    key = task['convId']
    parent = {'_swarmParentTurnId': task['config']['_turnId'],
              '_swarmParentAttemptId': source['attemptId'], '_swarmParentTaskId': source['taskId'],
              '_swarmToolNames': []}
    if fault == 'wrong_accepted_scope':
        parent['_tenant_id'] = task['_tenant_id'] = 'foreign-tenant'
    run = 'frozen-original-run'
    specs = [{'id': 'child', 'role': 'coder', 'objective': 'recover'}]
    if fault in ('activation_blocked_enqueue', 'dedup_empty'):
        specs += [{'id': 'unsafe', 'role': 'coder', 'objective': 'recover' if fault == 'dedup_empty' else 'unsafe'},
                  {'id': 'blocked', 'role': 'coder', 'objective': 'blocked', 'depends_on': ['unsafe']}]
    if fault in ('partial_executor_refusal', 'nested_only_refusal', 'late_dependent_refusal'):
        specs += [{'id': 'sibling', 'role': 'coder', 'objective': 'sibling'},
                  {'id': 'dependent', 'role': 'coder', 'objective': 'dependent', 'depends_on': ['child']},
                  {'id': 'tail', 'role': 'coder', 'objective': 'tail', 'depends_on': ['dependent']}]
    if fault == 'driverless_refusal':
        specs.append({'id': 'reject', 'role': 'coder', 'objective': 'reject'})
    if fault == 'late_dependent_refusal':
        specs += [{'id': 'second-ready', 'role': 'coder', 'objective': 'second ready', 'depends_on': ['child']},
                  {'id': 'admitted-peer', 'role': 'coder', 'objective': 'admitted peer'}]
    persistence.save_session(key, task_id=run, conv_id=key,
        specs=specs,
        status='terminated' if fault.endswith('_terminated') else 'running',
        config={'user_id': 7, 'parent_cfg': parent, 'model': 'test-model',
                'thinking_enabled': False, 'project_path': '',
                'native_execution_required': fault == 'native'}, required=True, save_mode='launch')
    messages = [{'role': 'user', 'content': 'original objective'},
                {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'effect-1',
                 'type': 'function', 'function': {'name': 'read_files', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'effect-1', 'content': 'original receipt'}]
    persistence.save_agent(key, 'child', role='coder', objective='recover', status='running',
        messages=messages, result={'checkpoint_phase': 'tools_in_flight'}, rounds_used=2,
        run_id=run, required=True)
    if fault in ('activation_blocked_enqueue', 'dedup_empty'):
        persistence.save_agent(key, 'unsafe', role='coder', objective='unsafe', status='failed',
            messages=messages, result={'recovery_reason': 'restart_checkpoint_invalid'},
            rounds_used=2, run_id=run, required=True)
        persistence.save_agent(key, 'blocked', role='coder', objective='blocked', status='running',
            messages=messages, result={'checkpoint_phase': 'tools_in_flight'},
            rounds_used=2, run_id=run, required=True)
    before = persistence.load_session(key, required=True)
    expected_agents = before['agents']
    identity = persistence.run_identity(key, before)
    from lib.swarm.persistence_contract import SWARM_RUN_RESULT_FORMAT, SWARM_RUN_RESULT_NAMESPACE, swarm_run_result_key
    archive_payload = dict(namespace=SWARM_RUN_RESULT_NAMESPACE, key=swarm_run_result_key(7, run, 'child'),
        value=dict(format=SWARM_RUN_RESULT_FORMAT, ownerId=7, swarmRunId=run, agentId='child',
                   result={'final_answer': 'original archive'}))
    real_command = client.command
    real_command('record.put', archive_payload, uuid4().hex)
    if fault.startswith('job_cancel'):
        real_command('worker_job.enqueue', dict(task_id=task['id'], user_id=7, tenant_id='',
            task_kind='swarm-continue-test', idempotency_key='job-continue', payload={}, now_ms=1), uuid4().hex)
    archive_address = {k: archive_payload[k] for k in ('namespace', 'key')}
    archive_before = client.query('record.get', archive_address)
    assert archive_before is not None
    original_receipt = persistence.read_lifecycle(identity)
    restored = []
    monkeypatch.setattr(_FakeAgent, 'restore_checkpoint',
        lambda self, messages, rounds, result: restored.append((messages, rounds)), raising=False)
    model_probe = []
    if fault == 'later_model':
        from functools import partial
        from types import SimpleNamespace
        from lib.llm.llm_errors import ModelAdmissionRefused
        from lib.llm.request import dispatch_model_request
        from lib.swarm.agent import SubAgent

        def probe_forbidden_model(agent):
            plan = SimpleNamespace(physical_model_dispatch_guard=partial(
                SubAgent._admit_recovery_model_dispatch, agent))

            def first_send():
                model_probe.append('first mocked send')
                current = persistence.load_session(key, required=True, identity=identity)
                with pytest.raises(StorageError, match='no longer runnable'):
                    persistence.rebind_continue(identity, current, current['config'], task,
                                                activation_failed=True)
                settle_turn(client, task, 'interrupted')

            dispatch_model_request(plan, first_send)
            with pytest.raises(ModelAdmissionRefused) as raised:
                dispatch_model_request(plan, lambda: model_probe.append('forbidden retry'))
            assert isinstance(raised.value.__cause__, StorageError)
            model_probe.append('retry refused')
            return agent.result

        monkeypatch.setattr(_FakeAgent, 'run', probe_forbidden_model)
    monkeypatch.setattr('lib.tasks_pkg.persistence_wait.wait_for_persistence',
        lambda task, send, **kw: retry_once(send))
    calls = []

    def retry_once(send):
        try:
            return send()
        except TimeoutError:
            return send()

    def cancel():
        if fault.startswith(('stop', 'replacement')):
            disposition = fault.split('_')[0]
            assert persistence.dispose_run(identity, disposition)
            if disposition == 'replacement':
                persistence.save_session(key, task_id='replacement-run', conv_id=key,
                    specs=[], config={'user_id': 7}, required=True, save_mode='launch')
        elif fault.startswith('job_cancel'):
            result = real_command('worker_job.request_cancel', dict(task_id=task['id'], user_id=7,
                now_ms=2), uuid4().hex)
            assert result['accepted']
        else:
            settle_turn(client, task, 'interrupted')

    def command(operation, payload, command_id):
        if (fault in ('lost_ack', 'activation_restore_ack_lost') and operation == 'swarm.session.save'
                and payload.get('save_mode') == 'admit' and payload.get('stage') == 'restore'):
            result = real_command(operation, payload, command_id)
            if fault == 'activation_restore_ack_lost' and len(calls) == 1:
                raise StorageError('database_unavailable', 'injected lost restore ACK')
            with pytest.raises(StorageError, match='restore was already admitted'):
                real_command(operation, payload, uuid4().hex)
            return result
        if operation != 'swarm.session.save' or payload.get('save_mode') != 'continue_rebind':
            return real_command(operation, payload, command_id)
        if payload.get('activation_failed'):
            return real_command(operation, payload, command_id)
        calls.append(command_id)
        if fault.startswith('activation_before_rebind') and len(calls) == 1:
            raise RuntimeError('injected precommit activation failure')
        if fault == 'activation_storage_before_commit' and len(calls) == 1:
            raise StorageError('database_unavailable', 'injected precommit storage failure')
        if fault.endswith('_first'):
            cancel()
        if fault == 'stale_revision':
            real_command('swarm.session.save', dict(swarm_key=key, task_id=run, conv_id=key,
                save_mode='update', specs=before['specs'], config=before['config'], now_ms=0,
                expected_updated_at=before['updated_at']), uuid4().hex)
        result = real_command(operation, payload, command_id)
        assert persistence.load_session(key, required=True)['agents'] == expected_agents
        assert client.query('record.get', archive_address) == archive_before
        if fault == 'activation_after_rebind' and len(calls) == 1:
            raise RuntimeError('injected postcommit activation failure')
        if fault == 'cold_retry' and len(calls) == 1:
            raise StorageError('database_unavailable', 'lost ACK, process can no longer observe')
        if fault.endswith('_after'):
            cancel()
        if fault in ('lost_ack', 'stale_ack') and len(calls) == 1:
            if fault == 'stale_ack':
                cancel()
            raise TimeoutError('lost after commit')
        return result

    monkeypatch.setattr(client, 'command', command)
    if fault == 'wrong_owner':
        task['_userId'] = 8
    if fault == 'wrong_project':
        task['config']['projectPath'] = '/elsewhere'
    if fault == 'wrong_tenant':
        task['_tenant_id'] = 'wrong'
    if fault in ('partial_executor_refusal', 'nested_only_refusal', 'late_dependent_refusal'):
        from lib.swarm.scheduler import RecoverableAgentExecutor
        physical = []
        original_submit = RecoverableAgentExecutor.submit
        delayed = []
        driver_waiting = threading.Event()
        factory_entered, release_factory = threading.Event(), threading.Event()
        admitted_entered, release_admitted = threading.Event(), threading.Event()
        refusal_seen, release_driver = threading.Event(), threading.Event()
        blocked = {'dependent', 'tail'}
        if fault == 'late_dependent_refusal':
            blocked.add('second-ready')
        if fault != 'late_dependent_refusal':
            blocked.add('sibling')

        def submit(_executor, worker, spec):
            physical.append(spec.id)
            if spec.id in blocked:
                if fault == 'late_dependent_refusal' and spec.id == 'dependent':
                    assert driver_waiting.wait(3), 'driver did not return to its condition wait'
                raise RuntimeError('fixture executor refused')
            if fault == 'late_dependent_refusal' and spec.id in ('child', 'sibling', 'admitted-peer'):
                delayed.append((worker, spec))
            else:
                worker(spec)

        monkeypatch.setattr(RecoverableAgentExecutor, 'submit', submit)
        if fault == 'late_dependent_refusal':
            iterate = StreamingScheduler.iter_completions
            # The test deadline is shorter than polling: refusal must notify.
            def iterate_with_barrier(scheduler):
                try:
                    yield from iterate(scheduler, poll_interval=60)
                except RuntimeError:
                    refusal_seen.set()
                    assert release_driver.wait(5)
                    raise

            def factory(spec, **kwargs):
                if spec.id == 'sibling':
                    factory_entered.set()
                    assert release_factory.wait(5)
                return _FakeAgent(spec)

            ran = []

            def run_fixture_agent(agent):
                ran.append(agent.spec.id)
                if agent.spec.id == 'admitted-peer':
                    admitted_entered.set()
                    assert release_admitted.wait(5)
                    assert not session._parent_task_proxy.get('aborted')
                return agent.result

            monkeypatch.setattr(StreamingScheduler, 'iter_completions', iterate_with_barrier)
            monkeypatch.setattr('lib.swarm.master._build_sub_agent', factory)
            monkeypatch.setattr(_FakeAgent, 'run', run_fixture_agent)
            assert resume_swarm_for_continue(task, [])
            session = integration.get_active_session(key)
            assert physical == ['child', 'sibling', 'admitted-peer']
            assert not session.is_terminated
            condition = session._scheduler._running_drained
            wait_for = condition.wait_for

            def observe_wait(predicate, *args, **kwargs):
                if 'child' in session._scheduler._completed and not predicate():
                    driver_waiting.set()
                return wait_for(predicate, *args, **kwargs)

            monkeypatch.setattr(condition, 'wait_for', observe_wait)
            child, sibling, admitted = delayed
            with ThreadPoolExecutor(2) as peers:
                preparing = peers.submit(*sibling)
                entered = peers.submit(*admitted)
                try:
                    assert factory_entered.wait(3) and admitted_entered.wait(3)
                    child[0](child[1])
                    assert driver_waiting.is_set() and refusal_seen.wait(3)
                    release_factory.set()
                    preparing.result(3)
                    release_driver.set()
                    assert _wait_until(lambda: session.is_restart_suspended, timeout=3)
                    panel = _state.resident_swarm_status(key, user_id=7)
                    assert panel['active'] and not panel['awaitingContinue']
                    _accept_after_failed_activation(client, task, stopped=True)
                    during_drain = persistence.load_session(key, required=True)
                    during_receipt = persistence.read_lifecycle(identity)
                    draining_task = {**task, 'config': dict(task['config'])}
                    driver = session._driver_thread
                    join = driver.join

                    def cancel_wait(timeout=None):
                        assert driver.is_alive() and not entered.done()
                        draining_task['aborted'] = True
                        join(timeout)

                    with monkeypatch.context() as patch:
                        patch.setattr(driver, 'join', cancel_wait)
                        assert not resume_swarm_for_continue(draining_task, [])
                    assert draining_task['aborted']
                    assert persistence.load_session(key, required=True) == during_drain
                    assert persistence.read_lifecycle(identity) == during_receipt
                    assert physical == ['child', 'sibling', 'admitted-peer', 'dependent']
                finally:
                    release_factory.set()
                    release_driver.set()
                    release_admitted.set()
                entered.result(3)
            blocked.add('sibling')
            assert 'sibling' not in ran
            assert session._results_by_id['admitted-peer'][1].status == 'completed'
            session._driver_thread.join(3)
            assert not session._driver_thread.is_alive()
            assert session.is_terminated
            assert 'SwarmExecutorHandoffError' in session._driver_error
            assert session.is_restart_suspended
            assert session._scheduler.pending_count == session._scheduler.running_count == 0
            assert blocked.isdisjoint(session._scheduler._completed)
            assert blocked.isdisjoint(session._results_by_id)
            assert physical == ['child', 'sibling', 'admitted-peer', 'dependent']
        else:
            with pytest.raises(RuntimeError, match='executor refused'):
                resume_swarm_for_continue(task, [])
            assert physical == ['child', 'dependent']
        assert len(restored) == 1
        receipt = persistence.read_lifecycle(identity)
        assert receipt['worker_admitted'] and receipt['activation_failed'] is None
        after = persistence.load_session(key, required=True)
        assert after['status'] == 'running'
        if fault == 'late_dependent_refusal':
            retirement_dispatches = []
            monkeypatch.setattr(_state, '_start_retirement_worker_locked', retirement_dispatches.extend)
            # Model the scheduler's live reservation after its bounded driver
            # drain has returned. The same registry sweep precedes Continue.
            with session._scheduler._lock:
                session._scheduler._running['held-peer'] = object()
            try:
                with _state._sessions_lock:
                    monkeypatch.setitem(_state._session_timestamps, key, 0)
                    monkeypatch.setattr(integration, '_last_cleanup', 0)
                with pytest.raises(RuntimeError, match='still has live workers'):
                    resume_swarm_for_continue(task, [])
                assert integration.get_active_session(key) is session
                assert persistence.load_session(key, required=True) == after
                assert persistence.read_lifecycle(identity) == receipt
            finally:
                with session._scheduler._lock:
                    session._scheduler._running.pop('held-peer')
            with _state._sessions_lock:
                monkeypatch.setitem(_state._session_timestamps, key, 0)
                monkeypatch.setattr(integration, '_last_cleanup', 0)
            assert integration.get_active_session(key) is None
            assert retirement_dispatches == []
            assert persistence.load_session(key, required=True) == after
            assert persistence.read_lifecycle(identity) == receipt
        assert not blocked.intersection(row['agent_id'] for row in after['agents'])
        for agent in blocked:
            assert persistence.load_run_result(owner_id=7, swarm_run_id=run, agent_id=agent) is None
        if fault == 'late_dependent_refusal':
            monkeypatch.setattr(client, 'command', real_command)
            monkeypatch.setattr(RecoverableAgentExecutor, 'submit', original_submit)
            monkeypatch.setattr(StreamingScheduler, 'iter_completions', iterate)
            # The same durable accepted successor can reconcile once the real
            # peer and its driver have exited. No new Attempt is minted here.
            assert resume_swarm_for_continue(task, [])
            successor = persistence.read_lifecycle(identity)
            assert successor['checkpoint_writer_epoch'] == receipt['checkpoint_writer_epoch'] + 1
            assert successor['continue_parent']['attempt_id'] == task['config']['_attemptId']
        return
    if fault in ('driverless_refusal', 'driver_start_failure'):
        from lib.swarm.scheduler import RecoverableAgentExecutor

        entered, release, rejected = threading.Event(), threading.Event(), threading.Event()
        real_submit = RecoverableAgentExecutor.submit

        def run_fixture_agent(agent):
            entered.set()
            assert release.wait(5)
            return agent.result

        def submit(pool, worker, spec):
            if spec.id == 'reject':
                assert entered.wait(3)
                rejected.set()
                raise RuntimeError('fixture executor refused')
            return real_submit(pool, worker, spec)

        if fault == 'driver_start_failure':
            start = threading.Thread.start

            def fail_driver_start(thread):
                if thread.name == f'swarm-driver-{run}':
                    assert entered.wait(3)
                    rejected.set()
                    raise RuntimeError('fixture driver start failed')
                return start(thread)

            monkeypatch.setattr(threading.Thread, 'start', fail_driver_start)
        monkeypatch.setattr(_FakeAgent, 'run', run_fixture_agent)
        monkeypatch.setattr(RecoverableAgentExecutor, 'submit', submit)
        with ThreadPoolExecutor(2) as threads:
            activation = threads.submit(resume_swarm_for_continue, task, [])
            try:
                assert rejected.wait(3)
                session = integration.get_active_session(key)
                assert _wait_until(lambda: session.is_restart_suspended, timeout=3)
                assert session._driver_thread is None and session.holds_parent(task['id'])
                panel = _state.resident_swarm_status(key, user_id=7)
                assert panel['active'] and not panel['awaitingContinue']
                waiter = threads.submit(session.wait_terminated)
                assert not waiter.done()
            finally:
                release.set()
            with pytest.raises(RuntimeError, match='executor refused|driver start failed'):
                activation.result(3)
            signalled = session._terminated_event.is_set()
            if not signalled:  # Do not strand the test's own waiter on failure.
                session._terminated_event.set()
            waiter.result(3)
            assert signalled
        assert not session.holds_parent(task['id'])
        assert persistence.read_lifecycle(identity)['worker_admitted']
        assert persistence.load_run_result(owner_id=7, swarm_run_id=run, agent_id='reject') is None
        return
    if fault == 'dedup_empty':
        with pytest.raises(RuntimeError, match='no admitted specs'):
            resume_swarm_for_continue(task, [])
        assert submitted == restored == []
        assert not agent_inbox.peek(key)
        assert persistence.load_session(key, required=True)['agents'] == expected_agents
        assert client.query('record.get', archive_address) == archive_before
        receipt = persistence.read_lifecycle(identity)
        assert not receipt['worker_admitted']
        assert receipt['activation_failed']['attempt_id'] == task['config']['_attemptId']
        return
    if fault.startswith('activation_'):
        def reject_policy(*args, **kwargs):
            raise RuntimeError('injected early child-policy failure')

        with monkeypatch.context() as patch:
            if fault == 'activation_policy':
                patch.setattr('lib.tasks_pkg.child_invocation.snapshot_conversation_child_policy', reject_policy)
            if fault == 'activation_checkpoint_read':
                real_load = persistence.load_session

                def load(*args, **kwargs):
                    if kwargs.get('identity') is not None:
                        raise StorageError('database_unavailable', 'injected scoped checkpoint read failure')
                    return real_load(*args, **kwargs)

                patch.setattr(persistence, 'load_session', load)
            if fault == 'activation_page_cut':
                actual_query = client.query

                def advance_cut(operation, payload, *args, **kwargs):
                    result = actual_query(operation, payload, *args, **kwargs)
                    if operation == 'swarm.agents.page':
                        patch.setattr(client, 'query', actual_query)
                        persistence.save_agent(key, 'child', role='coder', objective='recover', status='running',
                            messages=messages, result={'checkpoint_phase': 'tools_in_flight'}, rounds_used=2,
                            run_id=run, required=True)
                    return result

                patch.setattr(client, 'query', advance_cut)
            if fault == 'activation_blocked_enqueue':
                patch.setattr('lib.swarm.scheduler.RecoverableAgentExecutor.submit', reject_policy)
            with pytest.raises((RuntimeError, StorageError), match='injected|checkpoint cut changed|executor refused'):
                resume_swarm_for_continue(task, [])
        if fault == 'activation_blocked_enqueue':
            assert submitted == ['child']  # Physical reservation refused before worker entry.
            assert restored == []
            assert not agent_inbox.peek(key)
            submitted.clear()
        else:
            assert submitted == []
        failed_receipt = persistence.read_lifecycle(identity)
        if fault in ('activation_policy', 'activation_storage_before_commit', 'activation_checkpoint_read'):
            assert persistence.load_session(key, required=True) == before
            assert client.query('record.get', archive_address) == archive_before
        if fault in ('activation_page_cut', 'activation_blocked_enqueue'):
            post_failure = persistence.load_session(key, required=True)
            assert post_failure['updated_at'] == before['updated_at'] + 1
            assert client.query('record.get', archive_address) == archive_before
            if fault == 'activation_blocked_enqueue':
                assert post_failure['agents'] == before['agents']
            else:
                expected_agents = post_failure['agents']
        if fault == 'activation_restore_ack_lost':
            assert failed_receipt['restore_claimed'] and not failed_receipt['worker_admitted']
            assert failed_receipt['activation_failed'] is None
        else:
            assert not resume_swarm_for_continue(task, [])
            assert failed_receipt['activation_failed']['attempt_id'] == task['config']['_attemptId']
        _accept_after_failed_activation(client, task, stopped=fault.endswith('_stopped'))
        assert resume_swarm_for_continue(task, [])
    elif fault in ('native', 'stop_first', 'cancel_first', 'replacement_first',
                 'wrong_accepted_scope', 'stale_revision', 'cold_retry', 'job_cancel_first', 'job_cancel_after'):
        with pytest.raises(RuntimeError):
            resume_swarm_for_continue(task, [])
        assert submitted == []
        if fault == 'cold_retry':
            assert resume_swarm_for_continue(task, [])
            assert len(calls) == 2 and calls[0] != calls[1]
    else:
        assert resume_swarm_for_continue(task, []) is (fault in ('none', 'lost_ack', 'later_model'))
    if fault in ('none', 'lost_ack', 'later_model', 'cold_retry') or fault.startswith('activation_'):
        assert _wait_until(lambda: bool(restored), timeout=10)
        assert submitted == ['child']
        assert restored[0][0][:2] == messages[:2]
        assert restored[0][0][2]['tool_call_id'] == 'effect-1'
        assert restored[0][0][2]['content'].startswith('original receipt')
        assert restored[0][1] == 3
        receipt = persistence.read_lifecycle(identity)
        assert receipt['parent'] == original_receipt['parent']
        assert receipt['continue_parent']['attempt_id'] == task['config']['_attemptId']
        # Archive equality is checked at rebind, before a legitimate new
        # writer can publish a new (unsealed) outcome at the live address.
        if fault == 'later_model':
            assert _wait_until(lambda: 'retry refused' in model_probe, timeout=10)
            assert model_probe == ['first mocked send', 'retry refused']
        if fault == 'lost_ack':
            assert len(calls) == 2 and calls[0] == calls[1]
    else:
        assert submitted == restored == []
        if fault.startswith('replacement'):
            assert persistence.load_session(key, required=True)['task_id'] == 'replacement-run'
        elif not fault.startswith('stop'):
            assert persistence.load_session(key, required=True)['agents'] == before['agents']
        if fault == 'stale_ack':
            assert len(calls) == 2 and calls[0] == calls[1]


@pytest.mark.parametrize('refusal_first', [True, False])
def test_preparation_settlement_orders_with_executor_refusal(refusal_first):
    from lib.swarm.protocol import SubAgentResult, SubTaskSpec

    published = []

    def publish(spec, outcome):
        # Refusal after the terminal claim, but before publication completes.
        with scheduler._lock:
            scheduler._recovery_submission_refused = True
        published.append((spec, outcome))

    scheduler = StreamingScheduler(agent_factory=_FakeAgent, on_agent_complete=publish)
    spec = SubTaskSpec(id='preparing', role='coder', objective='prepare')
    result = SubAgentResult(status='failed', error_message='fixture preparation failure')
    with scheduler._lock:
        scheduler._running[spec.id] = spec
        scheduler._recovery_submissions.add(id(spec))
        scheduler._recovery_submission_refused = refusal_first
    try:
        assert scheduler._record_completion(spec, result) == []
        assert published == ([] if refusal_first else [(spec, result)])
        assert scheduler.completed_count == (0 if refusal_first else 1)
        assert scheduler.running_count == 0
    finally:
        scheduler.shutdown()


def _accept_after_failed_activation(client, task, *, stopped=False):
    cfg = task['config']
    turn_id, conv = cfg['_turnId'], task['convId']

    def command(op, **payload):
        return client.command(op, dict(payload, user_id=7), uuid4().hex)

    status = 'interrupted' if stopped else 'failed'
    assert command('turn.event.record', attempt_id=cfg['_attemptId'], task_id=task['id'],
        terminal=True, status=status, event_type='aborted' if stopped else 'error',
        event_payload={}, projection={}, now=5,
        settlement={'outcome': status, 'cause': 'user_abort' if stopped else 'activation_failed', 'resumeOptions': [
            {'operation': 'continue', 'anchor': {'type': 'replay_only', 'contentChars': 0}}]})['applied']
    turn = client.query('turn.get', dict(conversation_id=conv, turn_id=turn_id, user_id=7))
    result = command('turn.attempt.create', conversation_id=conv, turn_id=turn_id,
        command_id='continue-after-failure', operation='continue',
        expected_projection_revision=turn['projectionRevision'], config={})
    successor = result['attempt']['attemptId']
    assert command('turn.attempt.claim', attempt_id=successor, dispatch_owner_id='test')
    command('turn.attempt.bind', attempt_id=successor, task_id='retry', dispatch_owner_id='test')
    assert command('turn.attempt.start', attempt_id=successor, task_id='retry')['status'] == 'running'
    task['id'] = 'retry'
    cfg['_attemptId'] = successor


def test_checkpoint_pages_do_not_bind_successor_rows_to_prior_header(runtime, monkeypatch):
    client, task, _ = runtime
    old = prior(client, task)
    actual = client.query
    switched = False

    def switch(operation, payload):
        nonlocal switched
        if operation == 'swarm.agents.page' and not switched:
            switched = True
            assert persistence.dispose_run(old, 'replacement')
            persistence.save_session(task['convId'], task_id='successor', conv_id=task['convId'],
                specs=[], config={'user_id': 7}, required=True, save_mode='launch')
            assert persistence.save_agent(task['convId'], 'child', run_id='successor', role='coder',
                objective='new', status='running', messages=[], required=True)
        return actual(operation, payload)

    monkeypatch.setattr(client, 'query', switch)
    with pytest.raises(StorageError, match='checkpoint cut changed'):
        persistence.load_session(task['convId'], required=True)
    assert persistence.load_session(task['convId'], required=True)['task_id'] == 'successor'


def test_missing_resident_is_not_authority_to_replace_running_legacy_run(runtime):
    client, task, submitted = runtime
    key = task['convId']
    persistence.save_session(key, task_id='legacy', conv_id=key, specs=[],
        config={'user_id': 7}, required=True)
    before = persistence.load_session(key, required=True)
    assert launch(task)['code'] == 'swarm_durable_admission_failed'
    assert submitted == []
    assert persistence.load_session(key, required=True) == before
    assert persistence.read_lifecycle(lifecycle(client, task)) is None


def settle_turn(client, task, status):
    result = client.command('turn.event.record', dict(user_id=7,
        attempt_id=task['config']['_attemptId'], task_id=task['id'], terminal=True,
        status=status, event_type='done' if status == 'completed' else 'aborted',
        event_payload={}, projection={}, settlement={'outcome': status,
            'cause': 'normal_stop' if status == 'completed' else 'user_abort'},
        now=3), uuid4().hex)
    assert result.get('applied'), result
    from lib.turn_lifecycle import get_turn
    turn = get_turn(task['convId'], task['config']['_turnId'], user_id=7, include_projection=False)
    assert turn['status'] == status
    assert turn['settlement']['cause'] == ('normal_stop' if status == 'completed' else 'user_abort')


def later_caller(client, original):
    conv = original['convId']
    settle_turn(client, original, 'completed')
    original['status'] = 'completed'

    def command(operation, **payload):
        return client.command(operation, dict(payload, user_id=7), uuid4().hex)

    pair = command('turn.create_pair', conversation_id=conv, command_id='later',
                   input_projection={'content': 'another question'}, config={})
    attempt = pair['attempt']['attemptId']
    assert command('turn.attempt.claim', attempt_id=attempt, dispatch_owner_id='test')
    command('turn.attempt.bind', attempt_id=attempt, task_id='later', dispatch_owner_id='test')
    assert command('turn.attempt.start', attempt_id=attempt, task_id='later')['status'] == 'running'
    return dict(id='later', convId=conv, _userId=7, status='running',
                config={'_turnId': pair['turn']['turnId'], '_attemptId': attempt})


@pytest.mark.parametrize('stage', ['enqueue', 'worker_start'])
@pytest.mark.parametrize('cause', ['turn_cancel', 'exact_stop'])
def test_cancel_wins_before_handoff(runtime, monkeypatch, stage, cause):
    from lib.swarm.scheduler import RecoverableAgentExecutor

    client, task, _ = runtime
    query, command = client.query, client.command
    submit, run = RecoverableAgentExecutor.submit, _FakeAgent.run
    physical, entered = [], []
    saved = False
    fired = threading.Event()
    worker_reads = 0

    def cancel(identity):
        fired.set()  # Reads used to prove cancellation must not re-enter the injector.
        if cause == 'turn_cancel':
            settle_turn(client, task, 'interrupted')
        else:
            assert persistence.dispose_run(identity, 'stop')

    def fault_command(operation, payload, command_id):
        nonlocal saved
        mode = payload.get('save_mode') if operation == 'swarm.session.save' else None
        if mode == 'admit' and payload['stage'] == stage:
            cancel(lifecycle(client, task))
        result = command(operation, payload, command_id)
        if mode == 'launch':
            saved = True
        return result

    def fault_query(operation, payload):
        nonlocal worker_reads
        result = query(operation, payload)
        # On the original candidate, inject after the stale Turn observation
        # during enqueue, or after the final exact-run observation in a worker.
        if saved and not fired.is_set():
            if stage == 'enqueue' and not physical:
                seam = 'turn.get' if cause == 'turn_cancel' else 'swarm.session.lifecycle.get'
                if operation == seam:
                    cancel(lifecycle(client, task))
            elif stage == 'worker_start' and physical and operation == 'swarm.session.lifecycle.get':
                worker_reads += 1
                if worker_reads == 2:
                    cancel(lifecycle(client, task))
        return result

    def observe_submit(executor, fn, spec):
        physical.append(spec.id)
        return submit(executor, fn, spec)

    def observe_run(agent):
        entered.append(agent.spec.id)
        return run(agent)

    monkeypatch.setattr(client, 'command', fault_command)
    monkeypatch.setattr(client, 'query', fault_query)
    monkeypatch.setattr(RecoverableAgentExecutor, 'submit', observe_submit)
    monkeypatch.setattr(_FakeAgent, 'run', observe_run)
    response = launch(task)
    if stage == 'enqueue':
        assert response.get('code') == 'swarm_durable_admission_failed', response
        assert physical == []
    else:
        assert response['status'] == 'async_launched', response
        session = integration.get_active_session(task['convId'])
        assert _wait_until(lambda: session.is_terminated, timeout=10)
        assert physical == ['child']
    assert fired.is_set()
    assert entered == []


@pytest.mark.parametrize('cancel_later', [True, False], ids=['actual-caller-stop', 'held-roster-ack'])
def test_followup_wave_admission_survives_idle_or_uses_actual_caller(runtime, monkeypatch, cancel_later):
    client, task, submitted = runtime
    command, run = client.command, _FakeAgent.run
    initial_started, finish_first = threading.Event(), threading.Event()
    roster_committed, release_ack = threading.Event(), threading.Event()
    driver_closing, allow_termination = threading.Event(), threading.Event()
    mark_terminated = persistence.mark_session_terminated
    later_submitted, allow_entry, entered = threading.Event(), threading.Event(), []
    mark = StreamingScheduler._mark_admitted

    def hold_entry(scheduler, spec):
        if spec.id == 'added':
            later_submitted.set()
            assert allow_entry.wait(10)
        return mark(scheduler, spec)

    def hold_run(agent):
        entered.append(agent.spec.id)
        if agent.spec.id == 'child':
            initial_started.set()
            assert finish_first.wait(10)
        return run(agent)

    def hold_ack(operation, payload, command_id):
        result = command(operation, payload, command_id)
        if operation == 'swarm.session.save' and payload.get('save_mode') == 'roster':
            roster_committed.set()
            assert release_ack.wait(10)
        return result

    def hold_termination(*args, **kwargs):
        # If the old driver closes the executor, preserve the original SQL
        # header until the tool finishes so the false ACK is observable.
        driver_closing.set()
        assert allow_termination.wait(10)
        return mark_terminated(*args, **kwargs)

    monkeypatch.setattr(persistence, 'mark_session_terminated', hold_termination)
    monkeypatch.setattr(_FakeAgent, 'run', hold_run)
    monkeypatch.setattr(StreamingScheduler, '_mark_admitted', hold_entry)
    monkeypatch.setattr(client, 'command', hold_ack)
    try:
        assert launch(task)['status'] == 'async_launched'
        assert initial_started.wait(10)
        caller = later_caller(client, task)
        session = integration.get_active_session(task['convId'])
        with ThreadPoolExecutor() as pool:
            followup = pool.submit(launch, caller, 'added')
            try:
                assert roster_committed.wait(10)
                if not cancel_later:
                    finish_first.set()
                    assert _wait_until(lambda: session._scheduler.is_idle, timeout=10)
                    # An idle driver must not finish while this committed
                    # wave has not yet received its ACK.
                    driver_closing.wait(.75)
                release_ack.set()
                response = followup.result(timeout=10)
                assert response['status'] == 'async_launched' and response['is_followup'], response
                assert later_submitted.wait(3), 'roster ACK did not yield physical execution'
                if cancel_later:
                    settle_turn(client, caller, 'interrupted')
            finally:
                release_ack.set()
                finish_first.set()
                allow_entry.set()
                allow_termination.set()
        assert _wait_until(lambda: session.is_terminated, timeout=10)
        assert submitted == ['child', 'added']
        assert entered == (['child'] if cancel_later else ['child', 'added'])
    finally:
        finish_first.set()
        release_ack.set()
        allow_entry.set()
        allow_termination.set()


def test_sequential_launches_and_retired_alias_cannot_stop_successor(runtime):
    _, task, submitted = runtime
    first = launch(task)
    key = task['convId']
    resident = integration.get_active_session(key)
    resident._driver_thread.join(10)
    assert resident.is_terminated
    original = resident._sql_run_identity
    # The fake agent does not write a resumable checkpoint. Supply one so
    # this also checks sealing of the real checkpoint archive at replacement.
    assert persistence.save_agent(key, 'child', run_id=original['run_id'], role='coder',
        objective='work child', status='completed', messages=[],
        result={'final_answer': 'original evidence'}, required=True)
    with _state._sessions_lock:
        _state._retire_terminal_session_locked(key)
    assert _state._key_aliases[task['id']] == key
    second = launch(task, 'second')
    assert second['status'] == 'async_launched', second
    assert second['swarm_run_id'] != first['swarm_run_id']
    current = integration.get_active_session(key)._sql_run_identity
    assert persistence.read_lifecycle(current)['parent']['task_id'] == task['id']
    assert submitted == ['child', 'second']
    assert persistence.read_lifecycle(original)['disposition'] == 'replacement'
    for stale in (task['id'], first['swarm_run_id']):
        assert integration.abort_swarm(stale, user_id=7)['outcome'] == 'not_found'
    assert persistence.load_session(key, required=True)['task_id'] == second['swarm_run_id']
    archive = persistence.load_run_result(owner_id=7, swarm_run_id=first['swarm_run_id'],
                                          agent_id='child')
    assert archive['sealedByTeardown'] and archive['result']['final_answer'] == 'original evidence'


def test_exact_stop_reconciles_lost_ack_and_lost_readback(runtime, monkeypatch):
    client, task, _ = runtime
    response = launch(task)
    run = response['swarm_run_id']
    resident = integration.get_active_session(task['convId'])
    resident._driver_thread.join(10)
    identity = resident._sql_run_identity
    command, query = client.command, client.query
    lost = False

    def lose_ack(operation, payload, command_id):
        nonlocal lost
        result = command(operation, payload, command_id)
        if operation == 'swarm.session.delete_exact' and payload['disposition'] == 'stop':
            assert not lost, 'committed Stop must not be submitted twice'
            lost = True
            raise StorageError('database_busy', 'lost ACK')
        return result

    def lose_read(operation, payload):
        if lost and operation == 'swarm.session.lifecycle.get':
            raise StorageError('database_busy', 'readback unavailable')
        return query(operation, payload)

    monkeypatch.setattr(client, 'command', lose_ack)
    monkeypatch.setattr(client, 'query', lose_read)
    assert integration.abort_swarm(run, user_id=7)['outcome'] == 'failed'
    monkeypatch.setattr(client, 'query', query)
    assert persistence.read_lifecycle(identity)['disposition'] == 'stop'
    successor = launch(task, 'second')
    assert successor['status'] == 'async_launched', successor
    # Model a cold Stop resolver with no process-local address. No restart,
    # worker recovery, scheduler re-entry or storage backend change is needed.
    with _state._sessions_lock:
        _state._stop_addresses.pop((7, run), None)
    assert integration.abort_swarm(run, user_id=8)['outcome'] == 'not_found'
    expected = {'success': True, 'outcome': 'aborted', 'task_id': run}
    assert integration.abort_swarm(run, user_id=7) == expected
    assert integration.abort_swarm(run, user_id=7) == expected
    assert persistence.load_session(task['convId'], required=True)['task_id'] == successor['swarm_run_id']


def test_pre_enqueue_interruption_reconciles_exact_orphan_after_delete_rollback(runtime, monkeypatch):
    client, task, submitted = runtime
    from lib.storage_authority_sql.operations_pkg import _runs
    command, delete_rows = client.command, _runs._swarm_session_delete_rows
    orphan = None

    def interrupt(operation, payload, command_id):
        nonlocal orphan
        result = command(operation, payload, command_id)
        if operation == 'swarm.session.save' and payload.get('save_mode') == 'launch' and orphan is None:
            orphan = lifecycle(client, task)
            raise StorageError('database_conflict', 'interrupted after durable launch')
        return result

    def rollback(*args):
        delete_rows(*args)
        raise StorageError('database_conflict', 'rollback exact retirement after deletion')

    monkeypatch.setattr(client, 'command', interrupt)
    monkeypatch.setattr(_runs, '_swarm_session_delete_rows', rollback)
    assert launch(task)['code'] == 'swarm_durable_admission_failed'
    assert submitted == []
    assert integration.get_active_session(task['convId']) is None
    assert lifecycle(client, task) == orphan
    assert persistence.read_lifecycle(orphan)['disposition'] is None
    monkeypatch.setattr(_runs, '_swarm_session_delete_rows', delete_rows)
    response = launch(task, 'fresh')
    assert response['status'] == 'async_launched', response
    assert response['swarm_run_id'] != orphan['run_id']
    assert submitted == ['fresh']
    assert persistence.read_lifecycle(orphan)['disposition'] == 'replacement'
    assert integration.abort_swarm(orphan['run_id'], user_id=7)['outcome'] == 'not_found'


def test_legacy_exact_task_id_can_stop_its_own_generation(runtime):
    client, task, _ = runtime
    from lib.swarm.master import MasterOrchestrator
    key = task['convId']
    identity = prior(client, task)
    resident = MasterOrchestrator(task_id='legacy', conv_id=key, user_id=7, specs=[], inbox_key=key)
    resident._sql_run_identity = identity
    _state._set_session(key, resident, task_id='legacy')
    assert integration.abort_swarm('legacy', user_id=8)['outcome'] == 'not_found'
    assert integration.abort_swarm('legacy', user_id=7)['outcome'] == 'aborted'
    assert persistence.read_lifecycle(identity)['disposition'] == 'stop'


@pytest.mark.parametrize('cause', ['turn', 'run'])
def test_handoff_commit_orders_a_cancellation_at_the_last_sql_observation(runtime, monkeypatch, cause):
    from concurrent.futures import TimeoutError as FutureTimeout
    from lib.storage_authority_sql.operations_pkg import _swarm_admission

    client, task, submitted = runtime
    key = task['convId']
    persistence.save_session(key, conv_id=key, task_id='probe-run', specs=[],
                             config={'user_id': 7}, required=True, save_mode='launch')
    identity = lifecycle(client, task)
    observed, release, cancellation_called = (threading.Event() for _ in range(3))
    read_receipt = _swarm_admission.lifecycle_row

    def hold_final_read(*args, **kwargs):
        receipt = read_receipt(*args, **kwargs)
        observed.set()
        assert release.wait(10)
        return receipt

    def admit():
        persistence.admit_handoff(identity, caller_task_id=task['id'],
            turn_id=task['config']['_turnId'], attempt_id=task['config']['_attemptId'],
            stage='worker_start')

    def cancel():
        cancellation_called.set()
        if cause == 'turn':
            settle_turn(client, task, 'interrupted')
        else:
            assert persistence.dispose_run(identity, 'stop')

    monkeypatch.setattr(_swarm_admission, 'lifecycle_row', hold_final_read)
    with ThreadPoolExecutor() as pool:
        admission = pool.submit(admit)
        try:
            assert observed.wait(10)
            cancellation = pool.submit(cancel)
            assert cancellation_called.wait(10)
            # Cancellation cannot commit between the final authority read and
            # the receipt commit. Releasing this transaction gives admission
            # the first decision, but never grants a subsequent handoff.
            with pytest.raises(FutureTimeout):
                cancellation.result(timeout=.2)
        finally:
            release.set()
        admission.result(timeout=10)
        cancellation.result(timeout=10)
    with pytest.raises(StorageError) as rejected:
        admit()
    assert rejected.value.code == 'database_conflict'
    assert submitted == []


def test_durable_roster_is_not_launch_ack_if_executor_refuses(runtime, monkeypatch):
    from lib.swarm.scheduler import RecoverableAgentExecutor

    client, task, submitted = runtime
    physical, entered = [], []

    def refuse(_executor, _fn, spec):
        physical.append(spec.id)
        raise RuntimeError('physical executor refused submit')

    def run(agent):
        entered.append(agent.spec.id)
        return agent.result

    monkeypatch.setattr(RecoverableAgentExecutor, 'submit', refuse)
    monkeypatch.setattr(_FakeAgent, 'run', run)
    response = launch(task)
    assert response.get('code') == 'swarm_launch_unknown', response
    assert response['submission_state'] == 'unknown'
    assert submitted == physical == ['child']
    assert entered == []
    session = integration.get_active_session(task['convId'])
    assert _wait_until(lambda: session.is_terminated, timeout=10)
    receipt = persistence.read_lifecycle(lifecycle(client, task))
    assert [spec['id'] for spec in receipt['roster']] == ['child']
