"""The async SSE shell sends the exact frame it hashed.

The sync shell is pinned in ``test_sent_frame.py``; Codex streaming runs
through the async twin (``lib/llm/astream.py::_async_stream_chat_once_impl``),
so its content-encoded send boundary needs the same specification: a zstd body
is compressed exactly once, the very bytes hashed into the sent frame are the
bytes handed to httpx as ``content``, and the final usage receipt carries that
frame. A legacy plain-dict plan must not grow frame evidence.
"""
from __future__ import annotations

import asyncio
import hashlib
import time

import httpx
import pytest

from lib.llm._sse_core import RequestPlan
from lib.llm.cache_exchange import CACHE_EXCHANGE_KEY
from lib.llm.diagnostics import RawSSEDumper
from lib.llm.request import seal_request
from lib.llm.request_encoding import zstd_available, zstd_compress

pytestmark = pytest.mark.unit

_SSE_LINES = [
    'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
    'data: [DONE]',
]
_SSE_BODY = b''.join(line.encode('utf-8') + b'\n\n' for line in _SSE_LINES)


def _admitted(count=2):
    body = {'model': 'test-model',
            'messages': [{'role': 'user', 'content': f'item {index}'}
                         for index in range(count)]}
    return seal_request(
        body, model='test-model', protocol='openai', input_tokens=1,
        semantic_id='async-frame-fixture', content_encoding='zstd')


def _plan(body, *, headers=None):
    return RequestPlan(
        url='http://fake.local/v1/chat/completions',
        hdrs=dict(headers or {}), body=body,
        trace_id='async-frame-trace',
        raw_dumper=RawSSEDumper('test-model', 'async-frame-trace', {}),
        wire_translator=None, t0=time.monotonic())


def _run(monkeypatch, plan, *, handle):
    """Drive the real async shell, with only preparation/egress/client faked."""
    from lib.llm import astream as async_stream
    import lib.desktop.egress as _eg

    monkeypatch.setattr(async_stream, 'prepare_request', lambda *a, **kw: plan)
    monkeypatch.setattr(_eg, 'route_request', lambda url, **kw: 'direct')

    async def execute():
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(async_stream, 'get_async_client', lambda **_: client)
            return await async_stream._async_stream_chat_once_impl(
                plan.body, api_key='sk-test',
                base_url='http://fake.local/v1', log_prefix='[t]')

    return asyncio.run(execute())


@pytest.mark.skipif(not zstd_available(), reason='zstd request codec unavailable')
def test_async_sse_shell_sends_the_hashed_compressed_bytes(monkeypatch):
    from lib.llm import request_encoding

    request = _admitted(2)
    plan = _plan(request, headers={'Content-Encoding': 'zstd'})
    sent = []

    def handle(http_request):
        sent.append(http_request.content)
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                              content=_SSE_BODY)

    original_compress = request_encoding.zstd_compress
    compressions = []

    def counted(data):
        compressions.append(len(data))
        return original_compress(data)

    monkeypatch.setattr(request_encoding, 'zstd_compress', counted)

    result = _run(monkeypatch, plan, handle=handle)

    assert len(compressions) == 1, 'the request must be compressed exactly once'
    assert len(sent) == 1
    expected = original_compress(request.payload)
    assert sent[0] == expected
    exchange = result.usage[CACHE_EXCHANGE_KEY]
    assert exchange['frame_sha256'] == hashlib.sha256(sent[0]).hexdigest()
    assert exchange['frame_bytes'] == len(sent[0])
    assert exchange['frame_kind'] == 'full'
    assert exchange['frame_reason'] is None
    assert exchange['frame_content_encoding'] == 'zstd'
    assert exchange['transport'] == 'sse'
    assert result.message.get('content') == 'ok'


def test_async_sse_shell_invents_no_frame_for_a_legacy_dict_body(monkeypatch):
    from lib.llm import request_encoding

    body = {'model': 'test-model',
            'messages': [{'role': 'user', 'content': 'hi'}]}
    plan = _plan(body)
    sent = []

    def handle(http_request):
        sent.append(http_request.content)
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                              content=_SSE_BODY)

    compressions = []
    original_compress = request_encoding.zstd_compress
    monkeypatch.setattr(
        request_encoding, 'zstd_compress',
        lambda data: (compressions.append(len(data)), original_compress(data))[1])

    result = _run(monkeypatch, plan, handle=handle)

    assert compressions == []
    assert len(sent) == 1
    assert CACHE_EXCHANGE_KEY not in result.usage
    assert result.message.get('content') == 'ok'
