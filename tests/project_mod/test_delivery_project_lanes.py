"""The integration receiver gives each project its own lane.

One project's slow or doomed capture never delays another project's
publication. Rows of one project prepare concurrently; the repository lock
still orders each row's delta-sized publication, and a backlog reclamation
runs only while no delivery row waits or runs.
"""
import threading
import time
from pathlib import Path

import pytest

from lib import integration_control as ic
from lib import integration_state_repository as state
from lib.project_mod import delivery
from tests.storage.test_integration_delivery import git

pytestmark = pytest.mark.unit

FRESH_SCHEDULE = (('_RECOVERY_CURSOR', 0), ('_RECOVERY_RETAINED', False),
                  ('_ACTION_OUTSTANDING', False), ('_SCAN_REQUESTED', False),
                  ('_RECOVERY_REQUESTED', False))
PENDING = {'state': 'pending', 'phase': 'checkpoint', 'taskOutcome': 'completed'}


@pytest.fixture
def fresh_schedule(monkeypatch):
    for name, value in FRESH_SCHEDULE:
        monkeypatch.setattr(delivery, name, value)


@pytest.fixture
def projects(tmp_path, monkeypatch, fresh_schedule):
    from lib.storage import StorageSupervisor
    from lib.storage.runtime import StorageRuntime
    from lib.storage.service import install_runtime_for_test
    runtime = StorageRuntime(supervisor=StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60), auto_restart=False)
    install_runtime_for_test(runtime)
    runtime.start()
    monkeypatch.setattr(ic, '_push', lambda *a, **k: None)
    monkeypatch.setattr(delivery, '_canonical_in_use', lambda *a: False)
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', '')
    monkeypatch.setenv('TOFU_INTEGRATION_AUTORUN', '1')
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'managed'))
    roots = {}
    for name in ('alpha', 'beta'):
        root = tmp_path / name
        root.mkdir()
        git(root, 'init', '-b', 'main')
        git(root, 'config', 'user.name', 'Lane test')
        git(root, 'config', 'user.email', 'lane@example.invalid')
        (root / 'base.txt').write_text('base\n')
        git(root, 'add', 'base.txt')
        git(root, 'commit', '-m', 'base')
        roots[name] = root
    try:
        yield roots
    finally:
        try:
            assert ic.stop_worker(timeout=60)
        finally:
            install_runtime_for_test(None)


def finished_task(root, task_id):
    """A completed Worker whose capture and publication are the receiver's debt."""
    created = ic.create_task_workspace(str(root), task_id, user_id=1, conversation_id=f'conv-{task_id}')
    delivery.bind_task_workspace(str(root), task_id, user_id=1, conversation_id=f'conv-{task_id}')
    (Path(created['workspacePath']) / f'{task_id}.txt').write_text(f'{task_id} result\n')
    delivery.record_task_workspace_intent(str(root), task_id, user_id=1, outcome='completed')


def receipt(root, task_id):
    return state.get_workspace(str(root), task_id, user_id=1)['origin']['delivery']


def wait_for(predicate, seconds, interval=0.05):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(interval)
    return True


def test_slow_capture_in_one_project_does_not_delay_another_projects_ready_row(
        projects, monkeypatch):
    """2026-11-06: one project's 2.26M-file capture held every project's publication."""
    alpha, beta = projects['alpha'], projects['beta']
    finished_task(alpha, 'alpha-task')  # The lower row ID: a serial pass reaches it first.
    finished_task(beta, 'beta-task')
    capturing, release = threading.Event(), threading.Event()
    capture = ic.checkpoint_workspace

    def slow_alpha_capture(project_path, task_id, **kwargs):
        if task_id == 'alpha-task':
            capturing.set()
            assert release.wait(300), 'the test never released the slow capture'
        return capture(project_path, task_id, **kwargs)

    monkeypatch.setattr(ic, 'checkpoint_workspace', slow_alpha_capture)
    assert ic.ensure_worker_started()
    try:
        assert capturing.wait(120), (
            f'the receiver never started the slow capture: {receipt(alpha, "alpha-task")}')
        # Storage polls stay sparse: the receiver shares the same authority.
        published = wait_for(lambda: receipt(beta, 'beta-task')['state'] == 'delivered',
                             120, interval=0.5)
        assert published, ("project beta's ready row waited behind project alpha's capture: "
                           f"{receipt(beta, 'beta-task')}")
        assert (beta / 'beta-task.txt').read_text() == 'beta-task result\n'
        # Still capturing: alpha published nothing (its receipt may still name
        # a busy admission that an earlier pass hit before this capture began).
        assert not release.is_set() and receipt(alpha, 'alpha-task')['state'] != 'delivered'
        assert not (alpha / 'alpha-task.txt').exists()
    finally:
        release.set()
    assert wait_for(lambda: receipt(alpha, 'alpha-task')['state'] == 'delivered', 120, interval=0.5)
    assert (alpha / 'alpha-task.txt').read_text() == 'alpha-task result\n'


