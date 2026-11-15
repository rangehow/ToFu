"""Regression tests for the compaction token gate (_count_tokens_authoritative).

Two properties, both born from the conv=mq7y3irly1r4hu fatal-loop:

  1. The gate accounts for the live tool schema stashed on
     ``task['_tool_schema']`` — the schema ships in every request and the
     gateway tokenizes it, so omitting it under-counts on tool-heavy configs.

  2. The gate counts in the serving model's units, never Tofu's local units:
     tiktoken's cl100k vocabulary under-counts Claude's tokenizer (high-entropy
     base64 included), and trusting the lower number lets an oversized prompt
     slip past the trigger into the fatal reactive path. The entropy heuristic
     is only the fallback when the local tokenizer fails; it never inflates a
     tokenizer count.
"""
from __future__ import annotations

import pytest

from tests.compaction.test_compaction_history_wire import compact as compact

from lib.tasks_pkg.compaction._tokens import (
    _compaction_trigger_threshold,
    _count_tokens_authoritative,
)

pytestmark = pytest.mark.unit


@pytest.mark.usefixtures('compact')
@pytest.mark.parametrize('mode', ['manual', 'window', 'economic'])
def test_only_economic_compaction_requires_the_foldable_region_estimate(monkeypatch, mode):
    from copy import deepcopy
    from lib.tasks_pkg.compaction._layer2 import _compact as owner

    cold = 'economic-estimate-only-cold-evidence'
    messages = [
        {'role': 'system', 'content': 'Preserve scoped policy.'},
        {'role': 'user', 'content': 'Complete the scoped work.'},
        {'role': 'assistant', 'content': (cold + ' ') * 1000},
        {'role': 'user', 'content': 'Continue the remaining work.'},
    ]
    original = deepcopy(messages)
    estimate = owner._estimate_total_tokens

    class EconomicEstimateRequested(RuntimeError):
        pass

    def count(rows):
        if (any(cold in str(row.get('content')) for row in rows)
                and not any(row.get('role') == 'system' for row in rows)):
            raise EconomicEstimateRequested('foldable-only economic estimate')
        return estimate(rows)

    monkeypatch.setattr(owner, '_estimate_total_tokens', count)
    meta = {}
    options = dict(
        preserve_budget_tokens=1, _result_meta=meta,
        _proactive_economic=mode != 'manual',
        _message_tokens_before=2_000_000 if mode == 'window' else 50_000,
        _compaction_trigger='window' if mode == 'window' else 'manual',
        _dispatch_prompt_measure=lambda candidate: 1,
        _dispatch_prompt_target=1_000_000,
    )
    task = {'id': 'economic-estimate-guard', '_userId': 41, 'config': {}}
    if mode == 'economic':
        with pytest.raises(EconomicEstimateRequested):
            owner.execute_compact_tool(messages, task, **options)
        assert messages == original and meta['compacted'] is False
    else:
        owner.execute_compact_tool(messages, task, **options)
        assert meta['compacted'] is True
        assert meta['projected_summary_usage_tokens'] == 0
        assert meta['economics'] is None


@pytest.mark.unit
def test_high_entropy_claude_request_is_counted_in_claude_units():
    # A transcript full of high-entropy base64: Tofu's tiktoken count is
    # far below what Claude's tokenizer reports for it.
    import base64
    import math
    import os

    from lib.model_info.token_units import token_unit_fact
    from lib.token_counter import count_tokens

    blob = base64.b64encode(os.urandom(60_000)).decode()
    msgs = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'analyze this file'},
        {'role': 'tool', 'name': 'read_files', 'tool_call_id': 't1',
         'content': f'File: data.b64\n\n{blob}'},
    ]
    model = 'aws.claude-opus-4.8'
    task = {'config': {'model': model}, 'convId': 'gate_floor'}

    gate, method = _count_tokens_authoritative(msgs, task)

    local = count_tokens(msgs, model=model, mode='tiktoken')['tokens']
    assert method == 'tiktoken+measured_bound'
    assert gate == math.ceil(token_unit_fact(model).maximum * local) > local


