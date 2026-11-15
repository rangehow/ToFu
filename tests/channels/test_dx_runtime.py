"""The 大象 launcher's one Node (docs/CHANNELS.md C6): resolved from TOFU_NODE, verified once
per file, handed to the bridge and its SSO step, and checked at boot.

Recorded 2026-11-10 06:45: the launcher's SSO step refused with SSO_NODE_UNAVAILABLE after its
version probe timed out during a host stall; the Node was fine and the next launch worked. A
missing or too-old Node must instead be a typed ``runtime_unavailable`` before any launch, and a
verified Node must never be probed again on a launch.
"""
import json
import os
import threading
from types import SimpleNamespace

import pytest

from lib.channels.dx import login, runtime
from tests.channels.conftest import OWNER, OWNER_UID

pytestmark = pytest.mark.unit


class SsoAdapter:
    """The SSO adapter's ``node_runtime`` verdict, answered per configured executable."""

    def __init__(self):
        self.queries: list[dict] = []
        self.verdicts: dict[str, object] = {}  # executable -> version string or refusal dict

    def __call__(self, source, environment, *args):
        assert 'node_runtime()' in source
        self.queries.append(dict(environment))
        executable = environment.get('MTSSO_NODE_EXECUTABLE', '')
        verdict = self.verdicts.get(executable, 'v22.23.4')
        if isinstance(verdict, dict):
            return {'refusal': verdict}
        path = os.path.realpath(executable)
        info = os.stat(path)
        return {'runtime': {'path': path, 'identity': [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns],
                            'version': verdict}}


@pytest.fixture
def adapter(monkeypatch):
    """The real check over a scripted SSO adapter, in a fresh process (nothing verified yet)."""
    answer = SsoAdapter()
    monkeypatch.setattr(runtime, 'adapter_answer', answer)
    monkeypatch.setattr(runtime, '_VERIFIED', {})
    monkeypatch.setattr('lib.channels.dx.supervisor.bridge_node', runtime.bridge_node)
    monkeypatch.setattr('lib.channels.dx.adapter.bridge_node', runtime.bridge_node)
    monkeypatch.setattr(login, 'shared_session_mis', lambda environment: 'owner')
    monkeypatch.delenv('MTSSO_NODE_EXECUTABLE', raising=False)
    monkeypatch.delenv('MTSSO_NODE_VERIFIED', raising=False)
    return answer


@pytest.fixture
def node(tmp_path, monkeypatch):
    """TOFU_NODE names an executable file."""
    path = tmp_path / 'node-v22.23.4' / 'bin' / 'node'
    path.parent.mkdir(parents=True)
    path.write_text('#!/bin/sh\n')
    path.chmod(0o755)
    monkeypatch.setenv('TOFU_NODE', str(path))
    return str(path)


def test_every_launcher_child_runs_the_configured_node_verified_once(adapter, node, monkeypatch):
    from lib.channels.dx.adapter import launcher_session
    # An ambient override must not split the bridge and its SSO step across two binaries.
    monkeypatch.setenv('MTSSO_NODE_EXECUTABLE', '/srv/other-node/bin/node')
    first = launcher_session(OWNER, 'owner').environment
    real = os.path.realpath(node)
    assert first['DX_NODE'] == first['MTSSO_NODE_EXECUTABLE'] == real
    info = os.stat(real)
    assert json.loads(first['MTSSO_NODE_VERIFIED']) == {
        'path': real, 'identity': [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns],
        'version': 'v22.23.4'}
    assert launcher_session(OWNER, 'owner').environment == first
    # One probe, of TOFU_NODE itself, with no stale hand-off of its own.
    [query] = adapter.queries
    assert query['MTSSO_NODE_EXECUTABLE'] == node and 'MTSSO_NODE_VERIFIED' not in query
    with open(node, 'a') as handle:
        handle.write('# replaced\n')  # a changed file is a new identity: verified again
    assert launcher_session(OWNER, 'owner').environment['DX_NODE'] == real
    assert len(adapter.queries) == 2


