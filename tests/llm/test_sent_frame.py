"""Exact sent-frame identity for every transport and its cache receipt.

The frame names the bytes a transport handed over, not the pre-transport body:
a cache audit must be able to compare ``frame_sha256`` with the exact payload a
client sent. These specs pin full/incremental/reset frames, content-encoded
bytes (compressed exactly once), UTF-8 payloads, enum validation and the
bounded receipt projection.
"""
import hashlib
import json

import pytest

from lib.llm.cache_exchange import (
    CACHE_EXCHANGE_KEY,
    cache_exchange_receipt,
    loggable_cache_exchange,
)
from lib.llm.request import seal_request, transport_body_arguments
from lib.llm.request_encoding import zstd_available, zstd_compress
from lib.llm.sent_frame import (
    FRAME_CONTENT_ENCODINGS,
    FRAME_KINDS,
    FRAME_TRANSPORTS,
    MAX_REASON_CHARS,
    SentFrame,
    full_sent_frame,
    sent_frame,
    transport_send,
)

pytestmark = pytest.mark.unit


def admitted(count=2, *, protocol='openai', content_encoding=''):
    history = 'input' if protocol == 'responses' else 'messages'
    body = {'model': 'test-model',
            history: [{'role': 'user', 'content': f'item {index}'}
                      for index in range(count)]}
    return seal_request(
        body, model='test-model', protocol=protocol, input_tokens=1,
        semantic_id='semantic-fixture', content_encoding=content_encoding)


def test_full_frame_hashes_the_exact_http_bytes_with_request_identities():
    request = admitted(2)
    frame = full_sent_frame(request, transport='http')

    assert frame.sha256 == hashlib.sha256(request.encoded_payload()).hexdigest()
    assert frame.byte_length == len(request.payload)
    assert frame.request_prefix_id == request.provider_prefix_id
    assert frame.semantic_id == request.semantic_id
    assert (frame.protocol, frame.transport, frame.kind) == ('openai', 'http', 'full')
    assert (frame.reason, frame.content_encoding) == ('', '')
    assert frame.logical_items == frame.sent_items == 2


@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
def test_full_frame_counts_the_protocol_history_field(protocol):
    frame = full_sent_frame(admitted(3, protocol=protocol), transport='sse')
    assert frame.logical_items == 3
    assert frame.sent_items == 3


def test_evidence_is_bounded_and_carries_request_identities():
    request = admitted(2)
    evidence = full_sent_frame(request, transport='sse').evidence()

    assert set(evidence) == {
        'semantic_request_id', 'provider_prefix_id', 'protocol',
        'frame_sha256', 'frame_bytes', 'frame_kind', 'frame_reason',
        'frame_content_encoding'}
    assert evidence['semantic_request_id'] == request.semantic_id
    assert evidence['provider_prefix_id'] == request.provider_prefix_id
    assert evidence['frame_sha256'] == hashlib.sha256(request.payload).hexdigest()
    assert evidence['frame_bytes'] == len(request.payload)
    assert (evidence['frame_kind'], evidence['frame_reason'],
            evidence['frame_content_encoding']) == ('full', None, '')


def test_incremental_frame_records_its_own_item_count():
    request = admitted(3)
    payload = json.dumps({'type': 'response.create', 'input': []})
    frame = sent_frame(request, payload, transport='websocket',
                       kind='incremental', sent_items=2)

    assert frame.sha256 == hashlib.sha256(payload.encode('utf-8')).hexdigest()
    assert frame.byte_length == len(payload.encode('utf-8'))
    assert frame.logical_items == 3 and frame.sent_items == 2

    receipt = cache_exchange_receipt(
        request, {}, transport='websocket', verified=True, frame=frame)
    assert receipt['logical_input_items'] == 3
    assert receipt['sent_input_items'] == 2
    assert receipt['incremental'] is True
    assert receipt['full_reason'] is None
    assert receipt['frame_kind'] == 'incremental'
    assert receipt['frame_sha256'] == frame.sha256
    assert receipt['frame_bytes'] == frame.byte_length


def test_reset_frame_keeps_its_bounded_reason():
    request = admitted(3)
    frame = sent_frame(request, b'{}', transport='websocket', kind='reset',
                       reason='history_changed')

    receipt = cache_exchange_receipt(
        request, {}, transport='websocket', verified=True, frame=frame)
    assert receipt['incremental'] is False
    assert receipt['full_reason'] == 'history_changed'
    assert receipt['frame_kind'] == 'reset'
    assert receipt['frame_reason'] == 'history_changed'


def test_str_payload_is_utf8_encoded():
    request = admitted(1)
    frame = sent_frame(request, '中文 frame', transport='native')
    encoded = '中文 frame'.encode('utf-8')
    assert frame.sha256 == hashlib.sha256(encoded).hexdigest()
    assert frame.byte_length == len(encoded)
    assert frame.content_encoding == ''


