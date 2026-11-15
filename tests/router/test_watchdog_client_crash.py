"""Browser crash fault signals over a real storage authority (docs/LOGGING.md).

The stored crash record is the evidence; the Watchdog signal is the wake. The
real producer runs here, never a monkeypatched stand-in, so a producer that
does not exist fails this spec. One crash episode is one page instance: the
reloaded tab's unclean-exit report and Chrome's own report of the same crash
open one ``client`` signal and one wake, whichever arrives first. The signal
carries the page instance, view, classification and time, never page content.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = [pytest.mark.unit,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)]

PAGE = '0b5f6c6e-1d1b-4b8e-9f43-1f0a9c3c2d10'
CONTENT = 'owner draft about the quarterly plan'


def _unclean_exit(page_instance_id: str = PAGE) -> dict:
    return {
        'schema': 'tofu.client-crash/v1', 'pageInstanceId': page_instance_id,
        'classification': 'renderer_crash_suspected', 'startedAt': 1794186600000,
        'lastSeenAt': 1794187396000, 'detectedAt': 1794187420000, 'navigationType': 'reload',
        'environment': {'userAgent': 'Mozilla/5.0 Chrome/155.0.0.0', 'extensions': []},
        'context': {'at': 1794187300000, 'view': 'chat', 'conversationId': 'conv-crashed',
                    'streamActive': True},
        'freeze': {'at': 1794187396000, 'evidence': {'event': 'browser.main_thread_stall', 'durationMs': 24300}},
        'errors': [{'at': 1794187000000, 'kind': 'error', 'message': CONTENT, 'count': 1,
                    'lastAt': 1794187000000}],
        'droppedErrors': 0,
    }


def _browser_report(page_instance_id: str = PAGE) -> list[dict]:
    return [{'type': 'crash', 'age': 42, 'url': 'http://localhost/', 'user_agent': 'Mozilla/5.0 Chrome/155.0.0.0',
             'body': {'reason': 'oom', 'crash_report_api': {'pageInstanceId': page_instance_id, 'view': 'chat'}}}]


@pytest.fixture
def world(semantic_authority, monkeypatch, tmp_path):
    from lib.router.binding import global_router
    from lib.router.watchdog.binding import router_watchdog
    from lib.scheduler.conversation_dispatch import ScheduledTurnDispatch
    client, owner, _engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    monkeypatch.setattr('lib.scheduler.conversation_dispatch.get_storage_client', client)
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    monkeypatch.setenv('TOFU_CLIENT_REPORTS_DIR', str(tmp_path / 'client_reports'))
    global_router(owner, create=True)
    watchdog = router_watchdog(owner, create=True)
    woken = []

    def dispatch(conversation_id, message, tools_config, **kwargs):
        woken.append((conversation_id, message, kwargs))
        return ScheduledTurnDispatch('started', task_id=f'wake-{len(woken)}')
    # The wake's delivery seam only; the producer and the signal store are real.
    monkeypatch.setattr('lib.scheduler.conversation_dispatch.dispatch_scheduled_turn', dispatch)
    return SimpleNamespace(owner=owner, watchdog=watchdog, woken=woken)


def test_a_crash_episode_is_one_owner_scoped_signal_and_one_wake(world):
    from lib.logging_pkg.client_crash import record_browser_reports, record_client_report
    from lib.router.watchdog.signals import FaultSignals

    record_client_report(world.owner, _unclean_exit())
    # Chrome's report of the same crash is more evidence, not a second signal or wake.
    record_browser_reports(world.owner, _browser_report(), origin_header='http://localhost', host='localhost')
    [signal] = FaultSignals(world.owner).open_page()['signals']
    assert signal['source'] == 'client' and signal['owner'] == world.owner
    assert signal['subject'] == {'role': 'browser', 'pageInstanceId': PAGE}
    assert signal['error']['kind'] == 'browser_renderer_crash'
    detected = datetime.fromtimestamp(1794187420, tz=timezone.utc).strftime('%Y-%m-%d')
    for fact in ('renderer_crash_suspected', 'view chat', detected):
        assert fact in signal['error']['detail'], fact
    assert PAGE in signal['error']['hint']
    [(conversation_id, message, options)] = world.woken
    assert conversation_id == world.watchdog['conversationId']
    assert options['command_id'] == 'router-watchdog-signal:' + signal['signalId']
    assert message['_initiator'] == 'peer' and f'page instance={PAGE}' in message['content']
    assert CONTENT not in json.dumps([signal, message], ensure_ascii=False)


def test_another_page_instance_is_another_episode(world):
    from lib.logging_pkg.client_crash import record_client_report
    from lib.router.watchdog.signals import FaultSignals

    record_client_report(world.owner, _unclean_exit())
    record_client_report(world.owner, _unclean_exit('7d1c2b3a-0000-4000-8000-000000000002'))
    pages = sorted(s['subject']['pageInstanceId'] for s in FaultSignals(world.owner).open_page()['signals'])
    assert pages == sorted([PAGE, '7d1c2b3a-0000-4000-8000-000000000002'])
    assert len(world.woken) == 2


def test_discards_are_never_signalled_and_reports_never_raise(world):
    from lib.logging_pkg.client_crash import record_client_report
    from lib.router.watchdog.signals import FaultSignals, report_client_crash

    record_client_report(world.owner, {**_unclean_exit(), 'classification': 'tab_discarded'})
    assert FaultSignals(world.owner).open_page()['signals'] == []
    # An owner without a Watchdog records nothing; a crash without a page id is no episode.
    assert report_client_crash(world.owner + 1, page_instance_id=PAGE, kind='browser_renderer_crash',
                               detail='d', hint='h') is None
    assert report_client_crash(world.owner, page_instance_id='', kind='browser_renderer_crash',
                               detail='d', hint='h') is None
    assert world.woken == []
