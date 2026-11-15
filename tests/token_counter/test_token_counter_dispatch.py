"""Token-counter dispatch cost contracts.

The request-wide heuristic is a network-tier prefilter and final fallback. It
must stay lazy when a local-tokenizer tier succeeds, and a network skip
followed by heuristic fallback must reuse the same scan.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import lib.token_counter.api as token_api
from lib.token_counter.heuristic import HeuristicCounter

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _default_dispatch_mode(monkeypatch):
    """Keep operator environment overrides out of dispatcher unit tests."""
    monkeypatch.setattr(token_api, 'MODE', 'auto')


@dataclass
class _Counter:
    name: str
    result: int | None
    needs_network: bool = False
    confidence: str = 'good'
    calls: int = 0

    def supports(self, _model: str) -> bool:
        return True

    def count(self, _messages: list, *, model: str, **_kwargs) -> int | None:
        self.calls += 1
        return self.result


@pytest.mark.parametrize('counter_name', ['hf', 'tiktoken'])
def test_successful_non_network_tier_skips_heuristic_scan(
    monkeypatch, counter_name,
):
    """A result already chosen by policy must not pay for a discarded scan."""
    counter = _Counter(counter_name, 77)
    monkeypatch.setattr(token_api, 'resolve', lambda _model: [counter])
    monkeypatch.setattr(
        token_api, 'cheap_estimate',
        lambda *a, **k: pytest.fail('eager request-wide heuristic scan'))

    result = token_api.count_tokens(
        [{'role': 'user', 'content': 'x' * 100_000}],
        model='gpt-4o', context_limit=128_000)

    assert result['tokens'] == 77
    assert result['method'] == counter_name
    assert counter.calls == 1


def test_network_skip_and_heuristic_fallback_share_one_scan(monkeypatch):
    """The prefilter becomes the fallback result instead of being recomputed."""
    network = _Counter('network', 99, needs_network=True)
    estimates = []

    def estimate(messages, *, system=None, tools=None, estimate_text=None):
        estimates.append((messages, system, tools))
        return 40

    monkeypatch.setattr(
        token_api, 'resolve',
        lambda _model: [network, HeuristicCounter()])
    monkeypatch.setattr(token_api, 'cheap_estimate', estimate)
    monkeypatch.setattr(token_api, 'API_THRESHOLD', 0.5)

    result = token_api.count_tokens(
        [{'role': 'user', 'content': 'small'}],
        model='claude-test', context_limit=100)

    assert result['tokens'] == 40
    assert result['method'] == 'heuristic'
    assert network.calls == 0
    assert len(estimates) == 1


def test_network_dispatch_computes_prefilter_once(monkeypatch):
    """An eligible network tier still receives the unchanged threshold gate."""
    network = _Counter('network', 88, needs_network=True, confidence='exact')
    estimates = []
    monkeypatch.setattr(token_api, 'resolve', lambda _model: [network])
    monkeypatch.setattr(
        token_api, 'cheap_estimate',
        lambda *a, **k: estimates.append((a, k)) or 60)
    monkeypatch.setattr(token_api, 'API_THRESHOLD', 0.5)

    result = token_api.count_tokens(
        [{'role': 'user', 'content': 'near limit'}],
        model='claude-test', context_limit=100)

    assert result['tokens'] == 88
    assert result['method'] == 'network'
    assert network.calls == 1
    assert len(estimates) == 1


def test_network_prefilter_reuses_remembered_request_text_estimates(monkeypatch):
    """Each round's prefilter rescanned the whole history with regexes."""
    import lib.token_counter.tiktoken_counter as tiktoken_counter
    from lib.token_counter.heuristic import cheap_estimate

    tiktoken_counter._reset_text_count_cache_for_tests()
    scanned = []
    original = tiktoken_counter._cheap_estimate_text
    monkeypatch.setattr(tiktoken_counter, '_cheap_estimate_text',
                        lambda text: scanned.append(text) or original(text))
    network = _Counter('network', 99, needs_network=True)
    monkeypatch.setattr(token_api, 'resolve',
                        lambda _model: [network, HeuristicCounter()])
    monkeypatch.setattr(token_api, 'API_THRESHOLD', 0.5)
    stable = 'stable tool output 上下文 ' * 60
    history = [{'role': 'tool', 'content': stable},
               {'role': 'user', 'content': 'short question'}]

    results = [token_api.count_tokens(history, model='claude-test',
                                      context_limit=10_000_000)
               for _round in range(2)]
    tiktoken_counter._reset_text_count_cache_for_tests()

    assert [result['tokens'] for result in results] == [cheap_estimate(history)] * 2
    assert network.calls == 0
    assert scanned.count(stable) == 1
