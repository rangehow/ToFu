"""A key-wide 402 stop ends by the provider's answer to one probe.

The gateway refuses a spent daily credit with HTTP 402 ``您的Credit已耗尽``
and refills it at an instant its responses do not state (2026-11-12/13: the
credit was back by the first requests after midnight; the retained logs do
not establish the refill instant). So the stop is not wiped with the day's
counters: it crosses the day boundary as ``probe_due``, every request stays
refused except one real request at a time (dispatch-side, see
``tests/llm_dispatch/test_billing_stop_every_picker.py``), a served request
ends the stop durably and a 402 records it again for the rest of the day.
"""
from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

PROVIDER = 'owner:1:sankuai'
KEY = 'cred_2009fa1132a88c55243ebcd3'
SIBLING = 'credential-1792419920936-26sj7f'
PAIR = f'{PROVIDER}::{KEY}'
DAY, NEXT, AFTER = '2026-11-12', '2026-11-13', '2026-11-14'


@pytest.fixture
def stats(monkeypatch, tmp_path):
    import lib.key_stats as ks
    from lib.key_stats import _state

    ks.shutdown_key_stats()
    monkeypatch.setattr(_state, 'KEY_STATS_FLUSH_INTERVAL_SECONDS', 3600.0)
    snapshot = {name: ks._cache[name]
                for name in ('day', 'stats', 'overrides', 'loaded')}
    pending_snapshot = list(_state._pending_mutations)
    path = tmp_path / 'key_stats.json'
    monkeypatch.setattr(ks, '_STATS_PATH', str(path))
    monkeypatch.setattr(ks, '_list_siblings',
                        lambda _provider: [PAIR, f'{PROVIDER}::{SIBLING}'])
    today = [DAY]
    monkeypatch.setattr(ks, '_today', lambda: today[0])
    ks._cache.update(day='', stats={}, overrides={}, loaded=False)
    _state._pending_mutations.clear()
    yield ks, path, today
    _state._pending_mutations.clear()
    ks.shutdown_key_stats()
    ks._cache.update(snapshot)
    _state._pending_mutations[:] = pending_snapshot


def _reload(ks):
    """Read the store as a process that starts now would."""
    ks.flush_key_stats()
    with ks._lock:
        ks._cache['loaded'] = False


def test_a_key_wide_stop_crosses_the_day_boundary_awaiting_one_probe(stats):
    ks, path, today = stats
    ks.record_outcome(PROVIDER, KEY, True)
    ks.mark_key_exhausted(PROVIDER, KEY, reason='API HTTP 402: 您的Credit已耗尽')
    assert ks.is_key_enabled(PROVIDER, KEY) is False
    assert ks.billing_stop_probe_due(PROVIDER, KEY) is False

    today[0] = NEXT
    assert ks.is_key_enabled(PROVIDER, KEY) is False
    assert ks.billing_stop_probe_due(PROVIDER, KEY) is True
    row = ks.get_today_stats(PROVIDER, KEY)
    assert row['exhausted'] is True and row['success'] == 0
    assert row['last_error'] == 'API HTTP 402: 您的Credit已耗尽'

    # Another process starting after midnight reads yesterday's document.
    _reload(ks)
    assert ks.is_key_enabled(PROVIDER, KEY) is False
    assert ks.billing_stop_probe_due(PROVIDER, KEY) is True
    assert json.loads(path.read_text(encoding='utf-8'))['day'] in (DAY, NEXT)


def test_a_served_probe_ends_the_stop_durably(stats):
    ks, path, today = stats
    ks.mark_key_exhausted(PROVIDER, KEY, reason='API HTTP 402')
    today[0] = NEXT
    ks.record_outcome(PROVIDER, KEY, True)
    assert ks.is_key_enabled(PROVIDER, KEY) is True
    assert ks.billing_stop_probe_due(PROVIDER, KEY) is False
    _reload(ks)
    assert ks.is_key_enabled(PROVIDER, KEY) is True
    stored = json.loads(path.read_text(encoding='utf-8'))
    assert stored['day'] == NEXT and stored['stats'][PAIR]['exhausted'] is False


def test_a_refused_probe_holds_until_the_next_day_boundary(stats):
    ks, _path, today = stats
    ks.mark_key_exhausted(PROVIDER, KEY, reason='API HTTP 402')
    today[0] = NEXT
    ks.mark_key_exhausted(PROVIDER, KEY, reason='API HTTP 402 again')
    assert ks.is_key_enabled(PROVIDER, KEY) is False
    assert ks.billing_stop_probe_due(PROVIDER, KEY) is False
    _reload(ks)
    assert ks.billing_stop_probe_due(PROVIDER, KEY) is False
    today[0] = AFTER
    assert ks.billing_stop_probe_due(PROVIDER, KEY) is True


def test_a_failed_probe_without_a_402_leaves_the_stop_due(stats):
    ks, _path, today = stats
    ks.mark_key_exhausted(PROVIDER, KEY, reason='API HTTP 402')
    today[0] = NEXT
    ks.record_outcome(PROVIDER, KEY, False, error='read timeout')
    ks.record_rate_limit(PROVIDER, KEY, reason='每分钟请求次数超过限制')
    assert ks.is_key_enabled(PROVIDER, KEY) is False
    assert ks.billing_stop_probe_due(PROVIDER, KEY) is True


def test_per_model_stops_stay_day_scoped_and_user_disables_are_never_probed(stats):
    ks, _path, today = stats
    ks.mark_key_exhausted(PROVIDER, SIBLING, reason='insufficient_quota',
                          model='qwen3.5-plus')
    ks.set_key_override(PROVIDER, KEY, False)
    ks.mark_key_exhausted(PROVIDER, KEY, reason='API HTTP 402')
    today[0] = NEXT
    assert ks.is_key_enabled(PROVIDER, SIBLING, model='qwen3.5-plus') is True
    assert ks.billing_stop_probe_due(PROVIDER, SIBLING) is False
    assert ks.billing_stop_probe_due(PROVIDER, KEY) is False
    assert ks.is_key_enabled(PROVIDER, KEY) is False


def test_an_owner_re_enable_still_clears_a_carried_stop(stats):
    ks, _path, today = stats
    ks.mark_key_exhausted(PROVIDER, KEY, reason='API HTTP 402')
    today[0] = NEXT
    ks.set_key_override(PROVIDER, KEY, True)
    assert ks.is_key_enabled(PROVIDER, KEY) is True
    assert ks.billing_stop_probe_due(PROVIDER, KEY) is False
