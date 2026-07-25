"""routes/push.py — Unified server-push WebSocket endpoint.

Single global WebSocket per client that multiplexes all real-time events.
Replaces per-task WebSocket, paper report polling, and translation polling.

Protocol:
  Client → Server:
    {action: 'subscribe', channel: 'chat', taskId: '<id>'}
    {action: 'unsubscribe', channel: 'chat', taskId: '<id>'}
    {action: 'abort', channel: 'chat', taskId: '<id>'}

  Server → Client:
    {channel: 'chat', taskId: '<id>', type: 'content_delta', delta: '...'}
    {channel: 'chat', taskId: '<id>', type: 'done', finishReason: 'stop'}
    {channel: 'paper', taskId: '<id>', type: 'progress', ...}
    {channel: 'translate', taskId: '<id>', type: 'done', translated: '...'}
    {channel: 'system', type: 'ping'}
"""

import asyncio
import os

from flask import Blueprint, jsonify, request
from quart import websocket

from lib.api_response import api_error
from lib.log import get_logger
from lib.push import PushClient, hub

logger = get_logger(__name__)

push_bp = Blueprint('push', __name__)


@push_bp.route('/api/push/debug/presence', methods=['POST'])
def debug_presence():
    """Emit synthetic cross-conversation presence frames (DEBUG ONLY).

    Gated OFF by default — only active when ``TOFU_PRESENCE_DEBUG=1``. The
    presence push hub is an in-process singleton, so a standalone script
    cannot light up a live browser by itself; this endpoint runs the registry
    mutations INSIDE the server process so their broadcasts reach connected
    WebSocket clients. Used by ``debug/presence_smoke.py --live`` to eyeball
    the "who's working" strip without orchestrating two real conversations.

    Body: ``{root: "<abs path>", action: "scenario"|"subagents"|"clear"}``.
    ``scenario`` announces two sibling-CONVERSATION peers touching a shared file
    (cross-conversation conflict); ``subagents`` announces ONE conversation with
    two SUB-AGENTS clobbering the same file (within-conversation conflict +
    nested rows); both leave the peers ACTIVE so the strip stays lit. ``clear``
    departs everything.
    """
    if os.environ.get('TOFU_PRESENCE_DEBUG') not in ('1', 'true', 'yes'):
        return api_error('presence debug disabled '
                        '(set TOFU_PRESENCE_DEBUG=1 to enable)', status=403)
    body = request.get_json(silent=True) or {}
    root = (body.get('root') or '').strip()
    action = (body.get('action') or 'scenario').strip()
    if not root:
        return api_error('root is required', status=400)
    from lib import presence
    if action == 'clear':
        presence.depart(root, 'dbg-peer-1')
        presence.depart(root, 'dbg-peer-2')
        presence.depart(root, 'dbg-swarm', agent_id='agent-coder-1')
        presence.depart(root, 'dbg-swarm', agent_id='agent-coder-2')
        presence.depart(root, 'dbg-swarm')
        return jsonify({'ok': True, 'action': 'clear', 'root': root})
    if action == 'subagents':
        # ONE conversation, TWO sub-agents clobbering the SAME file → a
        # within-conversation conflict advisory + nested rows on the strip.
        presence.announce(root, 'dbg-swarm', task_id='dbg-task-3',
                          title='Swarm session', objective='parallel refactor',
                          phase='working')
        for aid in ('agent-coder-1', 'agent-coder-2'):
            presence.announce(root, 'dbg-swarm', agent_id=aid, task_id='dbg-task-3',
                              title='coder', parent_title='Swarm session',
                              phase='working')
            presence.record_files(root, 'dbg-swarm',
                                  [{'path': 'lib/llm/stream.py', 'action': 'patched'}],
                                  agent_id=aid)
        snap = presence.snapshot(root)
        logger.info('[Push] debug presence SUB-AGENT scenario fired root=%s peers=%d',
                    root, len(snap.get('peers') or []))
        return jsonify({'ok': True, 'action': 'subagents', 'root': root,
                        'activePeers': len(snap.get('peers') or [])})
    # scenario: two peers, a shared-file conflict, both left active.
    presence.announce(root, 'dbg-peer-1', task_id='dbg-task-1',
                      title='Refactor the parser', objective='make it ship',
                      phase='working')
    presence.announce(root, 'dbg-peer-2', task_id='dbg-task-2',
                      title='Tune the LLM stream', objective='cut TTFT',
                      phase='working')
    presence.record_files(root, 'dbg-peer-1',
                          [{'path': 'lib/llm/stream.py', 'action': 'patched'}])
    presence.record_files(root, 'dbg-peer-2',
                          [{'path': 'lib/llm/stream.py', 'action': 'patched'}])
    snap = presence.snapshot(root)
    logger.info('[Push] debug presence scenario fired root=%s peers=%d',
                root, len(snap.get('peers') or []))
    return jsonify({'ok': True, 'action': 'scenario', 'root': root,
                    'activePeers': len(snap.get('peers') or [])})


