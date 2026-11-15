"""A real linked git worktree for write-boundary tests.

``git worktree add`` produces the production ``.git`` gitfile layout that the
write path's same-repo rule (``lib/project_mod/write_tools/_paths.py``) reads,
so tests exercise the only way a write may extend the workspace.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest


def add_linked_worktree(repository: str, worktree: str) -> None:
    """Commit once in ``repository`` and check out ``worktree`` as its linked worktree.

    ``worktree`` must not exist yet. Skips the calling test when git is absent.
    """
    git = shutil.which('git')
    if not git:
        raise unittest.SkipTest('git binary not available')

    def run(*args: str) -> None:
        subprocess.run([git, *args], cwd=repository, check=True, capture_output=True)

    run('init', '-q')
    run('config', 'user.email', 't@t')
    run('config', 'user.name', 't')
    with open(os.path.join(repository, 'README'), 'w', encoding='utf-8') as handle:
        handle.write('x\n')
    run('add', 'README')
    run('commit', '-q', '-m', 'init')
    # Git 2.11 supports linked worktrees but not ``worktree add -q``.
    run('worktree', 'add', worktree)
