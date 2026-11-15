"""Provider context overflow compacts and retries the model that rejected it.

``_llm_call_with_fallback`` owns one reactive retry loop for the primary model
and one for the configured fallback: a ``PromptTooLongError`` shrinks the
derived payload and retries the same model before ordinary fallback policy.
"""

from __future__ import annotations

import lib.llm as _fallback_llm
import lib.tasks_pkg.llm_fallback._retry as _fallback_retry
import lib.tasks_pkg.llm_fallback._state as _fallback_state
import lib.tasks_pkg.llm_fallback._transport as _fallback_transport
import lib.tasks_pkg.llm_fallback._usage as _fallback_usage
import lib.tasks_pkg.manager._events as _fallback_events

import threading

import pytest


pytestmark = pytest.mark.unit


def _task(task_id: str) -> dict:
    return {
        'id': task_id,
        'convId': f'conv-{task_id}',
        '_userId': 1,
        'config': {},
        'content': '',
        'thinking': '',
        'events': [],
        'events_lock': threading.Lock(),
    }


def _patch_recovery_runtime(monkeypatch, stream_impl, compact_impl):
    import lib.tasks_pkg.compaction.api as compaction_api
    import lib.tasks_pkg.llm_fallback._call as fallback

    events = []
    monkeypatch.setattr(_fallback_transport, 'stream_llm_response', stream_impl)
    monkeypatch.setattr(
        _fallback_retry, '_get_fallback_model', lambda _task: 'fallback-model')
    monkeypatch.setattr(_fallback_usage, '_emit_round_usage', lambda *a, **kw: None)
    monkeypatch.setattr(
        _fallback_events, 'append_event', lambda _task, event: events.append(event))
    monkeypatch.setattr(compaction_api, 'reactive_compact', compact_impl)

    def _build_body(model, messages, **kwargs):
        return {
            'model': model,
            'messages': list(messages),
            'temperature': kwargs.get('temperature', 1.0),
        }

    monkeypatch.setattr(_fallback_llm, 'build_body', _build_body)
    return fallback, events


def _call(fallback, task, messages, on_tool_call_ready=None):
    return fallback._llm_call_with_fallback(
        task,
        {'model': 'primary-model', 'messages': list(messages)},
        'primary-model', 0, 512, False, None, messages,
        'low', False, {}, [], on_tool_call_ready=on_tool_call_ready)


def test_provider_overflow_compacts_then_recovers_on_same_model(monkeypatch):
    from lib.llm import PromptTooLongError

    calls = []
    compact_calls = []
    delivered = []
    callback = lambda *args, **kwargs: delivered.append((args, kwargs))

    def _stream(_task, body, tag='', on_tool_call_ready=None, **kwargs):
        calls.append((body['model'], tag, on_tool_call_ready, kwargs))
        if len(calls) == 1:
            raise PromptTooLongError('prompt is too long')
        return ({'role': 'assistant', 'content': 'recovered'}, 'stop', {
            'prompt_tokens': 7,
            'completion_tokens': 3,
        })

    def _compact(messages, **kwargs):
        compact_calls.append(kwargs)
        messages[:] = messages[-2:]
        return True

    fallback, events = _patch_recovery_runtime(
        monkeypatch, _stream, _compact)
    task = _task('overflow-recovers')
    _fallback_state._reactive_compact_attempts.pop(task['id'], None)
    try:
        result = _call(
            fallback, task,
            [{'role': 'user', 'content': 'old'},
             {'role': 'assistant', 'content': 'work'},
             {'role': 'user', 'content': 'continue'}],
            on_tool_call_ready=callback)
    finally:
        _fallback_state._reactive_compact_attempts.pop(task['id'], None)

    assert result['assistant_msg']['content'] == 'recovered'
    assert result['model'] == 'primary-model'
    assert [call[:2] for call in calls] == [
        ('primary-model', 'R1'),
        ('primary-model', 'R1-REACTIVE'),
    ]
    calls[1][2]('call-a', tool_name='read_files')
    assert delivered == [(('call-a',), {'tool_name': 'read_files'})]
    assert [kwargs.get('error_text') for kwargs in compact_calls] == [
        'prompt is too long']
    assert '_fallback_model' not in task
    assert any(event.get('detailKey') == 'stream.phase.reactiveCompact'
               for event in events)


