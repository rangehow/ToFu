"""Task checkouts come from prepared spares, and retired checkouts become spares.

Owner decision 2026-11-12: a task's workspace must be ready at once. These
specifications use real Git in a canonical repository under tmp_path with the
local placement of ``test_local_task_checkouts.py``; spare preparation runs
only where a test starts it, never on a background thread.
"""
from __future__ import annotations

import fcntl
import json
from pathlib import Path

import pytest

from lib import integration_control as ic
from lib import integration_state_repository as state
from lib import observability
from lib.project_mod import delivery, task_spares
from lib.project_mod.lanes import move_checkout
from lib.tasks_pkg import workspace_delivery
from serverctl_pkg import local_placement
from tests.storage.test_local_task_checkouts import (
    OWNER, create, git, layout as layout, locked, worktrees)

pytestmark = pytest.mark.unit


@pytest.fixture
def started(monkeypatch):
    """Background preparations ``ensure`` started, by thread name; run on demand."""
    requests = []
    monkeypatch.setattr(task_spares, '_start', lambda target, name: requests.append((name, target)))
    monkeypatch.setattr(task_spares, '_PREPARING', set())
    return requests


def commit(root: Path, name: str, text: str) -> str:
    (root / name).write_text(text)
    git(root, 'add', name)
    git(root, 'commit', '-qm', f'commit {name}')
    return git(root, 'rev-parse', 'HEAD')


def spares(root: Path) -> list[Path]:
    return task_spares._spares(task_spares.pool(root))


def no_whole_tree_checkout(monkeypatch):
    monkeypatch.setattr(ic, '_materialize_worktree', lambda *args, **kwargs: pytest.fail(
        'a task with a spare checked out the whole tree'))


def test_a_task_workspace_takes_a_spare_and_writes_only_what_differs(layout, started, monkeypatch):
    root = layout['root']
    assert task_spares.prepare(root) == 'ready'
    [spare] = spares(root)
    assert locked(root, spare)
    assert git(spare, 'status', '--porcelain', '--ignored') == ''
    head = commit(root, 'later.txt', 'committed after the spare\n')
    no_whole_tree_checkout(monkeypatch)

    checkout = create(layout)

    assert not spare.exists() and str(spare) not in worktrees(root)
    assert checkout.parent.parent == task_spares.pool(root)
    assert git(checkout, 'rev-parse', 'HEAD') == head
    assert (checkout / 'later.txt').read_text() == 'committed after the spare\n'
    assert git(checkout, 'status', '--porcelain') == ''
    assert locked(root, checkout)
    row = state.get_workspace(str(root), 'worker', user_id=OWNER)
    assert row['workspace_path'] == str(checkout) and row['managed']
    # The next task's spare is prepared in the background, and only once.
    assert [name for name, _target in started] == [
        f'tofu-task-spare-{task_spares.pool(root).name}']
    assert task_spares.ensure(root) is False


def test_a_repository_that_commits_deliveries_starts_tasks_at_its_branch_without_a_capture(
        layout, started, monkeypatch):
    root = layout['root']
    git(root, 'config', '--local', 'tofu.linearCheckpoint', 'true')
    head = git(root, 'rev-parse', 'HEAD')
    (root / 'base.txt').write_text('another writer, uncommitted\n')
    (root / 'scratch.txt').write_text('another writer, untracked\n')
    monkeypatch.setattr(ic, '_task_input_checkpoint', lambda *args, **kwargs: pytest.fail(
        'a repository that commits its deliveries captured the working tree'))

    checkout = create(layout)

    assert git(checkout, 'rev-parse', 'HEAD') == head
    assert (checkout / 'base.txt').read_text() == 'base\n'
    assert not (checkout / 'scratch.txt').exists()
    origin = state.get_workspace(str(root), 'worker', user_id=OWNER)['origin']
    assert (origin['inputBaseSha'], origin['inputHeadSha'], origin['sourceSnapshot'],
            origin['sourceHead']) == (head, head, head, head)
    # A branch input needs no ref of its own: its branch keeps it reachable.
    assert not git(root, 'rev-parse', '--verify', '--quiet', ic._input_ref(OWNER, 'worker'),
                   check=False)