@push_bp.websocket('/api/push')
async def push_ws():
    """Global push channel WebSocket endpoint.

    Resolves ``AuthContext`` at handshake and stashes ``user_id`` on the
    ``PushClient`` so downstream frame handlers scope by owner without
    re-doing auth. Quart's ``before_request`` middleware does NOT fire
    on WS routes, so we resolve inline from ``websocket.cookies`` and
    ``websocket.headers`` — same transports the HTTP gate accepts, but
    read from the WebSocket-scoped globals rather than ``request``.

    Pre-auth / open-mode / bad-token clients still get through (empty
    ``user_id='' → unscoped``); a valid Bearer/cookie token yields the
    real ``AuthContext.user_id`` so multi-tenant snapshots are correctly
    scoped. pt_ab42421158214591.
    """
    # ── pt_ab42421158214591: resolve WS handshake auth ────────────
    _user_id = ''
    try:
        from lib.api_keys import validate_token
        from routes.api_v1.auth import SESSION_COOKIE
        _tok = ''
        # Priority mirrors _extract_bearer_or_cookie() in the HTTP gate:
        # Authorization header > x-api-key > session cookie. Query-string
        # token is not accepted on a WS upgrade (the URL is HTTP-only).
        try:
            _auth_hdr = (websocket.headers.get('Authorization') or '')
            if _auth_hdr.lower().startswith('bearer '):
                _tok = _auth_hdr[7:].strip()
            if not _tok:
                _xapi = (websocket.headers.get('x-api-key') or '').strip()
                if _xapi.startswith(('tofu_live_', 'tofu_admin_')):
                    _tok = _xapi
            if not _tok:
                _cookie = (websocket.cookies.get(SESSION_COOKIE) or '').strip()
                if _cookie.startswith(('tofu_live_', 'tofu_admin_')):
                    _tok = _cookie
        except Exception as _e:
            logger.debug('[Push] WS auth transport read failed: %s', _e)
        if _tok:
            _ctx = validate_token(_tok)
            if _ctx is not None:
                _user_id = getattr(_ctx, 'user_id', '') or ''
    except Exception as _e:
        logger.debug('[Push] WS auth resolve failed (proceeding unscoped): %s',
                     _e)
        _user_id = ''

    client = PushClient(user_id=_user_id)
    hub.register(client)
    logger.info('[Push] WS connected (clients=%d, user=%s)',
                hub.client_count, _user_id or '<unscoped>')

    send_task = None
    recv_task = None

    async def _sender():
        """Drain the client queue and send frames to the WebSocket."""
        try:
            while True:
                frame = await client.drain()
                if frame is None:
                    break
                # A ping means the 30s drain window elapsed with no traffic —
                # use it as the subscription-registry heartbeat so a LIVING
                # subscriber's cross-replica lease (sub:*) never expires under
                # the 90s TTL (design B.5.2, refresh at ~ttl/3).
                if frame.get('type') == 'ping':
                    try:
                        hub.refresh_subscriptions()
                    except Exception as e:
                        logger.debug('[Push] registry heartbeat failed: %s', e)
                await websocket.send_json(frame)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug('[Push] Sender error: %s', e)

    async def _receiver():
        """Receive client commands (subscribe, unsubscribe, abort, ping)."""
        try:
            while True:
                raw = await websocket.receive_json()
                _handle_client_frame(client, raw)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug('[Push] Receiver error: %s', e)

    try:
        send_task = asyncio.create_task(_sender())
        recv_task = asyncio.create_task(_receiver())
        await asyncio.gather(send_task, recv_task)
    except asyncio.CancelledError:
        pass
    finally:
        client.disconnect()
        hub.unregister(client)
        if send_task and not send_task.done():
            send_task.cancel()
        if recv_task and not recv_task.done():
            recv_task.cancel()
        logger.info('[Push] WS disconnected (clients=%d)', hub.client_count)


