"""The Anthropic recount reuses each history tool input across rounds, exactly.

``recount_anthropic_input`` must return what the former implementation
returned (every ``tool_use`` input re-encoded from the translated body) for
every request shape, while a later round over the same history neither
decodes nor encodes the unchanged calls again. The translator's task-owned
``ToolInputs`` memo keys held ``arguments`` strings; the settled inputs keep
their compact text and count digest in their own memo slots.
"""
from __future__ import annotations

import json
import random

import pytest

pytestmark = pytest.mark.unit

from lib.llm.anthropic_outbound import openai_body_to_anthropic
from lib.llm.anthropic_outbound._tool_inputs import (
    ANTHROPIC_TOOL_INPUTS_KEY, ToolInputs,
)
from lib.llm.llm_errors import PromptTooLongError
from lib.llm.request import detach_input, recount_anthropic_input
from lib.llm_sanitize._fields import _strip_non_api_fields
from lib.token_counter import RequestTextCounts, count_tokens
from lib.token_counter.evidence import TEXT_COUNT_VIEW_KEY

MODEL = 'claude-opus-4.6'
TOOLS = [{'type': 'function', 'function': {
    'name': name, 'description': f'{name} tool',
    'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}}
    for name in ('write_file', 'read_files', 'grep')]


def _legacy_recount(body, *, model):
    """The former implementation, verbatim, counted without any memo."""
    import json as _json
    from lib.llm.body._admission import enforce_body_input_limit

    def _encode(value):
        return _json.JSONEncoder(ensure_ascii=False, separators=(',', ':'),
                                 allow_nan=False).encode(value).encode('utf-8')
    messages = []
    for message in body.get('messages') or ():
        projected = dict(message)
        content = message.get('content')
        if isinstance(content, list):
            blocks, calls, thinking = [], [], []
            for block in content:
                kind = block.get('type') if isinstance(block, dict) else None
                if kind == 'tool_use':
                    calls.append({'function': {
                        'name': block.get('name', ''),
                        'arguments': _encode(block.get('input', {})).decode()}})
                elif kind == 'image':
                    blocks.append({'type': 'image_url'})
                elif kind == 'thinking':
                    thinking.append({'text': block.get('thinking', '')})
                else:
                    blocks.append(block)
            projected.update(content=blocks, tool_calls=calls,
                             reasoning_content=thinking)
        messages.append(projected)
    return enforce_body_input_limit(
        {'messages': messages, 'system': body.get('system'),
         'tools': body.get('tools')}, model=model)


def _arguments(rng, index):
    kinds = [
        lambda: json.dumps({'path': f'lib/m{index}.py', 'content': 'x = 1\n' * rng.randint(9_000, 30_000)}),
        lambda: json.dumps({'note': '上下文 🙂 "quoted" \\ back\u2028sep', 'n': index}, ensure_ascii=True),
        lambda: json.dumps({'note': '上下文 🙂', 'deep': [[{'k': [1, 2.5, -0.0, 1e21, True, None]}]]},
                           ensure_ascii=False, indent=2),
        lambda: '{"a":1.0,"b":1E5,"c":-0,"d":"\\u00e9\\/"}',
        lambda: '{}',
        lambda: json.dumps({'reads': [{'path': f'p{n}', 'start_line': n} for n in range(rng.randint(1, 40))]}),
        lambda: json.dumps({'big': {str(n): 'v' * rng.randint(0, 300) for n in range(rng.randint(50, 400))}}),
    ]
    return kinds[index % len(kinds)]()


def _history(seed, pairs, *, colon_ids=False, cache_tail=False, opaque=False):
    rng = random.Random(seed)
    messages = [{'role': 'system', 'content': 'system policy 🙂'},
                {'role': 'user', 'content': [
                    {'type': 'text', 'text': 'look at this'},
                    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,iVBORw0KGgo='}}]}]
    for index in range(pairs):
        call = f'functions.grep:{index}' if colon_ids else f'call_{seed}_{index}'
        assistant = {'role': 'assistant', 'content': f'step {index}' if index % 2 else '',
                     'tool_calls': [{'id': call, 'type': 'function', 'function': {
                         'name': TOOLS[index % 3]['function']['name'],
                         'arguments': _arguments(rng, index)}}]}
        if index % 3 == 0:
            assistant['reasoning_content'] = f'thinking {index} 上下文'
            assistant['thinking_signature'] = f'sig{index}'
        if opaque and index == 1:
            assistant['_anthropic_content_blocks'] = [
                {'type': 'server_tool_use', 'id': 'srv_1', 'name': 'web_search',
                 'input': {'query': 'q'}},
                {'type': 'web_search_tool_result', 'tool_use_id': 'srv_1', 'content': []}]
        messages.append(assistant)
        messages.append({'role': 'tool', 'tool_call_id': call,
                         'content': f'result {index} ' + 'r' * rng.randint(0, 400)})
    if cache_tail:
        tail = messages[-2]
        tail['content'] = [{'type': 'text', 'text': tail['content'] or 'tail',
                            'cache_control': {'type': 'ephemeral'}}]
        messages[-1]['content'] = [{'type': 'text', 'text': messages[-1]['content'],
                                    'cache_control': {'type': 'ephemeral'}}]
    return messages


class _Round:
    """One round's canonical body exactly as stream ingress receives it."""

    def __init__(self, memo, inputs):
        self.memo, self.inputs = memo, inputs

    def wire(self, messages, *, owner=True, cloak=False):
        body = {'model': MODEL, 'max_tokens': 4096, 'tools': TOOLS,
                'messages': _strip_non_api_fields(messages)}
        detach_input(body)
        view = None
        if self.memo is not None:
            view = self.memo.view('provider')
            count_tokens(body['messages'], model=MODEL, tools=TOOLS,
                         mode='tiktoken', text_count_memo=view)
            body[TEXT_COUNT_VIEW_KEY] = view
        if owner and self.inputs is not None:
            body[ANTHROPIC_TOOL_INPUTS_KEY] = self.inputs
        wire = openai_body_to_anthropic(body)
        if cloak:
            from lib.oauth.outbound import apply_claude_cloak
            wire, _reverse = apply_claude_cloak(wire)
            wire.pop('metadata')  # a random fake user id per call
        return wire, view


SHAPES = [
    dict(seed=1, pairs=12),
    dict(seed=2, pairs=9, colon_ids=True),
    dict(seed=3, pairs=8, cache_tail=True),
    dict(seed=4, pairs=7, opaque=True),
    dict(seed=5, pairs=10, cloak=True),
    dict(seed=6, pairs=5, colon_ids=True, cache_tail=True, cloak=True),
]


@pytest.mark.parametrize('shape', SHAPES, ids=lambda shape: '-'.join(
    f'{key}={value}' for key, value in shape.items()))
@pytest.mark.parametrize('with_memo', [True, False])
def test_recount_equals_former_implementation_across_rounds(shape, with_memo):
    shape = dict(shape)
    cloak = shape.pop('cloak', False)
    history = _history(**shape)
    memo = RequestTextCounts() if with_memo else None
    rounds = _Round(memo, ToolInputs() if with_memo else None)
    reference = _Round(None, None)
    # Round N, N+1 (one more call), N+2 (compaction dropped the oldest pair).
    for messages in (history[:-2], history, history[:2] + history[4:]):
        scope = memo.shared_text_identities() if memo else None
        if scope:
            scope.__enter__()
        try:
            wire, view = rounds.wire(messages, cloak=cloak)
            plain, _ = reference.wire(messages, cloak=cloak)
            # The translated wire is unchanged, byte for byte.
            assert json.dumps(wire, ensure_ascii=False) == json.dumps(plain, ensure_ascii=False)
            expected = _legacy_recount(plain, model=MODEL)
            assert recount_anthropic_input(
                wire, model=MODEL, provider_id='p', text_count_view=view) == expected
            assert recount_anthropic_input(wire, model=MODEL) == expected
        finally:
            if scope:
                scope.__exit__(None, None, None)


def test_later_round_decodes_and_encodes_only_new_calls(monkeypatch):
    import lib.llm.anthropic_outbound._tool_inputs as tool_inputs
    import lib.llm.request as request
    history = _history(11, 20)
    memo, owner = RequestTextCounts(), ToolInputs()
    rounds = _Round(memo, owner)
    with memo.shared_text_identities():
        wire, view = rounds.wire(history[:-2])
        recount_anthropic_input(wire, model=MODEL, text_count_view=view)

    decoded, encoded, hashed = [], [], []
    real_decode, real_encode = tool_inputs.object_arguments, request._encode
    monkeypatch.setattr(tool_inputs, 'object_arguments',
                        lambda value: decoded.append(value) or real_decode(value))
    monkeypatch.setattr(request, '_encode',
                        lambda value, **kw: encoded.append(value) or real_encode(value, **kw))
    import lib.token_counter.tiktoken_counter as counter
    real_digest = counter._text_digest
    monkeypatch.setattr(counter, '_text_digest',
                        lambda text: hashed.append(text) or real_digest(text))
    with memo.shared_text_identities():
        wire, view = rounds.wire(history)
        hashed.clear()
        tokens = recount_anthropic_input(wire, model=MODEL, text_count_view=view)
    new_arguments = history[-2]['tool_calls'][0]['function']['arguments']
    assert decoded == [new_arguments]
    assert len(encoded) == 1 and encoded[0] == json.loads(new_arguments)
    new_text = request.tool_input_text(encoded[0])
    assert all(text == new_text or not text.startswith('{') for text in hashed)
    plain, _ = _Round(None, None).wire(history)
    assert tokens == _legacy_recount(plain, model=MODEL)


def test_owner_retains_exactly_the_latest_request_and_checks_identity():
    owner = ToolInputs()
    first = _history(21, 4)
    rounds = _Round(None, owner)
    rounds.wire(first)
    held = {id(text) for text, _value in owner._held.values()}
    assert held == {id(m['tool_calls'][0]['function']['arguments'])
                    for m in first if m.get('tool_calls')}
    # Equal content in a new string object is decoded again, never trusted by id.
    copied = [dict(m) for m in first]
    copied[2] = {**copied[2], 'tool_calls': [{**copied[2]['tool_calls'][0], 'function': {
        **copied[2]['tool_calls'][0]['function'],
        'arguments': ''.join(copied[2]['tool_calls'][0]['function']['arguments'])}}]}
    shorter = copied[:2] + copied[4:]
    wire, _ = rounds.wire(shorter)
    assert len(owner._held) == sum(1 for m in shorter if m.get('tool_calls'))
    plain, _ = _Round(None, None).wire(shorter)
    assert json.dumps(wire) == json.dumps(plain)


def test_admission_decision_is_unchanged(monkeypatch):
    import lib.tasks_pkg.compaction._tokens as tokens_module
    history = _history(31, 6)
    plain, _ = _Round(None, None).wire(history)
    expected = _legacy_recount(plain, model=MODEL)
    memo = RequestTextCounts()
    rounds = _Round(memo, ToolInputs())
    for window, refused in ((expected, False), (expected - 1, True)):
        monkeypatch.setattr(tokens_module, 'resolve_model_context_limit',
                            lambda *_a, _w=window, **_k: _w)
        with memo.shared_text_identities():
            wire, view = rounds.wire(history)
            if refused:
                with pytest.raises(PromptTooLongError):
                    recount_anthropic_input(wire, model=MODEL, text_count_view=view)
            else:
                assert recount_anthropic_input(
                    wire, model=MODEL, text_count_view=view) == expected


def test_invalid_arguments_fail_as_before_and_are_not_memoized():
    from lib.llm.conversion import ProtocolConversionError
    owner = ToolInputs()
    history = _history(41, 2)
    history[2]['tool_calls'][0]['function']['arguments'] = '{"a": NaN}'
    with pytest.raises(ProtocolConversionError):
        openai_body_to_anthropic({'model': MODEL, 'messages': history,
                                  ANTHROPIC_TOOL_INPUTS_KEY: owner})
    assert owner._held == {}
