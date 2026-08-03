#!/usr/bin/env python3
"""tests/test_desktop_pair_agent.py — agent-side pairing + discovery (P3).

Covers the pure logic of lib/desktop_agent/_pair.py: the pair-exchange
wire client (success / invalid / rate-limited / transport failures), the
LAN broadcast probe (magic out, verified URLs back), the ssh-config
parser, the self-tunnel lifecycle (kept on success, killed on failure),
and the ladder's rung order + short-circuit. The tk dialog itself is
deliberately untested (no display on CI), consistent with
prompt_connect_line's existing coverage.
"""

import socket as _socket
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.desktop_agent import _pair  # noqa: E402

pytestmark = pytest.mark.unit


# ── Fakes ────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, body=None, raise_json=False):
        self.status_code = status
        self._body = body
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError('not json')
        return self._body


class _FakeSock:
    """Scripted UDP socket: returns queued (data, addr) then timeouts."""

    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.sent = []
        self.opts = []
        self.closed = False
        self.timeout = None

    def setsockopt(self, *a):
        self.opts.append(a)

    def settimeout(self, t):
        self.timeout = t

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def recvfrom(self, _n):
        if self.scripted:
            return self.scripted.pop(0)
        raise _socket.timeout()

    def close(self):
        self.closed = True


class _FakePopen:
    def __init__(self, poll_seq):
        self._poll_seq = list(poll_seq)
        self.killed = False
        self.cmd = None

    def poll(self):
        if self._poll_seq:
            return self._poll_seq.pop(0)
        return None

    def kill(self):
        self.killed = True


# ── exchange_pair_code ───────────────────────────────────────────────────

def _patch_post(monkeypatch, handler):
    import requests
    monkeypatch.setattr(requests, 'post', handler)


def test_exchange_success_returns_token(monkeypatch):
    seen = {}

    def fake_post(url, json=None, timeout=None, proxies=None):
        seen['url'] = url
        seen['json'] = json
        return _Resp(201, {'ok': True, 'token': 'tok-123', 'id': 'k1'})

    _patch_post(monkeypatch, fake_post)
    ok, val = _pair.exchange_pair_code('http://srv:15000/', '482916',
                                       name='box', platform='win32')
    assert ok is True and val == 'tok-123'
    assert seen['url'] == 'http://srv:15000/api/desktop/pair'
    assert seen['json']['code'] == '482916'
    assert seen['json']['name'] == 'box'
    assert seen['json']['platform'] == 'win32'


def test_exchange_invalid_code_is_409(monkeypatch):
    _patch_post(monkeypatch, lambda *a, **k: _Resp(409, {'ok': False}))
    ok, val = _pair.exchange_pair_code('http://srv', '000000')
    assert (ok, val) == (False, 'invalid_code')


def test_exchange_rate_limited_is_429(monkeypatch):
    _patch_post(monkeypatch, lambda *a, **k: _Resp(429, {'ok': False}))
    ok, val = _pair.exchange_pair_code('http://srv', '000000')
    assert (ok, val) == (False, 'rate_limited')


def test_exchange_other_http_status(monkeypatch):
    # A proxy/SSO edge answering 401: the ADDRESS is wrong, not the code —
    # it must surface as http_401, never as invalid_code.
    _patch_post(monkeypatch, lambda *a, **k: _Resp(401, {'error': 'x'}))
    ok, val = _pair.exchange_pair_code('http://proxy', '000000')
    assert (ok, val) == (False, 'http_401')


def test_exchange_unreachable_and_timeout(monkeypatch):
    import requests

    def raise_conn(*a, **k):
        raise requests.exceptions.ConnectionError()

    def raise_to(*a, **k):
        raise requests.exceptions.ConnectTimeout()

    _patch_post(monkeypatch, raise_conn)
    assert _pair.exchange_pair_code('http://srv', '1') == (False, 'unreachable')
    _patch_post(monkeypatch, raise_to)
    assert _pair.exchange_pair_code('http://srv', '1') == (False, 'timeout')


def test_exchange_bad_response_shapes(monkeypatch):
    _patch_post(monkeypatch, lambda *a, **k: _Resp(201, {'ok': True}))  # no token
    assert _pair.exchange_pair_code('http://srv', '1') == (False, 'bad_response')
    _patch_post(monkeypatch, lambda *a, **k: _Resp(201, raise_json=True))
    assert _pair.exchange_pair_code('http://srv', '1') == (False, 'bad_response')


