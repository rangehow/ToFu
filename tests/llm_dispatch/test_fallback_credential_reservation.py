"""Fallback dispatch never selects an experiment-reserved credential.

Server generation 7d3ec336 (2026-11-18) logged 180 per-AppId minute-limit
429s on FRIDAY AppId ...5409. 177 came from conversations accepted on
claude-opus-5.5 and served on kimi-k3.1-pro (or rescued onto
gemini-3.8-flash) after a content-filter refusal; the parallel project's
Paper B / Paper 3 relays depend on that AppId and on ...4861. The
reservation is declared data (contracts/experiment_credential_reservations_v1.json,
sourced from those launchers); fallback dispatch, its recovery and pool
rescue select only other credentials, while primary traffic is unchanged.
"""

from __future__ import annotations

import json
import threading
from contextlib import nullcontext
from pathlib import Path

import pytest

import lib.tasks_pkg.llm_fallback._pool as fallback_pool
from lib.llm_dispatch.credential_reservation import (
    active_reservations,
    fallback_dispatch,
    reservations,
)
from lib.llm_dispatch.dispatcher import LLMDispatcher
from lib.llm_dispatch.slot import Slot

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_FRIDAY = 'https://api.openai.com/v1'
_EPISODE = _ROOT / 'tests/fixtures/llm_dispatch/episode_7d3ec336_appid_5409_429s.json'


def _slot(model, tail, *, base_url=_FRIDAY, latency=1000.0):
    return Slot(key_name=f'cred-{tail}:dep-{model}',
                api_key=f'2000000000000000{tail}', model=model,
                logical_model=model, capabilities={'text'},
                provider_id='example-corp', base_url=base_url, latency_ema=latency)


def _dispatcher(*slots):
    dispatcher = LLMDispatcher()
    dispatcher.slots = list(slots)
    dispatcher._initialized = True
    return dispatcher


def _tail(slot):
    return slot.api_key[-4:] if slot is not None else None


@pytest.fixture(autouse=True)
def _keys_enabled(monkeypatch):
    monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *_a, **_kw: True)


def test_reservation_is_declared_from_the_experiment_launchers():
    document = json.loads(
        (_ROOT / 'contracts/experiment_credential_reservations_v1.json').read_text())
    assert document['schema'] == 'tofu.experiment-credential-reservations/v1'
    rows = document['reservations']
    # Which keys are reserved and who declared them; no rate, cap or weight.
    for row in rows:
        assert set(row) == {'gateway_host', 'key_tail', 'reserved_for',
                            'declared_by', 'read_at'}
        assert row['declared_by']
    assert reservations() == {('your-llm-gateway.example.com', '4861'),
                              ('your-llm-gateway.example.com', '5409')}


def test_primary_dispatch_still_reaches_a_reserved_key():
    reserved = _slot('kimi-k3.1-pro', '5409', latency=100.0)
    other = _slot('kimi-k3.1-pro', '6672', latency=3000.0)
    dispatcher = _dispatcher(reserved, other)

    assert active_reservations() is None
    assert dispatcher.pick_slot(prefer_model='kimi-k3.1-pro',
                                strict_model=True) is reserved


def test_fallback_dispatch_picks_another_key_when_an_experiment_key_is_reserved():
    reserved = _slot('kimi-k3.1-pro', '5409', latency=100.0)
    experiment = _slot('kimi-k3.1-pro', '4861', latency=100.0)
    other = _slot('kimi-k3.1-pro', '6672', latency=3000.0)
    dispatcher = _dispatcher(reserved, experiment, other)

    with fallback_dispatch():
        picks = {_tail(dispatcher.pick_and_reserve(
            prefer_model='kimi-k3.1-pro', strict_model=True)) for _ in range(20)}
        assert picks == {'6672'}
        assert dispatcher.has_capable_slots(prefer_model='kimi-k3.1-pro')
    assert active_reservations() is None


