"""Frozen task checkpoints recover independently of mutable source admission."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import errno
import subprocess
import threading

import pytest

from lib import integration_control as ic
from lib import integration_state_repository as state
from lib.project_mod import delivery
from lib.project_mod.workspace_lease import workspace_file_write_lease, workspace_write_lease
from tests.storage.test_integration_delivery import (
    automatic_settle, automatic_workspace, git, repo as repo,
)

pytestmark = pytest.mark.unit


@contextmanager
def held_source(work, *, metadata=False):
    entered, release = threading.Event(), threading.Event()

    def hold():
        admission = (workspace_write_lease(str(work)) if metadata else
                     workspace_file_write_lease(str(work / 'later.txt')))
        with admission:
            entered.set()
            # The main test owns the contention window. Its finally releases
            # this holder even on pytest timeout; wall-clock expiry would
            # silently remove contention while delivery is still running.
            release.wait()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(hold)
        try:
            assert entered.wait(5)
            yield
        finally:
            release.set()
            future.result(timeout=5)


def queue_completion(root, monkeypatch, owner):
    with monkeypatch.context() as patch:
        patch.setattr(delivery, '_settle_registered', lambda row, receipt: receipt)
        return delivery.settle_task_workspace(str(root), 'automatic', user_id=owner, outcome='completed')


def receipt(root):
    return state.get_workspace(str(root), 'automatic', user_id=1)['origin']['delivery']


def test_frozen_checkpoint_recovers_while_source_is_busy(repo, monkeypatch):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('captured task\n')
    queue_completion(root, monkeypatch, owner)
    ic.checkpoint_workspace(str(root), 'automatic', user_id=owner)
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    delivery._save(row, delivery._receipt(row), checkpointSha=row['checkpoint_sha'])
    (work / 'later.txt').write_text('uncaptured source work\n')
    with held_source(work):
        assert delivery.resume_pending_workspace_deliveries()
        result = receipt(root)
        assert result['state'] == 'delivered', result
        assert (root / 'task.txt').read_text() == 'captured task\n'
        assert not (root / 'later.txt').exists()
    assert (work / 'later.txt').read_text() == 'uncaptured source work\n'


def test_no_delta_probe_compares_commits_without_copying_the_writer_index(repo, monkeypatch):
    root, _, owner = repo
    automatic_workspace(repo, monkeypatch)
    queue_completion(root, monkeypatch, owner)
    ic.checkpoint_workspace(str(root), 'automatic', user_id=owner)
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    from lib.project_mod import git_inspection_execution, task_delta_delivery
    monkeypatch.setattr(
        git_inspection_execution, '_copy_read_index',
        lambda *args: pytest.fail('an immutable tree comparison must not copy the writer index'))
    assert task_delta_delivery.checkpoint_has_no_delta(row)


@pytest.mark.parametrize('legacy_phase', [False, True])
def test_publication_hold_resumes_the_captured_checkpoint_after_authorization(repo, monkeypatch, legacy_phase):
    from lib.project_dispatch import publication

    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('captured task\n')
    allowed = [False]
    monkeypatch.setattr(publication, 'publication_reason',
        lambda row, receipt: '' if allowed[0] else 'project_publication_authority_missing')
    held = automatic_settle(repo)
    assert held['state'] == 'blocked' and held['reason'] == 'project_publication_authority_missing'
    assert held['checkpointSha'] and not (root / 'task.txt').exists()
    if legacy_phase:
        row = state.get_workspace(str(root), 'automatic', user_id=owner)
        delivery._save(row, held, phase='checkpoint')

    allowed[0] = True
    monkeypatch.setattr(ic, 'checkpoint_workspace',
        lambda *args, **kwargs: pytest.fail('Publication must reuse the reviewed checkpoint'))
    (work / 'later.txt').write_text('unreviewed source work\n')
    with held_source(work):
        assert delivery.resume_pending_workspace_deliveries()
        result = receipt(root)
        assert result['state'] == 'delivered', result.get('reason')
        assert result['checkpointSha'] == held['checkpointSha']
    assert (root / 'task.txt').read_text() == 'captured task\n'
    assert not (root / 'later.txt').exists()


@pytest.mark.parametrize('metadata', [False, True])
def test_source_contention_preserves_frozen_conflict_evidence(repo, monkeypatch, metadata):
    root, _, _ = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'base.txt').write_text('task version\n')
    (root / 'base.txt').write_text('canonical version\n')
    first = automatic_settle(repo)
    assert first['state'] == 'blocked'
    assert first['retry']['condition'] == 'conflict_inputs_changed'
    before = git(root, 'rev-parse', 'HEAD')
    with held_source(work, metadata=metadata):
        assert not delivery.resume_pending_workspace_deliveries()
        assert receipt(root) == first
    assert git(root, 'rev-parse', 'HEAD') == before


def test_failed_release_validation_no_longer_prevents_workspace_delivery(repo, monkeypatch):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('task\n')
    queue_completion(root, monkeypatch, owner)
    ic.checkpoint_workspace(str(root), 'automatic', user_id=owner)
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    from lib.project_mod.delivery_retry import retry_evidence
    source = ic._alternate_index_checkpoint(root, ic._rev(root, 'HEAD'), 'automatic')
    retry = retry_evidence('validation_inputs_changed', '0' * 64,
                           row['origin']['sourceSnapshot'], row['checkpoint_sha'], source)
    delivery._save(row, delivery._receipt(row), state='failed', checkpointSha=row['checkpoint_sha'],
                   reason='Gate failed: legacy verification', retry=retry)
    with held_source(work):
        assert delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'delivered', (receipt(root).get('reason'), receipt(root).get('taskOutcome'), receipt(root).get('phase'))
    assert (root / 'task.txt').read_text() == 'task\n'


def intercept_git_spawns(patch, decide):
    """Route every Git spawn through DECIDE: both the deadline and the stall-supervised runner.

    Capture runs its Git under the stall supervisor (no total deadline), so a
    seam on ``subprocess.run`` alone would never see it. DECIDE returns a
    replacement result, raises a spawn error, or returns None to run Git.
    """
    from lib.project_mod import activity_supervised_process as supervised
    run, supervised_run = subprocess.run, supervised.run_activity_supervised

    def deadline_run(argv, *args, **kwargs):
        return decide(argv) or run(argv, *args, **kwargs)

    def stall_supervised_run(argv, *args, **kwargs):
        return decide(argv) or supervised_run(argv, *args, **kwargs)

    patch.setattr(ic.subprocess, 'run', deadline_run)
    patch.setattr(supervised, 'run_activity_supervised', stall_supervised_run)


@pytest.mark.parametrize('error_number', [errno.EAGAIN, errno.EINTR, errno.ENOMEM])
def test_transient_git_spawn_failure_during_capture_resumes_pending_delivery(
        repo, monkeypatch, error_number):
    root, _, _ = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('completed task bytes\n')

    def fail_git_diff_spawn(argv):
        if 'diff' in argv:
            raise OSError(error_number, errno.errorcode[error_number])

    with monkeypatch.context() as patch:
        intercept_git_spawns(patch, fail_git_diff_spawn)
        first = automatic_settle(repo)
    assert first['state'] == 'pending', first['reason']
    assert 'Errno %d' % error_number in first['reason']
    assert not (root / 'task.txt').exists()

    assert automatic_settle(repo)['state'] == 'delivered'
    assert (root / 'task.txt').read_text() == 'completed task bytes\n'


def test_deterministic_git_failure_during_capture_stays_blocked(repo, monkeypatch):
    root, _, _ = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('unpublishable task bytes\n')

    def reject_git_diff(argv):
        if 'diff' in argv:
            return subprocess.CompletedProcess(argv, 128, '', 'invalid object name')

    with monkeypatch.context() as patch:
        intercept_git_spawns(patch, reject_git_diff)
        first = automatic_settle(repo)
    assert first['state'] == 'blocked', first
    assert 'invalid object name' in first['reason']
    with monkeypatch.context() as patch:
        def unexpected_capture(*args, **kwargs):
            raise AssertionError('a blocked deterministic capture must not be recaptured')
        patch.setattr(ic, 'checkpoint_workspace', unexpected_capture)
        assert automatic_settle(repo) == first
    assert not (root / 'task.txt').exists()
    assert receipt(root) == first


def test_legacy_transient_capture_receipt_recovers_through_resume_queue(repo, monkeypatch):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('completed before restart\n')
    queue_completion(root, monkeypatch, owner)
    ic.checkpoint_workspace(str(root), 'automatic', user_id=owner)
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    delivery._save(row, delivery._receipt(row), state='blocked', phase='checkpoint',
                   reason='git diff failed: [Errno 11] Resource temporarily unavailable',
                   checkpointSha=row['checkpoint_sha'])

    assert delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'delivered', receipt(root)
    assert (root / 'task.txt').read_text() == 'completed before restart\n'


def _captured_completion(repo, monkeypatch, text):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text(text)
    queue_completion(root, monkeypatch, owner)
    ic.checkpoint_workspace(str(root), 'automatic', user_id=owner)
    return state.get_workspace(str(root), 'automatic', user_id=owner)


@pytest.mark.parametrize('expiry', ['deadline', 'stall'])
def test_git_deadline_expiry_during_publication_stays_retryable(repo, monkeypatch, expiry):
    """4ea26e1e: an expired Git deadline is the host's state, not the delivery's verdict."""
    from lib.project_mod import task_delta_delivery
    from lib.project_mod.activity_supervised_process import ProcessStalled

    root = repo[0]
    row = _captured_completion(repo, monkeypatch, 'accepted delivery\n')
    delivery._save(row, delivery._receipt(row), checkpointSha=row['checkpoint_sha'])
    merge = task_delta_delivery._merge_workspace_delta

    def expire(*args, **kwargs):
        cause = (subprocess.TimeoutExpired(['git', 'checkout-index'], 30.0) if expiry == 'deadline'
                 else ProcessStalled('no Git thread ran'))
        raise ic.IntegrationError('git checkout-index failed: expired') from cause

    monkeypatch.setattr(task_delta_delivery, '_merge_workspace_delta', expire)
    delivery.resume_pending_workspace_deliveries()
    held = receipt(root)
    assert held['state'] == 'pending', held
    assert held['reason'].startswith('retryable git deadline:')
    assert not (root / 'task.txt').exists()

    monkeypatch.setattr(task_delta_delivery, '_merge_workspace_delta', merge)
    monkeypatch.setattr(ic, 'checkpoint_workspace',
        lambda *args, **kwargs: pytest.fail('a captured checkpoint is never recaptured'))
    assert delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'delivered', receipt(root)
    assert receipt(root)['checkpointSha'] == row['checkpoint_sha']
    assert (root / 'task.txt').read_text() == 'accepted delivery\n'


def test_legacy_git_deadline_hold_recovers_without_recapture(repo, monkeypatch):
    """The receipt 4ea26e1e was held with before expiry was retryable."""
    root = repo[0]
    row = _captured_completion(repo, monkeypatch, 'held before restart\n')
    delivery._save(row, delivery._receipt(row), state='blocked', phase='checkpoint',
                   reason="git checkout-index failed: Command '['/usr/bin/git', 'checkout-index', "
                          "'--all', '--prefix', '/tmp/tofu-generated-merge-c2szixjz/']' "
                          "timed out after 30.0 seconds",
                   checkpointSha=row['checkpoint_sha'])
    monkeypatch.setattr(ic, 'checkpoint_workspace',
        lambda *args, **kwargs: pytest.fail('a captured checkpoint is never recaptured'))

    assert delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'delivered', receipt(root)
    assert (root / 'task.txt').read_text() == 'held before restart\n'


def test_initial_capture_still_requires_source_writer_admission(repo, monkeypatch):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('not captured\n')
    queue_completion(root, monkeypatch, owner)
    with held_source(work):
        delivery.resume_pending_workspace_deliveries()
        assert receipt(root)['reason'] == 'workspace_busy'
        assert state.get_workspace(str(root), 'automatic', user_id=owner)['checkpoint_sha'] == ''
    assert not (root / 'task.txt').exists()


def test_capture_refused_by_a_busy_source_is_retried_once_it_is_free(repo, monkeypatch):
    """Busy admission refused the capture before it ran; it is not a capture failure."""
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('captured once the holder leaves\n')
    queue_completion(root, monkeypatch, owner)
    with held_source(work):
        delivery.resume_pending_workspace_deliveries()
        assert receipt(root)['reason'] == 'workspace_busy'
    delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'delivered', receipt(root)
    assert (root / 'task.txt').read_text() == 'captured once the holder leaves\n'


def test_recovery_restores_conflict_cause_overwritten_by_busy_receipt(repo, monkeypatch):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'base.txt').write_text('task version\n')
    (root / 'base.txt').write_text('canonical version\n')
    first = automatic_settle(repo)
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    delivery._save(row, first, state='blocked', phase='checkpoint', reason='workspace_busy')
    assert delivery.resume_pending_workspace_deliveries()
    restored = receipt(root)
    assert restored['retry'] == first['retry']
    assert restored['phase'] == 'workspace_adoption'
    assert restored['state'] == 'blocked'
    assert restored['reason'].startswith('task_delta_conflict:')
    assert not delivery.resume_pending_workspace_deliveries()


@pytest.mark.parametrize('kind', ['commands', 'agents'])
def test_frozen_checkpoint_still_waits_for_live_descendants(repo, monkeypatch, kind):
    from lib.tasks_pkg import background_commands
    from lib.swarm import integration

    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('task\n')
    queue_completion(root, monkeypatch, owner)
    ic.checkpoint_workspace(str(root), 'automatic', user_id=owner)
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    delivery._save(row, delivery._receipt(row), checkpointSha=row['checkpoint_sha'])
    if kind == 'commands':
        monkeypatch.setattr(background_commands, 'task_has_running_commands', lambda **k: True)
    else:
        monkeypatch.setattr(integration, 'task_has_running_agents', lambda **k: True)
    with held_source(work):
        delivery.resume_pending_workspace_deliveries()
        result = receipt(root)
        assert result['reason'] == ('task_background_commands_running' if kind == 'commands'
                                    else 'task_child_agents_running'), result
    assert not (root / 'task.txt').exists()


def test_a_vanished_project_root_holds_once_and_resumes_when_it_returns(repo, monkeypatch):
    """Each pass used to rewrite the receipt, alternating a busy lease and a Git ENOENT."""
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('captured task\n')
    queue_completion(root, monkeypatch, owner)
    ic.checkpoint_workspace(str(root), 'automatic', user_id=owner)
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    delivery._save(row, delivery._receipt(row), checkpointSha=row['checkpoint_sha'])
    away = root.with_name(root.name + '-away')
    root.rename(away)  # e.g. a /tmp project emptied at container boot
    writes = []
    set_meta = state.set_workspace_meta
    monkeypatch.setattr(state, 'set_workspace_meta',
                        lambda **kwargs: writes.append(kwargs) or set_meta(**kwargs))
    for _ in range(4):
        delivery.resume_pending_workspace_deliveries()
    held = receipt(root)
    assert (held['state'], held['reason']) == ('blocked', 'project_root_missing'), held
    assert len(writes) == 1
    away.rename(root)
    delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'delivered'
    assert (root / 'task.txt').read_text() == 'captured task\n'


def test_publication_pin_retires_the_stale_hold_without_publishing(repo, monkeypatch):
    """A hold written before the pin must not keep reading as a refusal."""
    from lib.project_dispatch import publication

    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('captured task\n')
    allowed = [False]
    monkeypatch.setattr(publication, 'publication_reason',
        lambda row, receipt: '' if allowed[0] else 'project_publication_authority_missing')
    held = automatic_settle(repo)
    assert held['state'] == 'blocked'
    with ic._repo_lock(str(root)):
        assert delivery.release_publication_hold(str(root), 'automatic', user_id=owner) == held
        allowed[0] = True
        released = delivery.release_publication_hold(str(root), 'automatic', user_id=owner)
    assert (released['state'], released['reason']) == ('pending', '')
    assert released['checkpointSha'] == held['checkpointSha']
    # Only the receiver publishes, after rechecking every fence.
    assert not (root / 'task.txt').exists()
    assert delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'delivered'
    assert (root / 'task.txt').read_text() == 'captured task\n'
