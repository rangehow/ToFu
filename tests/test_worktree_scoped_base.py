"""tests/test_worktree_scoped_base.py — build step 3 (§3.1/§3.2).

``scoped_base_path(project_path, conv_id)`` is the ONE seam that scopes project
FILE TOOLS (and ``run_command`` cwd) to a conversation's own git worktree. The
project tools thread ``project_path`` as an explicit parameter (no
``os.getcwd()`` dependence), so worktree-scoping is a path-resolution swap, not
a chdir. These pin:

  * OFF (default) → returns project_path UNCHANGED (byte-identical single-box);
    creates NO worktree.
  * ON → returns the conv's OWN worktree checkout; two convs get two distinct
    worktrees off the integration branch (structural isolation).
  * fail-open → a non-git path returns project_path unchanged (degrade to the
    shared checkout, never break the task).
  * a real file written under the scoped base lands in the conv's worktree, NOT
    the primary checkout (the isolation property step 3 delivers).
"""
from __future__ import annotations

import os
import subprocess

import pytest

from lib.conversations import project_worktree as pw

pytestmark = pytest.mark.unit


def _git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True,
                          capture_output=True, text=True)


def _seed_repo(tmp_path):
    repo = str(tmp_path)
    _git(repo, 'init', '-q')
    _git(repo, 'config', 'user.email', 'wt@test')
    _git(repo, 'config', 'user.name', 'wt')
    (tmp_path / 'base.txt').write_text('base\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', 'init')
    return repo


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv('TOFU_WORKTREE_ISOLATION', 'on')
    pw.reset_for_test()
    yield
    pw.reset_for_test()


def test_off_returns_project_path_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv('TOFU_WORKTREE_ISOLATION', raising=False)
    repo = _seed_repo(tmp_path)
    assert pw.scoped_base_path(repo, 'c1') == repo
    # No worktree artifacts created — single-box byte-identical.
    assert not os.path.exists(pw.worktrees_root(repo))


def test_on_returns_conv_worktree(tmp_path, on):
    repo = _seed_repo(tmp_path)
    scoped = pw.scoped_base_path(repo, 'convA')
    assert scoped != repo
    assert os.path.isdir(scoped)
    assert scoped == pw.worktree_path(repo, 'convA')
    # It's a real git worktree checked out from integration (base.txt present).
    assert os.path.exists(os.path.join(scoped, 'base.txt'))


def test_two_convs_get_distinct_worktrees(tmp_path, on):
    repo = _seed_repo(tmp_path)
    a = pw.scoped_base_path(repo, 'convA')
    b = pw.scoped_base_path(repo, 'convB')
    assert a != b
    assert os.path.isdir(a) and os.path.isdir(b)


def test_idempotent_same_conv_same_path(tmp_path, on):
    repo = _seed_repo(tmp_path)
    a1 = pw.scoped_base_path(repo, 'convA')
    a2 = pw.scoped_base_path(repo, 'convA')
    assert a1 == a2


def test_writes_land_in_worktree_not_primary(tmp_path, on):
    repo = _seed_repo(tmp_path)
    scoped = pw.scoped_base_path(repo, 'convA')
    # Simulate a tool write against the scoped base.
    with open(os.path.join(scoped, 'only_in_worktree.txt'), 'w') as f:
        f.write('x\n')
    # The primary checkout must NOT see it (isolation).
    assert not os.path.exists(os.path.join(repo, 'only_in_worktree.txt'))
    assert os.path.exists(os.path.join(scoped, 'only_in_worktree.txt'))


def test_non_git_path_fails_open(tmp_path, on):
    # A directory that is not a git repo → return project_path unchanged
    # (degrade to the shared checkout, never break the task).
    plain = str(tmp_path / 'plain')
    os.makedirs(plain)
    assert pw.scoped_base_path(plain, 'convA') == plain


def test_empty_inputs_passthrough(tmp_path, on):
    assert pw.scoped_base_path('', 'convA') == ''
    repo = _seed_repo(tmp_path)
    assert pw.scoped_base_path(repo, '') == repo


if __name__ == '__main__':
    import pytest as _pt
    _pt.main([__file__, '-v'])
