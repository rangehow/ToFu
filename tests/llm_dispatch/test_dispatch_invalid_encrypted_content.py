"""Invalid Responses state stops neutrally; ordinary 400 pairs never reopen."""

import asyncio
import copy
import threading
from collections import Counter

import pytest

from lib.llm import llm_errors as errors
from lib.llm_dispatch import api
from lib.llm_dispatch.slot import Slot

pytestmark = pytest.mark.unit


class _Router:
    def __init__(self, slots):
        self.slots = slots
        self.pairs_seen = []
        self.picks = 0

    def pick_and_reserve(self, **kwargs):
        pairs = set(kwargs.get('exclude_pairs') or ())
        self.pairs_seen.append(pairs)
        self.picks += 1
        for slot in self.slots:
            if (slot.key_name, slot.model) in pairs:
                continue
            if slot.key_name in (kwargs.get('exclude_keys') or ()):
                continue
            if slot.model in (kwargs.get('exclude_models') or ()):
                continue
            slot.record_request()
            return slot
        return None

    def has_capable_slots(self, *args, **kwargs):
        return any((slot.key_name, slot.model) not in (kwargs.get('exclude_pairs') or ())
                   and slot.key_name not in (kwargs.get('exclude_keys') or ())
                   and slot.model not in (kwargs.get('exclude_models') or ())
                   for slot in self.slots)

    def summarize_slots(self, *args, **kwargs):
        return 'synthetic slots'


class _Clock:
    now = 100.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 61.0)


def _slot(key, model='unit-model'):
    return Slot(key_name=key, api_key='synthetic-' + key, model=model,
                protocol='responses', responses_profile='compatible',
                base_url='https://api.example.test/v1', capabilities={'text'})


def _health(slot):
    return {name: getattr(slot, name) for name in (
        'consecutive_errors', 'total_errors', 'contention_errors', 'gateway_errors',
        'cooldown_until', 'cooldown_reason', 'is_available', 'rpm_limit',
        'last_error_time', 'last_error_msg', 'last_success_time',
        'latency_ema', 'ttft_ema', 'throughput_ema')}


def _invalid_state_error():
    try:
        errors._classify_http_error(
            400, '{"error":{"code":"invalid_encrypted_content",'
                 '"param":"input[1].encrypted_content"}}', 'unit-model', '')
    except Exception as error:
        return error
    pytest.fail('error classification returned normally')


@pytest.fixture
def key_health(monkeypatch):
    calls = []
    for name in ('record_rate_limit', 'record_outcome', 'mark_key_exhausted',
                 'record_gateway_error'):
        monkeypatch.setattr('lib.key_stats.' + name,
                            lambda *a, **k: calls.append((a, k)) or False)
    return calls


def _install_transport(monkeypatch, mode, transport):
    if mode == 'chat':
        monkeypatch.setattr('lib.llm.chat', transport)
    elif mode == 'sync':
        monkeypatch.setattr('lib.llm.stream_chat', transport)
    else:
        async def async_transport(*args, **kwargs):
            return transport(*args, **kwargs)
        monkeypatch.setattr('lib.llm.astream.async_stream_chat', async_transport)


def _dispatch(mode, body, **kwargs):
    if mode == 'chat':
        return api.dispatch_chat(body['messages'], extra={'tools': body['tools']}, **kwargs)
    if mode == 'sync':
        return api.dispatch_stream(body, **kwargs)
    return asyncio.run(api.async_dispatch_stream(body, **kwargs))


def _body():
    return {'model': 'unit-model', 'max_tokens': 128, 'messages': [
        {'role': 'user', 'content': 'Continue without repeating completed work'},
        {'role': 'assistant', 'content': 'File saved',
         '_responses_items': [{'type': 'compaction', 'encrypted_content': 'opaque-synthetic-state'}],
         'tool_calls': [{'id': 'done-call', 'type': 'function', 'function': {
             'name': 'write_file', 'arguments': '{"path":"done.txt","content":"saved"}'}}]},
        {'role': 'tool', 'tool_call_id': 'done-call', 'content': 'Saved once'},
    ], 'tools': [{'type': 'function', 'function': {'name': 'write_file',
        'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'},
                       'content': {'type': 'string'}}}}}]}


@pytest.mark.parametrize('mode', ['sync', 'async', 'chat'])
def test_invalid_state_releases_once_without_slot_cycle_or_health_change(monkeypatch, key_health, mode):
    slots = [_slot('primary'), _slot('other'), _slot('fallback', 'fallback-model')]
    router = _Router(slots)
    monkeypatch.setattr(api, 'get_dispatcher', lambda: router)
    failure = _invalid_state_error()
    calls, releases, tools, restarts, retries, attempts = [], [], [], [], [], []
    for slot in slots:
        release = slot.release
        monkeypatch.setattr(slot, 'release', lambda release=release, slot=slot:
                            (releases.append(slot.key_name), release())[-1])
    health_before = [_health(slot) for slot in slots]

    def transport(*args, **kwargs):
        calls.append(kwargs['api_key'])
        raise failure

    _install_transport(monkeypatch, mode, transport)
    body = _body()
    before = copy.deepcopy(body)
    options = {} if mode == 'chat' else dict(
        on_tool_call_ready=tools.append, on_retry=lambda **k: retries.append(k),
        on_attempt_restart=lambda **k: restarts.append(k),
        on_provider_attempt=lambda **k: attempts.append(k))
    with pytest.raises(errors.RequestScopedError) as caught:
        _dispatch(mode, body, max_retries=3, prefer_model='unit-model', **options)
    assert type(caught.value).__name__ == 'InvalidEncryptedContentError'
    assert caught.value is failure
    assert calls == [slots[0].api_key]
    assert router.picks == 1
    assert releases == ['primary']
    assert all(slot.inflight == 0 for slot in slots)
    assert [_health(slot) for slot in slots] == health_before
    assert key_health == []
    assert body == before
    assert tools == restarts == retries == []
    if mode != 'chat':
        assert [event['kind'] for event in attempts] == ['start', 'complete']
        assert attempts[-1]['status'] == 'failed'


