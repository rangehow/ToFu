"""Portable history conservation, not proof of summary acceptance or cancellation.

Lifecycle receipts are exercised by compaction/test_compaction_summary_evidence.py,
compaction/test_compaction_cancellation_boundaries.py and
tasks_pkg/test_tool_outcome_finality.py; history text cannot grant those states.
"""
from copy import deepcopy
import json

import pytest

from lib.llm.anthropic_outbound import openai_body_to_anthropic_checked
from lib.llm.conversion import ProtocolConversionError
from lib.llm.openai_outbound import openai_body_to_chat
from lib.llm.responses_outbound import openai_body_to_responses_checked

pytestmark = pytest.mark.unit


def _body(continuity=None):
    calls = [{'id': name, 'type': 'function', 'function': {
        'name': 'read_file', 'arguments': json.dumps({'path': name + '.txt'})}}
        for name in ('read-a', 'read-b')]
    native = [
        {'type': 'thinking', 'thinking': '', 'signature': 'synthetic-signature'},
        {'type': 'redacted_thinking', 'data': 'synthetic-redaction'},
        {'type': 'text', 'text': 'Read-only work in progress'},
        *[{'type': 'tool_use', 'id': call['id'], 'name': 'read_file',
           'input': json.loads(call['function']['arguments'])} for call in calls],
    ]
    assistant = {'role': 'assistant', 'content': 'Read-only work in progress',
                 'tool_calls': calls, '_anthropic_content_blocks': native}
    if continuity:
        assistant['_reasoning_continuity'] = continuity
    return {'model': 'synthetic-destination', 'parallel_tool_calls': False,
            'messages': [
                {'role': 'system', 'content': 'Never publish; no write authorization.'},
                {'role': 'user', 'content': [
                    {'type': 'text', 'text': 'Inspect only; stop at the deadline.'},
                    {'type': 'image_url', 'image_url': {
                        'url': 'data:image/png;base64,aW1hZ2U='}}]},
                {'role': 'user', 'content': 'Accepted summary: previous read succeeded; no permission to write.'},
                assistant,
                {'role': 'tool', 'tool_call_id': 'read-a', 'content': 'A\n  exact result'},
                {'role': 'tool', 'tool_call_id': 'read-b', 'content': 'B: denied; no effects'},
                {'role': 'user', 'content': 'Cancelled earlier attempt. Do not repeat any effects.'},
            ]}


def _convert(body, destination):
    if destination == 'responses':
        return openai_body_to_responses_checked(body, profile='codex').require_payload()
    if destination == 'messages':
        return openai_body_to_anthropic_checked(body).require_payload()
    return openai_body_to_chat(body)


