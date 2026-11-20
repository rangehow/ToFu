"""Quota-watch fault signals over a real storage authority (docs/ROUTER.md R7).

Every process that runs conversation rounds keeps its own quota watch, so one
throttled (AppId, model) lane opens and closes episodes in each of them. The
Watchdog producer merges them into one alert per lane: its closed-to-open
transition is one fault signal of source ``quota`` and one wake; an open while
it is open (another process, or a replay) joins it and wakes nothing; it
closes, and its signal resolves, with its last live holder. The signal and the
wake carry the AppId only by the last four characters the provider itself
shows; never the key, its hint or the credential identity. A Watchdog Turn's
own dispatches step off an AppId with an open alert.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = [pytest.mark.unit,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)]

T0 = 1_794_200_000.0
API_KEY = 'APPSECRET-9f3c2e71b4d8-0733'
CREDENTIAL = 'credential-1792419920936-26sj7f'
REPLAY = Path(__file__).resolve().parents[1] / 'fixtures' / 'router' / 'quota_watch_7d3ec336.json'


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


def _episode(owner, reporter='shard-a'):
    """One open and one close transition from the real watch for (…0733, claude-opus-5.5)."""
    from lib.llm.llm_errors import RateLimitError
    from lib.llm_dispatch.quota_watch import QuotaWatch, production_dispatch
    from lib.llm_dispatch import quota_watch
    from lib.llm_dispatch.slot import Slot
    clock = [T0]
    delivered = []
    watch = QuotaWatch(clock=lambda: clock[0], deliver=delivered.append, reporter=reporter)
    slot = Slot(key_name=f'{CREDENTIAL}:deployment-claude', api_key=API_KEY, model='claude-opus-5.5',
                capabilities={'text'}, provider_id='sankuai', route_credential_id=CREDENTIAL)
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
                                lane=transition['lane'], credential=transition['credential'],
                                key_tail=transition['keyTail'], model=transition['model'],
                                opened_at_ms=transition['openedAtMs'], detail=transition['detail'],
                                reporter=transition['reporter'], dispatch=world.dispatch)


def _close(world, transition):
    from lib.router.watchdog.quota import resolve_quota_episode
    return resolve_quota_episode(transition['owner'], lane=transition['lane'],
                                 reporter=transition['reporter'], resolution=transition['resolution'])


def test_a_quota_episode_is_one_signal_one_wake_and_its_producer_closes_it(world):
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
    assert _close(world, closed) is True
    resolved = FaultSignals(world.owner).get(signal['signalId'])
    assert resolved['state'] == 'resolved' and resolved['resolution'].startswith('Recovered')
    assert FaultSignals(world.owner).open_page()['signals'] == []
    assert len(world.woken) == 1


def test_episodes_of_one_lane_in_several_processes_are_one_alert_until_the_last_closes(world):
    from lib.router.watchdog.signals import FaultSignals
    first_open, first_close = _episode(world.owner, reporter='shard-a')
    second_open, second_close = _episode(world.owner, reporter='shard-b')
    assert first_open['lane'] == second_open['lane']
    signal = _report(world, first_open)
    # Same alert, lane and state: the second process joins it and wakes nothing.
    assert _report(world, {**second_open, 'openedAtMs': second_open['openedAtMs'] + 5000}) == signal
    assert len(world.woken) == 1
    assert _close(world, first_close) is False
    assert FaultSignals(world.owner).get(signal['signalId'])['state'] == 'open'
    assert _close(world, second_close) is True
    assert FaultSignals(world.owner).get(signal['signalId'])['state'] == 'resolved'
    # A later open is a real state change: a new signal and one more wake.
    reopened = _report(world, {**first_open, 'openedAtMs': first_open['openedAtMs'] + 120_000})
    assert reopened['signalId'] != signal['signalId'] and reopened['state'] == 'open'
    assert len(world.woken) == 2


def test_a_holder_proved_dead_no_longer_holds_the_alert_open(world):
    from lib.process_owner import process_owner
    from lib.router.watchdog.signals import FaultSignals
    from lib.storage.generated_worker_job_contract import WORKER_PROCESS_OWNER_PREFIX
    from lib.storage.worker_process import parse_worker_process
    process = process_owner()
    if process is None:
        pytest.skip('no process identity on this host')
    # This pid with another start time: the kernel proves that process is gone.
    dead = (WORKER_PROCESS_OWNER_PREFIX + ':'.join(str(process[field]) for field in
                                                   ('boot', 'pid_namespace', 'pid'))
            + f':{int(process["started"]) + 1}:' + 'f' * 32)
    assert parse_worker_process(dead) is not None
    opened, _closed = _episode(world.owner, reporter=dead)
    stale = _report(world, opened)
    live_open, live_close = _episode(world.owner, reporter='shard-live')
    fresh = _report(world, {**live_open, 'openedAtMs': live_open['openedAtMs'] + 1000})
    assert fresh['signalId'] != stale['signalId'] and len(world.woken) == 2
    assert FaultSignals(world.owner).get(stale['signalId'])['state'] == 'resolved'
    assert _close(world, live_close) is True
    assert FaultSignals(world.owner).get(fresh['signalId'])['state'] == 'resolved'


def test_quota_reports_never_raise_into_the_watch(world):
    from lib.router.watchdog.quota import report_quota_episode, resolve_quota_episode
    opened, _closed = _episode(world.owner)
    # An owner without a Watchdog records nothing; an unknown lane closes nothing.
    assert report_quota_episode(world.owner + 1, provider_id='sankuai', lane=opened['lane'],
                                credential=CREDENTIAL, key_tail='0733', model='claude-opus-5.5',
                                opened_at_ms=1, detail='x', reporter='shard-a', dispatch=world.dispatch) is None
    assert resolve_quota_episode(world.owner, lane='0' * 16, reporter='shard-a', resolution='Recovered.') is False
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


def test_a_watchdog_dispatch_skips_an_appid_with_an_open_quota_alert(world, monkeypatch):
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    from lib.llm_dispatch.quota_watch import avoid_throttled_appids
    from lib.llm_dispatch.slot import Slot
    monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *_a, **_kw: True)

    def slot(credential, deployment, latency):
        return Slot(key_name=f'{credential}:{deployment}', api_key=f'sk-{credential}', model='kimi-k3.1-pro',
                    logical_model='kimi-k3.1-pro', capabilities={'text'}, provider_id='sankuai',
                    route_credential_id=credential, route_deployment_id=deployment, latency_ema=latency)
    throttled, quiet = slot(CREDENTIAL, 'dep-throttled', 100.0), slot('credential-quiet', 'dep-quiet', 9000.0)
    dispatcher = LLMDispatcher()
    dispatcher.slots = [throttled, quiet]
    dispatcher._initialized = True
    opened, _closed = _episode(world.owner)
    assert opened['credential'] == CREDENTIAL
    _report(world, opened)

    def pick():
        return dispatcher.pick_slot(capability='text', prefer_model='kimi-k3.1-pro', strict_model=True)
    assert pick() is throttled      # every other dispatch is unchanged
    with avoid_throttled_appids(world.owner):
        assert pick() is quiet
        dispatcher.slots = [throttled]
        assert pick() is throttled  # the only route still serves: avoidance never empties the pool
    with avoid_throttled_appids(world.owner + 1):
        dispatcher.slots = [throttled, quiet]
        assert pick() is throttled  # alerts are the owner's own


def _lane_digest(provider, key_tail, model):
    return hashlib.sha256(json.dumps([provider, key_tail, model]).encode()).hexdigest()[:16]


def _replay_reporters(events):
    """Assign each logged transition to the process watch that made it.

    The log names the task (rid) whose round caused a transition, not the
    process. A shard runs one task at a time and later tasks after it, and a
    process's transitions on one lane alternate open/close. A task's first
    transition that is a close therefore belongs to an open process whose
    earlier task has no later transition; which one does not change the
    merged state.
    """
    last_index = {event['rid']: index for index, event in enumerate(events)}
    process_of, task_of, state, last_seen, assigned = {}, {}, defaultdict(lambda: 'close'), {}, []
    for index, event in enumerate(events):
        lane = (event['providerId'], event['keyTail'], event['model'])
        process = process_of.get(event['rid'])
        if process is None:
            if event['kind'] == 'open':
                process = f'replay-shard-{len(task_of)}'
            else:
                process = min((p for (p, l), s in state.items()
                               if l == lane and s == 'open' and last_index[task_of[p]] < index),
                              key=lambda p: last_seen[p])
            process_of[event['rid']] = process
            task_of[process] = event['rid']
        assert state[(process, lane)] != event['kind'], (index, event)
        state[(process, lane)] = event['kind']
        last_seen[process] = index
        assigned.append(process)
    return assigned


def test_replay_of_the_7d3ec336_throttling_wakes_once_per_real_state_change(world):
    """Production replay, 2026-11-18 08:33-10:34 CST (LEAD_METRICS_STAGE2 S2-2).

    Before: each of the 73 episode opens (71 on …5409) was a new signal and a
    wake; the Watchdog ran 51 Turns for $55.78, 45 of them woken by quota
    signals. After: a wake only where the lane's merged alert went from closed
    to open, and the Watchdog Turns those wakes started.
    """
    from lib.router.watchdog.signals import FaultSignals
    replay = json.loads(REPLAY.read_text(encoding='utf-8'))
    events = replay['quotaEvents']
    assert len(events) == 133
    assert sum(e['kind'] == 'open' and e['keyTail'] == '5409' for e in events) == 71
    day = datetime(2026, 11, 18)

    def at_ms(clock):
        return int((datetime.combine(day, datetime.strptime(clock, '%H:%M:%S').time())).timestamp() * 1000)

    holders, real_opens, waking_opens = defaultdict(set), [], []
    for event, reporter in zip(events, _replay_reporters(events)):
        lane = _lane_digest(event['providerId'], event['keyTail'], event['model'])
        transition = {'owner': world.owner, 'providerId': event['providerId'], 'lane': lane,
                      'credential': f'credential-{event["keyTail"]}', 'keyTail': event['keyTail'],
                      'model': event['model'], 'reporter': reporter}
        before = len(world.woken)
        if event['kind'] == 'open':
            if not holders[lane]:
                real_opens.append(event)
            holders[lane].add(reporter)
            _report(world, {**transition, 'openedAtMs': at_ms(event['at']), 'detail': 'replay'})
        else:
            holders[lane].discard(reporter)
            _close(world, {**transition, 'resolution': 'Recovered (replay).'})
        if len(world.woken) > before:
            waking_opens.append(event)
    # At most one wake per real state change, and exactly one per closed-to-open transition.
    assert waking_opens == real_opens
    assert len(world.woken) == 6
    open_signals = FaultSignals(world.owner).open_page()['signals']
    assert len(open_signals) == 3  # …5409 kimi, …5409 gemini, …4861 flash stay held at the window end

    # The Watchdog Turns of the window, before and after.
    turns = replay['watchdogTurns']
    assert (len(turns), round(sum(t['costUsd'] for t in turns), 2)) == (51, 55.78)
    assert sum(t['rounds'] for t in turns) == 136 and sum(t['roundsOffPrimary'] for t in turns) == 63
    opens_at = {e['at'] for e in events if e['kind'] == 'open'}
    waking_at = {e['at'] for e in waking_opens}

    def cst(utc):
        return (datetime.strptime(utc, '%H:%M:%S') + timedelta(hours=8)).strftime('%H:%M:%S')
    quota_turns = [t for t in turns if t['signalSource'] == 'quota']
    assert len(quota_turns) == 45 and all(cst(t['signalOpenedUtc']) in opens_at for t in quota_turns)
    kept = [t for t in turns if t['signalSource'] != 'quota' or cst(t['signalOpenedUtc']) in waking_at]
    after = (len(kept), round(sum(t['costUsd'] for t in kept), 2))
    print(f'Watchdog Turns 08:33-10:34: before 51 / $55.78, after {after[0]} / ${after[1]}')
    assert after == (12, 17.35)


def test_only_a_watchdog_turn_dispatches_inside_the_appid_avoidance(world, monkeypatch, tmp_path):
    from lib.llm_dispatch.quota_watch import throttled_appids
    from lib.tasks_pkg.manager import _stream
    opened, _closed = _episode(world.owner)
    _report(world, opened)
    seen = []
    def dispatch(*args, **kwargs):
        seen.append(throttled_appids())
        return {'role': 'assistant', 'content': 'ok'}, 'stop', {}

    monkeypatch.setattr(_stream, 'dispatch_stream', dispatch)
    watchdog_turn = {'id': 't-watchdog', '_userId': world.owner,
                     'config': {'projectPath': world.watchdog['projectPath']}}
    worker_turn = {'id': 't-worker', '_userId': world.owner, 'config': {'projectPath': str(tmp_path)}}
    for task in (watchdog_turn, worker_turn):
        result = _stream._watchdog_aware_dispatch_stream(task, {})
        assert result.message == {'role': 'assistant', 'content': 'ok'}
    assert seen == [frozenset({('sankuai', CREDENTIAL)}), None]


# ── Settlement: alerts close on evidence when no holder's episode can close them ──


def _lane_alert(world, *, provider='provider-a', credential='credential-a', model='alpha-wire-a',
                key_tail='aaaa', reporter='shard-live', opened_at_ms=1_794_200_000_000):
    """Open one producer alert as a live process's watch would, and return its signal."""
    lane = hashlib.sha256(json.dumps([provider, credential, model]).encode()).hexdigest()[:16]
    return _report(world, {'owner': world.owner, 'providerId': provider, 'lane': lane, 'credential': credential,
                           'keyTail': key_tail, 'model': model, 'openedAtMs': opened_at_ms,
                           'detail': 'rejected and held', 'reporter': reporter})


