"""Regenerate packages/tofu-core-rs/tests/fixtures/stream_golden.json from Python.

``PYTHONPATH=. .venv/bin/python3 packages/tofu-core-rs/scripts/gen_stream_golden.py [--check]``

Each case's raw SSE text goes through the production byte path
(``SSEByteStream`` -> ``SSEAccumulator.feed_event`` -> ``finalize``) with the
translator ``prepare_request`` would attach for its protocol. The recorded
projection excludes nondeterministic fields (trace ids, timings, request
receipts, wire fingerprints). ``--check`` exits 1 when the committed fixture
differs from a fresh regeneration.
"""
import json
import sys
from pathlib import Path

import lib.model_info as model_info
from lib.cost import normalize_usage, split_input_tokens, usage_cache_convention
from lib.llm import _sse_core
from lib.llm._sse_core import SSEAccumulator
from lib.llm._sse_stream import SSEByteStream
from lib.llm.anthropic_outbound._sse import AnthropicSSETranslator
from lib.llm.responses_outbound._sse import ResponsesSSETranslator
from lib.tasks_pkg.llm_fallback._refusal import is_model_refusal

OUT = Path(__file__).resolve().parents[1] / 'tests' / 'fixtures' / 'stream_golden.json'
TRACE_ID = 'golden-trace'
MAX_TOKENS = 4096
MESSAGE_KEYS = ('role', 'content', 'reasoning_content', 'thinking_signature', 'tool_calls')
ERROR_ATTRS = ('status_code', 'is_gateway', 'is_quota', 'is_shared_contention', 'reason')
# Recorded only when neither None nor False, so classes that merely default
# them (every RateLimitError has is_subscription_quota=False) keep their shape.
EXTRA_ERROR_ATTRS = ('is_subscription_quota', 'retry_after_s', 'model', 'detected_limit',
                     'requested_limit', 'param', 'error_code', 'gateway_message',
                     'category', 'explanation')
NONDETERMINISTIC_USAGE_KEYS = ('trace_id', 'stream_elapsed_ms')


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


def _translator(protocol, model):
    if protocol == 'openai':
        return None
    if protocol == 'anthropic':
        return AnthropicSSETranslator(model=model)
    return ResponsesSSETranslator(model=model, reasoning_replay=True)


def _recording_learner(calls):
    """Stand-in for ``_learn_model_limit``: records instead of persisting.

    The real learner mutates the process-global learned limits and writes
    server_config.json; the projection records that call as ``learned_limit``.
    """
    def learn(model, limit, *, route_key=''):
        calls.append({'model': model, 'limit': limit, 'route_key': route_key})
    return learn


def project(protocol, model, sse):
    body = {'model': model, 'max_tokens': MAX_TOKENS}
    acc = SSEAccumulator(body, TRACE_ID, _Dumper(), _translator(protocol, model), 0.0)
    stream = SSEByteStream(acc)
    learned = []
    learner = _recording_learner(learned)
    saved = (_sse_core._learn_model_limit, model_info._learn_model_limit)
    _sse_core._learn_model_limit = model_info._learn_model_limit = learner
    try:
        stream.feed(sse.encode('utf-8'))
        stream.finalize(interrupted=False)
        acc.fire_final_tool_callback()
        result = acc.finalize()
    except Exception as error:  # the projection records the oracle's raise
        projected = {'class': type(error).__name__}
        for attr in ERROR_ATTRS:
            value = getattr(error, attr, None)
            if value is not None:
                projected[attr] = value
        for attr in EXTRA_ERROR_ATTRS:
            value = getattr(error, attr, None)
            if value is not None and value is not False:
                projected[attr] = value
        if learned:
            assert len(learned) == 1, learned
            projected['learned_limit'] = learned[0]
        projected['message'] = str(error)
        return {'error': projected}
    finally:
        _sse_core._learn_model_limit, model_info._learn_model_limit = saved
    assert not learned, learned
    usage = result.usage
    message = {key: result.message[key] for key in result.message if key in MESSAGE_KEYS}
    return {
        'message': message,
        'provider_finish_reason': result.provider_finish_reason,
        'finish_reason': result.finish_reason,
        'saw_done': result.saw_done,
        'saw_finish_reason': result.saw_finish_reason,
        'malformed_frame_count': result.malformed_frame_count,
        'state': result.state.value,
        'model_refusal': usage.get('_model_refusal'),
        'is_model_refusal': is_model_refusal(result),
        'usage': {key: value for key, value in usage.items()
                  if not key.startswith('_') and key not in NONDETERMINISTIC_USAGE_KEYS},
        'normalized': normalize_usage(usage),
        'convention': usage_cache_convention(usage),
        'split_input': list(split_input_tokens(usage)),
    }


def sse(*frames, done=False):
    """One ``data:`` event per frame (dicts are JSON-encoded)."""
    parts = [f'data: {json.dumps(f, ensure_ascii=False) if not isinstance(f, str) else f}\n\n'
             for f in frames]
    if done:
        parts.append('data: [DONE]\n\n')
    return ''.join(parts)


def named(*events):
    """Anthropic/Responses wire: ``event: <type>`` + ``data:`` per event."""
    return ''.join(f'event: {e["type"]}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n'
                   for e in events)


def delta(finish=None, usage=None, **fields):
    chunk = {'id': 'c1', 'object': 'chat.completion.chunk',
             'choices': [{'index': 0, 'delta': fields, 'finish_reason': finish}]}
    if usage is not None:
        chunk['usage'] = usage
    return chunk


def tc(index, id=None, name=None, arguments=None, **extra):
    call = {'index': index} if index is not None else {}
    if id is not None:
        call['id'] = id
        call['type'] = 'function'
    function = {}
    if name is not None:
        function['name'] = name
    if arguments is not None:
        function['arguments'] = arguments
    if function:
        call['function'] = function
    call.update(extra)
    return call


def usage(prompt, completion, **extra):
    return {'prompt_tokens': prompt, 'completion_tokens': completion,
            'total_tokens': prompt + completion, **extra}


def err(**fields):
    return {'error': fields}


CHAT = 'kimi-k3'
CLAUDE = 'claude-opus-5'
GPT = 'gpt-5.5'


