"""Shutdown races for the owned MCP prewarm/auto-connect worker."""

from __future__ import annotations

import threading

import pytest


pytestmark = pytest.mark.unit


def test_stop_during_prewarm_prevents_late_connect(monkeypatch):
    import lib.mcp.client as client
    import lib.mcp.startup as startup

    entered = threading.Event()
    release = threading.Event()
    calls = []

    def prewarm(**_):
        entered.set()
        release.wait(2.0)
        return []

    class Bridge:
        def connect_all(self):
            calls.append('connect')
            return {}

        def disconnect_all(self):
            calls.append('disconnect')

    monkeypatch.setattr(startup, '_worker', None)
    monkeypatch.setattr(client, 'prewarm_all_vendored', prewarm)
    monkeypatch.setattr(client, 'get_bridge', lambda owner: Bridge())

    assert startup.start_mcp_auto_connect({'one': {'enabled': True}}, owner_user_id=7)
    assert entered.wait(1.0)
    assert startup.stop_mcp_auto_connect(timeout=0.01) is False
    release.set()
    thread = startup._worker
    assert thread is not None
    thread.join(1.0)
    assert startup.stop_mcp_auto_connect(timeout=0.1) is True
    assert calls == []


def test_connect_finishing_after_stop_disconnects_itself(monkeypatch):
    import lib.mcp.client as client
    import lib.mcp.startup as startup

    connecting = threading.Event()
    release = threading.Event()
    calls = []

    class Bridge:
        def connect_all(self):
            calls.append('connect')
            connecting.set()
            release.wait(2.0)
            return {'server': ['tool']}

        def disconnect_all(self):
            calls.append('disconnect')

    monkeypatch.setattr(startup, '_worker', None)
    monkeypatch.setattr(client, 'prewarm_all_vendored', lambda **_: [])
    monkeypatch.setattr(client, 'get_bridge', lambda owner: Bridge())

    assert startup.start_mcp_auto_connect({'one': {'enabled': True}}, owner_user_id=7)
    assert connecting.wait(1.0)
    assert startup.stop_mcp_auto_connect(timeout=0.01) is False
    release.set()
    thread = startup._worker
    assert thread is not None
    thread.join(1.0)
    assert startup.stop_mcp_auto_connect(timeout=0.1) is True
    assert calls == ['connect', 'disconnect']


def test_duplicate_start_does_not_spawn_a_second_owner(monkeypatch):
    import lib.mcp.client as client
    import lib.mcp.startup as startup

    entered = threading.Event()
    release = threading.Event()

    def prewarm(**_):
        entered.set()
        release.wait(2.0)
        return []

    monkeypatch.setattr(startup, '_worker', None)
    monkeypatch.setattr(client, 'prewarm_all_vendored', prewarm)
    assert startup.start_mcp_auto_connect({}, owner_user_id=7) is True
    assert entered.wait(1.0)
    assert startup.start_mcp_auto_connect({}, owner_user_id=7) is False
    release.set()
    assert startup.stop_mcp_auto_connect(timeout=1.0) is True


