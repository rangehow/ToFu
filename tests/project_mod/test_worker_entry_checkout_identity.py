"""Worker entry asks Git for a checkout's identity once per attempt.

Binding (``workspace_delivery._checkout``) identifies each writable root; the
isolated-workspace signal at the same worker entry reuses that top level instead
of spawning ``git rev-parse --show-toplevel`` again.
"""
import subprocess

import pytest

import lib.integration_control as integration
from lib.conversations.project_brain import deterministic_work_id, note_isolated_workspace_signal
from lib.tasks_pkg import workspace_delivery

pytestmark = pytest.mark.unit


def git(root, *args):
    subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, text=True,
                   timeout=20)


@pytest.fixture
def committed(tmp_path):
    root = (tmp_path / 'repo')
    root.mkdir()
    root = root.resolve()
    git(root, 'init', '-q', '-b', 'main')
    git(root, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
        'commit', '-q', '--allow-empty', '-m', 'seed')
    return root


def test_admission_identity_names_the_top_level_git_resolves(committed, tmp_path):
    sub = committed / 'pkg'
    sub.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(committed, target_is_directory=True)
    linked = tmp_path / 'linked'
    git(committed, 'worktree', 'add', '-q', '-b', 'side', str(linked))
    for path in (committed, sub, alias, alias / 'pkg', linked):
        assert workspace_delivery._checkout(str(path))[0] == str(integration._repo_root(str(path)))


def test_signal_reuses_the_admitted_checkout_root(committed, monkeypatch):
    task = {'id': 'task-signal', 'convId': 'conv-signal', '_userId': 1,
            'config': {'projectPath': str(committed)},
            '_admittedCheckoutRoots': {str(committed): str(committed)}}
    monkeypatch.setattr(integration, '_repo_root',
                        lambda path: pytest.fail('worker entry asked Git for the checkout again'))
    looked_up = []
    monkeypatch.setattr(integration._state, 'find_workspace',
                        lambda root, work_id, *, user_id: looked_up.append((root, work_id)))

    assert note_isolated_workspace_signal(task, str(committed)) == ''
    assert looked_up == [(str(committed), deterministic_work_id('task-signal'))]


def test_signal_for_a_folder_admission_found_no_checkout_in_asks_nothing(tmp_path, monkeypatch):
    plain = tmp_path / 'plain'
    plain.mkdir()
    assert workspace_delivery._checkout(str(plain)) is None
    task = {'id': 'task-plain', 'convId': 'conv-plain', '_userId': 1,
            'config': {'projectPath': str(plain)}, '_admittedCheckoutRoots': {str(plain.resolve()): ''}}
    monkeypatch.setattr(integration, 'has_active_workspace_for_work',
                        lambda *args, **kwargs: pytest.fail('no checkout has no isolated workspace'))

    assert note_isolated_workspace_signal(task, str(plain)) == ''


def test_signal_without_an_admitted_root_still_resolves_it(committed, monkeypatch):
    task = {'id': 'task-unbound', 'convId': 'conv-unbound', '_userId': 1,
            'config': {'projectPath': str(committed)}}
    looked_up = []
    monkeypatch.setattr(integration._state, 'find_workspace',
                        lambda root, work_id, *, user_id: looked_up.append(root))

    assert note_isolated_workspace_signal(task, str(committed)) == ''
    assert looked_up == [str(committed)]
