"""Git admission skips broken binaries without retrying repository operations."""
import os
import subprocess
import threading

import pytest

from lib import git_runtime
from lib.tasks_pkg import workspace_delivery

pytestmark = pytest.mark.unit


def test_binary_probe_does_not_inherit_checkout_directory(tmp_path, monkeypatch):
    executable = tmp_path / 'git'
    executable.write_text('#!/bin/sh\necho "git version synthetic"\n')
    executable.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.chdir(tmp_path)
    run = subprocess.run

    def stalled_checkout(command, **kwargs):
        if kwargs.get('cwd') in (None, str(tmp_path)):
            raise subprocess.TimeoutExpired(command, 5)
        return run(command, **kwargs)

    monkeypatch.setattr(subprocess, 'run', stalled_checkout)
    assert git_runtime.git_executable() == str(executable)


def test_incompatible_path_git_does_not_block_workspace(tmp_path, monkeypatch):
    broken = tmp_path / 'conda'
    broken.mkdir()
    executable = broken / 'git'
    executable.write_text('#!/bin/sh\necho "GLIBC_2.28 not found" >&2\nexit 1\n')
    executable.chmod(0o755)
    repository = tmp_path / 'repository'
    repository.mkdir()
    good_git = git_runtime.git_executable()
    subprocess.run([good_git, '-C', str(repository), 'init'], check=True,
                   capture_output=True)
    monkeypatch.setenv('PATH', str(broken) + os.pathsep + os.environ['PATH'])
    assert git_runtime.git_executable() != str(executable)
    assert workspace_delivery._checkout(str(repository)) == (str(repository), str(repository))


def test_slow_binary_probe_is_bounded_by_activity_not_five_seconds(tmp_path, monkeypatch):
    executable = tmp_path / 'git'
    executable.write_text('#!/bin/sh\nsleep 6\necho "git version synthetic"\n')
    executable.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path))
    assert git_runtime.git_executable() == str(executable)


def test_failed_probe_is_not_cached(tmp_path, monkeypatch):
    from lib.project_mod.activity_supervised_process import ProcessStalled
    executable = tmp_path / 'git'
    executable.write_text('#!/bin/sh\necho "git version synthetic"\n')
    executable.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.setattr(os, 'defpath', str(tmp_path))
    attempts = 0

    def probe(argv, **kwargs):
        nonlocal attempts
        attempts += 1
        assert argv == [str(executable), '--version']
        assert kwargs['stall_seconds'] == git_runtime.GIT_STALL_SECONDS
        if attempts == 1:
            raise ProcessStalled('temporarily unresponsive')
        return subprocess.CompletedProcess(argv, 0, stdout='git version synthetic\n', stderr='')

    monkeypatch.setattr(
        'lib.project_mod.activity_supervised_process.run_activity_supervised', probe)
    with pytest.raises(git_runtime.GitExecutableUnavailable, match='temporarily unresponsive'):
        git_runtime.git_executable()
    assert git_runtime.git_executable() == str(executable)
    assert attempts == 2


def test_slow_probe_does_not_hold_the_selection_cache_lock(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def blocked(path, library_path, preload):
        entered.set()
        assert release.wait(30)
        return '/slow/git'

    monkeypatch.setattr(git_runtime, '_resolve', blocked)
    monkeypatch.setattr(git_runtime, '_RESOLVED_GITS', {})
    monkeypatch.setenv('PATH', str(tmp_path / 'slow'))
    selected = []
    waiter = threading.Thread(target=lambda: selected.append(git_runtime.git_executable()))
    waiter.start()
    assert entered.wait(30)
    monkeypatch.setenv('PATH', str(tmp_path / 'other'))
    monkeypatch.setattr(git_runtime, '_resolve', lambda *_: '/other/git')
    assert git_runtime.git_executable() == '/other/git'
    release.set()
    waiter.join(30)
    assert not waiter.is_alive() and selected == ['/slow/git']


def test_no_runnable_git_retains_loader_failure_and_can_recover(tmp_path, monkeypatch):
    broken = tmp_path / 'git'
    broken.write_text('#!/bin/sh\necho "GLIBC_2.28 not found" >&2\nexit 1\n')
    broken.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.setattr(os, 'defpath', str(tmp_path))
    with pytest.raises(git_runtime.GitExecutableUnavailable, match='GLIBC_2.28 not found'):
        git_runtime.git_executable()
    broken.write_text('#!/bin/sh\necho "git version synthetic"\n')
    assert git_runtime.git_executable() == str(broken)


def test_repository_failure_keeps_git_stderr(tmp_path):
    (tmp_path / '.git').mkdir()
    with pytest.raises(workspace_delivery.WorkspaceBindingError, match='(?i)not a git repository'):
        workspace_delivery._checkout(str(tmp_path))
