#!/usr/bin/env python3
"""tests/llm_dispatch/test_402_manual_on_pause.py — a key-wide HTTP 402 stop
must hold even while Settings carries a manual-ON override.

Observed 2026-11-09 20:27–21:33 CST (attempt a3c09a90-8a71-458e-a307-c6812c080078,
turn 411d2578-81f5-4d93-ad30-2a71d917f12e, 367 tool rounds before it died):

  * One credential of the gateway provider (``sankuai``) has been answering
    ``402 {"message":"您的Credit已耗尽", ext.error.source=AIGC, stage=validation}``
    since at least 12:30. ``data/config/key_stats.json`` recorded a PERSISTENT
    ``override: true`` for that credential (set before the retained logs) and a
    key-wide ``exhausted: true`` stop 38 times that day. The rule below is
    credential-agnostic by construction; no key is assumed here.
  * ``lib.key_stats._enable.is_key_enabled`` let the manual ON outrank the
    stop, so the durable disqualifier was inert. Only the slot-local 3600s
    quota cooldown held the key back, and at 21:27:34 — exactly one hour after
    the 20:27:32 402 — SIX waiting tasks dispatched that key at the same second
    and each took another live 402 (11 such re-charges in the day's log).
  * Attempt a3c09a90's round 247 had its kimi-k3.1-pro-m17 route durably
    excluded by an unrelated 400 at 20:31:30, so the only remaining route for
    the model was the stopped key. ``has_capable_slots`` called that healable capacity
    under the override and the strict loop slept for 56 minutes (≈10,660
    cooldown cycles) before buying the fatal 402 instead of failing over.

Contract after the fix (no new knob, no routing-policy change):

  1. A key-wide 402 billing stop wins over a manual ON. Re-enabling a key
     CLEARS that day's stops, so a stop still recorded under an override is
     necessarily the provider's NEWER statement that this key cannot pay —
     the newest explicit statement wins, not the older one. The override row
     itself is preserved and still beats the success-rate auto-disable.
  2. Recovery is unchanged and owner-driven: re-enable after topping up, or
     the first request served after the day boundary (one probe at a
     time). The per-model stop from an ambiguous
     ``429 + insufficient_quota`` body KEEPS manual-ON precedence (owner
     2026-07-29 aggregating-gateway ruling); ordinary 429 handling is
     untouched — it still never disables a key.
  3. Selection AND capacity read that same verdict, so a stopped key is not
     re-picked, not reported as healable cooldown, and a healthy sibling
     credential serves the round instead.
  4. The stop's reason and time are projected for the Settings key card.

Run:  pytest tests/llm_dispatch/test_402_manual_on_pause.py -m unit
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

PROV = 'owner:1:credit-pause-test'
DEAD = 'cred_dead_manual_on'
LIVE = 'cred_live_sibling'
DEAD_PK = f'{PROV}::{DEAD}'
LIVE_PK = f'{PROV}::{LIVE}'
MODEL = 'kimi-k3.1-pro'


@pytest.fixture
def stats(monkeypatch, tmp_path):
    """Real lib.key_stats on an isolated store; both keys are its siblings."""
    import lib.key_stats as ks

    snapshot = dict(ks._cache)
    monkeypatch.setattr(ks, '_STATS_PATH', str(tmp_path / 'key_stats.json'))
    monkeypatch.setattr(ks, '_list_siblings', lambda pid: [DEAD_PK, LIVE_PK])
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


def _dispatcher(slots):
    """Real picker over a real SlotPool, so the key-health credential index
    the selection batch reads is the one the pool maintains."""
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


def _slot(key=DEAD, model=MODEL):
    from lib.llm_dispatch.slot import Slot

    return Slot(key_name=key, api_key='synthetic-' + key, model=model,
                capabilities={'text'}, provider_id=PROV,
                routing_owner_user_id=1,
                routing_provider_id=PROV.split(':', 2)[2],
                route_credential_id=key)


def test_keywide_stop_wins_over_an_earlier_manual_on(stats):
    """The incident itself: an override set BEFORE the 402 does not buy
    another live 402 every hour, and the owner's row is not deleted."""
    ks = stats
    ks.set_key_override(PROV, DEAD, True)
    ks.mark_key_exhausted(PROV, DEAD, reason='HTTP 402 Credit exhausted')

    assert ks.is_key_enabled(PROV, DEAD) is False
    assert ks.is_key_enabled(PROV, DEAD, model=MODEL) is False
    with ks.strict_billing_stop_admission():
        assert ks.is_key_enabled(PROV, DEAD, model=MODEL) is False
    assert ks.is_key_refused_for_optional_work(
        PROV, DEAD, model=MODEL, honor_override=True) is True

    row = ks.get_today_stats(PROV, DEAD)
    assert row['override'] is True, 'the recorded user decision is kept'
    assert row['enabled'] is False
    assert row['exhausted'] is True
    assert row['last_error'] == 'HTTP 402 Credit exhausted'
    assert isinstance(row['exhausted_at'], (int, float)) and row['exhausted_at'] > 0, (
        'Settings must be able to say WHEN the provider reported exhaustion')
    assert ks.get_all_stats()['keys'][DEAD_PK]['enabled'] is False
    assert ks.get_all_stats()['keys'][DEAD_PK]['exhausted_at'] == row['exhausted_at']

    # A key-wide stop recorded AFTER the manual ON survives a persist/reload
    # round-trip too — the durable document is the cross-process evidence.
    ks._cache['loaded'] = False
    assert ks.is_key_enabled(PROV, DEAD, model=MODEL) is False