@pytest.mark.unit
def test_entropy_heuristic_never_inflates_a_tokenizer_count(monkeypatch):
    """A local tokenizer count is not raised by the message heuristic.

    Regression for conv=msn5s9y58cwopu: R11 measured 111,552 tokens, while the
    message heuristic estimated 164,562 and falsely crossed the 128K economic
    trigger. The heuristic is only the fallback when the tokenizer fails.
    """
    import lib.token_counter as token_counter
    import lib.tasks_pkg.compaction._tokens as token_mod

    messages = [{'role': 'user', 'content': 'x' * 500_000}]
    monkeypatch.setattr(
        token_counter, 'count_tokens',
        lambda *a, **k: {'tokens': 111_552, 'method': 'tiktoken'})
    monkeypatch.setattr(
        token_mod, '_estimate_total_tokens',
        lambda _m: pytest.fail('the gate scanned the message heuristic'))

    measurement = {}
    count, method = _count_tokens_authoritative(
        messages,
        # gpt-5.6-sol counts in o200k, which no measured fact describes.
        {'config': {'model': 'gpt-5.6-sol'}, 'convId': 'measured-wins'},
        measurement_out=measurement,
    )

    assert (count, method) == (111_552, 'tiktoken+local_unbounded')
    assert measurement['gate_tokens'] == measurement['local_tokens'] == 111_552
    assert measurement['message_tokens'] == 111_552
    assert measurement['token_units'] == {
        'model': 'gpt-5.6-sol', 'source': 'local_unbounded',
        'localTokens': 111_552, 'coldBoundTokens': 111_552,
        'family': 'gpt', 'bounded': False}


@pytest.mark.unit
def test_counter_failure_reuses_single_message_estimate(monkeypatch):
    """The fallback result and conservative gate floor share one scan."""
    import lib.token_counter as token_counter
    import lib.tasks_pkg.compaction._tokens as token_mod

    estimate_calls = []

    def estimate(messages):
        estimate_calls.append(messages)
        return 321

    monkeypatch.setattr(
        token_counter, 'count_tokens',
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('offline')))
    monkeypatch.setattr(token_mod, '_estimate_total_tokens', estimate)

    measurement = {}
    count, method = _count_tokens_authoritative(
        [{'role': 'user', 'content': 'hello'}],
        {'config': {'model': 'gpt-4o'}, 'convId': 'fallback-once'},
        measurement_out=measurement,
    )

    # A heuristic estimate is no tiktoken count: no measured fact applies.
    assert (count, method) == (321, 'heuristic_fallback+local_unbounded')
    assert len(estimate_calls) == 1
    assert measurement['message_tokens'] == 321


def test_general_gate_measurement_does_not_collect_identity_hints(monkeypatch):
    import lib.token_counter as token_counter

    observed_measurement_sinks = []

    def count(*args, **kwargs):
        observed_measurement_sinks.append(kwargs.get('measurement_out'))
        return {'tokens': 123, 'method': 'tiktoken'}

    monkeypatch.setattr(token_counter, 'count_tokens', count)
    measurement = {}

    _count_tokens_authoritative(
        [{'role': 'user', 'content': 'hello'}],
        {'config': {'model': 'gpt-5.6-sol'}},
        measurement_out=measurement,
    )

    assert observed_measurement_sinks == [None]
    assert measurement['gate_tokens'] >= 123


