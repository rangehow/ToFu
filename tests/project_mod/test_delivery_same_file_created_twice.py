"""A file the task created with bytes the branch also held is one file, not an add/add conflict.

Audit 2026-11-18 (assignment 0a147aa7): parallel 1b3b958a was held as "task and
canonical both created different content" at four infra/hope/*.sh scripts. The
Worker had copied them into the shared folder (Hope jobs read the shared volume),
and the next delegation's folder snapshot ("Tofu checkpoint: the folder contents
at delegation") committed them executable; the Worker's checkpoint held the same
bytes as 644. Nine other holds named files whose task bytes equal a version the
branch held since the task's baseline. Different bytes stay a held conflict.
"""
import pytest

from tests.project_mod.test_delivery_merged_branch import (
    canonical_commit, repo as repo, reviewed_workspace, settle, task_commit)
from tests.storage.test_integration_delivery import git

pytestmark = pytest.mark.unit

SCRIPT = '#!/bin/sh\necho elastic\n'


def _write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def test_the_folder_snapshot_of_the_tasks_own_file_lands_with_the_branch_version(repo, monkeypatch):
    root = repo[0]
    work = reviewed_workspace(repo, monkeypatch)
    task_commit(work, 'task: scripts', lambda w: (
        _write(w / 'infra/watch.sh', SCRIPT), _write(w / 'result.md', 'result\n')))
    canonical_commit(root, 'Tofu checkpoint: the folder contents at delegation',
                     lambda r: _write(r / 'infra/watch.sh', SCRIPT, 0o755))

    receipt = settle(repo)

    assert receipt['state'] == 'delivered', receipt
    assert git(root, 'ls-tree', 'HEAD', 'infra/watch.sh').split()[0] == '100755'
    assert git(root, 'show', 'HEAD:result.md') == 'result'


def test_bytes_the_branch_held_and_then_changed_keep_the_branch_change(repo, monkeypatch):
    root = repo[0]
    work = reviewed_workspace(repo, monkeypatch)
    task_commit(work, 'task: report', lambda w: _write(w / 'REPORT.md', 'draft\n'))
    canonical_commit(root, 'another delivery: report', lambda r: _write(r / 'REPORT.md', 'draft\n'))
    canonical_commit(root, 'lead edits the report', lambda r: _write(r / 'REPORT.md', 'draft\nreviewed\n'))

    receipt = settle(repo)

    assert receipt['state'] in ('delivered',), receipt
    assert (root / 'REPORT.md').read_text() == 'draft\nreviewed\n'


def test_two_different_versions_of_one_new_file_stay_held(repo, monkeypatch):
    root = repo[0]
    work = reviewed_workspace(repo, monkeypatch)
    task_commit(work, 'task: report', lambda w: _write(w / 'REPORT.md', 'task version\n'))
    canonical_commit(root, 'another worker: report', lambda r: _write(r / 'REPORT.md', 'other version\n'))

    receipt = settle(repo)

    assert receipt['state'] == 'blocked'
    assert 'both created different content at: REPORT.md' in receipt['reason']