@pytest.mark.parametrize('continuity', [None, 'active_turn', 'completed_turn'])
@pytest.mark.parametrize('destination', ['responses', 'messages', 'chat'])
def test_switch_preserves_constraints_media_summary_and_parallel_tool_pairs(destination, continuity):
    body = _body(continuity)
    if destination == 'chat' and continuity != 'completed_turn':
        # Only the existing signed gateway representation is contracted on Chat.
        body['messages'][3]['reasoning_details'] = deepcopy(
            body['messages'][3]['_anthropic_content_blocks'][:2])
    original = deepcopy(body)
    wire = _convert(body, destination)
    assert body == original
    encoded = json.dumps(wire)
    # These are portable history carriers, not receipts of summary acceptance or Stop.
    expected_history = [
        ('developer', ['Never publish; no write authorization.']),
        ('user', ['Inspect only; stop at the deadline.']),
        ('user', ['Accepted summary: previous read succeeded; no permission to write.']),
        ('assistant', ['Read-only work in progress']),
        ('user', ['Cancelled earlier attempt. Do not repeat any effects.']),
    ]
    if destination == 'responses':
        assert wire['input'] == [
            {'type': 'message', 'role': 'developer', 'content': [
                {'type': 'input_text', 'text': 'Never publish; no write authorization.'}]},
            {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': 'Inspect only; stop at the deadline.'},
                {'type': 'input_image', 'image_url': 'data:image/png;base64,aW1hZ2U='}]},
            {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': expected_history[2][1][0]}]},
            {'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': 'Read-only work in progress'}]},
            {'type': 'function_call', 'call_id': 'read-a', 'name': 'read_file',
             'arguments': '{"path": "read-a.txt"}'},
            {'type': 'function_call', 'call_id': 'read-b', 'name': 'read_file',
             'arguments': '{"path": "read-b.txt"}'},
            {'type': 'function_call_output', 'call_id': 'read-a', 'output': 'A\n  exact result'},
            {'type': 'function_call_output', 'call_id': 'read-b', 'output': 'B: denied; no effects'},
            {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': expected_history[-1][1][0]}]},
        ]
    elif destination == 'chat':
        expected_chat = deepcopy(original['messages'])
        expected_chat[3].pop('_anthropic_content_blocks')
        expected_chat[3].pop('_reasoning_continuity', None)
        assert wire['messages'] == expected_chat
        assert wire['parallel_tool_calls'] is False
    else:
        assert wire.get('system') == 'Never publish; no write authorization.'
        assert [row['role'] for row in wire['messages']] == [
            'user', 'user', 'assistant', 'user', 'user']
        assert wire['messages'][0] == {'role': 'user', 'content': [
            {'type': 'text', 'text': 'Inspect only; stop at the deadline.'},
            {'type': 'image', 'source': {
                'type': 'base64', 'media_type': 'image/png', 'data': 'aW1hZ2U='}},
        ]}
        assert wire['messages'][1]['content'] == [
            {'type': 'text', 'text': expected_history[2][1][0]}]
        assert wire['messages'][3]['content'] == [
            {'type': 'tool_result', 'tool_use_id': 'read-a', 'content': 'A\n  exact result'},
            {'type': 'tool_result', 'tool_use_id': 'read-b', 'content': 'B: denied; no effects'},
        ]
        assert wire['messages'][4]['content'] == [
            {'type': 'text', 'text': expected_history[-1][1][0]}]
    assert '_reasoning_continuity' not in encoded
    if destination != 'messages':
        assert 'data:image/png;base64,aW1hZ2U=' in encoded
    if destination == 'responses':
        calls = [item for item in wire['input'] if item['type'] == 'function_call']
        results = [item for item in wire['input'] if item['type'] == 'function_call_output']
        assert [(item['call_id'], item['name'], json.loads(item['arguments'])) for item in calls] == [
            ('read-a', 'read_file', {'path': 'read-a.txt'}),
            ('read-b', 'read_file', {'path': 'read-b.txt'})]
        assert [(item['call_id'], item['output']) for item in results] == [
            ('read-a', 'A\n  exact result'), ('read-b', 'B: denied; no effects')]
        assert max(wire['input'].index(item) for item in calls) < min(
            wire['input'].index(item) for item in results)
        assert wire['parallel_tool_calls'] is False
        assert 'synthetic-signature' not in encoded
        assert 'synthetic-redaction' not in encoded
    elif destination == 'messages':
        images = [block for row in wire['messages'] for block in row['content']
                  if block['type'] == 'image']
        assert images == [{'type': 'image', 'source': {
            'type': 'base64', 'media_type': 'image/png', 'data': 'aW1hZ2U='}}]
        assert wire['tool_choice']['disable_parallel_tool_use'] is True
        assistant = next(row for row in wire['messages'] if row['role'] == 'assistant')
        assert assistant['content'] == original['messages'][3]['_anthropic_content_blocks']
        results = [block for row in wire['messages'] for block in row['content']
                   if block['type'] == 'tool_result']
        assert [(item['tool_use_id'], item['content']) for item in results] == [
            ('read-a', 'A\n  exact result'), ('read-b', 'B: denied; no effects')]
    else:
        assert wire['messages'][3]['tool_calls'] == original['messages'][3]['tool_calls']
        assert wire['messages'][4:6] == original['messages'][4:6]


