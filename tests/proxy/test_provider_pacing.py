"""Provider pacing: first-in, first-out send admission from provider signals.

The network authority (docs/NETWORK.md, "Provider pacing") paces the requests
of one provider quota lane. The only inputs are the provider's own signals:
a refusal naming a request limit over the lane's window, ``Retry-After`` and
rate-limit headers. These specs drive ``ProviderPacer`` on a fake clock.
"""
from __future__ import annotations

import pytest

from lib.http_transport.provider_pacing import (
    ProviderPacer, RateSignals, current_paced_send, note_wire_send, paced_send,
    response_rate_signals,
)

pytestmark = pytest.mark.unit

LANE = ('gw', 'cred-5409', 'kimi-k3.1-pro')
WINDOW = 60.0


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _pacer():
    clock = Clock()
    return ProviderPacer(clock=clock), clock


def _send(pacer, clock):
    ticket = pacer.join(LANE, window_s=WINDOW)
    delay, _signal = ticket.poll()
    assert delay == 0.0, delay
    return ticket


def _learn_limit(pacer, clock, accepted=4):
    """``accepted`` requests one second apart, then a refused one."""
    sent = []
    for _ in range(accepted):
        sent.append(_send(pacer, clock))
        clock.now += 1.0
    refused = _send(pacer, clock)
    refused.rejected(request_limit=True)
    return sent


def test_a_lane_is_unpaced_until_the_provider_states_a_limit():
    pacer, clock = _pacer()
    for _ in range(20):
        _send(pacer, clock)
    assert pacer.snapshot() == [{
        'lane': list(LANE), 'limit': None, 'limit_source': None,
        'window_s': WINDOW, 'in_window': 20, 'waiting': 0, 'hold_s': 0.0}]


def test_a_request_limit_refusal_teaches_the_requests_still_in_the_window():
    pacer, clock = _pacer()
    _learn_limit(pacer, clock, accepted=4)
    assert pacer.snapshot()[0]['limit'] == 4
    assert pacer.snapshot()[0]['limit_source'] == 'learned'

    # The next request waits until the oldest of the four leaves the window,
    # not for a probe schedule: the provider admitted it at 1000.
    clock.now += 1.0
    waiting = pacer.join(LANE, window_s=WINDOW)
    delay, _signal = waiting.poll()
    assert delay == pytest.approx(1000.0 + WINDOW - clock.now)

    clock.now = 1000.0 + WINDOW
    assert waiting.poll()[0] == 0.0


def test_a_refusal_naming_no_request_limit_teaches_nothing():
    pacer, clock = _pacer()
    for _ in range(3):
        _send(pacer, clock)
    _send(pacer, clock).rejected(request_limit=False)
    assert pacer.snapshot()[0]['limit'] is None
    assert pacer.join(LANE, window_s=WINDOW).poll()[0] == 0.0


def test_a_refusal_with_nothing_of_ours_in_the_window_teaches_nothing():
    pacer, clock = _pacer()
    _send(pacer, clock).rejected(request_limit=True)
    assert pacer.snapshot() == []
    assert pacer.join(LANE, window_s=WINDOW).poll()[0] == 0.0


def test_waiters_are_granted_in_the_order_they_joined():
    pacer, clock = _pacer()
    _learn_limit(pacer, clock, accepted=4)   # sends at 1000..1003
    clock.now += 0.5
    queue = [pacer.join(LANE, window_s=WINDOW) for _ in range(3)]
    delays = [ticket.poll()[0] for ticket in queue]
    expected = [1000.0 + WINDOW, 1001.0 + WINDOW, 1002.0 + WINDOW]
    assert delays == pytest.approx([at - clock.now for at in expected])

    clock.now = 1001.0 + WINDOW            # two oldest sends have left
    # Polling the last waiter grants every due one ahead of it, in order.
    assert queue[2].poll()[0] == pytest.approx(1.0)
    assert [ticket.state for ticket in queue] == ['granted', 'granted', 'waiting']
    assert queue[0].granted_at == queue[1].granted_at == clock.now


