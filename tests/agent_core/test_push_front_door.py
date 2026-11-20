"""Python side of the Rust front door's push bridge (tofu.push-bridge/v1).

The Rust hub owns browser sockets; these cases pin what Python must do for
it: mirror subscriptions into PushHub (has_subscriber, ordering ack), forward
publications as the exact bytes a local socket would send, keep owner
filtering and latest-value keys, relay control RPC, and release everything
when the bridge ends. Cross-language behaviour is covered by
packages/tofu-front-rs/push/tests and tests/blackbox/test_blackbox_push*.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from lib.agent_core.push import PushClient, PushHub
from lib.agent_core.push_front_door import (
    BRIDGE_HEADER, FrontDoorBridgeSession, bridge_request_authorized,
    bridge_token)

pytestmark = pytest.mark.unit


class _Headers(dict):
    def getlist(self, name):
        value = self.get(name)
        return [] if value is None else value if isinstance(value, list) else [value]


def test_bridge_handshake_is_default_deny():
    token = bridge_token('secret')
    assert bridge_request_authorized(_Headers({BRIDGE_HEADER: token}), ('127.0.0.1', 5), 'secret')
    assert bridge_request_authorized(_Headers({BRIDGE_HEADER: token}), ('::1', 5, 0, 0), 'secret')
    assert not bridge_request_authorized(_Headers({BRIDGE_HEADER: token}), ('10.0.0.2', 5), 'secret')
    assert not bridge_request_authorized(_Headers({BRIDGE_HEADER: [token, token]}), ('127.0.0.1', 5), 'secret')
    assert not bridge_request_authorized(_Headers({BRIDGE_HEADER: 'nope'}), ('127.0.0.1', 5), 'secret')
    assert not bridge_request_authorized(_Headers({}), ('127.0.0.1', 5), 'secret')
    assert not bridge_request_authorized(_Headers({BRIDGE_HEADER: bridge_token('')}), ('127.0.0.1', 5), '')


def test_token_matches_the_rust_constant():
    # packages/tofu-front-rs/push/src/bridge.rs::tests::token_matches_python_hmac
    assert bridge_token('k') == '74bc21f785ad241f51ccb50e4fc003a848c14264cd24bd369bc6aea78d22af64'


class _Bridge:
    """Drives one session with an inbound queue and captured sends."""

    def __init__(self, hub, admit=None, aborts=None):
        self.inbound: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self._new_send = asyncio.Event()

        async def default_admit(op):
            return {'status': 101, 'userId': '1', 'rid': 'r', 'subprotocol': None}

        def abort(task_id, *, user_id, req_id=''):
            (aborts if aborts is not None else []).append((task_id, user_id, req_id))

        self.session = FrontDoorBridgeSession(
            hub, admit=admit or default_admit, abort=abort,
            encode_control=lambda frame: json.dumps(frame, sort_keys=True))
        self.task = asyncio.get_running_loop().create_task(
            self.session.run(self.inbound.get, self._send))

    async def _send(self, text):
        self.sent.append(json.loads(text))
        self._new_send.set()

    def op(self, **op):
        self.inbound.put_nowait(json.dumps(op))

    async def next(self, kind):
        while True:
            for index, op in enumerate(self.sent):
                if op['op'] == kind:
                    return self.sent.pop(index)
            self._new_send.clear()
            await asyncio.wait_for(self._new_send.wait(), 10)

    async def close(self):
        self.session.disconnect()
        await self.task
        await self.session.aclose()


def test_subscription_mirror_routes_publications_and_releases_on_close():
    async def scenario():
        hub = PushHub()
        bridge = _Bridge(hub)
        await bridge.next('build_id')
        bridge.op(op='subscribe', id=7, channel='notify', taskId='*', userId='1')
        assert (await bridge.next('mirrored'))['id'] == 7
        assert hub.has_subscriber('notify', 1)
        assert not hub.has_subscriber('notify', 2)

        hub.push_event('notify', '__folders__', {'type': 'folders_changed', 'userId': 1, 'é': '中'},
                       user_id=1)
        hub.push_event('notify', '__folders__', {'type': 'foreign'}, user_id=2)
        forwarded = await bridge.next('frame')
        assert forwarded == {
            'op': 'frame', 'channel': 'notify', 'taskId': '__folders__', 'userId': '1',
            'latestKey': None,
            # tofu_workers.json_frames._json_text: what a local socket sends.
            'text': '{"type":"folders_changed","userId":1,"é":"中","channel":"notify","taskId":"__folders__"}',
        }
        hub.push_event('notify', 'x', {'type': 'fact', 'n': 1}, user_id=1, latest_value_key='k')
        latest = await bridge.next('frame')
        assert latest['latestKey'] == 'k'
        assert json.loads(latest['text']) == {'type': 'fact', 'n': 1, 'channel': 'notify', 'taskId': 'x'}
        assert not [op for op in bridge.sent if op['op'] == 'frame']

        bridge.op(op='unsubscribe', channel='notify', taskId='*', userId='1')
        bridge.op(op='subscribe', id=8, channel='chat', taskId='t', userId='1')
        await bridge.next('mirrored')
        assert not hub.has_subscriber('notify', 1)
        assert hub.has_subscriber('chat', 1)
        await bridge.close()
        assert not hub.has_subscriber('chat', 1)
        assert not hub._subscriptions

    asyncio.run(scenario())


def test_admit_open_abort_rpc_and_close_ops():
    async def scenario():
        hub = PushHub()
        aborts = []
        seen = []

        async def admit(op):
            seen.append(op)
            return {'status': 401, 'rid': 'denied'}

        bridge = _Bridge(hub, admit=admit, aborts=aborts)
        bridge.op(op='admit', id=3, headers=[['Host', 'h']], query='', peer=['127.0.0.1', 9],
                  subprotocols=[])
        assert await bridge.next('admitted') == {'op': 'admitted', 'id': 3, 'status': 401, 'rid': 'denied'}
        assert seen[0]['headers'] == [['Host', 'h']]

        bridge.op(op='open', socket=5, userId='1', rid='page')
        bridge.op(op='abort', taskId='t9', userId='1', rid='page')
        bridge.op(op='rpc', socket=5, frame={'jsonrpc': '2.0', 'id': 'r1', 'method': 'no.such'})
        reply = await bridge.next('rpc')
        assert reply['socket'] == 5
        assert json.loads(reply['text']) == {
            'jsonrpc': '2.0', 'id': 'r1', 'error': {'code': -32601, 'message': 'Method not found'}}
        assert aborts == [('t9', '1', 'page')]
        # Frames for an unopened or closed socket are never answered.
        bridge.op(op='rpc', socket=99, frame={'jsonrpc': '2.0', 'id': 'r2', 'method': 'no.such'})
        bridge.op(op='close', socket=5)
        bridge.op(op='rpc', socket=5, frame={'jsonrpc': '2.0', 'id': 'r3', 'method': 'no.such'})
        bridge.op(op='heartbeat')
        bridge.op(op='subscribe', id=9, channel='c', taskId='*', userId='1')
        await bridge.next('mirrored')
        assert not [op for op in bridge.sent if op['op'] == 'rpc']
        await bridge.close()

    asyncio.run(scenario())


def test_shutdown_fence_ends_the_bridge():
    async def scenario():
        hub = PushHub()
        bridge = _Bridge(hub)
        assert hub.register(bridge.session)
        assert hub.close_clients_for_shutdown() == 1
        await asyncio.wait_for(bridge.task, 10)
        await bridge.session.aclose()
        assert hub.is_draining

    asyncio.run(scenario())


def test_ownerless_mirror_is_still_acknowledged():
    async def scenario():
        hub = PushHub()
        bridge = _Bridge(hub)
        bridge.op(op='subscribe', id=1, channel='notify', taskId='*', userId='')
        assert (await bridge.next('mirrored'))['id'] == 1
        assert not hub._subscriptions
        await bridge.close()

    asyncio.run(scenario())


def test_notify_reconnect_requires_resubscription_without_replay(monkeypatch):
    """Paired local/bridge bytes; no Rust socket or runtime is exercised."""
    monkeypatch.setenv('TOFU_RUNTIME_STATE_BACKEND', 'inproc')

    async def scenario():
        local_hub, bridge_hub = PushHub(), PushHub()
        local = PushClient(user_id=1)
        bridge = _Bridge(bridge_hub)
        local_parts, bridge_parts = [], []

        def publish(sequence):
            for hub in (local_hub, bridge_hub):
                hub.push_event('notify', '__folders__',
                               {'type': 'folders_changed', 'sequence': sequence}, user_id=1)

        async def receive_pair():
            local_parts.append(await asyncio.wait_for(local.drain(encoded=True), 10))
            bridge_parts.append((await bridge.next('frame'))['text'])

        try:
            local_hub.subscribe(local, 'notify', '__folders__')
            bridge.op(op='subscribe', id=1, channel='notify', taskId='__folders__', userId='1')
            assert (await bridge.next('mirrored'))['id'] == 1
            publish(0)
            await receive_pair()

            local_hub.unregister(local)
            await local.aclose()
            await bridge.close()
            assert not local_hub.has_subscriber('notify', 1)
            assert not bridge_hub.has_subscriber('notify', 1)
            publish(1)

            local = PushClient(user_id=1)
            bridge = _Bridge(bridge_hub)
            await bridge.next('build_id')
            publish(2)
            local_hub.subscribe(local, 'notify', '__folders__')
            bridge.op(op='subscribe', id=2, channel='notify', taskId='__folders__', userId='1')
            assert (await bridge.next('mirrored'))['id'] == 2
            assert local_hub.has_subscriber('notify', 1)
            assert bridge_hub.has_subscriber('notify', 1)
            assert not [op for op in bridge.sent if op['op'] == 'frame']

            publish(3)
            publish(4)
            await receive_pair()
            await receive_pair()
            expected = [
                '{"type":"folders_changed","sequence":0,"channel":"notify","taskId":"__folders__"}',
                '{"type":"folders_changed","sequence":3,"channel":"notify","taskId":"__folders__"}',
                '{"type":"folders_changed","sequence":4,"channel":"notify","taskId":"__folders__"}',
            ]
            assert local_parts == expected
            assert bridge_parts == expected
            # The bridge sender is FIFO: this acknowledgement follows every
            # publication already queued, including any trailing replay.
            bridge.op(op='subscribe', id=3, channel='notify', taskId='__folders__', userId='1')
            assert (await bridge.next('mirrored'))['id'] == 3
            assert not [op for op in bridge.sent if op['op'] == 'frame']
            assert local._active_event is None
            assert local._queue.empty()
        finally:
            local_hub.unregister(local)
            await local.aclose()
            await bridge.close()
            local_hub.stop()
            bridge_hub.stop()

    asyncio.run(scenario())
