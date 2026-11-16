"""Telemetry needs no durability barrier; owner decisions keep theirs (executable specification).

``key_stats.json`` holds two durability classes. Owner decisions (manual overrides) and billing stops are
durable before ``set_key_override``/``mark_key_exhausted`` return. Health telemetry (counters, last errors)
is reconstructible and already has a documented crash-loss window (``KEY_STATS_FLUSH_INTERVAL_SECONDS``).
Every telemetry flush rewrote the whole store with an fsync: 4 per Turn on STORAGE-PRESSURE-COMPARE-1's fixed
task. A telemetry-only flush now publishes ``key_stats.telemetry.json`` atomically without a durability
barrier and never rewrites the decision document, so no power loss during it can touch a decision.
"""
from __future__ import annotations

import json
import os

import pytest

from tests.key_stats.test_key_stats_persistence import KEY, PAIR, PROVIDER, isolated_stats  # noqa: F401

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]


def _counting_fsync(monkeypatch):
    calls = []
    real = os.fsync

    def fsync(descriptor):
        calls.append(descriptor)
        return real(descriptor)
    monkeypatch.setattr(os, 'fsync', fsync)
    return calls


def _fresh_view(ks):
    """What another process reads: drop this process's cache and load the store."""
    with ks._lock:
        ks._cache.update(day='', stats={}, overrides={}, loaded=False)
    return ks.get_today_stats(PROVIDER, KEY)


def test_a_telemetry_flush_has_no_durability_barrier_and_never_rewrites_decisions(
        isolated_stats, monkeypatch):  # noqa: F811 (pytest fixture)
    ks, stats_path = isolated_stats
    ks.set_key_override(PROVIDER, KEY, False)
    decisions_before = stats_path.read_bytes()
    inode_before = os.stat(stats_path).st_ino
    for _ in range(3):
        ks.record_outcome(PROVIDER, KEY, success=True)
    fsyncs = _counting_fsync(monkeypatch)
    assert ks.flush_key_stats() is True
    assert fsyncs == [], 'a telemetry-only flush waited for a durability barrier'
    assert stats_path.read_bytes() == decisions_before
    assert os.stat(stats_path).st_ino == inode_before
    today = _fresh_view(ks)
    assert today['success'] == 3
    assert today['override'] is False


def test_a_decision_commit_is_durable_and_carries_earlier_telemetry(isolated_stats, monkeypatch):  # noqa: F811 (pytest fixture)
    ks, stats_path = isolated_stats
    for _ in range(2):
        ks.record_rate_limit(PROVIDER, KEY)
    assert ks.flush_key_stats() is True
    ks.record_outcome(PROVIDER, KEY, success=True)
    fsyncs = _counting_fsync(monkeypatch)
    ks.set_key_override(PROVIDER, KEY, False)
    assert fsyncs, 'an owner decision returned before it was durable'
    document = json.loads(stats_path.read_text(encoding='utf-8'))
    assert document['overrides'] == {PAIR: False}
    # Telemetry recorded before the decision commits with it, in order.
    assert document['stats'][PAIR]['rate_limited'] == 2
    assert document['stats'][PAIR]['success'] == 1


def test_a_folded_namespace_is_counted_once_after_a_telemetry_flush(isolated_stats, monkeypatch):  # noqa: F811 (pytest fixture)
    """The telemetry document is the complete telemetry of its generation; a
    decision-document entry the fold moved away never counts a second time."""
    from lib.json_store import write_json_atomic
    from lib.key_stats import _state

    ks, stats_path = isolated_stats
    write_json_atomic(str(stats_path), {
        'day': ks._today(),
        'stats': {'gw_anthropic::gw_anthropic_key_0': {'success': 4}},
        'overrides': {},
    })
    monkeypatch.setattr(_state, '_namespace_fold_map', lambda: {'gw_anthropic': 'gw'})
    ks.record_outcome(PROVIDER, KEY, success=True)
    assert ks.flush_key_stats() is True
    for _ in range(2):
        with ks._lock:
            ks._cache.update(day='', stats={}, overrides={}, loaded=False)
        assert ks.get_today_stats('gw', 'gw_key_0')['success'] == 4
        assert ks.get_today_stats(PROVIDER, KEY)['success'] == 1


def test_an_unreadable_telemetry_document_loses_only_telemetry(isolated_stats):  # noqa: F811 (pytest fixture)
    ks, stats_path = isolated_stats
    ks.mark_key_exhausted(PROVIDER, KEY, reason='HTTP 402', model='m1')
    ks.set_key_override(PROVIDER, KEY, False)
    ks.record_outcome(PROVIDER, KEY, success=True)
    assert ks.flush_key_stats() is True
    telemetry_path = stats_path.with_name('key_stats.telemetry.json')
    assert telemetry_path.exists()
    # A power loss during an unsynchronized publication can leave it torn.
    telemetry_path.write_bytes(b'{"base": ')
    today = _fresh_view(ks)
    assert today['override'] is False
    assert today['exhausted_models'] == {'m1': 'HTTP 402'}
    # The next flush republishes the telemetry document from the decisions' view.
    ks.record_outcome(PROVIDER, KEY, success=True)
    assert ks.flush_key_stats() is True
    json.loads(telemetry_path.read_text(encoding='utf-8'))
    today = _fresh_view(ks)
    assert today['success'] >= 1
    assert today['override'] is False
    assert today['exhausted_models'] == {'m1': 'HTTP 402'}
