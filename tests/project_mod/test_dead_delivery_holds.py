"""A delivery its assignment can never publish closes; one only its lead can move reaches it.

Audit 2026-11-18 (assignment 0a147aa7): of 96 held receipts in the tofu, knowledge,
parallel and router projects, 71 belonged to cancelled assignments and 23 to
results reviewed reject or rework. Each stayed ``blocked``
(project_publication_authority_missing) or ``preserved`` for good: the receiver
decided it again on every recovery pass, views and polls showed it as a hold, and
nothing could ever lift it. The two that needed a lead (a result never reviewed,
an accept recorded without checkpoint_sha) belonged to finished requests, which
no continuation brief listed.
"""
from copy import deepcopy

import pytest

from lib import integration_state_repository as state
from lib.project_dispatch import admission, publication
from lib.project_dispatch.repository import (
    DispatchGrantRevoked, PublicationAwaitsReview, PublicationClosed)
from lib.project_mod import delivery
from tests.project_mod.test_project_publication import (
    FakeRepository, authority as authority, receipt, repo as repo, workspace)
from tests.storage.test_integration_delivery import automatic_settle

pytestmark = pytest.mark.unit


class FinalVerdictRepository(FakeRepository):
    """Refuses as the real repository does, final verdicts included."""
    def validate_publication(self, binding, task_id):
        self.calls.append(task_id)
        row = self.assignment
        if row['grant']['revoked']:
            raise PublicationClosed('cancelled', row['id'])
        decision = (row['review'] or {}).get('decision')
        if decision in ('reject', 'rework'):
            raise PublicationClosed(decision + 'ed', row['id'])
        if not row['publication'] or not row['review']:
            raise PublicationAwaitsReview('Project publication requires explicit human authorization')
        return deepcopy(row)


@pytest.fixture
def final(repo, authority, monkeypatch):
    repository = FinalVerdictRepository(repo[0])
    monkeypatch.setattr(admission, 'repository_for', lambda binding: repository)
    publication.forget_assignment_decision()
    return repository


def _held(repo, final):
    work = workspace(repo, final)
    (work / 'allowed').mkdir()
    (work / 'allowed/result.txt').write_text('reviewable result\n')
    held = automatic_settle(repo)
    assert held['state'] == 'blocked' and held['reason'] == 'project_publication_authority_missing'
    return held


def _action_rows(root):
    return [row['task_id'] for row in state.delivery_action_page(0, 64)['rows']
            if row['project_root'] == str(root)]


@pytest.mark.parametrize('verdict', ['cancelled', 'rejected', 'reworked'])
def test_a_hold_whose_assignment_became_final_closes_and_leaves_the_receiver(repo, final, verdict):
    root = repo[0]
    _held(repo, final)
    delivery.resume_pending_workspace_deliveries()  # decided at rest and remembered
    if verdict == 'cancelled':
        final.assignment['grant'].update(revoked=True, revision=2)
    else:
        final.assignment['review'] = {'decision': verdict[:-2], 'resultRevision': 7}
    # The cancel or review commit releases the remembered hold (service._close_final_hold).
    closed = delivery.release_publication_hold(str(root), 'automatic', user_id=1)
    assert closed['state'] == 'closed', closed
    assert closed['reason'].startswith(f'project_publication_closed: assignment {verdict}')
    assert closed['checkpointSha'] and not (root / 'allowed/result.txt').exists()
    calls = len(final.calls)
    assert 'automatic' not in _action_rows(root)
    delivery.resume_pending_workspace_deliveries(retained=True)
    assert len(final.calls) == calls and receipt(root)['state'] == 'closed'


def test_a_hold_written_before_closing_existed_closes_on_the_next_recovery_pass(repo, final):
    root = repo[0]
    _held(repo, final)
    final.assignment['grant'].update(revoked=True, revision=2)
    delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'blocked'  # remembered: only the assignment can change it
    delivery.resume_pending_workspace_deliveries(retained=True)
    assert receipt(root)['state'] == 'closed'


def test_an_accept_without_a_pin_stays_held_for_its_lead(repo, final):
    root = repo[0]
    _held(repo, final)
    final.accept()
    assert delivery.release_publication_hold(str(root), 'automatic', user_id=1)['state'] == 'blocked'
    delivery.resume_pending_workspace_deliveries(retained=True)
    assert receipt(root)['reason'] == 'project_publication_authority_missing'


@pytest.mark.parametrize('revoked', [True, False])
def test_an_incomplete_task_of_a_cancelled_assignment_closes_instead_of_preserving(repo, final, revoked):
    root = repo[0]
    work = workspace(repo, final)
    (work / 'allowed').mkdir()
    (work / 'allowed/partial.txt').write_text('partial\n')
    final.assignment['grant']['revoked'] = revoked
    settled = automatic_settle(repo, outcome='cancelled')
    assert settled['state'] == ('closed' if revoked else 'preserved'), settled
    if not revoked:
        # A later cancel closes the preserved checkpoint the moment it commits.
        final.assignment['grant'].update(revoked=True, revision=2)
        assert delivery.release_publication_hold(str(root), 'automatic', user_id=1)['state'] == 'closed'


