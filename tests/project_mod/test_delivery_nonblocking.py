"""Completion and worker startup never wait for another task's merge.

Owner decision 2026-11-11: a task's completion is acknowledged once its delivery
intent is durable, and the existing delivery worker merges; repository admission
covers only delta-sized publication, so a worker binding never waits behind a
capture, merge or checkout removal (docs/modules/project_integration.md).
"""
from concurrent.futures import ThreadPoolExecutor
import queue
import threading

import pytest

from lib import integration_control as ic
from lib import integration_state_repository as state
from lib.project_mod import delivery, task_delta_delivery
from lib.tasks_pkg import workspace_delivery
from tests.storage.test_integration_delivery import repo as _repo_fixture, git, automatic_workspace

repo = _repo_fixture
pytestmark = pytest.mark.unit
# Generous only because the host may be loaded; a binding needs about a second.
_BINDING_SECONDS = 60


def _receipt(root, owner, task_id='automatic'):
    return state.get_workspace(str(root), task_id, user_id=owner)['origin']['delivery']


def _completed_task(root, work, owner, task_id='automatic'):
    return {'id': task_id, 'convId': 'conv', '_userId': owner, 'status': 'done',
            'finishReason': 'stop', 'config': {'projectPath': str(work)},
            '_workspaceDelivery': {'repositories': [{
                'canonicalRoot': str(root), 'workspacePath': str(work), 'workId': task_id}]}}


def _block_merges(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = task_delta_delivery._merge_workspace_delta

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(_BINDING_SECONDS), 'test failed to release the merge'
        return original(*args, **kwargs)

    monkeypatch.setattr(task_delta_delivery, '_merge_workspace_delta', blocked)
    return entered, release


def test_completion_is_acknowledged_while_the_merge_is_blocked(repo, monkeypatch):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('task result\n')
    entered, release = _block_merges(monkeypatch)
    task = _completed_task(root, work, owner)
    with ThreadPoolExecutor(max_workers=1) as pool:
        acknowledged = pool.submit(workspace_delivery.persist_terminal_workspace_intent, task)
        try:
            assert acknowledged.result(timeout=_BINDING_SECONDS) is True
            assert not entered.is_set(), 'completion ran the merge'
        finally:
            release.set()
    receipt = _receipt(root, owner)
    assert (receipt['state'], receipt['phase'], receipt['taskOutcome']) == (
        'pending', 'checkpoint', 'completed')
    assert not (root / 'task.txt').exists()


def test_the_delivery_worker_merges_the_acknowledged_intent(repo, monkeypatch):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('task result\n')
    assert workspace_delivery.persist_terminal_workspace_intent(
        _completed_task(root, work, owner)) is True
    assert _receipt(root, owner)['state'] == 'pending'
    assert delivery.resume_pending_workspace_deliveries()
    receipt = _receipt(root, owner)
    assert receipt['state'] == 'delivered', receipt
    assert receipt['checkpointSha']
    assert (root / 'task.txt').read_text() == 'task result\n'


def test_a_worker_binding_proceeds_in_every_heavy_phase_of_another_delivery(repo, monkeypatch):
    """Captures, the merge and checkout removal hold no repository admission."""
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('task result\n')
    phases = queue.Queue()
    releases = []

    def blocking(name, original):
        def heavy(*args, **kwargs):
            if threading.current_thread().name == 'merging-task':
                release = threading.Event()
                releases.append(release)
                phases.put((name, release))
                assert release.wait(_BINDING_SECONDS * 3), 'test failed to release ' + name
            return original(*args, **kwargs)
        return heavy

    for module, name in [(ic, '_alternate_index_tree'),
                         (task_delta_delivery, '_merge_workspace_delta'),
                         (ic, '_remove_controlled_worktree_owned')]:
        monkeypatch.setattr(module, name, blocking(name, getattr(module, name)))
    settled = {}
    merging = threading.Thread(name='merging-task', target=lambda: settled.update(
        receipt=delivery.settle_task_workspace(str(root), 'automatic', user_id=owner,
                                               outcome='completed')))
    seen = []
    merging.start()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            while merging.is_alive() or not phases.empty():
                try:
                    name, release = phases.get(timeout=0.2)
                except queue.Empty:
                    continue
                seen.append(name)
                starter = f'starter-{len(seen)}'
                bound = pool.submit(automatic_workspace, repo, monkeypatch, starter)
                assert bound.result(timeout=_BINDING_SECONDS).is_dir(), name
                release.set()
    finally:
        for release in releases:
            release.set()
        merging.join(_BINDING_SECONDS)
    assert settled['receipt']['state'] == 'delivered', settled
    assert {'_alternate_index_tree', '_merge_workspace_delta',
            '_remove_controlled_worktree_owned'} <= set(seen), seen
    assert (root / 'task.txt').read_text() == 'task result\n'
    assert not work.exists()


def test_task_paths_changed_during_the_merge_are_merged_again_without_losing_either_edit(
        repo, monkeypatch):
    root, _, owner = repo
    lines = [f'line {number}\n' for number in range(1, 13)]
    (root / 'shared.txt').write_text(''.join(lines))
    git(root, 'add', 'shared.txt')
    git(root, 'commit', '-m', 'shared')
    work = automatic_workspace(repo, monkeypatch)
    (work / 'shared.txt').write_text(''.join(['task edit\n', *lines[1:]]))
    peer = ''.join([*lines[:-1], 'peer edit\n'])
    original = task_delta_delivery._merge_workspace_delta
    sources = []

    def peer_writes_during_merge(root_arg, source, *args, **kwargs):
        sources.append(source)
        if len(sources) == 1:
            # A tool or person writes without any lease after the unlocked capture.
            (root / 'shared.txt').write_text(peer)
        return original(root_arg, source, *args, **kwargs)

    monkeypatch.setattr(task_delta_delivery, '_merge_workspace_delta', peer_writes_during_merge)
    receipt = delivery.settle_task_workspace(str(root), 'automatic', user_id=owner,
                                             outcome='completed')
    assert receipt['state'] == 'delivered', receipt
    assert (root / 'shared.txt').read_text() == ''.join(['task edit\n', *lines[1:-1], 'peer edit\n'])
    assert len(sources) == 2 and sources[0] != sources[1]
    assert git(root, 'show', f'{receipt["sourceSha"]}:shared.txt') + '\n' == peer
