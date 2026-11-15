"""scripts/git_task_ref_archive.py: per-task refs of finished assignments.

Every test builds its own repository under tmp_path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts' / 'git_task_ref_archive.py'
CACHE_TAG = 'Signature: 8a477f597d28d172789f06886806bc55\n'


def git(cwd, *args, stdin=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    result = subprocess.run(['git', '-C', str(cwd), *args], env=env, text=True, input=stdin,
                            capture_output=True, timeout=60)
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout.strip()


def run(repo, *args):
    env = {**os.environ, 'PYTHONPATH': str(ROOT)}
    result = subprocess.run([sys.executable, str(SCRIPT), '--repo', str(repo), '--json', *args],
                            env=env, text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def snapshot(repo, files: dict[str, str], parent: str) -> str:
    """A checkpoint-style commit: FILES written over PARENT's tree, HEAD and index untouched."""
    index = repo / '.git' / 'snapshot-index'
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env['GIT_INDEX_FILE'] = str(index)
    subprocess.run(['git', '-C', str(repo), 'read-tree', parent], env=env, check=True)
    for name, content in files.items():
        blob = git(repo, 'hash-object', '-w', '--stdin', stdin=content)
        subprocess.run(['git', '-C', str(repo), 'update-index', '--add', '--cacheinfo',
                        f'100644,{blob},{name}'], env=env, check=True)
    tree = subprocess.run(['git', '-C', str(repo), 'write-tree'], env=env, check=True,
                          capture_output=True, text=True).stdout.strip()
    index.unlink()
    return git(repo, 'commit-tree', tree, '-p', parent, '-m', 'checkpoint')


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'config', 'user.name', 'Archive')
    git(repo, 'config', 'user.email', 'archive@example.invalid')
    (repo / 'app.py').write_text('v1\n')
    git(repo, 'add', 'app.py')
    git(repo, 'commit', '-q', '-m', 'base')
    return repo


def test_finished_task_refs_are_archived_or_dropped_and_live_ones_stay(repo, tmp_path):
    base = git(repo, 'rev-parse', 'HEAD')
    unmerged = snapshot(repo, {'app.py': 'v2 work\n',
                               'packages/x/target/CACHEDIR.TAG': CACHE_TAG,
                               'packages/x/target/debug/huge.rlib': 'build output\n'}, base)
    git(repo, 'update-ref', 'refs/tofu/checkpoints/u1/finished-aaaa', unmerged)
    git(repo, 'update-ref', 'refs/tofu/inputs/u1/finished-aaaa', base)
    plain = snapshot(repo, {'notes.md': 'unmerged notes\n'}, base)
    git(repo, 'update-ref', 'refs/tofu/workspace-wip/u1/discarded-bbbb', plain)
    live = snapshot(repo, {'app.py': 'live\n'}, base)
    git(repo, 'update-ref', 'refs/tofu/checkpoints/u1/live-cccc', live)
    git(repo, 'worktree', 'add', '-q', '--detach', str(tmp_path / 'live-cccc'), base)
    status = tmp_path / 'status.json'
    status.write_text(json.dumps({'data': {'workspaces': [
        {'path': str(tmp_path / 'gone' / 'restoring-dddd')}]}}))
    restoring = snapshot(repo, {'app.py': 'restore me\n'}, base)
    git(repo, 'update-ref', 'refs/tofu/checkpoints/u1/restoring-dddd', restoring)

    report = run(repo, '--branch', 'main', '--live-status', str(status), '--apply')

    actions = {entry['ref']: entry['action'] for entry in report['entries']}
    assert actions == {'refs/tofu/checkpoints/u1/finished-aaaa': 'archive',
                       'refs/tofu/inputs/u1/finished-aaaa': 'delete-reachable',
                       'refs/tofu/workspace-wip/u1/discarded-bbbb': 'archive'}
    remaining = git(repo, 'for-each-ref', '--format=%(refname)', 'refs/tofu').split()
    assert remaining == ['refs/tofu/checkpoints/u1/live-cccc',
                         'refs/tofu/checkpoints/u1/restoring-dddd']

    restore = tmp_path / 'restore'
    git(tmp_path, 'clone', '-q', str(repo), str(restore))
    git(restore, 'fetch', '-q', report['bundle'], 'refs/tofu/archived/*:refs/tofu/archived/*')
    archived = {ref.split('/', 4)[-1]: ref for ref in git(
        restore, 'for-each-ref', '--format=%(refname)', 'refs/tofu/archived').split()}
    work = archived['checkpoints/u1/finished-aaaa']
    assert git(restore, 'show', f'{work}:app.py') == 'v2 work'
    assert git(restore, 'ls-tree', '-r', '--name-only', work).split() == ['app.py']
    assert f'Tofu-Original-Commit: {unmerged}' in git(restore, 'log', '-1', '--format=%B', work)
    assert git(restore, 'rev-parse', archived['workspace-wip/u1/discarded-bbbb']) == plain


def test_the_default_run_changes_nothing(repo):
    base = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'update-ref', 'refs/tofu/checkpoints/u1/finished-aaaa',
        snapshot(repo, {'app.py': 'v2\n'}, base))

    report = run(repo, '--branch', 'main')

    assert [entry['action'] for entry in report['entries']] == ['archive']
    assert git(repo, 'for-each-ref', 'refs/tofu') != ''
    assert not (repo / 'data' / 'git-hygiene').exists()


def test_a_ref_moved_after_planning_keeps_every_ref(repo, monkeypatch):
    sys.path.insert(0, str(ROOT))
    from scripts import git_task_ref_archive as archive

    base = git(repo, 'rev-parse', 'HEAD')
    first = snapshot(repo, {'app.py': 'v2\n'}, base)
    git(repo, 'update-ref', 'refs/tofu/checkpoints/u1/finished-aaaa', first)
    git(repo, 'update-ref', 'refs/tofu/inputs/u1/finished-aaaa', base)
    report = archive.plan(repo, base, set())
    git(repo, 'update-ref', 'refs/tofu/checkpoints/u1/finished-aaaa',
        snapshot(repo, {'app.py': 'v3\n'}, base))

    with pytest.raises(archive.HygieneError):
        archive.apply(repo, report)

    assert len(git(repo, 'for-each-ref', 'refs/tofu/checkpoints', 'refs/tofu/inputs').split('\n')) == 2