def test_healthy_sibling_and_manual_on_without_a_stop_stay_enabled(stats):
    """No new restriction for keys that never reported a billing stop."""
    ks = stats
    ks.set_key_override(PROV, DEAD, True)
    ks.mark_key_exhausted(PROV, DEAD, reason='HTTP 402 Credit exhausted')

    assert ks.is_key_enabled(PROV, LIVE, model=MODEL) is True
    ks.set_key_override(PROV, LIVE, True)
    assert ks.is_key_enabled(PROV, LIVE, model=MODEL) is True


def test_manual_off_and_auto_disable_keep_their_authority(stats):
    """An explicit OFF with NO stop, and a success-rate disable without an
    override, must still project enabled=False (regression guard)."""
    ks = stats
    ks.set_key_override(PROV, DEAD, False)
    assert ks.get_today_stats(PROV, DEAD)['enabled'] is False
    assert ks.get_all_stats()['keys'][DEAD_PK]['enabled'] is False
    ks.clear_key_override(PROV, DEAD)
    for _ in range(ks.MIN_ATTEMPTS):
        ks.record_outcome(PROV, DEAD, success=False,
                          error='HTTP 401 invalid_api_key', credential_failure=True)
    row = ks.get_today_stats(PROV, DEAD)
    assert row['auto_disabled'] is True
    assert row['exhausted'] is False
    assert row['enabled'] is False
    assert ks.get_all_stats()['keys'][DEAD_PK]['enabled'] is False


def test_top_up_reenable_recovers_and_clears_the_timestamp(stats):
    ks = stats
    ks.set_key_override(PROV, DEAD, True)
    ks.mark_key_exhausted(PROV, DEAD, reason='HTTP 402 Credit exhausted')
    assert ks.is_key_enabled(PROV, DEAD) is False

    row = ks.set_key_override(PROV, DEAD, True)
    assert row['enabled'] is True
    assert ks.is_key_enabled(PROV, DEAD, model=MODEL) is True
    assert ks.get_today_stats(PROV, DEAD)['exhausted_at'] is None


def test_day_rollover_recovers_a_keywide_stop_under_manual_on(stats, monkeypatch):
    """Past the day boundary the manual-ON key awaits one probe; served,
    the stop ends and the override applies again."""
    ks = stats
    ks.set_key_override(PROV, DEAD, True)
    ks.mark_key_exhausted(PROV, DEAD, reason='HTTP 402 Credit exhausted')
    monkeypatch.setattr(ks, '_today', lambda: '2099-01-02')
    assert ks.billing_stop_probe_due(PROV, DEAD) is True
    assert ks.is_key_enabled(PROV, DEAD, model=MODEL) is False
    ks.record_outcome(PROV, DEAD, True)
    assert ks.is_key_enabled(PROV, DEAD, model=MODEL) is True