def test_provider_overflow_on_configured_fallback_recovers_in_place(
        monkeypatch):
    from lib.llm import PermissionError_, PromptTooLongError

    calls = []

    def _stream(_task, body, tag='', **kwargs):
        calls.append((body['model'], tag, kwargs))
        if len(calls) == 1:
            raise PermissionError_('primary key rejected')
        if len(calls) == 2:
            raise PromptTooLongError('fallback prompt is too long')
        return ({'role': 'assistant', 'content': 'fallback recovered'},
                'stop', {'prompt_tokens': 4, 'completion_tokens': 2})

    fallback, _events = _patch_recovery_runtime(
        monkeypatch, _stream, lambda *_a, **_kw: True)
    task = _task('fallback-overflow')
    _fallback_state._reactive_compact_attempts.pop(task['id'], None)
    try:
        result = _call(
            fallback, task, [{'role': 'user', 'content': 'continue'}])
    finally:
        _fallback_state._reactive_compact_attempts.pop(task['id'], None)

    assert [(model, tag) for model, tag, _kwargs in calls] == [
        ('primary-model', 'R1'),
        ('fallback-model', 'R1-FALLBACK'),
        ('fallback-model', 'R1-REACTIVE'),
    ]
    assert not any(kwargs.get('pool_wide') for _model, _tag, kwargs in calls)
    assert result['assistant_msg']['content'] == 'fallback recovered'
    assert result['model'] == 'fallback-model'
    assert task['_fallback_model'] == 'fallback-model'


def test_reactive_rejection_raises_actual_failure_without_switching(monkeypatch):
    from lib.llm import BadRequestError, PromptTooLongError

    rejected = BadRequestError('malformed tool payload after compaction')
    calls = []

    def stream(_task, body, **_kwargs):
        calls.append(body['model'])
        if len(calls) == 1:
            raise PromptTooLongError('prompt is too long')
        raise rejected

    fallback, _events = _patch_recovery_runtime(
        monkeypatch, stream, lambda *_a, **_kw: True)
    task = _task('reactive-payload-rejected')
    try:
        with pytest.raises(BadRequestError) as caught:
            _call(fallback, task, [{'role': 'user', 'content': 'continue'}])
    finally:
        _fallback_state._reactive_compact_attempts.pop(task['id'], None)

    assert caught.value is rejected
    assert rejected._user_message['kind'] == 'bad_request'
    assert calls == ['primary-model', 'primary-model']
    assert '_fallback_model' not in task


def test_reactive_refusal_retires_speculative_tools_before_fallback(monkeypatch):
    """A refused retry retires its early tool work like a refused primary.

    The refused response is not an adopted answer: Anthropic requires its
    partial text and early tool work to be discarded. Prior tool results stay
    in the messages, so the fallback resumes from them instead of re-running.
    """
    from lib.llm import PromptTooLongError
    from lib.llm.stream_result import ProviderStreamResult

    class Speculative:
        def __init__(self):
            self.restarts = []

        def on_provider_attempt_restart(self, *, reason=''):
            self.restarts.append(reason)

    tools = Speculative()
    calls = []

    def stream(_task, body, tag='', **kwargs):
        calls.append(tag)
        if len(calls) == 1:
            raise PromptTooLongError('prompt is too long')
        if tag.endswith('-REACTIVE'):
            return ProviderStreamResult.from_legacy(
                {'role': 'assistant', 'content': ''}, 'content_filter',
                {'prompt_tokens': 5, 'completion_tokens': 1,
                 '_model_refusal': {'category': 'cyber', 'explanation': None}})
        return ({'role': 'assistant', 'content': 'fallback answer'}, 'stop',
                {'prompt_tokens': 5, 'completion_tokens': 2})

    fallback, _events = _patch_recovery_runtime(
        monkeypatch, stream, lambda *_a, **_kw: True)
    task = _task('reactive-refusal-tools')
    # Retiring a refused attempt rolls the round's text back to its base.
    task['content_lock'] = threading.Lock()
    task['_round_base_content'] = ''
    task['_round_base_thinking'] = ''
    api_rounds = []
    messages = [{'role': 'user', 'content': 'continue'}]
    try:
        result = fallback._llm_call_with_fallback(
            task, {'model': 'primary-model'}, 'primary-model', 0, 512, True,
            None, messages, 'low', False, {}, api_rounds,
            on_tool_call_ready=tools.on_provider_attempt_restart)
    finally:
        _fallback_state._reactive_compact_attempts.pop(task['id'], None)

    assert result['assistant_msg']['content'] == 'fallback answer'
    assert tools.restarts == ['model refusal']
    assert [row['tag'] for row in api_rounds] == [
        'R1-REACTIVE-REFUSED', 'R1-FALLBACK']
    assert api_rounds[0]['responseAuthoring'] is False
    # The preserved turn input survives; only the context composer adds
    # carriers. No refusal recovery rewrites or drops the tool evidence.
    assert {'role': 'user', 'content': 'continue'} in messages
