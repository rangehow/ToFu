"""Count function-row preparation on matched synthetic tool-return requests."""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

from lib.llm._sse_core import prepare_request
from lib.llm.responses_outbound import _to_responses as outbound

pytestmark = pytest.mark.unit


def _tools():
    return [{'type': 'function', 'function': {
        'name': f'synthetic_read_{index}', 'description': f'Read synthetic {index}.',
        'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}},
        **({'strict': True} if index == 1 else {})}}
        for index in range(12)]


def _wire(body):
    plan = prepare_request(body, api_protocol='responses',
                           api_key='synthetic-only', base_url='http://127.0.0.1:9/v1')
    return json.dumps(plan.body.wire_body(), ensure_ascii=False, separators=(',', ':'))


def _body(tools, messages, *, rows=None, **config):
    body = {'model': 'synthetic-responses', 'messages': deepcopy(messages),
            'max_tokens': 512, 'tools': tools,
            '_executable_tool_catalog': tools, '_tool_wire_catalog': tools,
            '_tool_search_mode': 'off', '_programmatic_tool_calling': 'off',
            '_multi_agent_mode': 'off', **config}
    if rows is not None:
        body['_wire_tool_rows'] = rows
    return body


def _returned(messages, index):
    messages.extend([
        {'role': 'assistant', 'content': '', 'tool_calls': [{
            'id': f'call_{index}', 'type': 'function', 'function': {
                'name': 'synthetic_read_0', 'arguments': json.dumps({'path': f'note-{index}'})}}]},
        {'role': 'tool', 'tool_call_id': f'call_{index}', 'content': f'synthetic result {index}'},
    ])


def test_normal_seven_round_trace_flattens_each_stable_function_once():
    tools, rows = _tools(), {}
    messages = [{'role': 'system', 'content': 'Synthetic rules A.'},
                {'role': 'user', 'content': 'Read synthetic notes.'}]
    converted = []
    original = outbound._convert_tools

    def observe(*args):
        result = original(*args)
        converted.extend(result)
        return result

    for index in range(7):
        expected = _wire(_body(deepcopy(tools), messages))
        with patch.object(outbound, '_convert_tools', side_effect=observe):
            actual = _wire(_body(tools, messages, rows=rows))
        assert actual == expected
        _returned(messages, index)
    unique = len({id(row) for row in converted})
    assert unique == 12, f'{unique} function rows constructed for 84 appearances'
    print(f'normal trace: 84 appearances, {unique} constructed rows, exact wire parity')


@pytest.mark.parametrize('change', [
    'rules', 'output', 'profile', 'schema', 'description', 'strict',
    'visibility', 'permission', 'reorder', 'tool_choice', 'search', 'mutable',
])
def test_changed_inputs_match_fresh_preparation(change):
    tools, rows = _tools(), {}
    messages = [{'role': 'system', 'content': 'Synthetic rules A.'},
                {'role': 'user', 'content': 'Read synthetic notes.'}]
    prepared = []
    original = outbound._convert_tools

    def observe(*args):
        result = original(*args)
        prepared.extend(result)
        return result

    config = ({'model': 'gpt-5.6', '_responses_feature_profile': 'compatible'}
              if change == 'profile' else {})
    with patch.object(outbound, '_convert_tools', side_effect=observe):
        _wire(_body(tools, messages, rows=rows, **config))
    _returned(messages, 0)
    before_profile = (_wire(_body(deepcopy(tools), messages, **config))
                      if change == 'profile' else None)
    if change == 'rules':
        messages[0]['content'] = 'Synthetic rules B.'
    elif change == 'output':
        config['max_tokens'] = 123
    elif change == 'profile':
        config['_responses_feature_profile'] = 'openai'
    elif change == 'schema':
        tools = deepcopy(tools)
        tools[0]['function']['parameters']['properties']['path']['type'] = 'integer'
    elif change == 'description':
        tools = deepcopy(tools)
        tools[0]['function']['description'] = 'Changed synthetic contract.'
    elif change == 'strict':
        tools = deepcopy(tools)
        tools[0]['function']['strict'] = True
    elif change == 'visibility':
        config['_tool_wire_catalog'] = tools[:2]
    elif change == 'permission':
        tools = tools[:2]
    elif change == 'reorder':
        tools = tools[::-1]
    elif change == 'tool_choice':
        config['tool_choice'] = {'type': 'function', 'function': {'name': 'synthetic_read_1'}}
    elif change == 'search':
        config['_tool_search_mode'] = 'local'
    elif change == 'mutable':
        tools[0]['function']['description'] = 'In-place mutable source change.'
    with patch.object(outbound, '_convert_tools', side_effect=observe):
        actual = _wire(_body(tools, messages, rows=rows, **config))
    expected = _wire(_body(deepcopy(tools), messages, **deepcopy(config)))
    assert actual == expected
    sent = json.loads(actual)['tools']
    original_names = {tool['function']['name'] for tool in _tools()}
    unique = len({id(row) for row in prepared if row['name'] in original_names})
    expected_unique = (24 if change == 'reorder' else
                       13 if change in ('schema', 'description', 'strict', 'mutable') else 12)
    assert unique == expected_unique
    print(f'{change}: {unique} constructed original-function rows, exact wire parity')
    if change in ('visibility', 'permission'):
        assert [row['name'] for row in sent] == ['synthetic_read_0', 'synthetic_read_1']
    elif change == 'rules':
        assert 'Synthetic rules B.' in actual and 'Synthetic rules A.' not in actual
    elif change == 'profile':
        before = json.loads(before_profile)
        after = json.loads(actual)
        assert 'include' not in before
        assert after['include'] == ['reasoning.encrypted_content']
        assert before_profile != actual


