"""Black-box behaviour of the ``WS /api/push`` multiplex any Tofu server keeps.

Speaks RFC 6455 over ``blackbox_url`` with the standard library
(``websocket_client.py``) and reads only committed contracts. Expected bytes
are the Python server's wire: control-lane frames are Quart ``send_json``
(``json.dumps`` with sorted keys, ASCII escapes and ``', '`` separators,
``docs/contracts/EVENTS.md``), event frames are compact JSON
(``contracts/events_v1.json`` push frames), refusals happen before the
upgrade, and a server-side release closes with 1001
(``routes/push.py::PUSH_SERVER_RELEASE_CLOSE_CODE``).

The cases need the open-mode loopback grant. The only write is an empty
folder reorder (``POST /api/v4/folders/reorder`` with ``order: []``), which
changes nothing durable and publishes the contracted ``folders_changed`` hint.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.blackbox.test_blackbox_http import CROSS_SITE, credential_free_access, request
from tests.blackbox.websocket_client import HandshakeRefused, PushSocket

pytestmark = pytest.mark.api

PUSH_SUBPROTOCOL = 'tofu.push.v1'


@pytest.fixture
def push(blackbox_url):
    if not credential_free_access(blackbox_url):
        pytest.skip('push socket cases need the open-mode loopback grant')
    with PushSocket(blackbox_url, headers={'X-Request-ID': 'blackbox-push'}) as socket:
        yield socket


def roundtrip_ping(socket: PushSocket, t=1) -> str:
    socket.send_text(json.dumps({'action': 'ping', 't': t}))
    kind, text = socket.receive()
    assert kind == 'text'
    return text


def test_ping_answers_the_control_lane_pong_bytes(push):
    assert roundtrip_ping(push, 1731234567890) == \
        '{"channel": "system", "t": 1731234567890, "type": "pong"}'
    push.send_text('{"action":"ping","t":12.50}')
    assert push.receive() == ('text', '{"channel": "system", "t": 12.5, "type": "pong"}')
    push.send_text('{"action":"ping","t":"\u00e9\U0001f600"}')
    assert push.receive() == ('text', '{"channel": "system", "t": "\\u00e9\\ud83d\\ude00", "type": "pong"}')
    push.send_text('{"action":"ping"}')
    assert push.receive() == ('text', '{"channel": "system", "t": null, "type": "pong"}')


def test_build_probe_pong_carries_the_served_entry(push):
    push.send_text(json.dumps({'action': 'ping', 't': 2, 'buildProbe': True}))
    kind, text = push.receive()
    assert kind == 'text'
    pong = json.loads(text)
    assert text == json.dumps(pong, sort_keys=True)
    assert pong['type'] == 'pong' and pong['t'] == 2
    if 'buildId' in pong:
        assert re.fullmatch(r'main-[A-Za-z0-9_-]+\.js', pong['buildId'])


def test_inert_frames_keep_the_socket_open(push):
    for frame in ('[1, 2]', '"text"', '{}', '{"action": "subscribe"}',
                  '{"action": "subscribe", "channel": "notify", "taskId": null}',
                  '{"action": "unsubscribe", "channel": "never", "taskId": "subscribed"}',
                  '{"action": "abort", "channel": "chat", "taskId": "blackbox-no-such-task"}',
                  '{"action": "no-such-action"}'):
        push.send_text(frame)
    assert roundtrip_ping(push, 3) == '{"channel": "system", "t": 3, "type": "pong"}'


def test_unknown_control_rpc_method_answers_method_not_found(push):
    push.send_text(json.dumps({'jsonrpc': '2.0', 'id': 'bb-1', 'method': 'blackbox.no_such'}))
    assert push.receive() == (
        'text', '{"error": {"code": -32601, "message": "Method not found"}, "id": "bb-1", "jsonrpc": "2.0"}')
    push.send_text(json.dumps({'jsonrpc': '1.0', 'id': 7, 'method': 'x'}))
    assert push.receive() == (
        'text', '{"error": {"code": -32600, "message": "Invalid JSON-RPC version"}, "id": 7, "jsonrpc": "2.0"}')


def test_malformed_json_releases_the_socket_with_going_away(push):
    push.send_text('{not json')
    assert push.receive() == ('close', 1001)


def test_oversized_message_closes_with_message_too_big(push):
    push.send_text(json.dumps({'action': 'ping', 'pad': 'x' * (64 * 1024)}))
    assert push.receive() == ('close', 1009)


def test_published_hint_reaches_a_subscriber_once_subscribe_was_answered(blackbox_url, push):
    push.send_text(json.dumps({'action': 'subscribe', 'channel': 'notify', 'taskId': '__folders__'}))
    # A reply to a later frame proves the subscription is held.
    roundtrip_ping(push)
    reply = request(blackbox_url, '/api/v4/folders/reorder', method='POST',
                    headers={'Content-Type': 'application/json'}, body=b'{"order": []}')
    assert reply.status == 200, reply.body[:200]
    kind, text = push.receive()
    assert kind == 'text'
    frame = json.loads(text)
    assert text == json.dumps(frame, ensure_ascii=False, separators=(',', ':'))
    assert frame['type'] == 'folders_changed'
    assert (frame['channel'], frame['taskId']) == ('notify', '__folders__')
    assert isinstance(frame['userId'], int)
    assert not {'_ownerUserId', '_latestValueKey'} & set(frame)


def test_unsubscribed_socket_receives_no_hint(blackbox_url, push):
    push.send_text(json.dumps({'action': 'subscribe', 'channel': 'notify', 'taskId': '__folders__'}))
    push.send_text(json.dumps({'action': 'unsubscribe', 'channel': 'notify', 'taskId': '__folders__'}))
    roundtrip_ping(push)
    assert request(blackbox_url, '/api/v4/folders/reorder', method='POST',
                   headers={'Content-Type': 'application/json'}, body=b'{"order": []}').status == 200
    assert roundtrip_ping(push, 9) == '{"channel": "system", "t": 9, "type": "pong"}'


def test_offered_push_subprotocol_is_selected(blackbox_url):
    if not credential_free_access(blackbox_url):
        pytest.skip('needs the open-mode loopback grant')
    with PushSocket(blackbox_url, headers={'Sec-WebSocket-Protocol': PUSH_SUBPROTOCOL}) as socket:
        assert socket.response_headers.get('sec-websocket-protocol') == PUSH_SUBPROTOCOL


def test_cross_site_upgrade_is_refused_before_accepting(blackbox_url):
    with pytest.raises(HandshakeRefused) as refused:
        PushSocket(blackbox_url, headers=CROSS_SITE)
    assert refused.value.status == 401


@pytest.mark.slow
def test_idle_socket_receives_the_keepalive_ping(push):
    # PushClient.drain: 30 s without an outbound frame sends the ping.
    assert push.receive(timeout=45) == ('text', '{"channel": "system", "type": "ping"}')