def test_frame_for_another_request_or_transport_is_ignored():
    request = admitted(2)
    frame = full_sent_frame(request, transport='http')
    other_request = dataclass_replace(frame, semantic_id='another-request')
    receipt = cache_exchange_receipt(
        request, {}, transport='http', verified=True, frame=other_request)
    assert 'frame_sha256' not in receipt
    assert receipt['transport'] == 'http'

    receipt = cache_exchange_receipt(
        request, {}, transport='sse', verified=True, frame=frame)
    assert 'frame_sha256' not in receipt


def dataclass_replace(frame, **changes):
    from dataclasses import replace
    return replace(frame, **changes)


def test_receipt_without_a_frame_keeps_its_existing_shape():
    request = admitted(2)
    receipt = cache_exchange_receipt(
        request, {'prompt_tokens': 10}, transport='http', verified=True)
    assert receipt['sent_input_items'] == receipt['logical_input_items'] == 2
    assert receipt['full_reason'] is None
    assert not any(key.startswith('frame_') for key in receipt)


def test_loggable_receipt_bounds_frame_fields():
    request = admitted(2)
    frame = full_sent_frame(request, transport='http')
    receipt = cache_exchange_receipt(
        request, {}, transport='http', verified=True, frame=frame)
    logged = loggable_cache_exchange(receipt)
    assert logged['frame_sha256'] == frame.sha256
    assert logged['frame_bytes'] == frame.byte_length
    assert logged['frame_kind'] == 'full'
    assert logged['frame_reason'] is None
    assert logged['frame_content_encoding'] == ''

    tampered = {**receipt,
                'frame_sha256': 'A' * 64,
                'frame_bytes': 2**60,
                'frame_kind': 'partial',
                'frame_reason': 'private provider text ' * 10,
                'frame_content_encoding': 'br'}
    bounded = loggable_cache_exchange(tampered)
    assert bounded['frame_sha256'] is None
    assert bounded['frame_bytes'] is None
    assert bounded['frame_kind'] is None
    assert bounded['frame_reason'] is None
    assert bounded['frame_content_encoding'] is None
    assert 'private' not in str(bounded)


def test_enum_values_are_closed():
    request = admitted(1)
    with pytest.raises(ValueError, match='transport'):
        sent_frame(request, b'{}', transport='carrier-pigeon')
    with pytest.raises(ValueError, match='kind'):
        sent_frame(request, b'{}', transport='sse', kind='partial')
    with pytest.raises(ValueError, match='encoding'):
        sent_frame(request, b'{}', transport='sse', content_encoding='br')
    assert FRAME_TRANSPORTS == ('http', 'sse', 'websocket', 'native')
    assert FRAME_KINDS == ('full', 'incremental', 'reset')
    assert FRAME_CONTENT_ENCODINGS == ('', 'zstd', 'gzip', 'deflate')


def test_reason_is_bounded_and_must_be_a_string():
    request = admitted(1)
    frame = sent_frame(request, b'{}', transport='sse', reason='x' * 500)
    assert len(frame.reason) == MAX_REASON_CHARS
    with pytest.raises(TypeError, match='reason'):
        sent_frame(request, b'{}', transport='sse', reason=object())


def test_construction_validates_identity_and_counts():
    frame = full_sent_frame(admitted(1), transport='sse')
    with pytest.raises(ValueError, match='sha256'):
        dataclass_replace(frame, sha256='not-a-digest')
    with pytest.raises(ValueError, match='byte_length'):
        dataclass_replace(frame, byte_length=2**53)
    with pytest.raises(ValueError, match='byte_length'):
        dataclass_replace(frame, byte_length=True)


def test_frame_requires_an_admitted_request():
    with pytest.raises(TypeError, match='AdmittedRequest'):
        sent_frame({'model': 'm'}, b'{}', transport='sse')
    with pytest.raises(TypeError, match='AdmittedRequest'):
        full_sent_frame({'model': 'm'}, transport='sse')


@pytest.mark.skipif(not zstd_available(), reason='zstd request codec unavailable')
def test_content_encoded_send_hashes_the_data_bytes_and_compresses_once(monkeypatch):
    from lib.llm import request_encoding
    calls = []

    def counted(data):
        calls.append(len(data))
        return original_compress(data)

    original_compress = request_encoding.zstd_compress
    monkeypatch.setattr(request_encoding, 'zstd_compress', counted)
    request = admitted(2, content_encoding='zstd')

    payload, frame = transport_send(request, transport='sse', frame=None)

    assert len(calls) == 1, 'the request must be compressed exactly once'
    assert payload == original_compress(request.payload)
    assert frame.sha256 == hashlib.sha256(payload).hexdigest()
    assert frame.byte_length == len(payload)
    assert frame.content_encoding == 'zstd'
    # The very object that was hashed is what requests/httpx receive.
    assert transport_body_arguments(
        request, transport='requests', payload=payload)['data'] is payload
    assert transport_body_arguments(
        request, transport='httpx', payload=payload)['content'] is payload


@pytest.mark.skipif(not zstd_available(), reason='zstd request codec unavailable')
def test_full_frame_declares_the_request_content_encoding():
    request = admitted(2, content_encoding='zstd')
    frame = full_sent_frame(request, transport='sse')
    assert frame.content_encoding == 'zstd'
    assert frame.sha256 == hashlib.sha256(zstd_compress(request.payload)).hexdigest()


