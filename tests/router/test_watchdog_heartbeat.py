"""Watchdog review heartbeat: the out-of-band dead-man signal (lib/router/watchdog/heartbeat.py).

Each periodic review that read storage writes one per-owner file under the
logs root with the time its next review must have ended; disabling the review
says so; a review that cannot run writes nothing, which is the staleness the
watcher outside the application alerts on.
"""
from __future__ import annotations

from datetime import datetime
import json

import pytest

from lib.router.watchdog import heartbeat, review
from lib.scheduler.contract import DUE_CLAIM_INTERVAL_SECONDS

pytestmark = pytest.mark.unit

OWNER = 7
NOW = datetime(2030, 1, 1, 12, 7, 30)


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, '_logs_root', lambda: tmp_path)
    return tmp_path


def _beat(logs, owner=OWNER):
    return json.loads((logs / f'watchdog-heartbeat-{owner}.json').read_text())


def test_a_review_records_when_the_next_one_must_have_ended(logs):
    heartbeat.record_review(OWNER, '7,37 * * * *', now=NOW)
    beat = _beat(logs)
    next_run = datetime(2030, 1, 1, 12, 37).timestamp()
    assert beat == {'version': 1, 'owner': OWNER, 'at': NOW.timestamp(), 'schedule': '7,37 * * * *',
                    'enabled': True,
                    'staleAfter': next_run + DUE_CLAIM_INTERVAL_SECONDS + heartbeat.REVIEW_MAX_RUNTIME_S}
    heartbeat.record_review(OWNER + 1, '*/15 * * * *', now=NOW)  # one file per owner
    assert _beat(logs)['schedule'] == '7,37 * * * *' and _beat(logs, OWNER + 1)['owner'] == OWNER + 1
    assert sorted(path.name for path in logs.iterdir()) == ['watchdog-heartbeat-7.json',
                                                            'watchdog-heartbeat-8.json']


def test_disabling_the_review_ends_the_watch(logs):
    heartbeat.record_configured(OWNER, '7,37 * * * *', False, now=NOW)
    assert _beat(logs)['enabled'] is False and _beat(logs)['staleAfter'] is None


def test_an_unwritable_heartbeat_never_raises_into_the_review(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(heartbeat, '_logs_root', lambda: tmp_path / 'missing')
    heartbeat.record_review(OWNER, '7,37 * * * *', now=NOW)
    assert 'review heartbeat not written' in caplog.text


def _review(monkeypatch, *, available=True, page=None):
    page = page or {'global_router': None, 'projects': [], 'next_cursor': None, 'open_signals': [],
                    'open_signals_next_cursor': None, 'outbox': [], 'channels': []}
    monkeypatch.setattr('lib.router.watchdog.binding.router_watchdog', lambda owner, create: {
        'availability': 'available' if available else 'missing', 'conversationId': 'watchdog'})
    monkeypatch.setattr('lib.router.watchdog.snapshot.health_snapshot', lambda owner, after_id=None: page)
    monkeypatch.setattr('lib.router.watchdog.signals.wake_watchdog', lambda owner, watchdog, **kwargs: 'started')
    return review.run_scheduled_review({'id': review.review_task_id(OWNER), 'user_id': OWNER,
                                        'system_key': review.REVIEW_SYSTEM_KEY, 'schedule': '*/15 * * * *',
                                        'command': ''})


def test_every_periodic_review_that_ran_writes_its_heartbeat_last(logs, monkeypatch):
    accepted, text = _review(monkeypatch)
    assert accepted and text.startswith('Nothing to review') and _beat(logs)['schedule'] == '*/15 * * * *'
    (logs / f'watchdog-heartbeat-{OWNER}.json').unlink()
    busy = {'global_router': None, 'projects': [{'workers': [{'phase': 'needs_review'}], 'stalled': True}],
            'next_cursor': None,
            'open_signals': [], 'open_signals_next_cursor': None, 'outbox': [], 'channels': []}
    accepted, text = _review(monkeypatch, page=busy)
    assert accepted and text.startswith('Watchdog woken') and _beat(logs)['enabled'] is True


def test_a_review_that_cannot_run_leaves_the_heartbeat_to_go_stale(logs, monkeypatch):
    accepted, _text = _review(monkeypatch, available=False)
    assert not accepted and not (logs / f'watchdog-heartbeat-{OWNER}.json').exists()
