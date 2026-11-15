"""The per-project repository lock serializes only what correctness requires (SCALE-REPO-2).

A Worker's terminal intent never waits for another Worker's publication: only a
revocation of a receipt that capture or publication could act on still waits for
repository admission, and two settlements of one checkout serialize on that
checkout's own lock. Nothing a delivered Worker leaves behind grows with the
number of Workers: no destination lock file, no per-Worker ref. Measurement and
decision: "Repository admission at scale" in docs/modules/project_integration.md.

Real Git and the SQLite integration authority, with the local placement layout of
``test_local_task_checkouts.py``; nothing runs in the background.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import threading
import time

import pytest

from lib import integration_control as ic
from lib import integration_state_repository as state
from lib.project_dispatch import publication
from lib.project_dispatch.admission import KEY
from lib.project_mod import delivery
from tests.storage.test_local_task_checkouts import OWNER, create, git, layout as layout  # noqa: F401

pytestmark = pytest.mark.unit

# How long a blocked call is given to prove it does not finish: a settlement
# that does not wait completes in well under a second.
BLOCKED = 2.0


def start(layout, task_id: str, attempt: str) -> Path:
    checkout = create(layout, task_id)
    delivery.bind_task_workspace(str(layout['root']), task_id, user_id=OWNER,
                                 conversation_id='conversation', executor_task_id=attempt)
    return checkout


def settle(layout, task_id: str, attempt: str, outcome: str = 'completed') -> dict:
    return ic.settle_task_workspace(str(layout['root']), task_id, user_id=OWNER,
                                    outcome=outcome, executor_task_id=attempt)


class Held:
    """Hold a lock on another thread until released, as a long publication would."""

    def __init__(self, lock):
        self.lock, self.held, self.release = lock, threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        with self.lock:
            self.held.set()
            self.release.wait(120)

    def __enter__(self):
        self.thread.start()
        assert self.held.wait(30), 'the holder never acquired the lock'
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.thread.join(30)


def in_thread(function):
    result = {}

    def run():
        try:
            result['value'] = function()
        except BaseException as error:  # surfaced by the asserting caller
            result['error'] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


def test_terminal_intent_is_recorded_while_a_publication_holds_repository_admission(layout):
    root = layout['root']
    start(layout, 'worker', 'attempt-1')

    with Held(ic._repo_lock(str(root))):
        thread, result = in_thread(lambda: settle(layout, 'worker', 'attempt-1'))
        thread.join(30)
        assert not thread.is_alive(), 'terminal intent waited for repository admission'

    assert 'error' not in result, result.get('error')
    receipt = state.get_workspace(str(root), 'worker', user_id=OWNER)['origin']['delivery']
    assert (receipt['state'], receipt['taskOutcome'], receipt['executorTaskId']) == (
        'pending', 'completed', 'attempt-1')


def test_revoking_a_receipt_publication_could_act_on_still_waits_for_repository_admission(layout):
    root = layout['root']
    start(layout, 'worker', 'attempt-1')
    assert settle(layout, 'worker', 'attempt-1')['taskOutcome'] == 'completed'

    with Held(ic._repo_lock(str(root))):
        thread, result = in_thread(lambda: settle(layout, 'worker', 'attempt-1', 'failed'))
        thread.join(BLOCKED)
        assert thread.is_alive(), 'a revocation interleaved with a publication holding admission'
    thread.join(30)

    assert 'error' not in result, result.get('error')
    receipt = state.get_workspace(str(root), 'worker', user_id=OWNER)['origin']['delivery']
    assert (receipt['state'], receipt['taskOutcome']) == ('pending', 'failed')


def test_settlements_of_one_checkout_serialize_on_its_destination(layout):
    root = layout['root']
    checkout = start(layout, 'worker', 'attempt-1')

    with Held(ic._destination_lock(checkout)):
        thread, result = in_thread(lambda: settle(layout, 'worker', 'attempt-1'))
        thread.join(BLOCKED)
        assert thread.is_alive(), 'a settlement did not wait for its checkout\'s owner'
    thread.join(30)

    assert 'error' not in result, result.get('error')
    receipt = state.get_workspace(str(root), 'worker', user_id=OWNER)['origin']['delivery']
    assert receipt['state'] == 'pending'


def test_two_settlements_of_one_generation_reach_one_verdict(layout, monkeypatch):
    """Completion and a recovery with the opposite verdict race: whichever runs second
    sees the first one's receipt and decides under repository admission."""
    root = layout['root']
    checkout = start(layout, 'worker', 'attempt-1')
    admitted, real = [], ic._repo_lock

    def counted(*args, **kwargs):
        admitted.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(ic, '_repo_lock', counted)
    with Held(ic._destination_lock(checkout)):
        racing = [in_thread(lambda outcome=outcome: settle(layout, 'worker', 'attempt-1', outcome))
                  for outcome in ('completed', 'failed')]
        time.sleep(0.2)
    for thread, result in racing:
        thread.join(30)
        assert not thread.is_alive() and 'error' not in result, result.get('error')

    receipt = state.get_workspace(str(root), 'worker', user_id=OWNER)['origin']['delivery']
    # A recorded denial is final within its generation, and a completed receipt is revoked.
    assert (receipt['state'], receipt['taskOutcome'], receipt['executorTaskId']) == (
        'pending', 'failed', 'attempt-1')
    assert admitted == [str(root.resolve())], (
        'only the settlement that found an actionable receipt waits for repository admission')


