"""A key-wide 402 stop holds on every dispatcher selection path.

2026-11-12 10:24-15:18: after key_stats recorded …5409's key-wide stop
(``您的Credit已耗尽``), task rounds, request-scoped route groups for fallback
rounds and Fetch:Extract kept sending to the key: ten more live 402s. The
deployed key admission then let the key's persistent manual ON outrank the
stop (``_raw_enabled_unlocked`` read the override first; 6d028dc5d8 was
reverted by cc34c56eb9, re-landed by 214650e681, live from the 11-12 15:53
restart). Every picker must ask that one admission: the conversation picker,
request-scoped route groups, racing (``pick_top_n``), model discovery
(``pick_best_slots``) and key rotation (``pick_key_for_model``). Pacing-aware
ranking must never prefer the stopped key, however empty its provider window.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

PROV = 'owner:1:example-corp'
DEAD = 'cred_2009fa1132a88c55243ebcd3'      # …5409
LIVE = 'credential-1792419920936-26sj7f'    # …0733
MODEL = 'kimi-k3.1-pro'


@pytest.fixture
def stats(monkeypatch, tmp_path):
    """Real lib.key_stats on an isolated store; both keys are its siblings."""
    import lib.key_stats as ks

    snapshot = dict(ks._cache)
    monkeypatch.setattr(ks, '_STATS_PATH', str(tmp_path / 'key_stats.json'))
    monkeypatch.setattr(ks, '_list_siblings',
                        lambda pid: [f'{PROV}::{DEAD}', f'{PROV}::{LIVE}'])
    ks._cache['day'] = ''
    ks._cache['stats'] = {}
    ks._cache['overrides'] = {}
    ks._cache['loaded'] = False
    from lib.llm_dispatch import conv_affinity
    saved_affinity = dict(conv_affinity._conv_keys)
    conv_affinity._conv_keys.clear()
    yield ks
    ks._cache.clear()
    ks._cache.update(snapshot)
    with conv_affinity._conv_lock:
        conv_affinity._conv_keys.clear()
        conv_affinity._conv_keys.update(saved_affinity)


def _slot(credential, key_name=None, *, deployment='dep_195f'):
    from lib.llm_dispatch.slot import Slot

    return Slot(key_name=key_name or credential, api_key='synthetic-' + credential,
                model=MODEL, capabilities={'text', 'vision'}, provider_id=PROV,
                routing_owner_user_id=1, routing_provider_id='default',
                route_credential_id=credential, route_deployment_id=deployment)


def _dispatcher(slots):
    """Real picker over a real SlotPool (the key-health credential index)."""
    import threading
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    from lib.llm_dispatch.slot_pool import SlotPool

    disp = object.__new__(LLMDispatcher)
    disp._lock = threading.Lock()
    disp._pool = SlotPool()
    disp._pool.publish(list(slots))
    disp._contention_strikes = {}
    disp._logical_index = {}
    disp._direct_models = set()
    disp.initialize = lambda: None
    return disp


def _stop(ks, *, manual_on):
    """Record the provider's key-wide credit refusal, as dispatch does."""
    if manual_on:
        ks.set_key_override(PROV, DEAD, True)
    ks.mark_key_exhausted(PROV, DEAD, reason='API HTTP 402: 您的Credit已耗尽')


@pytest.mark.parametrize('manual_on', [False, True],
                         ids=['no-override', 'stale-manual-on'])
def test_every_picker_refuses_a_key_with_a_recorded_402_stop(stats, manual_on):
    dead, live = _slot(DEAD), _slot(LIVE, deployment='dep_m17')
    live.latency_ema = 99999.0          # the dead key wins on score alone
    disp = _dispatcher([dead, live])
    assert disp._pick('text', MODEL, None, None) is dead
    dead.release()
    _stop(stats, manual_on=manual_on)

    chosen = disp._pick('text', MODEL, None, None)
    assert chosen is live
    chosen.release()
    raced = disp.pick_top_n(n=2, prefer_model=MODEL, reserve=False)
    assert [slot.key_name for slot in raced] == [LIVE]
    assert [slot.key_name for slot in disp.pick_best_slots('vision', n=5)] == [LIVE]
    assert disp.pick_key_for_model(MODEL)[1] == LIVE