@pytest.mark.parametrize('prior_tools', [False, True])
def test_typed_state_error_bypasses_task_model_fallback_preserving_completed_work(
        monkeypatch, key_health, prior_tools):
    from lib.tasks_pkg.llm_fallback import _call

    slots = [_slot('primary'), _slot('other'), _slot('fallback', 'fallback-model')]
    router = _Router(slots)
    monkeypatch.setattr(api, 'get_dispatcher', lambda: router)
    monkeypatch.setattr(_call, '_get_fallback_model', lambda task: 'fallback-model')
    failure = _invalid_state_error()
    calls, tools, rescues = [], [], []

    def transport(body, **kwargs):
        calls.append(body['model'])
        raise failure

    def stream(task, body, **kwargs):
        return api.dispatch_stream(body, on_tool_call_ready=kwargs.get('on_tool_call_ready'))

    monkeypatch.setattr('lib.llm.stream_chat', transport)
    monkeypatch.setattr(_call, 'stream_llm_response', stream)
    monkeypatch.setattr(_call, '_attempt_pool_rescue', lambda *a, **k: rescues.append(1))
    body = _body()
    before = copy.deepcopy(body)
    task = {'id': 'synthetic-invalid-state-task', 'config': {}, 'events': [],
            'events_lock': threading.Lock(), 'content': 'File already saved',
            'thinking': 'Retained prior reasoning',
            'toolRounds': [{'round': 1, 'content': 'Saved once'}]}
    usage, rounds = {}, []
    args = (task, body, 'unit-model', 1, 128, prior_tools, body['tools'],
            body['messages'], 'low', False, usage, rounds)
    if prior_tools:
        result = _call._llm_call_with_fallback(*args, on_tool_call_ready=tools.append)
        assert result['_loop_action'] == 'break'
        assert result['finish_reason'] == 'error'
        assert result['model'] == 'unit-model'
        envelope = task['error']
    else:
        with pytest.raises(errors.RequestScopedError) as caught:
            _call._llm_call_with_fallback(*args, on_tool_call_ready=tools.append)
        assert caught.value is failure
        envelope = caught.value._user_message
    assert envelope['kind'] == 'bad_request'
    assert envelope['retryable'] is False
    assert calls == ['unit-model']
    assert tools == rescues == []
    assert not any(key.startswith('_fallback') for key in task)
    assert task['content'] == 'File already saved'
    assert task['thinking'] == 'Retained prior reasoning'
    assert task['toolRounds'] == [{'round': 1, 'content': 'Saved once'}]
    assert body == before
    assert usage == {} and rounds == []
    assert all(slot.inflight == 0 and slot.total_errors == 0 for slot in slots)
    assert key_health == []


@pytest.mark.parametrize('mode', ['sync', 'async', 'chat'])
def test_bad_request_pair_stays_excluded_after_other_key_429_reset(monkeypatch, key_health, mode):
    rejected, capacity = _slot('rejected'), _slot('capacity')
    router = _Router([rejected, capacity])
    clock = _Clock()
    monkeypatch.setattr(api, 'get_dispatcher', lambda: router)
    monkeypatch.setattr(api, 'time', clock)

    async def sleep(seconds, abort_check=None):
        clock.sleep(seconds)

    monkeypatch.setattr('lib.llm._transport.async_abortable_sleep', sleep)
    calls = Counter()
    rejected_health = _health(rejected)

    def transport(*args, **kwargs):
        key = kwargs['api_key']
        calls[key] += 1
        if key == rejected.api_key:
            raise errors.BadRequestError('ordinary invalid tool schema')
        if calls[key] == 1:
            raise errors.RateLimitError('synthetic capacity refusal', status_code=429)
        return ('ok', {}) if mode == 'chat' else ('ok', 'stop', {})

    _install_transport(monkeypatch, mode, transport)
    result = _dispatch(mode, _body(), prefer_model='unit-model', max_retries=4)
    assert result[0] == 'ok'
    assert calls == {rejected.api_key: 1, capacity.api_key: 2}
    assert clock.now >= 161.0  # the capacity backoff passed the exclusion reset
    pair = (rejected.key_name, rejected.model)
    assert all(pair in pairs for pairs in router.pairs_seen[1:])
    assert all(slot.inflight == 0 for slot in router.slots)
    assert _health(rejected) == rejected_health
