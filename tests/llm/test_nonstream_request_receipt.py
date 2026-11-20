"""Successful nonstream responses retain receipts without decoding prompt bytes."""
import hashlib
import importlib
import json

import pytest

from lib.llm.request import seal_request
from lib.llm.request_receipt import request_parameter_receipt
from lib.llm.request_encoding import zstd_available

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
def test_successful_nonstream_response_uses_sealed_mapping(monkeypatch, protocol):
    chat_module = importlib.import_module('lib.llm.chat')
    calls = []
    sent = []
    payloads = {
        'openai': {'choices': [{'message': {'content': 'Translated input'},
                               'finish_reason': 'stop'}],
                   'usage': {'prompt_tokens': 10, 'completion_tokens': 3}},
        'anthropic': {'content': [{'type': 'text', 'text': 'Translated input'}],
                      'stop_reason': 'end_turn',
                      'usage': {'input_tokens': 10, 'output_tokens': 3}},
        'responses': {'output': [{'type': 'message', 'role': 'assistant',
                                  'content': [{'type': 'output_text',
                                               'text': 'Translated input'}]}],
                      'status': 'completed',
                      'usage': {'input_tokens': 10, 'output_tokens': 3}},
    }

    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return payloads[protocol]

    def post(*args, **kwargs):
        assert isinstance(kwargs['data'], bytes)
        sent.append(kwargs['data'])
        calls.append(json.loads(kwargs['data']))
        return Response()

    monkeypatch.setattr(chat_module, 'http_post', post)
    text, usage = chat_module.chat(
        [{'role': 'user', 'content': '需要翻译的输入'}],
        model='test-model', api_key='test-only',
        base_url='https://public.example/v1', api_protocol=protocol,
        max_tokens=128, temperature=0, max_retries=0,
    )
    assert text == 'Translated input'
    assert len(calls) == 1
    assert usage['prompt_tokens'] == 10
    assert usage['completion_tokens'] == 3
    receipt = usage['_request_parameters']
    token_key = 'max_output_tokens' if protocol == 'responses' else 'max_tokens'
    assert calls[0][token_key] == 128
    assert receipt['max_tokens'] == calls[0].get('max_tokens')
    assert 'messages' not in receipt
    exchange = usage['_cache_exchange']
    assert exchange['protocol'] == protocol
    assert exchange['transport'] == 'http' and exchange['verified']
    assert exchange['logical_input_items'] == exchange['sent_input_items'] == 1
    assert exchange['provider_input_tokens'] == 10
    assert exchange['provider_cached_tokens'] is None
    assert usage['_request_identity']['semantic_request_id']
    # The receipt names the exact bytes the client received, not the body
    # before transport encoding.
    assert exchange['frame_sha256'] == hashlib.sha256(sent[0]).hexdigest()
    assert exchange['frame_bytes'] == len(sent[0])
    assert exchange['frame_kind'] == 'full'
    assert exchange['frame_reason'] is None
    assert exchange['frame_content_encoding'] == ''


