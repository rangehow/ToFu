"""Failed native Messages work remains portable without inventing signed state."""
from copy import deepcopy
import json

import pytest

from lib.llm.anthropic_outbound import openai_body_to_anthropic_checked
from lib.llm.responses_outbound import openai_body_to_responses_checked

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('model', ['gpt-6.1-sol', 'opaque-destination'])
@pytest.mark.parametrize('continuity', [None, 'active_turn', 'completed_turn'])
def test_failed_native_tool_prefix_crosses_to_responses_without_state_forgery(model, continuity):
    # Synthetic shape of retained failed Worker rounds: opaque thinking, native
    # tool_use plus canonical call, exact tool result; not a new owner message.
    messages = [{'role': 'system', 'content': 'Owner constraints remain binding'},
                {'role': 'user', 'content': 'Finish the authorized audit'}]
    for index in range(3):
        calls = [{'id': f'call-{index}-{sibling}', 'type': 'function', 'function': {
            'name': 'read_file' if sibling == 0 else 'inspect_file',
            'arguments': json.dumps({'path': f'source-{index}-{sibling}.py'})}}
            for sibling in range(2)]
        assistant = {'role': 'assistant', 'content': f'Checking {index}',
                     'reasoning_content': f'Reasoning {index}\n  exact',
                     'thinking_signature': f'opaque-{index}',
                     '_anthropic_content_blocks': [
                         {'type': 'thinking', 'thinking': f'\nReasoning {index}\n  exact\t ',
                          'signature': f'opaque-{index}'},
                         {'type': 'redacted_thinking', 'data': f'opaque-redacted-{index}'},
                         {'type': 'text', 'text': f'Checking {index}'},
                         *[{'type': 'tool_use', 'id': call['id'],
                            'name': call['function']['name'],
                            'input': json.loads(call['function']['arguments'])}
                           for call in calls]],
                     'tool_calls': calls}
        if continuity is not None:
            assistant['_reasoning_continuity'] = continuity
        messages.append(assistant)
        messages.extend({'role': 'tool', 'tool_call_id': calls[sibling]['id'],
                         'content': f'Exact result {index}-{sibling}\n  unchanged'}
                        for sibling in (1, 0))
    original = deepcopy(messages)
    result = openai_body_to_responses_checked({'model': model, 'messages': messages}, profile='codex')
    wire = result.require_payload()
    expected_losses = {}
    for row in original:
        if row['role'] != 'assistant':
            continue
        # Each public reasoning projection, flat signature and native private block
        # is an independently reported omission; completion changes only classification.
        if row.get('reasoning_content'):
            expected_losses['optional_reasoning_omitted'] = (
                expected_losses.get('optional_reasoning_omitted', 0) + 1)
        completed = row.get('_reasoning_continuity') == 'completed_turn'
        if row.get('thinking_signature'):
            category = ('historical_thinking_signature_omitted' if completed
                        else 'cross_protocol_reasoning_omitted')
            expected_losses[category] = expected_losses.get(category, 0) + 1
        for block in row.get('_anthropic_content_blocks', []):
            if block['type'] in {'thinking', 'redacted_thinking'}:
                category = ('historical_thinking_block_omitted' if completed
                            else 'cross_protocol_reasoning_omitted')
                expected_losses[category] = expected_losses.get(category, 0) + 1
    assert result.permitted_conversion_losses == expected_losses
    assert messages == original
    expected = [
        {'type': 'message', 'role': 'developer', 'content': [
            {'type': 'input_text', 'text': 'Owner constraints remain binding'}]},
        {'type': 'message', 'role': 'user', 'content': [
            {'type': 'input_text', 'text': 'Finish the authorized audit'}]},
    ]
    for index in range(3):
        expected.append({'type': 'message', 'role': 'assistant', 'content': [
            {'type': 'output_text', 'text': f'Checking {index}'}]})
        expected.extend({'type': 'function_call', 'call_id': f'call-{index}-{sibling}',
                         'name': 'read_file' if sibling == 0 else 'inspect_file',
                         'arguments': json.dumps({'path': f'source-{index}-{sibling}.py'})}
                        for sibling in range(2))
        expected.extend({'type': 'function_call_output', 'call_id': f'call-{index}-{sibling}',
                         'output': f'Exact result {index}-{sibling}\n  unchanged'}
                        for sibling in (1, 0))
    assert wire['input'] == expected
    assert 'opaque-' not in repr(wire['input'])
    assert '_reasoning_continuity' not in repr(wire)
    # Same protocol still receives the exact original signatures/redactions.
    native = openai_body_to_anthropic_checked({'messages': messages}).require_payload()
    assert native.get('system') == 'Owner constraints remain binding'
    expected_native = [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'Finish the authorized audit'}]}]
    for index in range(3):
        expected_native.append({'role': 'assistant', 'content':
            original[2 + index * 3]['_anthropic_content_blocks']})
        expected_native.append({'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': f'call-{index}-{sibling}',
             'content': f'Exact result {index}-{sibling}\n  unchanged'}
            for sibling in (1, 0)]})
    assert native['messages'] == expected_native
    assert messages == original


