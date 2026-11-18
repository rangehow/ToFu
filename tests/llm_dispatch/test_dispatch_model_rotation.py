"""A rate limit rotates a stateless pick to another Model.

A 429 is evidence about a Model's quota, not only one key's. On 2026-11-12
every gateway AppId serving four Gemini Models answered 429 for hours; a
whole-turn translation rotated keys within those Models and spent its 8/8
upstream-429 budget in three seconds while other ``cheap`` Models served.

A pick with no explicit model, provider pin or conversation warm key tries
the Models its dispatch has not seen limited first, preferring Models no gate
reports as rejecting other requests; once every Model was limited it retries
the one limited longest ago. Rotation orders candidates and never removes the
last of them. ``prefer_fast`` judges speed by Artificial Analysis evidence
and, for Models without it, by the throughput measured on this process's own
routes.
"""

from __future__ import annotations

import uuid

import pytest

from lib.llm_dispatch.dispatcher import LLMDispatcher
from lib.llm_dispatch.measured_speed import measured_speed_evidence
from lib.llm_dispatch.provider_pin import provider_pin
from lib.llm_dispatch.slot import Slot
from lib.model_catalog import aa


pytestmark = pytest.mark.unit


def _slot(model, key, *, latency=1000.0, throughput=0.0):
    slot = Slot(key_name=key, api_key=f'sk-{key}', model=f'wire-{model}',
                logical_model=model, capabilities={'text', 'cheap'},
                provider_id='gateway-a', latency_ema=latency)
    slot.throughput_ema = throughput
    return slot


def _dispatcher(*slots):
    dispatcher = LLMDispatcher()
    dispatcher.slots = list(slots)
    dispatcher._initialized = True
    return dispatcher


@pytest.fixture(autouse=True)
def _keys_enabled(monkeypatch):
    monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *_a, **_kw: True)


def test_a_limited_model_yields_to_another_model_before_its_other_keys():
    flash_a = _slot('flash', 'a', latency=100.0)
    flash_b = _slot('flash', 'b', latency=100.0)
    other = _slot('other', 'c', latency=3000.0)
    dispatcher = _dispatcher(flash_a, flash_b, other)

    assert dispatcher.pick_slot(capability='cheap') in (flash_a, flash_b)
    assert dispatcher.pick_slot(
        capability='cheap', rotate_models={'flash': 1}) is other


def test_once_every_model_was_limited_the_one_limited_longest_ago_goes_next():
    first = _slot('first', 'a', latency=100.0)
    second = _slot('second', 'b', latency=3000.0)
    dispatcher = _dispatcher(first, second)

    assert dispatcher.pick_slot(
        capability='cheap', rotate_models={'first': 2, 'second': 1}) is second
    assert dispatcher.pick_slot(
        capability='cheap', rotate_models={'first': 1, 'second': 2}) is first


