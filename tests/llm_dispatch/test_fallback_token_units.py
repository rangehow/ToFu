"""A fallback, failover or pool rescue re-checks the request in its own units.

Every slot attempt passes the selected-slot guard
(``_adapt_stream_body_for_slot`` -> ``enforce_body_input_limit``). The guard
compared Tofu's local tiktoken count with the slot's window, so a request
admitted for kimi-k3.1-pro reached claude-opus-5.5 through pool rescue and
Claude refused it: "prompt too long: 1,075,092 > 1,000,000" (worker
12d175fc, turn 411d2578, R247, 2026-11-09). It recurred on 2026-11-13 for a
resumed request with no Claude provider count (worker a0f217eb, R1437). Claude
counted 1.09-2.22x Tofu's local count by 2026-11-13
(``lib/model_info/token_unit_facts.json``).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib.llm.llm_errors import PromptTooLongError
from lib.llm_dispatch.slot import Slot
from lib.token_counter.evidence import ADMITTED_INPUT_TOKENS_KEY

pytestmark = pytest.mark.unit

KIMI = 'kimi-k3.1-pro'
CLAUDE = 'claude-opus-5.5'

# Turn 411d2578 of worker-12d175fc72d5ca8423588a9a, from its persisted rounds.
# R246 is the last measured request; R247 appended one more round to it, was
# admitted on kimi and rescued onto claude-opus-5.5 after a 402.
R246_LOCAL_TOKENS = 657_055
R246_KIMI_TOKENS = 682_153
R247_CLAUDE_TOKENS = 1_075_092
CLAUDE_WINDOW = 1_000_000
KIMI_WINDOW = 1_048_576

# Conversation worker-a0f217ebc2af3d51e6a47e4e, 2026-11-13 23:29:44: after the
# restart the resumed attempt 0cc61a84 sent R1437 to claude-opus-5.5 with no
# Claude provider count, at Tofu's count of 650,002; Claude refused it with
# "prompt is too long: 1016533 tokens > 1000000 maximum".
R1437_LOCAL_TOKENS = 650_002
R1437_CLAUDE_TOKENS = 1_016_533


def _slot(logical_model: str, window: int) -> Slot:
    return Slot(key_name='test', api_key='', model='gateway-' + logical_model,
                logical_model=logical_model, capabilities={'text'},
                context_window=window, provider_id='request-a',
                routing_provider_id='gateway', routing_owner_user_id=7,
                request_scoped=True)


@pytest.fixture
def routes(monkeypatch):
    from lib.llm_dispatch.slot_pool import SlotPool
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher',
                        lambda: SimpleNamespace(slots=SlotPool([])))
    monkeypatch.setattr('lib.context_limits.resolve_learned_context_limit',
                        lambda provider, model, window: window)


def _adapt(slot: Slot, body: dict) -> dict:
    from lib.llm_dispatch._api_stream_state import _adapt_stream_body_for_slot
    return _adapt_stream_body_for_slot(
        slot, dict(body), True, tools=None, max_tokens=1_024, temperature=1,
        thinking_enabled=False, preset='medium', effort=None)


def test_kimi_admitted_request_rescued_onto_claude_is_refused_before_transport(
        routes, monkeypatch):
    """The exact R247 failure: 1,075,092 > 1,000,000 must never reach Claude."""
    monkeypatch.setattr(
        'lib.token_counter.count_tokens',
        lambda *args, **kwargs: {'tokens': R246_LOCAL_TOKENS, 'method': 'tiktoken'})
    body = {'model': KIMI, 'max_tokens': 1_024,
            'messages': [{'role': 'user', 'content': 'Continue the audit.'}],
            ADMITTED_INPUT_TOKENS_KEY: R246_KIMI_TOKENS}

    # The kimi route holds the request in kimi's own units.
    assert _adapt(_slot(KIMI, KIMI_WINDOW), body)['model'] == 'gateway-' + KIMI

    with pytest.raises(PromptTooLongError) as refused:
        _adapt(_slot(CLAUDE, CLAUDE_WINDOW), body)

    assert refused.value.origin == 'local'
    assert refused.value.context_window_tokens == CLAUDE_WINDOW
    # Claude's own count of that request is inside what the guard charged.
    assert refused.value.total_tokens >= R247_CLAUDE_TOKENS > CLAUDE_WINDOW


def test_resumed_request_without_a_claude_count_is_refused_before_transport(
        routes, monkeypatch):
    """Tonight's R1437: 1,016,533 > 1,000,000 must never reach Claude."""
    monkeypatch.setattr(
        'lib.token_counter.count_tokens',
        lambda *args, **kwargs: {'tokens': R1437_LOCAL_TOKENS, 'method': 'tiktoken'})
    body = {'model': CLAUDE, 'max_tokens': 1_024,
            'messages': [{'role': 'user', 'content': 'Continue the assigned audit.'}]}

    with pytest.raises(PromptTooLongError) as refused:
        _adapt(_slot(CLAUDE, CLAUDE_WINDOW), body)

    assert refused.value.origin == 'local'
    assert refused.value.context_window_tokens == CLAUDE_WINDOW
    assert refused.value.total_tokens >= R1437_CLAUDE_TOKENS > CLAUDE_WINDOW


def _round_body() -> tuple[dict, int]:
    """A round body admitted for the primary model, as the root loop builds it."""
    from lib.token_counter import count_tokens

    messages = [{'role': 'system', 'content': 'Follow the scoped policy.'},
                {'role': 'user', 'content': ' '.join(
                    f'line {index} of retained scoped evidence'
                    for index in range(6_000))}]
    local = count_tokens(messages, model=KIMI, mode='tiktoken')['tokens']
    body = {'model': KIMI, 'messages': messages, 'max_tokens': 1_024,
            ADMITTED_INPUT_TOKENS_KEY: local}
    return body, local


def test_fallback_to_a_smaller_window_model_is_refused_in_its_units(routes):
    body, local = _round_body()
    # The fallback window holds Tofu's local count, not Claude's count of it:
    # every measured Claude request counted at least 1.11x the local count.
    window = int(local * 1.1)

    with pytest.raises(PromptTooLongError) as refused:
        _adapt(_slot(CLAUDE, window), body)

    assert refused.value.origin == 'local'
    assert refused.value.context_window_tokens == window
    assert refused.value.total_tokens > window


def test_same_model_slot_keeps_the_admitted_serving_measurement(routes):
    """Root admission's provider-anchored count is not re-inflated on its model."""
    from lib.token_counter.evidence import ADMITTED_SERVING_UNITS_KEY
    from lib.token_counter.serving_units import (
        SOURCE_PROVIDER_COUNT, ServingMeasurement,
    )

    body, local = _round_body()
    body[ADMITTED_SERVING_UNITS_KEY] = ServingMeasurement(
        tokens=local, model=KIMI, source=SOURCE_PROVIDER_COUNT,
        local_tokens=local, cold_bound_tokens=local * 2, family='kimi',
        bounded=True, provider_tokens=local - 10)
    window = int(local * 1.1)

    adapted = _adapt(_slot(KIMI, window), body)

    assert adapted[ADMITTED_INPUT_TOKENS_KEY] == local
    with pytest.raises(PromptTooLongError):
        _adapt(_slot(CLAUDE, window), body)
