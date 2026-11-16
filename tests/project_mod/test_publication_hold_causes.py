"""A publication hold names what holds it: the assignment's verdict, a check that repeats, or a defect.

docs/PROJECT_COORDINATION.md, "A publication hold names its cause". Every
failure to read the assignment's authority used to read as
``project_publication_authority_missing`` and every failed checkpoint diff as
``project_publication_checkpoint_unavailable``. A storage outage, dispatch
contention or a Git timeout then told the Coordinator to act on a hold that
clears by itself, and a Tofu bug looked like missing authority.
"""
import logging
import subprocess

import pytest

from lib import integration_control as ic, integration_state_repository as state
from lib.agent_core.storage_errors import StorageError
from lib.project_dispatch import admission, publication
from lib.project_dispatch.repository import (
    DispatchContention, DispatchGrantRevoked, ProjectDispatchError, PublicationAwaitsReview)
from lib.project_mod import delivery
from tests.project_mod.test_project_publication import FakeRepository, receipt, workspace
from tests.storage.test_integration_delivery import repo as repo_fixture, automatic_settle

repo = repo_fixture  # Temporary Git + offline SQLite authority, never live storage.
pytestmark = pytest.mark.unit

AUTHORITY_MISSING = 'project_publication_authority_missing'
CHECKPOINT_UNAVAILABLE = 'project_publication_checkpoint_unavailable'
CHECK_UNAVAILABLE = 'project_publication_check_unavailable'
BINDING = {'owner': 1, 'projectPath': '/project', 'assignmentId': 'assignment',
           'recipientId': 'worker', 'revision': 1}
CHECKPOINT = 'c' * 40


def _row(receipt_value=None):
    origin = {admission.KEY: dict(BINDING), 'sourceSnapshot': 'b' * 40}
    if receipt_value is not None:
        origin['delivery'] = receipt_value
    return {'task_id': 'task', 'user_id': 1, 'project_root': '/project',
            'checkpoint_sha': CHECKPOINT, 'origin': origin}


class Assignment:
    """The dispatch repository port: an accepted, pinned assignment unless ``failure`` is set."""

    def __init__(self):
        self.failure = None

    def validate_publication(self, binding, task_id):
        if self.failure is not None:
            raise self.failure
        return {'id': 'assignment', 'publication': {'checkpointSha': CHECKPOINT},
                'grant': {'owner': 1, 'revision': 1, 'revoked': False},
                'request': {'projectPath': '/project', 'recipientId': 'worker',
                            'profile': {'mode': 'project', 'writePaths': ['.']}}}


@pytest.fixture
def assignment(monkeypatch):
    port = Assignment()
    monkeypatch.setattr(publication, '_ASSIGNMENT_DECIDED', {})
    monkeypatch.setattr(publication, '_LOGGED_HOLD_CAUSES', {})
    monkeypatch.setattr(admission, 'repository_for', lambda binding: port)
    return port


@pytest.fixture
def git_diff(monkeypatch):
    """The checkpoint diff: no changed paths unless ``failure`` is set."""
    from types import SimpleNamespace
    port = SimpleNamespace(failure=None)

    def run(cwd, args, **kwargs):
        if port.failure is not None:
            raise port.failure
        return SimpleNamespace(stdout='')
    monkeypatch.setattr(ic, '_git', run)
    return port


def _git_failure(cause):
    """What ``integration_control._git`` raises: IntegrationError, chained to its cause."""
    try:
        raise ic.IntegrationError(f'git diff failed: {cause}') from cause
    except ic.IntegrationError as error:
        return error


