"""OpenAI-compatible responses name the model that actually answered.

OpenAI defines ``chat.completion.model`` / ``chat.completion.chunk.model`` as
the model used for the completion. A real ``run_task`` drives the configured
fallback through a deterministic transport stub that records each dispatched
``body['model']``; the paid ``apiRounds`` ledger written by the fallback layer is
the attribution source. The requested model stays visible in ``tofu``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

import lib.tasks_pkg.llm_fallback._transport as _fallback_transport

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

REQUESTED = 'requested-model'
FALLBACK = 'fallback-model'
ANSWER = 'deterministic answer'
USAGE = {'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13}


def _install(monkeypatch, *, primary_fails, answer_usage=USAGE, rescue=False):
    from lib import llm
    from lib.agent_core.events import EventType, build_event
    from lib.tasks_pkg.llm_fallback import _retry
    from lib.tasks_pkg.manager._events import append_event

    dispatched = []

    def stream(task, body, *args, **kwargs):
        model = 'rescue-model' if rescue and kwargs.get('pool_wide') else body['model']
        dispatched.append(model)
        if primary_fails and (model == REQUESTED or (rescue and model == FALLBACK)):
            raise llm.PermissionError_('primary unavailable')
        append_event(task, build_event(EventType.DELTA, thinking=f'thinking on {model}'))
        with task['content_lock']:
            task['content'] += ANSWER
        append_event(task, build_event(EventType.DELTA, content=ANSWER))
        usage = dict(answer_usage)
        if rescue and kwargs.get('pool_wide'):
            usage['_dispatch'] = {'model': model}
        return ({'role': 'assistant', 'content': ANSWER, 'tool_calls': []},
                'stop', usage)

    monkeypatch.setattr(_fallback_transport, 'stream_llm_response', stream)
    monkeypatch.setattr(_retry, '_get_fallback_model', lambda _task=None: FALLBACK)
    if rescue:
        from lib.llm_dispatch.factory import get_dispatcher
        monkeypatch.setattr(type(get_dispatcher()), 'has_capable_slots',
                            lambda *args, **kwargs: True)
        monkeypatch.setattr(_retry, '_get_pool_rescue_model', lambda failed: 'rescue-model')
    return dispatched


def _run(monkeypatch, conv_id, **kwargs):
    from lib.tasks_pkg.manager import create_task
    from lib.tasks_pkg.orchestrator.api import run_task
    from tests._seed import delete_conversation, seed_conversation

    dispatched = _install(monkeypatch, **kwargs)
    seed_conversation(conv_id, messages=[
        {'role': 'user', 'content': 'answer', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [],
         'timestamp': 2},
    ], title='compat-attribution')
    task = create_task(
        conv_id, [{'role': 'user', 'content': 'answer'}],
        {'model': REQUESTED, 'projectEnabled': False, 'webSearchEnabled': False},
        user_id=1,
    )
    try:
        run_task(task)
    finally:
        try:
            delete_conversation(conv_id)
        except Exception:
            pass
    return task, dispatched


def _stream(task):
    from lib.compat.openai import stream_openai_chunks

    async def collect():
        return [frame async for frame in stream_openai_chunks(task, model=REQUESTED)]
    frames = asyncio.new_event_loop().run_until_complete(collect())
    return [json.loads(frame[6:]) for frame in frames
            if frame.startswith('data: {')]


def test_fallback_answer_reports_actual_model_sync_and_stream(monkeypatch):
    from lib.compat.openai import build_openai_response

    task, dispatched = _run(monkeypatch, 'cv-attr-fb-' + str(os.getpid()),
                            primary_fails=True)
    assert dispatched == [REQUESTED, FALLBACK]
    assert [(row['model'], row['tag']) for row in task['apiRounds']] == [
        (FALLBACK, 'R1-FALLBACK')]

    body = build_openai_response(task, model=REQUESTED)
    assert body['model'] == FALLBACK
    assert body['choices'][0]['message']['content'] == ANSWER
    assert body['tofu'] == {'requested_model': REQUESTED,
                            'served_models': [FALLBACK]}

    chunks = _stream(task)
    assert all('error' not in chunk for chunk in chunks)
    assert {chunk['model'] for chunk in chunks} == {FALLBACK}
    assert chunks[-1]['tofu'] == {'requested_model': REQUESTED,
                                  'served_models': [FALLBACK]}
    assert ANSWER in ''.join(
        chunk['choices'][0]['delta'].get('content') or '' for chunk in chunks)


def test_no_fallback_response_is_unchanged(monkeypatch):
    from lib.compat.openai import build_openai_response

    task, dispatched = _run(monkeypatch, 'cv-attr-primary-' + str(os.getpid()),
                            primary_fails=False)
    assert dispatched == [REQUESTED]

    body = build_openai_response(task, model=REQUESTED)
    assert body['model'] == REQUESTED
    assert 'tofu' not in body
    chunks = _stream(task)
    assert {chunk['model'] for chunk in chunks} == {REQUESTED}
    assert all('tofu' not in chunk and 'error' not in chunk for chunk in chunks)


def test_fallback_without_ledger_evidence_fails_explicitly(monkeypatch):
    from lib.compat._common import CompatModelAttributionUnknown
    from lib.compat.openai import build_openai_response

    task, dispatched = _run(monkeypatch, 'cv-attr-unknown-' + str(os.getpid()),
                            primary_fails=True)
    assert dispatched == [REQUESTED, FALLBACK]
    # The fallback layer always writes a ledger row (even for empty provider
    # usage). Remove it to model a task whose answering row is unavailable
    # while the MODEL_FALLBACK event and task stamp remain.
    task['apiRounds'] = []
    for event in task['events']:
        if event.get('type') == 'done':
            event.pop('apiRounds', None)

    with pytest.raises(CompatModelAttributionUnknown):
        build_openai_response(task, model=REQUESTED)
    chunks = _stream(task)
    assert chunks[-1]['error']['code'] == 'model_attribution_unknown'
    assert not any(chunk['choices'][0].get('finish_reason')
                   or chunk['choices'][0]['delta'].get('content')
                   for chunk in chunks if chunk.get('choices'))


def test_ledger_attribution_uses_last_adopted_answer_row():
    from lib.compat._common import responding_models

    task = {'id': 'task-a', 'events': [], 'apiRounds': [
        {'round': 1, 'model': REQUESTED, 'tag': 'R1-REFUSED',
         'responseAuthoring': False},
        {'round': 1, 'model': FALLBACK, 'tag': 'R1-FALLBACK'},
        {'round': 2, 'model': 'discarded', 'tag': 'R2-DISCARDED'},
        {'round': 2, 'model': REQUESTED, 'tag': 'R2'},
        {'round': 3, 'model': 'child-model', 'taskId': 'child', 'sourceRound': 1},
        {'round': 'fallback', 'model': 'synthesis-model', 'tag': 'FALLBACK'},
        {'round': 'compaction-1', 'kind': 'compaction', 'model': 'compactor'},
    ]}
    assert responding_models(task, REQUESTED) == (
        'synthesis-model', [FALLBACK, REQUESTED, 'synthesis-model'])


def test_no_ledger_and_no_switch_keeps_requested_model():
    from lib.compat._common import responding_models

    assert responding_models({'id': 't', 'events': []}, REQUESTED) == (
        REQUESTED, [REQUESTED])


@pytest.mark.parametrize('models', [[FALLBACK], [REQUESTED, FALLBACK], [REQUESTED]])
def test_unowned_reasoning_is_never_relabelled(monkeypatch, models):
    """Rescue announces itself AFTER reasoning; the ledger cannot split deltas."""
    from lib.compat.openai import build_openai_response

    task, _ = _run(monkeypatch, 'cv-attr-unowned-' + str(os.getpid()),
                   primary_fails=False)
    task['apiRounds'] = [
        {'round': i + 1, 'model': name, 'tag': f'R{i + 1}'}
        for i, name in enumerate(models)]
    # Deterministic replay of the ordering from pool rescue, including mixed
    # primary/tool-round reasoning before a different model's final answer.
    done = next(ev for ev in task['events'] if ev.get('type') == 'done')
    done['apiRounds'] = task['apiRounds']
    task['events'] = [
        {'type': 'delta', 'thinking': f'private reasoning {name}'}
        for name in models]
    if models[-1] != REQUESTED:
        task['events'].append({'type': 'model_fallback',
                               'fallbackModel': models[-1]})
    task['events'].append(done)

    body = build_openai_response(task, model=REQUESTED)
    assert body['model'] == models[-1]
    assert 'reasoning_content' not in body['choices'][0]['message']
    chunks = _stream(task)
    assert {chunk['model'] for chunk in chunks} == {models[-1]}
    assert not any('reasoning_content' in chunk['choices'][0]['delta']
                   for chunk in chunks)
    assert ANSWER in ''.join(chunk['choices'][0]['delta'].get('content', '')
                             for chunk in chunks)


@pytest.mark.parametrize('row', [
    {'model': 'refused', 'responseAuthoring': False},
    {'model': 'refused', 'tag': 'R2-REFUSED'},
    {'model': 'discarded', 'tag': 'R2-DISCARDED'},
    {'model': 'compactor', 'kind': 'compaction'},
    {'model': 'child', 'taskId': 'child'},
    {'model': 'transfer', 'sourceRound': 1},
])
def test_billing_rows_do_not_author_response(row):
    from lib.compat._common import responding_models

    task = {'id': 'root', 'apiRounds': [
        {'model': FALLBACK, 'round': 1}, row]}
    assert responding_models(task, REQUESTED) == (FALLBACK, [FALLBACK])


def test_real_pool_rescue_streams_before_switch_but_never_mislabels(monkeypatch):
    from lib.compat.openai import build_openai_response

    task, dispatched = _run(monkeypatch, 'cv-attr-rescue-' + str(os.getpid()),
                            primary_fails=True, rescue=True)
    assert dispatched == [REQUESTED, FALLBACK, 'rescue-model']
    events = task['events']
    reasoning_index = next(i for i, ev in enumerate(events)
                           if ev.get('thinking') == 'thinking on rescue-model')
    rescue_index = next(i for i, ev in enumerate(events)
                        if ev.get('type') == 'model_fallback'
                        and ev.get('fallbackModel') == 'rescue-model')
    assert reasoning_index < rescue_index
    assert build_openai_response(task, REQUESTED)['model'] == 'rescue-model'
    chunks = _stream(task)
    assert {chunk['model'] for chunk in chunks} == {'rescue-model'}
    assert not any(chunk['choices'][0]['delta'].get('reasoning_content')
                   for chunk in chunks)


def test_withheld_reasoning_keeps_heartbeat_and_disconnect_cleanup(monkeypatch):
    from lib.agent_core import admission
    from lib.compat.openai import stream_openai_chunks

    task = {'id': 'withheld-reasoning', 'status': 'running', 'events': [
        {'type': 'delta', 'thinking': 'unattributed reasoning'}]}
    waits, cleaned = [], []

    async def wait(task_id, timeout):
        waits.append(task_id)
        return False

    monkeypatch.setattr(admission, 'wait_for_event', wait)
    monkeypatch.setattr(admission, 'unregister_waiter', cleaned.append)

    async def consume():
        stream = stream_openai_chunks(task, REQUESTED)
        assert await anext(stream) == ': heartbeat\n\n'
        await stream.aclose()

    asyncio.run(consume())
    assert waits == ['withheld-reasoning']
    assert cleaned == ['withheld-reasoning']


def _anthropic_stream(task):
    from lib.compat.anthropic import stream_anthropic_chunks

    async def collect():
        return [frame async for frame in stream_anthropic_chunks(task, REQUESTED)]
    return [json.loads(frame.split('data: ', 1)[1])
            for frame in asyncio.run(collect()) if 'data: ' in frame]


@pytest.mark.parametrize('primary_fails,rescue', [(False, False), (True, False), (True, True)])
def test_anthropic_actual_dispatch_attribution(monkeypatch, primary_fails, rescue):
    from lib.compat.anthropic import build_anthropic_response

    task, dispatched = _run(monkeypatch, 'cv-ant-attr-' + str(os.getpid()),
                            primary_fails=primary_fails, rescue=rescue)
    body = build_anthropic_response(task, REQUESTED)
    assert body['model'] == dispatched[-1]
    assert body['content'] == [{'type': 'text', 'text': ANSWER}]
    frames = _anthropic_stream(task)
    assert frames[0]['type'] == 'message_start'
    assert frames[0]['message']['model'] == dispatched[-1]
    assert frames[-1]['type'] == 'message_stop'
    assert not any('thinking' in json.dumps(frame) for frame in frames)


@pytest.mark.parametrize('models', [[REQUESTED], [REQUESTED, FALLBACK]])
def test_anthropic_mixed_reasoning_and_tool_calls(models):
    from lib.compat.anthropic import build_anthropic_response

    task = {'id': 'ant-tools', 'status': 'done', 'finishReason': 'tool_use',
            'content': '', 'thinking': 'private mixed reasoning',
            'apiRounds': [{'round': i + 1, 'model': model} for i, model in enumerate(models)],
            'events': [{'type': 'delta', 'thinking': 'private mixed reasoning'},
                       {'type': 'done', 'finishReason': 'tool_use'}],
            'toolRounds': [{'tool_calls': [{'id': 'call-1', 'function': {
                'name': 'weather', 'arguments': '{"city":"SF"}'}}]}]}
    body = build_anthropic_response(task, REQUESTED)
    assert body['model'] == models[-1]
    assert body['content'] == [{'type': 'tool_use', 'id': 'call-1',
                               'name': 'weather', 'input': {'city': 'SF'}}]
    frames = _anthropic_stream(task)
    assert frames[0]['message']['model'] == models[-1]
    assert frames[-2]['delta']['stop_reason'] == 'tool_use'
    assert any(frame.get('delta', {}).get('partial_json') == '{"city": "SF"}'
               for frame in frames)
    assert 'private mixed reasoning' not in json.dumps(frames)


def test_anthropic_unknown_emits_only_error():
    from lib.compat._common import CompatModelAttributionUnknown
    from lib.compat.anthropic import build_anthropic_response

    task = {'id': 'ant-unknown', 'status': 'done', 'finishReason': 'stop',
            'content': ANSWER, '_fallback_model': FALLBACK, 'events': []}
    with pytest.raises(CompatModelAttributionUnknown):
        build_anthropic_response(task, REQUESTED)
    frames = _anthropic_stream(task)
    assert len(frames) == 1
    assert frames[0]['error']['code'] == 'model_attribution_unknown'


@pytest.mark.parametrize('cancel', [False, True])
def test_anthropic_waiting_heartbeat_and_cancellation_cleanup(monkeypatch, cancel):
    from lib.agent_core import admission
    from lib.compat.anthropic import stream_anthropic_chunks

    task = {'id': 'ant-wait', 'status': 'running', 'events': [
        {'type': 'delta', 'thinking': 'unattributed'}]}
    cleaned = []

    async def wait(task_id, timeout):
        assert task_id == task['id']
        if cancel:
            raise asyncio.CancelledError()
        return False

    monkeypatch.setattr(admission, 'wait_for_event', wait)
    monkeypatch.setattr(admission, 'unregister_waiter', cleaned.append)

    async def consume():
        stream = stream_anthropic_chunks(task, REQUESTED)
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await anext(stream)
        else:
            assert await anext(stream) == ': heartbeat\n\n'
            await stream.aclose()

    asyncio.run(consume())
    assert cleaned == [task['id']]
