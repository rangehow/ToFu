"""The router home is an owner-scoped Tofu-created Git repository (docs/ROUTER.md R1)."""
from __future__ import annotations

import os
import subprocess

import pytest

from lib.router.home import is_router_home, router_home_location, router_home_path

pytestmark = pytest.mark.unit


def _git(root, *args):
    result = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-C', str(root), *args],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / 'data' / 'router'
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(path))
    return path


def test_location_is_owner_scoped_and_creates_nothing(root):
    first, second = router_home_location(7), router_home_location(8)
    assert first != second and first.startswith(str(root.resolve()))
    assert not os.path.exists(first)
    assert is_router_home(7, first + '/') and not is_router_home(8, first)
    assert not is_router_home(7, '') and not is_router_home(7, os.path.dirname(first))
    with pytest.raises((TypeError, ValueError)):
        router_home_location(0)


def test_creation_is_an_idempotent_committed_repository(root):
    home = router_home_path(7)
    assert home == router_home_location(7)
    head = _git(home, 'rev-parse', 'HEAD')
    tracked = set(_git(home, 'ls-files').splitlines())
    assert {'README.md', '.gitignore', 'RESOURCES.md'} <= tracked
    assert _git(home, 'status', '--porcelain') == ''
    assert _git(home, 'rev-parse', '--show-toplevel') == home
    assert router_home_path(7) == home
    assert _git(home, 'rev-parse', 'HEAD') == head


def test_resource_registry_seeded_once_and_never_overwritten(root):
    from lib.router.home import owner_resources, router_home_location

    def read(home):
        return owner_resources(7, content=True)

    # A pre-existing home without a registry (the R13 gap) is seeded on the next
    # idempotent open; the path leads are given is the one that exists.
    home = router_home_path(7)
    os.unlink(os.path.join(home, 'RESOURCES.md'))
    assert read(home) == {'path': os.path.join(router_home_location(7), 'RESOURCES.md'),
                          'available': False}
    assert router_home_path(7) == home
    registry = read(home)
    assert registry['available'] is True and registry['bytes'] > 0
    seed = registry['content']
    assert seed.startswith('# Owner resource registry')
    assert 'Never store a secret here' in seed
    assert 'how to obtain' in seed
    # The registry is names and how to obtain them, never a credential value.
    # (Its agreement with the private Hope patrol's queue list is
    # tests/router/test_router_home_hope_seed.py, which stays private with
    # scripts/hope_patrol/.)
    for forbidden in ('ghp_', 'github_pat_', 'pypi-', 'BEGIN', 'Bearer '):
        assert forbidden not in seed
    # The seed matches the committed baseline byte for byte, so a home left at
    # its seed stays a clean repository.
    assert _git(home, 'status', '--porcelain') == ''
    assert _git(home, 'show', 'HEAD:RESOURCES.md') == seed.rstrip('\n')

    # Owner or reviewed-Worker edits win: the seed never restores or overwrites.
    edited = '# Owner resources\n\n| Resource | Detail |\n| H800 queue | 64 GPUs |\n'
    with open(os.path.join(home, 'RESOURCES.md'), 'w', encoding='utf-8') as stream:
        stream.write(edited)
    router_home_path(7)
    assert read(home)['content'] == edited


def test_resource_registry_seed_without_a_bundle_names_no_resources(root, monkeypatch):
    from lib.router.home import owner_resources
    monkeypatch.setattr('tofu_extensions.bundle_directories', lambda kind: [])
    router_home_path(7)
    seed = owner_resources(7, content=True)['content']
    assert seed.startswith('# Owner resource registry')
    assert 'Never store a secret here' in seed
    assert 'No resources are recorded yet' in seed
    assert 'hope_patrol' not in seed


def test_home_inside_a_source_checkout_is_its_own_repository(tmp_path, monkeypatch):
    outer = tmp_path / 'checkout'
    outer.mkdir()
    _git(outer, 'init', '-q')
    (outer / 'tracked.txt').write_text('outer\n')
    _git(outer, 'add', 'tracked.txt')
    _git(outer, '-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'outer')
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(outer / 'data' / 'router'))
    home = router_home_path(3)
    assert _git(home, 'rev-parse', '--show-toplevel') == home
    assert _git(outer, 'status', '--porcelain') in ('', '?? data/')


def test_git_metadata_commands_use_the_shared_activity_window(root, monkeypatch):
    from lib import git_runtime
    home = root / '7'
    home.mkdir(parents=True)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

    monkeypatch.setattr('lib.git_runtime.git_executable', lambda: '/usr/bin/git')
    monkeypatch.setattr(
        'lib.project_mod.activity_supervised_process.run_activity_supervised', run)
    from lib.router.home import _git
    assert _git(str(home), 'rev-parse', 'HEAD').returncode == 0
    argv, kwargs = calls[0]
    assert argv[:2] == ['/usr/bin/git', '-c']
    assert kwargs['stall_seconds'] == git_runtime.GIT_STALL_SECONDS
    assert 'timeout' not in kwargs


def test_git_probe_failure_is_retryable_and_preserves_private_diagnostics(
        root, monkeypatch, caplog):
    from lib.git_runtime import GitExecutableUnavailable
    from lib.router.home import RouterGitUnavailable
    home = root / '7'
    (home / '.git').mkdir(parents=True)
    monkeypatch.setattr(
        'lib.git_runtime.git_executable',
        lambda: (_ for _ in ()).throw(GitExecutableUnavailable('private loader detail')))
    with pytest.raises(RouterGitUnavailable, match='Retry opening') as failure:
        router_home_path(7)
    assert 'private loader detail' not in str(failure.value)
    assert 'private loader detail' in caplog.text


def test_existing_guards_accept_the_home_as_a_worker_project_root(root):
    from lib.tasks_pkg.workspace_delivery import isolated_workspace_refusal
    home = router_home_path(7)
    assert isolated_workspace_refusal(home) is None
