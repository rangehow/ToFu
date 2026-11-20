"""Responses WebSocket framing, continuation, Codex side channels, fallback."""

from __future__ import annotations

from lib.oauth.codex import CodexAccessTokenSnapshot

import hashlib
import json
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_CODEX_BASE = 'https://chatgpt.com/backend-api/codex'


def _install_connection(ws, monkeypatch, *connections, handshakes=None):
    pending = list(connections)

    def open_session(plan, key, handshake, owner_scope=''):
        if handshakes is not None:
            handshakes.append(dict(handshake))
        session = ws._Session(connection=pending.pop(0),
                              state_key=plan.responses_state_key)
        ws._sessions()[key] = session
        return session
    monkeypatch.setattr(ws, '_open_session', open_session)


class _ScriptedConnection:
    """Replays one scripted event list per ``response.create`` frame."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.sent = []
        self.raw_sent = []
        self.events = []
        self.closed = False

    def send(self, payload):
        self.raw_sent.append(payload)
        self.sent.append(json.loads(payload))
        self.events = list(self.scripts.pop(0))

    def recv(self, timeout=None):
        if self.events:
            return json.dumps(self.events.pop(0))
        raise TimeoutError

    def close(self):
        self.closed = True


_CALL = {'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1',
         'name': 'read_files', 'arguments': '{"paths":["README.md"]}'}


def _tool_call_round(response_id='resp_1', *, reasoning=None):
    output = ([reasoning] if reasoning else []) + [dict(_CALL, status='completed')]
    events = []
    if reasoning:
        events += [
            {'type': 'response.output_item.added', 'output_index': 0,
             'item': {'type': 'reasoning', 'id': reasoning['id'], 'summary': []}},
            {'type': 'response.output_item.done', 'output_index': 0,
             'item': reasoning},
        ]
    index = len(output) - 1
    events += [
        {'type': 'response.output_item.added', 'output_index': index,
         'item': dict(_CALL, arguments='')},
        {'type': 'response.function_call_arguments.delta', 'item_id': 'fc_1',
         'output_index': index, 'delta': _CALL['arguments']},
        {'type': 'response.output_item.done', 'output_index': index,
         'item': dict(_CALL, status='completed')},
        {'type': 'response.completed', 'response': {
            'id': response_id, 'status': 'completed', 'output': output,
            'usage': {'input_tokens': 10, 'output_tokens': 2}}},
    ]
    return events


def _text_round(text='done', response_id='resp_2'):
    return [
        {'type': 'response.output_text.delta', 'delta': text},
        {'type': 'response.completed', 'response': {
            'id': response_id, 'status': 'completed',
            'output': [{'type': 'message', 'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': text}]}],
            'usage': {'input_tokens': 4, 'output_tokens': 1}}},
    ]


def _canonical_body(*, with_tool_output: bool):
    messages = [{'role': 'user', 'content': 'read the file'}]
    if with_tool_output:
        messages.extend([
            {'role': 'assistant', 'tool_calls': [{
                'id': 'call_1', 'type': 'function',
                'function': {'name': 'read_files',
                             'arguments': '{"paths":["README.md"]}'},
            }]},
            {'role': 'tool', 'tool_call_id': 'call_1',
             'content': 'README contents'},
        ])
    return {
        'model': 'gpt-5.6-sol', '_task_id': 'task-websocket',
        '_responses_feature_profile': 'openai',
        '_responses_transport': 'websocket',
        'messages': messages,
        'tools': [{'type': 'function', 'function': {
            'name': 'read_files', 'description': 'read files',
            'parameters': {'type': 'object'},
        }}],
    }


@pytest.fixture(autouse=True)
def _fresh_thread_state():
    from lib.llm import responses_websocket as ws
    ws._sessions().clear()
    ws._disabled_tasks().clear()
    ws._idle_checkpoints.clear()
    yield
    ws._sessions().clear()
    ws._disabled_tasks().clear()
    ws._idle_checkpoints.clear()


def test_continuation_sends_previous_response_id_and_only_new_input(
        monkeypatch):
    from lib.llm._sse_core import prepare_request
    from lib.llm import responses_websocket as ws

    connection = _ScriptedConnection(_tool_call_round(), _text_round())
    _install_connection(ws, monkeypatch, connection)

    first_plan = prepare_request(
        _canonical_body(with_tool_output=False), api_key='key',
        base_url='https://api.openai.com/v1', api_protocol='responses')
    first_msg, first_finish, first_usage = ws.stream_responses_websocket(first_plan)
    assert first_finish == 'tool_calls'
    assert first_msg['tool_calls'][0]['function']['name'] == 'read_files'
    assert connection.closed is False

    second_plan = prepare_request(
        _canonical_body(with_tool_output=True), api_key='key',
        base_url='https://api.openai.com/v1', api_protocol='responses')
    second_msg, second_finish, second_usage = ws.stream_responses_websocket(second_plan)
    assert second_finish == 'stop'
    assert second_msg['content'] == 'done'

    first_frame, second_frame = connection.sent
    # Flat Responses WebSocket envelope, never the Realtime-style nesting.
    assert first_frame['type'] == 'response.create'
    assert first_frame['model'] == 'gpt-5.6-sol' and 'response' not in first_frame
    assert first_frame['previous_response_id'] is None
    assert 'stream' not in first_frame
    assert second_frame['previous_response_id'] == 'resp_1'
    assert [item['type'] for item in second_frame['input']] == [
        'function_call_output']
    assert second_frame['input'][0]['call_id'] == 'call_1'
    assert second_frame['tools'] == first_frame['tools']
    # The sealed body stays the complete HTTP-valid request for SSE fallback.
    sealed = second_plan.body.wire_body()
    assert sealed['stream'] is True and len(sealed['input']) == 3
    exchange = second_usage['_cache_exchange']
    assert exchange['transport'] == 'websocket' and exchange['incremental']
    assert exchange['reused_connection'] and not exchange['reused_idle_connection']
    assert exchange['logical_input_items'] == 3 and exchange['sent_input_items'] == 1
    assert exchange['provider_input_tokens'] == 4
    assert exchange['provider_cached_tokens'] is None
    assert connection.closed is True

    # Each receipt names the exact response.create text written to the socket.
    first_exchange = first_usage['_cache_exchange']
    assert first_exchange['frame_kind'] == 'full'
    assert exchange['frame_kind'] == 'incremental'
    for index, receipt in enumerate((first_exchange, exchange)):
        raw = connection.raw_sent[index].encode('utf-8')
        assert receipt['frame_sha256'] == hashlib.sha256(raw).hexdigest()
        assert receipt['frame_bytes'] == len(raw)
        assert receipt['frame_content_encoding'] == ''
        assert receipt['sent_input_items'] == len(connection.sent[index]['input'])
        assert (receipt['protocol'], receipt['transport']) == (
            'responses', 'websocket')
    assert first_exchange['sent_input_items'] == first_exchange['logical_input_items']
    assert exchange['sent_input_items'] == 1
    assert exchange['frame_reason'] is None
    # Full and incremental frames are different bytes and never share a digest.
    assert first_exchange['frame_sha256'] != exchange['frame_sha256']


@pytest.mark.parametrize('cut', ['initial_open', 'during_setup', 'after_open', 'during_budget_mark', 'reconnect'])
def test_recovery_authority_fences_websocket_open_and_frame(monkeypatch, cut):
    from lib.llm import responses_websocket as ws
    from lib.llm._sse_core import prepare_request
    from lib.llm.llm_errors import ModelAdmissionRefused
    from lib.llm.request import physical_model_dispatch_scope

    revoked = cut == 'initial_open'
    opens, upgrades = [], []
    connection = _ScriptedConnection(_tool_call_round(), [
        {'type': 'error', 'error': {'code': 'previous_response_not_found'}},
    ])

    def connect(*args, **kwargs):
        nonlocal revoked
        opens.append(args[0])
        if cut == 'during_setup':
            revoked = True
        cls = kwargs['create_connection']
        object.__new__(cls).handshake()
        if cut == 'after_open':
            revoked = True
        return connection

    refusal = ModelAdmissionRefused('fixture authority revoked')

    def guard():
        if revoked or (cut == 'reconnect' and len(connection.sent) == 2):
            raise refusal

    def mark_dispatched():
        nonlocal revoked
        revoked = True

    def plan(with_tool_output=False):
        prepared = prepare_request(_canonical_body(with_tool_output=with_tool_output),
            api_key='fixture-key', base_url='https://fixture.invalid/v1', api_protocol='responses')
        if cut == 'during_budget_mark':
            prepared.model_budget_lease = SimpleNamespace(mark_dispatched=mark_dispatched)
        return prepared

    monkeypatch.setattr(ws, '_websocket_proxy', lambda *a: None)
    monkeypatch.setattr('websockets.sync.client.connect', connect)
    monkeypatch.setattr('websockets.sync.client.ClientConnection.handshake', lambda self: upgrades.append(True))
    if cut == 'during_budget_mark':
        unrelated = _ScriptedConnection()
        ws._sessions()['unrelated'] = ws._Session(connection=unrelated, state_key='unrelated')
        close = connection.close

        def close_with_error():
            close()
            raise OSError('fixture close failure')

        monkeypatch.setattr(connection, 'close', close_with_error)
    with physical_model_dispatch_scope(guard):
        if cut == 'reconnect':
            ws.stream_responses_websocket(plan())
        with pytest.raises(ModelAdmissionRefused, match='fixture authority revoked') as caught:
            ws.stream_responses_websocket(plan(with_tool_output=cut == 'reconnect'))
    assert caught.value is refusal
    assert len(opens) == (0 if cut == 'initial_open' else 1)
    assert len(upgrades) == (0 if cut in ('initial_open', 'during_setup') else 1)
    assert len(connection.sent) == (2 if cut == 'reconnect' else 0)
    if cut == 'during_budget_mark':
        assert connection.closed
        assert all(entry.connection is not connection for entry in ws._sessions().values())
        assert ws._sessions()['unrelated'].connection is unrelated and not unrelated.closed


def _continuation_session(previous, output):
    from lib.llm import responses_websocket as ws
    return ws._Session(connection=None, previous_response_id='resp_prev',
                       last_request=previous, last_output=output)


def test_continuation_keeps_every_occurrence_of_recycled_tool_outputs():
    from lib.llm import responses_websocket as ws

    prior = [{'type': 'message', 'role': 'user', 'content': 'go'}]
    result = {'type': 'function_call_output', 'call_id': 'recycled-call',
              'output': 'same receipt'}
    previous = {'model': 'm', 'input': prior, 'store': False}
    session = _continuation_session(previous, [dict(_CALL)])
    current = dict(previous, input=[
        *prior, dict(_CALL, id='other-id'), dict(result), dict(result)])

    assert ws._continuation_input(session, current) == ([result, result], '')


@pytest.mark.parametrize('drift', [
    'instructions', 'tools', 'reasoning', 'history', 'replayed_output',
    'model_item_in_delta', 'nothing_new'])
def test_any_drift_resets_the_chain_with_complete_input(drift):
    from lib.llm import responses_websocket as ws
    from lib.llm.request import seal_request

    prior = [{'type': 'message', 'role': 'user', 'content': 'go'}]
    previous = {'model': 'gpt-test', 'instructions': 'stable',
                'tools': [{'type': 'function', 'name': 'read_files'}],
                'reasoning': {'effort': 'high'}, 'store': False,
                'input': prior}
    current = json.loads(json.dumps(previous))
    current['input'] += [dict(_CALL), {
        'type': 'function_call_output', 'call_id': 'call_1', 'output': 'ok'}]
    if drift == 'instructions':
        current['instructions'] = 'replaced prefix'
    elif drift == 'tools':
        current['tools'] = []
    elif drift == 'reasoning':
        current['reasoning'] = {'effort': 'low'}
    elif drift == 'history':
        current['input'][0]['content'] = 'compacted history'
    elif drift == 'replayed_output':
        current['input'][1]['arguments'] = '{"paths":["OTHER.md"]}'
    elif drift == 'model_item_in_delta':
        current['input'].append(dict(_CALL, call_id='call_2'))
    else:
        current['input'] = current['input'][:2]
    sealed = seal_request(current, model='gpt-test', input_tokens=10,
                          semantic_id='test')
    session = _continuation_session(previous, [dict(_CALL)])

    payload, request, full_reason = ws._request_payload(SimpleNamespace(body=sealed), session)

    assert full_reason == {
        'instructions': 'request_fields_changed',
        'tools': 'request_fields_changed',
        'reasoning': 'request_fields_changed',
        'history': 'history_changed',
        'replayed_output': 'previous_output_changed',
        'model_item_in_delta': 'non_application_delta',
        'nothing_new': 'empty_delta',
    }[drift]
    assert payload['previous_response_id'] is None
    assert payload['input'] == current['input']
    assert request == current


def test_exact_extension_continues_and_detaches_the_sealed_body():
    from lib.llm import responses_websocket as ws
    from lib.llm.request import seal_request

    prior = [{'type': 'message', 'role': 'user', 'content': 'go'}]
    previous = {'model': 'gpt-test', 'store': False, 'input': prior}
    tool_output = {'type': 'function_call_output', 'call_id': 'call_1',
                   'output': 'ok'}
    current = dict(previous, input=[*prior, dict(_CALL), tool_output],
                   stream=True)
    sealed = seal_request(current, model='gpt-test', input_tokens=10,
                          semantic_id='test')
    session = _continuation_session(previous, [dict(_CALL, status='completed')])

    payload, request, full_reason = ws._request_payload(SimpleNamespace(body=sealed), session)

    assert full_reason == ''
    assert payload['previous_response_id'] == 'resp_prev'
    assert payload['input'] == [tool_output]
    assert 'stream' not in payload and 'stream' not in request
    assert len(request['input']) == 3
    payload['input'].clear()
    assert len(sealed['input']) == 3


def test_websocket_protocol_activity_outlives_stream_idle_window(
        monkeypatch):
    from lib.llm._sse_core import prepare_request
    from lib.llm import responses_websocket as ws
    from lib.llm.stream_result import ProviderStreamState

    class ProtocolActivityConnection(_ScriptedConnection):
        def recv(self, timeout=None):
            time.sleep(0.02)
            return super().recv(timeout)

    connection = ProtocolActivityConnection(
        [{'type': 'response.in_progress'} for _ in range(21)] + [
            {'type': 'response.output_text.delta', 'delta': 'done'},
            {'type': 'response.completed', 'response': {
                'id': 'resp-long', 'status': 'completed', 'output': [],
                'usage': {'input_tokens': 4, 'output_tokens': 21}}},
        ])
    _install_connection(ws, monkeypatch, connection)
    monkeypatch.setattr(
        'lib.llm._transport.IDLE_STREAM_TIMEOUT_S', 0.15)
    plan = prepare_request(
        _canonical_body(with_tool_output=False), api_key='key',
        base_url='https://api.openai.com/v1', api_protocol='responses')

    result = ws.stream_responses_websocket(plan)

    assert result.state is ProviderStreamState.PROVIDER_FINISHED
    assert result.message['content'] == 'done'
    assert result.message.get('reasoning_content', '') == ''
    assert '_no_actionable_timeout' not in result.usage
    assert '_semantic_progress_timeout' not in result.usage


def test_websocket_transport_silence_finalizes_as_premature_close(monkeypatch):
    from lib.llm._sse_core import prepare_request
    from lib.llm import responses_websocket as ws
    from lib.llm.stream_result import ProviderStreamState

    class SilentConnection(_ScriptedConnection):
        def recv(self, timeout=None):
            time.sleep(float(timeout or 0))
            raise TimeoutError

    connection = SilentConnection([])
    _install_connection(ws, monkeypatch, connection)
    monkeypatch.setattr(
        'lib.llm._transport.IDLE_STREAM_TIMEOUT_S', 0.08)
    plan = prepare_request(
        _canonical_body(with_tool_output=False), api_key='key',
        base_url='https://api.openai.com/v1', api_protocol='responses')

    started = time.monotonic()
    result = ws.stream_responses_websocket(plan)

    assert time.monotonic() - started < 1.0
    assert result.state is ProviderStreamState.PREMATURE_CLOSE
    assert result.usage['_failure_stage'] == 'midstream_close'
    assert result.usage['_missing_done'] is True
    assert connection.closed is True


def test_unverified_partial_tool_response_never_advances_socket_state(
        monkeypatch):
    from dataclasses import replace

    from lib.llm._sse_core import prepare_request
    from lib.llm import responses_websocket as ws
    from lib.llm.stream_result import ProviderStreamState

    class PartialToolConnection(_ScriptedConnection):
        def recv(self, timeout=None):
            if self.events:
                return json.dumps(self.events.pop(0))
            time.sleep(float(timeout or 0))
            raise TimeoutError

    connection = PartialToolConnection([{
        'type': 'response.output_item.added', 'output_index': 0,
        'item': {
            'type': 'function_call', 'id': 'partial-item',
            'call_id': 'partial-call', 'name': 'read_files',
            'arguments': '{}',
        },
    }])
    _install_connection(ws, monkeypatch, connection)
    monkeypatch.setattr('lib.llm._transport.IDLE_STREAM_TIMEOUT_S', 0.08)
    original_finalize = ws.SSEAccumulator.finalize

    def compatibility_tool_finish(accumulator, *args, **kwargs):
        result = original_finalize(accumulator, *args, **kwargs)
        return replace(result, compatibility_finish_reason='tool_calls')

    monkeypatch.setattr(ws.SSEAccumulator, 'finalize', compatibility_tool_finish)
    plan = prepare_request(
        _canonical_body(with_tool_output=False), api_key='key',
        base_url='https://api.openai.com/v1', api_protocol='responses')
    # Exercise the dangerous compatibility shape directly: partial tools plus
    # a stale/non-terminal response token must still never keep the session.
    plan.wire_translator.response_id = 'resp-partial'

    result = ws.stream_responses_websocket(plan)

    assert result.state is ProviderStreamState.PREMATURE_CLOSE
    assert result.finish_reason == 'tool_calls'
    assert connection.closed is True
    assert ws._sessions() == {}


def test_non_object_websocket_event_is_typed_malformed_stream(monkeypatch):
    from lib.llm._sse_core import prepare_request
    from lib.llm import responses_websocket as ws
    from lib.llm.stream_result import ProviderStreamState

    class InvalidShapeConnection(_ScriptedConnection):
        def recv(self, timeout=None):
            if self.events:
                self.events.clear()
                return json.dumps(['not', 'an', 'event'])
            raise TimeoutError

    connection = InvalidShapeConnection([{'type': 'placeholder'}])
    _install_connection(ws, monkeypatch, connection)
    plan = prepare_request(
        _canonical_body(with_tool_output=False), api_key='key',
        base_url='https://api.openai.com/v1', api_protocol='responses')

    result = ws.stream_responses_websocket(plan)

    assert result.state is ProviderStreamState.MALFORMED_STREAM
    assert result.evidence.malformed_frame_count == 1
    assert connection.closed is True


# ── ChatGPT Codex subscription backend ─────────────────────────────────


@pytest.fixture
def codex_identity(monkeypatch):
    monkeypatch.setattr('lib.oauth.codex.codex_get_valid_token_snapshot',
                        lambda *args, **kwargs: CodexAccessTokenSnapshot('access-tok', 'acc-1'))
    monkeypatch.setattr('lib.oauth.codex_catalog.cached_codex_provider_models',
                        lambda: [])
    monkeypatch.setattr('lib.oauth.codex_version.codex_client_version',
                        lambda: '0.165.0')
    from lib.subscription_quota import _reset_subscription_quota_cache_for_tests
    _reset_subscription_quota_cache_for_tests()
    yield
    from lib.oauth.codex_turn_state import release_turn_state
    release_turn_state('task-codex')
    _reset_subscription_quota_cache_for_tests()


def _codex_plan(messages, *, task_id='task-codex', conv_id='conv-1'):
    from lib.llm._sse_core import prepare_request
    return prepare_request({
        'model': 'gpt-6-sol', '_task_id': task_id, '_conv_id': conv_id,
        # The public experiment flag's default; Codex follows its first-party
        # WebSocket contract regardless.
        '_responses_transport': 'sse',
        'messages': messages,
        'tools': [{'type': 'function', 'function': {
            'name': 'read_files', 'description': 'read files',
            'parameters': {'type': 'object'}}}],
    }, api_key='', base_url=_CODEX_BASE, api_protocol='responses',
        oauth='codex')


_REASONING = {'type': 'reasoning', 'id': 'rs_1', 'summary': [],
              'encrypted_content': 'opaque-reasoning-1'}
_RATE_LIMITS = {
    'type': 'codex.rate_limits', 'plan_type': 'pro',
    'rate_limits': {'allowed': True, 'limit_reached': False,
                    'primary': {'used_percent': 28, 'window_minutes': 10080,
                                'reset_after_seconds': 100,
                                'reset_at': 1793774203},
                    'secondary': None},
    'credits': {'has_credits': False, 'unlimited': False, 'balance': '0'},
}
_METADATA = {'type': 'codex.response.metadata',
             'headers': {'x-codex-turn-state': 'routing-1',
                         'x-models-etag': 'W/"catalog"'}}
_TIMING = {'type': 'responsesapi.websocket_timing',
           'timing_metrics': {'pre_inference_ms': 300.0}}


def _codex_round_one():
    return [_RATE_LIMITS, _METADATA,
            *_tool_call_round(reasoning=dict(_REASONING))[:-1], _TIMING,
            _tool_call_round(reasoning=dict(_REASONING))[-1]]


def _round_two_messages(first_msg):
    return [{'role': 'user', 'content': 'read the file'}, first_msg,
            {'role': 'tool', 'tool_call_id': 'call_1',
             'content': 'README contents'}]


def test_codex_tool_loop_continues_on_one_socket_with_side_channels(
        monkeypatch, codex_identity, caplog):
    from lib.llm import responses_websocket as ws
    from lib.oauth.codex_turn_state import turn_state

    handshakes = []
    connection = _ScriptedConnection(_codex_round_one(), _text_round())
    _install_connection(ws, monkeypatch, connection, handshakes=handshakes)

    first_plan = _codex_plan([{'role': 'user', 'content': 'read the file'}])
    assert first_plan.responses_transport == 'websocket'
    with caplog.at_level('INFO', logger='lib.llm.responses_websocket'):
        first_msg, first_finish, first_usage = ws.stream_responses_websocket(
            first_plan)
    assert first_finish == 'tool_calls'
    second_plan = _codex_plan(_round_two_messages(first_msg))
    _msg, second_finish, _usage = ws.stream_responses_websocket(second_plan)
    assert second_finish == 'stop'

    handshake = handshakes[0]
    assert handshake['OpenAI-Beta'] == ws.CODEX_WEBSOCKET_BETA
    assert handshake['x-codex-routing-hint'] == 'model=gpt-6-sol'
    assert handshake['session-id'] and handshake['chatgpt-account-id'] == 'acc-1'
    assert not {'accept', 'content-type', 'content-encoding'} & {
        name.lower() for name in handshake}
    assert len(handshakes) == 1  # one socket for the whole tool loop

    first_frame, second_frame = connection.sent
    assert first_frame['previous_response_id'] is None
    assert 'client_metadata' not in first_frame
    # Verbatim reasoning replay plus rebuilt calls prove the exact extension.
    assert second_frame['previous_response_id'] == 'resp_1'
    assert [item['type'] for item in second_frame['input']] == [
        'function_call_output']
    assert second_frame['client_metadata'] == {'x-codex-turn-state': 'routing-1'}
    assert turn_state('task-codex') == 'routing-1'

    quota = first_usage['_subscription_quota']
    assert quota['source'] == 'websocket_event'
    assert quota['plan_type'] == 'pro'
    assert quota['primary']['used_percent'] == 28.0
    assert quota['primary']['window_minutes'] == 10080
    assert quota['primary']['resets_at'] == 1793774203
    # Side-channel frames are transport metadata, not unknown stream events.
    assert first_plan.wire_translator.unknown_event_types == set()
    assert connection.closed is False and ws._sessions() == {}

    # The first completed exchange logs the exact sent-frame identity and
    # nothing payload-sized.
    first_raw = connection.raw_sent[0].encode('utf-8')
    first_log = next(
        record.getMessage() for record in caplog.records
        if record.name == 'lib.llm.responses_websocket'
        and 'trace=' in record.getMessage())
    assert 'frame_kind=full' in first_log
    assert f'frame_sha256={hashlib.sha256(first_raw).hexdigest()}' in first_log
    assert f'frame_bytes={len(first_raw)}' in first_log
    assert 'read the file' not in first_log


@pytest.mark.parametrize('change', [None, 'conversation', 'owner', 'credential',
                                    'proxy', 'history'])
def test_codex_terminal_response_can_continue_in_next_task(
        monkeypatch, codex_identity, change):
    from concurrent.futures import ThreadPoolExecutor

    from lib.llm import responses_websocket as ws
    from lib.oauth.codex_turn_state import observe_turn_state

    opener = {'role': 'user', 'content': 'hi'}
    followup = {'role': 'user', 'content': 'continue'}
    old_socket = _ScriptedConnection(_text_round())
    fresh_socket = _ScriptedConnection(_text_round(response_id='resp_3'))
    same_socket = change in (None, 'history')
    if same_socket:
        old_socket.scripts.append(_text_round(response_id='resp_3'))
    _install_connection(ws, monkeypatch, old_socket, fresh_socket)
    monkeypatch.setattr(ws, '_websocket_proxy',
                        lambda plan, owner: 'http://proxy:3128'
                        if change == 'proxy' else None)

    def first_turn():
        result = ws.stream_responses_websocket(_codex_plan([opener]), owner_scope='1')
        ws.release_task_websockets('task-codex')
        return result.message

    # A settled task can hand off to a different durable-worker thread.
    with ThreadPoolExecutor(max_workers=1) as executor:
        assistant = executor.submit(first_turn).result()
    assert not old_socket.closed
    observe_turn_state('task-codex', 'first-turn-only')
    first_item = dict(opener, content='edited') if change == 'history' else opener
    second = _codex_plan(
        [first_item, assistant, followup], task_id='next-task',
        conv_id='other' if change == 'conversation' else 'conv-1')
    if change == 'credential':
        second.hdrs = dict(second.hdrs, Authorization='Bearer changed')
    ws.stream_responses_websocket(second, owner_scope='2' if change == 'owner' else '1')

    frame = (old_socket if same_socket else fresh_socket).sent[-1]
    assert 'x-codex-turn-state' not in frame.get('client_metadata', {})
    if change is None:
        assert frame['previous_response_id'] == 'resp_2'
        assert len(frame['input']) == 1
    else:
        assert frame['previous_response_id'] is None
        assert len(frame['input']) == 3


_PREVIOUS_REJECTED = {'type': 'error', 'status': 400, 'error': {
    'type': 'invalid_request_error',
    'message': 'Invalid `previous_response_id`.'}}
_CONNECTION_LIMIT = {'type': 'error', 'status': 400, 'error': {
    'type': 'invalid_request_error',
    'code': 'websocket_connection_limit_reached',
    'message': 'Responses websocket connection limit reached (60 minutes).'}}


def test_refused_continuation_reconnects_and_resends_complete_input_once(
        monkeypatch, codex_identity):
    from lib.llm import responses_websocket as ws

    first = _ScriptedConnection(_codex_round_one(), [_PREVIOUS_REJECTED])
    second = _ScriptedConnection(_text_round())
    _install_connection(ws, monkeypatch, first, second)

    first_msg, _finish, _usage = ws.stream_responses_websocket(
        _codex_plan([{'role': 'user', 'content': 'read the file'}]))
    msg, finish, _usage = ws.stream_responses_websocket(
        _codex_plan(_round_two_messages(first_msg)))

    assert finish == 'stop' and msg['content'] == 'done'
    assert first.sent[1]['previous_response_id'] == 'resp_1'
    assert first.closed is True
    replayed = second.sent[0]
    assert replayed['previous_response_id'] is None
    assert [item['type'] for item in replayed['input']] == [
        'message', 'reasoning', 'function_call', 'function_call_output']


def test_chain_reset_resends_complete_input_as_a_bounded_reset_frame(
        monkeypatch):
    from lib.llm._sse_core import prepare_request
    from lib.llm.cache_exchange import loggable_cache_exchange
    from lib.llm import responses_websocket as ws

    refused = _ScriptedConnection(_tool_call_round(), [
        {'type': 'error', 'error': {'code': 'previous_response_not_found'}},
    ])
    reset = _ScriptedConnection(_text_round())
    _install_connection(ws, monkeypatch, refused, reset)

    first_plan = prepare_request(
        _canonical_body(with_tool_output=False), api_key='key',
        base_url='https://api.openai.com/v1', api_protocol='responses')
    first_msg, _finish, _usage = ws.stream_responses_websocket(first_plan)
    second_plan = prepare_request(
        _canonical_body(with_tool_output=True), api_key='key',
        base_url='https://api.openai.com/v1', api_protocol='responses')
    _msg, finish, second_usage = ws.stream_responses_websocket(second_plan)

    assert finish == 'stop'
    # The refused frame carried the delta; the retry reconnects and resends the
    # complete input as a new chain.
    assert refused.sent[1]['previous_response_id'] == 'resp_1'
    assert reset.sent[0]['previous_response_id'] is None
    exchange = second_usage['_cache_exchange']
    raw = reset.raw_sent[0].encode('utf-8')
    assert exchange['frame_kind'] == 'reset'
    assert exchange['frame_sha256'] == hashlib.sha256(raw).hexdigest()
    assert exchange['frame_bytes'] == len(raw)
    assert exchange['sent_input_items'] == len(reset.sent[0]['input']) == 3
    assert exchange['frame_reason'] == 'provider_reconnect'
    assert exchange['frame_content_encoding'] == ''
    assert loggable_cache_exchange(exchange)['frame_reason'] == 'provider_reconnect'
    # The lost incremental frame and this reset full frame describe the same
    # logical request, and their exact bytes must not be conflated.
    refused_hash = hashlib.sha256(refused.raw_sent[1].encode('utf-8')).hexdigest()
    assert exchange['frame_sha256'] != refused_hash
    assert (exchange['protocol'], exchange['transport']) == (
        'responses', 'websocket')


def test_every_websocket_frame_reason_validates_against_the_contract():
    from pathlib import Path

    from jsonschema import Draft202012Validator

    from lib.llm import responses_websocket as ws
    from lib.llm._sse_core import prepare_request
    from lib.llm.cache_exchange import (
        FULL_REASONS, cache_exchange_receipt, loggable_cache_exchange)
    from lib.llm.sent_frame import sent_frame

    schema = json.loads((Path(__file__).resolve().parents[2]
                         / 'contracts/llm_request_evidence_v1.schema.json')
                        .read_text(encoding='utf-8'))
    validator = Draft202012Validator(schema)
    plan = prepare_request(
        _canonical_body(with_tool_output=False), api_key='key',
        base_url='https://api.openai.com/v1', api_protocol='responses')

    # Server refusal codes are detection vocabulary, not frame reasons: a
    # refused chain is resent as provider_reconnect.
    assert ws._CHAIN_RESET_CODES.isdisjoint(ws._RESET_FRAME_REASONS)
    assert ws._RESET_FRAME_REASONS <= set(FULL_REASONS)
    _payload, _request, reconnect = ws._request_payload(
        plan, ws._Session(connection=None), force_full=True)
    assert reconnect == 'provider_reconnect'

    for reason in FULL_REASONS:
        frame = sent_frame(
            plan.body, '{"type":"response.create"}', transport='websocket',
            kind='reset' if reason in ws._RESET_FRAME_REASONS else 'full',
            reason=reason, sent_items=1)
        receipt = cache_exchange_receipt(
            plan.body, {}, transport='websocket', verified=True, frame=frame)
        validator.validate(receipt)
        validator.validate(loggable_cache_exchange(receipt))


def test_connection_limit_reconnects_and_a_second_refusal_is_retryable(
        monkeypatch, codex_identity):
    from lib.llm import responses_websocket as ws
    from lib.llm.llm_errors import RetryableAPIError

    recovered = _ScriptedConnection(_text_round())
    _install_connection(ws, monkeypatch,
                        _ScriptedConnection([_CONNECTION_LIMIT]), recovered)
    msg, _finish, _usage = ws.stream_responses_websocket(
        _codex_plan([{'role': 'user', 'content': 'hi'}]))
    assert msg['content'] == 'done'
    assert recovered.sent[0]['previous_response_id'] is None

    _install_connection(ws, monkeypatch,
                        _ScriptedConnection([_CONNECTION_LIMIT]),
                        _ScriptedConnection([_CONNECTION_LIMIT]))
    with pytest.raises(RetryableAPIError):
        ws.stream_responses_websocket(
            _codex_plan([{'role': 'user', 'content': 'hi'}]))


def test_socket_closed_between_rounds_reconnects_with_complete_input(
        monkeypatch, codex_identity):
    from lib.llm import responses_websocket as ws

    first = _ScriptedConnection(_codex_round_one())
    second = _ScriptedConnection(_text_round())
    _install_connection(ws, monkeypatch, first, second)

    first_msg, _finish, _usage = ws.stream_responses_websocket(
        _codex_plan([{'role': 'user', 'content': 'read the file'}]))
    first.state = SimpleNamespace(name='CLOSED')
    msg, _finish, _usage = ws.stream_responses_websocket(
        _codex_plan(_round_two_messages(first_msg)))

    assert msg['content'] == 'done'
    assert len(first.sent) == 1
    assert second.sent[0]['previous_response_id'] is None
    assert len(second.sent[0]['input']) == 4


def test_open_failure_moves_the_task_to_sse_until_teardown(
        monkeypatch, codex_identity):
    from lib.llm import responses_websocket as ws

    attempts = []

    def refuse(plan, key, handshake, owner_scope=''):
        attempts.append(key)
        raise ws.ResponsesWebSocketUnavailable('HTTP 426')

    monkeypatch.setattr(ws, '_open_session', refuse)
    plan = _codex_plan([{'role': 'user', 'content': 'hi'}])
    with pytest.raises(ws.ResponsesWebSocketUnavailable) as first:
        ws.stream_responses_websocket(plan)
    with pytest.raises(ws.ResponsesWebSocketUnavailable) as latched:
        ws.stream_responses_websocket(plan)

    assert first.value.expected is False and latched.value.expected is True
    assert len(attempts) == 1
    ws.release_task_websockets('task-codex')
    with pytest.raises(ws.ResponsesWebSocketUnavailable):
        ws.stream_responses_websocket(plan)
    assert len(attempts) == 2


def test_post_send_socket_failure_keeps_later_rounds_on_sse(
        monkeypatch, codex_identity):
    from lib.llm import responses_websocket as ws
    from lib.llm.llm_errors import RetryableAPIError

    class Broken(_ScriptedConnection):
        def recv(self, timeout=None):
            raise ConnectionResetError('peer reset')

    _install_connection(ws, monkeypatch, Broken([]))
    plan = _codex_plan([{'role': 'user', 'content': 'hi'}])
    with pytest.raises(RetryableAPIError):
        ws.stream_responses_websocket(plan)
    with pytest.raises(ws.ResponsesWebSocketUnavailable) as latched:
        ws.stream_responses_websocket(plan)
    assert latched.value.expected is True


def test_teardown_closes_the_task_socket_without_touching_others(
        monkeypatch, codex_identity):
    from lib.llm import responses_websocket as ws

    mine = ws._Session(connection=_ScriptedConnection(), state_key='task-codex')
    other = ws._Session(connection=_ScriptedConnection(), state_key='task-other')
    ws._sessions().update({'mine': mine, 'other': other})

    ws.release_task_websockets('task-codex')

    assert mine.connection.closed is True and other.connection.closed is False
    assert list(ws._sessions()) == ['other']


def test_open_session_forwards_handshake_and_never_waits_to_close(monkeypatch):
    import websockets.sync.client as client
    from lib.llm import responses_websocket as ws

    captured = {}

    def fake_connect(url, **kwargs):
        captured.update(kwargs, url=url)
        return SimpleNamespace()

    monkeypatch.setattr(client, 'connect', fake_connect)
    monkeypatch.setattr(ws, '_websocket_proxy',
                        lambda plan, owner: 'http://proxy:3128')
    plan = SimpleNamespace(url=_CODEX_BASE + '/responses',
                           responses_state_key='task-open', hdrs={})
    handshake = {'OpenAI-Beta': ws.CODEX_WEBSOCKET_BETA}

    session = ws._open_session(plan, 'key-open', handshake, 'owner')

    assert captured['url'] == 'wss://chatgpt.com/backend-api/codex/responses'
    assert captured['additional_headers'] == handshake
    assert captured['proxy'] == 'http://proxy:3128'
    # Sockets are closed only when discarded: never stall a finished turn on
    # the peer's closing acknowledgement.
    assert captured['close_timeout'] == 0
    assert session.state_key == 'task-open'
    assert ws._sessions()['key-open'] is session


def test_socket_follows_the_http_egress_decision(monkeypatch):
    from lib.llm import responses_websocket as ws
    from lib.network_routes import Route

    plan = SimpleNamespace(url=_CODEX_BASE + '/responses')
    monkeypatch.setattr('lib.desktop.egress.route_request',
                        lambda *args, **kwargs: 'agent-1')
    with pytest.raises(ws.ResponsesWebSocketUnavailable) as relayed:
        ws._websocket_proxy(plan, 'owner')
    assert relayed.value.expected is True

    monkeypatch.setattr('lib.desktop.egress.route_request',
                        lambda *args, **kwargs: 'direct')
    for routes, expected in (
            ([Route('direct', 'direct', 'direct')], None),
            ([Route('env', 'environment proxy', 'proxy', proxy_url='http://env:8080')], 'http://env:8080'),
            ([Route('pool', 'pool', 'proxy', proxy_url='http://proxy:3128')],
             'http://proxy:3128')):
        monkeypatch.setattr('lib.proxy.network_route_candidates',
                            lambda *args, routes=routes, **kwargs: routes)
        assert ws._websocket_proxy(plan, 'owner') == expected

    monkeypatch.setattr('lib.proxy.network_route_candidates',
                        lambda *args, **kwargs: [])
    with pytest.raises(ws.ResponsesWebSocketUnavailable, match='no reachable'):
        ws._websocket_proxy(plan, 'owner')