def _routing(document_change=None):
    """The owner's model routing: provider-a's credential-a serves alpha-wire-a."""
    import copy
    from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary
    from tests.model_routing.test_model_routing_contract import _full_document
    from tests.support.model_registration import complete_registration
    document = complete_registration(_full_document())
    if document_change is not None:
        document_change(document)
    repository = InMemoryModelRoutingRepository()
    owners = {}

    class _PerOwner:
        def get(self, boundary):
            if boundary.owner_user_id not in owners:
                repository.compare_and_swap(boundary, copy.deepcopy(document), expected_revision=0)
                owners[boundary.owner_user_id] = True
            return repository.get(boundary)
    assert OwnerBoundary.create(1).owner_user_id == 1
    return _PerOwner()


def _unroutable(repository, *, key_enabled=True):
    from functools import partial
    from lib.router.watchdog.quota import lane_unroutable
    return partial(lane_unroutable, repository=repository, key_enabled=lambda *_a, **_kw: key_enabled)


def test_an_alert_whose_lane_is_still_routable_and_held_stays_open(world):
    """Genuinely ongoing: a live holder and a routable lane are no evidence to close anything."""
    from lib.router.watchdog.quota import open_quota_appids, settle_quota_alerts
    from lib.router.watchdog.signals import FaultSignals
    signal = _lane_alert(world)
    assert settle_quota_alerts(world.owner, unroutable=_unroutable(_routing())) == []
    assert FaultSignals(world.owner).get(signal['signalId'])['state'] == 'open'
    assert open_quota_appids(world.owner) == frozenset({('provider-a', 'credential-a')})


