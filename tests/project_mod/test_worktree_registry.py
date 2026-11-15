"""worktree_registry answers what ``git worktree list --porcelain`` reports, without listing.

Every test builds its own repository under tmp_path and compares the registry's
answers with Git's own porcelain listing of the same repository.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from lib.project_mod import worktree_registry as registry

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def git(cwd, *args) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    result = subprocess.run(['git', '-C', str(cwd), *args], env=env, text=True,
                            capture_output=True, timeout=60)
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout


def listing(cwd) -> list[tuple[Path, str | None]]:
    """(path, branch) for every entry of Git's porcelain listing, in its order."""
    entries = []
    for block in git(cwd, 'worktree', 'list', '--porcelain').strip().split('\n\n'):
        lines = block.splitlines()
        branch = next((line[len('branch '):] for line in lines if line.startswith('branch ')),
                      None)
        entries.append((Path(lines[0][len('worktree '):]), branch))
    return entries


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    main = tmp_path / 'main'
    main.mkdir()
    git(main, 'init', '-q', '-b', 'main')
    git(main, 'config', 'user.name', 'Registry')
    git(main, 'config', 'user.email', 'registry@example.invalid')
    (main / 'file.txt').write_text('base\n')
    git(main, 'add', 'file.txt')
    git(main, 'commit', '-q', '-m', 'base')
    git(main, 'worktree', 'add', '-q', '-b', 'lane-a', str(tmp_path / 'lane-a'))
    git(main, 'worktree', 'add', '-q', '-b', 'lane-b', str(tmp_path / 'old-b'))
    (tmp_path / 'moved').mkdir()
    git(main, 'worktree', 'move', str(tmp_path / 'old-b'), str(tmp_path / 'moved' / 'lane-b'))
    git(main, 'worktree', 'add', '-q', '--detach', str(tmp_path / 'detached'))
    git(main, 'worktree', 'add', '-q', '-b', 'gone', str(tmp_path / 'gone'))
    shutil.rmtree(tmp_path / 'gone')  # registration stays: prunable, still listed
    git(main, 'worktree', 'add', '-q', '-b', 'locked', str(tmp_path / 'locked'))
    git(main, 'worktree', 'lock', str(tmp_path / 'locked'))
    (main / '.git' / 'worktrees' / 'stray').mkdir()  # no gitdir file: not a registration
    return main


def test_registry_reports_what_git_lists(repository: Path, tmp_path: Path):
    common = repository / '.git'
    listed = listing(repository)

    assert registry.registered_worktrees(common) == [path for path, _branch in listed]
    assert registry.main_worktree(common) == listed[0][0] == repository.resolve()
    assert registry.has_main_checkout(common)
    branches = {branch: path for path, branch in listed if branch}
    assert set(branches) == {'refs/heads/main', 'refs/heads/lane-a', 'refs/heads/lane-b',
                             'refs/heads/gone', 'refs/heads/locked'}
    for branch, path in branches.items():
        assert registry.worktree_with_branch(common, branch) == path
    assert registry.worktree_with_branch(common, 'refs/heads/absent') is None
    for path, _branch in listed:
        assert registry.is_registered(common, path)
    unregistered = tmp_path / 'unregistered'
    unregistered.mkdir()
    assert not registry.is_registered(common, unregistered)
    assert not registry.is_registered(common, tmp_path / 'old-b')


def test_registration_through_a_later_symlink_still_resolves(repository: Path, tmp_path: Path):
    """A moved data root leaves recorded paths behind a symlink; Path.resolve equality holds."""
    common = repository / '.git'
    (tmp_path / 'alias').symlink_to(tmp_path, target_is_directory=True)
    (common / 'worktrees' / 'lane-a' / 'gitdir').write_text(f'{tmp_path}/alias/lane-a/.git\n')
    real = tmp_path / 'real-target'
    real.mkdir()
    (tmp_path / 'final-link').symlink_to(real, target_is_directory=True)
    admin = common / 'worktrees' / 'linked-name'
    admin.mkdir()
    (admin / 'gitdir').write_text(f'{tmp_path}/final-link/.git\n')
    resolved = {path.resolve() for path, _branch in listing(repository)}

    for target in (tmp_path / 'lane-a', real):
        assert target.resolve() in resolved
        assert registry.is_registered(common, target)
    assert registry.registered_worktrees(common) == [path for path, _ in listing(repository)]


def test_repository_without_a_main_checkout(tmp_path: Path):
    bare = tmp_path / 'repo.git'
    git(tmp_path, 'init', '-q', '--bare', '-b', 'main', str(bare))
    assert registry.main_worktree(bare) == listing(bare)[0][0] == bare.resolve()
    assert not registry.has_main_checkout(bare)
    assert registry.worktree_with_branch(bare, 'refs/heads/main') is None


def _load_git_land():
    spec = importlib.util.spec_from_file_location('git_land_registry_test',
                                                  ROOT / 'scripts' / 'git_land.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_landing_lookups_read_the_registry_not_a_listing(repository: Path, tmp_path: Path,
                                                         monkeypatch):
    land = _load_git_land()
    original = land._git
    calls = []

    def observe(cwd, *args, **kwargs):
        calls.append(args)
        return original(cwd, *args, **kwargs)

    monkeypatch.setattr(land, '_git', observe)
    assert land._main_checkout(tmp_path / 'lane-a') == repository.resolve()
    assert land._worktree_of(repository, 'refs/heads/lane-b') == tmp_path / 'moved' / 'lane-b'
    assert land._worktree_of(repository, 'refs/heads/main') == repository.resolve()
    assert land._worktree_of(repository, 'refs/heads/absent') is None
    assert not [args for args in calls if args[:1] == ('worktree',)]

    bare = tmp_path / 'repo.git'
    git(tmp_path, 'init', '-q', '--bare', '-b', 'main', str(bare))
    with pytest.raises(land.Refused, match='no main checkout'):
        land._main_checkout(bare)