def test_a_request_that_comes_back_keeps_its_first_turn():
    pacer, clock = _pacer()
    sent = _learn_limit(pacer, clock, accepted=4)
    later = pacer.join(LANE, window_s=WINDOW)
    # Two earlier requests come back (refused, or moved from another lane)
    # in the reverse of their first order; each keeps its own turn.
    second_back = pacer.join(LANE, window_s=WINDOW, order=sent[1].order)
    first_back = pacer.join(LANE, window_s=WINDOW, order=sent[0].order)
    assert first_back.poll()[0] < second_back.poll()[0] < later.poll()[0]


def test_one_capacity_probe_per_window_retires_or_relearns_the_limit():
    pacer, clock = _pacer()
    sent = _learn_limit(pacer, clock, accepted=2)   # limit 2 learned at 1002
    for ticket in sent:
        ticket.accepted()

    # A window after the refusal, the lane is full again and work waits:
    # exactly one request goes beyond the learned limit.
    clock.now = 1002.0 + WINDOW + 1.0
    full = [_send(pacer, clock), _send(pacer, clock)]
    probe = pacer.join(LANE, window_s=WINDOW)
    behind = pacer.join(LANE, window_s=WINDOW)
    assert probe.poll()[0] == 0.0 and probe.probe
    assert behind.poll()[0] > 0.0

    # Refused: the limit is taught again and the next probe is a window away.
    probe.rejected(request_limit=True)
    assert pacer.snapshot()[0]['limit'] == 2
    assert behind.poll()[0] == pytest.approx(full[0].granted_at + WINDOW - clock.now)

    # Served: the provider took more than the limit, so pacing stops.
    clock.now = full[0].granted_at + WINDOW
    for ticket in full:
        ticket.accepted()
    assert behind.poll()[0] == 0.0 and not behind.probe
    _send(pacer, clock)                      # the lane is at its limit again
    second_probe = pacer.join(LANE, window_s=WINDOW)
    assert second_probe.poll()[0] == 0.0 and second_probe.probe
    second_probe.accepted()
    assert pacer.snapshot()[0]['limit'] is None
    assert pacer.join(LANE, window_s=WINDOW).poll()[0] == 0.0


def test_retry_after_and_an_exhausted_reset_hold_the_whole_lane():
    pacer, clock = _pacer()
    _send(pacer, clock).rejected(RateSignals(retry_after_s=7.0))
    assert pacer.join(LANE, window_s=WINDOW).poll()[0] == pytest.approx(7.0)

    clock.now += 7.0
    _send(pacer, clock).rejected(RateSignals(remaining=0, reset_s=12.5))
    assert pacer.join(LANE, window_s=WINDOW).poll()[0] == pytest.approx(12.5)


def test_a_declared_limit_paces_without_probes():
    pacer, clock = _pacer()
    first = _send(pacer, clock)
    clock.now += 1.0
    second = _send(pacer, clock)
    second.rejected(RateSignals(limit=1, window_s=30.0))
    assert pacer.snapshot()[0]['limit_source'] == 'declared'
    assert pacer.snapshot()[0]['window_s'] == 30.0
    clock.now += 40.0                        # far past any probe time
    queued = pacer.join(LANE, window_s=WINDOW)
    assert queued.poll()[0] == 0.0           # the first send left the window
    behind = pacer.join(LANE, window_s=WINDOW)
    assert behind.poll()[0] == pytest.approx(30.0)
    assert not queued.probe
    del first


def test_cancel_leaves_the_queue_and_wakes_the_waiters_behind():
    pacer, clock = _pacer()
    _learn_limit(pacer, clock, accepted=1)   # limit 1 learned
    ahead = pacer.join(LANE, window_s=WINDOW)
    behind = pacer.join(LANE, window_s=WINDOW)
    before, signal = behind.poll()
    ahead.cancel()
    assert signal.is_set()
    assert behind.poll()[0] < before
    with pytest.raises(RuntimeError):
        ahead.poll()


def test_idle_lanes_keep_learned_limits_under_the_route_health_bound():
    from lib.network_routes import HEALTH_CACHE_ORIGINS, HEALTH_CACHE_TTL_S
    pacer, clock = _pacer()
    busy = pacer.join(('busy',), window_s=WINDOW)
    for index in range(HEALTH_CACHE_ORIGINS + 5):
        _send_on = pacer.join(('lane', index), window_s=WINDOW)
        assert _send_on.poll()[0] == 0.0
        clock.now += WINDOW                  # each lane goes idle
    assert len(pacer._lanes) <= HEALTH_CACHE_ORIGINS + 2
    assert ('busy',) in pacer._lanes and busy.state == 'waiting'
    clock.now += HEALTH_CACHE_TTL_S
    pacer.join(('fresh',), window_s=WINDOW)
    assert set(pacer._lanes) <= {('busy',), ('fresh',)}