def serve_pages(monkeypatch, rows, size=64):
    """Serve the action page from ``rows``; lane scheduling needs no storage."""
    def page(after_id, _limit, *, retained=False):
        selected = [row for row in rows if row['id'] > after_id]
        more = len(selected) > size
        selected = selected[:size]
        return {'rows': [dict(row) for row in selected], 'hasMore': more,
                'nextAfterId': selected[-1]['id'] if more else 0}
    monkeypatch.setattr(state, 'delivery_action_page', page)


def pending_rows(*tasks):
    return [{'id': index, 'user_id': 1, 'project_root': f'/fixture/{project}', 'task_id': task,
             'state': 'running', 'workspace_path': f'/fixture/work/{task}',
             'origin': {'delivery': dict(PENDING)}}
            for index, (project, task) in enumerate(tasks, start=1)]


def test_one_projects_rows_prepare_concurrently_beside_other_projects(
        fresh_schedule, monkeypatch):
    serve_pages(monkeypatch, pending_rows(('alpha', 'alpha-1'), ('alpha', 'alpha-2'),
                                          ('beta', 'beta-1')))
    started = {task: threading.Event() for task in ('alpha-1', 'alpha-2', 'beta-1')}
    release_first = threading.Event()
    order = []
    lock = threading.Lock()

    def settle(row, current):
        task_id = row['task_id']
        with lock:
            order.append(task_id)
        started[task_id].set()
        if task_id == 'alpha-1':
            assert release_first.wait(60)
        return {**current, 'state': 'preserved'}

    monkeypatch.setattr(delivery, '_settle_registered', settle)
    delivery.request_workspace_delivery_scan()
    assert delivery.resume_due_workspace_deliveries(lanes=True) is False, \
        'the receiver loop dispatches the page and never waits for row work'
    try:
        assert started['alpha-1'].wait(30)
        assert started['beta-1'].wait(30), "beta's row waited for alpha's busy lane"
        assert started['alpha-2'].wait(30), "alpha's second row waited for its first"
    finally:
        release_first.set()
    assert delivery.stop_project_lanes(timeout=30)
    assert sorted(order) == ['alpha-1', 'alpha-2', 'beta-1']


def test_a_busy_lane_keeps_rows_found_by_later_passes(fresh_schedule, monkeypatch):
    """A lane still busy when the pass reaches its next page queues, never drops, the rows."""
    serve_pages(monkeypatch, pending_rows(('alpha', 'alpha-1'), ('alpha', 'alpha-2')), size=1)
    release_first, first_started, both = threading.Event(), threading.Event(), threading.Event()
    settled, lock = [], threading.Lock()

    def settle(row, current):
        with lock:
            settled.append(row['task_id'])
            count = len(settled)
        if count == 1:
            first_started.set()
            assert release_first.wait(60)
        if count == 2:
            both.set()
        return {**current, 'state': 'preserved'}

    monkeypatch.setattr(delivery, '_settle_registered', settle)
    delivery.request_workspace_delivery_scan()
    try:
        assert delivery.resume_due_workspace_deliveries(lanes=True) is True
        assert first_started.wait(30)
        # The pass reaches the next page while the lane is still busy with the first row.
        assert delivery.resume_due_workspace_deliveries(lanes=True) is False
    finally:
        release_first.set()
    assert both.wait(30)
    assert delivery.stop_project_lanes(timeout=30)
    assert sorted(settled) == ['alpha-1', 'alpha-2']


