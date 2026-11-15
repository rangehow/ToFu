"""Admission and compaction measure each request in the serving model's units.

Tofu's local tokenizer (tiktoken) is not the tokenizer of most serving models:
Claude counted 1.09-2.22x Tofu's count of the same request by 2026-11-13
(measured facts in ``lib/model_info/token_unit_facts.json``). The Watchdog transcript reached
926-942K real Claude tokens while Tofu counted about 642K, so every request
was admitted untrimmed. Every admission and compaction decision therefore uses
the serving model's provider-reported count of the owner's previous request
plus a measured bound for what changed since, or the measured bound of the
local count when no provider count exists.
"""
from __future__ import annotations

import math
import threading
from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit

CLAUDE = 'claude-opus-5.5'
KIMI = 'kimi-k3.1-pro'


def _code(lines: int, tag: str) -> str:
    # Code-shaped text: the entropy heuristic reads it close to tiktoken, so
    # only the serving model's own count reveals the real request size.
    return '\n'.join(
        f'    {tag}_{index} = compute(path="lib/mod_{index}.py", '
        f'retries={index % 7}, ok=True)' for index in range(lines))


def _task(conv: str, model: str) -> dict:
    return {'id': 'task-' + uuid4().hex, 'convId': conv, '_userId': 1,
            '_turnId': 'turn', '_attemptId': 'attempt', 'toolRounds': [],
            'model': model, 'config': {'model': model},
            'content': '', 'thinking': '', 'content_lock': threading.Lock()}


def _working_messages() -> list[dict]:
    # Working rows carry engine fields the provider body never sees.
    return [
        {'role': 'system', 'content': 'Follow the scoped project policy.'},
        {'role': 'user', 'content': _code(600, 'history'),
         '_contextBlockId': 'carrier-1'},
    ]


def _local(messages, model) -> int:
    from lib.token_counter import count_tokens
    return count_tokens(messages, model=model, mode='tiktoken')['tokens']


@pytest.fixture
def harness(monkeypatch):
    from lib.acceptance import turns
    from lib.tasks_pkg.compaction import _prompt_admission as admission
    from lib.tasks_pkg.llm_fallback import _usage as usage
    from lib.token_counter import invalidate

    conv = 'serving-units-' + uuid4().hex
    monkeypatch.setattr(turns, 'read_execution_requirements',
                        lambda *a, **k: None)
    monkeypatch.setattr(usage, 'append_event', lambda *a, **k: None)
    monkeypatch.setattr(admission, 'recompose_context_after_compaction',
                        lambda *a, **k: None)
    compactions: list[dict] = []

    def compact(messages, *, task, **kwargs):
        compactions.append(kwargs)
        del messages[1:-1]
        return True

    monkeypatch.setattr(admission, 'force_compact_if_needed', compact)
    state = {'conv': conv, 'compactions': compactions, 'ceiling': None}
    monkeypatch.setattr(
        admission, '_resolved_ceiling',
        lambda *a, **k: (state['ceiling'], state['ceiling']))
    yield state
    invalidate(conv)


def _admit(task, messages, *, round_num=0):
    from lib.tasks_pkg.compaction._prompt_admission import (
        enforce_dispatch_prompt_limit,
    )
    return enforce_dispatch_prompt_limit(
        messages, [], task, round_num=round_num,
        model=task['model'])


def _settle_provider_usage(task, messages, admission, *, model, prompt_tokens,
                           round_num=1):
    """Settle one completed stream of the body built from ``admission``.

    The body carries the admission's measurement when the admission produced
    one, as ``build_round_request`` attaches it; the two hooks are those every
    completed stream runs.
    """
    from lib.tasks_pkg.llm_fallback import _usage as usage
    from lib.tasks_pkg.manager._stream import _record_stream_prompt_usage
    from lib.token_counter.evidence import ADMITTED_INPUT_TOKENS_KEY

    body = {'model': model, 'messages': [
        {key: value for key, value in message.items()
         if not key.startswith('_')} for message in messages],
        ADMITTED_INPUT_TOKENS_KEY: admission['totalTokens']}
    measured = admission.get('_servingMeasurement')
    if measured is not None:
        from lib.token_counter.evidence import ADMITTED_SERVING_UNITS_KEY
        body[ADMITTED_SERVING_UNITS_KEY] = measured
    provider_usage = {'prompt_tokens': prompt_tokens, 'completion_tokens': 10,
                      '_dispatch': {'logical_model': model}}
    _record_stream_prompt_usage(task, body, provider_usage,
                                {'role': 'assistant', 'content': 'done'},
                                model=model, prefix='[test]')
    usage._emit_round_usage(task, round_num, model, provider_usage)