def test_transport_send_reuses_an_identity_frame_without_rehashing(monkeypatch):
    request = admitted(2)
    frame = full_sent_frame(request, transport='sse')
    monkeypatch.setattr('lib.llm.sent_frame.sha256_holding_gil',
                        lambda data: pytest.fail('identity payload rehashed'))
    payload, reused = transport_send(request, transport='sse', frame=frame)
    assert payload is request.payload
    assert reused is frame


@pytest.mark.skipif(not zstd_available(), reason='zstd request codec unavailable')
def test_sync_stream_shell_sends_the_hashed_compressed_bytes(monkeypatch):
    import time

    from lib.llm import request_encoding
    from lib.llm import stream as stream_module
    from lib.llm._sse_core import RequestPlan
    from lib.llm.diagnostics import RawSSEDumper

    request = admitted(2, content_encoding='zstd')
    plan = RequestPlan(
        url='http://fake.local/v1/chat/completions', hdrs={}, body=request,
        trace_id='frame-trace',
        raw_dumper=RawSSEDumper('test-model', 'frame-trace', {}),
        wire_translator=None, t0=time.monotonic())
    sent = []
    lines = [
        'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        'data: [DONE]',
    ]

    class Response:
        status_code = 200
        headers = {}
        encoding = None

        def iter_lines(self, decode_unicode=False):
            yield from lines

        def close(self):
            pass

    class Session:
        def post(self, *args, **kwargs):
            sent.append(kwargs['data'])
            return Response()

    original_compress = request_encoding.zstd_compress
    compressions = []

    def counted(data):
        compressions.append(len(data))
        return original_compress(data)

    monkeypatch.setattr(request_encoding, 'zstd_compress', counted)
    monkeypatch.setattr(stream_module, 'prepare_request', lambda *a, **kw: plan)
    monkeypatch.setattr(stream_module, 'get_sync_session', lambda: Session())
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda url, **kw: 'direct')

    result = stream_module._stream_chat_once(
        {'model': 'test-model', 'messages': []},
        api_key='sk-test', base_url='http://fake.local/v1', log_prefix='[t]')

    assert len(compressions) == 1
    assert len(sent) == 1
    assert sent[0] == original_compress(request.payload)
    exchange = result.usage[CACHE_EXCHANGE_KEY]
    assert exchange['frame_sha256'] == hashlib.sha256(sent[0]).hexdigest()
    assert exchange['frame_bytes'] == len(sent[0])
    assert exchange['frame_content_encoding'] == 'zstd'
    assert exchange['transport'] == 'sse'


def test_sse_accumulator_frames_the_sealed_payload_bytes():
    from lib.llm._sse_core import SSEAccumulator
    from lib.llm.diagnostics import RawSSEDumper

    request = admitted(2)
    # Production shells pass the frame built at the send boundary; the
    # accumulator must never reconstruct one from a payload it did not send.
    frame = full_sent_frame(request, transport='sse')
    accumulator = SSEAccumulator(
        request, 'frame-trace', RawSSEDumper('test-model', 'frame-trace', {}), None, 0.0,
        sent_frame=frame)
    accumulator.feed_line('data: ' + json.dumps({
        'choices': [{'delta': {'content': 'hi'}, 'finish_reason': 'stop'}]}))
    accumulator.feed_line('data: [DONE]')
    _message, _finish, usage = accumulator.finalize()

    exchange = usage[CACHE_EXCHANGE_KEY]
    assert exchange['frame_sha256'] == hashlib.sha256(request.encoded_payload()).hexdigest()
    assert exchange['frame_sha256'] == frame.sha256
    assert exchange['frame_bytes'] == len(request.payload)
    assert exchange['frame_kind'] == 'full'
    assert exchange['frame_content_encoding'] == ''
    assert exchange['sent_input_items'] == exchange['logical_input_items'] == 2


def test_sse_accumulator_without_a_send_frame_reports_unknown_bytes():
    from lib.llm._sse_core import SSEAccumulator
    from lib.llm.diagnostics import RawSSEDumper

    request = admitted(2)
    accumulator = SSEAccumulator(
        request, 'frame-trace', RawSSEDumper('test-model', 'frame-trace', {}), None, 0.0)
    accumulator.feed_line('data: ' + json.dumps({
        'choices': [{'delta': {'content': 'hi'}, 'finish_reason': 'stop'}]}))
    accumulator.feed_line('data: [DONE]')
    _message, _finish, usage = accumulator.finalize()

    exchange = usage[CACHE_EXCHANGE_KEY]
    # No send boundary named these bytes: the receipt reports the frame as
    # unknown rather than claiming a send it cannot prove.
    assert not any(key.startswith('frame_') for key in exchange)
    assert exchange['transport'] == 'sse'
    assert exchange['sent_input_items'] == exchange['logical_input_items'] == 2
    assert loggable_cache_exchange(exchange)['frame_sha256'] is None