@pytest.mark.parametrize('failure, reason', [
    pytest.param(StorageError('database_unavailable', 'storage authority restarting', True),
                 CHECK_UNAVAILABLE, id='storage-unavailable'),
    pytest.param(StorageError('database_busy', 'writer busy', True), CHECK_UNAVAILABLE, id='storage-busy'),
    pytest.param(StorageError('database_timeout', 'deadline elapsed'), CHECK_UNAVAILABLE, id='storage-deadline'),
    pytest.param(DispatchContention('Project dispatch CAS ended in conflicts'), CHECK_UNAVAILABLE,
                 id='dispatch-contention'),
    pytest.param(KeyError('grant'), 'project_publication_check_defect: KeyError', id='key-error'),
    pytest.param(TypeError("'NoneType' object is not subscriptable"),
                 'project_publication_check_defect: TypeError', id='type-error'),
    pytest.param(StorageError('database_integrity', 'page checksum mismatch'),
                 'project_publication_check_defect: StorageError database_integrity', id='storage-integrity'),
    pytest.param(StorageError('database_resource_exhausted', 'turn response exceeds its bound'),
                 'project_publication_check_defect: StorageError database_resource_exhausted',
                 id='storage-bound'),
    # The assignment's own verdicts keep the permanent reason.
    pytest.param(DispatchGrantRevoked('Grant revoked (assignment assignment)'), AUTHORITY_MISSING,
                 id='grant-revoked'),
    pytest.param(PublicationAwaitsReview('Project publication requires explicit human authorization'),
                 AUTHORITY_MISSING, id='awaits-review'),
    pytest.param(PermissionError('No current exact publication authority'), AUTHORITY_MISSING,
                 id='authority-refusal'),
    pytest.param(ProjectDispatchError('Publication has no execution association'), AUTHORITY_MISSING,
                 id='dispatch-refusal'),
    pytest.param(StorageError('database_not_found', 'Assignment not found'), AUTHORITY_MISSING,
                 id='assignment-gone'),
    pytest.param(StorageError('database_forbidden', 'Owner boundary mismatch'), AUTHORITY_MISSING,
                 id='owner-boundary'),
])
def test_a_failed_authority_read_holds_with_its_own_cause(assignment, git_diff, failure, reason):
    assignment.failure = failure
    assert publication.publication_reason(_row(), {'state': 'blocked'}) == reason


def test_only_the_assignment_verdicts_wait_for_the_assignment(assignment, git_diff):
    """A memoized hold skips the receiver's per-pass check; a repeating check must not."""
    row = _row()
    for failure, remembered in (
            (StorageError('database_unavailable', 'storage authority restarting', True), False),
            (KeyError('grant'), False),
            (PermissionError('No current exact publication authority'), False),
            (PublicationAwaitsReview('Project publication requires explicit human authorization'), True)):
        publication.forget_assignment_decision()
        assignment.failure = failure
        assert publication.publication_reason(row, {'state': 'blocked'})
        assert publication.awaits_assignment(row) is remembered, failure


@pytest.mark.parametrize('failure, reason', [
    pytest.param(_git_failure(subprocess.TimeoutExpired(['git', 'diff'], 30.0)), CHECK_UNAVAILABLE,
                 id='git-timeout'),
    pytest.param(_git_failure(BlockingIOError(11, 'Resource temporarily unavailable')), CHECK_UNAVAILABLE,
                 id='git-spawn-eagain'),
    pytest.param(ic.IntegrationError("git diff failed: fatal: bad object " + CHECKPOINT),
                 CHECKPOINT_UNAVAILABLE, id='git-missing-object'),
    pytest.param(AttributeError("'NoneType' object has no attribute 'split'"),
                 'project_publication_check_defect: AttributeError', id='defect'),
])
def test_a_failed_checkpoint_diff_holds_with_its_own_cause(assignment, git_diff, failure, reason):
    git_diff.failure = failure
    assert publication.publication_reason(_row(), {'state': 'blocked'}) == reason


def test_a_defect_is_logged_once_at_error_with_its_traceback(assignment, git_diff, caplog):
    assignment.failure = KeyError('grant')
    held = {'state': 'blocked', 'reason': 'project_publication_check_defect: KeyError'}
    with caplog.at_level(logging.WARNING, logger=publication.__name__):
        for _ in range(3):
            assert publication.publication_reason(_row(), held) == held['reason']
    assert len(caplog.records) == 1, [record.getMessage() for record in caplog.records]
    record = caplog.records[0]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None and record.exc_info[0] is KeyError
    assert "KeyError: 'grant'" in record.getMessage()


@pytest.mark.parametrize('reason, status', [
    (CHECK_UNAVAILABLE, 'pending'),
    ('project_publication_check_defect: KeyError', 'blocked'),
    (AUTHORITY_MISSING, 'blocked'),
])
def test_a_hold_that_clears_by_itself_reads_as_delivery_in_progress(reason, status):
    """Coordinator waits settle on a blocked delivery and ask it to act; a repeating check needs no act."""
    row = _row({'state': 'blocked', 'reason': reason, 'checkpointSha': CHECKPOINT})
    assignment = {'id': 'assignment',
                  'execution': {'attemptId': 'attempt', 'turnId': 'turn', 'taskId': 'task'},
                  'request': {'recipientId': 'worker', 'projectPath': '/project'}, 'grant': {'owner': 1}}
    attempt = {'attemptId': 'attempt', 'turnId': 'turn', 'conversationId': 'worker', 'taskId': 'task'}
    view = publication.delivery_view(assignment, attempt, lambda root, task_id, user_id: row)
    assert (view['status'], view['reason']) == (status, reason)