def test_fallback_with_only_reserved_keys_has_no_route():
    dispatcher = _dispatcher(_slot('MiniMax-M2.7', '4861'),
                             _slot('MiniMax-M2.7', '5409'))

    with fallback_dispatch():
        assert dispatcher.pick_slot(prefer_model='MiniMax-M2.7',
                                    strict_model=True) is None
        assert not dispatcher.has_capable_slots(
            prefer_model='MiniMax-M2.7', exclude_models=set())


def test_a_matching_tail_on_another_gateway_is_not_reserved():
    elsewhere = _slot('gpt-x', '5409', base_url='https://api.example.com/v1')
    dispatcher = _dispatcher(elsewhere)

    with fallback_dispatch():
        assert dispatcher.pick_slot(prefer_model='gpt-x',
                                    strict_model=True) is elsewhere


def test_pool_rescue_dispatches_inside_the_reservation(monkeypatch):
    seen = []

    class _Dispatcher:
        def has_capable_slots(self, *_a, **_kw):
            seen.append(('gate', active_reservations()))
            return True

    def fake_stream(task, body, **_kwargs):
        seen.append(('stream', active_reservations()))
        raise RuntimeError('no slot')

    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', _Dispatcher)
    monkeypatch.setattr(fallback_pool._transport, 'stream_attempt', fake_stream)
    monkeypatch.setattr(fallback_pool.task_events, 'append_event',
                        lambda *_a, **_kw: None)
    task = {'id': 'rescue-task', 'convId': 'conv', 'config': {},
            'events': [], 'events_lock': threading.Lock()}

    assert fallback_pool._attempt_pool_rescue(
        task, {'model': 'kimi-k3.1-pro'}, 3, 4096, None, {}, [],
        failed_models={'claude-opus-5.5', 'kimi-k3.1-pro'},
        original_model='claude-opus-5.5', cause_exc=RuntimeError('refused'),
        preset='medium', thinking_enabled=True) is None
    assert seen == [('gate', reservations()), ('stream', reservations())]


def test_replay_of_the_7d3ec336_episode_sends_no_fallback_traffic_to_5409():
    episode = json.loads(_EPISODE.read_text())
    columns = episode['columns']
    rows = [dict(zip(columns, row)) for row in episode['rows']]
    assert len(rows) == 180

    # The production inventory for the episode's wires, with ...5409 the
    # preferred pick (the warm, lowest-latency key that took the 429s).
    dispatcher = _dispatcher(
        _slot('kimi-k3.1-pro', '5409', latency=100.0),
        _slot('kimi-k3.1-pro', '6672'),
        _slot('kimi-k3.1-pro', '0733'),
        _slot('gemini-3.8-flash', '5409', latency=100.0),
        _slot('gemini-3.8-flash', '4861'),
        _slot('gemini-3.8-flash', '0733'),
        _slot('gpt-6-luna', '5409', latency=100.0),
        _slot('gpt-6-luna', '0733'),
    )

    def replay(scoped):
        on_5409 = {'fallback': 0, 'auxiliary': 0}
        for row in rows:
            origin = ('fallback' if row['accepted_model']
                      and row['accepted_model'] != row['wire_model']
                      else 'auxiliary')
            in_scope = scoped and origin == 'fallback'
            with fallback_dispatch() if in_scope else nullcontext():
                slot = dispatcher.pick_slot(prefer_model=row['wire_model'],
                                            strict_model=True)
            assert slot is not None
            if _tail(slot) == '5409':
                on_5409[origin] += 1
        return on_5409

    # Baseline: every Tofu-attributable 429 of the episode reached ...5409.
    assert replay(scoped=False) == {'fallback': 177, 'auxiliary': 3}
    # With the reservation, fallback traffic never selects ...5409. The three
    # Fetch:Extract calls are auxiliary primary traffic, outside this rule.
    assert replay(scoped=True) == {'fallback': 0, 'auxiliary': 3}