def test_stop_retires_a_running_prewarm_process_tree(tmp_path, monkeypatch):
    """Found by the chaos suite (docs/ROBUSTNESS_AUDIT_20261117.md R2): a uv
    warm outlived every host and held the uv cache lock for the next boot."""
    import os
    import sys
    import time

    import lib.mcp.client as client
    import lib.mcp.client._vendor as vendor
    import lib.mcp.startup as startup

    pidfile = tmp_path / 'grandchild.pid'
    # warm = argv[:-1] + ['python', '-c', 'import slow_mcp']: the shell runs the
    # script with $0=python, and its sleep grandchild shares its group.
    script = f'sleep 60 & echo $! > {pidfile}; wait'
    monkeypatch.setattr(startup, '_worker', None)
    monkeypatch.setattr(client, '_VENDORED_LAUNCHERS', {'slow-mcp': {}})
    monkeypatch.setattr(client, '_reload_vendored_if_changed', lambda: None)
    monkeypatch.setattr(client, '_find_vendored_source', lambda command: (str(tmp_path), True))
    monkeypatch.setattr(client, 'vendored_launch_argv', lambda command: ['sh', '-c', script, 'placeholder'])
    monkeypatch.setattr(vendor, '_prewarm_unreachable_reason', lambda: '')
    monkeypatch.setattr(vendor, '_ensure_writable_caches', lambda env: None)
    monkeypatch.setattr(vendor, 'inherited_child_environment', lambda: {'PATH': os.environ['PATH']})

    assert startup.start_mcp_auto_connect({}, owner_user_id=7)
    deadline = time.monotonic() + 10
    while not (pidfile.exists() and pidfile.read_text().strip()):
        assert time.monotonic() < deadline, 'the warm never started'
        time.sleep(0.02)
    grandchild = int(pidfile.read_text())

    assert startup.stop_mcp_auto_connect(timeout=5.0) is True
    try:
        os.kill(grandchild, 0)
        state = open(f'/proc/{grandchild}/stat').read().rsplit(')', 1)[1].split()[0]
    except (ProcessLookupError, FileNotFoundError):
        state = 'gone'
    assert state in ('gone', 'Z'), 'shutdown left the warm process tree running'
    assert sys.platform.startswith('linux')


class _ReconcileBridge:
    def __init__(self):
        self.calls = []
        self.connected = set()

    def list_servers(self):
        return [{'name': name} for name in sorted(self.connected)]

    def connect_server(self, name, row):
        self.calls.append(('connect', name, row.get('command')))
        self.connected.add(name)
        return []

    def _disconnect_one(self, name, forget=False):
        self.calls.append(('disconnect', name, forget))
        self.connected.discard(name)
        return True

    def set_disabled_tools(self, name, tools):
        self.calls.append(('tools', name, list(tools)))


def _drained(startup, timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    while True:
        with startup._worker_lock:
            idle = not startup._pending and not startup._draining
        if idle:
            return
        assert time.monotonic() < deadline, 'reconcile queue never drained'
        time.sleep(0.01)


def test_reconcile_applies_management_actions_with_the_stored_row(monkeypatch):
    """A split API's MCP management reaches this process's bridge
    (docs/ROBUSTNESS_AUDIT_20261117.md R4/R5)."""
    import lib.mcp.client as client
    import lib.mcp.config as config
    import lib.mcp.startup as startup

    bridge = _ReconcileBridge()
    rows = {'chaos': {'command': 'chaos-v1', 'enabled': True, 'disabled_tools': ['chaos_record']}}
    monkeypatch.setattr(startup, '_worker', None)
    monkeypatch.setattr(startup, '_draining', False)
    monkeypatch.setattr(startup, '_pending', [])
    startup._worker_stop.clear()
    monkeypatch.setattr(client, 'get_bridge', lambda owner: bridge)
    monkeypatch.setattr(config, 'load_mcp_config', lambda user_id: rows)

    assert startup.request_mcp_reconcile(7, 'chaos', 'connect') is True
    _drained(startup)
    assert startup.request_mcp_reconcile(7, 'chaos', 'tools') is True
    _drained(startup)
    rows['chaos']['enabled'] = False  # a connect of a disabled row disconnects
    assert startup.request_mcp_reconcile(7, 'chaos', 'connect') is True
    _drained(startup)
    assert bridge.calls == [('connect', 'chaos', 'chaos-v1'), ('tools', 'chaos', ['chaos_record']),
                            ('disconnect', 'chaos', True)]

    assert startup.stop_mcp_auto_connect(timeout=1.0) is True
    assert startup.request_mcp_reconcile(7, 'chaos', 'connect') is False
    with pytest.raises(ValueError):
        startup.request_mcp_reconcile(7, 'chaos', 'restart')
