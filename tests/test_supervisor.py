"""Tests for supervisor.py — the remote start/stop daemon for Tofu projects.

Covers the pure logic (allow-list, status parsing, idempotency, auth) and a
live HTTP round-trip against a fake project dir containing stub server.py /
stop.sh scripts, so no real Tofu server is spawned.

Run: pytest -p no:napari tests/test_supervisor.py
"""

import json
import os
import threading
import urllib.request
import urllib.error

import pytest

import supervisor


# ── fixtures ──────────────────────────────────────────────────────────

def _make_project(tmp_path, with_scripts=True):
    """Create a fake project dir with stub server.py / stop.sh + data/."""
    proj = tmp_path / 'proj'
    proj.mkdir()
    (proj / 'data').mkdir()
    if with_scripts:
        # A stub server.py that just sleeps; a stub stop.sh that exits 0.
        (proj / 'server.py').write_text('import time; time.sleep(300)\n')
        (proj / 'stop.sh').write_text('#!/usr/bin/env bash\necho stopped\nexit 0\n')
        os.chmod(proj / 'stop.sh', 0o755)
    return proj


def _write_lock(proj, pid, host='thishost'):
    (proj / 'data' / '.server.lock').write_text(f'{pid}@{host}\n')


# ── allow-list ────────────────────────────────────────────────────────

def test_parse_allowlist_normalizes_and_dedupes(tmp_path):
    a = str(tmp_path / 'a')
    raw = f'{a}{os.pathsep}{a}{os.pathsep}  {os.pathsep}'
    result = supervisor.parse_allowlist(raw)
    assert result == {os.path.realpath(a)}


def test_parse_allowlist_empty():
    assert supervisor.parse_allowlist('') == set()
    assert supervisor.parse_allowlist(None) == set()


def test_is_allowed_requires_membership_and_scripts(tmp_path):
    proj = _make_project(tmp_path)
    allow = {os.path.realpath(str(proj))}
    assert supervisor.is_allowed(str(proj), allow) is True
    # Not in allow-list.
    assert supervisor.is_allowed(str(proj), set()) is False
    # Empty path.
    assert supervisor.is_allowed('', allow) is False


def test_is_allowed_rejects_traversal(tmp_path):
    """A '..' path that canonicalises OUTSIDE the allow-list is rejected."""
    proj = _make_project(tmp_path)
    allow = {os.path.realpath(str(proj))}
    sneaky = str(proj / '..' / 'proj' / '..' / 'other')
    assert supervisor.is_allowed(sneaky, allow) is False


def test_is_allowed_rejects_dir_without_scripts(tmp_path):
    proj = _make_project(tmp_path, with_scripts=False)
    allow = {os.path.realpath(str(proj))}
    # In allow-list but missing server.py/stop.sh → not runnable.
    assert supervisor.is_allowed(str(proj), allow) is False


# ── status ────────────────────────────────────────────────────────────

def test_read_status_no_lock(tmp_path):
    proj = _make_project(tmp_path)
    st = supervisor.read_status(str(proj))
    assert st['running'] is False
    assert st['lockPresent'] is False
    assert st['pid'] is None


def test_read_status_stale_lock_dead_pid(tmp_path):
    proj = _make_project(tmp_path)
    # A pid that is (almost certainly) not alive.
    _write_lock(proj, 999999)
    st = supervisor.read_status(str(proj))
    assert st['lockPresent'] is True
    assert st['running'] is False
    assert st['stale'] is True


def test_read_status_malformed_lock(tmp_path):
    proj = _make_project(tmp_path)
    (proj / 'data' / '.server.lock').write_text('garbage-no-at-sign\n')
    st = supervisor.read_status(str(proj))
    assert st['running'] is False
    assert st['stale'] is True


def test_read_status_running(tmp_path, monkeypatch):
    proj = _make_project(tmp_path)
    _write_lock(proj, 4242)
    # Force the liveness probe to say "yes, that's a live server.py".
    monkeypatch.setattr(supervisor, '_pid_is_server', lambda pid: pid == 4242)
    st = supervisor.read_status(str(proj))
    assert st['running'] is True
    assert st['pid'] == 4242


# ── start idempotency ─────────────────────────────────────────────────

def test_do_start_idempotent_when_running(tmp_path, monkeypatch):
    proj = _make_project(tmp_path)
    _write_lock(proj, 4242)
    monkeypatch.setattr(supervisor, '_pid_is_server', lambda pid: True)
    spawned = []
    monkeypatch.setattr(supervisor.subprocess, 'Popen',
                        lambda *a, **k: spawned.append(a) or pytest.fail('spawned'))
    res = supervisor.do_start(str(proj))
    assert res['ok'] is True
    assert res['alreadyRunning'] is True
    assert spawned == []  # no second process


def test_do_start_spawns_when_stopped(tmp_path, monkeypatch):
    proj = _make_project(tmp_path)
    monkeypatch.setattr(supervisor, '_pid_is_server', lambda pid: False)

    class FakeProc:
        pid = 7777

    calls = {}

    def fake_popen(cmd, **kw):
        calls['cmd'] = cmd
        calls['cwd'] = kw.get('cwd')
        calls['new_session'] = kw.get('start_new_session')
        return FakeProc()

    monkeypatch.setattr(supervisor.subprocess, 'Popen', fake_popen)
    res = supervisor.do_start(str(proj))
    assert res['ok'] is True
    assert res['alreadyRunning'] is False
    assert res['launcherPid'] == 7777
    assert calls['cmd'][1] == 'server.py'
    assert calls['cwd'] == os.path.realpath(str(proj))
    assert calls['new_session'] is True