def test_a_request_occupies_the_window_from_its_write_not_its_grant():
    """An older request that reaches the provider later frees its room later."""
    pacer, clock = _pacer()
    _send(pacer, clock)                             # sent at 1000
    clock.now += 1.0
    _send(pacer, clock).rejected(RateSignals(limit=1))
    clock.now = 1000.0 + WINDOW
    big = pacer.join(LANE, window_s=WINDOW)
    assert big.poll()[0] == 0.0                     # granted at 1060
    clock.now += 0.4                                # preparing a large body
    with paced_send(big):
        note_wire_send()                            # written at 1060.4
    clock.now += 0.1
    small = pacer.join(LANE, window_s=WINDOW)
    assert small.poll()[0] == pytest.approx(1060.4 + WINDOW - clock.now)
    assert current_paced_send() is None


def test_a_refusals_round_trip_bounds_how_long_a_write_counts():
    """The provider counts a request from when it reaches it, after our write."""
    pacer, clock = _pacer()
    _send(pacer, clock)                             # written at 1000
    clock.now += 1.0
    refused = _send(pacer, clock)                   # written at 1001
    clock.now += 0.25                               # refused 0.25 s later
    refused.rejected(request_limit=True)
    assert pacer.snapshot()[0]['limit'] == 1
    waiting = pacer.join(LANE, window_s=WINDOW)
    assert waiting.poll()[0] == pytest.approx(1000.0 + WINDOW + 0.25 - clock.now)


def test_a_write_outside_a_paced_send_or_after_its_outcome_stamps_nothing():
    pacer, clock = _pacer()
    ticket = _send(pacer, clock)
    clock.now += 5.0
    note_wire_send()
    assert ticket.sent_at == 1000.0
    ticket.accepted()
    with paced_send(ticket):
        note_wire_send()
    assert ticket.sent_at == 1000.0


def test_the_header_wait_thread_inherits_the_paced_send():
    """Sync streams post on a worker thread; the binding must follow it."""
    from lib.llm._transport import post_headers_abortable
    pacer, clock = _pacer()
    ticket = _send(pacer, clock)
    with paced_send(ticket):
        seen = post_headers_abortable(current_paced_send, is_aborted=None)
    assert seen is ticket
    assert post_headers_abortable(current_paced_send, is_aborted=None) is None


@pytest.mark.parametrize('headers, expected', [
    ({'Retry-After': '30'}, RateSignals(retry_after_s=30.0)),
    ({'retry-after': 'Thu, 12 Nov 2026 23:00:30 GMT'},
     RateSignals(retry_after_s=30.0)),
    ({'x-ratelimit-limit-requests': '60', 'x-ratelimit-remaining-requests': '0',
      'x-ratelimit-reset-requests': '1m0.5s'},
     RateSignals(limit=60, remaining=0, reset_s=60.5)),
    ({'x-ratelimit-reset-requests': '20ms', 'x-ratelimit-limit-requests': '5'},
     RateSignals(limit=5, reset_s=0.02)),
    ({'anthropic-ratelimit-requests-limit': '50',
      'anthropic-ratelimit-requests-remaining': '0',
      'anthropic-ratelimit-requests-reset': '2026-11-12T23:00:12Z'},
     RateSignals(limit=50, remaining=0, reset_s=12.0)),
    ({'RateLimit-Limit': '10', 'RateLimit-Remaining': '0',
      'RateLimit-Reset': '25', 'RateLimit-Policy': '10;w=60'},
     RateSignals(limit=10, remaining=0, reset_s=25.0, window_s=60.0)),
    ({'Content-Type': 'application/json'}, None),
    ({'Retry-After': 'soon'}, None),
])
def test_response_rate_signals_read_the_providers_headers(headers, expected):
    now_epoch = 1794524400.0                 # 2026-11-12T23:00:00Z
    assert response_rate_signals(headers, now_epoch=now_epoch) == expected
