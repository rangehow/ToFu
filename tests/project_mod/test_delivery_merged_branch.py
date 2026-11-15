"""A Worker that merged the current branch publishes only its own delta.

Workers merge the shared branch into their worktree before saving a
checkpoint (AGENTS.md). The delivery's three-way base is then the merge base
of the checkpoint and the canonical branch, not the registered baseline:
content the branch already holds is never re-applied as the task's own, so it
neither collides with itself (a file canonical created read as "both created
different content") nor with later task edits inside merged regions. A real
conflict with a canonical change the task never merged is still held.
"""
import pytest

from lib import integration_control as ic, integration_state_repository as state
from lib.project_dispatch import publication
from lib.project_dispatch.admission import KEY
from lib.project_mod import delivery
from tests.storage.test_integration_delivery import automatic_workspace, git, repo as repo

pytestmark = pytest.mark.unit

LINES = ''.join(f'line {number}\n' for number in range(1, 21))


def replace_line(path, number, text):
    path.write_text(path.read_text().replace(f'line {number}\n', f'{text}\n', 1))


def reviewed_workspace(repo, monkeypatch):
    root, _work, owner = repo
    (root / 'shared.txt').write_text(LINES)
    git(root, 'add', 'shared.txt')
    git(root, 'commit', '-m', 'shared')
    git(root, 'config', '--local', 'tofu.linearCheckpoint', 'true')
    monkeypatch.setattr(publication, 'publication_reason', lambda row, receipt: '')
    work = automatic_workspace(repo, monkeypatch)
    state.set_workspace_meta(project_root=str(root), task_id='automatic', user_id=owner,
                             now=ic._now(), patch={KEY: {'assignmentId': 'assignment-1'}})
    return work


def canonical_commit(root, message, edit):
    edit(root)
    git(root, 'add', '-A')
    git(root, 'commit', '-m', message)


def task_commit(work, message, edit):
    edit(work)
    git(work, 'add', '-A')
    git(work, 'commit', '-m', message)


def settle(repo):
    root, _work, owner = repo
    delivery.settle_task_workspace(str(root), 'automatic', user_id=owner, outcome='completed')
    delivery.resume_pending_workspace_deliveries()
    return state.get_workspace(str(root), 'automatic', user_id=owner)['origin']['delivery']


def canonical_adds_file_and_edits_shared(root):
    (root / 'canonical.txt').write_text('created by canonical\n')
    replace_line(root / 'shared.txt', 5, 'line 5 (canonical)')


def test_merged_branch_publishes_cleanly_and_keeps_canonical_files_identical(repo, monkeypatch):
    root, _work, _owner = repo
    work = reviewed_workspace(repo, monkeypatch)
    task_commit(work, 'task: first step', lambda w: (w / 'step.txt').write_text('step\n'))
    canonical_commit(root, 'canonical: add file', canonical_adds_file_and_edits_shared)
    git(work, 'merge', '--no-edit', 'main')
    # Edits inside the region the merged commit changed, plus a created file:
    # diffed from the registered baseline this conflicted with itself.
    task_commit(work, 'task: second step', lambda w: (
        replace_line(w / 'shared.txt', 6, 'line 6 (task)'),
        (w / 'feature.txt').write_text('task feature\n')))
    canonical = git(root, 'rev-parse', 'HEAD')

    receipt = settle(repo)

    assert receipt['state'] == 'delivered' and receipt['publicationMode'] == 'committed', receipt
    assert git(root, 'rev-parse', 'HEAD^') == canonical
    assert git(root, 'show', 'HEAD:canonical.txt') == 'created by canonical'
    assert git(root, 'rev-parse', 'HEAD:canonical.txt') == git(root, 'rev-parse', f'{canonical}:canonical.txt')
    shared = git(root, 'show', 'HEAD:shared.txt')
    assert 'line 5 (canonical)\nline 6 (task)\n' in shared
    assert git(root, 'show', 'HEAD:feature.txt') == 'task feature'
    assert git(root, 'show', 'HEAD:step.txt') == 'step'
    assert git(root, 'status', '--porcelain') == ''
    message = git(root, 'log', '-1', '--format=%B')
    assert 'task: first step' in message and 'task: second step' in message
    assert 'canonical: add file' not in message


def test_task_edit_of_a_file_canonical_created_is_not_an_add_add_conflict(repo, monkeypatch):
    root, _work, _owner = repo
    work = reviewed_workspace(repo, monkeypatch)
    canonical_commit(root, 'canonical: add file', canonical_adds_file_and_edits_shared)
    git(work, 'merge', '--no-edit', 'main')
    task_commit(work, 'task: edit the canonical file',
                lambda w: (w / 'canonical.txt').write_text('created by canonical\nedited by task\n'))

    receipt = settle(repo)

    assert receipt['state'] == 'delivered', receipt
    assert git(root, 'show', 'HEAD:canonical.txt') == 'created by canonical\nedited by task'
    assert 'line 5 (canonical)' in git(root, 'show', 'HEAD:shared.txt')


def test_conflict_with_a_canonical_change_the_task_never_merged_is_held(repo, monkeypatch):
    root, _work, _owner = repo
    work = reviewed_workspace(repo, monkeypatch)
    canonical_commit(root, 'canonical: add file', canonical_adds_file_and_edits_shared)
    git(work, 'merge', '--no-edit', 'main')
    canonical_commit(root, 'canonical: line 10',
                     lambda r: replace_line(r / 'shared.txt', 10, 'line 10 (canonical)'))
    task_commit(work, 'task: line 10', lambda w: (
        replace_line(w / 'shared.txt', 10, 'line 10 (task)'),
        (w / 'feature.txt').write_text('task feature\n')))
    head = git(root, 'rev-parse', 'HEAD')

    receipt = settle(repo)

    assert receipt['state'] == 'blocked' and receipt['phase'] == 'commit_adoption', receipt
    assert receipt['reason'] == 'task_delta_conflict: three-way merge conflicts at: shared.txt'
    assert receipt['retry']['conflictPaths'] == ['shared.txt']
    assert git(root, 'rev-parse', 'HEAD') == head
    assert git(root, 'status', '--porcelain') == ''
