"""A key's own minute limit cannot hold a round past its provider window.

Incident 2026-11-10/11: a Watchdog Turn retried round 1 for 22 hours, about
4,800 times, all HTTP 429 "App:**4861在模型:kunlunbedrock.claude-opus-5.5-m17
每分钟请求次数超过限制" (the sankuai AppId is the calling key; the limit is per
AppId per model). The warm key is paced for one provider window to keep its
prompt cache (docs/modules/llm_io.md). After that window the request leaves
the pair: another key serves it, or, with no eligible pair left, dispatch
raises the typed rate_limited_no_route RateLimitError for the caller's model
fallback instead of waiting forever.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

APP_MODEL_RPM_BODY = (
    'API HTTP 429: {"status":429,"message":"App:**4861在模型:'
    'kunlunbedrock.claude-opus-5.5-m17每分钟请求次数超过限制","data":null}'
)
MODEL = 'claude-opus-5.5'
PROBE_SPACING_S = 15.0  # the gate's maximum probe spacing


class _Spun(BaseException):
    """Escapes dispatch's retry handling when a limited key keeps being re-picked."""


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
    return Slot(key_name=key, api_key=f'sk-{key}-4861', model=MODEL,
                capabilities={'text'})


class _Router:
    """Holds the warm (first eligible) key; gates track rejection runs."""

    def __init__(self, clock, *slots):
        self.slots = list(slots)
        self.clock = clock
        self.rejecting_since = {}

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
        assert per_credential, 'an AppId minute limit arms only its own lane'
        self.rejecting_since.setdefault(slot.key_name, self.clock.now)
        return max(PROBE_SPACING_S, retry_after_s or 0.0)

    def shared_contention_rejecting_past_window(self, slot):
        from lib.llm_dispatch.quota_watch import WINDOW_S
        since = self.rejecting_since.get(slot.key_name)
        return since is not None and self.clock.now - since >= WINDOW_S


def _limited(calls, key):
    from lib.llm.llm_errors import _classify_http_error
    calls[key] = calls.get(key, 0) + 1
    if calls[key] > 20:
        raise _Spun(f'dispatch kept retrying the rate-limited key {key}')
    _classify_http_error(429, APP_MODEL_RPM_BODY, MODEL, '[t]')


def _install(monkeypatch, router, clock, stream):
    from lib.llm_dispatch import api
    import lib.llm as llm_mod
    monkeypatch.setattr(api, 'get_dispatcher', lambda: router)
    monkeypatch.setattr(api, 'time', clock)
    monkeypatch.setattr(llm_mod, 'stream_chat', stream)
    return api


def test_warm_key_is_paced_for_one_window_then_another_key_serves(monkeypatch):
    from lib.llm_dispatch.quota_watch import WINDOW_S
    clock = _Clock()
    ka, kb = _slot('kA'), _slot('kB')
    calls = {}

    def stream(body, api_key=None, **kwargs):
        if api_key == ka.api_key:
            _limited(calls, 'kA')
        calls['kB'] = calls.get('kB', 0) + 1
        return 'ok', 'stop', {}

    api = _install(monkeypatch, _Router(clock, ka, kb), clock, stream)
    msg, finish, _usage = api.dispatch_stream(
        [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
        prefer_model=MODEL, strict_model=True)
    assert (msg, finish) == ('ok', 'stop')
    assert calls['kA'] >= 2, 'the warm key is paced, not dropped on its first 429'
    assert clock.now >= WINDOW_S
    assert calls['kB'] == 1


@pytest.mark.parametrize('hold', [None, 31.0, 60.0], ids=['no-hold', 'hold-31s', 'hold-60s'])
def test_a_named_hold_keeps_the_rejection_run_until_the_window_passes(monkeypatch, hold):
    """A provider-named hold (Retry-After) longer than the gate's decay grace
    is waiting, not quiet: the warm key's run of rejections continues across
    it, so the request still leaves the pair after one provider window
    instead of re-probing the limited key once per hold forever."""
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    from lib.llm_dispatch.quota_watch import WINDOW_S
    now = [100.0]
    monkeypatch.setattr('lib.llm_dispatch.dispatcher.time.monotonic', lambda: now[0])
    slot = _slot('kA')
    disp = LLMDispatcher()
    disp.slots = [slot]
    disp.initialize = lambda: None
    disp._contention_strikes = {}
    disp._logical_index = {}
    disp._direct_models = {MODEL}
    lane = disp._contention_gates(slot)[0]
    for _ in range(6):
        disp.note_shared_contention(slot, per_credential=True, retry_after_s=hold)
        if disp.shared_contention_rejecting_past_window(slot):
            break
        # The next probe goes out once the gate allows it, as admission waits.
        now[0] = max(now[0] + PROBE_SPACING_S, disp._contention_strikes[lane].next_probe_at)
    assert disp.shared_contention_rejecting_past_window(slot), (
        f'still re-probing the limited key after {now[0] - 100.0:.0f}s')
    assert now[0] - 100.0 >= WINDOW_S


def test_every_key_past_its_window_raises_typed_no_route(monkeypatch):
    from lib.llm import RateLimitError
    from lib.llm_dispatch._api_errors import DispatchRateLimitedNoRoute
    clock = _Clock()
    ka, kb = _slot('kA'), _slot('kB')
    calls = {}

    def stream(body, api_key=None, **kwargs):
        _limited(calls, 'kA' if api_key == ka.api_key else 'kB')

    api = _install(monkeypatch, _Router(clock, ka, kb), clock, stream)
    with pytest.raises(DispatchRateLimitedNoRoute) as caught:
        api.dispatch_stream([{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
                            prefer_model=MODEL, strict_model=True)
    # A RateLimitError, so the caller's model fallback takes over; with no
    # fallback the attempt settles with this reason instead of spinning.
    assert isinstance(caught.value, RateLimitError)
    assert caught.value.reason == 'rate_limited_no_route'
    assert set(calls) == {'kA', 'kB'}
