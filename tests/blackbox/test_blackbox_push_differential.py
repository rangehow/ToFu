"""Differential push-frame comparison: one scripted session, two servers.

Runs the same ``/api/push`` script against ``TOFU_BLACKBOX_REFERENCE_URL``
(the Python server) and ``blackbox_url`` (the server under test, e.g. the
Rust front door relaying to that same Python server) and compares every
recorded part byte for byte: handshake outcomes, each frame's exact text and
close codes. Nothing is normalised: the script carries no connection ids or
clocks. ``scripts/blackbox_run.py --server-url URL --reference-url REF``
sets both URLs; without a reference the case is skipped.
"""

from __future__ import annotations

import json
import os

import pytest

from tests.blackbox.test_blackbox_http import CROSS_SITE, credential_free_access, request
from tests.blackbox.websocket_client import HandshakeRefused, PushSocket

pytestmark = pytest.mark.api

REFERENCE_URL_ENV = 'TOFU_BLACKBOX_REFERENCE_URL'
FOLDERS_REORDER = ('/api/v4/folders/reorder', b'{"order": []}')


def _receive(socket: PushSocket) -> bytes:
    kind, value = socket.receive(timeout=30)
    if kind == 'text':
        return b'text ' + value.encode('utf-8')
    return f'{kind} {value}'.encode('ascii')


def record_session(base: str) -> list[tuple[str, bytes]]:
    """The scripted session; each step yields one named part."""
    parts: list[tuple[str, bytes]] = []
    with PushSocket(base, headers={'X-Request-ID': 'push-differential'}) as socket:
        def step(name: str, *frames: str) -> None:
            for frame in frames:
                socket.send_text(frame)
            parts.append((name, _receive(socket)))

        step('ping-int', '{"action":"ping","t":1731234567890}')
        step('ping-float-build-probe', '{"action":"ping","t":1.50,"buildProbe":true}')
        step('ping-unicode', '{"action":"ping","t":"\u00e9\U0001f600\\n"}')
        step('ping-nested', '{"action":"ping","t":{"b":[1e16,0.00001,-0],"a":null}}')
        step('subscribe-then-ping',
             '{"action":"subscribe","channel":"notify","taskId":"__folders__"}',
             '{"action":"ping","t":4}')
        reply = request(base, FOLDERS_REORDER[0], method='POST',
                        headers={'Content-Type': 'application/json'}, body=FOLDERS_REORDER[1])
        parts.append(('reorder-status', str(reply.status).encode('ascii')))
        parts.append(('folders-changed-frame', _receive(socket)))
        step('rpc-method-not-found', '{"jsonrpc":"2.0","id":"d-1","method":"blackbox.no_such"}')
        step('rpc-bad-version', '{"jsonrpc":"1.0","id":2,"method":"x"}')
        step('rpc-invalid-request', '{"jsonrpc":"2.0","id":true,"method":"x"}')
        step('rpc-params-not-object', '{"jsonrpc":"2.0","id":"d-3","method":"project.browse","params":[1]}')
        step('rpc-cancel-is-silent', '{"jsonrpc":"2.0","method":"$/cancelRequest","params":{"id":"zz"}}',
             '{"action":"ping","t":5}')
        step('inert-frames',
             '[1]', '"x"', '{}', '{"action":"abort","channel":"chat","taskId":"differential-missing"}',
             '{"action":"unsubscribe","channel":"notify","taskId":"__folders__"}',
             '{"action":"ping","t":6}')
        reply = request(base, FOLDERS_REORDER[0], method='POST',
                        headers={'Content-Type': 'application/json'}, body=FOLDERS_REORDER[1])
        step('unsubscribed-gets-only-pong', '{"action":"ping","t":7}')
        step('malformed-json-close', '{oops')
    with PushSocket(base) as socket:
        socket.send_text(json.dumps({'pad': 'x' * (64 * 1024)}))
        parts.append(('oversize-close', _receive(socket)))
    with PushSocket(base, headers={'Sec-WebSocket-Protocol': 'tofu.push.v1'}) as socket:
        parts.append(('subprotocol', socket.response_headers.get('sec-websocket-protocol', '').encode()))
    try:
        PushSocket(base, headers=CROSS_SITE).close()
        parts.append(('cross-site', b'101'))
    except HandshakeRefused as refused:
        parts.append(('cross-site', str(refused.status).encode('ascii')))
    return parts


def first_difference(expected: bytes, actual: bytes) -> str:
    """First differing offset with 80 bytes of context on each side."""
    offset = next((index for index, (left, right) in enumerate(zip(expected, actual)) if left != right),
                  min(len(expected), len(actual)))
    window = slice(max(0, offset - 80), offset + 80)
    return f'offset {offset}: expected {expected[window]!r} actual {actual[window]!r}'


def test_push_session_is_byte_identical_to_the_reference(blackbox_url):
    reference = os.environ.get(REFERENCE_URL_ENV, '').strip().rstrip('/')
    if not reference:
        pytest.skip(f'set {REFERENCE_URL_ENV} to the reference (Python) server')
    if reference == blackbox_url:
        pytest.skip('reference and target are the same server')
    if not credential_free_access(blackbox_url) or not credential_free_access(reference):
        pytest.skip('the scripted session needs the open-mode loopback grant on both servers')
    expected = record_session(reference)
    actual = record_session(blackbox_url)
    assert [name for name, _ in actual] == [name for name, _ in expected]
    mismatches = [f'{name}: {first_difference(want, got)}'
                  for (name, want), (_, got) in zip(expected, actual) if want != got]
    assert not mismatches, '\n'.join(mismatches)