def test_per_model_429_quota_stop_keeps_manual_on_precedence(stats):
    """Owner 2026-07-29: an ambiguous vendor-quota 429 is NOT the account's
    credit pool, so this fix must not change that precedence."""
    ks = stats
    ks.set_key_override(PROV, DEAD, True)
    ks.mark_key_exhausted(PROV, DEAD, reason='insufficient_quota', model=MODEL)

    assert ks.is_key_enabled(PROV, DEAD, model=MODEL) is True
    assert ks.is_key_enabled(PROV, DEAD) is True
    assert ks.get_today_stats(PROV, DEAD)['exhausted_models'] == {
        MODEL: 'insufficient_quota'}


def test_ordinary_429_still_never_disables_a_key(stats):
    ks = stats
    for _ in range(ks.MIN_ATTEMPTS + 10):
        ks.record_rate_limit(PROV, DEAD, reason='HTTP 429')
    assert ks.is_key_enabled(PROV, DEAD, model=MODEL) is True
    row = ks.get_today_stats(PROV, DEAD)
    assert row['exhausted'] is False
    assert row['enabled'] is True


def test_pick_and_capacity_skip_a_manual_on_billing_dead_key(stats):
    ks = stats
    ks.set_key_override(PROV, DEAD, True)
    ks.mark_key_exhausted(PROV, DEAD, reason='HTTP 402 Credit exhausted')
    disp = _dispatcher([_slot(DEAD), _slot(LIVE)])

    from lib.llm_dispatch.provider_pin import provider_pin
    with provider_pin(PROV):
        picked = disp._pick('text', MODEL, None, None, strict_model=True)
        assert picked is not None and picked.key_name == LIVE

        # A pool whose only route for the model is the stopped key has no
        # healable capacity, so the strict loop cannot wait out a cooldown.
        dead_only = _dispatcher([_slot(DEAD)])
        assert dead_only.has_capable_slots('text', prefer_model=MODEL) is False


def test_cooling_dead_key_is_not_healable_capacity(stats):
    """The exact R247 shape: a quota-cooled slot under a manual ON must not
    keep the strict loop cycling for an hour."""
    import time

    ks = stats
    ks.set_key_override(PROV, DEAD, True)
    ks.mark_key_exhausted(PROV, DEAD, reason='HTTP 402 Credit exhausted')
    dead = _slot(DEAD)
    dead.cooldown_until = time.time() + 3600
    dead.cooldown_reason = 'quota'
    disp = _dispatcher([dead])

    from lib.llm_dispatch.provider_pin import provider_pin
    with provider_pin(PROV):
        assert disp.has_capable_slots('text', prefer_model=MODEL) is False


def test_warm_sticky_hold_never_waits_for_a_billing_dead_key(stats):
    """The conv-affinity warm hold must not park a round on a 402-stopped key
    (a recorded billing stop is durable, unlike a 0.5s rate-limit nudge)."""
    import time

    from lib.llm_dispatch.conv_affinity import record_conv_key
    from lib.llm_dispatch.provider_pin import provider_pin

    ks = stats
    ks.set_key_override(PROV, DEAD, True)
    ks.mark_key_exhausted(PROV, DEAD, reason='HTTP 402 Credit exhausted')
    dead = _slot(DEAD)
    dead.cooldown_until = time.time() + 3600
    dead.cooldown_reason = 'quota'
    disp = _dispatcher([dead, _slot(LIVE)])
    affinity = dead.affinity_key()
    conv = 'conv-402-warm-hold'
    with provider_pin(PROV):
        record_conv_key(conv, affinity, route_key=MODEL)
        assert disp.sticky_cooldown_remaining_s(conv, prefer_model=MODEL) is None


