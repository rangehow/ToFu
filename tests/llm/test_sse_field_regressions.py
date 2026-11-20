"""Malformed provider fields must not escape as Python conversion errors."""
import json

import pytest

from lib.llm.llm_errors import RetryableAPIError
from tests.sse.test_sse_core_parity import _run_sync, _run_async

pytestmark = pytest.mark.unit


def wire(*events):
    return b''.join(b'data: ' + json.dumps(event).encode() + b'\n\n' for event in events)


@pytest.mark.parametrize('run', [_run_sync, _run_async])
@pytest.mark.parametrize('code', ['²', '5٠⁴'])
def test_digit_only_http_status_uses_retryable_fallback(run, code):
    with pytest.raises(RetryableAPIError) as caught:
        run([], raw_chunks=[wire({'error': {'message': 'temporarily unavailable', 'http_code': code}})])
    assert caught.value.status_code == 500


@pytest.mark.parametrize('run', [_run_sync, _run_async])
@pytest.mark.parametrize('index', ['²', '5٠⁴'])
def test_digit_only_tool_index_is_malformed_not_fatal(run, index):
    result = run([], raw_chunks=[wire({'choices': [{'delta': {'tool_calls': [
        {'index': index, 'id': 'call_a', 'function': {'name': 'read_files', 'arguments': '{}'}}
    ]}, 'finish_reason': 'tool_calls'}]})])
    assert result.malformed_frame_count == 1
    assert result.message['tool_calls'][0]['function']['name'] == 'read_files'


@pytest.mark.parametrize('run', [_run_sync, _run_async])
@pytest.mark.parametrize('details', [None, [], 'bad', 7, {'reasoning_tokens': None},
                                     {'reasoning_tokens': '7'}, {'reasoning_tokens': []},
                                     {'reasoning_tokens': True}])
def test_minimax_malformed_reasoning_details_do_not_break_completion(run, details):
    result = run([], model='MiniMax-M2.5', raw_chunks=[wire(
        {'choices': [{'delta': {'content': '<think>plan</think>Answer'}, 'finish_reason': 'stop'}],
         'usage': {'prompt_tokens': 4, 'completion_tokens': 3, 'completion_tokens_details': details}})])
    assert result.message['content'] == 'Answer'
    assert result.message['reasoning_content'] == 'plan'
    assert 'reasoning_tokens' not in result.usage


@pytest.mark.parametrize('run', [_run_sync, _run_async])
@pytest.mark.parametrize('existing', [None, 0, 9])
def test_minimax_preserves_explicit_reasoning_token_field(run, existing):
    result = run([], model='MiniMax-M2.5', raw_chunks=[wire(
        {'choices': [{'delta': {'content': '<think>plan</think>Answer'}, 'finish_reason': 'stop'}],
         'usage': {'reasoning_tokens': existing, 'completion_tokens_details': {'reasoning_tokens': 7}}})])
    assert result.usage['reasoning_tokens'] == existing
