"""Python side of the Rust front door's push bridge (``tofu.push-bridge/v1``).

Responsibility: while the Rust front door (``packages/tofu-front-rs/push``)
owns the browser ``/api/push`` sockets, Python owners keep publishing through
``hub.push_event``. One bridge WebSocket per front door carries:

- Rust -> Python: handshake admission (``admit``; the auth policy stays the
  one in ``routes/push.py``), socket ``open``/``close``, subscription mirror
  transitions (``subscribe``/``unsubscribe``: first and last socket of an
  owner on a ``(channel, taskId)``), ``heartbeat`` (the idle-ping lease
  refresh), ``abort``, ``build_probe`` and JSON-RPC frames (``rpc``) for a
  per-socket ``ControlRpcSession``;
- Python -> Rust: ``admitted``, ``mirrored`` (a first subscription is held
  by the hub; the socket reads its next frame only then), every publication a mirrored subscription
  selects (``frame``, text encoded exactly as a local socket would send it),
  ``build_id`` snapshots and RPC responses (``rpc``).

A mirrored subscription is held in ``PushHub`` by one ``_FrontDoorSubscriber``
per owner, so routing, owner filtering, ``has_subscriber``, the subscription
lease registry and latest-value hand-off run through the hub's unchanged
code paths. The Rust hub routes each forwarded frame to its own sockets.

Entry points: ``bridge_request_authorized`` (handshake check) and
``FrontDoorBridgeSession`` (driven by ``routes/push.py``).
Dependencies: ``lib.agent_core.push``, ``lib.agent_core.push_policy``,
``lib.control_rpc``, ``lib.observability``, ``lib.vite_assets``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
from collections import deque
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from typing import Any

from lib.log import get_logger, task_log_tag

logger = get_logger(__name__)

BRIDGE_PATH = '/api/push/front-door-bridge'
BRIDGE_HEADER = 'X-Tofu-Push-Bridge'
BRIDGE_LABEL = b'tofu.push-bridge/v1'


def bridge_token(secret_key: str) -> str:
    return hmac.new(secret_key.encode('utf-8'), BRIDGE_LABEL,
                    hashlib.sha256).hexdigest()


def _is_loopback(peer) -> bool:
    host = peer[0] if isinstance(peer, (tuple, list)) and peer else peer
    try:
        address = ipaddress.ip_address(str(host or ''))
    except ValueError:
        return False
    mapped = getattr(address, 'ipv4_mapped', None)
    return address.is_loopback or bool(mapped and mapped.is_loopback)


def bridge_request_authorized(headers, peer, secret_key: str) -> bool:
    """Default deny: one matching token from a loopback socket peer."""
    if not secret_key or not _is_loopback(peer):
        return False
    presented = headers.getlist(BRIDGE_HEADER) if hasattr(headers, 'getlist') \
        else [headers.get(BRIDGE_HEADER)]
    if len(presented) != 1 or not presented[0]:
        return False
    return hmac.compare_digest(str(presented[0]).encode('utf-8'),
                               bridge_token(secret_key).encode('ascii'))


class _FrontDoorSubscriber:
    """Stands in, inside ``PushHub``, for one owner's front-door sockets."""

    __slots__ = ('user_id', '_session', '__weakref__')

    def __init__(self, session: 'FrontDoorBridgeSession', user_id: str):
        self.user_id = user_id
        self._session = session

    def enqueue(self, frame: dict) -> None:
        self._session.forward(
            str(frame.get('channel', '')), str(frame.get('taskId', '*')),
            self.user_id, frame, None, None)

    def enqueue_latest(self, stream, key: str, value) -> None:
        # ``value.text()`` is the encoding shared with local sockets.
        self._session.forward(stream[0], stream[1], self.user_id,
                              value.frame, key, value.text())


class _BridgedRpcClient:
    """``ControlRpcSession``'s response port for one front-door socket."""

    __slots__ = ('_session', '_socket_id')

    def __init__(self, session: 'FrontDoorBridgeSession', socket_id: int):
        self._session = session
        self._socket_id = socket_id

    def enqueue_rpc(self, frame: dict) -> bool:
        return self._session.forward_rpc(self._socket_id, frame)


class _Socket:
    __slots__ = ('user_id', 'rid', 'rpc')

    def __init__(self, user_id: str, rid: str):
        self.user_id = user_id
        self.rid = rid
        self.rpc = None