def test_exchange_rejects_bad_inputs():
    assert _pair.exchange_pair_code('not-a-url', '123456')[0] is False
    assert _pair.exchange_pair_code('http://srv', '')[0] is False


# ── lan_probe ────────────────────────────────────────────────────────────

def test_lan_probe_broadcasts_magic_and_keeps_verified(monkeypatch):
    url_a = 'http://192.168.1.20:15000'
    url_b = 'http://192.168.1.21:15000'
    scripted = [(b'\x00' * 16 + url_a.encode(), ('192.168.1.20', 15001)),
                (b'\x00' * 16 + url_b.encode(), ('192.168.1.21', 15001)),
                (b'too-short', ('192.168.1.22', 15001))]
    sock = _FakeSock(scripted)
    monkeypatch.setattr(_pair, 'probe_server',
                        lambda url, timeout=0: (url == url_a, ''))
    found = _pair.lan_probe(timeout=0.05,
                            _factory=lambda *a, **k: sock)
    assert found == [url_a]
    assert sock.sent and sock.sent[0][0] == _pair._LAN_MAGIC
    assert sock.sent[0][1][1] == 15001
    assert any(o[1] == _socket.SO_BROADCAST for o in sock.opts)
    assert sock.closed is True


def test_lan_probe_empty_when_nothing_answers(monkeypatch):
    sock = _FakeSock([])
    monkeypatch.setattr(_pair, 'probe_server', lambda url, timeout=0: (True, ''))
    assert _pair.lan_probe(timeout=0.02, _factory=lambda *a, **k: sock) == []


# ── ssh_config_hosts ─────────────────────────────────────────────────────

def test_ssh_config_hosts_skips_wildcards_and_defaults(tmp_path):
    cfg = tmp_path / 'config'
    cfg.write_text(
        '# comment\n'
        'Host *\n'
        '  ServerAliveInterval 30\n'
        'Host codelab\n'
        '  HostName 10.0.0.5\n'
        '  User dev\n'
        'Host dev-*\n'
        'Host office\n'
        'Host codelab\n',  # duplicate must not repeat
        encoding='utf-8')
    assert _pair.ssh_config_hosts(str(cfg)) == ['codelab', 'office']


def test_ssh_config_hosts_missing_file():
    assert _pair.ssh_config_hosts('/nonexistent/path/config') == []


# ── try_ssh_tunnel ───────────────────────────────────────────────────────

def test_tunnel_success_keeps_process_alive():
    proc = _FakePopen([None, None])
    holder = {}

    def fake_popen(cmd, stdout=None, stderr=None):
        holder['cmd'] = cmd
        return proc

    calls = {'n': 0}

    def fake_probe(url, timeout=0):
        calls['n'] += 1
        return True, ''

    url = _pair.try_ssh_tunnel('codelab', local_port=15234, timeout=2,
                               _popen=fake_popen, _probe=fake_probe)
    assert url == 'http://127.0.0.1:15234'
    assert proc.killed is False
    assert proc in _pair._ACTIVE_TUNNELS
    _pair._ACTIVE_TUNNELS.remove(proc)  # keep the registry clean
    assert '-L' in holder['cmd'] and '15234:127.0.0.1:15000' in holder['cmd']
    assert 'BatchMode=yes' in holder['cmd']
    assert holder['cmd'][-1] == 'codelab'


def test_tunnel_probe_failure_kills_process():
    proc = _FakePopen([None])
    url = _pair.try_ssh_tunnel('codelab', timeout=0.3,
                               _popen=lambda *a, **k: proc,
                               _probe=lambda url, timeout=0: (False, 'unreachable'))
    assert url == ''
    assert proc.killed is True
    assert proc not in _pair._ACTIVE_TUNNELS


def test_tunnel_early_death_kills_process():
    proc = _FakePopen([1])  # exits immediately (auth refused / bind failed)
    url = _pair.try_ssh_tunnel('codelab', timeout=2,
                               _popen=lambda *a, **k: proc,
                               _probe=lambda url, timeout=0: (True, ''))
    assert url == ''
    assert proc.killed is True