@pytest.mark.parametrize('reserve', [
    pytest.param(lambda d: d['credentials'][0].update(enabled=False), id='credential-disabled'),
    pytest.param(lambda d: d['credentials'][0]['authorization'].update(connection_ids=[]), id='reserved-off-connection'),
    pytest.param(lambda d: d['credentials'][0]['authorization']['models'][0].update(
        excluded_wire_model_ids=['alpha-wire-a']), id='wire-model-excluded'),
])
def test_a_reserved_key_supersedes_its_alert_by_policy(world, reserve):
    from lib.router.watchdog.quota import open_quota_appids, settle_quota_alerts
    from lib.router.watchdog.signals import FaultSignals
    signal = _lane_alert(world)
    assert settle_quota_alerts(world.owner, unroutable=_unroutable(_routing(reserve))) == [signal['signalId']]
    resolved = FaultSignals(world.owner).get(signal['signalId'])
    assert resolved['state'] == 'resolved'
    assert resolved['resolution'].startswith('Superseded by policy') and 'no probe' in resolved['resolution']
    assert open_quota_appids(world.owner) == frozenset()
    assert len(world.woken) == 1   # closing wakes nobody
    # The holder's own later close finds nothing left to close; a new episode is a new alert.
    assert _lane_alert(world, opened_at_ms=1_794_200_900_000)['signalId'] != signal['signalId']


