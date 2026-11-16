"""Quota-watch fault signals over a real storage authority (docs/ROUTER.md R7).

One production quota-watch episode of one (AppId, model) lane is one Watchdog
fault signal of source ``quota``: recorded once, waking the Watchdog once,
closed by the producer when the watch observes recovery. The signal and the
wake carry the AppId only by the last four characters the provider itself
shows; never the key, its hint or the credential identity.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = [pytest.mark.unit,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)]

T0 = 1_794_200_000.0
API_KEY = 'APPSECRET-9f3c2e71b4d8-0733'
CREDENTIAL = 'credential-1792419920936-26sj7f'


@pytest.fixture
def world(semantic_authority, monkeypatch, tmp_path):
    from lib.router.binding import global_router
    from lib.router.watchdog.binding import router_watchdog
    from lib.scheduler.conversation_dispatch import ScheduledTurnDispatch
    client, owner, _engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    monkeypatch.setattr('lib.scheduler.conversation_dispatch.get_storage_client', client)
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    global_router(owner, create=True)
    watchdog = router_watchdog(owner, create=True)
    woken = []

    def dispatch(conversation_id, message, tools_config, **kwargs):
        woken.append((conversation_id, message, kwargs))
        return ScheduledTurnDispatch('started', task_id=f'wake-{len(woken)}')
    return SimpleNamespace(owner=owner, watchdog=watchdog, woken=woken, dispatch=dispatch)


def _episode(owner):
    """One open and one close transition from the real watch for (…0733, claude-opus-5.5)."""
    from lib.llm.llm_errors import RateLimitError
    from lib.llm_dispatch.quota_watch import QuotaWatch, production_dispatch
    from lib.llm_dispatch import quota_watch
    from lib.llm_dispatch.slot import Slot
    clock = [T0]
    delivered = []
    watch = QuotaWatch(clock=lambda: clock[0], deliver=delivered.append)
    slot = Slot(key_name=f'{CREDENTIAL}:deployment-claude', api_key=API_KEY, model='claude-opus-5.5',
                capabilities={'text'}, provider_id='default', route_credential_id=CREDENTIAL)
    rejection = RateLimitError('API HTTP 429: {"status":429,"message":"App:**0733在模型:claude-opus-5.5'
                               '每分钟请求次数超过限制","data":null}', status_code=429, is_shared_contention=True)
    original, quota_watch._WATCH = quota_watch._WATCH, watch
    try:
        with production_dispatch(owner):
            quota_watch.observe_rejection(slot, rejection, queue_wait_ms=0.0)
            clock[0] = T0 + 2
            quota_watch.observe_settlement(slot, ttft_ms=2600.0, queue_wait_ms=1800.0)
            clock[0] = T0 + 63
            quota_watch.observe_settlement(slot, ttft_ms=1500.0, queue_wait_ms=0.0)
    finally:
        quota_watch._WATCH = original
    return delivered


def _report(world, transition):
    from lib.router.watchdog.quota import report_quota_episode
    return report_quota_episode(transition['owner'], provider_id=transition['providerId'],
                                lane=transition['lane'], key_tail=transition['keyTail'],
                                model=transition['model'], opened_at_ms=transition['openedAtMs'],
                                detail=transition['detail'], dispatch=world.dispatch)


def test_a_quota_episode_is_one_signal_one_wake_and_its_producer_closes_it(world):
    from lib.router.watchdog.quota import resolve_quota_episode
    from lib.router.watchdog.signals import FaultSignals
    from lib.secret_envelope import secret_hint
    opened, closed = _episode(world.owner)
    assert (opened['kind'], closed['kind']) == ('open', 'close')
    signal = _report(world, opened)
    assert signal['source'] == 'quota' and signal['state'] == 'open'
    assert signal['subject'] == {'role': 'quota', 'keyTail': '0733', 'model': 'claude-opus-5.5'}
    assert signal['error']['kind'] == 'production_quota_contention'
    assert '…0733' in signal['error']['detail'] and 'held' in signal['error']['detail']
    assert 'watchdog_notify_router' in signal['error']['hint']
    # A replayed delivery is the same signal and no second wake.
    assert _report(world, opened)['signalId'] == signal['signalId']
    [(conversation_id, message, options)] = world.woken
    assert conversation_id == world.watchdog['conversationId']
    assert options['command_id'] == 'router-watchdog-signal:' + signal['signalId']
    assert message['_initiator'] == 'peer' and 'production_quota_contention' in message['content']
    rendered = json.dumps([signal, message], ensure_ascii=False)
    for secret in (API_KEY, API_KEY[:-4], secret_hint(API_KEY), CREDENTIAL):
        assert secret not in rendered
    assert resolve_quota_episode(world.owner, signal['signalId'], closed['resolution']) is True
    resolved = FaultSignals(world.owner).get(signal['signalId'])
    assert resolved['state'] == 'resolved' and resolved['resolution'].startswith('Recovered')
    assert FaultSignals(world.owner).open_page()['signals'] == []
    assert len(world.woken) == 1


def test_quota_reports_never_raise_into_the_watch(world):
    from lib.router.watchdog.quota import report_quota_episode, resolve_quota_episode
    opened, _closed = _episode(world.owner)
    # An owner without a Watchdog records nothing; an unknown signal is not resolved.
    assert report_quota_episode(world.owner + 1, provider_id='default', lane=opened['lane'], key_tail='0733',
                                model='claude-opus-5.5', opened_at_ms=1, detail='x', dispatch=world.dispatch) is None
    assert resolve_quota_episode(world.owner, 'unknown-signal', 'Recovered.') is False
    assert world.woken == []


def test_the_watch_delivers_open_then_close_off_the_dispatch_thread(world, monkeypatch):
    """The default delivery reaches the producer in order: the close resolves the signal the open made."""
    import threading
    from lib.llm_dispatch import quota_watch
    from lib.router.watchdog import quota as quota_signals
    from lib.router.watchdog.signals import FaultSignals
    calls = []
    original_report = quota_signals.report_quota_episode

    def _report_on_thread(*args, **kwargs):
        calls.append(threading.current_thread() is threading.main_thread())
        return original_report(*args, **{**kwargs, 'dispatch': world.dispatch})

    monkeypatch.setattr(quota_signals, 'report_quota_episode', _report_on_thread)
    opened, closed = _episode(world.owner)
    for transition in (opened, closed):
        quota_watch._deliver_to_watchdog(transition)
    # Each lane's last delivery thread joins its predecessor before it runs.
    for thread in list(quota_watch._DELIVERIES.values()):
        thread.join(timeout=60)
    assert calls == [False]
    signal_id = opened['delivery']['signalId']
    assert FaultSignals(world.owner).get(signal_id)['state'] == 'resolved'
    assert len(world.woken) == 1
