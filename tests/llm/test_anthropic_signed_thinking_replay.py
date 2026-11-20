"""Signed Anthropic thinking blocks replay exactly as returned.

Opus 4.7+, Fable 5.x, Sonnet 5.5 and Haiku 5.5 default ``thinking.display`` to
``"omitted"``: each thinking block arrives with an empty ``thinking`` field and
an opaque ``signature`` that carries the encrypted reasoning. Opus 5.5 also
returns its notes between tool calls as further thinking blocks. The API
requires every assistant turn to be sent back unchanged — every block, in
order, including thinking blocks whose text is empty — or the model loses its
earlier reasoning on each tool round without any error:
https://platform.claude.com/docs/en/build-with-claude/preserved-thinking
"""
import json
import time

import pytest

from lib.llm._sse_core import SSEAccumulator
from lib.llm._sse_framer import SSEFramer
from lib.llm.anthropic_outbound import (
    anthropic_response_to_openai,
    openai_body_to_anthropic,
)
from lib.llm.anthropic_outbound._sse import AnthropicSSETranslator
from lib.tasks_pkg.conv_message_builder._toolcalls import (
    _reconstruct_tool_call_messages,
    build_assistant_tool_call_message,
)

pytestmark = pytest.mark.unit

_WIRE = 'kunlunbedrock.claude-opus-5.5-m17'
_TOOLS = [{'type': 'function', 'function': {
    'name': 'read', 'parameters': {'type': 'object', 'properties': {}}}}]


class _Dumper:
    enabled = False

    def line(self, *_args, **_kwargs):
        pass

    def payload(self, *_args, **_kwargs):
        pass

    def dump_anomaly(self, *_args, **_kwargs):
        pass

    def finish(self, *_args, **_kwargs):
        pass


