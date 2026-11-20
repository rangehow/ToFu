"""Native settlement cache evidence is bounded, joinable and content-free."""
import json

import pytest

import lib.tasks_pkg.native_model_account as native_account
from lib.tasks_pkg.native_model_account import log_cache_exchange

pytestmark = pytest.mark.unit

FRAME_SHA256 = 'c381c85f7c0d2c451f4154bf9233e4de5897e2d153b9c0fb4d72c9bb3ffdc4b5'
MARKER = '[CacheExchange] '


def _task(**overrides):
    task = {'id': 'task-native', 'convId': 'conv-native', 'model': 'wire-model',
            'provider_id': 'owned-provider', 'config': {'model': 'logical-model'}}
    task.update(overrides)
    return task


def _usage(**exchange):
    receipt = {
        'protocol': 'anthropic', 'transport': 'sse', 'verified': True,
        'logical_input_items': 3, 'sent_input_items': 3,
        'incremental': False, 'reused_connection': False,
        'reused_idle_connection': False, 'full_reason': None,
        'frame_sha256': FRAME_SHA256, 'frame_bytes': 1234, 'frame_kind': 'full',
        'frame_reason': None, 'frame_content_encoding': '',
        'provider_input_tokens': 120, 'provider_cached_tokens': 90,
        'prompt': 'SECRET-PROMPT',
    }
    receipt.update(exchange)
    return {'prompt_tokens': 120, 'cache_read_input_tokens': 90,
            '_cache_exchange': receipt}


def _capture(monkeypatch):
    lines = []
    monkeypatch.setattr(native_account.logger, 'info',
                        lambda message, *args: lines.append(message % args))
    return lines


def _payload(line):
    assert line.startswith(MARKER)
    return json.loads(line[len(MARKER):])


def test_native_cache_exchange_line_names_round_and_exact_frame(monkeypatch):
    lines = _capture(monkeypatch)
    assert log_cache_exchange(_task(), 'task-native', 3, _usage()) is True
    assert len(lines) == 1
    record = _payload(lines[0])
    assert record['task'] == 'task-native'
    assert record['conversation'] == 'conv-native'
    assert record['round'] == 3
    assert record['model'] == 'wire-model'
    assert record['provider'] == 'owned-provider'
    exchange = record['exchange']
    assert exchange['protocol'] == 'anthropic'
    assert exchange['transport'] == 'sse'
    assert exchange['frame_sha256'] == FRAME_SHA256
    assert exchange['frame_bytes'] == 1234
    assert exchange['frame_kind'] == 'full'
    assert exchange['frame_reason'] is None
    assert exchange['frame_content_encoding'] == ''
    assert exchange['provider_input_tokens'] == 120
    assert exchange['provider_cached_tokens'] == 90
    assert 'SECRET-PROMPT' not in lines[0]
    assert len(lines[0]) < 2048


@pytest.mark.parametrize('frame_bytes', [True, -1, 2 ** 53, '12', 1.5, None])
def test_malformed_frame_bytes_degrade_to_none(monkeypatch, frame_bytes):
    lines = _capture(monkeypatch)
    assert log_cache_exchange(_task(), 'task-native', 1,
                              _usage(frame_bytes=frame_bytes)) is True
    assert _payload(lines[0])['exchange']['frame_bytes'] is None


def test_out_of_vocabulary_frame_fields_degrade_to_none(monkeypatch):
    """Short but invalid values must not override the bounded receipt."""
    lines = _capture(monkeypatch)
    assert log_cache_exchange(
        _task(), 'task-native', 1,
        _usage(frame_kind='partial', frame_reason='bogus private text',
               frame_content_encoding='br')) is True
    exchange = _payload(lines[0])['exchange']
    assert exchange['frame_kind'] is None
    assert exchange['frame_reason'] is None
    assert exchange['frame_content_encoding'] is None
    assert 'bogus private text' not in lines[0]


def test_oversized_identity_values_degrade_to_none_without_raising(monkeypatch):
    lines = _capture(monkeypatch)
    usage = _usage(frame_sha256='z' * 64, frame_bytes=2 ** 53,
                   frame_kind='k' * 1000, frame_reason={'prompt': 'SECRET-PROMPT'},
                   frame_content_encoding='e' * 1000)
    assert log_cache_exchange(
        _task(convId='c' * 1000, model='m' * 1000, provider_id='p' * 1000,
              config={}),
        't' * 1000, 2, usage) is True
    record = _payload(lines[0])
    assert record['task'] is None
    assert record['conversation'] is None
    assert record['model'] is None
    assert record['provider'] is None
    exchange = record['exchange']
    for key in ('frame_sha256', 'frame_bytes', 'frame_kind',
                'frame_reason', 'frame_content_encoding'):
        assert exchange[key] is None
    assert 'SECRET-PROMPT' not in lines[0]


def test_unvalidated_receipt_writes_nothing(monkeypatch):
    lines = _capture(monkeypatch)
    assert log_cache_exchange(_task(), 'task-native', 1, None) is False
    assert log_cache_exchange(_task(), 'task-native', 1, {'_cache_exchange': 'broken'}) is False
    assert log_cache_exchange(_task(), 'task-native', 1, {
        '_cache_exchange': {'protocol': 'openai', 'transport': 'stdio'},
    }) is False
    assert lines == []


def test_model_falls_back_to_dispatch_and_config_identity(monkeypatch):
    lines = _capture(monkeypatch)
    usage = _usage()
    usage.pop('prompt_tokens')
    usage['_dispatch'] = {'model': 'dispatch-model', 'provider_id': 'dispatch-provider'}
    assert log_cache_exchange({'id': 'task-native', 'convId': 'conv-native',
                               'config': {'model': 'config-model'}},
                              'task-native', 4, usage) is True
    record = _payload(lines[0])
    assert record['model'] == 'dispatch-model'
    assert record['provider'] == 'dispatch-provider'