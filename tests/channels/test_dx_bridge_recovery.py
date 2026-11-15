"""bridge.mjs recovery when its own reconnect socket closes (docs/CHANNELS.md C6).

Recorded 2026-11-10 06:43 (DX-RECONNECT): a lost channel's single reconnect
socket closed about a second after it opened, nothing opened another
(autoConnect off), and the pending login() timed out: exit 4, a full relaunch
and a minutes-long SSO step. The stub engine below models the Web SDK surface
the bridge relies on (msg-codec dxSDK.src.js): ``instance.reconnect()`` does
nothing while a socket is connecting or authenticating and otherwise replaces
the socket, the registered login authenticates every newly connected socket,
a closed socket reports ``ConnStatusChange(DISCONNECTED, 'onClose')``, and
login() rejects at its deadline. It counts sockets and logins, so the spec also
pins one session: one new socket per closed one, never a second login or mint.

The bridge keeps the SDK's own reconnect cadence: its autoConnect waits
``RECONNECT_TIME = 2 * 1000`` ms after a DISCONNECTED before it opens the next
socket (msg-codec dxSDK.src.js, src/core/implmulti/login/impljs/connection.ts).
Without it, a far end that closes every socket at once (the shared proxy
refusing the upstream) bought a new socket per close until login()'s 12 s
deadline.
"""
import os

import pytest

from lib.channels.dx.adapter import DxBridge, _BRIDGE_SOURCE
from tests.channels.test_dx_bridge_contract import NODE, STUB_MESSAGES, STUB_MUTEX, STUB_PAYLOAD_CACHE

pytestmark = pytest.mark.unit

STUB_HANDLER = r"""
const MODE = process.env.STUB_MODE || 'recovery-socket-closes';
const DISCONNECTED = 8;
// The engine's login deadline (dx-websdk-runtime.js login(timeoutMs = 12_000)).
const LOGIN_DEADLINE_MS = 12000;
// instant-closes: the far end closes every socket opened this long after the loss at once.
const BURST_MS = Number(process.env.STUB_BURST_MS || 0);
export class BrowserlessDxRuntime {
  constructor() {
    const handlers = new Map();
    const emit = (event, payload) => { for (const h of handlers.get(event) ?? []) h(payload); };
    const counts = { logins: 0, mints: 0, sockets: 0, burstSockets: 0 };
    this.counts = counts;
    let socket = null;  // the one socket: { state: 'connecting' | 'connected' }
    let authenticated = false;
    let waiting = null;  // the pending login's resolver
    let closeNextRecoverySocket = MODE === 'recovery-socket-closes';
    let lostAt = 0;  // when the live channel dropped
    const open = () => {
      counts.sockets += 1;
      const mine = { state: 'connecting' };
      socket = mine;  // replaces (closes) any previous socket, as _useNextConnection does
      if (MODE === 'instant-closes' && Date.now() - lostAt < BURST_MS) {
        counts.burstSockets += 1;
        setTimeout(() => {
          if (socket !== mine) return;
          socket = null;
          emit('ConnStatusChange', [DISCONNECTED, 'onClose']);
        }, 0);
        return;
      }
      setTimeout(() => {
        if (socket !== mine) return;
        if (counts.logins > 1 && closeNextRecoverySocket) {
          // The recovery's own socket closes before it authenticates.
          closeNextRecoverySocket = false;
          socket = null;
          emit('ConnStatusChange', [DISCONNECTED, 'onClose']);
          return;
        }
        mine.state = 'connected';
        authenticated = true;
        waiting?.();
      }, 20);
    };
    this.credentialProvider = { async getCredentials() { return { uid: '1001' }; } };
    this.webSdkRuntime = {
      instance: {
        CONSTANT: { MESSAGE_EVENT: { IM_MESSAGE: 'IM_message', PUB_MESSAGE: 'PUB_MESSAGE' },
                    LOGIN_STATUS: { CONNECTING: 1, CONNECTED: 2, AUTHENTICATING: 5, AUTHENTICATED: 6,
                                    DISCONNECTING: 7, DISCONNECTED } },
        // The SDK's Connection.reconnect(): no second socket while one is connecting or authenticating.
        reconnect() { if (!socket || socket.state !== 'connecting') open(); },
      },
      async login() {
        counts.logins += 1;
        if (counts.logins === 1) {
          counts.mints += 1;
          socket = { state: 'connected' };
          counts.sockets += 1;
          authenticated = true;
          setTimeout(() => {  // the live channel drops
            lostAt = Date.now();
            socket = null;
            authenticated = false;
            emit('ConnStatusChange', [DISCONNECTED, 'onClose']);
          }, 100);
          return { credentials: { uid: '1001' }, events: ['AuthSuccess'] };
        }
        if (!authenticated) {
          await new Promise((resolve, reject) => {
            const timer = setTimeout(() => reject(new Error('DX Web SDK login timeout')), LOGIN_DEADLINE_MS);
            waiting = () => { clearTimeout(timer); waiting = null; resolve(); };
          });
        }
        return { credentials: { uid: '1001' }, events: ['AuthSuccess'] };
      },
      addEventHandler(event, handler) {
        if (!handlers.has(event)) handlers.set(event, new Set());
        handlers.get(event).add(handler);
        return () => handlers.get(event).delete(handler);
      },
    };
  }
  async handle(request) {
    if (request.action === 'get_current_uid') return { action: request.action, ok: true, uid: '1001', ...this.counts };
    return { action: request.action, ok: false, error: { code: 'unsupported_browserless_action' } };
  }
}
"""


