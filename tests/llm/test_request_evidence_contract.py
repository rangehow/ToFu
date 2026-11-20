"""Contract guard for per-attempt LLM request/cache-exchange evidence.

The wire authority is ``contracts/llm_request_evidence_v1.schema.json``. Real
Python receipts come from ``lib.llm.cache_exchange.cache_exchange_receipt`` and
its log projection ``loggable_cache_exchange``; the native fixture mirrors
``packages/tofu-runtime/src/agent_http.rs::cache_exchange``. Key, enum and bound
drift in either producer must fail here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

from lib.llm.cache_exchange import (
    FULL_REASONS,
    cache_exchange_receipt,
    loggable_cache_exchange,
)
from lib.llm.request import seal_request
from lib.llm.sent_frame import (
    FRAME_CONTENT_ENCODINGS,
    FRAME_KINDS,
    FRAME_TRANSPORTS,
    full_sent_frame,
    sent_frame,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads(
    (ROOT / 'contracts/llm_request_evidence_v1.schema.json').read_text(encoding='utf-8'))
FIXTURE = ROOT / 'tests/fixtures/llm_request_evidence_v1.json'
VALIDATOR = Draft202012Validator(SCHEMA)

PROTOCOLS = ('openai', 'anthropic', 'responses')
REQUIRED_KEYS = frozenset({
    'protocol', 'transport', 'verified', 'logical_input_items',
    'sent_input_items', 'incremental', 'reused_connection',
    'reused_idle_connection', 'full_reason', 'provider_input_tokens',
    'provider_cached_tokens',
})
FRAME_KEYS = frozenset({
    'frame_sha256', 'frame_bytes', 'frame_kind', 'frame_reason',
    'frame_content_encoding',
})
MAX_COUNT = 2**53 - 1
# Exact bytes pinned by agent_http.rs::model_admission_cost.
NATIVE_FRAME = (
    b'{"input":[{"type":"message"},{"type":"function_call"},'
    b'{"type":"function_call_output"}]}'
)
NATIVE_RECEIPT = {
    'protocol': 'responses',
    'transport': 'sse',
    'verified': True,
    'logical_input_items': 3,
    'sent_input_items': 3,
    'incremental': False,
    'reused_connection': False,
    'reused_idle_connection': False,
    'full_reason': None,
    'provider_input_tokens': 12,
    'provider_cached_tokens': 0,
    'frame_sha256': 'c381c85f7c0d2c451f4154bf9233e4de5897e2d153b9c0fb4d72c9bb3ffdc4b5',
    'frame_bytes': 87,
    'frame_kind': 'full',
    'frame_reason': None,
    'frame_content_encoding': '',
}


def admitted(count=3, *, protocol='openai'):
    history = 'input' if protocol == 'responses' else 'messages'
    body = {'model': 'test-model',
            history: [{'role': 'user', 'content': f'item {index}'}
                      for index in range(count)]}
    return seal_request(
        body, model='test-model', protocol=protocol, input_tokens=1,
        semantic_id='evidence-contract')


def usage_for(protocol):
    if protocol == 'responses':
        return {'_wire_responses_input_usage': {
            'input_tokens': 12, 'cache_read': 4, 'cache_write': None,
            'status': {'input_tokens': 'observed', 'cache_read': 'observed',
                       'cache_write': 'null'},
        }}
    if protocol == 'anthropic':
        return {'_wire_anthropic_input_tokens': 12,
                '_wire_anthropic_cached_tokens': 4}
    return {'prompt_tokens': 12, 'completion_tokens': 3,
            'prompt_tokens_details': {'cached_tokens': 4}}


def frame_receipt(protocol='responses'):
    request = admitted(protocol=protocol)
    frame = full_sent_frame(request, transport='sse')
    return cache_exchange_receipt(
        request, usage_for(protocol), transport='sse', verified=True,
        frame=frame)


def assert_valid_receipt(receipt, *, with_frame=True):
    """Validate one real producer receipt and its bounded log projection."""
    assert receipt is not None
    VALIDATOR.validate(receipt)
    expected = REQUIRED_KEYS | (FRAME_KEYS if with_frame else frozenset())
    assert set(receipt) == expected
    logged = loggable_cache_exchange(receipt)
    VALIDATOR.validate(logged)
    assert set(logged) == REQUIRED_KEYS | FRAME_KEYS
    return logged


def test_schema_is_valid_and_freezes_the_producer_key_set():
    Draft202012Validator.check_schema(SCHEMA)
    properties = SCHEMA['properties']
    assert set(properties) == REQUIRED_KEYS | FRAME_KEYS
    assert set(SCHEMA['required']) == REQUIRED_KEYS
    assert set(SCHEMA['dependentRequired']) == FRAME_KEYS


def test_schema_vocabulary_matches_producer_constants():
    properties = SCHEMA['properties']
    assert set(properties['protocol']['enum']) == set(PROTOCOLS)
    assert set(properties['transport']['enum']) == set(FRAME_TRANSPORTS)
    assert set(properties['frame_kind']['enum']) == set(FRAME_KINDS) | {None}
    assert (set(properties['frame_content_encoding']['enum'])
            == set(FRAME_CONTENT_ENCODINGS) | {None})
    assert set(properties['full_reason']['enum']) == set(FULL_REASONS) | {None}
    assert set(properties['frame_reason']['enum']) == set(FULL_REASONS) | {None}


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('with_frame', [False, True], ids=['no-frame', 'frame'])
def test_real_receipts_and_log_projection_match_schema(protocol, with_frame):
    request = admitted(protocol=protocol)
    frame = full_sent_frame(request, transport='sse') if with_frame else None
    receipt = cache_exchange_receipt(
        request, usage_for(protocol), transport='sse', verified=True,
        frame=frame)

    assert receipt is not None
    logged = assert_valid_receipt(receipt, with_frame=with_frame)
    assert receipt['protocol'] == protocol
    assert receipt['transport'] == 'sse'
    assert receipt['verified'] is True
    assert receipt['logical_input_items'] == receipt['sent_input_items'] == 3
    assert receipt['provider_input_tokens'] == 12
    assert receipt['provider_cached_tokens'] == 4
    if with_frame:
        assert receipt['frame_kind'] == 'full'
        assert receipt['frame_reason'] is None
        assert receipt['frame_content_encoding'] == ''
    else:
        assert not any(key.startswith('frame_') for key in receipt)


@pytest.mark.parametrize('kind,sent_items,reason,encoding', [
    ('full', 3, '', ''),
    ('incremental', 1, '', ''),
    ('reset', 3, 'history_changed', ''),
    ('full', 3, '', 'zstd'),
    ('full', 3, '', 'gzip'),
    ('full', 3, '', 'deflate'),
])
def test_frame_vocabulary_is_accepted_by_the_contract(
        kind, sent_items, reason, encoding):
    request = admitted(protocol='responses')
    frame = sent_frame(
        request, b'{"type":"response.create"}', transport='websocket',
        kind=kind, sent_items=sent_items, reason=reason,
        content_encoding=encoding)
    receipt = cache_exchange_receipt(
        request, usage_for('responses'), transport='websocket', verified=True,
        frame=frame)

    VALIDATOR.validate(receipt)
    VALIDATOR.validate(loggable_cache_exchange(receipt))
    assert receipt['frame_kind'] == kind
    assert receipt['frame_content_encoding'] == encoding


def test_websocket_reset_and_nonstream_http_receipts_match_the_contract():
    """Producer shapes from the WS reset and nonstream HTTP test paths."""
    request = admitted(protocol='responses')
    socket_payload = json.dumps({
        'type': 'response.create', 'model': 'test-model',
        'input': [{'type': 'message'}] * 3,
    })
    reset_frame = sent_frame(
        request, socket_payload, transport='websocket', kind='reset',
        reason='provider_reconnect', sent_items=3)
    websocket_receipt = cache_exchange_receipt(
        request, usage_for('responses'), transport='websocket', verified=True,
        frame=reset_frame, reused_connection=True)

    logged = assert_valid_receipt(websocket_receipt)
    assert websocket_receipt['transport'] == 'websocket'
    assert websocket_receipt['frame_kind'] == 'reset'
    assert websocket_receipt['frame_reason'] == 'provider_reconnect'
    assert websocket_receipt['frame_sha256'] == hashlib.sha256(
        socket_payload.encode('utf-8')).hexdigest()
    assert logged['frame_reason'] == 'provider_reconnect'

    http_request = admitted(protocol='openai')
    http_frame = full_sent_frame(http_request, transport='http')
    nonstream_receipt = cache_exchange_receipt(
        http_request, usage_for('openai'), transport='http', verified=True,
        frame=http_frame)

    logged = assert_valid_receipt(nonstream_receipt)
    assert nonstream_receipt['transport'] == 'http'
    assert nonstream_receipt['frame_kind'] == 'full'
    assert nonstream_receipt['frame_reason'] is None
    assert nonstream_receipt['frame_sha256'] == hashlib.sha256(
        http_request.payload).hexdigest()
    assert logged['frame_sha256'] == nonstream_receipt['frame_sha256']


def test_out_of_vocabulary_frame_reason_is_rejected_and_bounded():
    request = admitted(protocol='responses')
    frame = sent_frame(
        request, b'{"type":"response.create"}', transport='websocket',
        kind='reset', reason='private provider text')
    receipt = cache_exchange_receipt(
        request, usage_for('responses'), transport='websocket', verified=True,
        frame=frame)

    assert not VALIDATOR.is_valid(receipt)
    logged = loggable_cache_exchange(receipt)
    VALIDATOR.validate(logged)
    assert logged['frame_reason'] is None


def test_golden_native_style_receipt_matches_the_contract():
    VALIDATOR.validate(NATIVE_RECEIPT)
    VALIDATOR.validate(loggable_cache_exchange(NATIVE_RECEIPT))
    assert set(NATIVE_RECEIPT) == REQUIRED_KEYS | FRAME_KEYS
    assert NATIVE_RECEIPT['transport'] == 'sse'
    assert NATIVE_RECEIPT['frame_kind'] == 'full'
    assert NATIVE_RECEIPT['frame_reason'] is None
    assert NATIVE_RECEIPT['provider_input_tokens'] == 12
    assert NATIVE_RECEIPT['provider_cached_tokens'] == 0


def test_native_golden_fixture_names_the_rust_unit_fixture_bytes():
    fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
    VALIDATOR.validate(fixture)
    assert fixture == NATIVE_RECEIPT
    assert fixture['frame_sha256'] == hashlib.sha256(NATIVE_FRAME).hexdigest()
    assert fixture['frame_bytes'] == len(NATIVE_FRAME)
    assert fixture['logical_input_items'] == fixture['sent_input_items'] == 3


@pytest.mark.parametrize('key,value', [
    ('frame_sha256', 'A' * 64),
    ('frame_sha256', 'a' * 63),
    ('frame_bytes', MAX_COUNT + 1),
    ('frame_bytes', -1),
    ('frame_kind', 'partial'),
    ('transport', 'carrier-pigeon'),
], ids=['uppercase-hash', 'short-hash', 'frame-bytes-overflow',
        'negative-frame-bytes', 'unknown-frame-kind', 'unknown-transport'])
def test_schema_rejects_out_of_contract_values(key, value):
    receipt = frame_receipt()
    receipt[key] = value
    assert not VALIDATOR.is_valid(receipt)


def test_schema_rejects_an_unknown_key():
    receipt = frame_receipt()
    receipt['provider_cost_usd'] = 0.25
    assert not VALIDATOR.is_valid(receipt)


def test_schema_rejects_a_missing_required_key():
    receipt = frame_receipt()
    receipt.pop('verified')
    assert not VALIDATOR.is_valid(receipt)


@pytest.mark.parametrize('key', sorted(FRAME_KEYS))
def test_frame_block_is_atomic(key):
    receipt = frame_receipt()
    receipt.pop(key)
    assert not VALIDATOR.is_valid(receipt)


def test_count_bound_is_inclusive_at_2_pow_53_minus_1():
    receipt = frame_receipt()
    receipt['frame_bytes'] = MAX_COUNT
    VALIDATOR.validate(receipt)