def test_tunnel_spawn_failure_is_clean_miss():
    def boom(*a, **k):
        raise FileNotFoundError('ssh not installed')

    assert _pair.try_ssh_tunnel('h', _popen=boom) == ''


# ── discover: rung order + short-circuit ─────────────────────────────────

def test_discover_loopback_short_circuits(monkeypatch):
    called = []
    monkeypatch.setattr(_pair, 'loopback_probe',
                        lambda: called.append('lo') or 'http://127.0.0.1:15000')
    monkeypatch.setattr(_pair, 'lan_probe',
                        lambda: called.append('lan') or [])
    monkeypatch.setattr(_pair, 'try_ssh_tunnel',
                        lambda h, log=None: called.append('ssh') or '')
    assert _pair.discover() == 'http://127.0.0.1:15000'
    assert called == ['lo']


def test_discover_lan_beats_ssh(monkeypatch):
    called = []
    monkeypatch.setattr(_pair, 'loopback_probe',
                        lambda: called.append('lo') or '')
    monkeypatch.setattr(_pair, 'lan_probe',
                        lambda: called.append('lan') or ['http://10.0.0.2:15000'])
    monkeypatch.setattr(_pair, 'try_ssh_tunnel',
                        lambda h, log=None: called.append('ssh') or '')
    assert _pair.discover() == 'http://10.0.0.2:15000'
    assert called == ['lo', 'lan']


def test_discover_ssh_rung_and_candidate_cap(monkeypatch):
    tried = []
    monkeypatch.setattr(_pair, 'loopback_probe', lambda: '')
    monkeypatch.setattr(_pair, 'lan_probe', lambda: [])
    monkeypatch.setattr(_pair, 'ssh_config_hosts',
                        lambda: ['h1', 'h2', 'h3', 'h4', 'h5'])
    monkeypatch.setattr(_pair, 'try_ssh_tunnel',
                        lambda h, log=None: tried.append(h) or
                        ('http://127.0.0.1:15000' if h == 'h2' else ''))
    assert _pair.discover() == 'http://127.0.0.1:15000'
    assert tried == ['h1', 'h2']  # stops at first win


def test_discover_all_miss_returns_empty(monkeypatch):
    monkeypatch.setattr(_pair, 'loopback_probe', lambda: '')
    monkeypatch.setattr(_pair, 'lan_probe', lambda: [])
    monkeypatch.setattr(_pair, 'ssh_config_hosts', lambda: ['h1'])
    monkeypatch.setattr(_pair, 'try_ssh_tunnel', lambda h, log=None: '')
    assert _pair.discover() == ''


def test_discover_ssh_rung_capped(monkeypatch):
    tried = []
    monkeypatch.setattr(_pair, 'loopback_probe', lambda: '')
    monkeypatch.setattr(_pair, 'lan_probe', lambda: [])
    monkeypatch.setattr(_pair, 'ssh_config_hosts',
                        lambda: ['h%d' % i for i in range(10)])
    monkeypatch.setattr(_pair, 'try_ssh_tunnel',
                        lambda h, log=None: tried.append(h) or '')
    assert _pair.discover() == ''
    assert len(tried) == _pair._MAX_SSH_CANDIDATES


# ── the attach flow's sentinel plumbing (no UI) ─────────────────────────

def test_attachment_flow_falls_back_to_connect_line(monkeypatch):
    import desktop.connect_ui as cui
    called = []
    monkeypatch.setattr(cui, 'prompt_attach',
                        lambda url, log=None: cui.PREFER_CONNECT_LINE)
    monkeypatch.setattr(cui, 'prompt_connect_line',
                        lambda url, log=None: called.append(url) or
                        ('http://srv', 'tok'))
    assert cui.prompt_attachment_flow('http://pre', log=None) == ('http://srv', 'tok')
    assert called == ['http://pre']


def test_attachment_flow_cancel_stays_cancel(monkeypatch):
    import desktop.connect_ui as cui
    monkeypatch.setattr(cui, 'prompt_attach', lambda url, log=None: None)
    monkeypatch.setattr(
        cui, 'prompt_connect_line',
        lambda url, log=None: pytest.fail('cancel must not open the line dialog'))
    assert cui.prompt_attachment_flow('', log=None) is None
