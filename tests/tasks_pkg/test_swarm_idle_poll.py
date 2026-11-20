"""A blocking swarm poll hands off to the existing owned settlement wake."""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from lib.agent_core.events import EventType, build_event
from tests.tasks_pkg.test_idle_wait_checkpoint_publication import _attempt

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._chat_sidecar',)


@pytest.mark.parametrize('scenario', [
    'handoff', 'failed_child', 'disabled', 'subset', 'optional', 'other_call', 'no_timeout',
    'new_input', 'no_human_chain', 'recovery_spent', 'wrong_lane', 'superseded', 'stopped',
    'replacement', 'no_conversation', 'plan', 'partial', 'dispatch_timeout',
])
def test_swarm_poll_handoff_requires_an_authorized_unsettled_frontier(chat_sidecar, monkeypatch, scenario):
    from lib import agent_inbox, turn_lifecycle as lifecycle
    from lib.storage.model_continuation import ModelContinuationStore
    from lib.swarm import integration, master, restart_recovery
    from lib.swarm.integration import _tools
    from lib.swarm.turn_handoff import prepare_swarm_turn_end, resume_swarm_after_turn
    from lib.swarm.types import SubAgentResult, SubTaskSpec
    from lib.tasks_pkg.conversation_delivery import mark_no_saved_conversation
    from lib.tasks_pkg.handlers import poll
    from lib.tasks_pkg.orchestrator._root_agent_loop import _RootLoopHooks
    from lib.tasks_pkg.orchestrator._tool_loop_breaker import finish_after_idle_swarm_wait
    from lib.tasks_pkg.wait_interruption import WaitInterruption

    conversation = f'swarm-wait-{uuid.uuid4().hex}'
    task, view = _attempt(conversation, 'Delegate and wait for the results.')
    lifecycle.mark_task_started(task['_attemptId'], task['id'], user_id=1)
    task['status'] = 'running'
    first = SubTaskSpec(id='first')
    second = SubTaskSpec(id='second', required_for_completion=scenario != 'optional')
    session = master.MasterOrchestrator(task_id=task['id'], conv_id=conversation, user_id=1,
                                        specs=[first, second], parent_task=task)
    session.on_settled = lambda: integration._maybe_autocontinue(conversation, 1, source_id=session.task_id)
    monkeypatch.setattr(integration, 'SWARM_AUTOCONTINUE_ENABLED', scenario != 'disabled')
    replacement = SimpleNamespace(user_id=1, conv_id=conversation, continuation_source='replacement')
    monkeypatch.setattr(integration, 'get_active_session', lambda _: replacement if scenario == 'replacement' else session)
    monkeypatch.setattr(_tools, '_get_session', lambda _: session)
    monkeypatch.setattr(_tools, 'add_session_alias', lambda *_: None)
    monkeypatch.setattr(poll, '_polls_workers', lambda *_: False)
    monkeypatch.setattr(poll, '_refuse_foreign_handles', lambda *_: None)
    monkeypatch.setattr(poll, '_route', lambda *_: ('swarm', None))
    monkeypatch.setattr(poll, '_finalize_tool_round', lambda *_: None)
    monkeypatch.setattr(WaitInterruption, '_read_durable', lambda *_: None)
    if scenario == 'no_conversation':
        mark_no_saved_conversation(task)
    if scenario == 'plan':
        monkeypatch.setattr('lib.scheduler.conversation_dispatch._owned_conversation_settings',
                            lambda *_: {'planMode': True})
    ids = ['first'] if scenario == 'subset' else ['first', 'second']

    # Use the real session wait with a clock that reaches the deadline without sleeping.
    await_agents = session.await_agents

    def await_with_clock(**kwargs):
        if scenario == 'new_input':
            agent_inbox.enqueue(conversation, 'Please change course', mode='user-steer')
        if scenario == 'no_timeout':
            session._terminated = True
        if scenario == 'stopped':
            task['aborted'] = True
        if scenario == 'partial':
            session._results_by_id[first.id] = (first, SubAgentResult(status='completed'))
        with monkeypatch.context() as clock:
            clock.setattr(master, 'time', SimpleNamespace(monotonic=iter([0, 3]).__next__))
            return await_agents(**kwargs)

    monkeypatch.setattr(session, 'await_agents', await_with_clock)
    if scenario in ('no_human_chain', 'recovery_spent', 'wrong_lane', 'superseded'):
        live = {'actor': 'assistant', 'status': 'running', 'turnId': task['_turnId'],
                'currentAttemptId': task['_attemptId']}
        turns = [{'actor': 'human', 'kind': 'chat_input', 'status': 'completed'}, live]
        if scenario == 'no_human_chain':
            turns = [live]
        if scenario == 'recovery_spent':
            turns.insert(1, {'actor': 'virtual_user', 'kind': 'swarm_autocontinue_input',
                             'status': 'completed', 'projection': {'_swarmFailureRecovery': True}})
        if scenario == 'wrong_lane':
            live['turnId'] = 'different-tail'
        if scenario == 'superseded':
            live['currentAttemptId'] = 'different-attempt'
        monkeypatch.setattr(restart_recovery, 'read_continuation_turns', lambda *_, **__: {'turns': turns})

    try:
        args = {'ids': ids, 'wait_s': 1, 'mode': 'all' if scenario == 'partial' else 'any'}
        call = {'id': 'wait', 'type': 'function',
                'function': {'name': 'poll', 'arguments': json.dumps(args)}}
        calls = [call]
        if scenario == 'other_call':
            calls.append({'id': 'read', 'function': {'name': 'read_files'}})
        message = {'role': 'assistant', 'content': '', 'tool_calls': calls}
        _, result, _ = poll._handle_poll(task, call, 'poll', 'wait', args, 0, {}, {}, None, False)
        state = SimpleNamespace(model=task['model'], exit_reason='running')
        if scenario not in ('handoff', 'failed_child'):
            assert not finish_after_idle_swarm_wait(
                task, state, message=message, round_num=0, tid='idle',
                dispatch_timed_out=scenario == 'dispatch_timeout')
            assert state.exit_reason == 'running' and '_toolLoopCleanFinish' not in task
            if scenario == 'partial':
                assert json.loads(result)['timed_out'] is True and json.loads(result)['completed']
            return

        assert json.loads(result)['timed_out'] is True
        hooks = SimpleNamespace(task=task, state=state, messages=view, request=SimpleNamespace(tid='idle'))
        directive = _RootLoopHooks.after_tools(hooks, 0, message, {})
        assert directive is not None and state.exit_reason == 'swarm_idle_wait'
        assert 'Waiting' in task['content']
        prepare_swarm_turn_end(task, user_id=1, finish_reason='stop')
        assert task['_waiting_on']['kind'] == 'swarm'
        assert task['_waiting_on']['totalAgents'] == 2
        task.update(status='done', messages=[*view, message,
                    {'role': 'tool', 'tool_call_id': 'wait', 'content': result}])
        assert lifecycle.record_task_event(task, build_event(EventType.DONE, finishReason='stop'))
        checkpoint = ModelContinuationStore().get(user_id=1, conversation_id=conversation, lane_id='main')
        assert checkpoint['messages'][-1] == {'role': 'assistant', 'content': task['content']}

        # A settlement racing the parent's commit is rechecked by the ordinary
        # handoff, and the real continuation gate sees the completed parent.
        wakes = []
        monkeypatch.setattr(integration, '_key_is_live', lambda _: False)
        monkeypatch.setattr('lib.scheduler.conversation_dispatch.dispatch_scheduled_turn',
                            lambda *_, **kw: (wakes.append(kw) or SimpleNamespace(disposition='busy')))
        for spec in (first, second):
            status = 'failed' if scenario == 'failed_child' and spec is second else 'completed'
            session._results_by_id[spec.id] = (spec, SubAgentResult(status=status))
            agent_inbox.enqueue(conversation, 'Worker finished', mode='swarm-update', agent_id=spec.id,
                                extra={'status': status, 'swarm_run_id': session.task_id})
        resume_swarm_after_turn(task, user_id=1)
        assert len(wakes) == 1
        assert wakes[0]['parent_turn_id'] == task['_turnId']
        assert wakes[0]['command_id'] == f'swarm-autocontinue:{session.task_id}'
    finally:
        agent_inbox.reset_for_test(conversation)
