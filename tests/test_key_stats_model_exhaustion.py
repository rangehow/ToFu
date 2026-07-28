#!/usr/bin/env python3
"""tests/test_key_stats_model_exhaustion.py — per-(key, model) billing-stop
granularity guards (epic pt_69e9d6038c9344dc, 2026-07-28 incident).

The incident: sankuai_key_1 took an Aliyun ``insufficient_quota`` on
qwen3.5-plus (11:10:34) and ``mark_key_exhausted`` flipped the KEY-WIDE
flag — on an aggregating gateway where the same key also routes
kimi-k3→Moonshot, a qwen billing-stop cross-poisoned kimi capacity. And
the stop "didn't hold" in practice because all three keys carry a stale
manual ``override: true`` that silently wins over any billing-stop.

Pinned here:

  1. A model-named billing-stop blocks THAT model only — sibling models on
     the same key stay dispatchable (no cross-vendor poisoning).
  2. Callers that cannot name a model still get key-wide exhaustion
     (legacy contract preserved).
  3. A manual override still wins over per-model stops (user supremacy) —
     so nobody "fixes" the conflict by auto-clearing user state.
  4. Re-enabling a key (override True) clears per-model stops ("I topped
     up") — otherwise the next call would re-stop instantly.
  5. The stats rows expose ``exhausted_models`` (the UI badge's data) and
     the field survives a persist/reload round-trip.
  6. Dispatcher-level: the pick filter consults the model dimension — a
     slot whose (key, model) is stopped is skipped while the same key's
     other model is picked.

Run:  pytest tests/test_key_stats_model_exhaustion.py -m unit
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

PROV = 'gwtest'
KEY = 'gwtest_key_0'
PK = f'{PROV}::{KEY}'


@pytest.fixture
def fresh_stats(monkeypatch, tmp_path):
    """Isolate lib.key_stats onto a tmp stats file + known siblings."""
    import lib.key_stats as ks

    snapshot = {
        'day': ks._cache['day'],
        'stats': ks._cache['stats'],
        'overrides': ks._cache['overrides'],
        'loaded': ks._cache['loaded'],
    }
    monkeypatch.setattr(ks, '_STATS_PATH', str(tmp_path / 'key_stats.json'))
    monkeypatch.setattr(ks, '_list_siblings', lambda pid: [PK])
    ks._cache['day'] = ''
    ks._cache['stats'] = {}
    ks._cache['overrides'] = {}
    ks._cache['loaded'] = False
    yield ks
    ks._cache['day'] = snapshot['day']
    ks._cache['stats'] = snapshot['stats']
    ks._cache['overrides'] = snapshot['overrides']
    ks._cache['loaded'] = snapshot['loaded']


@pytest.mark.unit
class TestPerModelBillingStop:

    def test_model_stop_blocks_only_that_model(self, fresh_stats):
        """The incident's core: qwen's quota-death must NOT poison kimi on
        the same key."""
        ks = fresh_stats
        ks.mark_key_exhausted(PROV, KEY, reason='insufficient_quota (aliyun)',
                              model='qwen3.5-plus')

        assert ks.is_key_enabled(PROV, KEY, model='qwen3.5-plus') is False
        assert ks.is_key_enabled(PROV, KEY, model='kimi-k3') is True, (
            'cross-vendor poisoning: a qwen billing-stop took kimi down too')
        # Key-wide reads (no model given) stay enabled — the key itself is
        # not exhausted.
        assert ks.is_key_enabled(PROV, KEY) is True
        row = ks.get_today_stats(PROV, KEY)
        assert row['exhausted'] is False
        assert row['exhausted_models'] == {
            'qwen3.5-plus': 'insufficient_quota (aliyun)'}

    def test_modelless_stop_stays_key_wide(self, fresh_stats):
        """Legacy contract: no model named → the whole key stops."""
        ks = fresh_stats
        # A healthy sibling keeps the last-resort guard out of this test —
        # last-resort deliberately promotes the sole remaining key, which is
        # a separate documented behavior.
        ks._list_siblings = lambda pid: [PK, f'{PROV}::gwtest_key_1']
        ks.record_outcome(PROV, 'gwtest_key_1', success=True)
        ks.mark_key_exhausted(PROV, KEY, reason='HTTP 402')

        assert ks.is_key_enabled(PROV, KEY) is False
        assert ks.is_key_enabled(PROV, KEY, model='kimi-k3') is False

    def test_override_still_wins_over_model_stop(self, fresh_stats):
        """User supremacy: a manual ON defeats a billing-stop (the conflict
        is surfaced in the UI instead of auto-clearing user state)."""
        ks = fresh_stats
        ks.mark_key_exhausted(PROV, KEY, reason='insufficient_quota',
                              model='qwen3.5-plus')
        ks.set_key_override(PROV, KEY, True)
        # Re-enable clears stops ("I topped up")…
        assert ks.is_key_enabled(PROV, KEY, model='qwen3.5-plus') is True
        # …and a FRESH stop after the override is also overridden.
        ks.mark_key_exhausted(PROV, KEY, reason='insufficient_quota again',
                              model='qwen3.5-plus')
        assert ks.is_key_enabled(PROV, KEY, model='qwen3.5-plus') is True, (
            'manual override must keep winning — do not "fix" the conflict '
            'by silently clearing user state')
        assert ks.get_today_stats(PROV, KEY)['exhausted_models'], (
            'the stop is still RECORDED — the UI surfaces the conflict '
            'from this field')

    def test_reenable_clears_model_stops(self, fresh_stats):
        ks = fresh_stats
        ks.mark_key_exhausted(PROV, KEY, reason='q', model='qwen3.5-plus')
        ks.set_key_override(PROV, KEY, True)
        assert ks.get_today_stats(PROV, KEY)['exhausted_models'] == {}

    def test_exhausted_models_survive_persist_reload(self, fresh_stats):
        ks = fresh_stats
        ks.mark_key_exhausted(PROV, KEY, reason='q', model='qwen3.5-plus')
        # Force a reload from disk.
        ks._cache['loaded'] = False
        assert ks.is_key_enabled(PROV, KEY, model='qwen3.5-plus') is False
        assert ks.is_key_enabled(PROV, KEY, model='kimi-k3') is True

    def test_get_all_stats_exposes_exhausted_models(self, fresh_stats):
        ks = fresh_stats
        ks.mark_key_exhausted(PROV, KEY, reason='q', model='qwen3.5-plus')
        snap = ks.get_all_stats()
        assert snap['keys'][PK]['exhausted_models'] == {'qwen3.5-plus': 'q'}


@pytest.mark.unit
class TestDispatcherModelGate:

    def _dispatcher(self, slots):
        import threading
        from lib.llm_dispatch.dispatcher import LLMDispatcher
        disp = object.__new__(LLMDispatcher)
        disp._lock = threading.Lock()
        disp.slots = list(slots)
        disp.initialize = lambda: None
        return disp

    def _slot(self, model, key=KEY):
        from lib.llm_dispatch.slot import Slot
        return Slot(key_name=key, api_key='sk-test', model=model,
                    capabilities={'text'}, provider_id=PROV)

    def test_pick_skips_stopped_model_keeps_sibling(self, fresh_stats):
        """End-to-end at the pick seam: the stopped (key, model) slot is
        filtered out while the same key's healthy model is picked."""
        ks = fresh_stats
        ks.mark_key_exhausted(PROV, KEY, reason='insufficient_quota',
                              model='qwen3.5-plus')
        disp = self._dispatcher([
            self._slot('qwen3.5-plus'),
            self._slot('kimi-k3'),
        ])
        picked = disp._pick('text', None, None, None)
        assert picked is not None
        assert picked.model == 'kimi-k3', (
            'the pick filter must consult the model dimension — a stopped '
            '(key, model) slot has to be skipped, not dispatched')

    def test_pick_returns_none_when_only_model_stopped(self, fresh_stats):
        ks = fresh_stats
        ks.mark_key_exhausted(PROV, KEY, reason='insufficient_quota',
                              model='qwen3.5-plus')
        disp = self._dispatcher([self._slot('qwen3.5-plus')])
        assert disp._pick('text', None, None, None) is None


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