def test_a_repository_publishing_into_its_working_tree_still_gives_tasks_that_tree(
        layout, started):
    root = layout['root']
    assert task_spares.prepare(root) == 'ready'
    (root / 'base.txt').write_text('an earlier delivery, uncommitted\n')

    checkout = create(layout)

    assert (checkout / 'base.txt').read_text() == 'an earlier delivery, uncommitted\n'
    snapshot = git(root, 'rev-parse', ic._input_ref(OWNER, 'worker'))
    assert git(checkout, 'rev-parse', 'HEAD') == snapshot != git(root, 'rev-parse', 'HEAD')


def test_task_startup_takes_no_repository_admission_and_lists_no_worktrees(
        layout, started, monkeypatch):
    root = layout['root']
    git(root, 'config', '--local', 'tofu.workspaceMode', 'isolated')
    git(root, 'config', '--local', 'tofu.linearCheckpoint', 'true')
    assert task_spares.prepare(root) == 'ready'
    monkeypatch.setattr('lib.agent_core.events.emit_phase', lambda *args, **kwargs: None)
    monkeypatch.setattr(ic, '_repo_lock', lambda *args, **kwargs: pytest.fail(
        'task startup waited for repository admission'))
    commands, run = [], ic._git

    def recorded(cwd, args, **kwargs):
        commands.append(list(args))
        return run(cwd, args, **kwargs)

    monkeypatch.setattr(ic, '_git', recorded)
    anchors = []
    monkeypatch.setattr(observability.logger, 'info',
                        lambda fmt, *values: anchors.append(json.loads(values[0]))
                        if '[TaskStartupTiming]' in str(fmt) else None)
    task = {'id': 'attempt-1', 'convId': 'worker-conversation', '_userId': OWNER,
            '_attemptId': 'attempt-1', '_turnId': 'turn-1', 'status': 'running',
            '_workspaceWorkId': 'assignment-work', 'config': {'projectPath': str(root)}}
    try:
        workspace_delivery.bind_worker_workspace(task)

        checkout = Path(task['config']['projectPath'])
        assert checkout.parent.parent == task_spares.pool(root)
        receipt = state.get_workspace(str(root), 'assignment-work',
                                      user_id=OWNER)['origin']['delivery']
        assert (receipt['state'], receipt['executorTaskId']) == ('working', 'attempt-1')
        assert not [args for args in commands if args[:2] == ['worktree', 'list']]
        completed = [anchor for anchor in anchors if anchor['boundary'] == 'complete']
        assert {'workspace_lookup', 'workspace_mode', 'workspace_input', 'workspace_spare',
                'workspace_registration', 'workspace_link', 'workspace_bind',
                'workspace_root_binding', 'workspace_preflight'} <= {
            anchor['stage'] for anchor in completed}
        assert 'workspace_snapshot' not in {anchor['stage'] for anchor in completed}
        # Every span of one start names that attempt, never the work id it creates.
        assert {(anchor['task_id'], anchor['attempt_id']) for anchor in completed} == {
            ('attempt-1', 'attempt-1')}
    finally:
        from lib import project_mod
        project_mod.config.clear_conv_state('attempt-1')


def test_a_delivered_checkout_becomes_a_pristine_spare_the_next_task_takes(
        layout, started, monkeypatch):
    root = layout['root']
    checkout = create(layout)
    delivery.bind_task_workspace(str(root), 'worker', user_id=OWNER, conversation_id='c')
    (checkout / 'base.txt').write_text('delivered edit\n')
    (checkout / 'new.txt').write_text('delivered file\n')
    (checkout / '.tofu' / 'file-history').mkdir(parents=True)
    (checkout / '.tofu' / 'file-history' / 'snapshots.jsonl').write_text('{"id": "undo"}\n')
    from lib.project_mod.run_command import _get_cmd_env
    scratch = Path(_get_cmd_env(str(checkout), project_root=str(checkout))['TMPDIR'])
    assert scratch.parent == checkout.parent and scratch.is_dir()
    (scratch / 'build.o').write_text('a worker temporary\n')

    delivery.settle_task_workspace(str(root), 'worker', user_id=OWNER, outcome='completed')
    delivery.resume_pending_workspace_deliveries()

    row = state.get_workspace(str(root), 'worker', user_id=OWNER)
    assert row['origin']['delivery']['state'] == 'delivered'
    assert row['origin']['sourceRetirement'] == 'reclaimed'
    assert (root / 'new.txt').read_text() == 'delivered file\n'
    assert not checkout.exists() and str(checkout) not in worktrees(root)
    assert not scratch.exists(), "a delivered assignment's command scratch goes with its checkout"
    [spare] = spares(root)
    assert locked(root, spare)
    assert git(spare, 'rev-parse', 'HEAD') == git(root, 'rev-parse', 'HEAD')
    assert git(spare, 'status', '--porcelain', '--ignored') == ''

    no_whole_tree_checkout(monkeypatch)
    following = create(layout, 'following')

    assert not spare.exists() and following.is_dir()
    assert (following / 'new.txt').read_text() == 'delivered file\n'
    assert not (following / '.tofu').exists()


