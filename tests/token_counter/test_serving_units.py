"""Serving-unit measurement: provider counts bound to request ledgers.

``serving_units`` converts Tofu's local tiktoken count into the serving
model's own units. A provider count applies only to the content-free ledger of
the request it measured; every change since is charged from the family's
measured provider/local range (``lib/model_info/token_unit_facts.json``), and
without a usable provider count the local count is charged at the measured
maximum. These specs pin that arithmetic and the store's retention rules.
"""
from __future__ import annotations

import copy
import hashlib
import math
from uuid import uuid4

import pytest

from lib.model_info.token_units import (
    TokenUnitFactError,
    token_unit_fact,
    token_unit_facts,
    token_unit_family,
    validate_token_unit_catalog,
)
from lib.token_counter import RequestTextCounts, count_tokens
from lib.token_counter import usage_cache
from lib.token_counter.serving_units import (
    SOURCE_LOCAL_UNBOUNDED,
    SOURCE_MEASURED_BOUND,
    SOURCE_PROVIDER_COUNT,
    ServingMeasurement,
    measure_body,
    measure_request,
)
from lib.token_counter.tiktoken_counter import RequestLedger, request_ledger

pytestmark = pytest.mark.unit

CLAUDE = 'claude-opus-5.5'
KIMI = 'kimi-k3.1-pro'
ENCODING = 'cl100k_base'


def _identity(tag: str) -> bytes:
    return hashlib.sha256(tag.encode()).digest()


def _ledger(*entries: tuple[str, int], opaque: tuple[str, ...] = ()) -> RequestLedger:
    pairs = tuple((_identity(tag), tokens) for tag, tokens in entries)
    return RequestLedger(ENCODING, 400 + sum(tokens for _, tokens in pairs), pairs,
                         frozenset(_identity(tag) for tag in opaque))


@pytest.fixture
def owner():
    key = 'serving-units-' + uuid4().hex
    yield key
    usage_cache.invalidate(key)


def test_maintained_facts_validate_and_name_their_families():
    facts = token_unit_facts()
    assert {'claude', 'kimi', 'deepseek'} <= set(facts)
    for family, fact in facts.items():
        assert fact.family == family
        assert 0 < fact.minimum <= fact.maximum
        assert sum(count for _, count in fact.members) == fact.samples
        assert all(token_unit_family(model) == family for model, _ in fact.members)
    assert token_unit_family(CLAUDE) == 'claude'
    assert token_unit_family(KIMI) == 'kimi'
    assert token_unit_fact('mystery-model-1') is None


@pytest.mark.parametrize('mutate, field', [
    (lambda fact: fact.update(samples=fact['samples'] + 1), 'families.kimi.members'),
    (lambda fact: fact['members'].update({'claude-opus-5.5': 1}), 'families.kimi.members'),
    (lambda fact: fact.update(measured_from='2027-01-01'), 'families.kimi.measured_from'),
    (lambda fact: fact['provider_per_local'].update(min=2.0), 'families.kimi.provider_per_local'),
    (lambda fact: fact.pop('conversations'), 'families.kimi'),
])
def test_catalog_without_complete_provenance_is_refused(mutate, field):
    import json

    from lib.model_info.token_units import CATALOG_PATH

    document = json.loads(CATALOG_PATH.read_text(encoding='utf-8'))
    mutate(document['families']['kimi'])
    with pytest.raises(TokenUnitFactError) as refused:
        validate_token_unit_catalog(document)
    assert refused.value.field == field


def test_cold_request_is_charged_at_the_measured_maximum():
    fact = token_unit_fact(CLAUDE)
    measured = measure_request(model=CLAUDE, local_tokens=100_000,
                               local_encoding=ENCODING)
    assert measured.source == SOURCE_MEASURED_BOUND
    assert measured.tokens == measured.cold_bound_tokens == math.ceil(fact.maximum * 100_000)
    assert measured.bounded


def test_unmeasured_family_or_non_tiktoken_count_is_local_unbounded():
    assert measure_request(model='mystery-model-1', local_tokens=5_000,
                           local_encoding=ENCODING).source == SOURCE_LOCAL_UNBOUNDED
    heuristic = measure_request(model=CLAUDE, local_tokens=5_000, local_encoding=None)
    assert (heuristic.source, heuristic.tokens, heuristic.bounded) == (
        SOURCE_LOCAL_UNBOUNDED, 5_000, False)


def test_provider_count_plus_changed_entries_measures_the_next_request(owner):
    fact = token_unit_fact(CLAUDE)
    first = _ledger(('tools', 2_000), ('system', 1_000), ('history', 100_000),
                    ('user-1', 10_000))
    usage_cache.record_provider_count(owner, model=CLAUDE, provider_tokens=181_440,
                                      ledger=first)
    # One round later: the old user message was replaced and two were appended.
    second = _ledger(('tools', 2_000), ('system', 1_000), ('history', 100_000),
                     ('user-1b', 4_000), ('assistant-1', 3_000), ('tool-1', 5_000))

    measured = measure_request(model=CLAUDE, local_tokens=second.total,
                               ledger=second, owner_key=owner)

    expected = (181_440 + math.ceil(fact.maximum * 12_000)
                - math.floor(fact.minimum * 10_000))
    assert measured.source == SOURCE_PROVIDER_COUNT
    assert measured.tokens == expected < measured.cold_bound_tokens
    assert measured.provider_tokens == 181_440
    assert measured.ledger is second
    assert measured.evidence()['providerTokens'] == 181_440


