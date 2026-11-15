"""Storage fence signals over a real storage authority (docs/ROUTER.md R7).

While storage is fenced no storage-backed alert can be written, so the fence
episode is reported once readiness returns: one ``storage`` signal and one
wake per owner that has a Watchdog, naming since/until, the duration, the
cause and the last restart error. 2026-11-11: a 92-minute fence left no
owner-visible trace outside the logs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = [pytest.mark.unit,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)]

SINCE = datetime(2026, 11, 11, 11, 54, 44, tzinfo=timezone.utc).timestamp()
EPISODE = {
    'since': SINCE,
    'until': SINCE + 360.0,
    'cause': 'sidecar exited unexpectedly (-1)',
    'lastRestartError': ('RuntimeError: external storage sidecar startup timed out; last error: '
                         'RuntimeError: storage connection file is not available'),
    'restartAttempts': 4,
}


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
    # The wake's delivery seam only; the producer and the signal store are real.
    monkeypatch.setattr('lib.scheduler.conversation_dispatch.dispatch_scheduled_turn', dispatch)
    return SimpleNamespace(owner=owner, watchdog=watchdog, woken=woken)


def test_a_fence_episode_is_one_storage_signal_and_one_wake(world):
    from lib.router.watchdog.signals import FaultSignals
    from lib.router.watchdog.storage import report_storage_fence_to_owners

    report_storage_fence_to_owners(EPISODE)
    # A replay of the same episode is the same signal, not a second wake.
    report_storage_fence_to_owners(EPISODE)
    [signal] = FaultSignals(world.owner).open_page()['signals']
    assert signal['source'] == 'storage' and signal['owner'] == world.owner
    assert signal['subject'] == {'role': 'storage'}
    assert signal['error']['kind'] == 'storage_fenced'
    for fact in ('360s', '2026-11-11 11:54:44 UTC', '2026-11-11 12:00:44 UTC',
                 'sidecar exited unexpectedly (-1)', 'storage connection file is not available',
                 '4 restart attempt'):
        assert fact in signal['error']['detail'], fact
    assert 'server-manager.log' in signal['error']['hint']
    [(conversation_id, message, options)] = world.woken
    assert conversation_id == world.watchdog['conversationId']
    assert options['command_id'] == 'router-watchdog-signal:' + signal['signalId']
    assert message['_initiator'] == 'peer' and 'source=storage' in message['content']


def test_owners_without_a_watchdog_record_nothing_and_reports_never_raise(world):
    from lib.router.watchdog.signals import FaultSignals
    from lib.router.watchdog.storage import report_storage_fence_to_owners

    assert report_storage_fence_to_owners(EPISODE, owners=[world.owner + 1]) == []
    assert report_storage_fence_to_owners({'since': 'not a time'}) == []
    assert FaultSignals(world.owner).open_page()['signals'] == []
    assert world.woken == []


def test_only_the_request_services_process_reports_fences(world):
    from lib.router.watchdog.storage import register_storage_fence_reports, report_storage_fence_to_owners

    class Runtime:
        def __init__(self):
            self.callbacks = []

        def add_fence_recovery_callback(self, callback):
            self.callbacks.append(callback)

    api, execution, combined = Runtime(), Runtime(), Runtime()
    assert register_storage_fence_reports(api, process_role='api') is True
    assert register_storage_fence_reports(execution, process_role='execution') is False
    assert register_storage_fence_reports(combined, process_role='all') is True
    assert api.callbacks == [report_storage_fence_to_owners]
    assert execution.callbacks == []


def test_a_fence_is_its_own_storage_episode_under_the_condition_aware_contract(
        world, monkeypatch):
    """The fence keeps its own namespace beside the authority's storage episodes.

    The condition-aware ``_report`` (FAULT-INSPECTION / WATCHDOG-CARRY) unpacks
    ``(identity, subject, error, condition, episode_parts[, fault])``; this
    base reads the first three. A fence is a closed event keyed by its start
    under a ``fence`` tag, grouped in its own ``fence`` episode, so it never
    folds into the ``authority`` episodes the supervisor reports.
    """
    from lib.router.watchdog import storage as storage_signals

    built = []

    def condition_aware_report(owner, source, build, **_kwargs):
        identity, subject, error, condition, episode_parts, *fault = build(owner, None)
        built.append((source, identity, subject, error['kind'], condition, episode_parts, fault))
        return {'signalId': 'captured'}

    monkeypatch.setattr(storage_signals, '_report', condition_aware_report)
    assert storage_signals.report_storage_fence(world.owner, EPISODE) == {'signalId': 'captured'}
    [(source, identity, subject, kind, condition, episode_parts, fault)] = built
    assert source == 'storage' and subject == {'role': 'storage'}
    assert identity == ['fence', round(SINCE * 1000)]
    assert kind == 'storage_fenced'
    assert condition == {'kind': 'event'} and episode_parts == ['fence'] and fault == []
