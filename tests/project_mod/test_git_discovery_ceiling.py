"""Tofu's repository discovery stops at GIT_CEILING_DIRECTORIES, as Git's does.

2026-11-11 19:09: a misdirected ``git init`` left a repository at /tmp/.git.
Every path under /tmp then resolved to it: plain-folder specs bound /tmp as a
checkout and failed, and the write-tool radar recorded test writes into
/tmp/.git/tofu-radar. Git's own discovery, run on the same directories with the
same environment, is the oracle for every walker here.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from lib.git_runtime import beyond_git_ceiling
from lib.project_mod import integration_radar
from lib.tasks_pkg import workspace_delivery

pytestmark = pytest.mark.unit


def git(cwd, *args):
    cp = subprocess.run(['git', '-C', str(cwd), *args], text=True,
                        capture_output=True, timeout=60)
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()


def git_toplevel(directory: Path) -> Path | None:
    """The working tree Git itself discovers from DIRECTORY, or None."""
    cp = subprocess.run(['git', '-C', str(directory), 'rev-parse', '--show-toplevel'],
                        text=True, capture_output=True, timeout=60)
    return Path(cp.stdout.strip()).resolve() if cp.returncode == 0 else None


def walker_roots(directory: Path) -> dict[str, Path | None]:
    """What each Tofu walker discovers for a file in DIRECTORY."""
    found = integration_radar.find_checkout(directory)
    located = integration_radar.locate(directory / 'written.txt')
    bound = workspace_delivery._checkout(str(directory / 'written.txt'))
    return {'find_checkout': found and found.root,
            'locate': located and located[0].root,
            '_checkout': bound and Path(bound[0])}


@pytest.fixture
def stray(tmp_path):
    """A repository above the directories under test, as /tmp/.git was."""
    root = tmp_path.resolve() / 'stray'
    (root / 'ceiling' / 'plain').mkdir(parents=True)
    git(root, 'init', '-q')
    return root


def test_no_walker_resolves_a_repository_above_the_ceiling(stray, monkeypatch):
    plain = stray / 'ceiling' / 'plain'
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(stray / 'ceiling'))

    assert git_toplevel(plain) is None
    assert walker_roots(plain) == dict.fromkeys(walker_roots(plain))


def test_the_test_environment_hides_repositories_above_its_temporary_roots(
        tmp_path, tmp_path_factory):
    # tests/conftest.py sets the ceiling for every test; a stray repository at
    # either temporary root, like /tmp/.git, can never capture a test path.
    above = [tmp_path_factory.getbasetemp().parent]
    if tmp_path.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
        above.append(Path(tempfile.gettempdir()))
    assert all(beyond_git_ceiling(tmp_path, root) for root in above)
    assert not beyond_git_ceiling(tmp_path, tmp_path)


def test_the_radar_writes_nothing_into_a_repository_above_the_ceiling(stray, monkeypatch):
    plain = stray / 'ceiling' / 'plain'
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(stray / 'ceiling'))
    (plain / 'written.txt').write_text('test output\n')
    task = {'id': 'ceiling-task', 'convId': 'conv-ceiling'}

    assert integration_radar.note_writes(task, str(plain), ['written.txt']) == []
    assert not (stray / '.git' / integration_radar.RADAR_DIR).exists()


@pytest.mark.parametrize('ceiling, start', [
    ('', 'ceiling/plain'),  # no ceiling
    ('ceiling', 'ceiling/plain'),  # proper ancestor below the repository
    ('.', 'ceiling/plain'),  # the repository's own root
    ('ceiling', 'ceiling'),  # the start directory itself is always searched
    ('ceiling/plain/deeper', 'ceiling/plain'),  # not an ancestor of the start
    ('relative/ceiling', 'ceiling/plain'),  # relative entries are ignored
    ('ceiling', 'ceiling/inner/sub'),  # a repository below the ceiling stays visible
    ('/', 'ceiling/plain'),  # only the root directory itself is excluded
    ('link', 'ceiling/plain'),  # a symlinked entry is resolved
    (':link', 'ceiling/plain'),  # ...unless it follows an empty entry
])
def test_every_walker_agrees_with_git(stray, monkeypatch, ceiling, start):
    (stray / 'ceiling' / 'inner' / 'sub').mkdir(parents=True)
    git(stray / 'ceiling' / 'inner', 'init', '-q')
    (stray.parent / 'link').symlink_to(stray / 'ceiling', target_is_directory=True)

    def absolute(entry):
        if not entry or entry.startswith(('/', 'relative')):
            return entry
        return str(stray.parent / entry if entry == 'link' else stray / entry)

    monkeypatch.setenv('GIT_CEILING_DIRECTORIES',
                       os.pathsep.join(absolute(entry) for entry in ceiling.split(':')))
    directory = stray / start

    expected = git_toplevel(directory)
    assert walker_roots(directory) == dict.fromkeys(walker_roots(directory), expected)
