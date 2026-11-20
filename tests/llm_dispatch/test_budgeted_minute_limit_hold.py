"""A budgeted dispatch holds a key's minute limit for one provider window.

Incident 2026-11-18 (15 Turns, e.g. worker-5600910a turn 76a3afe4, attempt
36339e0e): claude-opus-5.5 refused a round (content filter), the configured
fallback kimi-k3.1-pro got the gateway's per-AppId minute limit
("App:**6672在模型:kimi-k3.1-pro-m17每分钟请求次数超过限制"), and its 3-response
fallback budget was spent by the gate's first probes within about two
seconds of a sixty-second window. The Turn settled ``done(error)`` although
the provider would have served the request once its window rolled.

A minute-limit rejection is the provider stating when retrying pays: not
before its window ends. A budgeted dispatch (configured fallback, pool
rescue, optional callers) therefore holds such a pair for one provider
window (``quota_watch.WINDOW_S``) of its own rejections instead of counting
each probe, then leaves the pair exactly as the shared rule does
(``DispatchRateLimitedNoRoute``). Cancellation and caller deadlines still end
the hold at once through ``abort_check``; other 429 kinds keep the count.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

APP_MODEL_RPM_BODY = (
    'API HTTP 429: {"status":429,"message":"App:**6672在模型:'
    'kimi-k3.1-pro-m17每分钟请求次数超过限制","data":null}'
)
MODEL = 'kimi-k3.1-pro'
PROBE_SPACING_S = 15.0  # the gate's maximum probe spacing
FALLBACK_BUDGET = 3     # _fallback_max_429_attempts default


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, secs):
        self.now += secs


def _slot(key):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key=f'sk-{key}-6672', model=MODEL,
                capabilities={'text'})


class _Router:
    """One warm key; the shared gate run can be reset by other traffic."""

    def __init__(self, clock, *slots, shared_run_resets=False):
        self.slots = list(slots)
        self.clock = clock
        self.rejecting_since = {}
        self.shared_run_resets = shared_run_resets

    def _eligible(self, kw):
        pairs = set(kw.get('exclude_pairs') or ())
        return [s for s in self.slots if (s.key_name, s.model) not in pairs]

    def pick_and_reserve(self, **kw):
        eligible = self._eligible(kw)
        if not eligible:
            return None
        eligible[0].record_request()
        return eligible[0]

    def has_capable_slots(self, *args, **kw):
        return bool(self._eligible(kw))

    def summarize_slots(self, *args, **kw):
        return 'router'

    def note_shared_contention(self, slot, *, per_credential=False, retry_after_s=None):
        self.rejecting_since.setdefault(slot.key_name, self.clock.now)
        return max(PROBE_SPACING_S, retry_after_s or 0.0)

    def shared_contention_rejecting_past_window(self, slot):
        if self.shared_run_resets:
            # Another request on the lane succeeded meanwhile: the shared
            # rejection run restarted, so only this request's own clock
            # can bound its hold.
            return False
        from lib.llm_dispatch.quota_watch import WINDOW_S
        since = self.rejecting_since.get(slot.key_name)
        return since is not None and self.clock.now - since >= WINDOW_S


class _Provider:
    """Per-minute request limit: rejects until ``reset_at``, then serves."""

    def __init__(self, clock, *, reset_at=None, body=APP_MODEL_RPM_BODY):
        self.clock = clock
        self.reset_at = reset_at
        self.body = body
        self.sends = []

    def __call__(self, body, api_key=None, **kwargs):
        from lib.llm.llm_errors import _classify_http_error
        self.sends.append(self.clock.now)
        if len(self.sends) > 50:
            raise AssertionError('dispatch kept probing a minute-limited key')
        if self.reset_at is None or self.clock.now < self.reset_at:
            _classify_http_error(429, self.body, MODEL, '[t]')
        return 'served', 'tool_calls', {}


def _install(monkeypatch, router, clock, provider):
    from lib.llm_dispatch import api
    import lib.llm as llm_mod
    monkeypatch.setattr(api, 'get_dispatcher', lambda: router)
    monkeypatch.setattr(api, 'time', clock)
    monkeypatch.setattr('lib.llm._transport.time', clock)
    monkeypatch.setattr(llm_mod, 'stream_chat', provider)
    return api


def _dispatch(api, **kwargs):
    return api.dispatch_stream(
        [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
        prefer_model=MODEL, strict_model=True,
        max_429_attempts=FALLBACK_BUDGET, **kwargs)


def test_budgeted_dispatch_waits_out_the_window_and_is_served(monkeypatch):
    from lib.llm_dispatch.quota_watch import WINDOW_S
    clock = _Clock()
    provider = _Provider(clock, reset_at=40.0)
    api = _install(monkeypatch, _Router(clock, _slot('kA')), clock, provider)

    msg, finish, _usage = _dispatch(api)

    assert (msg, finish) == ('served', 'tool_calls')
    assert len(provider.sends) > FALLBACK_BUDGET, (
        'probes inside one provider window are not separate budget attempts')
    assert provider.sends[-1] >= 40.0
    assert clock.now < WINDOW_S
    # The gate spaces the probes; holding the window does not flood the AppId.
    gaps = [b - a for a, b in zip(provider.sends, provider.sends[1:])]
    assert min(gaps) >= PROBE_SPACING_S


@pytest.mark.parametrize('shared_run_resets', [False, True],
                         ids=['shared-run', 'shared-run-reset-by-others'])
def test_a_saturated_key_is_left_after_one_window_of_own_rejections(
        monkeypatch, shared_run_resets):
    from lib.llm import RateLimitError
    from lib.llm_dispatch._api_errors import DispatchRateLimitedNoRoute
    from lib.llm_dispatch.quota_watch import WINDOW_S
    clock = _Clock()
    provider = _Provider(clock, reset_at=None)
    router = _Router(clock, _slot('kA'), shared_run_resets=shared_run_resets)
    api = _install(monkeypatch, router, clock, provider)

    with pytest.raises(DispatchRateLimitedNoRoute) as caught:
        _dispatch(api)

    assert isinstance(caught.value, RateLimitError)
    assert caught.value.reason == 'rate_limited_no_route'
    # Bounded by the provider window, never indefinitely.
    assert WINDOW_S <= clock.now <= WINDOW_S + PROBE_SPACING_S
    assert len(provider.sends) <= WINDOW_S / PROBE_SPACING_S + 2


def test_cancellation_ends_the_hold_without_another_send(monkeypatch):
    from lib.llm import AbortedError
    clock = _Clock()
    provider = _Provider(clock, reset_at=None)
    api = _install(monkeypatch, _Router(clock, _slot('kA')), clock, provider)
    stop_at = 20.0

    with pytest.raises(AbortedError):
        _dispatch(api, abort_check=lambda: clock.now >= stop_at)

    assert all(sent < stop_at for sent in provider.sends)


def test_a_caller_deadline_ends_the_hold_at_the_deadline(monkeypatch):
    """Task deadlines reach dispatch through ``abort_check``
    (``invocation_deadline_requested``); the hold never extends them."""
    from lib.llm import AbortedError
    clock = _Clock()
    provider = _Provider(clock, reset_at=50.0)
    api = _install(monkeypatch, _Router(clock, _slot('kA')), clock, provider)
    deadline = 25.0

    with pytest.raises(AbortedError):
        _dispatch(api, abort_check=lambda: clock.now >= deadline)

    assert all(sent < deadline for sent in provider.sends)
    assert clock.now <= deadline + PROBE_SPACING_S


def test_other_rate_limits_keep_the_exact_attempt_budget(monkeypatch):
    from lib.llm_dispatch._api_errors import DispatchRateLimitBudgetExceeded
    clock = _Clock()
    provider = _Provider(clock, reset_at=None,
                         body='API HTTP 429: {"error":"upstream overloaded"}')
    api = _install(monkeypatch, _Router(clock, _slot('kA')), clock, provider)

    with pytest.raises(DispatchRateLimitBudgetExceeded) as caught:
        _dispatch(api)

    assert (caught.value.attempts, caught.value.limit) == (FALLBACK_BUDGET, FALLBACK_BUDGET)
    assert len(provider.sends) == FALLBACK_BUDGET


def test_async_dispatch_holds_the_window_like_sync(monkeypatch):
    """The sync, async and non-stream loops share DispatchFailurePolicy."""
    import asyncio
    from lib.llm_dispatch.quota_watch import WINDOW_S
    clock = _Clock()
    provider = _Provider(clock, reset_at=40.0)
    api = _install(monkeypatch, _Router(clock, _slot('kA')), clock, provider)

    async def _advance(seconds, abort_check=None):
        clock.sleep(seconds)

    async def _async_provider(body, **kwargs):
        return provider(body, **kwargs)

    monkeypatch.setattr('lib.llm._transport.async_abortable_sleep', _advance)
    monkeypatch.setattr('lib.llm.astream.async_stream_chat', _async_provider)

    result = asyncio.run(api.async_dispatch_stream(
        [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
        prefer_model=MODEL, strict_model=True, max_429_attempts=FALLBACK_BUDGET))

    assert tuple(result)[:2] == ('served', 'tool_calls')
    assert len(provider.sends) > FALLBACK_BUDGET
    assert clock.now < WINDOW_S


def test_non_stream_chat_holds_the_window_like_stream(monkeypatch):
    from lib.llm_dispatch.quota_watch import WINDOW_S
    clock = _Clock()
    provider = _Provider(clock, reset_at=40.0)
    api = _install(monkeypatch, _Router(clock, _slot('kA')), clock, provider)

    def _chat(*args, api_key=None, **kwargs):
        provider(None, api_key=api_key)
        return 'served-text', {'completion_tokens': 1}

    monkeypatch.setattr('lib.llm.chat', _chat)

    content, _usage = api.dispatch_chat(
        [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
        prefer_model=MODEL, strict_model=True, max_429_attempts=FALLBACK_BUDGET)

    assert content == 'served-text'
    assert len(provider.sends) > FALLBACK_BUDGET
    assert clock.now < WINDOW_S


@pytest.mark.parametrize('saturation_budget', ['0', '1'])
def test_saved_conversation_holds_model_past_a_preaged_gate(monkeypatch, caplog, saturation_budget):
    """An already-saturated shared lane must not switch a new conversation round."""
    from lib.llm_dispatch.quota_watch import WINDOW_S
    from lib.tasks_pkg.manager import _stream

    clock = _Clock()
    clock.now = WINDOW_S
    provider = _Provider(clock, reset_at=3 * WINDOW_S)
    router = _Router(clock, _slot('kA'))
    router.rejecting_since['kA'] = 0.0
    _install(monkeypatch, router, clock, provider)
    monkeypatch.setenv('TOFU_429_SATURATION_SECS', saturation_budget)
    monkeypatch.setattr(_stream, '_watchdog_owner', lambda task: None)
    caplog.set_level('INFO', logger='lib.llm_dispatch.api')

    result = _stream._watchdog_aware_dispatch_stream(
        {'id': 'saved', 'convId': 'conv-saved', '_userId': 1},
        [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
        prefer_model=MODEL, strict_model=True, abort_check=lambda: False)

    assert result.message == 'served'
    assert provider.sends[0] == WINDOW_S
    assert provider.sends[-1] >= 3 * WINDOW_S
    assert min(b - a for a, b in zip(provider.sends, provider.sends[1:])) >= PROBE_SPACING_S
    assert sum('429 rate-limited on' in record.message
               for record in caplog.records) == 1


def test_saved_conversation_can_stop_after_a_provider_window(monkeypatch):
    from lib.llm import AbortedError
    from lib.llm_dispatch.quota_watch import WINDOW_S
    from lib.tasks_pkg.manager import _stream

    clock = _Clock()
    provider = _Provider(clock)
    _install(monkeypatch, _Router(clock, _slot('kA')), clock, provider)
    monkeypatch.setattr(_stream, '_watchdog_owner', lambda task: None)
    stop_at = WINDOW_S + 20

    with pytest.raises(AbortedError):
        _stream._watchdog_aware_dispatch_stream(
            {'id': 'saved', 'convId': 'conv-saved', '_userId': 1},
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
            prefer_model=MODEL, strict_model=True, abort_check=lambda: clock.now >= stop_at)

    assert WINDOW_S < provider.sends[-1] < stop_at


@pytest.mark.parametrize('mode', ['headless', 'watchdog', 'configured-fallback'])
def test_persistent_minute_limit_can_still_leave_model_outside_primary_chat(monkeypatch, mode):
    from lib.llm_dispatch._api_errors import DispatchRateLimitedNoRoute
    from lib.tasks_pkg.conversation_delivery import mark_no_saved_conversation
    from lib.tasks_pkg.manager import _stream

    clock = _Clock()
    provider = _Provider(clock)
    _install(monkeypatch, _Router(clock, _slot('kA')), clock, provider)
    monkeypatch.setattr(_stream, '_watchdog_owner', lambda task: 1 if mode == 'watchdog' else None)
    task = {'id': 't', 'convId': 'conv', '_userId': 1}
    if mode == 'headless':
        mark_no_saved_conversation(task)

    with pytest.raises(DispatchRateLimitedNoRoute):
        _stream._watchdog_aware_dispatch_stream(
            task, [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
            prefer_model=MODEL, strict_model=True, abort_check=lambda: False,
            max_429_attempts=FALLBACK_BUDGET if mode == 'configured-fallback' else None)


@pytest.mark.parametrize('primary_failure', ['refusal', 'primary_minute_limit_no_route'])
def test_configured_fallback_round_completes_after_the_window(monkeypatch, primary_failure):
    """Through the real fallback owner: the round the incident lost completes.

    The primary fails (the incident's content refusal, or its own minute
    limit settled as ``rate_limited_no_route``); the configured fallback's
    dispatch, with the real 3-response budget, holds its minute-limited key
    until the provider serves it. Exactly one fallback round is billed and its
    tool call is delivered once; no error settles the Turn.
    """
    import threading
    from lib.llm.llm_errors import ModelRefusalError, RateLimitError
    from lib.llm_dispatch._api_errors import DispatchRateLimitedNoRoute
    import lib.tasks_pkg.llm_fallback._retry as fallback_retry
    import lib.tasks_pkg.llm_fallback._transport as fallback_transport
    import lib.tasks_pkg.llm_fallback._usage as fallback_usage
    import lib.tasks_pkg.manager._events as task_events
    from lib.tasks_pkg.llm_fallback._call import _llm_call_with_fallback

    clock = _Clock()
    provider = _Provider(clock, reset_at=40.0)
    api = _install(monkeypatch, _Router(clock, _slot('kA')), clock, provider)
    tool_call = {'id': 'call-1', 'type': 'function',
                 'function': {'name': 'run_command', 'arguments': '{}'}}
    calls = []

    def _stream(task, body, tag='', on_tool_call_ready=None, **kwargs):
        calls.append((tag, kwargs.get('max_429_attempts')))
        if len(calls) == 1:
            if primary_failure == 'refusal':
                raise ModelRefusalError('claude-opus-5.5', {'category': 'frontier_llm'})
            raise DispatchRateLimitedNoRoute(RateLimitError('minute limit', status_code=429))
        api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix=f'[{tag}]',
            prefer_model=MODEL, strict_model=True,
            max_429_attempts=kwargs.get('max_429_attempts'))
        return ({'role': 'assistant', 'content': '', 'tool_calls': [tool_call]},
                'tool_calls', {'prompt_tokens': 5, 'completion_tokens': 3,
                               '_dispatch': {'model': MODEL, 'key': 'kA'}})

    monkeypatch.setattr(fallback_transport, 'stream_llm_response', _stream)
    monkeypatch.setattr(fallback_retry, '_get_fallback_model', lambda task: MODEL)
    monkeypatch.setattr(fallback_retry, '_get_pool_rescue_model', lambda failed: '')
    monkeypatch.setattr(fallback_usage, '_emit_round_usage', lambda *a, **kw: None)
    monkeypatch.setattr(task_events, 'append_event', lambda task, ev: None)
    task = {'id': 't-window', 'convId': 'conv-window', 'config': {},
            'content': '', 'thinking': '', 'content_lock': threading.Lock(),
            'events': [], 'events_lock': threading.Lock()}
    api_rounds = []

    result = _llm_call_with_fallback(
        task, {'model': 'claude-opus-5.5'}, 'claude-opus-5.5', 160, 512,
        True, None, [{'role': 'user', 'content': 'hi'}],
        'low', False, {}, api_rounds)

    assert [tag for tag, _ in calls] == ['R161', 'R161-FALLBACK']
    assert calls[1][1] == FALLBACK_BUDGET
    assert result['finish_reason'] == 'tool_calls'
    assert result['assistant_msg']['tool_calls'] == [tool_call]
    assert 'error' not in task
    assert len(provider.sends) > FALLBACK_BUDGET
    assert [r.get('tag') for r in api_rounds].count('R161-FALLBACK') == 1
