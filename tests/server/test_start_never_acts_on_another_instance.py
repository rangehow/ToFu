"""A start from one checkout never acts on another live Tofu instance.

2026-11-17: a Tofu shell exports the serving instance's identity (``TOFU_PROJECT_PATH`` of the
live checkout, its ``PORT``, process role and storage connection). ``serverctl.py start`` from a
worktree read ``TOFU_PROJECT_PATH`` as its project, ran engine deployment against the live tree and
launched a second manager. The manager read its worker's lock at ``<project>/data`` while a worker
on ``TOFU_DATA_DIR`` writes it under that data root, and every instance stamped the one host-global
heartbeat the live manager reads for wedge evidence.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

import runtime_path_policy
import server_manager as sm
import serverctl

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_serverctl_controls_its_own_checkout_whatever_the_shell_exports(tmp_path):
    environment = {**os.environ, 'TOFU_PROJECT_PATH': str(tmp_path), 'PYTHONPATH': str(ROOT)}
    completed = subprocess.run([sys.executable, '-c', 'import serverctl; print(serverctl.PROJECT)'],
                               cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.strip() == os.path.realpath(ROOT)


@pytest.mark.parametrize('command', [['start'], ['restart', '-y'], ['stop', '-y'], ['ensure'],
                                     ['prepare-frontend'], ['install-recovery']])
def test_an_inherited_instance_environment_refuses_every_instance_action(monkeypatch, tmp_path, capsys,
                                                                         command):
    monkeypatch.setenv('TOFU_PROJECT_PATH', str(tmp_path / 'live-checkout'))
    monkeypatch.setattr(serverctl, '_request',
                        lambda *_a, **_k: pytest.fail('a refused action must not reach any manager'))

    assert serverctl.main(command) == 2
    error = capsys.readouterr().err
    assert 'Refused' in error and str(tmp_path / 'live-checkout') in error and 'Nothing was changed' in error


def test_the_own_checkout_and_read_only_commands_are_not_refused(monkeypatch):
    monkeypatch.setenv('TOFU_PROJECT_PATH', serverctl.PROJECT)
    serverctl.refuse_inherited_instance_environment()
    monkeypatch.delenv('TOFU_PROJECT_PATH')
    serverctl.refuse_inherited_instance_environment()
    assert 'status' not in serverctl._INSTANCE_ACTIONS and 'doctor' not in serverctl._INSTANCE_ACTIONS


def test_a_manager_port_owned_by_another_project_is_never_relaunched(monkeypatch):
    monkeypatch.setattr(serverctl, '_request',
                        lambda path, *_a, **_k: {'ok': True, 'projects': ['/srv/live-tofu']})
    monkeypatch.setattr(serverctl, 'prepare_source_frontend_artifact',
                        lambda *_a: pytest.fail('a refused start must not build artifacts'))
    monkeypatch.setattr(serverctl.subprocess, 'run',
                        lambda *_a, **_k: pytest.fail('a refused start must not launch a manager'))

    with pytest.raises(serverctl.ManagerServesAnotherProject, match='/srv/live-tofu'):
        serverctl.ensure_manager()


def _project(tmp_path: Path) -> Path:
    project = tmp_path / 'project'
    (project / 'data').mkdir(parents=True)
    return project


def _stamp_lock(data: Path) -> None:
    data.mkdir(parents=True, exist_ok=True)
    (data / '.server.lock').write_text(f'{os.getpid()}@{sm._hostname()}\n', encoding='utf-8')


@pytest.mark.parametrize('source', ['ambient', 'dotenv', 'serverEnv'])
def test_the_manager_reads_its_worker_lock_under_the_worker_data_root(monkeypatch, tmp_path, source):
    project = _project(tmp_path)
    worker_data = tmp_path / 'worker-root' / 'data'
    _stamp_lock(worker_data)
    monkeypatch.delenv('TOFU_DATA_DIR', raising=False)
    server_env = {}
    if source == 'ambient':
        monkeypatch.setenv('TOFU_DATA_DIR', str(worker_data))
    elif source == 'dotenv':
        (project / '.env').write_text(f'TOFU_DATA_DIR={worker_data}\n', encoding='utf-8')
    else:
        server_env = {'TOFU_DATA_DIR': str(worker_data)}

    assert sm.worker_data_directory(str(project), server_env) == worker_data
    status = sm.read_lock_status(str(project), server_env)
    assert status['lockPresent'] and status['pid'] == os.getpid()


def test_an_in_tree_worker_lock_stays_at_the_project_data_directory(monkeypatch, tmp_path):
    project = _project(tmp_path)
    _stamp_lock(project / 'data')
    for name in ('TOFU_DATA_DIR', 'TOFU_DATA_LAYOUT'):
        monkeypatch.delenv(name, raising=False)

    assert sm.worker_data_directory(str(project)) == project / 'data'
    assert sm.read_lock_status(str(project))['lockPresent']


def test_each_instance_has_its_own_heartbeat_unless_one_is_named(tmp_path):
    live = runtime_path_policy.instance_heartbeat_path(tmp_path / 'live' / 'data', {})
    scratch = runtime_path_policy.instance_heartbeat_path(tmp_path / 'scratch' / 'data', {})
    assert live != scratch
    assert Path(live).name == Path(scratch).name == 'server.heartbeat'
    assert runtime_path_policy.instance_heartbeat_path(tmp_path / 'live' / 'data', {}) == live
    named = runtime_path_policy.instance_heartbeat_path(tmp_path / 'live' / 'data',
                                                        {'TOFU_HEARTBEAT_DIR': str(tmp_path / 'hb')})
    assert named == str(tmp_path / 'hb' / 'server.heartbeat')


def test_a_server_stamps_the_heartbeat_of_its_own_data_root(tmp_path):
    data = tmp_path / 'instance' / 'data'
    environment = {name: value for name, value in os.environ.items() if name != 'TOFU_HEARTBEAT_DIR'}
    environment.update({'TOFU_DATA_DIR': str(data), 'PYTHONPATH': str(ROOT)})
    completed = subprocess.run(
        [sys.executable, '-c', 'from lib.server_boot.lock import _heartbeat_path; print(_heartbeat_path())'],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.strip().splitlines()[-1] == runtime_path_policy.instance_heartbeat_path(data, {})


def test_the_manager_reads_the_heartbeat_its_worker_writes(monkeypatch, tmp_path):
    project = _project(tmp_path)
    worker_data = tmp_path / 'worker-root' / 'data'
    monkeypatch.delenv('TOFU_HEARTBEAT_DIR', raising=False)
    monkeypatch.delenv('TOFU_DATA_DIR', raising=False)
    heartbeat = Path(runtime_path_policy.instance_heartbeat_path(worker_data, {}))
    manager = sm.LifecycleManager.__new__(sm.LifecycleManager)
    manager.project = str(project)
    manager._state = {'serverEnv': {'TOFU_DATA_DIR': str(worker_data)}}
    read = []
    monkeypatch.setattr(sm, '_read_json', lambda path: read.append(Path(path)) or {'pid': 7, 'ts': 1.0})

    assert manager._heartbeat_state(7) == {'pid': 7, 'ts': 1.0}
    assert read == [heartbeat]