def openai_cases():
    text = [delta(role='assistant', content=''), delta(content='Hello'), delta(content=' world')]
    yield 'openai_text_usage', sse(*text, delta('stop', usage(100, 5)), done=True)
    yield 'openai_cached_tokens', sse(*text, delta('stop'), {'choices': [], 'usage': usage(1000, 10, cached_tokens=800)}, done=True)
    yield 'openai_effective_cached_tokens', sse(*text, delta('stop', usage(2000, 10, effectiveCachedTokens=1500, cache_read_tokens=0)), done=True)
    yield 'openai_prompt_details_cached', sse(*text, delta('stop', usage(1200, 9, prompt_tokens_details={'cached_tokens': 900}, completion_tokens_details={'reasoning_tokens': 3})), done=True)
    yield 'openai_cache_read_tokens', sse(*text, delta('stop', usage(700, 9, cache_read_tokens=500, cache_write_tokens=100)), done=True)
    yield 'openai_full_cache_equality', sse(*text, delta('stop', usage(428603, 12, cached_tokens=428603)), done=True)
    yield 'openai_hybrid_impossible_cache', sse(*text, delta('stop', usage(100, 12, cache_read_tokens=15000)), done=True)
    yield 'openai_cache_only_no_prompt', sse(*text, delta('stop', {'completion_tokens': 3, 'cached_tokens': 3328}), done=True)
    yield 'openai_choice_usage', sse(delta(content='x'), {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop', 'usage': usage(10, 1)}]}, done=True)
    yield 'openai_multi_tool_fragmented', sse(
        delta(role='assistant', content='Running both.'),
        delta(tool_calls=[tc(0, 'call_a', 'read_files', '')]),
        delta(tool_calls=[tc(0, arguments='{"pa')]),
        delta(tool_calls=[tc(1, 'call_b', 'run_command', '{"comm')]),
        delta(tool_calls=[tc(0, arguments='th": "a.txt"}')]),
        delta(tool_calls=[tc(1, arguments='and": "ls"}')]),
        delta('tool_calls', usage(50, 20)), done=True)
    yield 'openai_tool_name_fragment', sse(
        delta(tool_calls=[tc(0, 'call_f', 'read_')]),
        delta(tool_calls=[tc(0, name='files')]),
        delta(tool_calls=[tc(0, arguments='{"path": "x"}')]),
        delta('tool_calls'), done=True)
    yield 'openai_tool_empty_arguments', sse(delta(tool_calls=[tc(0, 'call_n', 'list_tasks')]), delta('tool_calls'), done=True)
    yield 'openai_tool_same_index_second_id', sse(
        delta(tool_calls=[tc(0, 'call_1', 'read_files', '{"path": "a"}')]),
        delta(tool_calls=[tc(0, 'call_2', 'run_command', '{"command": "ls"}')]),
        delta('tool_calls'), done=True)
    yield 'openai_tool_unindexed', sse(
        delta(tool_calls=[tc(None, 'call_u', 'read_files', '{"path"')]),
        delta(tool_calls=[tc(None, arguments=': "u"}')]),
        delta('tool_calls'), done=True)
    yield 'openai_tool_name_reissue', sse(
        delta(tool_calls=[tc(0, 'call_r', 'read_files', '{"path": "r"}')]),
        delta(tool_calls=[tc(0, 'call_r', 'read_files', '{"path": "r"}')]),
        delta('tool_calls'), done=True)
    yield 'openai_tool_internal_prefix_filtered', sse(
        delta(tool_calls=[tc(0, 'call_i', '__internal', '{}')]), delta('tool_calls'), done=True)
    yield 'openai_tool_string_index_and_caller', sse(
        delta(tool_calls=[tc('1', 'call_s', 'read_files', '{}', caller={'type': 'program'}, extra_content={'google': {'thought_signature': 'sig'}})]),
        delta('tool_calls'), done=True)
    yield 'openai_tool_invalid_index', sse(delta(tool_calls=[tc(True, 'call_b', 'read_files', '{}')]), delta('tool_calls'), done=True)
    yield 'openai_reasoning_content', sse(
        delta(reasoning_content='Let me think. '), delta(reasoning_content='Done.'),
        delta(content='Answer.'), delta('stop', usage(30, 40, completion_tokens_details={'reasoning_tokens': 25})), done=True)
    yield 'openai_reasoning_details_signature', sse(
        delta(reasoning_details=[{'type': 'thinking', 'thinking': 'Plan A. '}]),
        delta(reasoning_details=[{'type': 'thinking', 'text': 'Plan B.'}]),
        delta(reasoning_details=[{'type': 'thinking', 'signature': 'sig-part-1'}]),
        delta(reasoning_details=[{'type': 'thinking', 'signature': 'sig-part-2'}]),
        delta(content='Final.'), delta('stop', usage(10, 10)), done=True)
    yield 'openai_role_thinking_and_invalid_reasoning', sse(
        delta(role='thinking', content='hidden thought'), delta(reasoning={'x': 1}),
        delta(content='visible'), delta('stop'), done=True)
    yield 'openai_error_http_code_429', sse(delta(content='partial'), err(message='Rate limit reached for requests', type='requests', http_code='429'))
    yield 'openai_error_status_code_text_429', sse(err(message='(Service: BedrockRuntime, Status Code: 429, Request ID: abc) Too many tokens, please wait'))
    yield 'openai_error_too_many_requests', sse(err(message='Too Many Requests: slow down'))
    yield 'openai_error_chinese_overload', sse(err(message='当前模型负载较高，请稍后重试'))
    yield 'openai_error_server_error_type', sse(err(message='internal failure', type='server_error'))
    yield 'openai_error_gateway_503', sse(err(message='upstream unavailable', http_code=503))
    yield 'openai_error_text_502_gateway', sse(err(message='bad gateway, status code: 502 from upstream'))
    yield 'openai_error_invalid_api_key', sse(err(message='Invalid API key provided'))
    yield 'openai_error_empty_message', sse(err(message=''))
    yield 'openai_error_wrapped_overload', sse(err(message='No matching constant for [529]', type='server_error'))
    yield 'openai_error_authentication', sse(err(message='invalid x-api-key', type='authentication_error'))
    yield 'openai_error_vendor_transient_403', sse(err(message='请求失败,请稍后再尝试', status=403))
    yield 'openai_error_content_filter_450', sse(err(message='blocked by policy', code='content_filter'))
    yield 'openai_error_insufficient_quota', sse(err(message='You exceeded your current quota, please check your plan and billing details.', code='insufficient_quota'))
    yield 'openai_error_shared_project_limit', sse(err(message='request reached project (kimi-k3) TPM rate limit', code=429))
    yield 'openai_error_prompt_too_long_text', sse(err(message='prompt is too long: 300000 tokens'))
    yield 'openai_error_request_too_large_413', sse(err(message='payload too big', status_code=413))
    yield 'openai_error_not_found_404', sse(err(message='no such route', type='not_found_error'))
    yield 'openai_error_other_4xx', sse(err(message='conflict', http_code='409'))
    yield 'openai_error_non_dict', sse({'error': 'plain string failure'})
    yield 'openai_finish_content_filter', sse(delta(content=''), delta('content_filter', usage(20, 0)), done=True)
    yield 'openai_done_without_finish', sse(*text, done=True)
    yield 'openai_finish_without_done', sse(*text, delta('stop', usage(10, 2)))
    yield 'openai_cut_stream', sse(*text)
    yield 'openai_malformed_json_frame', sse(delta(content='a'), '{"choices": [', delta(content='b'), delta('stop'), done=True)
    yield 'openai_consecutive_parse_errors', sse(*(['{bad'] * 10))
    yield 'openai_empty_stop', sse(delta(role='assistant', content=''), delta('stop', usage(10, 0)), done=True)
    yield 'openai_gateway_limit_text', sse(delta(content='当前小时请求过于频繁'), delta('stop', usage(5, 9)), done=True)
    yield 'openai_gateway_limit_without_usage', sse(delta(content='rate limit exceeded'), delta('stop'), done=True)
    yield 'openai_tool_calls_no_payload', sse(delta(content='Calling'), delta('tool_calls', usage(10, 3)), done=True)
    yield 'openai_foreign_anthropic_payload', sse(delta(content='a'), {'type': 'message_start', 'message': {}}, delta('stop'), done=True)
    yield 'openai_invalid_shapes', sse(delta(content='a'), {'choices': {}}, {'choices': [1]}, {'choices': [{'delta': 'x'}]},
                                       {'choices': [{'message': {'content': 'm'}}]}, delta(finish=7), delta('stop', usage=[1]), done=True)
    yield 'openai_crlf_comments_multiline', (
        ': keep-alive\r\n\r\nevent: chunk\r\ndata: {"choices": [{"delta":\r\ndata: {"content": "A"}}]}\r\n\r\n'
        'id: 7\rretry: 10\rdata: {"choices": [{"delta": {"content": "B"}, "finish_reason": "stop"}]}\r\r'
        'data: [DONE]\n\n')
    yield 'openai_bom_and_truncated_tail', '\ufeffdata: ' + json.dumps(delta('stop', content='x')) + '\n\ndata: {"choices"'
    yield 'openai_control_only_tail', sse(delta('stop', content='x')) + 'event: ping\nid: 9'
    yield 'openai_done_then_trailer_ignored', sse(delta('stop', content='x'), done=True) + 'data: {"choices": [{"delta": {"content": "late"}}]}\n\n'


def minimax_cases():
    yield 'minimax_think_split', sse(
        delta(content='<thi'), delta(content='nk>reason'), delta(content='ing</th'),
        delta(content='ink>Answer <'), delta(content='b>bold</b>'),
        delta('stop', usage(40, 30, completion_tokens_details={'reasoning_tokens': 7})), done=True)
    yield 'minimax_think_whole_reasoning_tokens', sse(
        delta(content='<think>plan</think>Done.'),
        delta('stop', usage(12, 8, completion_tokens_details={'reasoning_tokens': 5})), done=True)
    yield 'minimax_unclosed_think', sse(delta(content='pre<think>still thinking'), delta('stop'), done=True)
    yield 'minimax_trailing_angle', sse(delta(content='a<think>t</think>value x <'), delta('stop', usage(1, 1)), done=True)


def a_start(usage_fields):
    return {'type': 'message_start', 'message': {'id': 'msg_1', 'type': 'message', 'role': 'assistant',
                                                  'model': CLAUDE, 'content': [], 'usage': usage_fields}}


def a_block(index, block):
    return {'type': 'content_block_start', 'index': index, 'content_block': block}


def a_delta(index, **fields):
    return {'type': 'content_block_delta', 'index': index, 'delta': fields}


def a_stop(index):
    return {'type': 'content_block_stop', 'index': index}


def a_message_delta(stop, usage_fields=None, **extra):
    event = {'type': 'message_delta', 'delta': {'stop_reason': stop, 'stop_sequence': None, **extra}}
    if usage_fields is not None:
        event['usage'] = usage_fields
    return event


A_END = {'type': 'message_stop'}


def anthropic_cases():
    base_usage = {'input_tokens': 12, 'output_tokens': 1}
    yield 'anthropic_text', named(
        a_start(base_usage), a_block(0, {'type': 'text', 'text': ''}), {'type': 'ping'},
        a_delta(0, type='text_delta', text='Hello'), a_delta(0, type='text_delta', text=' there'),
        a_stop(0), a_message_delta('end_turn', {'output_tokens': 6}), A_END)
    yield 'anthropic_thinking_signature', named(
        a_start(base_usage), a_block(0, {'type': 'thinking', 'thinking': '', 'signature': ''}),
        a_delta(0, type='thinking_delta', thinking='Consider. '), a_delta(0, type='thinking_delta', thinking='Done.'),
        a_delta(0, type='signature_delta', signature='EqQBsig=='), a_stop(0),
        a_block(1, {'type': 'text', 'text': ''}), a_delta(1, type='text_delta', text='Answer'), a_stop(1),
        a_message_delta('end_turn', {'output_tokens': 30}), A_END)
    yield 'anthropic_tool_use', named(
        a_start(base_usage), a_block(0, {'type': 'text', 'text': 'Let me run it.'}), a_stop(0),
        a_block(1, {'type': 'tool_use', 'id': 'toolu_1', 'name': 'run_command', 'input': {}}),
        a_delta(1, type='input_json_delta', partial_json=''), a_delta(1, type='input_json_delta', partial_json='{"comm'),
        a_delta(1, type='input_json_delta', partial_json='and": "ls -la"}'), a_stop(1),
        a_block(2, {'type': 'tool_use', 'id': 'toolu_2', 'name': 'read_files', 'input': {'path': 'a.txt'}}), a_stop(2),
        a_message_delta('tool_use', {'output_tokens': 44}), A_END)
    yield 'anthropic_cache_usage', named(
        a_start({'input_tokens': 30, 'cache_creation_input_tokens': 200, 'cache_read_input_tokens': 5000,
                 'output_tokens': 1, 'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 200}}),
        a_block(0, {'type': 'text', 'text': ''}), a_delta(0, type='text_delta', text='ok'), a_stop(0),
        a_message_delta('end_turn', {'output_tokens': 7, 'cache_read_input_tokens': None}), A_END)
    yield 'anthropic_refusal', named(
        a_start(base_usage), a_block(0, {'type': 'text', 'text': ''}), a_delta(0, type='text_delta', text='I cannot'), a_stop(0),
        a_message_delta('refusal', {'output_tokens': 3}, stop_details={'type': 'refusal', 'category': 'cyber', 'explanation': '  Declined for safety.  '}), A_END)
    yield 'anthropic_refusal_without_details', named(
        a_start(base_usage), a_message_delta('refusal', {'output_tokens': 0}), A_END)
    yield 'anthropic_error_overloaded', named(
        a_start(base_usage), {'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}})
    yield 'anthropic_error_rate_limit', named(
        {'type': 'error', 'error': {'type': 'rate_limit_error', 'message': 'Number of request tokens has exceeded your per-minute rate limit'}})
    yield 'anthropic_error_untyped', named({'type': 'error', 'error': {'message': 7}})
    yield 'anthropic_max_tokens', named(
        a_start(base_usage), a_block(0, {'type': 'text', 'text': ''}), a_delta(0, type='text_delta', text='trunc'),
        a_message_delta('max_tokens', {'output_tokens': 4096}), A_END)
    yield 'anthropic_stop_without_delta', named(a_start(base_usage), a_block(0, {'type': 'text', 'text': 'x'}), a_stop(0), A_END)
    yield 'anthropic_incomplete_tool_json', named(
        a_start(base_usage), a_block(0, {'type': 'tool_use', 'id': 'toolu_x', 'name': 'run_command', 'input': {}}),
        a_delta(0, type='input_json_delta', partial_json='{"command": "l'), a_message_delta('tool_use'), A_END)
    yield 'anthropic_delta_before_start', named(a_start(base_usage), a_delta(3, type='text_delta', text='gateway text'),
                                                a_message_delta('end_turn', {'output_tokens': 2}), A_END)
    yield 'anthropic_replayed_tool_start', named(
        a_start(base_usage), a_block(0, {'type': 'tool_use', 'id': 'toolu_r', 'name': 'run_command', 'input': {}}),
        a_block(0, {'type': 'tool_use', 'id': 'toolu_r', 'name': 'run_command', 'input': {}}),
        a_delta(0, type='input_json_delta', partial_json='{}'), a_stop(0), a_message_delta('tool_use', {'output_tokens': 5}), A_END)
    yield 'anthropic_malformed_frame', named(a_start(base_usage)) + 'event: content_block_delta\ndata: {not json\n\n' + named(
        a_block(0, {'type': 'text', 'text': ''}), a_delta(0, type='text_delta', text='y'), a_message_delta('end_turn'), A_END)
    yield 'anthropic_crlf_split_done_trailer', named(
        a_start(base_usage), a_block(0, {'type': 'text', 'text': ''}), a_delta(0, type='text_delta', text='z'),
        a_message_delta('end_turn', {'output_tokens': 1}), A_END).replace('\n', '\r\n') + 'data: [DO'


def r_event(type_, **fields):
    return {'type': type_, **fields}


def r_message(text, item_id='msg_1'):
    return {'type': 'message', 'id': item_id, 'status': 'completed', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}


def r_call(arguments, call_id='call_1', item_id='fc_1', name='run_command'):
    return {'type': 'function_call', 'id': item_id, 'call_id': call_id, 'name': name, 'arguments': arguments, 'status': 'completed'}


def r_usage(inp, out, cached=None):
    value = {'input_tokens': inp, 'output_tokens': out, 'total_tokens': inp + out}
    if cached is not None:
        value['input_tokens_details'] = {'cached_tokens': cached}
        value['output_tokens_details'] = {'reasoning_tokens': 0}
    return value


def responses_cases():
    created = r_event('response.created', response={'id': 'resp_1', 'status': 'in_progress', 'output': []})
    yield 'responses_output_text', named(
        created, r_event('response.output_item.added', output_index=0, item={'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'content': []}),
        r_event('response.output_text.delta', output_index=0, item_id='msg_1', delta='Hello'),
        r_event('response.output_text.delta', output_index=0, item_id='msg_1', delta=' world'),
        r_event('response.output_text.done', output_index=0, text='Hello world'),
        r_event('response.output_item.done', output_index=0, item=r_message('Hello world')),
        r_event('response.completed', response={'id': 'resp_1', 'output': [r_message('Hello world')], 'usage': r_usage(1000, 20, cached=600)}))
    yield 'responses_terminal_text_suffix', named(
        r_event('response.output_text.delta', output_index=0, delta='Hel'),
        r_event('response.completed', response={'id': 'resp_2', 'output': [r_message('Hello!')], 'usage': r_usage(5, 2)}))
    yield 'responses_function_call', named(
        created, r_event('response.output_item.added', output_index=0, item=r_call('')),
        r_event('response.function_call_arguments.delta', output_index=0, item_id='fc_1', delta='{"command"'),
        r_event('response.function_call_arguments.delta', output_index=0, item_id='fc_1', delta=': "ls"'),
        r_event('response.function_call_arguments.done', output_index=0, item_id='fc_1', arguments='{"command": "ls"}'),
        r_event('response.output_item.done', output_index=0, item=r_call('{"command": "ls"}')),
        r_event('response.output_item.added', output_index=1, item=r_call('', call_id='call_2', item_id='fc_2', name='read_files')),
        r_event('response.output_item.done', output_index=1, item=r_call('{"path": "b"}', call_id='call_2', item_id='fc_2', name='read_files')),
        r_event('response.completed', response={'id': 'resp_3', 'output': [r_call('{"command": "ls"}'), r_call('{"path": "b"}', call_id='call_2', item_id='fc_2', name='read_files')],
                                                'usage': r_usage(300, 40, cached=256)}))
    yield 'responses_incomplete_content_filter', named(
        r_event('response.output_text.delta', output_index=0, delta='partial'),
        r_event('response.incomplete', response={'id': 'resp_4', 'output': [], 'incomplete_details': {'reason': 'content_filter'}}))
    yield 'responses_incomplete_max_output_tokens', named(
        r_event('response.output_text.delta', output_index=0, delta='long answer'),
        r_event('response.incomplete', response={'id': 'resp_5', 'output': [r_message('long answer')], 'incomplete_details': {'reason': 'max_output_tokens'},
                                                 'usage': r_usage(10, 128000)}))
    yield 'responses_failed_rate_limit', named(
        created, r_event('response.failed', response={'id': 'resp_6', 'status': 'failed',
                                                      'error': {'code': 'rate_limit_exceeded', 'message': 'Rate limit reached for gpt-5.5'}}))
    yield 'responses_error_event_server', named(r_event('error', code='server_error', message='The server had an error'))
    yield 'responses_unknown_and_ignored_events', named(
        r_event('response.in_progress', response={}), r_event('keepalive'), r_event('response.web_search_call.searching', output_index=0),
        r_event('response.brand_new_event'), r_event('response.output_text.delta', output_index=0, delta='ok'),
        r_event('response.completed', response={'id': 'r', 'output': None}))
    yield 'responses_cut_stream', named(r_event('response.output_text.delta', output_index=0, delta='half'))
    yield 'responses_unknown_call_route', named(
        r_event('response.function_call_arguments.delta', item_id='fc_9', delta='{}'))
    yield 'responses_terminal_text_contradiction', named(
        r_event('response.output_text.delta', output_index=0, delta='abc'),
        r_event('response.completed', response={'id': 'r', 'output': [r_message('xyz')]}))


def openai_error_branch_cases():
    """In-stream errors reaching subscription, 400, 5xx and token-limit branches."""
    yield 'openai_error_subscription_resets_in', sse(err(type='usage_limit_reached', message='The usage limit has been reached', resets_in_seconds=1800))
    yield 'openai_error_subscription_capped_cooldown', sse(err(type='usage_limit_reached', message='limit', resets_in_seconds=999999))
    yield 'openai_error_subscription_capacity_no_reset', sse(err(message='Selected model is at capacity. Please try a different model.', code=429))
    yield 'openai_error_subscription_kimi_window', sse(err(message="You've reached your 5-hour usage limit", http_code=429))
    yield 'openai_error_subscription_sse_text', sse(err(message="Too many requests: usage_limit_reached {'resets_in_seconds': ١٢٠.5}"))
    yield 'openai_error_subscription_400', sse(err(message='Selected model is at capacity', code='invalid_request_error'))
    yield 'openai_error_subscription_other_status', sse(err(message='Selected model is at capacity', status=520))
    yield 'openai_error_subscription_gateway_503', sse(err(message="usage_limit_reached {'resets_in_seconds': 30}", http_code=503))
    yield 'openai_error_invalid_encrypted_content_code', sse(err(code='invalid_encrypted_content', message='The encrypted content for item rs_abc could not be verified.', param='input[003].encrypted_content'))
    yield 'openai_error_invalid_encrypted_content_sentence', sse(err(type='<nil>', message='The encrypted content for item rs_1 could not be verified. Reason: rotated key'))
    yield 'openai_error_invalid_encrypted_content_odd_param', sse(err(code='invalid_encrypted_content', message='x', param='messages[0]', http_code=500))
    yield 'openai_error_encrypted_sentence_with_code', sse(err(code='bad_state', message='The encrypted content for item rs_1 could not be verified.'))
    yield 'openai_error_400_quota', sse(err(message='Your credit balance is too low', type='invalid_request_error'))
    yield 'openai_error_400_route_missing', sse(err(message='The model `kimi-k9` does not exist', code='model_not_found', http_code=400))
    yield 'openai_error_400_token_limit', sse(err(message='max_tokens must be at most 32768', type='invalid_request_error'))
    yield 'openai_error_400_token_limit_exclusive', sse(err(message='Invalid max_tokens value: range is from 1 (inclusive) to 16385 (exclusive)', type='invalid_request_error'))
    yield 'openai_error_400_thinking_binding', sse(err(message='messages.3.content.0: Invalid `signature` in `thinking` block. The block is bound to a different conversation', type='invalid_request_error'))
    yield 'openai_error_400_thinking_binding_after_output', sse(delta(content='partial'), err(message='messages.1.content.2:  Invalid `signature` in `thinking` block.The block is bound to a different conversation', type='invalid_request_error'))
    yield 'openai_error_400_unsupported_parameter', sse(err(message="Unsupported parameter: 'temperature' is not supported with this model.", type='invalid_request_error', param='temperature', code='unsupported_parameter'))
    yield 'openai_error_400_extra_inputs', sse(err(message='top_k: Extra inputs are not permitted', type='invalid_request_error'))
    yield 'openai_error_400_anthropic_display_variant', sse(err(message='thinking.enabled.display: Extra inputs are not permitted', type='invalid_request_error'))
    yield 'openai_error_400_two_parameters', sse(err(message="Unsupported parameter: 'top_p'; Unknown parameter: `seed`", type='invalid_request_error'))
    yield 'openai_error_400_nested_parameter', sse(err(message='messages.0.temperature: Extra inputs are not permitted', type='invalid_request_error'))
    yield 'openai_error_400_unsupported_after_reasoning', sse(delta(reasoning_content='hmm'), err(message="'seed' is not supported with this model", http_code='400'))
    yield 'openai_error_400_required_parameter', sse(err(message="Unsupported parameter: 'messages'", code='unsupported_parameter', param='messages', status=400))
    yield 'openai_error_400_image', sse(err(message='Invalid image: could not process image', http_code='400'))
    yield 'openai_error_400_prompt_too_long', sse(err(message='prompt is too long: 250000 tokens > 200000 maximum', type='invalid_request_error'))
    yield 'openai_error_400_stream_only', sse(err(message='This model only support stream mode', status=400))
    yield 'openai_error_400_vendor_transient', sse(err(message='请求失败,请稍后重试', type='invalid_request_error'))
    yield 'openai_error_400_generic', sse(err(message='messages: text content blocks must be non-empty', type='invalid_request_error'))
    yield 'openai_error_400_mojibake_body', sse(err(message='请稍后重试'.encode('utf-8').decode('latin-1'), type='invalid_request_error'))
    yield 'openai_error_5xx_sensitive_words', sse(err(message='API HTTP 500: User sensitive words detected: foo bar (request id: 2026abc)', http_code=500))
    yield 'openai_error_5xx_sensitive_words_unicode_prefix', sse(err(message='API HTTP ٥٠٠:\tuser sensitive words detected: x', status=503))
    yield 'openai_error_5xx_route_unavailable_code', sse(err(code='model_not_found', message='分组 default 下模型 kimi-k3 无可用渠道（distributor） (request id: 20261118abc)', http_code=503))
    yield 'openai_error_5xx_route_unavailable_phrase', sse(err(message='分组 vip 下模型 glm-5 无可用渠道 ( Distributor )（request id：xyz）', status=503))
    yield 'openai_error_404_protocol_unsupported', sse(err(type='not_found_error', message='模型 [deepseek-v4.1-flash-m17] 不支持协议 ANTHROPIC_MESSAGES'))
    yield 'openai_error_404_protocol_unsupported_english', sse(err(message='model X does not support protocol responses', http_code='404'))
    yield 'openai_error_sse_token_limit_bracket', sse(err(message='Range of max_tokens should be [1, 8192]'))
    yield 'openai_error_sse_token_limit_or_equal', sse(err(message='MAX_OUTPUT_TOKENS must be less than or equal to 128000'))
    yield 'openai_error_sse_token_limit_between', sse(err(message='maxOutputTokens value must be between 1 and 65536'))
    yield 'openai_error_sse_token_limit_out_of_range', sse(err(message='max_tokens must be at most 2000000'))
    yield 'openai_error_sse_token_limit_newline', sse(err(message='max_tokens is invalid\nmust be at most 100'))
    yield 'openai_error_sse_token_limit_casefold', sse(err(message='MAX_TOKENſ must be at moſt 100 (İnvalid)'))
    yield 'openai_error_sse_token_limit_unicode_digits', sse(err(message='max_tokens must be less than ٣٢٧٦٩'))
    yield 'openai_error_type_integer', sse(err(message='', type=7))
    yield 'openai_error_type_container', sse(err(message='', type={'a': [1, 2.5, None, True], "it's": 'q"'}))
    yield 'openai_error_type_falsy', sse(err(message='', type=0, http_code=[5, 0, 3]))
    yield 'openai_error_http_code_object', sse(err(message='', http_code={'x': 'y\n'}))
    yield 'openai_error_non_dict_list', sse({'error': ['boom', 2, {'k': 1e-7}]})
    yield 'openai_error_non_dict_float', sse({'error': 1.5e16})
    yield 'openai_error_non_dict_false', sse({'error': False})
    yield 'openai_error_http_code_float', sse(err(message='server failure', http_code=503.0))
    yield 'openai_error_arabic_http_code', sse(err(message='temporarily unavailable', http_code='٥٠٣'))
    yield 'openai_error_arabic_http_code_server_error', sse(err(message='upstream failure', type='server_error', http_code='٥٠٣'))
    yield 'openai_error_superscript_http_code', sse(err(message='temporarily unavailable', http_code='²'))
    yield 'openai_error_mixed_digit_http_code', sse(err(message='temporarily unavailable', http_code='5٠⁴'))
    yield 'openai_error_arabic_status_text', sse(err(message='temporarily unavailable, status code: ٥٠٢ from upstream'))
    yield 'openai_error_arabic_status_text_not_retryable', sse(err(message='bad gateway, status code: ٥٠٢'))
    yield 'openai_error_vendor_escaped_upstream_status', sse(err(message='denied "upstreamStatus":٤٠٠', status=403, ext={'source': 'UPSTREAM_VENDOR'}))
    yield 'openai_error_vendor_veto_upstream_status', sse(err(message='denied', status=403, ext={'source': 'UPSTREAM_VENDOR', 'upstreamStatus': 400}))
    yield 'openai_error_wrapped_overload_long_s', sse(err(message='gateway: ſtatus_code: 529', status=500))
    yield 'openai_error_subscription_past_resets_at', sse(err(type='usage_limit_reached', message='The usage limit has been reached', resets_at=1700000000))
    yield 'openai_tool_index_unicode_digit', sse(delta(tool_calls=[tc(' ١ ', 'call_u1', 'read_files', '{}')]), delta('tool_calls'), done=True)
    yield 'openai_tool_index_padded', sse(delta(tool_calls=[tc(' 0007 ', 'call_p', 'read_files', '{}')]), delta('tool_calls'), done=True)
    yield 'openai_tool_index_superscript', sse(delta(tool_calls=[tc('²', 'call_s2', 'read_files', '{}')]), delta('tool_calls'), done=True)
    yield 'openai_tool_index_unicode_zeros', sse(delta(tool_calls=[tc('٠٠٠٠٠', 'call_z', 'read_files', '{}')]), delta('tool_calls'), done=True)


def chat_reasoning_cases():
    """Reasoning wire families on the OpenAI Chat face, as (model, name, sse)."""
    yield 'aws.claude-opus-4.6', 'chat_claude_reasoning_details_with_tool', sse(
        delta(role='assistant', content=''),
        delta(reasoning_content='Check the file. ', reasoning_details=[{'type': 'thinking', 'thinking': 'Check the file. '}]),
        delta(reasoning_details=[{'type': 'thinking', 'signature': 'ErUBCkYIBxgCKkA='}]),
        delta(tool_calls=[tc(0, 'toolu_bdrk_1', 'read_files', '{"path": "a"}')]),
        delta('tool_calls', usage(500, 60, completion_tokens_details={'reasoning_tokens': 12})), done=True)
    yield 'aws.claude-opus-4.6', 'chat_claude_flat_thinking_signature', sse(
        delta(thinking='Plan.'), delta(thinking_signature='sig-a'), delta(thinking_signature='sig-b'),
        delta(content='Done.'), delta('stop', usage(10, 5)), done=True)
    yield 'deepseek-v4', 'chat_deepseek_reasoning_content', sse(
        delta(role='assistant', content=None, reasoning_content=''),
        delta(content=None, reasoning_content='First, '), delta(content=None, reasoning_content='compute.'),
        delta(content='42', reasoning_content=None),
        delta('stop', usage(64, 20, prompt_cache_hit_tokens=32, prompt_cache_miss_tokens=32, completion_tokens_details={'reasoning_tokens': 18})), done=True)
    yield 'glm-5', 'chat_glm_reasoning_content', sse(
        delta(role='assistant', reasoning_content='分析问题。'), delta(content='答案'),
        delta('stop', usage(30, 10, prompt_tokens_details={'cached_tokens': 16})), done=True)
    yield 'qwen3-max', 'chat_qwen_enable_thinking', sse(
        delta(role='assistant', content='', reasoning_content='Think'), delta(content='', reasoning_content=' more'),
        delta(content='Result'), delta('stop'), {'choices': [], 'usage': usage(40, 12, completion_tokens_details={'reasoning_tokens': 8})}, done=True)
    yield 'gemini-3-pro-preview', 'chat_gemini_thought_signatures', sse(
        delta(role='assistant', reasoning_content='**Plan**'),
        delta(tool_calls=[tc(0, 'call_g1', 'read_files', '{"path": "a"}', extra_content={'google': {'thought_signature': 'CiQB-sig-1'}})]),
        delta(tool_calls=[tc(1, 'call_g2', 'run_command', '{"command": "ls"}')]),
        delta(tool_calls=[tc(1, extra_content={'google': {'thought_signature': 'CiQB-sig-2'}})]),
        delta('tool_calls', usage(80, 30)), done=True)
    yield 'MiniMax-M2.5', 'chat_minimax_reasoning_split', sse(
        delta(role='assistant', reasoning_details=[{'type': 'reasoning.text', 'id': 'r1', 'format': 'MiniMax-response-v1', 'index': 0, 'text': 'Split '}]),
        delta(reasoning_details=[{'type': 'reasoning.text', 'id': 'r1', 'format': 'MiniMax-response-v1', 'index': 0, 'text': 'reasoning.'}]),
        delta(content='Answer.'), delta('stop', usage(20, 9, completion_tokens_details={'reasoning_tokens': 6})), done=True)
    minimax_thinking = (delta(content='<think>t</think>x'),)
    for name, details in (('float', {'reasoning_tokens': 7.5}), ('bool', {'reasoning_tokens': True}),
                          ('zero_float', {'reasoning_tokens': 0.0}), ('string', {'reasoning_tokens': '7'}),
                          ('null', {'reasoning_tokens': None}), ('list', {'reasoning_tokens': [1]})):
        yield 'MiniMax-M2.5', f'chat_minimax_reasoning_tokens_{name}', sse(
            *minimax_thinking, delta('stop', usage(5, 5, completion_tokens_details=details)), done=True)
    # Malformed detail containers remain raw usage, never abort completion.
    for name, details in (('list', [1]), ('null', None), ('string', 'x'), ('integer', 7)):
        yield 'MiniMax-M2.5', f'chat_minimax_completion_details_{name}', sse(
            *minimax_thinking, delta('stop', usage(5, 5, completion_tokens_details=details)), done=True)


def r_reasoning(item_id, summary=None, encrypted='gAAAAB-opaque', **extra):
    item = {'type': 'reasoning', 'id': item_id, 'summary': summary if summary is not None else []}
    if encrypted is not None:
        item['encrypted_content'] = encrypted
    item.update(extra)
    return item


def r_summary(*texts):
    return [{'type': 'summary_text', 'text': text} for text in texts]


def responses_reasoning_cases():
    created = r_event('response.created', response={'id': 'resp_r', 'status': 'in_progress', 'output': []})
    message_events = (
        r_event('response.output_item.added', output_index=1, item={'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'content': []}),
        r_event('response.output_text.delta', output_index=1, item_id='msg_1', delta='Answer'))
    yield 'responses_reasoning_summary_stream', named(
        created, r_event('response.output_item.added', output_index=0, item=r_reasoning('rs_1', encrypted=None)),
        r_event('response.reasoning_summary_part.added', item_id='rs_1', output_index=0, summary_index=0, part={'type': 'summary_text', 'text': ''}),
        r_event('response.reasoning_summary_text.delta', item_id='rs_1', output_index=0, summary_index=0, delta='Thinking '),
        r_event('response.reasoning_summary_text.delta', item_id='rs_1', output_index=0, summary_index=0, delta='hard.'),
        r_event('response.reasoning_summary_text.done', item_id='rs_1', output_index=0, summary_index=0, text='Thinking hard.'),
        r_event('response.reasoning_summary_part.done', item_id='rs_1', output_index=0, summary_index=0, part={'type': 'summary_text', 'text': 'Thinking hard.'}),
        r_event('response.reasoning_summary_part.added', item_id='rs_1', output_index=0, summary_index=1, part={'type': 'summary_text', 'text': ''}),
        r_event('response.reasoning_summary_text.delta', item_id='rs_1', output_index=0, summary_index=1, delta='Second.'),
        r_event('response.output_item.done', output_index=0, item=r_reasoning('rs_1', r_summary('Thinking hard.', 'Second.'))),
        *message_events,
        r_event('response.completed', response={'id': 'resp_r', 'output': [r_reasoning('rs_1', r_summary('Thinking hard.', 'Second.')), r_message('Answer')],
                                                'usage': {**r_usage(900, 300, cached=512), 'output_tokens_details': {'reasoning_tokens': 256}}}))
    yield 'responses_reasoning_text_without_ids', named(
        r_event('response.reasoning_text.delta', delta='a'), r_event('response.reasoning_text.delta', delta='b'),
        r_event('response.reasoning_text.done', text='ab'), r_event('response.output_text.delta', output_index=0, delta='ok'),
        r_event('response.completed', response={'id': 'r', 'output': [r_message('ok')], 'usage': r_usage(10, 4)}))
    yield 'responses_reasoning_terminal_only', named(
        *message_events,
        r_event('response.completed', response={'id': 'r', 'output': [r_reasoning('rs_t', r_summary('Only at the end.')), r_message('Answer', 'msg_1')]}))
    yield 'responses_reasoning_snapshot_suffix', named(
        r_event('response.output_item.added', output_index=0, item=r_reasoning('rs_s', encrypted=None)),
        r_event('response.reasoning_summary_text.delta', item_id='rs_s', output_index=0, delta='Hel'),
        r_event('response.output_item.done', output_index=0, item=r_reasoning('rs_s', r_summary('Hello'))),
        r_event('response.completed', response={'id': 'r', 'output': [r_reasoning('rs_s', r_summary('Hello'))]}))
    yield 'responses_reasoning_contradiction', named(
        r_event('response.reasoning_summary_text.delta', item_id='rs_c', output_index=0, delta='abc'),
        r_event('response.reasoning_summary_text.done', item_id='rs_c', output_index=0, text='xyz'))
    yield 'responses_reasoning_encrypted_only', named(
        r_event('response.output_item.added', output_index=0, item=r_reasoning('rs_e')),
        r_event('response.output_item.done', output_index=0, item=r_reasoning('rs_e')),
        *message_events,
        r_event('response.completed', response={'id': 'r', 'output': [r_reasoning('rs_e'), r_message('Answer')], 'usage': r_usage(5, 50)}))
    yield 'responses_reasoning_two_items_buffered', named(
        r_event('response.output_item.added', output_index=0, item=r_reasoning('rs_a', encrypted=None)),
        r_event('response.reasoning_summary_text.delta', item_id='rs_a', output_index=0, delta='A1'),
        r_event('response.output_item.added', output_index=1, item=r_reasoning('rs_b', encrypted=None)),
        r_event('response.reasoning_summary_text.delta', item_id='rs_b', output_index=1, delta='B1'),
        r_event('response.output_item.done', output_index=0, item=r_reasoning('rs_a', r_summary('A1 tail'))),
        r_event('response.completed', response={'id': 'r', 'output': [r_reasoning('rs_a', r_summary('A1 tail')), r_reasoning('rs_b', r_summary('B1'))]}))
    yield 'responses_reasoning_unindexed_parts', named(
        r_event('response.reasoning_summary_part.added', part={'type': 'summary_text', 'text': ''}),
        r_event('response.reasoning_summary_text.delta', delta='x'),
        r_event('response.reasoning_summary_part.added', part={'type': 'summary_text', 'text': ''}),
        r_event('response.reasoning_summary_text.delta', delta='y'),
        r_event('response.completed', response={'id': 'r', 'output': []}))
    yield 'responses_reasoning_content_channel_item', named(
        r_event('response.output_item.added', output_index=0, item={'type': 'reasoning', 'id': 'rs_k', 'summary': [], 'content': []}),
        r_event('response.reasoning_text.delta', item_id='rs_k', output_index=0, content_index=0, delta='raw '),
        r_event('response.reasoning_summary_text.delta', item_id='rs_k', output_index=0, summary_index=0, delta='sum'),
        r_event('response.output_item.done', output_index=0, item={'type': 'reasoning', 'id': 'rs_k', 'summary': r_summary('sum'), 'content': [{'type': 'reasoning_text', 'text': 'raw text'}]}),
        r_event('response.completed', response={'id': 'r', 'output': []}))
    yield 'responses_reasoning_invalid_output_index', named(
        r_event('response.reasoning_summary_text.delta', output_index='x', delta='a'))
    yield 'responses_reasoning_part_not_object', named(
        r_event('response.reasoning_summary_part.done', output_index=0, part='text'))
    yield 'responses_reasoning_item_bad_summary', named(
        r_event('response.output_item.added', output_index=0, item=r_reasoning('rs_b', summary={'text': 'x'})))
    yield 'responses_reasoning_omitted_part', named(
        r_event('response.reasoning_summary_text.delta', item_id='rs_o', output_index=0, summary_index=1, delta='late'),
        r_event('response.completed', response={'id': 'r', 'output': [r_reasoning('rs_o', r_summary('only first'))]}))
    yield 'responses_reasoning_incomplete_length', named(
        r_event('response.reasoning_summary_text.delta', item_id='rs_l', output_index=0, delta='partial thought'),
        r_event('response.incomplete', response={'id': 'r', 'output': [r_reasoning('rs_l')],
                                                 'incomplete_details': {'reason': 'max_output_tokens'}, 'usage': r_usage(10, 4096)}))


def responses_tool_and_state_cases():
    caller = {'type': 'program', 'program_id': 'prog_1'}
    yield 'responses_function_call_caller', named(
        r_event('response.output_item.added', output_index=0, item={**r_call(''), 'caller': caller}),
        r_event('response.function_call_arguments.delta', output_index=0, item_id='fc_1', delta='{"command": "ls"}'),
        r_event('response.output_item.done', output_index=0, item={**r_call('{"command": "ls"}'), 'caller': caller}),
        r_event('response.completed', response={'id': 'r', 'output': [{**r_call('{"command": "ls"}'), 'caller': caller}], 'usage': r_usage(30, 8)}))
    yield 'responses_function_call_caller_changed', named(
        r_event('response.output_item.added', output_index=0, item={**r_call('{}'), 'caller': caller}),
        r_event('response.output_item.done', output_index=0, item={**r_call('{}'), 'caller': {'type': 'program', 'program_id': 'other'}}))
    yield 'responses_function_call_caller_appears', named(
        r_event('response.output_item.added', output_index=0, item={**r_call('{}'), 'caller': None}),
        r_event('response.completed', response={'id': 'r', 'output': [{**r_call('{}'), 'caller': None}]}))
    yield 'responses_function_call_caller_python_equal', named(
        r_event('response.output_item.added', output_index=0, item={**r_call('{}'), 'caller': {'type': 'program', 'depth': 1}}),
        r_event('response.completed', response={'id': 'r', 'output': [{**r_call('{}'), 'caller': {'type': 'program', 'depth': True}}]}))
    yield 'responses_function_call_caller_scalar', named(
        r_event('response.output_item.added', output_index=0, item={**r_call('{}'), 'caller': 'program'}),
        r_event('response.completed', response={'id': 'r', 'output': [{**r_call('{}'), 'caller': 'program'}]}))
    program = {'type': 'program', 'id': 'prog_1', 'code': 'print(1)'}
    program_output = {'type': 'program_output', 'id': 'po_1', 'program_id': 'prog_1', 'output': '1'}
    yield 'responses_program_output_followup', named(
        r_event('response.output_item.done', output_index=0, item=program),
        r_event('response.output_item.done', output_index=1, item=program_output),
        r_event('response.completed', response={'id': 'r', 'output': [program, program_output]}))
    yield 'responses_program_output_with_usage', named(
        r_event('response.completed', response={'id': 'r', 'output': [program, program_output], 'usage': r_usage(12, 3)}))
    yield 'responses_program_output_with_message', named(
        r_event('response.output_text.delta', output_index=2, delta='done'),
        r_event('response.completed', response={'id': 'r', 'output': [program, program_output, r_message('done')], 'usage': r_usage(12, 3)}))
    yield 'responses_failed_invalid_encrypted_content', named(
        r_event('response.failed', response={'id': 'r', 'status': 'failed', 'error': {
            'code': 'invalid_encrypted_content', 'message': 'The encrypted content for item rs_1 could not be verified.', 'param': 'input[2].encrypted_content'}}))
    yield 'responses_error_invalid_encrypted_content_rewrapped', named(
        {'type': 'error', 'error': {'type': '<nil>', 'message': 'The encrypted content for item rs_9 could not be verified.'}})
    yield 'responses_error_invalid_encrypted_content_flat', named(
        r_event('error', code=None, message='The encrypted content for item rs_7 could not be verified.', param='input.4'))
    root_commentary = {'type': 'message', 'id': 'msg_c', 'role': 'assistant', 'agent': {'agent_name': '/root'}, 'phase': 'commentary',
                       'content': [{'type': 'output_text', 'text': 'thinking aloud'}]}
    root_final = {'type': 'message', 'id': 'msg_f', 'role': 'assistant', 'agent': {'agent_name': '/root'}, 'phase': 'final_answer',
                  'content': [{'type': 'output_text', 'text': 'Final.'}]}
    worker_call = {**r_call('{"path": "w"}', call_id='call_w', item_id='fc_w', name='read_files'), 'agent': {'agent_name': '/worker'}}
    yield 'responses_multi_agent_attribution', named(
        r_event('response.output_item.added', output_index=0, item={**root_commentary, 'content': []}),
        r_event('response.output_text.delta', output_index=0, delta='thinking aloud'),
        r_event('response.output_text.delta', output_index=3, agent={'agent_name': '/worker'}, delta='worker chatter'),
        r_event('response.output_item.added', output_index=1, item=worker_call),
        r_event('response.output_item.added', output_index=2, item={'type': 'function_call', 'id': 'fc_r', 'call_id': 'call_r', 'name': 'run_command', 'arguments': '{}'},
                agent={'agent_name': '/root'}),
        r_event('response.output_item.added', output_index=4, item={**root_final, 'content': []}),
        r_event('response.output_text.delta', output_index=4, agent={'agent_name': '/root'}, delta='Final.'),
        r_event('response.completed', response={'id': 'r', 'output': [root_commentary, worker_call, root_final], 'usage': r_usage(70, 20)}))
    yield 'responses_multi_agent_bad_text_agent', named(
        r_event('response.output_text.delta', output_index=0, agent='root', delta='x'))
    yield 'responses_multi_agent_bad_item_agent', named(
        r_event('response.output_item.added', output_index=0, item={'type': 'message', 'id': 'm', 'agent': {'agent_name': ''}}))
    yield 'responses_multi_agent_bad_call_agent', named(
        r_event('response.output_item.added', output_index=0, item=r_call('{}'), agent=['x']))


def anthropic_block_cases():
    base_usage = {'input_tokens': 12, 'output_tokens': 1}
    yield 'anthropic_redacted_thinking', named(
        a_start(base_usage), a_block(0, {'type': 'thinking', 'thinking': '', 'signature': ''}),
        a_delta(0, type='thinking_delta', thinking='Visible part.'), a_delta(0, type='signature_delta', signature='sigA'), a_stop(0),
        a_block(1, {'type': 'redacted_thinking', 'data': 'EmwKAhgBEgy3va3pzix/LafPsn4aDFIT'}), a_stop(1),
        a_block(2, {'type': 'text', 'text': ''}), a_delta(2, type='text_delta', text='Done'), a_stop(2),
        a_message_delta('end_turn', {'output_tokens': 40}), A_END)
    yield 'anthropic_interleaved_thinking_tools', named(
        a_start(base_usage), a_block(0, {'type': 'thinking', 'thinking': 'Start. ', 'signature': 'init-'}),
        a_delta(0, type='signature_delta', signature='sig1'), a_stop(0),
        a_block(1, {'type': 'tool_use', 'id': 'toolu_a', 'name': 'read_files', 'input': {'path': 'x'}}), a_stop(1),
        a_block(2, {'type': 'thinking', 'thinking': ''}), a_delta(2, type='thinking_delta', thinking='Then.'),
        a_delta(2, type='signature_delta', signature=None), a_stop(2),
        a_message_delta('tool_use', {'output_tokens': 70}), A_END)
    yield 'anthropic_signature_without_start', named(
        a_start(base_usage), a_delta(0, type='thinking_delta', thinking='gw'), a_delta(0, type='signature_delta', signature='s'),
        a_message_delta('end_turn', {'output_tokens': 2}), A_END)
    yield 'anthropic_server_tool_use', named(
        a_start(base_usage), a_block(0, {'type': 'server_tool_use', 'id': 'srvtoolu_1', 'name': 'tool_search_tool_regex', 'input': {}}),
        a_delta(0, type='input_json_delta', partial_json='{"pattern": '), a_delta(0, type='input_json_delta', partial_json='"read"}'), a_stop(0),
        a_block(1, {'type': 'tool_search_tool_result', 'tool_use_id': 'srvtoolu_1', 'content': {'type': 'tool_search_tool_search_result', 'tool_references': [{'type': 'tool_reference', 'tool_name': 'read_files'}]}}), a_stop(1),
        a_block(2, {'type': 'tool_use', 'id': 'toolu_c', 'name': 'read_files', 'input': {}}),
        a_delta(2, type='input_json_delta', partial_json='{"path": "r"}'), a_stop(2),
        a_message_delta('tool_use', {'output_tokens': 33}), A_END)
    yield 'anthropic_server_tool_use_initial_input', named(
        a_start(base_usage), a_block(0, {'type': 'server_tool_use', 'id': 'srvtoolu_2', 'name': 'web_search', 'input': {'query': 'x'}}),
        a_delta(0, type='input_json_delta', partial_json='{}'))
    yield 'anthropic_server_tool_use_truncated_json', named(
        a_start(base_usage), a_block(0, {'type': 'server_tool_use', 'id': 'srvtoolu_3', 'name': 'web_search', 'input': {}}),
        a_delta(0, type='input_json_delta', partial_json='{"query'), a_stop(0))
    yield 'anthropic_server_tool_use_bad_input', named(
        a_start(base_usage), a_block(0, {'type': 'server_tool_use', 'id': 'srvtoolu_4', 'name': 'web_search', 'input': 'q'}))
    yield 'anthropic_server_tool_use_unclosed_at_stop', named(
        a_start(base_usage), a_block(0, {'type': 'server_tool_use', 'id': 'srvtoolu_5', 'name': 'web_search', 'input': {}}),
        a_delta(0, type='input_json_delta', partial_json='[1]'), a_message_delta('end_turn'), A_END)
    yield 'anthropic_compaction', named(
        a_start(base_usage), a_block(0, {'type': 'compaction', 'content': None}),
        a_delta(0, type='compaction_delta', content='Summary of earlier turns.'), a_stop(0),
        a_block(1, {'type': 'text', 'text': ''}), a_delta(1, type='text_delta', text='Continuing.'), a_stop(1),
        a_message_delta('end_turn', {'output_tokens': 9}), A_END)
    yield 'anthropic_compaction_bad_content', named(a_start(base_usage), a_block(0, {'type': 'compaction', 'content': 5}))
    yield 'anthropic_compaction_delta_wrong_block', named(
        a_start(base_usage), a_block(0, {'type': 'text', 'text': ''}), a_delta(0, type='compaction_delta', content='x'))
    yield 'anthropic_error_thinking_binding', named(
        a_start(base_usage), {'type': 'error', 'error': {'type': 'invalid_request_error',
                                                         'message': 'messages.1.content.0: Invalid `signature` in `thinking` block. The block is bound to a different conversation'}})
    yield 'anthropic_error_token_limit', named(
        {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'max_tokens: 200000 > 128000, which is the maximum allowed number of output tokens for claude-opus-5; max_tokens must be at most 128000'}})


CASES = (
    [('openai', CHAT, name, raw) for name, raw in openai_cases()]
    + [('openai', 'MiniMax-M2.5', name, raw) for name, raw in minimax_cases()]
    + [('anthropic', CLAUDE, name, raw) for name, raw in anthropic_cases()]
    + [('responses', GPT, name, raw) for name, raw in responses_cases()]
    + [('openai', CHAT, name, raw) for name, raw in openai_error_branch_cases()]
    + [('openai', model, name, raw) for model, name, raw in chat_reasoning_cases()]
    + [('responses', GPT, name, raw) for name, raw in responses_reasoning_cases()]
    + [('responses', GPT, name, raw) for name, raw in responses_tool_and_state_cases()]
    + [('anthropic', CLAUDE, name, raw) for name, raw in anthropic_block_cases()]
)


def render():
    cases = [{'name': name, 'protocol': protocol, 'model': model, 'sse': raw,
              'expected': project(protocol, model, raw)}
             for protocol, model, name, raw in CASES]
    return json.dumps({'generator': 'packages/tofu-core-rs/scripts/gen_stream_golden.py',
                       'trace_id': TRACE_ID, 'max_tokens': MAX_TOKENS, 'cases': cases},
                      indent=1, ensure_ascii=False) + '\n'


def main():
    text = render()
    if '--check' in sys.argv[1:]:
        if not OUT.exists() or OUT.read_text(encoding='utf-8') != text:
            print(f'{OUT} is stale; rerun without --check', file=sys.stderr)
            return 1
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
