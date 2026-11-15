"""Translation's fast preference never narrows eligibility.

``prefer_fast`` asks the picker for a Model Artificial Analysis measures as
fast. Among non-cooling fast candidates it takes the soonest-admissible gates,
then the highest Intelligence Index, then the ordinary score. A cooled fast
tier, a missing or stale AA snapshot, an explicit model and a provider pin all
keep the ordinary selection.
"""

from __future__ import annotations

import time

import pytest

from lib.llm_dispatch.dispatcher import LLMDispatcher
from lib.llm_dispatch.provider_pin import provider_pin
from lib.llm_dispatch.slot import Slot
from lib.model_catalog import aa


pytestmark = pytest.mark.unit

_INDEX = aa.RoutingSpeedIndex({
    "quicksmart": aa.RoutingSpeed(160.0, 32.6, True),
    "quickplain": aa.RoutingSpeed(120.0, 27.9, True),
    "quickunscored": aa.RoutingSpeed(130.0, None, True),
    "slowsmart": aa.RoutingSpeed(32.0, 46.5, False),
})


def _slot(model, *, latency=1000.0, key=None):
    return Slot(
        key_name=key or f"key-{model}", api_key="sk-test",
        model=f"wire-{model}", logical_model=model,
        capabilities={"text", "cheap"}, provider_id="provider-a",
        latency_ema=latency)


@pytest.fixture
def index_reads(monkeypatch):
    reads = []

    def routing_speed_index():
        reads.append(True)
        return _INDEX

    monkeypatch.setattr("lib.key_stats.is_key_enabled", lambda *_a, **_kw: True)
    monkeypatch.setattr(aa, "routing_speed_index", routing_speed_index)
    return reads


def _dispatcher(*slots):
    dispatcher = LLMDispatcher()
    dispatcher.slots = list(slots)
    dispatcher._initialized = True
    return dispatcher


def test_fast_tier_beats_a_better_scoring_slow_model(index_reads):
    slow = _slot("slow-smart", latency=100.0)
    quick = _slot("quick-smart", latency=3000.0)
    dispatcher = _dispatcher(slow, quick)

    assert dispatcher.pick_slot(capability="cheap") is slow
    assert dispatcher.pick_slot(capability="cheap", prefer_fast=True) is quick
    assert dispatcher.pick_and_reserve(capability="cheap", prefer_fast=True) is quick


def test_highest_intelligence_fast_model_leads_and_unscored_comes_last(index_reads):
    smart = _slot("quick-smart", latency=3000.0)
    plain = _slot("quick-plain", latency=100.0)
    unscored = _slot("quick-unscored", latency=50.0)
    dispatcher = _dispatcher(unscored, plain, smart)

    assert dispatcher.pick_slot(capability="cheap", prefer_fast=True) is smart
    assert dispatcher.pick_slot(
        capability="cheap", prefer_fast=True,
        exclude_models={"wire-quick-smart"}) is plain
    assert dispatcher.pick_slot(
        capability="cheap", prefer_fast=True,
        exclude_models={"wire-quick-smart", "wire-quick-plain"}) is unscored


def test_load_still_spreads_across_the_chosen_models_slots(index_reads):
    busy = _slot("quick-smart", key="busy", latency=100.0)
    idle = _slot("quick-smart", key="idle", latency=100.0)
    busy.inflight = 3
    dispatcher = _dispatcher(busy, idle, _slot("quick-plain", latency=10.0))

    assert dispatcher.pick_slot(capability="cheap", prefer_fast=True) is idle


def test_soonest_admissible_gate_beats_a_smarter_model_still_waiting(index_reads):
    smart = _slot("quick-smart", latency=100.0)
    plain = _slot("quick-plain", latency=3000.0)
    slow = _slot("slow-smart", latency=100.0)
    dispatcher = _dispatcher(smart, plain, slow)
    dispatcher.note_shared_contention(smart)

    assert dispatcher.pick_slot(capability="cheap") is slow
    assert dispatcher.pick_slot(capability="cheap", prefer_fast=True) is plain


def test_cooled_fast_tier_falls_back_to_the_whole_pool(index_reads):
    smart = _slot("quick-smart")
    plain = _slot("quick-plain")
    slow = _slot("slow-smart", latency=5000.0)
    for cooled in (smart, plain):
        cooled.cooldown_until = time.time() + 60
    dispatcher = _dispatcher(smart, plain, slow)

    assert dispatcher.pick_slot(capability="cheap", prefer_fast=True) is slow


def test_missing_speed_evidence_keeps_the_ordinary_selection(monkeypatch, index_reads):
    monkeypatch.setattr(aa, "routing_speed_index", aa.RoutingSpeedIndex)
    slow = _slot("slow-smart", latency=100.0)
    dispatcher = _dispatcher(slow, _slot("quick-smart", latency=3000.0))

    assert dispatcher.pick_slot(capability="cheap", prefer_fast=True) is slow


def test_explicit_model_and_provider_pin_outrank_the_tier(index_reads):
    slow = _slot("slow-smart", latency=3000.0)
    dispatcher = _dispatcher(slow, _slot("quick-smart", latency=100.0))

    assert dispatcher.pick_slot(
        capability="cheap", prefer_model="slow-smart", prefer_fast=True) is slow

    pin = "model-route:7:a"
    group = [
        Slot(key_name=f"ephemeral-{model}", api_key="sk-request",
             model=f"wire-{model}", logical_model=model,
             capabilities={"text", "cheap"}, provider_id=pin,
             request_scoped=True, routing_provider_id="provider-a",
             routing_owner_user_id=7, route_credential_id=f"credential-{model}",
             route_deployment_id=f"deployment-{rank}", route_rank=rank)
        for rank, model in enumerate(("slow-smart", "quick-smart"))
    ]
    dispatcher.slots = [*dispatcher.slots, *group]
    with provider_pin(pin):
        assert dispatcher.pick_slot(capability="cheap", prefer_fast=True) is group[0]
    assert index_reads == [], "an explicit model or pin must not consult AA"


class _PickSpy:
    def __init__(self):
        self.slots = []
        self.picks = []

    def pick_and_reserve(self, **kwargs):
        self.picks.append(kwargs)
        return None

    def has_capable_slots(self, *_args, **_kwargs):
        return False

    def summarize_slots(self, *_args, **_kwargs):
        return "pick-spy"


@pytest.mark.parametrize("entry", ["dispatch_chat", "smart_chat", "dispatch_stream"])
def test_dispatch_entry_points_forward_only_an_explicit_preference(monkeypatch, entry):
    from lib.llm_dispatch import api

    spy = _PickSpy()
    monkeypatch.setattr(api, "get_dispatcher", lambda: spy)
    call = getattr(api, entry)
    messages = [{"role": "user", "content": "hi"}]

    with pytest.raises(Exception):
        call(messages, capability="cheap", prefer_fast=True, log_prefix="[t]")
    assert spy.picks and all(pick.get("prefer_fast") is True for pick in spy.picks)

    spy.picks.clear()
    with pytest.raises(Exception):
        call(messages, capability="cheap", log_prefix="[t]")
    assert spy.picks and all("prefer_fast" not in pick for pick in spy.picks)