# The Web SDK's reconnect delay (connection.ts RECONNECT_TIME = 2 * 1000): one socket per this interval.
SDK_RECONNECT_TIME_MS = 2000


@pytest.fixture(autouse=True)
def web_channel_lock(tmp_path, monkeypatch):
    """This test's own Web channel lock directory, never the host's."""
    monkeypatch.setenv('DX_BROWSERLESS_WEB_LOCK_DIR', str(tmp_path / 'web-channel.lock.d'))
    monkeypatch.delenv('DX_BROWSERLESS_WEB_LOCK_TIMEOUT_MS', raising=False)


@pytest.fixture
def engine(tmp_path):
    if not os.path.exists(NODE):
        pytest.skip('node is not available')
    root = tmp_path / 'engine'
    (root / 'api').mkdir(parents=True)
    (root / 'handler.js').write_text(STUB_HANDLER)
    (root / 'api' / 'messages.js').write_text(STUB_MESSAGES)
    (root / 'web-channel-mutex.js').write_text(STUB_MUTEX)
    (root / 'payload-cache.js').write_text(STUB_PAYLOAD_CACHE)
    return str(root)


def _statuses(bridge, count):
    seen = []
    while len(seen) < count:
        item = bridge.events.get(timeout=10)
        if item is None:
            break
        if item.get('type') == 'status':
            seen.append(item['status'])
    return seen


def test_a_closed_recovery_socket_is_reconnected_within_the_same_login(engine):
    bridge = DxBridge([NODE, _BRIDGE_SOURCE.path(), '--engine', engine],
                      env={**os.environ, 'STUB_MODE': 'recovery-socket-closes'}, cwd=engine)
    try:
        assert _statuses(bridge, 3) == ['connected', 'disconnected', 'connected']
        assert bridge.alive()
        counts = bridge.request('whoami')['result']
        # One session: the re-armed transport served the same pending login (no second login or
        # credential mint), with exactly one new socket per closed one.
        assert (counts['logins'], counts['mints']) == (2, 1)
        assert counts['sockets'] == 3
    finally:
        assert bridge.close(5)
    assert bridge.returncode == 0


def test_a_burst_of_instant_far_end_closes_costs_one_socket_per_sdk_reconnect_time(engine):
    burst_ms = 3000
    bridge = DxBridge([NODE, _BRIDGE_SOURCE.path(), '--engine', engine],
                      env={**os.environ, 'STUB_MODE': 'instant-closes', 'STUB_BURST_MS': str(burst_ms)},
                      cwd=engine)
    try:
        assert _statuses(bridge, 3) == ['connected', 'disconnected', 'connected']
        counts = bridge.request('whoami')['result']
        assert (counts['logins'], counts['mints']) == (2, 1)
        # Every socket the far end closed at once was opened at least the SDK's reconnect time
        # after the previous one; the first socket after the burst authenticated the same login.
        assert 1 <= counts['burstSockets'] <= burst_ms // SDK_RECONNECT_TIME_MS + 1
        assert counts['sockets'] == 1 + counts['burstSockets'] + 1
    finally:
        assert bridge.close(5)
    assert bridge.returncode == 0