def test_402_fails_over_to_the_sibling_key_without_recharging_the_dead_one(
        stats, monkeypatch):
    """Full round through the real dispatch loop: one 402, then the live
    sibling answers. A later round spends ZERO further calls on the dead key,
    even after its slot-local cooldown expires."""
    import time

    from lib.llm.llm_errors import RateLimitError
    from lib.llm_dispatch import api

    ks = stats
    ks.set_key_override(PROV, DEAD, True)
    dead, live = _slot(DEAD, model=MODEL), _slot(LIVE, model=MODEL)
    # Deterministic precondition: the exhausted key is the PREFERRED slot
    # here, so the first dispatch provably pays its 402. Without this the
    # assertion depended on Slot.score()'s random tie-break between two
    # identical slots (random.uniform(0.95, 1.05)); that flakiness, not a
    # missing dispatcher dependency, is why this spec failed in isolation.
    live.latency_ema = dead.latency_ema * 10
    assert dead.score() < live.score()
    disp = _dispatcher([dead, live])
    monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
    monkeypatch.setattr(api, '_audit_severity_downgrade', lambda: None)
    sleeps = []
    monkeypatch.setattr(api.time, 'sleep', lambda seconds: sleeps.append(seconds))
    calls = []

    quota_error = None

    def stream(*args, **kwargs):
        nonlocal quota_error
        key = kwargs.get('api_key')
        calls.append(key)
        if key == 'synthetic-' + DEAD:
            quota_error = RateLimitError(
                'API HTTP 402: {"status":402,"message":"您的Credit已耗尽"}',
                is_quota=True, status_code=402)
            raise quota_error
        return ('live-answer', 'stop', {'prompt_tokens': 3, 'completion_tokens': 2})

    monkeypatch.setattr('lib.llm.stream_chat', stream)
    from lib.llm_dispatch.provider_pin import provider_pin
    with provider_pin(PROV):
        content, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'synthetic'}],
            prefer_model=MODEL, strict_model=True, max_retries=3)
        assert content == 'live-answer' and finish == 'stop'
        assert calls == ['synthetic-' + DEAD, 'synthetic-' + LIVE]
        # The refused key is named WITHOUT its secret, for the Settings card
        # and the Watchdog fault payload. The Turn's own routeSnapshot only
        # records the last successful round (here: the LIVE sibling).
        from lib.error_envelope import from_exception
        evidence = (from_exception(quota_error).get('billingStop') or {})
        assert evidence['providerId'] == PROV.split(':', 2)[2]
        assert evidence['credentialId'] == DEAD
        assert evidence['keyTail'] == ('synthetic-' + DEAD)[-4:]
        assert evidence['statusCode'] == 402
        assert evidence['scope'] == 'key'
        assert evidence['model'] == MODEL
        assert evidence['observedAtMs'] > 0
        assert 'synthetic-' + DEAD not in str(evidence), 'no key material leaks'

        # Same conversation later in the turn: the exhausted key is no
        # longer cooling, and the manual ON is still in the store.
        dead.cooldown_until = time.time() - 1
        content, finish, _ = api.dispatch_stream(
            [{'role': 'user', 'content': 'synthetic again'}],
            prefer_model=MODEL, strict_model=True, max_retries=3)
        assert content == 'live-answer'
        assert calls == ['synthetic-' + DEAD, 'synthetic-' + LIVE,
                         'synthetic-' + LIVE], (
            'the recorded 402 stop must prevent another live call on the '
            'manually-enabled exhausted key')
    assert sleeps == [], 'a healthy sibling means no cooldown waiting at all'
    assert ks.get_today_stats(PROV, DEAD)['exhausted'] is True


def test_manual_on_dead_pool_surfaces_the_quota_error_without_polling(
        stats, monkeypatch):
    """No sibling available: surface the recorded 402 instead of sleeping."""
    from lib.llm.llm_errors import RateLimitError
    from lib.llm_dispatch import api

    ks = stats
    ks.set_key_override(PROV, DEAD, True)
    ks.mark_key_exhausted(PROV, DEAD, reason='HTTP 402 Credit exhausted')
    disp = _dispatcher([_slot(DEAD)])
    monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
    monkeypatch.setattr(api, '_audit_severity_downgrade', lambda: None)
    sleeps = []
    monkeypatch.setattr(api.time, 'sleep', lambda seconds: sleeps.append(seconds))

    def must_not_send(*args, **kwargs):
        raise AssertionError('a 402-stopped key was dispatched again')

    monkeypatch.setattr('lib.llm.stream_chat', must_not_send)
    from lib.llm_dispatch.provider_pin import provider_pin
    with provider_pin(PROV):
        with pytest.raises((RateLimitError, RuntimeError, AssertionError)):
            api.dispatch_stream([{'role': 'user', 'content': 'synthetic'}],
                                prefer_model=MODEL, strict_model=True,
                                max_retries=2)
    assert sleeps == [], 'a policy-refused pool must not be polled as cooldown'
