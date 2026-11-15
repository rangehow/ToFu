"""Read-only guard of the browser crash-repro soak proxy.

The soak harness watches the production UI through
``scripts/browser_crash_repro/readonly_proxy.mjs``. Its only safety
mechanism is that writes never reach the upstream: non-GET HTTP is answered
locally and push-socket frames other than subscribe/unsubscribe/ping or the
read-only JSON-RPC allowlist (``lib/control_rpc.py``) are dropped. This test
runs the real proxy against a recording stand-in upstream.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

websockets = pytest.importorskip('websockets')
from websockets.asyncio.server import serve  # noqa: E402
from websockets.datastructures import Headers  # noqa: E402
from websockets.http11 import Response  # noqa: E402

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
PROXY = ROOT / 'scripts' / 'browser_crash_repro' / 'readonly_proxy.mjs'


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _insecure_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


async def _exercise(tmp_path: Path) -> dict:
    seen = {'http': [], 'ws': []}

    async def handler(connection):
        async for message in connection:
            seen['ws'].append(json.loads(message))

    def process_request(connection, request):
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            return None
        seen['http'].append(request.path)
        return Response(200, 'OK', Headers({'Content-Type': 'text/plain'}), b'upstream-ok')

    upstream_port = _free_port()
    proxy_port = _free_port()
    async with serve(handler, '127.0.0.1', upstream_port, process_request=process_request):
        proxy = subprocess.Popen(
            ['node', str(PROXY), '--listen', f'127.0.0.1:{proxy_port}',
             '--upstream', f'http://127.0.0.1:{upstream_port}',
             '--strip-prefix', '/proxy/15000', '--state-dir', str(tmp_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    socket.create_connection(('127.0.0.1', proxy_port), timeout=0.5).close()
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            base = f'https://127.0.0.1:{proxy_port}/proxy/15000'
            context = _insecure_context()

            def fetch(method: str, path: str):
                request = urllib.request.Request(base + path, method=method,
                                                 data=b'{}' if method != 'GET' else None)
                try:
                    with urllib.request.urlopen(request, context=context, timeout=10) as response:
                        return response.status, response.read()
                except urllib.error.HTTPError as error:
                    return error.code, error.read()

            loop = asyncio.get_running_loop()
            get_result = await loop.run_in_executor(None, fetch, 'GET', '/api/v4/health?x=1')
            writes = [await loop.run_in_executor(None, fetch, method, '/api/v4/logs/client')
                      for method in ('POST', 'PUT', 'PATCH', 'DELETE')]
            async with websockets.connect(f'wss://127.0.0.1:{proxy_port}/proxy/15000/api/push?_rid=t-ws1',
                                          ssl=context) as client:
                for frame in (
                    {'action': 'subscribe', 'channel': 'chat', 'taskId': '*'},
                    {'action': 'abort', 'channel': 'chat', 'taskId': 'task-1'},
                    {'jsonrpc': '2.0', 'id': 1, 'method': 'project.browse', 'params': {}},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'conversation.write', 'params': {}},
                    {'action': 'ping', 't': 1},
                ):
                    await client.send(json.dumps(frame))
                deadline = time.monotonic() + 10
                while len(seen['ws']) < 3 and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                await asyncio.sleep(0.3)
        finally:
            proxy.terminate()
            proxy.wait(timeout=10)
    blocked = [json.loads(line) for line in (tmp_path / 'proxy-blocked.jsonl').read_text().splitlines()]
    return {'seen': seen, 'get': get_result, 'writes': writes, 'blocked': blocked}


@pytest.mark.skipif(shutil.which('node') is None or shutil.which('openssl') is None,
                    reason='the proxy needs node and openssl')
def test_proxy_forwards_reads_and_never_forwards_writes(tmp_path):
    result = asyncio.run(_exercise(tmp_path))

    assert result['get'] == (200, b'upstream-ok')
    assert result['seen']['http'] == ['/api/v4/health?x=1'], 'the prefix is stripped and only GET reaches upstream'
    assert [status for status, _ in result['writes']] == [405, 405, 405, 405]

    forwarded = result['seen']['ws']
    assert [frame.get('action') or frame.get('method') for frame in forwarded] == [
        'subscribe', 'project.browse', 'ping']
    blocked = {(row['kind'], row['detail']) for row in result['blocked']}
    assert ('ws', 'abort') in blocked and ('ws', 'conversation.write') in blocked
    assert ('http', 'POST /api/v4/logs/client') in blocked


async def _exercise_overlay(tmp_path: Path) -> dict:
    """Upstream serves production's index and a pong with its build id."""
    overlay = tmp_path / 'vite'
    (overlay / 'assets').mkdir(parents=True)
    (overlay / 'manifest.json').write_text(json.dumps({
        'frontend/src/main.ts': {'file': 'assets/main-NEW456.js', 'isEntry': True},
        'frontend/src/chunk.ts': {'file': 'assets/chunk-AAA.js'},
    }))
    (overlay / 'assets' / 'main-NEW456.js').write_text('export const candidate = 1;\n')
    index = (b'<!doctype html><script type="module" src="/proxy/15000/static/vite/assets/main-OLD123.js"></script>'
             b'<link rel="modulepreload" href="/proxy/15000/static/vite/assets/index-SAME.js">')
    upstream_paths = []

    async def handler(connection):
        await connection.send(json.dumps({'channel': 'system', 'type': 'pong', 't': 1, 'buildId': 'main-OLD123.js'}))
        await connection.send(json.dumps({'channel': 'tasks', 'type': 'notify', 'payload': 'x' * 300}))
        async for _ in connection:
            pass

    def process_request(connection, request):
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            return None
        upstream_paths.append(request.path)
        if request.path == '/':
            return Response(200, 'OK', Headers({'Content-Type': 'text/html; charset=utf-8'}), index)
        return Response(200, 'OK', Headers({'Content-Type': 'text/javascript'}), b'export const production = 1;')

    upstream_port = _free_port()
    proxy_port = _free_port()
    async with serve(handler, '127.0.0.1', upstream_port, process_request=process_request):
        proxy = subprocess.Popen(
            ['node', str(PROXY), '--listen', f'127.0.0.1:{proxy_port}',
             '--upstream', f'http://127.0.0.1:{upstream_port}', '--strip-prefix', '/proxy/15000',
             '--state-dir', str(tmp_path / 'state'), '--frontend-dir', str(overlay)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    socket.create_connection(('127.0.0.1', proxy_port), timeout=0.5).close()
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            base = f'https://127.0.0.1:{proxy_port}/proxy/15000'
            context = _insecure_context()

            def get(path: str) -> bytes:
                with urllib.request.urlopen(base + path, context=context, timeout=10) as response:
                    return response.read()

            loop = asyncio.get_running_loop()
            document = await loop.run_in_executor(None, get, '/')
            entry = await loop.run_in_executor(None, get, '/static/vite/assets/main-NEW456.js')
            shared_chunk = await loop.run_in_executor(None, get, '/static/vite/assets/index-SAME.js')
            async with websockets.connect(f'wss://127.0.0.1:{proxy_port}/proxy/15000/api/push',
                                          ssl=context) as client:
                frames = [json.loads(await client.recv()) for _ in range(2)]
        finally:
            proxy.terminate()
            proxy.wait(timeout=10)
    return {'document': document, 'entry': entry, 'shared': shared_chunk, 'frames': frames,
            'upstream': upstream_paths}


@pytest.mark.skipif(shutil.which('node') is None or shutil.which('openssl') is None,
                    reason='the proxy needs node and openssl')
def test_frontend_overlay_swaps_the_bundle_but_not_the_server(tmp_path):
    result = asyncio.run(_exercise_overlay(tmp_path))

    assert b'static/vite/assets/main-NEW456.js' in result['document']
    assert b'main-OLD123.js' not in result['document']
    assert result['entry'] == b'export const candidate = 1;\n', 'overlay files are served locally'
    assert result['shared'] == b'export const production = 1;', 'files the overlay lacks still come from upstream'
    assert result['upstream'] == ['/', '/static/vite/assets/index-SAME.js']
    pong, notify = result['frames']
    assert pong == {'channel': 'system', 'type': 'pong', 't': 1}, 'the server build id is stripped'
    assert notify['payload'] == 'x' * 300, 'other frames pass unchanged'


async def _exercise_frontend_upstream(tmp_path: Path) -> dict:
    """A candidate server composes the page; the live upstream answers the API and push."""
    live_paths, candidate_paths = [], []

    async def push(connection):
        await connection.send(json.dumps({'channel': 'system', 'type': 'pong', 't': 1, 'buildId': 'main-LIVE.js'}))
        async for _ in connection:
            pass

    def live(connection, request):
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            return None
        live_paths.append(request.path)
        return Response(200, 'OK', Headers({'Content-Type': 'application/json'}), b'{"from":"live"}')

    def candidate(connection, request):
        candidate_paths.append(request.path)
        kind = 'text/html; charset=utf-8' if request.path == '/' else 'text/javascript'
        return Response(200, 'OK', Headers({'Content-Type': kind}), b'candidate:' + request.path.encode())

    live_port, candidate_port, proxy_port = _free_port(), _free_port(), _free_port()
    async with serve(push, '127.0.0.1', live_port, process_request=live), \
            serve(push, '127.0.0.1', candidate_port, process_request=candidate):
        proxy = subprocess.Popen(
            ['node', str(PROXY), '--listen', f'127.0.0.1:{proxy_port}',
             '--upstream', f'http://127.0.0.1:{live_port}', '--strip-prefix', '/proxy/15000',
             '--state-dir', str(tmp_path / 'state'), '--frontend-upstream', f'http://127.0.0.1:{candidate_port}'],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    socket.create_connection(('127.0.0.1', proxy_port), timeout=0.5).close()
                    break
                except OSError:
                    await asyncio.sleep(0.1)
            base = f'https://127.0.0.1:{proxy_port}/proxy/15000'
            context = _insecure_context()

            def fetch(method: str, path: str):
                request = urllib.request.Request(base + path, method=method,
                                                 data=b'{}' if method != 'GET' else None)
                try:
                    with urllib.request.urlopen(request, context=context, timeout=10) as response:
                        return response.status, response.read()
                except urllib.error.HTTPError as error:
                    return error.code, error.read()

            loop = asyncio.get_running_loop()
            reads = {path: await loop.run_in_executor(None, fetch, 'GET', path)
                     for path in ('/', '/static/vite/assets/main-NEW.js', '/api/v4/router?x=1')}
            write = await loop.run_in_executor(None, fetch, 'PATCH', '/api/v4/conversations/worker-1/settings/resolve')
            async with websockets.connect(f'wss://127.0.0.1:{proxy_port}/proxy/15000/api/push',
                                          ssl=context) as client:
                pong = json.loads(await client.recv())
        finally:
            proxy.terminate()
            proxy.wait(timeout=10)
    blocked = [json.loads(line) for line in (tmp_path / 'state' / 'proxy-blocked.jsonl').read_text().splitlines()]
    return {'reads': reads, 'write': write, 'pong': pong, 'live': live_paths, 'candidate': candidate_paths,
            'blocked': blocked}


@pytest.mark.skipif(shutil.which('node') is None or shutil.which('openssl') is None,
                    reason='the proxy needs node and openssl')
def test_frontend_upstream_serves_the_candidate_page_while_api_and_push_stay_live(tmp_path):
    result = asyncio.run(_exercise_frontend_upstream(tmp_path))

    assert result['reads']['/'] == (200, b'candidate:/'), 'the composed page comes from the candidate server'
    assert result['reads']['/static/vite/assets/main-NEW.js'] == (200, b'candidate:/static/vite/assets/main-NEW.js')
    assert result['reads']['/api/v4/router?x=1'] == (200, b'{"from":"live"}')
    assert result['candidate'] == ['/', '/static/vite/assets/main-NEW.js']
    assert result['live'] == ['/api/v4/router?x=1'], 'API reads reach only the live server'
    assert result['write'][0] == 405, 'writes reach neither server'
    assert [(row['kind'], row['detail']) for row in result['blocked']] == [
        ('http', 'PATCH /api/v4/conversations/<conv>/settings/resolve')]
    assert result['pong'] == {'channel': 'system', 'type': 'pong', 't': 1}, 'the live build id is stripped'