def test_a_model_rejecting_other_requests_waits_behind_quiet_models(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(
        'lib.llm_dispatch.dispatcher.time.monotonic', lambda: now[0])
    flash_a = _slot('flash', 'a', latency=100.0)
    flash_b = _slot('flash', 'b', latency=100.0)
    other = _slot('other', 'c', latency=3000.0)
    dispatcher = _dispatcher(flash_a, flash_b, other)

    dispatcher.note_shared_contention(flash_a, per_credential=True)
    # flash_b's own AppId lane is open, but its Model is mid rejection run.
    assert dispatcher.pick_slot(capability='cheap') is other

    now[0] += 1.0
    dispatcher.note_shared_success(flash_a, admitted_at=now[0])
    assert dispatcher.pick_slot(capability='cheap') in (flash_a, flash_b)


def test_the_rejection_evidence_ends_with_the_gates_decay_grace(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(
        'lib.llm_dispatch.dispatcher.time.monotonic', lambda: now[0])
    flash = _slot('flash', 'a', latency=100.0)
    other = _slot('other', 'b', latency=3000.0)
    dispatcher = _dispatcher(flash, other)

    dispatcher.note_shared_contention(flash, per_credential=True)
    assert dispatcher.pick_slot(capability='cheap') is other
    now[0] += dispatcher._CONTENTION_DECAY_GRACE_S + 1.0
    assert dispatcher.pick_slot(capability='cheap') is flash


def test_a_rotated_pick_leaves_the_appid_that_is_rejecting():
    """2026-11-18: fetch_url extract got 429 on AppId …5409 for one Model,
    then rotated to another Model on the same throttled AppId although that
    Model's other AppIds were quiet."""
    flash_throttled = _slot('flash', 'appid-throttled', latency=100.0)
    luna_throttled = _slot('luna', 'appid-throttled', latency=100.0)
    luna_quiet = _slot('luna', 'appid-quiet', latency=3000.0)
    dispatcher = _dispatcher(flash_throttled, luna_throttled, luna_quiet)

    dispatcher.note_shared_contention(flash_throttled, per_credential=True)
    assert dispatcher.pick_slot(
        capability='cheap', rotate_models={'flash': 1}) is luna_quiet
    assert dispatcher.pick_slot(capability='cheap') is luna_quiet


def test_a_rejecting_appid_still_serves_when_it_is_the_only_one():
    flash = _slot('flash', 'appid-throttled', latency=100.0)
    luna = _slot('luna', 'appid-throttled', latency=100.0)
    dispatcher = _dispatcher(flash, luna)

    dispatcher.note_shared_contention(flash, per_credential=True)
    assert dispatcher.pick_slot(
        capability='cheap', rotate_models={'flash': 1}) is luna


def test_a_project_wide_limit_is_no_evidence_against_an_appid():
    flash = _slot('flash', 'appid-a', latency=100.0)
    luna_a = _slot('luna', 'appid-a', latency=100.0)
    luna_b = _slot('luna', 'appid-b', latency=3000.0)
    dispatcher = _dispatcher(flash, luna_a, luna_b)

    dispatcher.note_shared_contention(flash, per_credential=False)
    assert dispatcher.pick_slot(
        capability='cheap', rotate_models={'flash': 1}) is luna_a


def test_a_lone_rejecting_model_still_rotates_its_keys():
    limited = _slot('kimi', 'k0', latency=100.0)
    open_appid = _slot('kimi', 'k1', latency=99999.0)
    dispatcher = _dispatcher(limited, open_appid)

    dispatcher.note_shared_contention(limited, per_credential=True)
    assert dispatcher.pick_slot(
        capability='cheap', rotate_models={'kimi': 1}) is open_appid


def test_an_explicit_model_keeps_rotating_within_its_own_keys():
    flash_a = _slot('flash', 'a', latency=100.0)
    flash_b = _slot('flash', 'b', latency=200.0)
    other = _slot('other', 'c', latency=10.0)
    dispatcher = _dispatcher(flash_a, flash_b, other)
    dispatcher.note_shared_contention(flash_a, per_credential=True)

    assert dispatcher.pick_slot(
        capability='cheap', prefer_model='flash',
        rotate_models={'flash': 1}) is flash_b


def test_a_provider_pin_keeps_its_rank_order():
    pin = 'model-route:7:a'
    group = [
        Slot(key_name=f'ephemeral-{model}', api_key='sk-request',
             model=f'wire-{model}', logical_model=model,
             capabilities={'text', 'cheap'}, provider_id=pin,
             request_scoped=True, routing_provider_id='gateway-a',
             routing_owner_user_id=7, route_credential_id=f'credential-{model}',
             route_deployment_id=f'deployment-{rank}', route_rank=rank)
        for rank, model in enumerate(('primary', 'secondary'))
    ]
    dispatcher = _dispatcher(*group)

    with provider_pin(pin):
        assert dispatcher.pick_slot(
            capability='cheap', rotate_models={'primary': 1}) is group[0]


def test_a_warm_conversation_key_keeps_its_cache_preserving_hold(monkeypatch):
    from lib.llm_dispatch.conv_affinity import conv_affinity

    monkeypatch.setenv('TOFU_CONV_STICKY_ROUTING', '1')
    warm = _slot('warm-model', 'k0', latency=100.0)
    other = _slot('other-model', 'k1', latency=3000.0)
    dispatcher = _dispatcher(warm, other)

    with conv_affinity(f'conv-{uuid.uuid4().hex}'):
        assert dispatcher.pick_slot(capability='text') is warm
        dispatcher.note_shared_contention(warm, per_credential=True)
        assert dispatcher.pick_slot(
            capability='text', rotate_models={'warm-model': 1}) is warm


def test_a_rejecting_fast_tier_yields_to_a_quiet_model(monkeypatch):
    index = aa.RoutingSpeedIndex({
        'quick': aa.RoutingSpeed(160.0, 32.6, True),
        'steady': aa.RoutingSpeed(40.0, 30.0, False),
    })
    monkeypatch.setattr(aa, 'routing_speed_index', lambda: index)
    quick_a = _slot('quick', 'a', latency=1000.0)
    quick_b = _slot('quick', 'b', latency=1000.0)
    steady = _slot('steady', 'c', latency=5000.0)
    dispatcher = _dispatcher(quick_a, quick_b, steady)

    assert dispatcher.pick_slot(
        capability='cheap', prefer_fast=True) in (quick_a, quick_b)
    dispatcher.note_shared_contention(quick_a, per_credential=True)
    assert dispatcher.pick_slot(capability='cheap', prefer_fast=True) is steady


def test_measured_route_speed_classifies_models_aa_never_lists(monkeypatch):
    monkeypatch.setattr(aa, 'routing_speed_index', aa.RoutingSpeedIndex)
    internal_fast = _slot('internal-flash', 'a', latency=3000.0, throughput=150.0)
    internal_mid = _slot('internal-mid', 'b', latency=500.0, throughput=60.0)
    internal_slow = _slot('internal-reasoner', 'c', latency=100.0, throughput=20.0)
    dispatcher = _dispatcher(internal_fast, internal_mid, internal_slow)

    assert dispatcher.pick_slot(capability='cheap') is internal_slow
    assert dispatcher.pick_slot(
        capability='cheap', prefer_fast=True) is internal_fast


def test_aa_evidence_outranks_measured_route_speed(monkeypatch):
    index = aa.RoutingSpeedIndex({'listed': aa.RoutingSpeed(30.0, 40.0, False)})
    monkeypatch.setattr(aa, 'routing_speed_index', lambda: index)
    listed = _slot('listed', 'a', latency=3000.0, throughput=500.0)
    plain = _slot('plain', 'b', latency=100.0, throughput=10.0)
    middle = _slot('middle', 'c', latency=200.0, throughput=50.0)
    dispatcher = _dispatcher(listed, plain, middle)

    # Measured throughput alone would call ``listed`` fast; AA says it is not.
    assert dispatcher.pick_slot(capability='cheap', prefer_fast=True) is plain


def test_measured_speed_is_each_models_median_against_the_median_model():
    evidence = measured_speed_evidence([
        _slot('a', 'k0', throughput=100.0),
        _slot('a', 'k1', throughput=300.0),
        _slot('b', 'k2', throughput=50.0),
        _slot('unmeasured', 'k3'),
        _slot('d', 'k4', throughput=120.0),
    ])

    assert sorted(evidence) == ['a', 'b', 'd']
    assert evidence['a'].output_tokens_per_second == 200.0
    assert [model for model, speed in evidence.items() if speed.fast] == ['a']
    assert measured_speed_evidence(
        [_slot('solo', 'k5', throughput=90.0)])['solo'].fast is False
    assert measured_speed_evidence([]) == {}


@pytest.mark.parametrize('entry', ['dispatch_chat', 'dispatch_stream'])
def test_a_budgeted_dispatch_spends_its_429s_on_distinct_models(
        monkeypatch, entry):
    """End to end, the 2026-11-12 shape: two saturated Models with two AppIds
    each score best and a slower third Model serves. Rotating keys spent a
    three-response budget on the saturated Models; rotating Models reaches
    the serving one on the third attempt."""
    from lib.llm.llm_errors import RateLimitError
    from lib.llm_dispatch import api
    from lib.llm_dispatch.conv_affinity import conv_affinity_cleared

    saturated = [_slot(model, f'{model}-{appid}', latency=100.0)
                 for model in ('gemini-a', 'gemini-b')
                 for appid in ('k0', 'k1')]
    serving = _slot('deepseek', 'deepseek-k0', latency=5000.0)
    dispatcher = _dispatcher(*saturated, serving)
    monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
    monkeypatch.setattr('lib.key_stats.record_outcome', lambda *a, **k: None)
    monkeypatch.setattr('lib.key_stats.record_rate_limit', lambda *a, **k: False)
    monkeypatch.setattr('lib.llm_dispatch.api.time.sleep', lambda _seconds: None)
    by_key = {slot.api_key: slot for slot in dispatcher.slots}
    used = []

    def respond(api_key):
        used.append(api_key)
        if api_key != serving.api_key:
            raise RateLimitError(
                'API HTTP 429: {"status":429,"message":"App:**4861在模型:'
                f'{by_key[api_key].model}每分钟请求次数超过限制"}}',
                status_code=429, is_shared_contention=True)

    def fake_chat(**kwargs):
        respond(kwargs['api_key'])
        return 'translated', {'completion_tokens': 3}

    def fake_stream(_body, **kwargs):
        respond(kwargs['api_key'])
        return 'translated', 'stop', {'completion_tokens': 3}

    monkeypatch.setattr('lib.llm.chat', fake_chat)
    monkeypatch.setattr('lib.llm.stream_chat', fake_stream)

    with conv_affinity_cleared():
        result = getattr(api, entry)(
            [{'role': 'user', 'content': 'hi'}], capability='cheap',
            max_429_attempts=3, log_prefix='[t]')

    tried = [by_key[api_key] for api_key in used]
    assert result[0] == 'translated'
    # Which saturated Model leads is incidental (their scores tie); that every
    # attempt reaches a different Model, ending on the serving one, is not.
    assert len({slot.logical_model for slot in tried}) == len(tried) == 3
    assert tried[-1] is serving
    # Each limit armed only its own AppId lane, exactly as the gateway's
    # per-minute wording demands; rotation, not a project gate, moved on.
    assert {(row['credential'], row['scope'])
            for row in dispatcher.get_shared_contention_info()} == {
        (slot.key_name, 'credential') for slot in tried[:2]}
