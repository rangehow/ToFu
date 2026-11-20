"""A model-permission refusal stays typed, attributable and recoverable.

Recorded evidence (2026-11-19): three Worker sessions dispatched on
``kimi-k3.1-pro`` were rejected with HTTP 401 "当前 App ID 已被禁用" while the
model slot itself was registered and capable. The refusal was retryable in
neither direction: the session died and the terminal error named only the last
model attempted. These specs pin that a 401/403 permission refusal is
distinguishable from content/transient failures, continues on the configured
fallback and, when no fallback can answer, reports the primary model's
permission cause instead of the last model's failure.
"""
from __future__ import annotations

import threading

import pytest

import lib.tasks_pkg.llm_fallback._call as call_mod
import lib.tasks_pkg.llm_fallback._pool as _pool
import lib.tasks_pkg.llm_fallback._retry as _retry
import lib.tasks_pkg.llm_fallback._transport as _transport
import lib.tasks_pkg.llm_fallback._usage as _usage
import lib.tasks_pkg.manager._events as _events
import lib.tasks_pkg.round_retry as _round_retry
from lib.agent_core.events import EventType
from lib.llm.llm_errors import ContentFilterError, PermissionError_, RateLimitError
from lib.llm.stream_result import ProviderStreamResult

pytestmark = pytest.mark.unit

_PERMISSION_BODY = (
    'API HTTP 401: {"status":401,"message":"当前 App ID 已被禁用，'
    '请前往Friday控制台查看详情或联系管理员。"}'
)


def _result(content: str) -> ProviderStreamResult:
    return ProviderStreamResult.from_legacy(
        {'role': 'assistant', 'content': content},
        'stop',
        {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12},
    )


def _task() -> dict:
    return {
        'id': 'permission-recovery-task',
        'convId': 'conv-permission-recovery',
        '_userId': 1,
        'config': {},
        'events': [],
        'events_lock': threading.Lock(),
        'content_lock': threading.Lock(),
        'content': '',
        'thinking': '',
        '_round_base_content': '',
        '_round_base_thinking': '',
    }


@pytest.fixture
def harness(monkeypatch):
    state = {'streams': [], 'script': [], 'events': [], 'rescues': 0}

    def fake_stream(task, body, tag='', on_tool_call_ready=None, **_kwargs):
        state['streams'].append((body.get('model'), tag))
        outcome = state['script'].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def no_rescue(*_args, **_kwargs):
        state['rescues'] += 1
        return None

    monkeypatch.setattr(_transport, 'stream_llm_response', fake_stream)
    monkeypatch.setattr(
        _retry, '_get_fallback_model', lambda _task: state['fallback']
    )
    monkeypatch.setattr(_pool, '_attempt_pool_rescue', no_rescue)
    monkeypatch.setattr(_usage, '_emit_round_usage', lambda *a, **k: None)
    monkeypatch.setattr(_usage, 'append_event',
                        lambda _task, event: state['events'].append(event))
    monkeypatch.setattr(_events, 'append_event',
                        lambda _task, event: state['events'].append(event))
    monkeypatch.setattr(_round_retry, 'append_event',
                        lambda _task, event: state['events'].append(event))
    monkeypatch.setattr('lib.log.audit_log', lambda *a, **k: None)
    return state


def _call(task, model='kimi-k3.1-pro'):
    return call_mod._llm_call_with_fallback(
        task,
        {'model': model},
        model,
        0,
        512,
        False,
        None,
        [{'role': 'user', 'content': 'continue the assigned work'}],
        'medium',
        True,
        {},
        [],
    )


def test_permission_refusal_continues_on_configured_fallback(harness):
    """A 401/403 is typed ``permission`` and the round continues on the
    configured fallback, naming which model continued."""
    harness['fallback'] = 'glm-5.3'
    harness['script'] = [
        PermissionError_(_PERMISSION_BODY, status_code=401),
        _result('fallback answer'),
    ]
    task = _task()

    result = _call(task)

    assert [model for model, _tag in harness['streams']] == [
        'kimi-k3.1-pro', 'glm-5.3']
    assert result['model'] == 'glm-5.3'
    assert result['assistant_msg']['content'] == 'fallback answer'
    assert 'error' not in task
    assert task['_fallback_from'] == 'kimi-k3.1-pro'
    assert task['_fallback_model'] == 'glm-5.3'
    assert task['_fallback_kind'] == 'permission'
    assert 'App ID 已被禁用' in task['_fallback_reason']

    switches = [event for event in harness['events']
                if event.get('type') == EventType.MODEL_FALLBACK]
    assert len(switches) == 1
    assert switches[0]['fallbackKind'] == 'permission'
    assert switches[0]['fallbackFrom'] == 'kimi-k3.1-pro'
    assert switches[0]['fallbackModel'] == 'glm-5.3'


def test_both_refused_reports_the_primary_permission_cause(harness):
    """When the fallback is refused for permission too, the terminal error
    keeps the primary model's permission cause (and the fallback refusal as
    evidence) instead of blaming only the last model."""
    harness['fallback'] = 'glm-5.3'
    harness['script'] = [
        PermissionError_(_PERMISSION_BODY, status_code=401),
        PermissionError_('API HTTP 403: model not granted to this account',
                         status_code=403),
    ]
    task = _task()

    result = _call(task)

    assert result['_loop_action'] == 'break'
    assert result['finish_reason'] == 'error'
    error = task['error']
    assert error['kind'] == 'permission'
    assert error['retryable'] is False
    assert error['model'] == 'kimi-k3.1-pro'
    assert 'App ID 已被禁用' in error['detail']
    assert error['fallbackFrom'] == 'kimi-k3.1-pro'
    assert error['fallbackModel'] == 'glm-5.3'
    assert error['fallbackFailed'] is True
    assert error['autoRetryExhausted'] is True
    assert error['fallbackFailureKind'] == 'permission'
    assert 'model not granted to this account' in error['fallbackDetail']
    # A permission refusal on the fallback never authorizes a pool-wide
    # rescue: the account, not the individual model, was refused.
    assert harness['rescues'] == 1


def test_transient_error_keeps_the_existing_fallback_path(harness):
    """A non-permission availability failure is not reclassified: it still
    moves to the configured fallback through the existing path."""
    harness['fallback'] = 'glm-5.3'
    harness['script'] = [
        RateLimitError('429 slow down', is_gateway=False, status_code=429),
        _result('fallback answer'),
    ]
    task = _task()

    result = _call(task)

    assert [model for model, _tag in harness['streams']] == [
        'kimi-k3.1-pro', 'glm-5.3']
    assert result['model'] == 'glm-5.3'
    assert task['_fallback_kind'] == 'ratelimit'
    switches = [event for event in harness['events']
                if event.get('type') == EventType.MODEL_FALLBACK]
    assert switches and switches[0]['fallbackKind'] == 'ratelimit'


def test_content_filter_is_not_treated_as_permission(harness):
    """An HTTP 450 content block settles as ``content_filter`` and never
    authorizes a model switch: the permission lane must stay distinct."""
    harness['fallback'] = 'glm-5.3'
    harness['script'] = [ContentFilterError('450 blocked content')]
    task = _task()

    result = _call(task)

    assert [model for model, _tag in harness['streams']] == ['kimi-k3.1-pro']
    assert result['finish_reason'] == 'content_filter'
    assert harness['rescues'] == 0