@pytest.mark.parametrize('verdict, receipt_state, outcome', [
    ({'status': 'done', 'finishReason': 'stop'}, 'delivered', 'completed'),
    (None, 'preserved', 'failed'),
])
def test_recovery_settles_the_working_receipt_a_crashed_attempt_left(
        layout, monkeypatch, verdict, receipt_state, outcome):
    """The attempt crashed after its start bound the receipt and before its terminal intent."""
    root = layout['root']
    git(root, 'config', '--local', 'tofu.linearCheckpoint', 'true')
    checkout = start(layout, 'worker', 'attempt-1')
    (checkout / 'work.txt').write_text('work before the crash\n')
    import lib.storage
    client = lib.storage.get_storage_client

    class DurableVerdict:
        def __init__(self, real):
            self.real = real

        def query(self, operation, payload, *args, **kwargs):
            if operation == 'task_results.replay_get':
                assert payload['key'] == 'attempt-1'
                return verdict
            return self.real.query(operation, payload, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.real, name)

    monkeypatch.setattr(lib.storage, 'get_storage_client',
                        lambda *args, **kwargs: DurableVerdict(client(*args, **kwargs)))

    while delivery.resume_pending_workspace_deliveries():
        pass

    receipt = state.get_workspace(str(root), 'worker', user_id=OWNER)['origin']['delivery']
    assert (receipt['state'], receipt['taskOutcome'], receipt['executorTaskId']) == (
        receipt_state, outcome, 'attempt-1')
    published = (root / 'work.txt').exists()
    assert published is (outcome == 'completed')


def test_recovery_leaves_the_working_receipt_of_a_live_attempt_alone(layout, monkeypatch):
    root = layout['root']
    checkout = start(layout, 'worker', 'attempt-1')
    (checkout / 'work.txt').write_text('in progress\n')
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    monkeypatch.setattr(chat_task_runtime, 'get', lambda task_id: (
        {'id': task_id, 'status': 'running'} if task_id == 'attempt-1' else None))

    delivery.resume_pending_workspace_deliveries()

    row = state.get_workspace(str(root), 'worker', user_id=OWNER)
    assert row['origin']['delivery']['state'] == 'working'
    assert row['state'] == 'running' and not row.get('checkpoint_sha')
    assert not (root / 'work.txt').exists()


TASK_REFS = ('refs/tofu/inputs', 'refs/tofu/checkpoints', 'refs/tofu/workspace-wip',
             'refs/tofu/delivery-inputs', 'refs/tofu/delivery-checkpoints')