class OutageRepository(FakeRepository):
    """An assignment whose authority read fails while ``failure`` is set."""

    failure = None

    def validate_publication(self, binding, task_id):
        if self.failure is not None:
            self.calls.append(task_id)
            raise self.failure
        return super().validate_publication(binding, task_id)


def test_an_accepted_delivery_held_by_an_outage_publishes_on_the_next_tick_without_review(repo, monkeypatch):
    """The real receiver: no review, accept or publish follows the outage."""
    root, _, _ = repo
    authority = OutageRepository(root)
    monkeypatch.setattr(admission, 'repository_for', lambda binding: authority)
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(root.parent / 'managed'))
    monkeypatch.setattr(ic, '_gate_commands', lambda *a, **k: (True, 'offline gate'))
    work = workspace(repo, authority)
    (work / 'allowed').mkdir()
    (work / 'allowed/result.txt').write_text('reviewable result\n')
    assert automatic_settle(repo)['reason'] == AUTHORITY_MISSING  # Not reviewed yet.
    authority.publish()
    authority.failure = StorageError('database_unavailable', 'storage authority restarting', True)
    delivery.resume_pending_workspace_deliveries()
    assert (receipt(root)['state'], receipt(root)['reason']) == ('blocked', CHECK_UNAVAILABLE)
    view = publication.delivery_view(authority.assignment, authority.attempt, state.get_workspace)
    assert (view['status'], view['reason']) == ('pending', CHECK_UNAVAILABLE)
    assert not (root / 'allowed/result.txt').exists()
    authority.failure = None
    # The receiver's own tick: the last pass returned this row, so the next one is due.
    assert delivery.resume_due_workspace_deliveries()
    assert receipt(root)['state'] == 'delivered'
    assert (root / 'allowed/result.txt').read_text() == 'reviewable result\n'


@pytest.mark.parametrize('revoked_after_accept', [False, True], ids=['accepted', 'revoked-after-accept'])
def test_a_hold_decided_before_the_accept_is_never_reported_as_held_after_it(
        repo, monkeypatch, revoked_after_accept):
    """2026-11-17: three accepts in a row first polled as held, then delivered by themselves.

    The receiver held each completed delivery as awaiting review seconds before
    the Coordinator accepted it. ``review_for_coordinator`` commits the pin, then
    releases that hold in a second write; a poll issued beside the review read
    the pin with the earlier hold, settled as ``held`` and asked the Coordinator
    to act on ``project_publication_authority_missing``. A revocation after the
    accept is a real verdict and still holds.
    """
    from lib.project_dispatch.continuation import worker_phase
    from tests.project_mod.test_project_publication import AssignmentDecidedRepository
    root, _, _ = repo
    authority = AssignmentDecidedRepository(root)
    monkeypatch.setattr(admission, 'repository_for', lambda binding: authority)
    monkeypatch.setattr(publication, '_ASSIGNMENT_DECIDED', {})
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(root.parent / 'managed'))
    monkeypatch.setattr(ic, '_gate_commands', lambda *a, **k: (True, 'offline gate'))
    work = workspace(repo, authority)
    (work / 'allowed').mkdir()
    (work / 'allowed/result.txt').write_text('reviewable result\n')
    assert automatic_settle(repo)['reason'] == AUTHORITY_MISSING  # Completed, not reviewed yet.
    delivery.resume_pending_workspace_deliveries()
    assert publication.awaits_assignment(state.get_workspace(str(root), 'automatic', user_id=1))

    # The review's first write: the accept and its pin. The hold release has not run.
    authority.publish()
    if revoked_after_accept:
        authority.assignment['grant']['revoked'] = True
    assert receipt(root)['reason'] == AUTHORITY_MISSING
    view = publication.delivery_view(authority.assignment, authority.attempt, state.get_workspace)
    summary = {'status': 'completed', 'review': authority.assignment['review'], 'delivery': view}
    if revoked_after_accept:
        assert (view['status'], view['reason']) == ('blocked', AUTHORITY_MISSING)
        assert worker_phase(summary) == 'held'
        return
    assert view['status'] == 'pending'
    assert worker_phase(summary) == 'delivering'
    # The receiver, not a release, delivers it: nothing else acts on the assignment.
    delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'delivered'
    assert (root / 'allowed/result.txt').read_text() == 'reviewable result\n'