def test_a_key_the_dispatcher_no_longer_admits_supersedes_its_alert(world):
    """…5409 out of credit: a billing stop disables the key for the day, so nothing routes there."""
    from lib.router.watchdog.quota import settle_quota_alerts
    from lib.router.watchdog.signals import FaultSignals
    signal = _lane_alert(world)
    settle_quota_alerts(world.owner, unroutable=_unroutable(_routing(), key_enabled=False))
    resolved = FaultSignals(world.owner).get(signal['signalId'])
    assert resolved['state'] == 'resolved' and 'no longer admits this key' in resolved['resolution']


def test_an_unreadable_routing_authority_or_unknown_credential_proves_nothing(world):
    from lib.router.watchdog.quota import lane_unroutable
    alert = {'providerId': 'provider-a', 'credential': 'configured-pool-key', 'model': 'alpha-wire-a'}
    always = lambda *_a, **_kw: True  # noqa: E731

    class _Down:
        def get(self, boundary):
            raise RuntimeError('authority unavailable')
    assert lane_unroutable(world.owner, alert, repository=_routing(), key_enabled=always) is None
    assert lane_unroutable(world.owner, {**alert, 'credential': 'credential-a'}, repository=_Down(),
                           key_enabled=always) is None


def test_a_dead_holder_alert_closes_on_settlement_without_another_report(world):
    from lib.process_owner import process_owner
    from lib.router.watchdog.quota import settle_quota_alerts
    from lib.router.watchdog.signals import FaultSignals
    from lib.storage.generated_worker_job_contract import WORKER_PROCESS_OWNER_PREFIX
    process = process_owner()
    if process is None:
        pytest.skip('no process identity on this host')
    dead = (WORKER_PROCESS_OWNER_PREFIX + ':'.join(str(process[field]) for field in
                                                   ('boot', 'pid_namespace', 'pid'))
            + f':{int(process["started"]) + 1}:' + 'f' * 32)
    signal = _lane_alert(world, reporter=dead)
    assert settle_quota_alerts(world.owner, unroutable=_unroutable(_routing())) == [signal['signalId']]
    resolved = FaultSignals(world.owner).get(signal['signalId'])
    assert resolved['state'] == 'resolved' and 'has exited' in resolved['resolution']