def test_command_scratch_exists_only_beside_pooled_task_checkouts(layout, tmp_path):
    checkout = create(layout)
    scratch = Path(task_spares.checkout_scratch(checkout))

    assert scratch == checkout.parent / f'.tmp-{checkout.name}' and scratch.is_dir()
    assert task_spares.checkout_scratch(tmp_path / 'elsewhere') is None
    assert task_spares.checkout_scratch(layout['root']) is None
    task_spares.retire_scratch(checkout)
    assert scratch.is_dir(), 'a live checkout keeps its scratch'


def test_spares_give_back_space_oldest_first_when_the_volume_nears_its_reserve(
        layout, started, monkeypatch):
    root = layout['root']
    retired = [create(layout, task_id) for task_id in ('first', 'second')]
    for checkout in retired:
        assert task_spares.recycle(root, checkout, lifecycle_owned=True)
    older, newer = spares(root)
    remaining = []

    def room_once_one_spare_is_left(path, required_bytes=0, *, required_inodes=0, **_kwargs):
        remaining.append(len(spares(root)))
        return len(spares(root)) <= 1

    monkeypatch.setattr(local_placement, 'has_free_space', room_once_one_spare_is_left)

    assert task_spares.make_room() is True

    assert spares(root) == [newer]
    assert str(older) not in worktrees(root) and str(newer) in worktrees(root)
    # Near the reserve a retired checkout is removed instead of recycled.
    third = create(layout, 'third')
    monkeypatch.setattr(local_placement, 'has_free_space', lambda *args, **kwargs: False)
    assert task_spares.recycle(root, third, lifecycle_owned=True) is False
    assert third.is_dir() and spares(root) == []


def test_spares_an_earlier_placement_left_are_retired_by_the_next_pass(
        layout, started, monkeypatch):
    """2026-11-12: task checkouts left the root overlay for the local temporary
    disk. A spare in the earlier root's pool is never claimed, refreshed or
    evicted again (``make_room`` reclaims the placed root only), so the
    repository's next maintenance pass removes it; spares hold no task's work."""
    root = layout['root']
    assert task_spares.prepare(root) == 'ready'
    [stranded] = spares(root)
    earlier = ic._placed_local_root()
    moved = layout['tmp'] / 'scratch-disk' / 'workspaces'
    monkeypatch.setattr(local_placement, 'choose_local_root', lambda source, purpose, **_kwargs:
                        local_placement.Placement(True, purpose, 'on', 'selected',
                                                  root=str(moved)))
    monkeypatch.setattr(ic, '_PLACED_WORKSPACE_ROOTS', {})
    monkeypatch.setattr(ic, '_local_workspace_roots', lambda: [
        ic._local_workspace_root(moved).resolve(), earlier.resolve()])
    assert ic._placed_local_root() != earlier

    assert task_spares.prepare(root) == 'ready'

    assert not stranded.exists() and str(stranded) not in worktrees(root)
    [spare] = spares(root)
    assert spare.is_relative_to(ic._local_workspace_root(moved))


def test_a_spare_is_claimed_once_and_unfinished_checkouts_are_never_recycled(
        layout, started):
    root = layout['root']
    assert task_spares.prepare(root) == 'ready'
    pool = task_spares.pool(root)
    head = git(root, 'rev-parse', 'HEAD')

    assert task_spares.take(root, pool / 'u7' / 'one', head) is True
    assert task_spares.take(root, pool / 'u7' / 'two', head) is False
    assert not (pool / 'u7' / 'two').exists()
    assert task_spares.take(root, layout['tmp'] / 'elsewhere', head) is False

    checkout = create(layout, 'merging')
    git(checkout, 'checkout', '-q', '-b', 'side')
    (checkout / 'base.txt').write_text('side\n')
    git(checkout, 'commit', '-qam', 'side')
    git(checkout, 'checkout', '-q', '--detach', head)
    (checkout / 'base.txt').write_text('detached\n')
    git(checkout, 'commit', '-qam', 'detached')
    git(checkout, 'merge', 'side', check=False)
    assert task_spares.recycle(root, checkout, lifecycle_owned=True) is False
    assert checkout.is_dir()