def test_provider_counts_above_the_ceiling_while_tofu_counts_below_compacts_first(harness):
    """The next turn's first request is measured from the provider's count."""
    first_turn = _task(harness['conv'], CLAUDE)
    messages = _working_messages()
    harness['ceiling'] = 10_000_000
    admitted = _admit(first_turn, messages)
    local = _local(messages, CLAUDE)
    provider = int(local * 1.7)
    _settle_provider_usage(first_turn, messages, admitted, model=CLAUDE,
                           prompt_tokens=provider)

    next_turn = _task(harness['conv'], CLAUDE)
    messages.append({'role': 'user', 'content': 'Continue the scoped work.'})
    local_now = _local(messages, CLAUDE)
    # Tofu's own count is far below the ceiling; Claude's is above it.
    harness['ceiling'] = int(local_now * 1.5)
    assert local_now < harness['ceiling'] < provider

    measurement = _admit(next_turn, messages)

    assert len(harness['compactions']) == 1
    assert harness['compactions'][0]['_compaction_trigger'] == 'dispatch_guard'
    assert next_turn['_lastPromptAdmission']['action'] == 'compact_then_admit'
    before = next_turn['_promptAdmissionHistory'][0]
    assert before['totalTokens'] >= provider
    assert before['tokenUnits']['source'] == 'provider_count'
    assert measurement['totalTokens'] <= harness['ceiling']


def test_previous_round_provider_count_is_the_measurement_when_present(harness):
    from lib.model_info.token_units import token_unit_fact

    task = _task(harness['conv'], CLAUDE)
    messages = _working_messages()
    harness['ceiling'] = 10_000_000
    first = _admit(task, messages)
    provider = int(_local(messages, CLAUDE) * 1.31)
    _settle_provider_usage(task, messages, first, model=CLAUDE,
                           prompt_tokens=provider)

    appended = {'role': 'user', 'content': _code(60, 'appended')}
    messages.append(appended)
    second = _admit(task, messages, round_num=1)

    fact = token_unit_fact(CLAUDE)
    delta = _local([appended], CLAUDE) - _local([], CLAUDE)
    assert provider + delta <= second['totalTokens'] <= (
        provider + math.ceil(fact.maximum * delta) + 1)
    assert second['tokenUnits']['source'] == 'provider_count'
    assert second['tokenUnits']['model'] == CLAUDE
    assert second['tokenUnits']['providerTokens'] == provider
    # The cold bound would have been far larger than the anchored count.
    assert second['totalTokens'] < first['tokenUnits']['coldBoundTokens'] + delta


def test_cold_request_is_measured_at_the_measured_bound(harness):
    """Without a provider count, the family's measured maximum bounds it."""
    from lib.model_info.token_units import token_unit_fact

    task = _task(harness['conv'], CLAUDE)
    messages = _working_messages()
    messages.append({'role': 'user', 'content': 'Continue the scoped work.'})
    local = _local(messages, CLAUDE)
    fact = token_unit_fact(CLAUDE)
    # Tofu's count fits; the measured bound of Claude's count does not.
    harness['ceiling'] = int(local * 1.5)

    _admit(task, messages)

    before = task['_promptAdmissionHistory'][0]
    assert before['totalTokens'] == math.ceil(fact.maximum * local)
    assert before['tokenUnits'] == {
        'model': CLAUDE, 'source': 'measured_bound', 'localTokens': local,
        'coldBoundTokens': math.ceil(fact.maximum * local),
        'family': 'claude', 'bounded': True}
    assert len(harness['compactions']) == 1