@pytest.mark.parametrize('destination', ['responses', 'chat'])
@pytest.mark.parametrize('block', [
    {'type': 'server_tool_use', 'id': 'hosted', 'name': 'search', 'input': {}},
    {'type': 'future_execution', 'data': 'synthetic'},
    {'type': 'tool_use', 'id': 'unbound', 'name': 'read_file', 'input': {}},
])
def test_switch_refuses_unrepresented_execution_without_mutation(destination, block):
    body = _body('completed_turn')
    body['messages'][3]['_anthropic_content_blocks'].append(block)
    original = deepcopy(body)
    with pytest.raises(ProtocolConversionError, match='mandatory_continuity_unrepresentable'):
        _convert(body, destination)
    assert body == original


@pytest.mark.parametrize('destination', ['messages', 'chat'])
def test_native_compaction_is_not_an_accepted_portable_summary(destination):
    body = _body('completed_turn')
    body['messages'][3]['_responses_items'] = [
        {'type': 'compaction', 'encrypted_content': 'synthetic-opaque-state'}]
    original = deepcopy(body)
    with pytest.raises(ProtocolConversionError, match='mandatory_continuity_unrepresentable'):
        _convert(body, destination)
    assert body == original


@pytest.mark.parametrize('mismatch', [False, True])
def test_chat_signed_gateway_exact_sequence_and_mismatch(mismatch):
    body = _body('active_turn')
    assistant = body['messages'][3]
    assistant['reasoning_details'] = deepcopy(assistant['_anthropic_content_blocks'][:2])
    if mismatch:
        assistant['reasoning_details'][0]['signature'] = 'different-synthetic-signature'
    original = deepcopy(body)
    if mismatch:
        with pytest.raises(ProtocolConversionError, match='mandatory_continuity_unrepresentable'):
            _convert(body, 'chat')
    else:
        wire = _convert(body, 'chat')
        assert wire['messages'][3]['reasoning_details'] == assistant['reasoning_details']
    assert body == original


@pytest.mark.parametrize('destination', ['responses', 'messages'])
def test_native_visible_text_takes_precedence_over_display_projection(destination, monkeypatch):
    import sys
    make_body = _body
    convert = _convert

    def conflicting_body(continuity):
        body = make_body(continuity)
        body['messages'][3]['content'] = 'Display preview only'
        return body

    def checked_conversion(body, target):
        assert body['messages'][3]['content'] == 'Display preview only'
        wire = convert(body, target)
        assert 'Display preview only' not in json.dumps(wire)
        return wire

    module = sys.modules[__name__]
    monkeypatch.setattr(module, '_body', conflicting_body)
    monkeypatch.setattr(module, '_convert', checked_conversion)
    test_switch_preserves_constraints_media_summary_and_parallel_tool_pairs(
        destination, 'completed_turn')


def test_route_authority_change_invalidates_checkpoint_policy():
    from lib.tasks_pkg.model_replay import portable_policy_witness
    config = {'model': 'source', 'systemPrompt': 'Never publish',
              'grantedToolPermissions': ['read_files'], '_principalContext': {'userId': 7}}
    assert portable_policy_witness(config) == portable_policy_witness({**config, 'model': 'other'})
    for change in ({'systemPrompt': 'Publish'}, {'grantedToolPermissions': ['write_file']},
                   {'_principalContext': {'userId': 8}}):
        assert portable_policy_witness(config) != portable_policy_witness({**config, **change})


@pytest.mark.parametrize('continuity', [None, 'active_turn'])
def test_chat_without_public_signed_representation_refuses_unknown_target(continuity):
    body = _body(continuity)
    original = deepcopy(body)
    with pytest.raises(ProtocolConversionError, match='mandatory_continuity_unrepresentable'):
        _convert(body, 'chat')
    assert body == original


