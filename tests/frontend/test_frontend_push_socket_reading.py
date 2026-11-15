"""The Push socket owner publishes honest readings for the network badge.

``push.js`` alone owns the socket: RTT, the per-ping timeout and close
verdicts. Its reading carries ``downReason`` so the badge
(``frontend/src/network-indicator.ts``, specified in its vitest file) can say
why the socket is down instead of calling a reachable server "offline". The
socket offers the page shell's session-bound same-origin proof as a
WebSocket subprotocol, never in the URL, so a gateway that rewrites ``Host``
under a ``/proxy/15000`` prefix does not refuse it and no URL log records it.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..', '..'))
JS_DIR = runtime_sections_dir()

# argv[2]=push.js path.
_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.location = { protocol: 'https:', host: 'ide.example' };
// Path-prefixed gateway: the page lives under /proxy/15000/.
global.apiUrl = (p) => '/proxy/15000' + p;
global.console = { ...console, info() {}, warn() {}, debug() {} };

let _clock = 2_000_000;
Date.now = () => _clock;
let _timeouts = [];
global.setInterval = () => 1;
global.clearInterval = () => {};
global.setTimeout = (fn, ms) => { _timeouts.push({ fn, ms }); return _timeouts.length; };
global.clearTimeout = (id) => { if (id && _timeouts[id-1]) _timeouts[id-1] = { fn: () => {}, ms: -1 }; };

let _sockets = [];
let _lastPingT = null;
function FakeWS(url, protocols) { this.url = url; this.protocols = protocols; this.readyState = 0; _sockets.push(this); }
FakeWS.OPEN = 1; FakeWS.CONNECTING = 0; FakeWS.CLOSING = 2; FakeWS.CLOSED = 3;
FakeWS.prototype.send = function (raw) {
  const f = JSON.parse(raw); if (f.action === 'ping') _lastPingT = f.t;
};
FakeWS.prototype.close = function () { this.readyState = 3; };
FakeWS.prototype.open = function () { this.readyState = 1; this.onopen(); };
FakeWS.prototype.drop = function (code) { this.readyState = 3; this.onclose({ code }); };
global.WebSocket = FakeWS;

global.document = {
  getElementById: (id) => (id === 'tofu-boot-config'
    ? { textContent: JSON.stringify({ entry: 'main', pushOriginProof: '0123abcd' }) }
    : null),
  addEventListener: () => {},
};

eval(fs.readFileSync(process.argv[2], 'utf8') + '\nglobal.__push = _push;');

const out = [];
function check(name, cond, detail) {
  out.push((cond ? 'PASS ' : 'FAIL ') + name + (cond ? '' : ' ' + JSON.stringify(detail)));
}
const readings = [];
pushOnLatency((r) => readings.push(r));
const last = () => readings[readings.length - 1];
const current = () => _sockets[_sockets.length - 1];
const reconnect = () => {
  const timer = [..._timeouts].reverse().find(t => t.ms >= 0 && t.ms <= 30000);
  timer.ms = -1; timer.fn();
};

check('initial_reading_is_connecting',
  last().connected === false && last().downReason === 'connecting', last());

pushConnect();
const url = current().url;
check('url_keeps_proxy_prefix_and_never_carries_the_proof',
  /^wss:\/\/ide\.example\/proxy\/15000\/api\/push\?_rid=[^&]+$/.test(url)
  && !url.includes('0123abcd'), url);
check('proof_travels_as_a_subprotocol',
  JSON.stringify(current().protocols)
    === JSON.stringify(['tofu.push.v1', 'tofu.origin-proof.0123abcd']),
  current().protocols);

// A refused/failed handshake closes before onopen.
current().drop(1006);
check('closed_before_open_is_handshake',
  last().connected === false && last().downReason === 'handshake', last());

reconnect();
current().open();
check('open_publishes_connected_without_waiting_for_pong',
  last().connected === true && last().downReason === null, last());
_clock += 30;
current().onmessage({ data: JSON.stringify({ type: 'pong', t: _lastPingT }) });
check('pong_publishes_rtt', last().connected === true && last().ms === 30, last());

current().drop(1006);
check('closed_after_open_is_dropped',
  last().connected === false && last().downReason === 'dropped', last());

reconnect();
current().open();
const watchdog = [..._timeouts].reverse().find(t => t.ms === 8000);
watchdog.fn();
check('silent_socket_is_timeout',
  last().connected === false && last().downReason === 'timeout'
  && last().state === 'timeout', last());

reconnect();
current().open();
current().drop(1008);
check('policy_close_is_refused',
  last().connected === false && last().downReason === 'refused', last());

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_push_reading_names_why_the_socket_is_down(tmp_path):
    harness = tmp_path / 'push-reading.js'
    harness.write_text(_HARNESS, encoding='utf-8')
    proc = subprocess.run(
        ['node', str(harness), os.path.join(JS_DIR, 'push.js')],
        capture_output=True, text=True, timeout=60,
    )
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'push reading failures:\n' + output
    assert output.count('PASS') == 9, output


def test_network_badge_owns_no_liveness_clock_or_connection():
    with open(os.path.join(ROOT, 'frontend', 'src', 'network-indicator.ts'),
              encoding='utf-8') as handle:
        source = handle.read()
    for forbidden in ('setInterval', 'setTimeout', 'WebSocket', 'fetch('):
        assert forbidden not in source, forbidden
