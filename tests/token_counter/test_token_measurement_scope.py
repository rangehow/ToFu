"""Provider counts measure only the request content they were bound to.

A provider-reported prompt count is kept with the content-free ledger of the
request it measured (``usage_cache``). The next request is measured as that
count plus a bounded charge for every ledger entry that changed: an edited,
replaced or reconstructed message is never reused at its old provider count,
another model's count never applies, and provider-owned (native compaction)
input never binds to the original body.
"""
from copy import deepcopy
import math
from uuid import uuid4

import pytest

from lib.model_info.token_units import token_unit_fact
from lib.token_counter import RequestTextCounts, count_tokens, usage_cache
from lib.token_counter.serving_units import measure_request
from lib.token_counter.tiktoken_counter import request_ledger

pytestmark = pytest.mark.unit

MODEL = 'claude-opus-5.5'


@pytest.fixture
def owner():
    key = 'scope-' + uuid4().hex
    yield key
    usage_cache.invalidate(key)


def _measure(messages, tools=None, *, owner_key='', model=MODEL):
    view = RequestTextCounts().view('request')
    local = count_tokens(messages, model=model, tools=tools, mode='tiktoken',
                         text_count_memo=view)['tokens']
    ledger = request_ledger(messages, tools, model=model, view=view)
    return measure_request(model=model, local_tokens=local, ledger=ledger,
                           owner_key=owner_key)


def _local(messages, tools=None):
    """Local tokens of exactly these messages and tools, without overhead."""
    empty = count_tokens([], model=MODEL, mode='tiktoken')['tokens']
    return count_tokens(messages, model=MODEL, tools=tools,
                        mode='tiktoken')['tokens'] - empty


def _history():
    return [{'role': 'system', 'content': 'Follow the scoped policy. ' * 40}] + [
        {'role': 'user' if index % 2 == 0 else 'assistant',
         'content': f'turn {index}: ' + 'evidence about lib/mod.py ' * 60}
        for index in range(8)]


def _anchor(owner_key, messages, tools=None):
    """Record a typical Claude count (1.6x local) for this exact request."""
    measured = _measure(messages, tools)
    provider = math.ceil(1.6 * measured.local_tokens)
    usage_cache.record_provider_count(owner_key, model=MODEL, provider_tokens=provider,
                                      ledger=measured.ledger)
    return provider


@pytest.mark.parametrize('change', ['early', 'late', 'tool_call', 'image'])
def test_an_edited_message_is_charged_as_a_change_never_reused(owner, change):
    fact = token_unit_fact(MODEL)
    messages = _history()
    provider = _anchor(owner, messages)
    edited = deepcopy(messages)
    index = 1 if change == 'early' else len(edited) - 1
    if change in ('early', 'late'):
        edited[index]['content'] += ' changed after the measurement'
    elif change == 'tool_call':
        edited[index]['tool_calls'] = [{'id': 'x', 'type': 'function', 'function': {
            'name': 'lookup', 'arguments': '{"query": "bulk"}'}}]
    else:
        edited[index]['content'] = [
            {'type': 'text', 'text': edited[index]['content']},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}}]

    measured = _measure(edited, owner_key=owner)

    old, new = _local([messages[index]]), _local([edited[index]])
    assert measured.source == 'provider_count'
    assert measured.tokens == (provider + math.ceil(fact.maximum * new)
                               - math.floor(fact.minimum * old))


def test_a_changed_tool_surface_is_charged_as_a_change(owner):
    fact = token_unit_fact(MODEL)
    messages = _history()
    tools = [{'type': 'function', 'function': {'name': 'one', 'description': 'first'}}]
    changed = [{'type': 'function', 'function': {'name': 'one', 'description': 'changed'}}]
    provider = _anchor(owner, messages, tools)

    measured = _measure(messages, changed, owner_key=owner)

    assert measured.source == 'provider_count'
    assert measured.tokens == (provider + math.ceil(fact.maximum * _local([], changed))
                               - math.floor(fact.minimum * _local([], tools)))


def test_another_models_count_never_applies(owner):
    messages = _history()
    _anchor(owner, messages)
    on_kimi = _measure(messages, owner_key=owner, model='kimi-k3.1-pro')
    assert on_kimi.source == 'measured_bound'
    assert on_kimi.provider_tokens is None


def test_reconstructed_history_cannot_reuse_the_previous_count(owner):
    """A historical count never caps a fresh request's measurement.

    Every reconstructed message is new content, charged at the measured
    maximum; the old count contributes only what it measured and is gone.
    """
    fact = token_unit_fact(MODEL)
    _anchor(owner, [{'role': 'user', 'content': 'compact prior request'}])
    reconstructed = [{'role': 'user', 'content': f'reconstructed {index}'}
                     for index in range(3_361)]
    measured = _measure(reconstructed, owner_key=owner)
    assert math.ceil(fact.maximum * _local(reconstructed)) <= measured.tokens
    assert measured.tokens <= measured.cold_bound_tokens


def test_appending_counts_only_the_suffix_and_its_opaque_reasoning(owner):
    fact = token_unit_fact(MODEL)
    messages = _history()
    measured = _measure(messages)
    provider = math.ceil(1.6 * measured.local_tokens)
    usage_cache.record_provider_count(owner, model=MODEL, provider_tokens=provider,
                                      ledger=measured.ledger, opaque_replay_tokens=12_000)
    answer = {'role': 'assistant', 'content': 'answer', '_responses_items': [
        {'type': 'reasoning', 'encrypted_content': 'ciphertext' * 10_000}]}

    appended = _measure(messages + [answer], owner_key=owner)

    # Ciphertext is not text: only the visible answer is counted locally.
    assert appended.tokens == provider + math.ceil(fact.maximum * _local([answer])) + 12_000


def test_native_compaction_input_never_binds_to_the_original_body(owner):
    from lib.tasks_pkg.manager._stream import _record_stream_prompt_usage
    from lib.token_counter.evidence import ADMITTED_SERVING_UNITS_KEY

    messages = _history()
    body = {'messages': messages, ADMITTED_SERVING_UNITS_KEY: _measure(messages)}
    task = {'convId': owner}
    _record_stream_prompt_usage(task, body, {'prompt_tokens': 9_000}, {},
                                model=MODEL, prefix='test')
    assert usage_cache.provider_count(owner, MODEL).provider_tokens == 9_000

    total = _record_stream_prompt_usage(
        task, body, {'input_tokens': 100_000, 'effective_prompt_tokens': 1_000}, {},
        model=MODEL, prefix='test')

    assert total == 1_000
    assert usage_cache.provider_count(owner, MODEL) is None