def test_an_abandoned_preparation_is_removed_and_a_live_one_is_not_duplicated(layout, started):
    root = layout['root']
    pool = task_spares.pool(root)
    pool.mkdir(parents=True)
    abandoned = pool / f'{task_spares.PREPARING_PREFIX}died'
    ic._materialize_worktree(root, abandoned, git(root, 'rev-parse', 'HEAD'))

    with open(pool / '.preparation.lock', 'a+b') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        assert task_spares.prepare(root) == 'underway'
    assert abandoned.is_dir()

    assert task_spares.prepare(root) == 'ready'

    assert not abandoned.exists() and str(abandoned) not in worktrees(root)
    assert len(spares(root)) == 1
    assert task_spares.prepare(root) == 'present'


def test_spares_follow_head_and_a_claim_prefers_one_already_there(layout, started, monkeypatch):
    root = layout['root']
    assert task_spares.prepare(root) == 'ready'
    head = commit(root, 'moved.txt', 'the branch moved\n')

    # The only spare is stale: a fresh one is created while it stays claimable.
    assert task_spares.prepare(root) == 'ready'
    pair = spares(root)
    assert len(pair) == 2
    assert {git(spare, 'rev-parse', 'HEAD') for spare in pair} == {head}
    assert all(git(spare, 'status', '--porcelain', '--ignored') == '' for spare in pair)
    assert task_spares.prepare(root) == 'present'

    head = commit(root, 'again.txt', 'and again\n')
    assert task_spares.prepare(root) == 'refreshed'
    assert len(spares(root)) == 2
    assert {git(spare, 'rev-parse', 'HEAD') for spare in spares(root)} == {head}

    newer = spares(root)[-1]
    git(newer, 'checkout', '-q', '--detach', 'HEAD~1')
    monkeypatch.setattr(task_spares, '_update', lambda *args, **kwargs: pytest.fail(
        'a spare at the revision was available'))
    assert task_spares.take(root, task_spares.pool(root) / 'u7' / 'claimed', head)
    assert spares(root) == [newer]


def test_a_new_process_warms_every_named_pool_to_its_head(layout, started):
    root = layout['root']
    assert task_spares.prepare(root) == 'ready'
    pool = task_spares.pool(root)
    assert (pool / '.repository').read_text() == str(root)
    head = commit(root, 'moved.txt', 'committed while the server was down\n')
    (pool.parent / 'unnamed-pool').mkdir()

    task_spares._warm()

    assert [name for name, _target in started] == [f'tofu-task-spare-{pool.name}']
    started[0][1]()
    assert {git(spare, 'rev-parse', 'HEAD') for spare in spares(root)} == {head}


def test_restoring_a_retired_checkout_takes_a_spare(layout, started, monkeypatch):
    root = layout['root']
    checkout = create(layout)
    (checkout / 'base.txt').write_text('saved work\n')
    wip = ic.save_workspace_wip(str(root), 'worker', user_id=OWNER)
    assert task_spares.recycle(root, checkout, lifecycle_owned=True)
    assert not checkout.exists()
    no_whole_tree_checkout(monkeypatch)

    receipt = ic.restore_task_workspace(str(root), 'worker', user_id=OWNER)

    assert (receipt['source'], receipt['sourceSha']) == ('wip', wip)
    assert (checkout / 'base.txt').read_text() == 'saved work\n'
    assert git(checkout, 'diff', '--name-only') == 'base.txt'
    assert locked(root, checkout) and spares(root) == []


def test_a_moved_spare_keeps_its_registration(layout, started):
    root = layout['root']
    assert task_spares.prepare(root) == 'ready'
    [spare] = spares(root)
    moved = spare.parent / f'{task_spares.SPARE_PREFIX}00000000000000000000-moved'

    assert move_checkout(spare, moved)

    assert git(moved, 'rev-parse', '--show-toplevel') == str(moved)
    assert str(moved) in worktrees(root) and str(spare) not in worktrees(root)