def _thinking(index, text, signature):
    events = [{'type': 'content_block_start', 'index': index,
               'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''}}]
    if text:
        events.append({'type': 'content_block_delta', 'index': index,
                       'delta': {'type': 'thinking_delta', 'thinking': text}})
    events += [{'type': 'content_block_delta', 'index': index,
                'delta': {'type': 'signature_delta', 'signature': signature}},
               {'type': 'content_block_stop', 'index': index}]
    return events


def _tool_use(index, call_id):
    return [{'type': 'content_block_start', 'index': index,
             'content_block': {'type': 'tool_use', 'id': call_id,
                               'name': 'read', 'input': {}}},
            {'type': 'content_block_delta', 'index': index,
             'delta': {'type': 'input_json_delta', 'partial_json': '{}'}},
            {'type': 'content_block_stop', 'index': index}]


def _stream(*blocks):
    return [
        {'type': 'message_start', 'message': {
            'id': 'msg_1', 'type': 'message', 'role': 'assistant',
            'model': 'claude-opus-5-5', 'content': [],
            'usage': {'input_tokens': 10, 'output_tokens': 0}}},
        *[event for block in blocks for event in block],
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'},
         'usage': {'output_tokens': 50}},
        {'type': 'message_stop'},
    ]


def _accumulate(events):
    accumulator = SSEAccumulator(
        {'model': _WIRE, 'tools': _TOOLS}, 'trace', _Dumper(),
        AnthropicSSETranslator(), time.monotonic())
    framer = SSEFramer()
    wire = ''.join(f'event: {e["type"]}\r\ndata: {json.dumps(e)}\r\n\r\n'
                   for e in events).encode()
    for event in framer.feed(wire):
        accumulator.feed_event(event)
    for event in framer.finalize():
        accumulator.feed_event(event)
    return dict(accumulator.finalize().message)


def _live_tail(message):
    """The orchestrator's live tool-round assistant row (tool_call prelude)."""
    kwargs = dict(tool_calls=message['tool_calls'], content=message.get('content'),
                  reasoning_content=message.get('reasoning_content'),
                  thinking_signature=message.get('thinking_signature'))
    if message.get('_anthropic_content_blocks'):
        kwargs['anthropic_content_blocks'] = message['_anthropic_content_blocks']
    return build_assistant_tool_call_message(**kwargs)


def _next_request(assistant, *call_ids):
    return {'model': _WIRE, 'max_tokens': 1000, 'stream': True,
            'thinking': {'type': 'adaptive'}, 'tools': _TOOLS,
            'messages': [{'role': 'user', 'content': 'go'}, assistant,
                         *[{'role': 'tool', 'tool_call_id': call_id, 'content': 'ok'}
                           for call_id in call_ids]]}


def test_omitted_display_signature_only_block_survives_the_tool_loop():
    message = _accumulate(_stream(_thinking(0, '', 'SIG_A'), _tool_use(1, 'toolu_1')))
    wire = openai_body_to_anthropic(_next_request(_live_tail(message), 'toolu_1'))
    assert wire['messages'][1]['content'] == [
        {'type': 'thinking', 'thinking': '', 'signature': 'SIG_A'},
        {'type': 'tool_use', 'id': 'toolu_1', 'name': 'read', 'input': {}},
    ]


def test_interleaved_thinking_blocks_keep_their_own_signatures_and_order():
    message = _accumulate(_stream(
        _thinking(0, '\n plan\n  \t', 'SIG_ONE'), _tool_use(1, 'toolu_1'),
        _thinking(2, ' progress note\t\n ', 'SIG_TWO'), _tool_use(3, 'toolu_2')))
    wire = openai_body_to_anthropic(
        _next_request(_live_tail(message), 'toolu_1', 'toolu_2'))
    expected = [
        {'type': 'thinking', 'thinking': '\n plan\n  \t', 'signature': 'SIG_ONE'},
        {'type': 'tool_use', 'id': 'toolu_1', 'name': 'read', 'input': {}},
        {'type': 'thinking', 'thinking': ' progress note\t\n ', 'signature': 'SIG_TWO'},
        {'type': 'tool_use', 'id': 'toolu_2', 'name': 'read', 'input': {}},
    ]
    assert message['_anthropic_content_blocks'] == expected
    assert _live_tail(message)['_anthropic_content_blocks'] == expected
    assert wire['messages'][1]['content'] == expected
    assert all('SIG_ONESIG_TWO' != block.get('signature')
               for block in wire['messages'][1]['content'])


def test_cold_history_reconstruction_replays_the_native_turn():
    message = _accumulate(_stream(_thinking(0, '', 'SIG_A'), _tool_use(1, 'toolu_1')))
    round_row = {
        'toolCallId': 'toolu_1', 'toolName': 'read', 'toolArgs': '{}',
        'toolContent': 'ok', 'status': 'done', 'llmRound': 0, 'roundNum': 0,
        'thinkingSignature': message['thinking_signature'],
        '_anthropicContentBlocks': message['_anthropic_content_blocks'],
    }
    rebuilt = _reconstruct_tool_call_messages([round_row])
    wire = openai_body_to_anthropic({**_next_request(rebuilt[0]), 'messages': [
        {'role': 'user', 'content': 'go'}, *rebuilt]})
    assert wire['messages'][1]['content'][0] == {
        'type': 'thinking', 'thinking': '', 'signature': 'SIG_A'}


@pytest.mark.parametrize('thinking', [
    [{'type': 'thinking', 'thinking': '\nplan\n', 'signature': 'SIG_A'}],
    [{'type': 'thinking', 'thinking': '', 'signature': 'SIG_A'}],
    [{'type': 'thinking', 'thinking': '\nplan\n', 'signature': 'SIG_A'},
     {'type': 'thinking', 'thinking': 'next', 'signature': 'SIG_B'}],
    [{'type': 'redacted_thinking', 'data': 'opaque-replay'}],
])
def test_cold_history_chat_replay_uses_exact_native_signed_thinking(thinking):
    """Display text may be trimmed; signatures still belong to native bytes."""
    from copy import deepcopy
    from lib.llm.body import build_body
    from lib.llm.openai_outbound import openai_body_to_chat
    native = [*thinking, {'type': 'tool_use', 'id': 'toolu_1',
                          'name': 'read', 'input': {}}]
    row = {
        'toolCallId': 'toolu_1', 'toolName': 'read', 'toolArgs': '{}',
        'toolContent': 'ok', 'status': 'done', 'llmRound': 0, 'roundNum': 0,
        'thinking': ''.join(block.get('thinking', '') for block in thinking).strip(),
        'thinkingSignature': thinking[-1].get('signature', ''),
        '_anthropicContentBlocks': native,
    }
    before = deepcopy(row)
    rebuilt = _reconstruct_tool_call_messages([row])
    body = build_body(_WIRE, [{'role': 'user', 'content': 'go'}, *rebuilt],
                      max_tokens=1000, context_window=128000,
                      precomputed_input_tokens=100, tools=_TOOLS)
    wire = openai_body_to_chat(body)
    assert wire['messages'][1]['reasoning_details'] == thinking
    assert openai_body_to_anthropic(body)['messages'][1]['content'] == native
    assert row == before


def test_replayed_native_turn_keeps_the_live_wire_bytes():
    """TofuDB (serde_json without ``preserve_order``) returns stored objects
    key-sorted. The live tail keeps the stream's key order, so replaying the
    reloaded blocks verbatim changed every native assistant turn's bytes at the
    next turn boundary and re-billed the cached history (WIRE PREFIX CHANGED
    ``<bytes>assistant{content}`` on the first round of each new turn)."""
    message = _accumulate(_stream(
        _thinking(0, '', 'SIG_A'),
        [{'type': 'content_block_start', 'index': 1, 'content_block': {
            'type': 'tool_use', 'id': 'toolu_1', 'name': 'read', 'input': {}}},
         {'type': 'content_block_delta', 'index': 1, 'delta': {
             'type': 'input_json_delta',
             'partial_json': '{"path": "a.py", "end_line": 3}'}},
         {'type': 'content_block_stop', 'index': 1}]))
    durable_blocks = json.loads(json.dumps(
        message['_anthropic_content_blocks'], sort_keys=True))
    assert json.dumps(durable_blocks) != json.dumps(
        message['_anthropic_content_blocks'])
    replayed = _reconstruct_tool_call_messages([{
        'toolCallId': 'toolu_1', 'toolName': 'read',
        'toolArgs': message['tool_calls'][0]['function']['arguments'],
        'toolContent': 'ok', 'status': 'done', 'llmRound': 0, 'roundNum': 0,
        'thinkingSignature': message['thinking_signature'],
        '_anthropicContentBlocks': durable_blocks,
    }])[0]
    live_wire = openai_body_to_anthropic(
        _next_request(_live_tail(message), 'toolu_1'))['messages'][1]
    replay_wire = openai_body_to_anthropic(
        _next_request(replayed, 'toolu_1'))['messages'][1]
    assert json.dumps(live_wire, ensure_ascii=False) == json.dumps(
        replay_wire, ensure_ascii=False)
    assert live_wire['content'][1]['input'] == {'path': 'a.py', 'end_line': 3}


def test_non_streaming_response_keeps_signed_empty_thinking_native():
    converted = anthropic_response_to_openai({
        'id': 'msg_1', 'model': 'claude-opus-5-5', 'stop_reason': 'tool_use',
        'usage': {'input_tokens': 1, 'output_tokens': 1},
        'content': [{'type': 'thinking', 'thinking': '', 'signature': 'SIG_A'},
                    {'type': 'tool_use', 'id': 'toolu_1', 'name': 'read', 'input': {}}],
    })
    message = converted['choices'][0]['message']
    assert message['_anthropic_content_blocks'][0] == {
        'type': 'thinking', 'thinking': '', 'signature': 'SIG_A'}


def test_settled_native_turn_still_crosses_to_responses_once():
    from lib.llm.responses_outbound import openai_body_to_responses_checked
    message = _accumulate(_stream(_thinking(0, '', 'SIG_A'), _tool_use(1, 'toolu_1')))
    assistant = dict(_live_tail(message), _reasoning_continuity='completed_turn')
    result = openai_body_to_responses_checked({
        'model': 'responses-model', 'tools': _TOOLS,
        'messages': [{'role': 'user', 'content': 'go'}, assistant,
                     {'role': 'tool', 'tool_call_id': 'toolu_1', 'content': 'ok'}]},
        profile='default')
    payload = result.require_payload()
    assert result.permitted_conversion_losses == {'historical_thinking_block_omitted': 1}
    assert [item['type'] for item in payload['input']] == [
        'message', 'function_call', 'function_call_output']
    assert 'SIG_A' not in repr(payload)


def test_active_signed_thinking_crosses_responses_with_explicit_loss_report():
    from lib.llm.responses_outbound import openai_body_to_responses_checked
    message = _accumulate(_stream(_thinking(0, '', 'SIG_A'), _tool_use(1, 'toolu_1')))
    result = openai_body_to_responses_checked({
        'model': 'responses-model', 'tools': _TOOLS,
        'messages': [{'role': 'user', 'content': 'go'}, _live_tail(message),
                     {'role': 'tool', 'tool_call_id': 'toolu_1', 'content': 'ok'}]},
        profile='default')
    payload = result.require_payload()
    assert result.permitted_conversion_losses == {'cross_protocol_reasoning_omitted': 1}
    assert [item['type'] for item in payload['input']] == [
        'message', 'function_call', 'function_call_output']
    assert payload['input'][-1]['output'] == 'ok'
    assert 'SIG_A' not in repr(payload)


@pytest.mark.parametrize('model_id', [
    'claude-opus-5.5', 'claude-opus-5-5', 'claude-opus-5', 'claude-opus-4.8',
    'claude-fable-5-1', 'claude-sonnet-5.5', 'claude-haiku-5.5'])
def test_v2_profile_requests_readable_thinking_on_omitted_default_models(model_id):
    """Production compiles the installed v2 profile, not the legacy adapter."""
    from lib.llm import build_body
    from lib.model_routing.model_defaults import apply_model_defaults
    model = {'creator_id': 'anthropic', 'model_id': model_id}
    assert apply_model_defaults(model)
    body = build_body(
        _WIRE, [{'role': 'user', 'content': 'hi'}], max_tokens=4096,
        thinking_enabled=True, thinking_depth='high', stream=True,
        inference_profile=model['inference_profile'])
    assert body['thinking']['type'] == 'adaptive'
    assert body['thinking']['display'] == 'summarized'


def test_superseded_shipped_profile_is_upgraded_but_owner_edit_is_kept():
    from lib.model_routing.model_defaults import apply_model_defaults
    shipped_without_display = {
        'source': 'official',
        'reasoning': {'support': 'supported', 'wire_format': 'thinking_type',
                      'enabled_type': 'adaptive', 'effort_field': 'effort',
                      'can_disable': False,
                      'supported_efforts': ['low', 'medium', 'high', 'xhigh', 'max'],
                      'default_effort': 'medium', 'effort_map': {'ultra': 'max'}},
        'sampling': {'omit': ['temperature', 'top_p', 'top_k']}}
    installed = {'creator_id': 'anthropic', 'model_id': 'claude-opus-5.5',
                 'inference_profile': shipped_without_display}
    assert apply_model_defaults(installed)
    assert installed['inference_profile']['reasoning']['display'] == 'summarized'
    owner_choice = {'creator_id': 'anthropic', 'model_id': 'claude-opus-5.5',
                    'inference_profile': {**shipped_without_display,
                                          'source': 'configured',
                                          'reasoning': {**shipped_without_display['reasoning'],
                                                        'display': 'omitted'}}}
    apply_model_defaults(owner_choice)
    assert owner_choice['inference_profile']['reasoning']['display'] == 'omitted'


def test_shipped_defaults_converge_after_one_application():
    """A seed equal to one of its superseded profiles would never converge."""
    import copy as _copy
    from lib.model_routing.model_defaults import apply_model_defaults
    for model_id in ('claude-opus-5', 'claude-opus-5.5', 'claude-fable-5-1'):
        model = {'creator_id': 'anthropic', 'model_id': model_id}
        assert apply_model_defaults(model)
        settled = _copy.deepcopy(model)
        assert not apply_model_defaults(settled)
        assert settled == model


def test_unsigned_thinking_is_not_promoted_to_replay_state():
    message = _accumulate([
        {'type': 'message_start', 'message': {
            'id': 'msg_1', 'type': 'message', 'role': 'assistant',
            'model': 'claude-opus-5-5', 'content': [],
            'usage': {'input_tokens': 10, 'output_tokens': 0}}},
        {'type': 'content_block_start', 'index': 0,
         'content_block': {'type': 'thinking', 'thinking': ''}},
        {'type': 'content_block_delta', 'index': 0,
         'delta': {'type': 'thinking_delta', 'thinking': 'gateway trace'}},
        {'type': 'content_block_stop', 'index': 0},
        *_tool_use(1, 'toolu_1'),
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'},
         'usage': {'output_tokens': 5}},
        {'type': 'message_stop'},
    ])
    assert '_anthropic_content_blocks' not in message
    wire = openai_body_to_anthropic(_next_request(_live_tail(message), 'toolu_1'))
    assert [block['type'] for block in wire['messages'][1]['content']] == ['tool_use']