def test_a_relinked_setting_is_verified_again(adapter, tmp_path, monkeypatch):
    from lib.channels.dx.adapter import launcher_session
    targets = []
    for name in ('node-v22.23.4', 'node-v24.14.1'):
        target = tmp_path / name
        target.write_text('#!/bin/sh\n')
        target.chmod(0o755)
        targets.append(str(target))
    link = tmp_path / 'node'
    link.symlink_to(targets[0])
    monkeypatch.setenv('TOFU_NODE', str(link))
    assert launcher_session(OWNER, 'owner').environment['DX_NODE'] == targets[0]
    link.unlink()
    link.symlink_to(targets[1])
    assert launcher_session(OWNER, 'owner').environment['DX_NODE'] == targets[1]
    assert len(adapter.queries) == 2


def test_the_configured_node_survives_a_restart(adapter, node, tmp_path, monkeypatch):
    """TOFU_NODE lives in the project .env, which every server start loads (lib/server/server_boot_env)."""
    from tofu_dotenv import load_dotenv_file
    from lib.channels.dx.adapter import launcher_session
    env_file = tmp_path / '.env'
    env_file.write_text(f'TOFU_NODE={node}\n')
    for _boot in (1, 2):
        monkeypatch.delenv('TOFU_NODE')  # a new process starts without it
        monkeypatch.setattr(runtime, '_VERIFIED', {})
        load_dotenv_file(env_file)
        assert launcher_session(OWNER, 'owner').environment['DX_NODE'] == os.path.realpath(node)
    assert len(adapter.queries) == 2  # verified once per server process


@pytest.mark.parametrize('version, qualifies', [
    ('v20.19.0', False), ('v22.20.0', False), ('v22.21.0', True), ('v22.23.4', True),
    ('v23.11.0', False), ('v24.0.0', True), ('v24.14.1', True), ('v25.2.1', True), ('', False), ('22', False),
])
def test_the_bridge_floor_is_node_use_env_proxy_support(version, qualifies):
    # contract x-dx.bridgeNode: fetch() honours NODE_USE_ENV_PROXY from 22.21.0 (22.x) and 24.0.0.
    assert runtime.meets_bridge_floor(version) is qualifies


# ── the boot check ────────────────────────────────────────────────────

class Bridge:
    instances: list = []

    def __init__(self, owner, *, store=None, login_mis=None):
        self.running, self.login_mis = False, login_mis
        Bridge.instances.append(self)

    def start(self):
        self.running = True

    def alive(self):
        return self.running

    def tick(self):
        pass

    def stop(self, timeout):
        self.running = False
        return True

    def outcome(self):
        return 'disconnected', 'bridge exited (1)'


@pytest.fixture
def supervisor(monkeypatch, tmp_path, router, store):
    from lib.channels.dx import supervisor as module
    from lib.channels.dx.adapter import LaunchSpec
    (tmp_path / 'engine').mkdir()
    (tmp_path / 'engine' / 'handler.js').write_text('')
    (tmp_path / 'dx-tofu.sh').write_text('')
    spec = LaunchSpec(skill_dir=str(tmp_path), launcher=str(tmp_path / 'dx-tofu.sh'),
                      engine_dir=str(tmp_path / 'engine'))
    monkeypatch.setattr(module, 'launch_spec', lambda owner: spec)
    Bridge.instances = []
    for name, value in (('_ADAPTERS', {}), ('_CONFIRMING', set()), ('_WORKERS', set()),
                        ('_STOPPING', threading.Event()), ('_HOSTING', threading.Event())):
        monkeypatch.setattr(module, name, value)
    reconcile = module.reconcile_owner
    monkeypatch.setattr(module, 'reconcile_owner',
                        lambda owner, **kwargs: reconcile(owner, **{**kwargs, 'adapter_factory': Bridge}))
    reported = []
    monkeypatch.setattr('lib.router.watchdog.signals.report_channel_status',
                        lambda owner, channel, account, **kwargs: reported.append(account['status']))
    store.update_account('dx', lambda account: {
        **account, 'enabled': True, 'identity': {'uid': OWNER_UID, 'mis': 'owner', 'name': 'Owner'},
        'frontierAtMs': 1})
    principal = SimpleNamespace(owner_user_id=OWNER)
    return SimpleNamespace(module=module, tick=lambda: module.reconcile_channel_bridges(principal),
                           reported=reported)