def reviewed_worker(layout, monkeypatch, index: int) -> Path:
    """A project Worker whose Coordinator review authorizes publication."""
    # Review and publication are validated by their owner (test_project_publication.py).
    monkeypatch.setattr(publication, 'publication_reason', lambda row, receipt: '')
    checkout = start(layout, f'worker-{index}', f'attempt-{index}')
    state.set_workspace_meta(project_root=str(layout['root']), task_id=f'worker-{index}',
                             user_id=OWNER, now=ic._now(),
                             patch={KEY: {'assignmentId': f'assignment-{index}'}})
    return checkout


def deliver(layout, index: int) -> dict:
    settle(layout, f'worker-{index}', f'attempt-{index}')
    while delivery.resume_pending_workspace_deliveries():
        pass
    return state.get_workspace(str(layout['root']), f'worker-{index}', user_id=OWNER)['origin']


def test_a_delivered_worker_leaves_no_lock_file_and_no_ref(layout, monkeypatch):
    root = layout['root']
    git(root, 'config', '--local', 'tofu.linearCheckpoint', 'true')
    locks = Path(layout['data']) / 'integration' / 'locks'
    for index in range(3):
        checkout = reviewed_worker(layout, monkeypatch, index)
        (checkout / f'delivered-{index}.txt').write_text(f'Worker {index}\n')
        origin = deliver(layout, index)
        assert (origin['delivery']['state'], origin['delivery']['publicationMode']) == (
            'delivered', 'committed')
        assert origin['sourceRetirement'] == 'reclaimed'
        assert git(root, 'show', f'HEAD:delivered-{index}.txt') == f'Worker {index}'

    assert git(root, 'for-each-ref', '--format=%(refname)', *TASK_REFS) == ''
    # One lock file per project remains (its repository lock), none per Worker.
    assert len(os.listdir(locks)) == 1


def test_a_delta_outside_the_branch_history_keeps_every_ref(layout, monkeypatch):
    """A repository that takes no automatic commits holds the delta in its working tree only."""
    root = layout['root']
    checkout = reviewed_worker(layout, monkeypatch, 0)
    (checkout / 'delivered.txt').write_text('only in the working tree\n')

    origin = deliver(layout, 0)

    assert (origin['delivery']['state'], origin['delivery']['publicationMode']) == (
        'delivered', 'working_tree')
    assert origin['deliveryCheckpoint']['status'] == 'disabled'
    assert origin['sourceRetirement'] == 'reclaimed'
    kept = git(root, 'for-each-ref', '--format=%(refname)', *TASK_REFS).splitlines()
    assert ic._checkpoint_ref(OWNER, 'worker-0') in kept


@pytest.fixture
def delivered_history(tmp_path):
    """A branch whose tip holds a delivered commit, and the task's refs around it."""
    root = tmp_path / 'repository'
    root.mkdir()
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'config', 'user.email', 'refs@example.invalid')
    git(root, 'config', 'user.name', 'Ref retirement')
    (root / 'base.txt').write_text('base\n')
    git(root, 'add', 'base.txt')
    git(root, 'commit', '-q', '-m', 'base')
    base = git(root, 'rev-parse', 'HEAD')
    checkpoint = git(root, 'commit-tree', '-p', base, '-m', 'checkpoint', f'{base}^{{tree}}')
    (root / 'delivered.txt').write_text('delivered\n')
    git(root, 'add', 'delivered.txt')
    git(root, 'commit', '-q', '-m', 'delivery')
    delivered = git(root, 'rev-parse', 'HEAD')
    refs = {'input': ic._input_ref(OWNER, 'task'), 'checkpoint': ic._checkpoint_ref(OWNER, 'task'),
            'target': f'refs/tofu/delivery-checkpoints/u{OWNER}/{ic._safe_task("task")}'}
    git(root, 'update-ref', refs['input'], base)
    git(root, 'update-ref', refs['checkpoint'], checkpoint)
    git(root, 'update-ref', refs['target'], delivered)
    return {'root': root, 'base': base, 'checkpoint': checkpoint, 'delivered': delivered,
            'refs': refs}