def test_switched_serving_model_is_measured_in_its_own_units(harness):
    """After claude -> kimi, kimi requests are not counted in Claude's units."""
    task = _task(harness['conv'], CLAUDE)
    messages = _working_messages()
    harness['ceiling'] = 10_000_000
    first = _admit(task, messages)
    _settle_provider_usage(task, messages, first, model=CLAUDE,
                           prompt_tokens=int(_local(messages, CLAUDE) * 1.7))
    messages.append({'role': 'assistant', 'content': 'Reading the next file.'})
    messages.append({'role': 'user', 'content': _code(20, 'round_one')})
    second = _admit(task, messages, round_num=1)
    local = _local(messages, CLAUDE)
    # Claude refused this round; the configured fallback served it on kimi
    # and the loop continues on kimi.
    _settle_provider_usage(task, messages, second, model=CLAUDE,
                           prompt_tokens=int(local * 1.7), round_num=2)
    _settle_provider_usage(task, messages, second, model=KIMI,
                           prompt_tokens=int(local * 1.03), round_num=2)
    task['model'] = KIMI

    messages.append({'role': 'user', 'content': 'Continue on the fallback.'})
    local_now = _local(messages, KIMI)
    harness['ceiling'] = int(local_now * 1.25)

    measurement = _admit(task, messages, round_num=2)

    assert harness['compactions'] == []
    assert task['_lastPromptAdmission']['action'] == 'admit'
    assert measurement['tokenUnits']['model'] == KIMI
    assert measurement['tokenUnits']['source'] == 'provider_count'
    assert measurement['totalTokens'] < int(local * 1.1)


def test_force_compact_gate_counts_in_serving_units(monkeypatch):
    """The pipeline gate compares the serving-unit count with the window."""
    from lib.model_info.token_units import token_unit_fact
    from lib.tasks_pkg.compaction import _tokens

    task = _task('serving-gate-' + uuid4().hex, CLAUDE)
    messages = _working_messages()
    local = _local(messages, CLAUDE)
    fact = token_unit_fact(CLAUDE)
    # A small window: usable context is 70% of it and the trigger 90% of that.
    window = math.ceil(local * 1.2 / 0.63)
    monkeypatch.setattr(_tokens, '_get_context_limit', lambda task=None: window)
    threshold = _tokens._compaction_trigger_threshold(task, context_limit=window)[0]
    # Tofu's count is under the trigger; Claude's measured bound is over it.
    assert local < threshold < math.ceil(fact.maximum * local)

    measurement: dict = {}
    assert _tokens._should_force_compact(messages, task, measurement_out=measurement)
    assert measurement['gate_tokens'] == math.ceil(fact.maximum * local)


def test_next_turn_admits_on_the_provider_count_when_the_cold_bound_is_over(harness):
    """A provider count clears a cold over-count across turns.

    Conv mvf9laurcvekgh: the gate demanded a summary at 908,877 tokens while
    the provider had billed 509,044 for the same conversation, because every
    new turn started uncalibrated. The owner's provider count binds the next
    turn's first request.
    """
    first_turn = _task(harness['conv'], CLAUDE)
    messages = _working_messages()
    harness['ceiling'] = 10_000_000
    admitted = _admit(first_turn, messages)
    provider = int(_local(messages, CLAUDE) * 1.3)
    _settle_provider_usage(first_turn, messages, admitted, model=CLAUDE,
                           prompt_tokens=provider)

    next_turn = _task(harness['conv'], CLAUDE)
    messages.append({'role': 'user', 'content': 'Continue the scoped work.'})
    cold = admitted['tokenUnits']['coldBoundTokens']
    # The cold bound is over the ceiling; the provider-anchored count is not.
    harness['ceiling'] = (provider + cold) // 2

    measurement = _admit(next_turn, messages)

    assert harness['compactions'] == []
    assert next_turn['_lastPromptAdmission']['action'] == 'admit'
    assert measurement['tokenUnits']['source'] == 'provider_count'
    assert measurement['totalTokens'] < harness['ceiling'] < cold


