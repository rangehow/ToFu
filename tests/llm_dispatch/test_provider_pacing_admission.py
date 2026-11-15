"""Dispatch admission waits for its provider pacing turn (docs/NETWORK.md).

After the shared-contention gate and before transport, an attempt joins the
provider pacing queue of its slot's quota lane. These specs pin the dispatch
side of that contract: the wait is abortable and metered as queue wait,
immediate-only work yields with the lane's delay, a refused send keeps its
turn, and dispatchers without a pacing lane (old/fake) are unchanged.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

MODEL = 'kimi-k3.1-pro'


def _slot(key='cred-5409'):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key='sk-test', model=MODEL,
                capabilities={'text'}, provider_id='gw')


def _dispatcher(slots):
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    disp = LLMDispatcher()
    disp.slots = list(slots)
    disp.initialize = lambda: None
    disp._contention_strikes = {}
    disp._logical_index = {}
    disp._direct_models = {MODEL}
    return disp


def _teach_limit_one(disp, slot, window_s):
    """One send the provider served, then one it refused for its minute limit."""
    from lib.http_transport.provider_pacing import shared_pacer
    lane = disp.pacing_lane(slot)
    served = shared_pacer().join(lane, window_s=window_s)
    assert served.poll()[0] == 0.0
    refused = shared_pacer().join(lane, window_s=window_s)
    assert refused.poll()[0] == 0.0
    refused.rejected(request_limit=True)
    return lane


def _admit(disp, slot, state, *, defer=False, abort_check=None):
    from lib.llm_dispatch._api_contention import _admit_or_defer_shared_contention
    return _admit_or_defer_shared_contention(
        disp, slot, lambda seconds: None, '[t]', defer=defer, state=state,
        abort_check=abort_check)


def test_the_pacing_lane_is_the_slots_quota_lane():
    slot = _slot()
    assert _dispatcher([slot]).pacing_lane(slot) == slot.quota_lane()


def test_a_paced_wait_is_metered_as_queue_wait_and_logged(monkeypatch):
    from lib.llm_dispatch import _api_contention, quota_watch
    from lib.llm_dispatch._api_retry_state import DispatchRetryState
    monkeypatch.setattr(quota_watch, 'WINDOW_S', 0.3)
    infos = []
    monkeypatch.setattr(_api_contention.logger, 'info',
                        lambda message, *args: infos.append(message % args))
    slot = _slot()
    disp = _dispatcher([slot])
    _teach_limit_one(disp, slot, 0.3)
    state = DispatchRetryState()
    _admit(disp, slot, state)
    assert state.queue_wait_ms >= 150
    assert state.pacing_ticket is not None and state.pacing_ticket.state == 'granted'
    paced = [line for line in infos if 'paced send waited' in line]
    provider, credential, model = slot.quota_lane()
    assert len(paced) == 1 and paced[0].startswith('[t] [Dispatch] paced send waited ')
    assert paced[0].endswith(
        f'for the provider window on {provider}/{credential}:{model}')


def test_immediate_only_work_yields_with_the_lanes_delay():
    from lib.http_transport.provider_pacing import shared_pacer
    from lib.llm_dispatch._api_errors import DispatchSharedContentionDeferred
    from lib.llm_dispatch._api_retry_state import DispatchRetryState
    slot = _slot()
    disp = _dispatcher([slot])
    _teach_limit_one(disp, slot, 60.0)
    state = DispatchRetryState()
    with pytest.raises(DispatchSharedContentionDeferred) as deferred:
        _admit(disp, slot, state, defer=True)
    assert 50.0 < deferred.value.retry_after_s <= 60.0
    assert state.pacing_ticket is None
    assert shared_pacer().snapshot()[0]['waiting'] == 0


def test_an_abort_while_paced_leaves_the_queue():
    from lib.http_transport.provider_pacing import shared_pacer
    from lib.llm import AbortedError
    from lib.llm_dispatch._api_retry_state import DispatchRetryState
    slot = _slot()
    disp = _dispatcher([slot])
    _teach_limit_one(disp, slot, 60.0)
    with pytest.raises(AbortedError):
        _admit(disp, slot, DispatchRetryState(), abort_check=lambda: True)
    assert shared_pacer().snapshot()[0]['waiting'] == 0


def test_a_minute_limit_refusal_teaches_the_lane_and_keeps_the_turn():
    from lib.http_transport.provider_pacing import shared_pacer
    from lib.llm.llm_errors import RateLimitError
    from lib.llm_dispatch._api_contention import _note_paced_send_refused
    from lib.llm_dispatch._api_retry_state import DispatchRetryState
    slot = _slot()
    disp = _dispatcher([slot])
    lane = disp.pacing_lane(slot)
    served = shared_pacer().join(lane, window_s=60.0)
    assert served.poll()[0] == 0.0
    state = DispatchRetryState()
    _admit(disp, slot, state)
    _note_paced_send_refused(state, RateLimitError(
        'API HTTP 429: {"status":429,"message":"App:**5409在模型:'
        'kimi-k3.1-pro每分钟请求次数超过限制"}',
        status_code=429, is_shared_contention=True))
    assert state.pacing_ticket is None and state.pacing_order is not None
    assert shared_pacer().snapshot()[0]['limit'] == 1

    later = shared_pacer().join(lane, window_s=60.0)
    from lib.llm_dispatch._api_contention import _join_paced_send
    ticket = _join_paced_send(disp, slot, state)    # the refused request's retry
    assert ticket.order < later.order
    assert ticket.poll()[0] < later.poll()[0]


def _declare_limit_one(disp, slot, *, queued):
    """A provider-declared limit of one per minute, full, with ``queued`` waiting."""
    from lib.http_transport.provider_pacing import RateSignals, shared_pacer
    lane = disp.pacing_lane(slot)
    served = shared_pacer().join(lane, window_s=60.0)
    assert served.poll()[0] == 0.0
    refused = shared_pacer().join(lane, window_s=60.0)
    assert refused.poll()[0] == 0.0
    refused.rejected(RateSignals(limit=1))
    return [shared_pacer().join(lane, window_s=60.0) for _ in range(queued)]


def test_automatic_work_takes_the_key_whose_window_has_room(monkeypatch):
    monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
    from lib.llm_dispatch.conv_affinity import conv_affinity_cleared
    paced, roomy = _slot('cred-5409'), _slot('cred-0733')
    roomy.latency_ema = 99999.0          # the paced key wins on score alone
    disp = _dispatcher([paced, roomy])
    with conv_affinity_cleared():
        assert disp._pick('text', MODEL, None, None) is paced
        _declare_limit_one(disp, paced, queued=0)
        assert disp._pick('text', MODEL, None, None) is roomy


def test_a_warm_conversation_leaves_only_a_queue_a_window_deep(monkeypatch):
    """Waiting under one provider window still pays for the warm cache."""
    import uuid
    from lib.http_transport.provider_pacing import shared_pacer
    from lib.llm_dispatch.conv_affinity import conv_affinity, get_preferred_key
    monkeypatch.setenv('TOFU_CONV_STICKY_ROUTING', '1')
    monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
    warm, other = _slot('cred-5409'), _slot('cred-0733')
    other.latency_ema = 99999.0
    disp = _dispatcher([warm, other])
    conv = f'conv-{uuid.uuid4().hex}'
    with conv_affinity(conv):
        assert disp._pick('text', MODEL, None, None) is warm
        waiting = _declare_limit_one(disp, warm, queued=0)
        assert disp._pick('text', MODEL, None, None) is warm
        waiting.append(shared_pacer().join(disp.pacing_lane(warm), window_s=60.0))
        assert disp._pick('text', MODEL, None, None) is other
    assert get_preferred_key(conv, route_key=MODEL) == other.affinity_key()
    for ticket in waiting:
        ticket.cancel()


def test_a_dispatcher_without_a_pacing_lane_does_not_pace():
    from types import SimpleNamespace
    from lib.http_transport.provider_pacing import shared_pacer
    from lib.llm_dispatch._api_contention import _join_paced_send
    state = SimpleNamespace()
    assert _join_paced_send(SimpleNamespace(), _slot(), state) is None
    assert shared_pacer().snapshot() == []