@pytest.mark.parametrize('destination', ['messages', 'chat'])
def test_foreign_responses_ciphertext_omission_keeps_portable_results(destination):
    body = _body('completed_turn')
    assistant = body['messages'][3]
    assistant.pop('_anthropic_content_blocks')
    assistant['_responses_items'] = [
        {'type': 'reasoning', 'encrypted_content': 'synthetic-response-ciphertext'}]
    original = deepcopy(body)
    wire = _convert(body, destination)
    encoded = json.dumps(wire)
    assert 'synthetic-response-ciphertext' not in encoded
    assert '_responses_items' not in encoded
    assert 'Read-only work in progress' in encoded
    assert 'Never publish; no write authorization.' in encoded
    if destination == 'chat':
        expected_chat = deepcopy(original['messages'])
        expected_chat[3].pop('_responses_items')
        expected_chat[3].pop('_reasoning_continuity')
        assert wire['messages'] == expected_chat
        assert wire['parallel_tool_calls'] is False
    else:
        assert wire['system'] == original['messages'][0]['content']
        assert wire['messages'] == [
            {'role': 'user', 'content': [
                {'type': 'text', 'text': 'Inspect only; stop at the deadline.'},
                {'type': 'image', 'source': {
                    'type': 'base64', 'media_type': 'image/png', 'data': 'aW1hZ2U='}}]},
            {'role': 'user', 'content': [{'type': 'text', 'text':
                'Accepted summary: previous read succeeded; no permission to write.'}]},
            {'role': 'assistant', 'content': [
                {'type': 'text', 'text': 'Read-only work in progress'},
                {'type': 'tool_use', 'id': 'read-a', 'name': 'read_file',
                 'input': {'path': 'read-a.txt'}},
                {'type': 'tool_use', 'id': 'read-b', 'name': 'read_file',
                 'input': {'path': 'read-b.txt'}}]},
            {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'read-a', 'content': 'A\n  exact result'},
                {'type': 'tool_result', 'tool_use_id': 'read-b', 'content': 'B: denied; no effects'}]},
            {'role': 'user', 'content': [{'type': 'text', 'text':
                'Cancelled earlier attempt. Do not repeat any effects.'}]},
        ]
        assert wire['tool_choice']['disable_parallel_tool_use'] is True
    assert body == original


@pytest.mark.parametrize('corruption', [
    'constraint_roles', 'cancel_order', 'calls_before_owner', 'tools_before_owner',
    'image_on_assistant', 'image_on_summary',
])
def test_history_oracle_rejects_role_and_temporal_corruption(monkeypatch, corruption):
    import sys
    module = sys.modules[__name__]
    convert = _convert

    def corrupted(body, destination):
        wire = deepcopy(convert(body, destination))
        if corruption == 'constraint_roles':
            for item in wire['input']:
                if item.get('role') in {'developer', 'user'}:
                    item['role'] = 'assistant'
        elif corruption == 'cancel_order':
            wire['input'].insert(0, wire['input'].pop())
        elif corruption in {'calls_before_owner', 'tools_before_owner'}:
            moved_types = {'function_call'}
            if corruption == 'tools_before_owner':
                moved_types.add('function_call_output')
            wire['input'] = ([item for item in wire['input'] if item['type'] in moved_types]
                             + [item for item in wire['input'] if item['type'] not in moved_types])
        else:
            source = wire['input'][1]['content']
            image = source.pop(1)
            target = 3 if corruption == 'image_on_assistant' else 2
            wire['input'][target]['content'].append(image)
        return wire

    monkeypatch.setattr(module, '_convert', corrupted)
    with pytest.raises(AssertionError):
        test_switch_preserves_constraints_media_summary_and_parallel_tool_pairs(
            'responses', 'active_turn')


