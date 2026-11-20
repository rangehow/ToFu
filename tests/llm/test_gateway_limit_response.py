"""tests/llm/test_gateway_limit_response.py — Text-shaped gateway throttle replies.

Some gateways (CatPaw ``mcli``) answer a rate-limited request with HTTP 200 and
a normal ``finish_reason=stop`` chat body whose entire content is the hourly
throttle notice, instead of HTTP 429. Without a dedicated stream state the
dispatcher counted a rate-limit as a verified success, never cooled the key,
and kept a broken endpoint sticky. These tests pin:

  - ``is_gateway_limit_response`` matches only SHORT, tool-call-free, stop
    bodies that contain a known limit phrase (never a genuine long answer);
  - ``classify_provider_stream_state`` routes the signal to
    ``GATEWAY_LIMIT_RESPONSE`` so the slot cools and rotates;
  - a real model answer that merely mentions throttling is never classified
    as a limit (the length gate is the primary false-positive guard).
"""

from __future__ import annotations

import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.llm.stream_result import (
    ProviderStreamState,
    classify_provider_stream_state,
    is_gateway_limit_response,
)


class IsGatewayLimitResponseTest(unittest.TestCase):

    def test_matches_short_limit_notice(self):
        self.assertTrue(is_gateway_limit_response(
            '当前小时请求过于频繁', 'stop', 0))
        self.assertTrue(is_gateway_limit_response(
            'rate limit exceeded, retry later', 'stop', 0))
        self.assertTrue(is_gateway_limit_response(
            'Too many requests', 'stop', 0))

    def test_rejects_tool_call_finish(self):
        self.assertFalse(is_gateway_limit_response(
            '当前小时请求过于频繁', 'stop', 1))

    def test_rejects_non_stop_finish(self):
        self.assertFalse(is_gateway_limit_response(
            '当前小时请求过于频繁', 'length', 0))
        self.assertFalse(is_gateway_limit_response(
            '当前小时请求过于频繁', 'tool_calls', 0))

    def test_rejects_long_genuine_answer(self):
        # A real model answer that mentions throttling must never match.
        long_answer = (
            'The provider returned "rate limit exceeded" because ' + ('x' * 200)
        )
        self.assertFalse(is_gateway_limit_response(long_answer, 'stop', 0))

    def test_rejects_empty(self):
        self.assertFalse(is_gateway_limit_response('', 'stop', 0))
        self.assertFalse(is_gateway_limit_response(None, 'stop', 0))


class ClassifyGatewayLimitTest(unittest.TestCase):

    def _classify(self, **kw):
        kw.setdefault('aborted', False)
        kw.setdefault('saw_finish_reason', True)
        kw.setdefault('malformed_frame_count', 0)
        kw.setdefault('empty_response', False)
        kw.setdefault('tool_payload_missing', False)
        return classify_provider_stream_state(**kw)

    def test_gateway_limit_state(self):
        self.assertEqual(
            self._classify(gateway_limit_response=True),
            ProviderStreamState.GATEWAY_LIMIT_RESPONSE)

    def test_default_is_provider_finished(self):
        self.assertEqual(
            self._classify(), ProviderStreamState.PROVIDER_FINISHED)

    def test_failure_priority_beats_gateway_limit(self):
        # A genuine transport failure (no finish frame) outranks the heuristic.
        self.assertEqual(
            self._classify(saw_finish_reason=False,
                           gateway_limit_response=True),
            ProviderStreamState.PREMATURE_CLOSE)


class GatewayLimitStreamTest(unittest.TestCase):

    def test_limit_notice_without_usage_settles_as_gateway_limit(self):
        from lib.llm._sse_core import SSEAccumulator
        from lib.llm._sse_stream import SSEByteStream

        class Dumper:
            def line(self, *_args, **_kwargs):
                pass

            payload = dump_anomaly = finish = line

        acc = SSEAccumulator({'model': 'm', 'max_tokens': 8}, 'trace', Dumper(), None, 0.0)
        stream = SSEByteStream(acc)
        stream.feed(
            'data: {"choices":[{"delta":{"content":"当前小时请求过于频繁"}}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            'data: [DONE]\n\n'.encode())
        stream.finalize(interrupted=False)
        result = acc.finalize()
        self.assertEqual(result.state, ProviderStreamState.GATEWAY_LIMIT_RESPONSE)
        self.assertEqual(result.usage['trace_id'], 'trace')


if __name__ == '__main__':
    unittest.main()
