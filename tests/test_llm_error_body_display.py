#!/usr/bin/env python3
"""tests/test_llm_error_body_display.py — error-body decode + HUD display guard.

Two production display bugs from the 2026-07-25 toio-gateway 400 incident
(yuju-claude-opus-5-evaDaily, request id toio20260725131310115086679KWLlduaj):

  1. Mojibake — the sync ``requests`` path built the error string from
     ``resp.text``. For a ``text/*`` error page without an explicit charset
     requests falls back to ISO-8859-1 (RFC 2616 default), garbling the
     gateway's UTF-8 Chinese body into ``è¯·æ±...`` in BOTH logs and the
     frontend retry HUD (the async httpx path already decoded UTF-8).
  2. Raw-JSON HUD — the retry bubble / error card dumped the whole
     ``{"error":{"message":...}}`` envelope instead of the message text.

Pins ``decode_error_body`` (UTF-8-first decode) and ``summarize_error_body``
(envelope → clean message) plus the ``_classify_http_error`` integration:
exception messages are clean, while the RAW text still drives the pattern
matchers (quota / prompt-too-long / wrapped-overload).
"""

import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.llm_errors import (  # noqa: E402
    PermissionError_,
    PromptTooLongError,
    RateLimitError,
    _classify_http_error,
    decode_error_body,
    summarize_error_body,
)

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_ZH_MESSAGE = '请求失败，请稍后再尝试 (request id: toio20260725131310115086679KWLlduaj)'
_ENVELOPE = ('{"error":{"message":"' + _ZH_MESSAGE + '",'
             '"type":"toio_api_error","param":"","code":null,"status_code":400},'
             '"ext":{"error":{"source":"UPSTREAM_VENDOR","service":"claude-opus-5",'
             '"stage":"downstream_http","upstreamStatus":400}}}')


class _FakeResp:
    """Duck-typed stand-in for the bits of requests.Response the decoder reads."""

    def __init__(self, content, encoding=None, apparent_encoding=None):
        self.content = content
        self.encoding = encoding
        self.apparent_encoding = apparent_encoding


# ══════════════════════════════════════════════════════════
#  decode_error_body
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDecodeErrorBody:

    def test_latin1_declared_utf8_bytes_decodes_chinese(self):
        """THE incident shape: gateway UTF-8 body + latin-1 fallback encoding.

        requests' RFC-2616 default for text/* without charset is ISO-8859-1;
        resp.text then garbles CJK. decode_error_body must prefer UTF-8.
        """
        resp = _FakeResp(_ENVELOPE.encode('utf-8'), encoding='ISO-8859-1')
        out = decode_error_body(resp)
        assert _ZH_MESSAGE in out, (
            f'expected readable Chinese, got mojibake: {out[:80]}')
        # The exact mojibake string seen in production must NOT survive.
        assert 'è¯' not in out

    def test_no_encoding_utf8_bytes(self):
        resp = _FakeResp(_ENVELOPE.encode('utf-8'), encoding=None,
                         apparent_encoding='UTF-8')
        assert _ZH_MESSAGE in decode_error_body(resp)

    def test_declared_utf8(self):
        resp = _FakeResp(_ENVELOPE.encode('utf-8'), encoding='utf-8')
        assert _ZH_MESSAGE in decode_error_body(resp)

    def test_declared_exotic_charset_honored(self):
        body = '网关错误，稍后重试'.encode('gbk')
        resp = _FakeResp(body, encoding='gbk')
        assert decode_error_body(resp) == '网关错误，稍后重试'

    def test_broken_bytes_fall_back_without_raising(self):
        # Not valid UTF-8 (lone high bytes); apparent encoding guess wins.
        resp = _FakeResp(b'caf\xe9 au lait', encoding='ISO-8859-1',
                         apparent_encoding='windows-1252')
        out = decode_error_body(resp)
        assert isinstance(out, str) and out.startswith('caf')

    def test_empty_body(self):
        assert decode_error_body(_FakeResp(b'', encoding='ISO-8859-1')) == ''
        assert decode_error_body(_FakeResp(None, encoding=None)) == ''