# ── stop ──────────────────────────────────────────────────────────────

def test_do_stop_runs_stop_sh(tmp_path, monkeypatch):
    proj = _make_project(tmp_path)
    monkeypatch.setattr(supervisor, '_pid_is_server', lambda pid: False)
    res = supervisor.do_stop(str(proj))
    # stub stop.sh exits 0 → ok.
    assert res['ok'] is True
    assert res['exitCode'] == 0


def test_do_stop_missing_script(tmp_path):
    proj = _make_project(tmp_path, with_scripts=False)
    res = supervisor.do_stop(str(proj))
    assert res['ok'] is False
    assert 'stop.sh not found' in res['message']


def test_do_stop_refused_exit1(tmp_path, monkeypatch):
    proj = _make_project(tmp_path)
    (proj / 'stop.sh').write_text('#!/usr/bin/env bash\nexit 1\n')
    os.chmod(proj / 'stop.sh', 0o755)
    monkeypatch.setattr(supervisor, '_pid_is_server', lambda pid: False)
    res = supervisor.do_stop(str(proj))
    assert res['ok'] is False   # exit 1 = refused
    assert res['exitCode'] == 1


# ── auth helpers ──────────────────────────────────────────────────────

def test_extract_bearer():
    assert supervisor.extract_bearer('Bearer abc123') == 'abc123'
    assert supervisor.extract_bearer('bearer xyz') == 'xyz'
    assert supervisor.extract_bearer('Basic abc') == ''
    assert supervisor.extract_bearer('') == ''
    assert supervisor.extract_bearer(None) == ''


def test_token_matches():
    assert supervisor.token_matches('s3cret', 's3cret') is True
    assert supervisor.token_matches('wrong', 's3cret') is False
    assert supervisor.token_matches('', 's3cret') is False
    assert supervisor.token_matches('s3cret', '') is False


# ── live HTTP round-trip ──────────────────────────────────────────────

@pytest.fixture
def live_server(tmp_path, monkeypatch):
    """Start the real ThreadingHTTPServer on an ephemeral port with a token."""
    proj = _make_project(tmp_path)
    canon = os.path.realpath(str(proj))
    monkeypatch.setenv(supervisor.ENV_HOST, '127.0.0.1')
    monkeypatch.setenv(supervisor.ENV_PORT, '0')      # ephemeral
    monkeypatch.setenv(supervisor.ENV_TOKEN, 'testtoken')
    monkeypatch.setenv(supervisor.ENV_PROJECTS, canon)
    # Never actually spawn server.py in the HTTP test.
    monkeypatch.setattr(supervisor, '_pid_is_server', lambda pid: False)
    httpd = supervisor.build_server()
    host, port = httpd.server_address
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield {'base': f'http://{host}:{port}', 'proj': canon}
    httpd.shutdown()
    httpd.server_close()


def _req(url, method='GET', token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    if data:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_http_health_no_auth(live_server):
    status, body = _req(live_server['base'] + '/health')
    assert status == 200
    assert body['ok'] is True


def test_http_status_no_token_readonly(live_server):
    # /status is read-only (least-privilege) → no token required; the
    # code-server cookie fronting the proxy is the gate.
    url = live_server['base'] + '/status?projectPath=' + live_server['proj']
    status, body = _req(url)  # no token
    assert status == 200
    assert body['ok'] is True
    assert body['running'] is False


def test_http_status_still_enforces_allowlist(live_server):
    # Even token-free, /status must reject a path outside the allow-list.
    status, body = _req(live_server['base'] + '/status?projectPath=/etc')
    assert status == 403


def test_http_start_requires_token(live_server):
    # State-changing → token still mandatory.
    status, body = _req(live_server['base'] + '/start', method='POST',
                        body={'projectPath': live_server['proj']})  # no token
    assert status == 401


def test_http_start_rejects_unlisted_path(live_server):
    status, body = _req(live_server['base'] + '/start', method='POST',
                        token='testtoken', body={'projectPath': '/etc'})
    assert status == 403


def test_http_stop_roundtrip(live_server):
    status, body = _req(live_server['base'] + '/stop', method='POST',
                        token='testtoken', body={'projectPath': live_server['proj']})
    assert status == 200
    assert body['ok'] is True   # stub stop.sh exits 0


def test_http_fail_closed_without_token(tmp_path, monkeypatch):
    """No TOFU_SUPERVISOR_TOKEN → STATE-CHANGING endpoints return 503 (fail-closed).

    /status stays reachable (read-only), so fail-closed is asserted on /start.
    """
    proj = _make_project(tmp_path)
    canon = os.path.realpath(str(proj))
    monkeypatch.setenv(supervisor.ENV_HOST, '127.0.0.1')
    monkeypatch.setenv(supervisor.ENV_PORT, '0')
    monkeypatch.delenv(supervisor.ENV_TOKEN, raising=False)
    monkeypatch.setenv(supervisor.ENV_PROJECTS, canon)
    httpd = supervisor.build_server()
    host, port = httpd.server_address
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        status, body = _req(f'http://{host}:{port}/start', method='POST',
                            token='anything', body={'projectPath': canon})
        assert status == 503
        assert body['ok'] is False
        # And /status is STILL reachable read-only even with no token configured.
        s2, b2 = _req(f'http://{host}:{port}/status?projectPath={canon}')
        assert s2 == 200
        assert b2['ok'] is True
    finally:
        httpd.shutdown()
        httpd.server_close()
