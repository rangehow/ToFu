#!/usr/bin/env python3
"""search_arxiv transient-failure retry (lib/paper/arxiv.py).

Regression for the integration bug the R4 real-run surfaced: arXiv rate-limits
(HTTP 429) and times out under load, and search_arxiv used to return [] on the
first failure — which silently starved the research chain's harvest seed (0
papers → gate fail → whole chain dead).

Proven here (no network — http_get is monkeypatched):
  1. a transient 429 on the first attempt is retried and the second (200)
     attempt's results are returned;
  2. NEUTER — if retry were removed (max 1 attempt), the 429 would return [];
  3. a PERMANENT failure (e.g. 400) is NOT retried (returns [] immediately,
     no wasted backoff);
  4. all attempts transient → [] after exhausting the budget (no exception).

Run standalone:  python tests/test_paper_arxiv_retry.py
Under pytest:    pytest tests/test_paper_arxiv_retry.py -m unit
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


try:
    import pytest
    pytestmark = [pytest.mark.unit]
except ImportError:
    pytest = None


# A minimal Atom feed with one entry so a 200 yields exactly one result.
_ATOM = b'''<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2301.12345v1</id>
    <title>A Real Paper</title>
    <summary>Body.</summary>
    <published>2023-01-01T00:00:00Z</published>
  </entry>
</feed>'''


class _Resp:
    def __init__(self, status, content=b''):
        self.status_code = status
        self.content = content
        self.text = content.decode('utf-8', 'ignore')

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            err = requests.exceptions.HTTPError(f'{self.status_code} error')
            err.response = self
            raise err


class _Timeout(Exception):
    """Stands in for a read-timeout exception (name contains 'timeout')."""


def _patch_http(sequence):
    """Patch arxiv.http_get to yield from a sequence of (_Resp | Exception).
    Returns (restore, calls_counter_list)."""
    import lib.paper.arxiv as ax
    calls = []
    it = iter(sequence)

    def fake_get(url, **kw):
        calls.append(url)
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return item
    orig = ax.http_get
    ax.http_get = fake_get
    # Neutralize the backoff sleep so the test is fast.
    orig_sleep = ax.time.sleep
    ax.time.sleep = lambda s: None
    def restore():
        ax.http_get = orig
        ax.time.sleep = orig_sleep
    return restore, calls


def test_transient_429_then_success_is_retried():
    import lib.paper.arxiv as ax
    restore, calls = _patch_http([_Resp(429), _Resp(200, _ATOM)])
    try:
        res = ax.search_arxiv('long context kv cache', max_results=5)
        assert len(res) == 1, f'expected 1 result after retry, got {len(res)}'
        assert res[0]['arxiv_id'].startswith('2301.12345'), res[0]['arxiv_id']
        assert len(calls) == 2, f'should have retried once (2 calls), got {len(calls)}'
    finally:
        restore()
    _ok('transient 429 then 200 → retried, second attempt result returned')


def test_no_retry_would_drop_the_result_NEUTER():
    """NEUTER: force the retry budget to 1 → the 429 is not retried → []."""
    import lib.paper.arxiv as ax
    orig_n = ax._ARXIV_SEARCH_RETRIES
    ax._ARXIV_SEARCH_RETRIES = 1
    restore, calls = _patch_http([_Resp(429), _Resp(200, _ATOM)])
    try:
        res = ax.search_arxiv('q', max_results=5)
        assert res == [], 'NEUTER FAILED: with retries=1 a 429 should drop to []'
        assert len(calls) == 1, 'with retries=1 there should be exactly 1 call'
    finally:
        restore()
        ax._ARXIV_SEARCH_RETRIES = orig_n
    _ok('NEUTER: with retry budget 1 the 429 drops the result (retry is what saves it)')


def test_timeout_is_retried():
    import lib.paper.arxiv as ax
    restore, calls = _patch_http([_Timeout('read timed out'), _Resp(200, _ATOM)])
    try:
        res = ax.search_arxiv('q', max_results=5)
        assert len(res) == 1, 'timeout should be retried then succeed'
        assert len(calls) == 2
    finally:
        restore()
    _ok('a read-timeout is treated as transient and retried')


def test_permanent_400_not_retried():
    import lib.paper.arxiv as ax
    restore, calls = _patch_http([_Resp(400), _Resp(200, _ATOM)])
    try:
        res = ax.search_arxiv('q', max_results=5)
        assert res == [], 'a 400 is permanent → [] without consuming the 200'
        assert len(calls) == 1, f'permanent failure must NOT retry, got {len(calls)} calls'
    finally:
        restore()
    _ok('permanent 400 is not retried (fails fast, no wasted backoff)')


def test_all_transient_exhausts_to_empty():
    import lib.paper.arxiv as ax
    restore, calls = _patch_http([_Resp(429), _Resp(429), _Resp(429)])
    try:
        res = ax.search_arxiv('q', max_results=5)
        assert res == [], 'all-transient should exhaust to [] (no exception)'
        assert len(calls) == ax._ARXIV_SEARCH_RETRIES
    finally:
        restore()
    _ok('all attempts transient → [] after exhausting the budget, no exception')


def main():
    print()
    print(_color('═══ search_arxiv Retry Tests ═══', '36'))
    print()
    tests = [
        test_transient_429_then_success_is_retried,
        test_no_retry_would_drop_the_result_NEUTER,
        test_timeout_is_retried,
        test_permanent_400_not_retried,
        test_all_transient_exhausts_to_empty,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
