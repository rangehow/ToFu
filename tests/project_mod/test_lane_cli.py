"""Executable spec for the lane CLI and its detached spare-preparation owner.

The regression this file pins: ``ensure_next_spare`` used to spawn
``scripts/lane.py prepare-spare`` even though that track-but-export-excluded
keeper was absent from a recovered checkout. Detached preparation therefore
always spawned a missing path and the next lane never recycled a spare, with
no error in the serving process.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.unit


def _stub(monkeypatch, **replacements):
    from lib.project_mod import lanes
    for name, value in replacements.items():
        monkeypatch.setattr(lanes, name, value, raising=True)
    return lanes


def test_module_cli_is_directly_executable():
    """The detached spawn target runs in a checkout without the private script."""
    env = {**os.environ, 'PYTHONPATH': str(ROOT)}
    result = subprocess.run(
        [sys.executable, '-m', 'lib.project_mod.lane_cli', '--help'],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    for command in ('new', 'restore', 'save', 'remove', 'list', 'prepare-spare'):
        assert command in result.stdout


@pytest.mark.skipif(not (ROOT / 'scripts' / 'lane.py').exists(),
                    reason='the opensource export drops the private wrapper')
def test_documented_wrapper_still_runs():
    env = {**os.environ, 'PYTHONPATH': str(ROOT)}
    result = subprocess.run(
        [sys.executable, str(ROOT / 'scripts' / 'lane.py'), '--help'],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'prepare-spare' in result.stdout


def test_new_resolves_the_repository_once_and_prepares_the_next_spare(
        monkeypatch, tmp_path, capsys):
    from lib.project_mod import lane_cli
    repo = SimpleNamespace(top=tmp_path)
    calls = []

    def repository(path):
        calls.append(('repository', path))
        return repo

    def create_lane(path, branch, base=None, repo=None):
        calls.append(('create', path, branch, base, repo))
        return tmp_path / 'lane', True

    def ensure_next_spare(path, repo=None):
        calls.append(('spare', path, repo))
        return True

    lanes = _stub(monkeypatch, bound_task_checkout=lambda cwd, env: None,
                  repository=repository, create_lane=create_lane,
                  ensure_next_spare=ensure_next_spare)
    assert lane_cli.main(
        ['new', 'fix/example', '--base', 'HEAD~1'], cwd=tmp_path, environ={}) == 0
    assert calls == [
        ('repository', tmp_path),
        ('create', tmp_path, 'fix/example', 'HEAD~1', repo),
        ('spare', tmp_path, repo),
    ]
    assert sum(1 for call in calls if call[0] == 'repository') == 1
    assert calls[1][4] is repo and calls[2][2] is repo
    assert capsys.readouterr().out.strip() == str(tmp_path / 'lane')


@pytest.mark.parametrize(('argv', 'owner', 'result', 'expected'), [
    (['restore', 'fix/example'], 'restore_lane',
     (Path('/lanes/example'), 'restored; no uncommitted work was saved'),
     'restored; no uncommitted work was saved'),
    (['remove', 'fix/example'], 'remove_lane',
     (Path('/lanes/example'), 'spare'), '/lanes/example: spare'),
    (['prepare-spare'], 'prepare_spare', 'ready', 'ready'),
    (['save'], 'save_wip', 'deadbeef', 'deadbeef'),
])
def test_commands_delegate_to_their_single_owner(
        monkeypatch, tmp_path, capsys, argv, owner, result, expected):
    from lib.project_mod import lane_cli
    calls = []

    def record(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    lanes = _stub(monkeypatch, bound_task_checkout=lambda cwd, env: None,
                  **{owner: record})
    if argv[0] == 'restore':
        _stub(monkeypatch, **{'ensure_next_spare': lambda *a, **k: True})
    assert lane_cli.main(argv, cwd=tmp_path, environ={}) == 0
    assert calls
    assert expected in capsys.readouterr().out


def test_list_reports_divergence_and_uncommitted_files(monkeypatch, tmp_path, capsys):
    from lib.project_mod import lane_cli
    rows = [{'branch': 'fix/example', 'checkout': '/lanes/example',
             'behind': 2, 'ahead': 1, 'uncommitted': 3, 'savedWork': ''}]
    _stub(monkeypatch, list_lanes=lambda path: rows)
    assert lane_cli.main(['list'], cwd=tmp_path, environ={}) == 0
    out = capsys.readouterr().out
    assert 'fix/example' in out and 'ahead=1' in out and 'uncommitted=3' in out
    assert '/lanes/example' in out


@pytest.mark.parametrize('command', ['new', 'restore', 'save'])
def test_bound_platform_worker_is_sent_to_its_own_worktree(
        monkeypatch, tmp_path, capsys, command):
    from lib.project_mod import lane_cli

    def forbidden(*_args, **_kwargs):
        raise AssertionError('a bound Worker must not touch a lane')

    argv = {'new': ['new', 'fix/example'], 'restore': ['restore', 'fix/example'],
            'save': ['save']}[command]
    _stub(monkeypatch,
          bound_task_checkout=lambda cwd, env: tmp_path / 'bound',
          repository=forbidden, create_lane=forbidden, restore_lane=forbidden,
          save_wip=forbidden, remove_lane=forbidden, list_lanes=forbidden,
          prepare_spare=forbidden, ensure_next_spare=forbidden)
    assert lane_cli.main(argv, cwd=tmp_path, environ={}) == 1
    assert 'integration_checkpoint' in capsys.readouterr().err


def test_read_commands_are_not_blocked_by_the_worker_guard(
        monkeypatch, tmp_path, capsys):
    from lib.project_mod import lane_cli
    _stub(monkeypatch, bound_task_checkout=lambda cwd, env: tmp_path / 'bound',
          list_lanes=lambda path: [], prepare_spare=lambda path: 'present')
    assert lane_cli.main(['list'], cwd=tmp_path, environ={}) == 0
    assert lane_cli.main(['prepare-spare'], cwd=tmp_path, environ={}) == 0
    assert 'no lanes' in capsys.readouterr().out


def test_typed_lane_refusal_is_a_clean_exit(monkeypatch, tmp_path, capsys):
    from lib.project_mod import lane_cli, lanes

    def refuse(_path):
        raise lanes.LaneError('the main checkout has no branch checked out')

    _stub(monkeypatch, list_lanes=refuse)
    assert lane_cli.main(['list'], cwd=tmp_path, environ={}) == 1
    assert 'lane: the main checkout has no branch checked out' in capsys.readouterr().err


def test_ensure_next_spare_spawns_the_tracked_module(monkeypatch, tmp_path):
    """Double neuter: restoring the old private-script argv makes this red."""
    from lib.project_mod import lanes
    repo = SimpleNamespace(top=tmp_path)
    captured = {}

    def popen(argv, **kwargs):
        captured['argv'] = argv
        captured['kwargs'] = kwargs

    fake_subprocess = SimpleNamespace(Popen=popen, DEVNULL=-1, STDOUT=-2)
    _stub(monkeypatch, lane_root=lambda _repo: tmp_path,
          subprocess=fake_subprocess)
    assert lanes.ensure_next_spare(tmp_path, repo=repo) is True
    argv = captured['argv']
    assert argv[:3] == [sys.executable, '-m', 'lib.project_mod.lane_cli'], argv
    assert argv[3:] == ['prepare-spare']
    assert all('lane.py' not in part for part in argv)
    assert captured['kwargs']['cwd'] == str(tmp_path)