def test_repeated_identical_messages_are_matched_by_multiplicity(owner):
    once = _ledger(('system', 1_000), ('ack', 10))
    usage_cache.record_provider_count(owner, model=KIMI, provider_tokens=1_500,
                                      ledger=once)
    twice = _ledger(('system', 1_000), ('ack', 10), ('ack', 10))
    measured = measure_request(model=KIMI, local_tokens=twice.total, ledger=twice,
                               owner_key=owner)
    assert measured.tokens == 1_500 + math.ceil(token_unit_fact(KIMI).maximum * 10)


def test_cold_bound_wins_when_tighter_than_the_anchored_count(owner):
    """After compaction removes most of a request the cold bound is tighter."""
    big = _ledger(('system', 1_000), ('history', 400_000))
    usage_cache.record_provider_count(owner, model=CLAUDE, provider_tokens=780_000,
                                      ledger=big)
    folded = _ledger(('system', 1_000), ('summary', 6_000))
    measured = measure_request(model=CLAUDE, local_tokens=folded.total,
                               ledger=folded, owner_key=owner)
    assert measured.source == SOURCE_MEASURED_BOUND
    assert measured.tokens == measured.cold_bound_tokens


def test_cold_bound_keeps_the_opaque_reasoning_it_cannot_see(owner):
    """Ciphertext is invisible locally; only the provider's reserve covers it."""
    big = _ledger(('system', 1_000), ('history', 400_000))
    usage_cache.record_provider_count(owner, model=CLAUDE, provider_tokens=790_000,
                                      ledger=big, opaque_replay_tokens=700)
    folded = _ledger(('system', 1_000), ('summary', 6_000), ('assistant', 50),
                     opaque=('assistant',))

    measured = measure_request(model=CLAUDE, local_tokens=folded.total,
                               ledger=folded, owner_key=owner)

    assert measured.source == SOURCE_MEASURED_BOUND
    assert measured.tokens == measured.cold_bound_tokens + 700
    assert measured.evidence()['opaqueReplayTokens'] == 700
    # The selected-slot guard keeps the reserve on the same model.
    assert measure_body(model=CLAUDE, local_tokens=folded.total,
                        admitted=measured).tokens == measured.tokens


def test_provider_count_outside_the_measured_range_is_trusted_over_the_fact(owner):
    fact = token_unit_fact(CLAUDE)
    first = _ledger(('system', 1_000), ('blob', 9_000))
    provider = math.ceil(first.total * fact.maximum * 1.2)
    usage_cache.record_provider_count(owner, model=CLAUDE, provider_tokens=provider,
                                      ledger=first)
    second = _ledger(('system', 1_000), ('blob', 9_000), ('more', 1_000))
    measured = measure_request(model=CLAUDE, local_tokens=second.total,
                               ledger=second, owner_key=owner)
    observed = provider / first.total
    assert measured.source == SOURCE_PROVIDER_COUNT
    assert measured.tokens == provider + math.ceil(observed * 1_000)
    assert measured.tokens > measured.cold_bound_tokens


def test_opaque_reasoning_is_added_once_when_its_message_replays(owner):
    first = _ledger(('system', 1_000), ('user', 2_000))
    usage_cache.record_provider_count(owner, model=CLAUDE, provider_tokens=5_000,
                                      ledger=first, opaque_replay_tokens=700)
    plain = _ledger(('system', 1_000), ('user', 2_000), ('assistant', 50))
    replayed = _ledger(('system', 1_000), ('user', 2_000), ('assistant', 50),
                       opaque=('assistant',))
    high = token_unit_fact(CLAUDE).maximum
    assert measure_request(model=CLAUDE, local_tokens=plain.total, ledger=plain,
                           owner_key=owner).tokens == 5_000 + math.ceil(high * 50)
    assert measure_request(model=CLAUDE, local_tokens=replayed.total,
                           ledger=replayed, owner_key=owner).tokens == (
        5_000 + math.ceil(high * 50) + 700)


def test_provider_count_applies_only_to_the_model_that_counted_it(owner):
    ledger = _ledger(('system', 1_000), ('user', 50_000))
    usage_cache.record_provider_count(owner, model=CLAUDE, provider_tokens=80_000,
                                      ledger=ledger)
    on_kimi = measure_request(model=KIMI, local_tokens=ledger.total, ledger=ledger,
                              owner_key=owner)
    assert on_kimi.source == SOURCE_MEASURED_BOUND
    assert on_kimi.model == KIMI