@pytest.mark.unit
def test_counter_failure_still_counts_the_selected_tool_surface(monkeypatch):
    """The declared full-request contract also holds on local degradation."""
    import lib.context_telemetry as context_telemetry
    import lib.token_counter as token_counter
    import lib.tasks_pkg.compaction._tokens as token_mod

    estimate_calls = []
    monkeypatch.setattr(
        token_counter, 'count_tokens',
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('offline')))
    monkeypatch.setattr(
        token_mod,
        '_estimate_total_tokens',
        lambda messages: estimate_calls.append(messages) or 321,
    )
    monkeypatch.setattr(
        context_telemetry, 'tool_schema_tokens',
        lambda tools, *, model='': 79,
    )

    count, method = _count_tokens_authoritative(
        [{'role': 'user', 'content': 'hello'}],
        {'config': {'model': 'gpt-4o'}, 'convId': 'fallback-tools'},
        tool_schema=[{'type': 'function'}],
    )

    assert (count, method) == (400, 'heuristic_fallback+local_unbounded')
    assert len(estimate_calls) == 1


@pytest.mark.unit
def test_gate_includes_tool_schema():
    # Identical messages; only difference is a fat tool schema stashed on
    # the task. The gate must count more tokens WITH the schema.
    msgs = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'hello'},
    ]
    fat_tools = [{
        'type': 'function',
        'function': {
            'name': f'tool_{i}',
            'description': 'A very thoroughly documented tool. ' * 40,
            'parameters': {
                'type': 'object',
                'properties': {f'p{j}': {'type': 'string',
                                         'description': 'param ' * 20}
                               for j in range(8)},
            },
        },
    } for i in range(30)]

    base = {'config': {'model': 'gpt-4o'}, 'convId': 'gate_tools_off'}
    with_tools = {'config': {'model': 'gpt-4o'}, 'convId': 'gate_tools_on',
                  '_tool_schema': fat_tools}

    n_off, _ = _count_tokens_authoritative(msgs, base)
    n_on, _ = _count_tokens_authoritative(msgs, with_tools)

    assert n_on > n_off, (n_on, n_off)


@pytest.mark.unit
def test_gate_no_tool_schema_key_is_safe():
    # Missing/empty _tool_schema must not raise and must still return a count.
    msgs = [{'role': 'user', 'content': 'hi there ' * 50}]
    task = {'config': {'model': 'gpt-4o'}, 'convId': 'gate_no_tools'}
    n, method = _count_tokens_authoritative(msgs, task)
    assert n > 0 and isinstance(method, str)


@pytest.mark.unit
@pytest.mark.parametrize('model', ['kimi-k3', 'gpt-5.6-sol'])
@pytest.mark.parametrize('legacy_target', [None, 0, 128_000, 300_000, 2_000_000])
def test_window_policy_does_not_accept_a_second_economic_target(
        monkeypatch, model, legacy_target):
    # Old presets/environment must not silently reactivate early summaries.
    monkeypatch.setenv('TOFU_WORKING_CONTEXT_TOKENS', '96000')
    effective, safety, economic = _compaction_trigger_threshold(
        {'config': {'model': model,
                    'compaction': {'workingSetTokens': legacy_target}}},
        context_limit=1_000_000)
    assert effective == safety
    assert safety > 300_000
    assert economic == 0


@pytest.mark.unit
@pytest.mark.parametrize(('prompt_tokens', 'expected'), [
    (150_000, False), (300_000, False), (900_000, True),
])
def test_waiting_uses_window_pressure_even_with_a_legacy_economic_override(
        monkeypatch, prompt_tokens, expected):
    from lib.tasks_pkg.compaction import _tokens as owner

    monkeypatch.setenv('TOFU_WORKING_CONTEXT_TOKENS', '128000')
    monkeypatch.setattr(owner, '_get_context_limit', lambda task: 1_000_000)
    monkeypatch.setattr(owner, '_count_tokens_authoritative',
                        lambda *a, **k: (prompt_tokens, 'provider_exact'))
    assert owner._should_force_compact(
        [{'role': 'user', 'content': 'Continue the authorized task.'}],
        {'id': 'timing-test', 'convId': 'timing-test',
         'config': {'compaction': {'workingSetTokens': 128_000}}}) is expected