def test_quota_signals_no_open_alert_holds_are_superseded(world):
    """The live shape: signals from before the per-lane alert record, whose processes exited at a restart."""
    from lib.router.watchdog.quota import QUOTA_ERROR_KIND, settle_quota_alerts
    from lib.router.watchdog.signals import TURN_FAULT_KINDS, FaultSignals
    signals = FaultSignals(world.owner)
    legacy = [signals.record('quota', ['sankuai', f'lane-{n}', 1_794_960_000_000 + n],
                             {'role': 'quota', 'keyTail': '5409', 'model': 'kimi-k3.1-pro'},
                             {'kind': QUOTA_ERROR_KIND, 'detail': 'legacy', 'hint': ''})[0]
              for n in range(3)]
    held = _lane_alert(world)
    other = signals.record('turn', ['c', 't', 'a'], {'role': 'worker', 'conversationId': 'c'},
                           {'kind': sorted(TURN_FAULT_KINDS)[0], 'detail': '', 'hint': ''})[0]
    resolved = settle_quota_alerts(world.owner, unroutable=_unroutable(_routing()))
    assert sorted(resolved) == sorted(signal['signalId'] for signal in legacy)
    for signal in legacy:
        after = signals.get(signal['signalId'])
        assert after['state'] == 'resolved' and after['resolution'].startswith('Superseded')
    assert signals.get(held['signalId'])['state'] == 'open'
    assert signals.get(other['signalId'])['state'] == 'open'


def test_settlement_never_raises(world):
    from lib.router.watchdog.quota import settle_quota_alerts
    _lane_alert(world)

    def broken(*_a, **_kw):
        raise RuntimeError('boom')
    assert settle_quota_alerts(world.owner, unroutable=broken) == []