@pytest.mark.parametrize('provider_usage', [
    {'input_tokens': 1809, 'cache_read_input_tokens': 73472,
     'cache_creation_input_tokens': 0, 'output_tokens': 10},
    {'prompt_tokens': 75281, 'prompt_tokens_details': {'cached_tokens': 73472},
     'completion_tokens': 10},
], ids=['anthropic-uncached-residual', 'openai-inclusive'])
def test_stream_settlement_keeps_the_full_prompt(harness, monkeypatch, provider_usage):
    """Anthropic usage reports cache reads outside ``input_tokens``.

    The kept count must be the full prompt (1,809 + 73,472 = 75,281), else
    every warm Anthropic-convention round is measured at its ~2K residual.
    """
    import lib.tasks_pkg.manager._stream as strm
    from lib.token_counter.evidence import ADMITTED_SERVING_UNITS_KEY
    from lib.token_counter.usage_cache import provider_count

    monkeypatch.setattr(strm, 'dispatch_stream', lambda body, **kw: (
        {'content': 'ok', 'reasoning_content': '', 'tool_calls': []}, 'stop',
        dict(provider_usage)))
    monkeypatch.setattr(strm, 'make_provider_abort_check', lambda _task: None)
    monkeypatch.setattr(strm, 'append_event', lambda *a, **kw: None)
    monkeypatch.setattr(strm, 'checkpoint_task_partial', lambda *a, **kw: None)
    task = {**_task(harness['conv'], KIMI), 'aborted': False}
    messages = [{'role': 'user', 'content': 'hi'}]
    harness['ceiling'] = 10_000_000
    admitted = _admit(task, messages)

    strm.stream_llm_response(task, {
        'model': KIMI, 'messages': messages,
        ADMITTED_SERVING_UNITS_KEY: admitted['_servingMeasurement']}, tag='R1')

    assert provider_count(harness['conv'], KIMI).provider_tokens == 75_281


# Conversation worker-a0f217ebc2af3d51e6a47e4e, 2026-11-13: R1436 ran on
# kimi-k3.1-pro before the 23:21 restart. The resumed attempt 0cc61a84 admitted
# R1437 for claude-opus-5.5 at Tofu's tiktoken count of 650,002 and Claude
# refused it at 23:29:44: "prompt is too long: 1016533 tokens > 1000000 maximum".
TONIGHT_LOCAL_TOKENS = 650_002
TONIGHT_CLAUDE_TOKENS = 1_016_533
CLAUDE_WINDOW = 1_000_000
_HISTORY_MARKER = 'history_0 = compute('


@pytest.mark.parametrize('last_provider_count', ['none-after-restart', KIMI])
def test_resumed_request_switched_to_claude_compacts_before_sending(
        harness, monkeypatch, last_provider_count):
    """Tonight's R1437: Tofu's count fits Claude's ceiling, Claude's count does not.

    No Claude provider count exists: the restart dropped every count, and the
    count R1436 left is kimi-k3.1-pro's, which never measures a Claude request.
    """
    import lib.token_counter as token_counter
    from lib.tasks_pkg.compaction._tokens import _compaction_trigger_threshold

    real_count = token_counter.count_tokens

    def tofu_count(messages, *args, **kwargs):
        counted = real_count(messages, *args, **kwargs)
        if any(_HISTORY_MARKER in str(message.get('content', ''))
               for message in messages):
            # Tofu's count of the uncompacted request.
            counted = {**counted, 'tokens': TONIGHT_LOCAL_TOKENS}
        return counted

    monkeypatch.setattr(token_counter, 'count_tokens', tofu_count)
    messages = _working_messages()
    if last_provider_count == KIMI:
        r1436 = _task(harness['conv'], KIMI)
        harness['ceiling'] = 10_000_000
        admitted = _admit(r1436, messages)
        _settle_provider_usage(r1436, messages, admitted, model=KIMI,
                               prompt_tokens=674_702)
    resumed = _task(harness['conv'], CLAUDE)
    messages.append({'role': 'user', 'content': 'Continue the assigned audit.'})
    harness['ceiling'] = _compaction_trigger_threshold(
        resumed, context_limit=CLAUDE_WINDOW)[0]
    assert TONIGHT_LOCAL_TOKENS < harness['ceiling'] < CLAUDE_WINDOW < TONIGHT_CLAUDE_TOKENS

    measurement = _admit(resumed, messages)

    assert len(harness['compactions']) == 1
    assert harness['compactions'][0]['_compaction_trigger'] == 'dispatch_guard'
    assert resumed['_lastPromptAdmission']['action'] == 'compact_then_admit'
    before = resumed['_promptAdmissionHistory'][0]
    # Claude's own count of the request is inside what admission charged.
    assert before['totalTokens'] >= TONIGHT_CLAUDE_TOKENS
    assert before['tokenUnits']['model'] == CLAUDE
    assert before['tokenUnits']['source'] == 'measured_bound'
    assert measurement['totalTokens'] <= harness['ceiling']