@pytest.mark.parametrize('block', [
    {'type': 'server_tool_use', 'id': 'server-call', 'name': 'web_search', 'input': {}},
    {'type': 'future_execution', 'data': 'mandatory-work'},
])
def test_private_thinking_does_not_make_native_execution_optional(block):
    message = {'role': 'assistant', 'content': 'Visible work', 'thinking_signature': 'opaque',
               'reasoning_content': '',
               '_anthropic_content_blocks': [
                   {'type': 'thinking', 'thinking': '', 'signature': 'opaque'}, block]}
    result = openai_body_to_responses_checked({'messages': [message]}, profile='codex')
    assert result.validation_issues == ('mandatory_continuity_unrepresentable',)


@pytest.mark.parametrize('corruption', [
    'roles', 'call_links', 'native_block_order', 'native_drop_system_users_results',
    'native_drop_result', 'native_wrong_link', 'native_drop_constraint', 'native_clear_thinking',
])
def test_portability_oracle_rejects_corrupted_payload(monkeypatch, corruption):
    from dataclasses import replace
    import sys

    module = sys.modules[__name__]
    if corruption.startswith('native_'):
        convert = openai_body_to_anthropic_checked

        def corrupted(body):
            result = convert(body)
            payload = deepcopy(result.require_payload())
            if corruption in {'native_drop_system_users_results', 'native_drop_constraint'}:
                payload.pop('system')
            if corruption == 'native_drop_system_users_results':
                payload['messages'] = [row for row in payload['messages']
                                       if row['role'] == 'assistant']
            for row in payload['messages']:
                if row['role'] == 'assistant':
                    if corruption == 'native_block_order':
                        row['content'].reverse()
                    if corruption == 'native_clear_thinking':
                        for block in row['content']:
                            if block['type'] == 'thinking':
                                block['thinking'] = ''
                if row['role'] == 'user':
                    if corruption == 'native_drop_result':
                        row['content'] = [block for block in row['content']
                                          if block['type'] != 'tool_result']
                    if corruption == 'native_wrong_link':
                        for block in row['content']:
                            if block['type'] == 'tool_result':
                                block['tool_use_id'] = 'wrong-id'
            return replace(result, payload=payload)

        monkeypatch.setattr(module, 'openai_body_to_anthropic_checked', corrupted)
    else:
        convert = openai_body_to_responses_checked

        def corrupted(body, **kwargs):
            result = convert(body, **kwargs)
            payload = deepcopy(result.require_payload())
            for item in payload['input']:
                if corruption == 'roles' and item['type'] == 'message':
                    item['role'] = 'assistant'
                if corruption == 'call_links' and item['type'] == 'function_call_output':
                    item['call_id'] = 'wrong-id'
            return replace(result, payload=payload)

        monkeypatch.setattr(module, 'openai_body_to_responses_checked', corrupted)
    with pytest.raises(AssertionError):
        test_failed_native_tool_prefix_crosses_to_responses_without_state_forgery(
            'opaque-destination', 'active_turn')


@pytest.mark.parametrize('continuity', [None, 'active_turn', 'completed_turn'])
@pytest.mark.parametrize('corruption', ['empty', 'classification', 'count'])
def test_portability_oracle_rejects_changed_loss_report(monkeypatch, continuity, corruption):
    from dataclasses import replace
    import sys

    module = sys.modules[__name__]
    convert = openai_body_to_responses_checked

    def corrupted(body, **kwargs):
        result = convert(body, **kwargs)
        report = dict(result.permitted_conversion_losses)
        if corruption == 'empty':
            report.clear()
        elif corruption == 'count':
            report['optional_reasoning_omitted'] += 1
        elif continuity == 'completed_turn':
            report['cross_protocol_reasoning_omitted'] = report.pop(
                'historical_thinking_signature_omitted') + report.pop(
                'historical_thinking_block_omitted')
        else:
            report['historical_thinking_block_omitted'] = report.pop(
                'cross_protocol_reasoning_omitted')
        return replace(result, permitted_conversion_losses=report)

    monkeypatch.setattr(module, 'openai_body_to_responses_checked', corrupted)
    with pytest.raises(AssertionError):
        test_failed_native_tool_prefix_crosses_to_responses_without_state_forgery(
            'opaque-destination', continuity)
