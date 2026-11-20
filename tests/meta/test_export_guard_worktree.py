"""The privacy drift gate runs in linked worktrees as well as ordinary clones."""
import os
import subprocess
import sys
from types import ModuleType

import pytest

from tests.meta import test_gitignore_covers_export_excludes as guard

pytestmark = pytest.mark.unit


def test_linked_worktree_is_not_mistaken_for_an_export(tmp_path, monkeypatch):
    repository = tmp_path / 'repository'
    repository.mkdir()
    subprocess.run(['git', 'init', '--template=', str(repository)], check=True,
        capture_output=True)
    subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Test',
        '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'initial'],
        check=True, capture_output=True)
    worktree = tmp_path / 'linked'
    subprocess.run(['git', '-C', str(repository), 'worktree', 'add', '--detach', str(worktree)],
        check=True, capture_output=True)
    assert (worktree / '.git').is_file()
    monkeypatch.setattr(guard, '_ROOT', str(worktree))
    try:
        guard._require_git_repo()
    except pytest.skip.Exception:
        pytest.fail('a valid linked Git worktree must execute the privacy drift guard')


def test_membership_diagnostic_is_independent_of_ignore_status(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith('GIT_'):
            monkeypatch.delenv(name)
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_SYSTEM', os.devnull)
    subprocess.run(['git', 'init', '--template=', str(tmp_path)], check=True,
        capture_output=True)
    (tmp_path / '.gitignore').write_text('/excluded/*\n!/excluded/exception.txt\n')
    (tmp_path / 'excluded').mkdir()
    paths = ['excluded/ignored.txt', 'excluded/exception.txt', 'excluded/keeper.txt']
    for path in paths:
        (tmp_path / path).write_text('synthetic fixture\n')
    subprocess.run(['git', '-C', str(tmp_path), 'add', '-f', '--', *paths],
        check=True, capture_output=True)
    ignored = subprocess.run(['git', '-C', str(tmp_path), '-c',
        'core.excludesFile=/dev/null', 'check-ignore', '--no-index', '--', *paths],
        check=True, capture_output=True, text=True)
    assert ignored.stdout.splitlines() == [paths[0], paths[2]]

    export = ModuleType('export')
    export.__dict__.update(
        ALWAYS_EXCLUDE_ROOT_ONLY_DIRS=(),
        _TOP_LEVEL_ONLY_EXCLUDE_DIRS=(),
        _TOP_LEVEL_ONLY_OPENSOURCE_EXCLUDE_DIRS={'excluded'},
        _OPENSOURCE_EXCLUDE_PATHS=(),
        ALWAYS_EXCLUDE_DIRS=(),
        OPENSOURCE_EXTRA_EXCLUDE_DIRS=(),
        ALWAYS_EXCLUDE_FILES=(),
        OPENSOURCE_EXTRA_EXCLUDE_FILES=(),
        ALWAYS_EXCLUDE_ROOT_ONLY_FILES=(),
        ALWAYS_EXCLUDE_GLOBS=(),
        _OPENSOURCE_KEEP_FILES={paths[2]},
    )
    monkeypatch.setitem(sys.modules, 'export', export)
    monkeypatch.setattr(guard, '_ROOT', str(tmp_path))
    tracked = guard._tracked_files()
    assert tracked == sorted(paths)
    assert guard._offenders(export, tracked) == sorted(paths[:2])
    with pytest.raises(AssertionError) as failure:
        guard.test_no_tracked_file_is_opensource_excluded()
    message = str(failure.value)
    assert '2 git-tracked file(s)' in message
    assert 'without a documented keeper' in message
    assert 'NOT gitignored' not in message
    assert 'Resolve tracking intent' in message
    assert all(path in message for path in paths[:2])
    assert paths[2] not in message
    monkeypatch.setattr(guard, '_tracked_files', lambda: [paths[2]])
    guard.test_no_tracked_file_is_opensource_excluded()


def test_invalid_git_inventory_cannot_become_an_empty_snapshot(tmp_path, monkeypatch):
    (tmp_path / '.git').write_text('gitdir: missing-git-directory\n')
    monkeypatch.setattr(guard, '_ROOT', str(tmp_path))
    with pytest.raises(subprocess.CalledProcessError):
        guard._tracked_files()