def retire(history, **overrides):
    arguments = {'branch': 'refs/heads/main', 'delivered': history['delivered'],
                 'checkpoint': history['checkpoint'], **overrides}
    return ic.retire_task_refs(history['root'], OWNER, 'task', **arguments)


def task_refs(history) -> list[str]:
    return git(history['root'], 'for-each-ref', '--format=%(refname)', 'refs/tofu').splitlines()


def test_retirement_deletes_the_refs_once_the_branch_holds_the_delivered_commit(delivered_history):
    assert sorted(retire(delivered_history)) == sorted(delivered_history['refs'].values())
    assert task_refs(delivered_history) == []


def test_retirement_keeps_every_ref_when_the_branch_lost_the_delivered_commit(delivered_history):
    git(delivered_history['root'], 'reset', '-q', '--hard', delivered_history['base'])

    assert retire(delivered_history) == []
    assert len(task_refs(delivered_history)) == 3


def test_retirement_keeps_every_ref_while_work_was_saved_outside_the_checkpoint(
        delivered_history):
    root = delivered_history['root']
    git(root, 'update-ref', ic._wip_ref(OWNER, 'task'), delivered_history['checkpoint'])

    assert retire(delivered_history) == []
    assert len(task_refs(delivered_history)) == 4


def test_retirement_keeps_every_ref_when_the_checkpoint_ref_holds_another_state(
        delivered_history):
    assert retire(delivered_history, checkpoint=delivered_history['base']) == []
    assert len(task_refs(delivered_history)) == 3


def test_a_private_input_snapshot_stays_recognizable_to_git_land(delivered_history):
    root = delivered_history['root']
    snapshot = git(root, 'commit-tree', '-p', delivered_history['base'], '-m', 'input snapshot',
                   f'{delivered_history["base"]}^{{tree}}')
    git(root, 'update-ref', delivered_history['refs']['input'], snapshot)

    retired = retire(delivered_history)

    assert delivered_history['refs']['input'] not in retired
    assert task_refs(delivered_history) == [delivered_history['refs']['input']]


def test_a_branch_input_writes_no_input_ref_and_restoration_still_finds_it(layout):
    root = layout['root']
    git(root, 'config', '--local', 'tofu.linearCheckpoint', 'true')
    head = git(root, 'rev-parse', 'HEAD')

    checkout = start(layout, 'worker', 'attempt-1')

    assert git(root, 'for-each-ref', '--format=%(refname)', 'refs/tofu/inputs') == ''
    row = state.get_workspace(str(root), 'worker', user_id=OWNER)
    assert ic._restoration_source(root, row, OWNER, 'worker') == ('input', head, head)
    assert git(checkout, 'rev-parse', 'HEAD') == head


def test_a_captured_input_keeps_its_ref_until_its_delivery_is_committed(layout):
    root = layout['root']
    (root / 'base.txt').write_text('an earlier delivery, uncommitted\n')

    start(layout, 'worker', 'attempt-1')

    snapshot = git(root, 'rev-parse', ic._input_ref(OWNER, 'worker'))
    assert snapshot != git(root, 'rev-parse', 'HEAD')
    assert git(root, 'show', f'{snapshot}:base.txt') == 'an earlier delivery, uncommitted'


def test_a_retired_destination_lock_admits_one_holder_at_a_time(layout, tmp_path):
    """Holders of a destination that does not exist reclaim its lock file on every release."""
    destination = tmp_path / 'never-created'
    holders, peak, failures = [0], [0], []
    guard = threading.Lock()

    def hold_repeatedly():
        try:
            for _ in range(40):
                with ic._destination_lock(destination):
                    with guard:
                        holders[0] += 1
                        peak[0] = max(peak[0], holders[0])
                    time.sleep(0.002)
                    with guard:
                        holders[0] -= 1
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=hold_repeatedly) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)

    assert not failures, failures
    assert peak[0] == 1, f'{peak[0]} holders of one destination at once'
    digest = hashlib.sha256(('destination:' + str(destination.resolve())).encode()).hexdigest()[:20]
    assert not (Path(layout['data']) / 'integration' / 'locks' / f'{digest}.lock').exists()