def test_a_count_without_a_ledger_keeps_the_count_bound_to_content(owner):
    """A rebuilt body's count is unbound; the earlier bound count stays."""
    ledger = _ledger(('system', 1_000))
    usage_cache.record_provider_count(owner, model=CLAUDE, provider_tokens=2_000,
                                      ledger=ledger)
    usage_cache.record_provider_count(owner, model=CLAUDE, provider_tokens=9_000,
                                      ledger=None)
    assert usage_cache.provider_count(owner, CLAUDE).provider_tokens == 2_000
    usage_cache.record_provider_count(owner, model=KIMI, provider_tokens=9_000,
                                      ledger=None)
    assert usage_cache.provider_count(owner, KIMI) is None


def test_counts_never_expire_and_capacity_bounds_retention(monkeypatch):
    usage_cache._reset_usage_cache_for_tests()
    monkeypatch.setattr(usage_cache, '_USAGE_CACHE_CAPACITY', 2)
    ledger = _ledger(('system', 1_000))
    owners = ['capacity-' + uuid4().hex for _ in range(3)]
    try:
        for key in owners:
            usage_cache.record_provider_count(key, model=KIMI, provider_tokens=1_100,
                                              ledger=ledger)
        assert usage_cache.provider_count(owners[0], KIMI) is None
        assert usage_cache.provider_count(owners[2], KIMI).provider_tokens == 1_100
        snapshot = usage_cache.usage_cache_snapshot()
        assert snapshot['entries'] == 2
        assert snapshot['capacityEvictions'] == 1
        assert 'ttlSeconds' not in snapshot
    finally:
        usage_cache._reset_usage_cache_for_tests()


def test_invalidate_drops_every_model_of_one_owner_only(owner):
    other = owner + '-sibling'
    ledger = _ledger(('system', 1_000))
    try:
        for key, model in ((owner, CLAUDE), (owner, KIMI), (other, CLAUDE)):
            usage_cache.record_provider_count(key, model=model, provider_tokens=1_500,
                                              ledger=ledger)
        usage_cache.invalidate(owner)
        assert usage_cache.provider_count(owner, CLAUDE) is None
        assert usage_cache.provider_count(owner, KIMI) is None
        assert usage_cache.provider_count(other, CLAUDE) is not None
    finally:
        usage_cache.invalidate(other)


def test_measure_body_reuses_admission_only_for_the_model_it_measured():
    admitted = ServingMeasurement(
        tokens=60_000, model=KIMI, source=SOURCE_PROVIDER_COUNT, local_tokens=58_000,
        cold_bound_tokens=70_000, family='kimi', bounded=True, provider_tokens=59_000)
    high = token_unit_fact(KIMI).maximum
    same = measure_body(model=KIMI, local_tokens=58_500, admitted=admitted)
    assert (same.tokens, same.source) == (60_000 + math.ceil(high * 500),
                                          SOURCE_PROVIDER_COUNT)
    other = measure_body(model=CLAUDE, local_tokens=58_500, admitted=admitted)
    assert other.source == SOURCE_MEASURED_BOUND
    assert other.tokens == math.ceil(token_unit_fact(CLAUDE).maximum * 58_500)
    # A malformed carrier is no evidence.
    assert measure_body(model=KIMI, local_tokens=58_500,
                        admitted={'model': KIMI, 'tokens': 1}).source == SOURCE_MEASURED_BOUND


def _count(messages, tools, memo):
    view = memo.view('request')
    local = count_tokens(messages, model=CLAUDE, tools=tools, mode='tiktoken',
                         text_count_memo=view)['tokens']
    return local, request_ledger(messages, tools, model=CLAUDE, view=view)


def test_request_ledger_equals_the_stateless_local_count_and_tracks_content():
    tools = [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {
        'type': 'object', 'properties': {'query': {'type': 'string'}}}}}]
    messages = [{'role': 'system', 'content': 'Follow the scoped policy.'},
                {'role': 'user', 'content': 'Read lib/mod.py and report.'},
                {'role': 'assistant', 'content': '', 'tool_calls': [{
                    'id': 'c1', 'type': 'function',
                    'function': {'name': 'lookup', 'arguments': '{"query":"mod"}'}}]},
                {'role': 'tool', 'tool_call_id': 'c1', 'content': 'def f(): pass'}]
    memo = RequestTextCounts()
    local, ledger = _count(messages, tools, memo)
    stateless = count_tokens(copy.deepcopy(messages), model=CLAUDE, tools=tools,
                             mode='tiktoken')['tokens']
    assert ledger.total == local == stateless
    assert len(ledger.entries) == 1 + len(messages)
    assert ledger.tool_tokens == ledger.entries[0][1] > 0

    _, again = _count(copy.deepcopy(messages), tools, memo)
    assert again.entries == ledger.entries

    edited = copy.deepcopy(messages)
    edited[1]['content'] = 'Read lib/other.py and report.'
    _, changed = _count(edited, tools, memo)
    assert changed.entries[2][0] != ledger.entries[2][0]
    assert [entry for index, entry in enumerate(changed.entries) if index != 2] == [
        entry for index, entry in enumerate(ledger.entries) if index != 2]
