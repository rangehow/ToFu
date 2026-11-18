"""An idle-wait Turn end settles a resumable model-replay checkpoint.

Event wakes (``finish_after_idle_coordinator_wait``) end a Coordinator's Turn
after a round whose every call was an empty Worker wait, with no model round
after the tool results. The working view then ends on a tool row; publication
refused it (``missing_final_model_message``), so the next wake restored an
older checkpoint plus a cold rebuild of every uncovered Turn and re-ran the
forced compaction the previous attempt had already done (knowledge lead,
2026-11-18: 1.0-1.6 M tokens force-compacted on every wake).
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from lib.agent_core.events import EventType, build_event

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._chat_sidecar',)

_MODEL = 'gpt-4o'
_POLL = {'id': 'call-poll', 'type': 'function',
         'function': {'name': 'poll', 'arguments': json.dumps({'wait_s': 1500})}}


def _idle_wait_round():
    return [{'role': 'assistant', 'content': '', 'tool_calls': [_POLL]},
            {'role': 'tool', 'tool_call_id': 'call-poll',
             'content': json.dumps({'wait': 'timeout', 'workers': [{'status': 'working'}]})}]


def _attempt(conversation, input_text):
    from lib import turn_lifecycle as lifecycle
    created = lifecycle.create_turn_pair(
        conversation, command_id=uuid.uuid4().hex, input_projection={'content': input_text},
        config={'model': _MODEL}, user_id=1,
        conversation_defaults={'allowCreate': True, 'title': 'idle wait'})
    task_id = f'task-{uuid.uuid4().hex}'
    lifecycle.bind_task(created['attempt']['attemptId'], task_id, user_id=1)
    config = {'model': _MODEL}
    # The attempt's model view, built exactly as a starting attempt builds it.
    view = lifecycle.build_api_messages(conversation, created['turn']['turnId'], config, user_id=1)
    return {'id': task_id, '_attemptId': created['attempt']['attemptId'],
            '_turnId': created['turn']['turnId'], '_userId': 1, 'convId': conversation,
            'status': 'done', 'content': '', 'thinking': '', 'finishReason': 'stop',
            'model': _MODEL, 'config': config}, view


def _end_on_idle_wait(task, messages):
    from lib import turn_lifecycle as lifecycle
    from lib.tasks_pkg.orchestrator._tool_loop_breaker import finish_after_idle_coordinator_wait
    task['messages'] = messages
    # The poll's own record: it timed out with nothing new and every pending
    # Worker's settlement resumes the Coordinator.
    task['_coordinatorIdleWaits'] = [{'call': 'call-poll'}]
    state = SimpleNamespace(model=_MODEL, exit_reason='running')
    assert finish_after_idle_coordinator_wait(
        task, state, message=messages[-2], round_num=0, tid='idle')
    assert state.exit_reason == 'coordinator_idle_wait'
    assert lifecycle.record_task_event(task, build_event(EventType.DONE, finishReason='stop'))


def _tokens(messages, conversation):
    from lib.tasks_pkg.compaction import _tokens as tokens
    task = {'model': _MODEL, 'convId': conversation, 'config': {'model': _MODEL}}
    threshold = tokens._compaction_trigger_threshold(task)[0]
    count, _method = tokens._count_tokens_authoritative(messages, task)
    return count, threshold, tokens._should_force_compact(messages, task)


def test_idle_wait_end_settles_a_checkpoint_the_next_wake_resumes(chat_sidecar):
    from lib import turn_lifecycle as lifecycle
    from lib.storage.model_continuation import ModelContinuationStore, settled_witness
    from lib.tasks_pkg.orchestrator._tool_loop_breaker import IDLE_COORDINATOR_WAIT_TEXT
    conversation = f'idle-wait-{uuid.uuid4().hex}'
    task, view = _attempt(conversation, 'Delegate the index rebuild and wait for it.')
    _end_on_idle_wait(task, [*view, *_idle_wait_round()])
    assert task['_modelReplayPublishStatus'] == 'stored'
    settled = lifecycle.get_turn(conversation, task['_turnId'], user_id=1)
    assert settled['status'] == 'completed'
    assert settled['projection']['content'] == IDLE_COORDINATOR_WAIT_TEXT
    checkpoint = ModelContinuationStore().get(user_id=1, conversation_id=conversation,
                                              lane_id='main')
    assert checkpoint['turns'][-1] == settled_witness(settled)
    # The tool batch is closed by the Turn's answer, exactly as a cold rebuild
    # renders a Turn whose final content follows its tool rounds.
    assert checkpoint['messages'][-3:] == [
        *_idle_wait_round(), {'role': 'assistant', 'content': IDLE_COORDINATOR_WAIT_TEXT}]

    wake, _ = _attempt(conversation, 'Continue the request: a Worker settled.')
    assert wake['config']['_modelReplayStatus'] == 'restored'


def test_next_wake_reuses_the_compaction_instead_of_compacting_again(chat_sidecar):
    from lib.tasks_pkg.compaction._handoff import make_compaction_handoff
    conversation = f'idle-wait-compacted-{uuid.uuid4().hex}'
    probe = {'model': _MODEL, 'convId': conversation, 'config': {'model': _MODEL}}
    from lib.tasks_pkg.compaction._tokens import _compaction_trigger_threshold
    huge_input = 'evidence ' * (_compaction_trigger_threshold(probe)[0] + 1000)
    task, view = _attempt(conversation, huge_input)
    tokens_before, threshold, forced = _tokens(view, conversation)
    assert forced and tokens_before > threshold, (tokens_before, threshold)
    # The attempt force-compacted its view, then ended on an idle wait.
    compacted = [make_compaction_handoff('The lead delegated the index rebuild.', phase='pre_turn'),
                 *_idle_wait_round()]
    _end_on_idle_wait(task, compacted)
    assert task['_modelReplayPublishStatus'] == 'stored'

    wake, wake_view = _attempt(conversation, 'Continue the request: a Worker settled.')
    assert wake['config']['_modelReplayStatus'] == 'restored'
    tokens_after, _, forced_again = _tokens(wake_view, conversation)
    assert not forced_again and tokens_after < threshold, (tokens_after, threshold)
    assert not any(huge_input in str(message.get('content')) for message in wake_view)
    assert wake_view[:len(compacted)] == compacted


def test_an_unanswered_tool_call_still_refuses_publication():
    from lib.tasks_pkg.model_replay import prepare_completed_task
    assistant, result = _idle_wait_round()
    assistant = {**assistant, 'tool_calls': [_POLL, {**_POLL, 'id': 'call-other'}]}
    task = {'convId': 'c', '_turnId': 't', '_userId': 1, 'model': _MODEL,
            'content': 'Waiting.', 'config': {'model': _MODEL, '_modelReplayBase': []},
            '_toolLoopCleanFinish': {'reason': 'coordinator_idle_wait', 'round': 1},
            'messages': [{'role': 'user', 'content': 'wait'}, assistant, result]}
    assert prepare_completed_task(task) is None
    assert task['_modelReplayPublishStatus'] == 'missing_final_model_message'