def _unanswered_tool_uses(wire_messages):
    """Anthropic's rule: each tool_use is answered in the very next message."""
    unanswered = []
    for index, message in enumerate(wire_messages):
        if message.get('role') != 'assistant' or not isinstance(message.get('content'), list):
            continue
        calls = [block['id'] for block in message['content'] if block.get('type') == 'tool_use']
        following = wire_messages[index + 1] if index + 1 < len(wire_messages) else {}
        answered = {block.get('tool_use_id')
                    for block in following.get('content') or ()
                    if isinstance(block, dict) and block.get('type') == 'tool_result'}
        unanswered.extend((index, call) for call in calls if call not in answered)
    return unanswered


_EDIT_POLL_TOOLS = [{'type': 'function', 'function': {
    'name': name, 'parameters': {'type': 'object', 'properties': {}}}}
    for name in ('edit_file', 'poll')]

_EDIT_AND_POLL = [
    {'type': 'thinking', 'thinking': '', 'signature': 'SIG_A'},
    {'type': 'tool_use', 'id': 'toolu_edit', 'name': 'edit_file',
     'input': {'path': 'a.py', 'edits': []}},
    {'type': 'tool_use', 'id': 'toolu_poll', 'name': 'poll',
     'input': {'id': 'bg_1', 'wait_s': 900}},
]


