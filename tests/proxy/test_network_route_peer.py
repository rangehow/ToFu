"""Routed responses name the address their connection actually reached.

A gateway host can resolve to several addresses (your-llm-gateway.example.com answered
10.192.19.176 and 10.176.18.71 in rotating order on 2026-11-12), and Tofu's
pooled connections enter it through any of them. Prompt-cache diagnosis must
be able to tell whether consecutive rounds of one conversation used the same
address (docs/LLM_COST_OPTIMIZATION.md, "Gateway address and cache domains"),
so every routed response carries ``peer`` beside its credential-free route id,
for new and reused keep-alive connections, on each transport.
"""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler

import pytest

from lib import proxy
from tests.support.loopback import LoopbackThreadingHTTPServer

pytestmark = pytest.mark.unit


@pytest.fixture
def direct_routes(monkeypatch):
    for key in ('http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY',
                'HTTPS_PROXY', 'ALL_PROXY', 'no_proxy', 'NO_PROXY'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(proxy, '_proxy_pool', [])
    monkeypatch.setattr(proxy, '_proxy_topology_epoch', 0)
    monkeypatch.setattr(proxy, '_pool_health', {})
    monkeypatch.setattr(proxy, '_pool_choice', {})
    monkeypatch.setattr(proxy, '_cred_cache', {})
    monkeypatch.setattr(proxy, '_bypass_domains', ())
    monkeypatch.setattr(proxy, '_proxy_config', {})
    monkeypatch.setattr(proxy, '_ENV_HTTP_PROXY', '')
    monkeypatch.setattr(proxy, '_ENV_HTTPS_PROXY', '')


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get('content-length') or 0))
            payload = b'data: {"type": "message_stop"}\n\n'
            self.send_response(200)
            self.send_header('content-type', 'text/event-stream')
            self.send_header('content-length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = LoopbackThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{httpd.server_port}/v1/messages', f'127.0.0.1:{httpd.server_port}'
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join()


def test_streamed_requests_response_names_its_peer_on_new_and_reused_connections(direct_routes, server):
    from lib.http_transport.outbound import managed_session
    url, peer = server
    session = managed_session()
    try:
        for _ in range(2):  # the second request reuses the keep-alive connection
            response = session.post(url, data=b'{}', stream=True, timeout=5)
            try:
                assert response.tofu_network_route['routeMode'] == 'direct'
                assert response.tofu_network_route['peer'] == peer
                assert response.content
            finally:
                response.close()
    finally:
        session.close()


def test_async_stream_names_its_peer(direct_routes, server):
    from lib.http_transport.client import async_http_stream, close_async_http_clients
    url, peer = server

    async def scenario():
        try:
            for _ in range(2):
                async with async_http_stream('POST', url, content=b'{}', timeout=5) as stream:
                    assert stream.extensions['tofu_network_route']['peer'] == peer
                    assert await stream.aread()
        finally:
            await close_async_http_clients()

    asyncio.run(scenario())


def test_httpx_client_stream_names_its_peer(direct_routes, server):
    from lib.http_transport.outbound_httpx import managed_httpx_client
    url, peer = server
    with managed_httpx_client(automatic=True) as client:
        for _ in range(2):
            with client.stream('POST', url, content=b'{}') as response:
                assert response.extensions['tofu_network_route']['peer'] == peer
                assert response.read()


def test_route_metadata_omits_an_unknown_peer():
    from lib.http_transport.outbound import route_metadata
    from lib.network_routes import Route
    route = Route('direct', 'direct', 'direct')
    assert 'peer' not in route_metadata(route)
    assert 'peer' not in route_metadata(route, None)
    assert route_metadata(route, '10.0.0.1:443')['peer'] == '10.0.0.1:443'