def _handle_client_frame(client: PushClient, raw) -> None:
    """Dispatch one inbound client command frame.

    Extracted from the ``_receiver`` coroutine so the routing — in particular
    the latency ``ping`` → ``pong`` echo — is directly unit-testable without a
    live WebSocket. Never writes the socket directly: outbound frames are
    ENQUEUED onto the client's queue so ``_sender`` stays the sole writer of
    the ASGI WebSocket (two coroutines writing it concurrently can
    interleave/corrupt frames).
    """
    if not raw or not isinstance(raw, dict):
        return
    action = raw.get('action', '')
    channel = raw.get('channel', '')
    task_id = raw.get('taskId', '*')

    if action == 'subscribe' and channel:
        hub.subscribe(client, channel, task_id)
        logger.debug('[Push] Subscribe: channel=%s taskId=%s', channel, task_id[:8])
        # ── pt_conv_state_ssot P1.5: server-authoritative connect snapshot ──
        # When a client subscribes to the notify wildcard (the sidebar's
        # subscription) send it a one-shot snapshot of the current running-
        # task state so it has authoritative busy info without waiting for
        # the next notify_conv_changed frame or the 25/90s poll fallback.
        # Delivered DIRECTLY to this client's outbound queue — never via
        # hub.push_event, which would fan out to every subscriber (leaking
        # snapshots into unrelated tabs/users) and cross-replica bus.
        if channel == 'notify' and task_id == '*':
            try:
                from lib.agent_core.push import build_conv_state_snapshot
                # pt_ab42421158214591: use the user_id resolved at
                # handshake and stashed on the client. Empty string is
                # the pre-auth default → unscoped snapshot (same as
                # before this change for personal-install / open-mode).
                # A real AuthContext.user_id gives a per-user scoped
                # snapshot that cannot leak sibling tenants' tasks.
                client.enqueue(build_conv_state_snapshot(user_id=client.user_id))
            except Exception as e:
                logger.debug('[Push] connect snapshot enqueue failed: %s', e)
    elif action == 'unsubscribe' and channel:
        hub.unsubscribe(client, channel, task_id)
    elif action == 'abort' and channel == 'chat' and task_id != '*':
        _handle_abort(task_id)
    elif action == 'ping':
        # Round-trip latency probe. Echo the client's timestamp back so the
        # client can compute RTT = now - t. Pure echo (no shared state) → works
        # on whatever replica the socket landed on. Route it through the
        # client's OUTBOUND QUEUE, NOT a direct websocket.send_json: _sender is
        # the sole writer of this socket (it drains the queue), and two
        # coroutines writing the same ASGI WebSocket concurrently can
        # interleave/corrupt frames. QueueFull drops the oldest frame, which is
        # acceptable for a latency probe.
        client.enqueue({'channel': 'system', 'type': 'pong', 't': raw.get('t')})


def _handle_abort(task_id: str):
    """Handle a client abort request for a chat task.

    Chat tasks predate the unified ``TaskRuntime.abort_event`` flag and
    still gate their work loop on ``task['aborted']``. We set both so
    the orchestrator stops regardless of which path it consults.
    """
    from lib.tasks_pkg import tasks, tasks_lock
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return
    task['aborted'] = True
    abort_evt = task.get('abort_event')
    if abort_evt is not None:
        try:
            abort_evt.set()
        except Exception as e:
            logger.debug('[Push] abort_event.set failed: %s', e)
    logger.info('[Push] Client abort for task %s', task_id[:8])