def _signal_identity(status):
    # lib.router.watchdog.signals.report_channel_status: one signal per (channel, state, connectedAtMs).
    return 'dx', status['state'], status.get('connectedAtMs')


@pytest.mark.parametrize('verdict, code', [
    ({'code': 'SSO_NODE_NOT_FOUND', 'message': '找不到官方 SSO 命令所需的 Node 20+'}, 'SSO_NODE_NOT_FOUND'),
    ({'code': 'SSO_NODE_TOO_OLD', 'message': '找到的 Node 版本过低（node v18.20.8）'}, 'SSO_NODE_TOO_OLD'),
    # Meets the SSO step's floor, not the bridge's: Node 20 ignores NODE_USE_ENV_PROXY.
    ('v20.19.0', 'DX_NODE_TOO_OLD'),
])
def test_a_missing_or_too_old_node_is_runtime_unavailable_at_boot(adapter, node, supervisor, store, verdict, code):
    adapter.verdicts[node] = verdict
    for _tick in (1, 2):
        supervisor.tick()
        status = store.account('dx')['status']
        assert status['state'] == 'runtime_unavailable'
        assert status['detail'].startswith(code + ':') and node in status['detail']
        if _tick == 1:
            began = status['updatedAtMs']
    assert status['updatedAtMs'] == began  # one persistent state, not a new one per tick
    assert Bridge.instances == []  # no launch can succeed on that Node
    # Settings shows the state; the Router Watchdog gets one signal for the episode.
    assert len(supervisor.reported) == 2 and len(set(map(_signal_identity, supervisor.reported))) == 1
    adapter.verdicts[node] = 'v22.23.4'  # repaired: the next tick launches
    with open(node, 'a') as handle:
        handle.write('# repaired\n')
    supervisor.tick()
    assert len(Bridge.instances) == 1 and Bridge.instances[0].running


def test_a_probe_timeout_defers_the_launch_without_a_fault(adapter, node, supervisor, store):
    adapter.verdicts[node] = {'code': 'SSO_NODE_PROBE_TIMED_OUT', 'message': 'Node 版本探测在主机繁忙时超时'}
    supervisor.tick()
    status = store.account('dx')['status']
    assert status['state'] == 'starting' and status['detail'].startswith('SSO_NODE_PROBE_TIMED_OUT:')
    from lib.router.watchdog.signals import UNHEALTHY_CHANNEL_STATES
    assert status['state'] not in UNHEALTHY_CHANNEL_STATES and Bridge.instances == []
    del adapter.verdicts[node]  # the host answers again: verified and launched at the next tick
    supervisor.tick()
    assert len(Bridge.instances) == 1 and Bridge.instances[0].running


def test_the_recovered_connection_reaches_the_watchdog_report(adapter, node, supervisor, store):
    """Every tick reports the account's current status, so a recovery closes nothing by itself here
    but gives FAULT-INSPECTION's auto-resolve its trigger (connected after an unhealthy state)."""
    adapter.verdicts[node] = {'code': 'SSO_NODE_NOT_FOUND', 'message': 'missing'}
    supervisor.tick()
    del adapter.verdicts[node]
    with open(node, 'a') as handle:
        handle.write('# installed\n')
    supervisor.tick()
    store.set_status('dx', 'connected', '', connected_at_ms=99)  # the new session's bridge connected
    supervisor.tick()
    assert [status['state'] for status in supervisor.reported] == ['runtime_unavailable', 'starting', 'connected']
    assert supervisor.reported[-1]['connectedAtMs'] == 99