def test_a_row_in_flight_is_not_queued_again(fresh_schedule, monkeypatch):
    serve_pages(monkeypatch, pending_rows(('alpha', 'alpha-1')))
    release, started = threading.Event(), threading.Event()
    settled = []

    def settle(row, current):
        settled.append(row['task_id'])
        started.set()
        assert release.wait(60)
        return current

    monkeypatch.setattr(delivery, '_settle_registered', settle)
    delivery.request_workspace_delivery_scan()
    delivery.resume_due_workspace_deliveries(lanes=True)
    assert started.wait(30)
    # A later pass finds the same row still due while it runs.
    delivery.request_workspace_delivery_scan()
    delivery.resume_due_workspace_deliveries(lanes=True)
    release.set()
    assert delivery.stop_project_lanes(timeout=30)
    assert settled == ['alpha-1'], 'its own receipt write makes the next pass read it afresh'


def test_a_source_retained_under_an_earlier_rule_waits_for_the_lanes_deliveries(
        fresh_schedule, monkeypatch):
    """Reconsidering one walks and removes a whole checkout, so deliveries go first."""
    from lib.project_mod.task_delta_delivery import RECLAMATION_RULE
    origin = {'executionMode': 'isolated-task', 'sourceRetirement': 'retained',
              'deliveryCheckpoint': {'status': 'committed'},
              'delivery': {'state': 'delivered', 'taskOutcome': 'completed',
                           'publicationMode': 'working_tree'}}

    def retained(row_id, task, **extra):
        return {'id': row_id, 'user_id': 1, 'project_root': '/fixture/alpha', 'task_id': task,
                'state': 'running', 'managed': True, 'workspace_path': f'/fixture/work/{task}',
                'origin': {**origin, **extra}}

    pending = {**pending_rows(('alpha', 'alpha-delivery'))[0], 'id': 2}
    serve_pages(monkeypatch, [retained(1, 'kept-earlier'), pending,
                              retained(3, 'kept-now', sourceRetirementRule=RECLAMATION_RULE)])
    order, finished, lock = [], threading.Event(), threading.Lock()

    def settle(row, current):
        with lock:
            order.append(('deliver', row['task_id']))
        return {**current, 'state': 'preserved'}

    def reclaim(row, *, reconsider=False):
        with lock:
            order.append(('reclaim', row['task_id'], reconsider))
        if reconsider:
            finished.set()

    monkeypatch.setattr(delivery, '_settle_registered', settle)
    monkeypatch.setattr(delivery, '_reclaim_completed_source', reclaim)
    delivery.request_workspace_delivery_scan(recovery=True)
    delivery.resume_due_workspace_deliveries(lanes=True)
    assert finished.wait(30)
    assert delivery.stop_project_lanes(timeout=30)
    assert order == [('deliver', 'alpha-delivery'), ('reclaim', 'kept-now', False),
                     ('reclaim', 'kept-earlier', True)]


def test_stopping_lanes_joins_running_rows(fresh_schedule, monkeypatch):
    serve_pages(monkeypatch, pending_rows(('alpha', 'alpha-1'), ('alpha', 'alpha-2')))
    release, entered = threading.Event(), threading.Semaphore(0)
    settled, lock = [], threading.Lock()

    def settle(row, current):
        with lock:
            settled.append(row['task_id'])
        entered.release()
        assert release.wait(60)
        return {**current, 'state': 'preserved'}

    monkeypatch.setattr(delivery, '_settle_registered', settle)
    delivery.request_workspace_delivery_scan()
    delivery.resume_due_workspace_deliveries(lanes=True)
    assert entered.acquire(timeout=30) and entered.acquire(timeout=30)
    assert delivery.stop_project_lanes(timeout=0.2) is False, 'a lane inside row work is still running'
    release.set()
    assert delivery.stop_project_lanes(timeout=30) is True
    assert sorted(settled) == ['alpha-1', 'alpha-2'], \
        'running rows keep their durable receipts and are joined, not dropped'
