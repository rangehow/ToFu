"""Unit tests for the shared stream-retry helpers in ``lib/llm/_transport.py``.

These three helpers were extracted from the duplicated retry loops in
``lib/llm/stream.py`` (sync) and ``lib/llm/astream.py`` (async), which were
line-for-line identical except for the sleep idiom. The sleep itself stays in
each transport module (so the ``abortable_sleep`` monkeypatch seam the streaming
tests rely on is untouched); only the sleep-free DECISION logic is shared here.
"""

import pytest

from lib.llm._transport import (
    MAX_STREAM_RETRIES,
    apply_model_limit_retry,
    attach_limit_learned,
    prepare_retryable_wait,
)
from lib.llm_errors import AbortedError, RetryableAPIError

pytestmark = pytest.mark.unit


# ── attach_limit_learned ──────────────────────────────────────────────────

def test_attach_limit_learned_noop_when_falsy():
    usage = {'tokens': 5}
    assert attach_limit_learned(usage, None) is usage
    assert 'tokens' in usage and '_model_limit_learned' not in usage


def test_attach_limit_learned_creates_usage_when_none():
    marker = {'model': 'm', 'old_limit': 1, 'new_limit': 2}
    got = attach_limit_learned(None, marker)
    assert got == {'_model_limit_learned': marker}


def test_attach_limit_learned_mutates_existing_usage():
    usage = {'tokens': 3}
    marker = {'model': 'm', 'old_limit': 1, 'new_limit': 2}
    got = attach_limit_learned(usage, marker)
    assert got is usage
    assert usage['_model_limit_learned'] == marker


# ── apply_model_limit_retry ───────────────────────────────────────────────

class _FakeModelLimitErr:
    def __init__(self):
        self.model = 'gpt-x'
        self.requested_limit = 8000
        self.detected_limit = 4096


def test_apply_model_limit_retry_clamps_body_and_returns_marker():
    body = {'max_tokens': 8000}
    err = _FakeModelLimitErr()
    marker = apply_model_limit_retry(body, err, log_prefix='[t]')
    assert body['max_tokens'] == 4096
    assert marker == {'model': 'gpt-x', 'old_limit': 8000, 'new_limit': 4096}


# ── prepare_retryable_wait ────────────────────────────────────────────────

def test_prepare_retryable_wait_returns_positive_wait_on_nonfinal_attempt():
    wait = prepare_retryable_wait(0, RetryableAPIError('boom'), abort_check=None,
                                  log_prefix='[t]')
    assert isinstance(wait, float) and wait > 0


def test_prepare_retryable_wait_raises_original_on_final_attempt():
    err = RetryableAPIError('boom')
    with pytest.raises(RetryableAPIError):
        prepare_retryable_wait(MAX_STREAM_RETRIES, err, abort_check=None,
                               log_prefix='[t]')


def test_prepare_retryable_wait_honors_abort_before_sleep():
    with pytest.raises(AbortedError):
        prepare_retryable_wait(0, RetryableAPIError('boom'),
                               abort_check=lambda: True, log_prefix='[t]')