def test_a_batch_sibling_missing_from_the_record_replays_a_typed_receipt():
    """WORKSPACE-ADMISSION-R2 (2026-11-11): only the batch's first row, which
    carries the response's native content, reached the record. Its poll
    sibling's row was lost, so every resume sent ``toolu_poll`` without a
    result and the provider rejected the request (HTTP 400)."""
    from lib.llm.body import build_body
    from lib.tool_results.tool_round_replay import UNRECORDED_TOOL_RESULT_TEXT
    row = {'toolCallId': 'toolu_edit', 'toolName': 'edit_file',
           'toolArgs': '{"path": "a.py", "edits": []}', 'toolContent': 'edited',
           'status': 'done', 'llmRound': 177, 'roundNum': 209,
           'attemptId': 'attempt-1', '_anthropicContentBlocks': _EDIT_AND_POLL}
    rebuilt = _reconstruct_tool_call_messages([row])
    body = build_body(_WIRE, [{'role': 'user', 'content': 'go'}, *rebuilt],
                      max_tokens=1000, context_window=128000,
                      precomputed_input_tokens=100, tools=_EDIT_POLL_TOOLS)
    wire = openai_body_to_anthropic(body)['messages']

    assert _unanswered_tool_uses(wire) == []
    # The provider's own content is replayed unchanged, signature included.
    assert wire[1]['content'] == _EDIT_AND_POLL
    results = {block['tool_use_id']: block for block in wire[2]['content']}
    assert set(results) == {'toolu_edit', 'toolu_poll'}
    assert UNRECORDED_TOOL_RESULT_TEXT in json.dumps(results['toolu_poll'])
    assert 'Result not recorded' not in json.dumps(results['toolu_edit'])



@pytest.mark.parametrize('corruption', ['clear', 'strip'])
def test_streaming_live_tail_oracle_rejects_changed_native_thinking(monkeypatch, corruption):
    import sys
    from copy import deepcopy

    module = sys.modules[__name__]
    live_tail = _live_tail

    def corrupted(message):
        tail = deepcopy(live_tail(message))
        for block in tail['_anthropic_content_blocks']:
            if block['type'] == 'thinking':
                block['thinking'] = '' if corruption == 'clear' else block['thinking'].strip()
        return tail

    monkeypatch.setattr(module, '_live_tail', corrupted)
    with pytest.raises(AssertionError):
        test_interleaved_thinking_blocks_keep_their_own_signatures_and_order()