def test_a_request_scoped_route_group_refuses_the_stopped_credential(stats):
    """Fallback rounds run on per-request groups naming the same credential."""
    group = [_slot(DEAD, 'ephemeral_f59754d25226c50a'),
             _slot(LIVE, 'ephemeral_a7dac283997f6773', deployment='dep_m17')]
    group[1].latency_ema = 99999.0
    disp = _dispatcher([_slot(DEAD), _slot(LIVE, deployment='dep_m17')])
    disp._selection_scope = lambda pinned: group   # the request's own group
    _stop(stats, manual_on=True)
    chosen = disp._pick('text', MODEL, None, None)
    assert chosen is group[1]
    chosen.release()
    assert disp.pick_key_for_model(MODEL)[1] == 'ephemeral_a7dac283997f6773'
    assert [slot.key_name for slot in disp.pick_top_n(
        n=2, prefer_model=MODEL, reserve=False)] == ['ephemeral_a7dac283997f6773']


def test_past_the_day_boundary_one_reserving_pick_probes_the_stopped_key(
        stats, monkeypatch):
    """One real request at a time; served ends the stop, others wait it out."""
    day = ['2026-11-12']
    monkeypatch.setattr(stats, '_today', lambda: day[0])
    dead, live = _slot(DEAD), _slot(LIVE, deployment='dep_m17')
    live.latency_ema = 99999.0          # the dead key wins on score alone
    disp = _dispatcher([dead, live])
    _stop(stats, manual_on=False)
    assert disp._pick('text', MODEL, None, None, reserve=True) is live
    live.release()

    day[0] = '2026-11-13'
    probe = disp._pick('text', MODEL, None, None, reserve=True)
    assert probe is dead
    # While the probe is in flight every other request is refused the key,
    # and pickers that reserve nothing never probe.
    assert disp._pick('text', MODEL, None, None, reserve=True) is live
    live.release()
    assert disp._pick('text', MODEL, None, None) is live
    assert disp.pick_key_for_model(MODEL)[1] == LIVE
    assert [s.key_name for s in disp.pick_top_n(
        n=2, prefer_model=MODEL, reserve=False)] == [LIVE]

    # No answer (abort, timeout): the next real request is the probe.
    probe.release()
    assert disp._pick('text', MODEL, None, None, reserve=True) is dead
    # Served: the stop ends for every picker.
    dead.record_success(latency_ms=10)
    assert stats.is_key_enabled(PROV, DEAD) is True
    assert disp.pick_key_for_model(MODEL)[1] == DEAD
    assert disp._pick('text', MODEL, None, None, reserve=True) is dead
    assert disp._pick('text', MODEL, None, None, reserve=True) is dead


def test_pacing_room_never_brings_back_a_stopped_key(stats):
    """The stopped key's lane is empty because nothing is sent to it."""
    from lib.http_transport.provider_pacing import RateSignals, shared_pacer
    dead, live = _slot(DEAD), _slot(LIVE, deployment='dep_m17')
    disp = _dispatcher([dead, live])
    lane = disp.pacing_lane(live)
    served = shared_pacer().join(lane, window_s=60.0)
    assert served.poll()[0] == 0.0
    refused = shared_pacer().join(lane, window_s=60.0)
    assert refused.poll()[0] == 0.0
    refused.rejected(RateSignals(limit=1))
    shared_pacer().join(lane, window_s=60.0)      # the live key's queue is deep
    assert shared_pacer().admission_delay(lane) >= 60.0
    assert shared_pacer().admission_delay(disp.pacing_lane(dead)) == 0.0
    _stop(stats, manual_on=False)
    assert disp._pick('text', MODEL, None, None) is live