class FrontDoorBridgeSession:
    """One bridge connection's state. Loop-thread only, like ``PushClient``.

    ``admit(op)`` decides a handshake (``routes/push.py``); ``abort(task_id,
    user_id=, req_id=)`` handles a chat abort; ``encode_control(frame)``
    encodes RPC responses as Quart's ``send_json`` would.
    """

    def __init__(self, hub, *,
                 admit: Callable[[dict], Awaitable[dict]],
                 abort: Callable[..., None],
                 encode_control: Callable[[Any], str]):
        from lib.agent_core.push_policy import resolve_push_event_queue_capacity
        self._hub = hub
        self._admit = admit
        self._abort = abort
        self._encode_control = encode_control
        # Reconstructible transport, bounded like one socket's backlog: a
        # wedged front door is disconnected and its browsers reconcile.
        self._capacity = resolve_push_event_queue_capacity()
        self._outbox: deque[tuple[dict, Future | None]] = deque()
        self._wake = asyncio.Event()
        self._connected = True
        self._subscribers: dict[str, _FrontDoorSubscriber] = {}
        self._sockets: dict[int, _Socket] = {}
        self._tasks: set[asyncio.Task] = set()
        self._build_id = None

    # ── Hub-facing (PushHub client protocol) ────────────────────────
    def disconnect(self) -> None:
        """Shutdown fence: end the bridge; the front door releases sockets."""
        self._connected = False
        self._outbox.clear()
        self._wake.set()

    def forward(self, channel: str, task_id: str, user_id: str, frame: dict,
                latest_key: str | None, text: Future | None) -> None:
        if not self._connected:
            return
        if text is None:
            text = self._hub.encode_frame(frame)
        header = {'op': 'frame', 'channel': channel, 'taskId': task_id,
                  'userId': user_id, 'latestKey': latest_key}
        self._enqueue(header, text)

    def forward_rpc(self, socket_id: int, frame: dict) -> bool:
        if not self._connected or socket_id not in self._sockets:
            return False
        self._enqueue({'op': 'rpc', 'socket': socket_id,
                       'text': self._encode_control(frame)}, None)
        return True

    def _enqueue(self, header: dict, text: Future | None) -> None:
        if len(self._outbox) >= self._capacity:
            logger.warning('[Push] front-door bridge backlog full (%d); '
                           'disconnecting the bridge', self._capacity)
            self.disconnect()
            return
        self._outbox.append((header, text))
        self._wake.set()

    # ── Transport loop ─────────────────────────────────────────────
    async def run(self, receive: Callable[[], Awaitable[Any]],
                  send: Callable[[str], Awaitable[None]]) -> None:
        """Serve until either half ends or the bridge is disconnected."""
        self._send_build_id()
        sender = asyncio.create_task(self._sender(send))
        receiver = asyncio.create_task(self._receiver(receive))
        try:
            await asyncio.wait({sender, receiver},
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sender, receiver):
                task.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)

    async def _sender(self, send) -> None:
        while self._connected:
            if not self._outbox:
                self._wake.clear()
                await self._wake.wait()
                continue
            header, text = self._outbox.popleft()
            if text is not None:
                header['text'] = await asyncio.shield(asyncio.wrap_future(text))
            await send(json.dumps(header, ensure_ascii=False,
                                  separators=(',', ':')))

    async def _receiver(self, receive) -> None:
        while self._connected:
            message = await receive()
            try:
                op = json.loads(message)
            except (TypeError, ValueError):
                logger.warning('[Push] front-door bridge sent non-JSON; ignored')
                continue
            if isinstance(op, dict):
                self.handle(op)

    def handle(self, op: dict) -> None:
        kind = op.get('op')
        if kind == 'admit':
            self._spawn(self._answer_admit(op))
        elif kind == 'open':
            self._open(op)
        elif kind == 'close':
            self._close_socket(op.get('socket'))
        elif kind == 'subscribe':
            subscriber = self._subscriber(op.get('userId'))
            if subscriber is not None:
                self._hub.subscribe(subscriber, str(op.get('channel')),
                                    str(op.get('taskId')))
                logger.debug('[Push] Subscribe via front door: channel=%s '
                             'taskId=%s', op.get('channel'),
                             task_log_tag(str(op.get('taskId'))))
            # The socket reads its next frame only after this, as a local
            # socket's hub.subscribe completes before its next receive.
            self._enqueue({'op': 'mirrored', 'id': op.get('id')}, None)
        elif kind == 'unsubscribe':
            subscriber = self._subscribers.get(str(op.get('userId')))
            if subscriber is not None:
                self._hub.unsubscribe(subscriber, str(op.get('channel')),
                                      str(op.get('taskId')))
        elif kind == 'heartbeat':
            try:
                self._hub.refresh_subscriptions()
            except Exception as e:
                logger.debug('[Push] registry heartbeat failed: %s', e)
        elif kind == 'abort':
            self._abort(op.get('taskId'), user_id=op.get('userId'),
                        req_id=str(op.get('rid') or ''))
        elif kind == 'build_probe':
            try:
                from lib.vite_assets import request_vite_build_id_refresh
                request_vite_build_id_refresh()
            except Exception as exc:
                logger.debug('[Push] build probe unavailable: %s', exc)
            self._send_build_id()
        elif kind == 'rpc':
            self._receive_rpc(op)
        else:
            logger.warning('[Push] front-door bridge sent unknown op %r', kind)

    def _spawn(self, coroutine) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer_admit(self, op: dict) -> None:
        try:
            reply = await self._admit(op)
        except Exception as e:
            logger.warning('[Push] front-door admission failed closed: %s', e)
            reply = {'status': 401}
        self._enqueue({'op': 'admitted', 'id': op.get('id'), **reply}, None)

    def _subscriber(self, user_id) -> _FrontDoorSubscriber | None:
        from lib.agent_core.push import _require_owner_user_id
        try:
            owner = _require_owner_user_id(user_id)
        except ValueError:
            logger.warning('[Push] front-door mirror without an owner; ignored')
            return None
        subscriber = self._subscribers.get(owner)
        if subscriber is None:
            subscriber = self._subscribers[owner] = _FrontDoorSubscriber(self, owner)
        return subscriber

    def _open(self, op: dict) -> None:
        from lib.observability import connection_open
        socket_id = op.get('socket')
        if not isinstance(socket_id, int):
            return
        self._sockets[socket_id] = _Socket(str(op.get('userId') or ''),
                                           str(op.get('rid') or ''))
        connection_open('ws', 'push')
        logger.info('[Push] WS connected via front door (socket=%s, user=%s, '
                    'rid=%s)', socket_id, op.get('userId'), op.get('rid'))

    def _close_socket(self, socket_id) -> None:
        from lib.observability import connection_close
        socket = self._sockets.pop(socket_id, None)
        if socket is None:
            return
        if socket.rpc is not None:
            self._spawn(socket.rpc.close())
        connection_close('ws', 'push', 'disconnected')
        logger.info('[Push] WS disconnected via front door (socket=%s, rid=%s)',
                    socket_id, socket.rid)

    def _receive_rpc(self, op: dict) -> None:
        socket_id = op.get('socket')
        socket = self._sockets.get(socket_id)
        if socket is None:
            return
        if socket.rpc is None:
            from lib.control_rpc import ControlRpcSession
            socket.rpc = ControlRpcSession(
                _BridgedRpcClient(self, socket_id),
                user_id=socket.user_id, request_id=socket.rid)
        socket.rpc.receive(op.get('frame'))

    def _send_build_id(self) -> None:
        try:
            from lib.vite_assets import get_vite_build_id
            build_id = get_vite_build_id('main')
        except Exception as exc:  # optional metadata never breaks the bridge
            logger.debug('[Push] build id unavailable: %s', exc)
            build_id = ''
        if build_id != self._build_id:
            self._build_id = build_id
            self._enqueue({'op': 'build_id', 'buildId': build_id or ''}, None)

    async def aclose(self) -> None:
        """Release mirrors, sockets and RPC sessions owned by this bridge."""
        self.disconnect()
        for subscriber in list(self._subscribers.values()):
            self._hub.unregister(subscriber)
        self._subscribers.clear()
        for socket_id in list(self._sockets):
            self._close_socket(socket_id)
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)


__all__ = [
    'BRIDGE_HEADER', 'BRIDGE_LABEL', 'BRIDGE_PATH', 'FrontDoorBridgeSession',
    'bridge_request_authorized', 'bridge_token',
]