def test_frame_names_the_budget_adjusted_sent_bytes(monkeypatch):
    chat_module = importlib.import_module('lib.llm.chat')
    sent = []

    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {'choices': [{'message': {'content': 'ok'},
                                 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 3}}

    class Lease:
        max_output_tokens = 16

        def prepare_input(self, body):
            self.dispatched = body

        def mark_dispatched(self):
            self.dispatched = True

        def settle(self, usage, unsent=False):
            pass

        def confirm_input(self, successful):
            assert successful is True

    def admit(**counts):
        assert counts['max_output_tokens'] == 128
        return Lease()

    def post(*args, **kwargs):
        sent.append(kwargs['data'])
        return Response()

    monkeypatch.setattr(chat_module, 'http_post', post)
    text, usage = chat_module.chat(
        [{'role': 'user', 'content': 'budgeted'}], model='test-model',
        api_key='test-only', base_url='https://public.example/v1',
        api_protocol='openai', max_tokens=128, temperature=0, max_retries=0,
        model_budget_admit=admit)
    assert text == 'ok'
    # The budget scope re-seals the request with its own output limit; the
    # frame must name those bytes, never the pre-budget request.
    assert json.loads(sent[0])['max_tokens'] == 16
    exchange = usage['_cache_exchange']
    assert exchange['frame_sha256'] == hashlib.sha256(sent[0]).hexdigest()
    assert exchange['frame_bytes'] == len(sent[0])


class _NonstreamReply:
    status_code = 200
    headers = {}

    def json(self):
        return {'choices': [{'message': {'content': 'ok'},
                             'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 4, 'completion_tokens': 1}}


def test_adapter_send_frames_the_exact_relay_bytes(monkeypatch):
    chat_module = importlib.import_module('lib.llm.chat')
    sent = []

    def relay(agent_id, port, path, **kwargs):
        sent.append(kwargs['body'])
        return _NonstreamReply()

    monkeypatch.setattr('lib.desktop.adapter.relay_http', relay)
    text, usage = chat_module.chat(
        [{'role': 'user', 'content': 'adapter'}], model='test-model',
        api_key='ta_test', base_url='http://127.0.0.1:8317/v1',
        adapter={'agent_id': 'agent-1', 'port': 8317}, max_retries=0)

    assert text == 'ok'
    assert len(sent) == 1 and isinstance(sent[0], bytes)
    assert json.loads(sent[0])['model'] == 'test-model'
    exchange = usage['_cache_exchange']
    assert exchange['frame_sha256'] == hashlib.sha256(sent[0]).hexdigest()
    assert exchange['frame_bytes'] == len(sent[0])
    assert exchange['transport'] == 'http'


def test_desktop_egress_send_frames_the_exact_relay_bytes(monkeypatch):
    chat_module = importlib.import_module('lib.llm.chat')
    sent = []

    def egress(url, **kwargs):
        sent.append(kwargs['body'])
        return _NonstreamReply()

    monkeypatch.setattr('lib.llm.egress_recovery.resolve_egress_route',
                        lambda *args, **kwargs: 'agent-desktop')
    monkeypatch.setattr('lib.desktop.egress.egress_http', egress)
    text, usage = chat_module.chat(
        [{'role': 'user', 'content': 'desktop'}], model='test-model',
        api_key='sk-test', base_url='https://public.example/v1',
        max_retries=0)

    assert text == 'ok'
    assert len(sent) == 1 and isinstance(sent[0], bytes)
    assert json.loads(sent[0])['model'] == 'test-model'
    exchange = usage['_cache_exchange']
    assert exchange['frame_sha256'] == hashlib.sha256(sent[0]).hexdigest()
    assert exchange['frame_bytes'] == len(sent[0])
    assert exchange['transport'] == 'http'


@pytest.mark.skipif(not zstd_available(), reason='zstd request codec unavailable')
def test_content_encoded_nonstream_send_compresses_once_and_frames_those_bytes(
        monkeypatch):
    from lib.llm import request_encoding
    from lib.llm import request as request_module
    from lib.llm.request_encoding import ZSTD

    chat_module = importlib.import_module('lib.llm.chat')
    sealed, sent = [], []
    real_seal = request_module.seal_request

    def zstd_seal(*args, **kwargs):
        kwargs['content_encoding'] = ZSTD
        body = real_seal(*args, **kwargs)
        sealed.append(body)
        return body

    original_compress = request_encoding.zstd_compress
    compressions = []

    def counted(data):
        compressions.append(len(data))
        return original_compress(data)

    monkeypatch.setattr('lib.llm.request.seal_request', zstd_seal)
    monkeypatch.setattr(request_encoding, 'zstd_compress', counted)
    monkeypatch.setattr(
        chat_module, 'http_post',
        lambda *args, **kwargs: (sent.append(kwargs['data']) or _NonstreamReply()))
    text, usage = chat_module.chat(
        [{'role': 'user', 'content': 'compressed'}], model='test-model',
        api_key='test-only', base_url='https://public.example/v1',
        api_protocol='openai', max_tokens=64, temperature=0, max_retries=0)

    assert text == 'ok'
    assert len(sealed) == 1
    expected = original_compress(sealed[0].payload)
    assert len(compressions) == 1, 'the request is compressed exactly once'
    assert sent == [expected]
    exchange = usage['_cache_exchange']
    assert exchange['frame_sha256'] == hashlib.sha256(sent[0]).hexdigest()
    assert exchange['frame_bytes'] == len(sent[0])
    assert exchange['frame_content_encoding'] == ZSTD


@pytest.mark.parametrize('controls, expected', [
    ({'effort': 'max', 'thinking': {'type': 'adaptive'}}, 'max'),
    ({'output_config': {'effort': 'high'}}, 'high'),
    ({'reasoning': {'effort': 'low'}}, 'low'),
    ({'reasoning_effort': 'xhigh'}, 'xhigh'),
    ({'effort': 'high', 'reasoning_effort': 'low'}, None),
    ({'effort': 'turbo'}, None),
])
def test_receipt_records_effort_under_every_native_spelling(controls, expected):
    body = {'messages': [{'role': 'user', 'content': 'hi'}], **controls}
    admitted = seal_request(body, model='test-model', input_tokens=1,
                            semantic_id='test')
    assert request_parameter_receipt(body)['reasoning_effort'] == expected
    assert request_parameter_receipt(admitted)['reasoning_effort'] == expected


def test_receipt_reads_only_controls_without_decoding_large_prompt(monkeypatch):
    body = {'messages': [{'role': 'user', 'content': 'private' * 200000}],
            'max_tokens': 128, 'temperature': 0, 'top_p': 0.9}
    admitted = seal_request(body, model='test-model', input_tokens=1,
                            semantic_id='test')

    def forbid_decode(*args, **kwargs):
        pytest.fail('receipt decoded sealed prompt bytes')

    monkeypatch.setattr('lib.llm.request.json.loads', forbid_decode)
    assert request_parameter_receipt(admitted) == request_parameter_receipt(body)
    assert request_parameter_receipt(admitted) == {
        'max_tokens': 128, 'max_completion_tokens': None,
        'temperature': 0, 'top_p': 0.9, 'reasoning_effort': None,
        'thinking_type': None}