def test_views_name_a_final_assignments_legacy_hold_closed(repo, final):
    root = repo[0]
    held = _held(repo, final)
    view = publication.delivery_view(final.assignment, final.attempt, state.get_workspace)
    assert view['status'] == 'blocked'
    final.assignment['grant'].update(revoked=True, revision=2)
    view = publication.delivery_view(final.assignment, final.attempt, state.get_workspace)
    assert view['status'] == 'closed' and 'assignment cancelled' in view['reason']
    assert view['checkpointSha'] == held['checkpointSha'] and 'conflict' not in view
    assert receipt(root)['state'] == 'blocked'  # a view never writes


def test_a_landed_checkpoint_settles_delivered_even_when_its_assignment_is_final(repo, final, monkeypatch):
    root = repo[0]
    _held(repo, final)
    final.assignment['grant'].update(revoked=True, revision=2)
    from lib.project_mod import task_delta_delivery
    monkeypatch.setattr(task_delta_delivery, 'checkpoint_contained', lambda row, tip='': True)
    assert delivery.release_publication_hold(str(root), 'automatic', user_id=1)['state'] != 'closed'


# The real repository raises the final verdicts.

from tests.project_mod.test_coordinator_continuation import run as run  # noqa: E402
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime as base_runtime, context as context  # noqa: E402,F401
from tests.project_mod.test_coordinator_dependency_links import completed  # noqa: E402


def _released(r):
    released = []
    r.service.publication_wake = lambda: None
    r.service.publication_release = lambda project, task_id, *, user_id: released.append(task_id)
    return released


@pytest.mark.parametrize('decision', ['reject', 'rework'])
def test_a_reject_or_rework_review_closes_publication_and_releases_the_hold(run, decision):
    r = run
    worker, _pin = completed(r)
    released = _released(r)
    reviewed = r.call('review_project_work', assignment_id=worker['id'], result_revision=1,
                      decision=decision, note='Not this result.')
    assert 'error' not in reviewed, reviewed
    task_id = worker['execution']['taskId']
    with pytest.raises(PublicationClosed) as closed:
        r.c.repo.validate_publication(r.c.repo.admission(worker['id']), task_id)
    assert closed.value.cause == decision + 'ed'
    assert released == [task_id]


def test_cancelling_an_accepted_published_worker_closes_its_publication(run):
    r = run
    worker, pin = completed(r)
    released = _released(r)
    assert 'error' not in r.call('review_project_work', assignment_id=worker['id'], result_revision=1,
                                 decision='accept', note='Verified', checkpoint_sha=pin['checkpointSha'])
    binding, task_id = r.c.repo.admission(worker['id']), worker['execution']['taskId']
    assert r.call('cancel_project_work', assignment_id=worker['id'])['revoked']
    with pytest.raises(PublicationClosed) as closed:
        r.c.repo.validate_publication(binding, task_id)
    assert closed.value.cause == 'cancelled'
    assert released == [task_id, task_id]  # the publish, then the cancel


def test_an_accept_without_checkpoint_still_awaits_its_lead(run):
    r = run
    worker, _pin = completed(r)
    _released(r)
    assert 'error' not in r.call('review_project_work', assignment_id=worker['id'], result_revision=1,
                                 decision='accept', note='Verified')
    with pytest.raises(PublicationAwaitsReview):
        r.c.repo.validate_publication(r.c.repo.admission(worker['id']), worker['execution']['taskId'])
    assert not issubclass(PublicationClosed, (DispatchGrantRevoked, PublicationAwaitsReview))


def test_an_earlier_requests_result_only_its_lead_can_move_reaches_the_continuation(run):
    """A result never reviewed, or accepted without checkpoint_sha, in a request that
    already finished reaches the binding's next continuation brief."""
    from lib.conversations.coordinator_continuation import continuation_context
    from tests.project_mod.test_coordinator_continuation import controls
    from tests.project_mod.test_coordinator_resumed_publication import (
        _continuation_with_another_requests_worker)
    r = run
    other = _continuation_with_another_requests_worker(r)
    wake = r.c.repo.read(controls(r)[-1]['id'])
    assert wake['request']['delegation']['attemptId'] != other['request']['delegation']['attemptId']

    assert other['id'] + ': result to review' in continuation_context(r.c.repo, wake)

    _released(r)
    assert 'error' not in r.call('review_project_work', assignment_id=other['id'], result_revision=1,
                                 decision='accept', note='Verified')
    # What the receiver records for it: no pin, so the hold stays.
    r.workspaces[other['execution']['taskId']]['origin']['delivery'].update(
        state='blocked', reason='project_publication_authority_missing', taskOutcome='completed')
    content = continuation_context(r.c.repo, wake)
    assert other['id'] + ': accepted result, no publication requested' in content
    assert 'inspect get_project_assignment' in content
    assert 'repeat review_project_work' not in content

    assert 'error' not in r.call('review_project_work', assignment_id=other['id'], result_revision=1,
                                 decision='accept', note='Verified', checkpoint_sha='a' * 40)
    assert other['id'] not in continuation_context(r.c.repo, wake)