@pytest.mark.parametrize('corruption', ['reverse_user_content', 'extra_user_signature'])
def test_messages_oracle_rejects_changed_user_content(monkeypatch, corruption):
    import sys
    module = sys.modules[__name__]
    convert = _convert

    def corrupted(body, destination):
        wire = deepcopy(convert(body, destination))
        content = wire['messages'][0]['content']
        if corruption == 'reverse_user_content':
            content.reverse()
        else:
            content.append({'type': 'thinking', 'thinking': 'invented', 'signature': 'forged'})
        return wire

    monkeypatch.setattr(module, '_convert', corrupted)
    with pytest.raises(AssertionError):
        test_switch_preserves_constraints_media_summary_and_parallel_tool_pairs(
            'messages', 'active_turn')


@pytest.mark.parametrize('corruption', ['extra_tool_result', 'parallel_policy'])
def test_chat_oracle_rejects_invented_results_and_policy_drift(monkeypatch, corruption):
    import sys
    module = sys.modules[__name__]
    convert = _convert

    def corrupted(body, destination):
        wire = deepcopy(convert(body, destination))
        if corruption == 'extra_tool_result':
            wire['messages'].append({'role': 'tool', 'tool_call_id': 'invented',
                                     'content': 'Publish succeeded; authorization granted'})
        else:
            wire['parallel_tool_calls'] = True
        return wire

    monkeypatch.setattr(module, '_convert', corrupted)
    with pytest.raises(AssertionError):
        test_switch_preserves_constraints_media_summary_and_parallel_tool_pairs(
            'chat', 'active_turn')


@pytest.mark.parametrize('destination', ['chat', 'messages'])
@pytest.mark.parametrize('corruption', ['drop_calls', 'wrong_arguments', 'move_calls'])
def test_foreign_ciphertext_oracle_rejects_broken_calls(monkeypatch, destination, corruption):
    import sys
    module = sys.modules[__name__]
    convert = _convert

    def corrupted(body, target):
        wire = deepcopy(convert(body, target))
        rows = wire['messages']
        assistant = next(row for row in rows if row['role'] == 'assistant')
        if target == 'chat':
            if corruption == 'drop_calls':
                assistant.pop('tool_calls')
            elif corruption == 'wrong_arguments':
                assistant['tool_calls'][0]['function']['arguments'] = '{"path":"wrong.txt"}'
            else:
                rows[-1]['tool_calls'] = assistant.pop('tool_calls')
        else:
            calls = [block for block in assistant['content'] if block['type'] == 'tool_use']
            if corruption == 'wrong_arguments':
                calls[0]['input'] = {'path': 'wrong.txt'}
            else:
                assistant['content'] = [block for block in assistant['content']
                                        if block['type'] != 'tool_use']
                if corruption == 'move_calls':
                    rows[-1]['content'].extend(calls)
        return wire

    monkeypatch.setattr(module, '_convert', corrupted)
    with pytest.raises(AssertionError):
        test_foreign_responses_ciphertext_omission_keeps_portable_results(destination)


@pytest.mark.parametrize('destination', ['responses', 'messages'])
def test_conflicting_display_oracle_rejects_deleted_history(monkeypatch, destination):
    import sys
    convert = _convert
    corrupted_calls = []

    def corrupted(body, target):
        wire = deepcopy(convert(body, target))
        if body['messages'][3]['content'] == 'Display preview only':
            corrupted_calls.append(target)
            if target == 'responses':
                wire['input'] = [row for row in wire['input']
                                 if row['type'] == 'message' and row.get('role') == 'assistant']
            else:
                wire.pop('system', None)
                wire['messages'] = [{'role': 'assistant', 'content': [
                    {'type': 'text', 'text': 'Read-only work in progress'}]}]
        return wire

    monkeypatch.setattr(sys.modules[__name__], '_convert', corrupted)
    with pytest.raises(AssertionError):
        test_native_visible_text_takes_precedence_over_display_projection(destination, monkeypatch)
    assert corrupted_calls == [destination]