def test_mutable_functions_are_not_shared_and_long_aliases_remain_request_local():
    from lib.conversation_sync.settled_rows import settle
    from lib.llm.conversion import ProtocolConversionError
    tools = _tools()
    assert outbound._convert_tools(tools, {})[0] is not outbound._convert_tools(tools, {})[0]
    tools[0]['function']['name'] = 'synthetic_' * 10
    settled = settle(tools)
    reverse = {}
    first = outbound._convert_tools(settled, reverse)
    fresh_reverse = {}
    second = outbound._convert_tools(settled, fresh_reverse)
    assert reverse == fresh_reverse and reverse
    assert first == second == outbound._convert_tools(tools, {})
    alias = first[0]['name']
    assert len(alias) <= 64
    with pytest.raises(ProtocolConversionError):
        outbound._convert_tools(settled, {alias: 'other-original'})


def test_reused_rows_allow_copy_on_write_programmatic_projection_and_dialect_changes():
    from lib.conversation_sync.settled_rows import SettledRowMutation, settle
    tools = settle(_tools())
    plain = deepcopy(tools)
    first = outbound._convert_tools(tools, {})
    with pytest.raises(SettledRowMutation):
        first[0]['description'] = 'changed'
    projected = outbound._enable_programmatic_tools(first, eligible={'synthetic_read_0'})
    assert projected[0]['allowed_callers'] == ['direct', 'programmatic']
    assert 'allowed_callers' not in outbound._convert_tools(tools, {})[0]
    for profile in ('default', 'codex', 'default'):
        body = {'model': 'synthetic-responses', 'tools': tools, 'max_tokens': 123,
                'messages': [{'role': 'user', 'content': 'Synthetic dialect transition.'}]}
        fresh = dict(body, tools=plain)
        actual, reverse = outbound.openai_body_to_responses(body, profile=profile)
        expected, fresh_reverse = outbound.openai_body_to_responses(fresh, profile=profile)
        assert json.dumps(actual) == json.dumps(expected)
        assert reverse == fresh_reverse
    assert outbound._convert_tools(tools, {})[0] is first[0]


def test_changed_request_alias_is_not_taken_from_a_warm_row():
    from lib.conversation_sync.settled_rows import settle
    tools = _tools()[:1]
    name = tools[0]['function']['name'] = 'synthetic_' * 10
    settled = settle(tools)
    first = outbound._convert_tools(settled, {})
    alternate = {'request_alias': name}
    changed = outbound._convert_tools(settled, alternate)
    assert changed[0]['name'] == 'request_alias'
    assert changed == outbound._convert_tools(tools, dict(alternate))
    assert outbound._convert_tools(settled, {}) == first