# ══════════════════════════════════════════════════════════
#  summarize_error_body
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSummarizeErrorBody:

    def test_openai_envelope_with_api_http_prefix(self):
        raw = f'API HTTP 400: {_ENVELOPE}'
        out = summarize_error_body(raw)
        assert out == f'API HTTP 400: {_ZH_MESSAGE}'
        assert '{' not in out

    def test_bare_envelope(self):
        assert summarize_error_body(_ENVELOPE) == _ZH_MESSAGE

    def test_anthropic_envelope(self):
        raw = ('{"type":"error","error":{"type":"invalid_request_error",'
               '"message":"max_tokens exceeds the model limit: 8192"}}')
        assert summarize_error_body(raw) == 'max_tokens exceeds the model limit: 8192'

    def test_plain_text_passthrough(self):
        assert summarize_error_body('API HTTP 502: <html>Bad Gateway</html>') == \
            'API HTTP 502: <html>Bad Gateway</html>'

    def test_non_envelope_json_passthrough(self):
        # The sankuai 500-wrap shape carries signal in other fields — keep raw.
        raw = '{"status":500,"data":"No matching constant for [529]"}'
        assert summarize_error_body(raw) == raw

    def test_empty_and_none(self):
        assert summarize_error_body('') == ''
        assert summarize_error_body(None) == ''


# ══════════════════════════════════════════════════════════
#  _classify_http_error integration — clean raises, raw patterns
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestClassifyDisplayIntegration:

    def test_generic_400_raises_clean_chinese(self):
        with pytest.raises(Exception) as ei:
            _classify_http_error(400, f'API HTTP 400: {_ENVELOPE}', 'm', '[t]')
        msg = str(ei.value)
        assert _ZH_MESSAGE in msg
        assert '{"error"' not in msg, f'raw JSON leaked into exception: {msg[:120]}'
        assert 'è¯' not in msg

    def test_prompt_too_long_still_detected_through_envelope(self):
        raw = ('API HTTP 400: {"error":{"message":"prompt is too long: '
               '432286 tokens exceeds the model limit"}}')
        with pytest.raises(PromptTooLongError):
            _classify_http_error(400, raw, 'm', '[t]')

    def test_quota_429_still_detected_through_envelope(self):
        raw = ('API HTTP 429: {"error":{"message":"You exceeded your current '
               'quota, check your plan and billing","code":"insufficient_quota"}}')
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(429, raw, 'm', '[t]')
        assert ei.value.is_quota is True

    def test_plain_text_message_byte_preserved(self):
        """Non-envelope bodies must raise with the raw text unchanged —
        byte-parity with pre-fix behaviour for non-JSON gateways."""
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(502, 'API HTTP 502: <html>Bad Gateway</html>',
                                 'm', '[t]')
        assert 'API HTTP 502: <html>Bad Gateway</html>' in str(ei.value)

    def test_permission_error_clean_message(self):
        raw = 'API HTTP 403: {"error":{"message":"key 无权限访问该模型"}}'
        with pytest.raises(PermissionError_) as ei:
            _classify_http_error(403, raw, 'm', '[t]')
        assert str(ei.value) == 'API HTTP 403: key 无权限访问该模型'

    def test_classify_status_error_end_to_end(self):
        """The streaming entry point raises the clean message too."""
        from lib.llm._sse_core import classify_status_error

        class _Dumper:
            enabled = False

        with pytest.raises(Exception) as ei:
            classify_status_error(400, _ENVELOPE, body={'model': 'm'},
                                  log_prefix='[t]', raw_dumper=_Dumper())
        assert str(ei.value) == f'API HTTP 400: {_ZH_MESSAGE}'


# ══════════════════════════════════════════════════════════
#  Static wire pins — the sync call sites must use the decoder
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCallSitePins:

    def test_stream_py_uses_decode_error_body(self):
        src = open(os.path.join(_ROOT, 'lib', 'llm', 'stream.py'),
                   encoding='utf-8').read()
        assert 'decode_error_body(resp)' in src, (
            'stream.py must decode the error body via decode_error_body, '
            'not resp.text (latin-1 mojibake source)')

    def test_chat_py_uses_decode_error_body(self):
        src = open(os.path.join(_ROOT, 'lib', 'llm', 'chat.py'),
                   encoding='utf-8').read()
        assert 'decode_error_body(resp)' in src


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
