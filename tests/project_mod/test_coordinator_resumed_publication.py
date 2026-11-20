"""A request's accepted review stays publishable after a restart.

RESTART-DRILL-1 coord-p-pk (2026-11-13: R3 240ca7ccf3, ship+fix, R4 carry): the
Coordinator Turn resumed after the kill accepted its Worker's result without
checkpoint_sha, so no publication pin was recorded and the receiver held the
delivery as project_publication_authority_missing (PublicationAwaitsReview). The
restart changed no authority: the pin's subject is the request's original
delegation, and the resumed attempt's own review passed every live check.

An accept recorded without a pin could gain one only by repeating the identical
review, and that stopped working once the Worker Turn gained a projection revision
(e93634d4, DB-3-R2: accepted at result 6681, current 6683 on 2026-11-13). Accepts
recorded without a pin on purpose ('accepted as evidence', the code shipping
elsewhere) hold the same way and stay unpublished. Re-affirming an accepted review
with the registered checkpoint at the current result revision now publishes it,
exactly as it already re-pinned a published accept; only the same request's
attempts, or any Turn of the Coordinator binding that dispatched it, may do it.
"""
from copy import deepcopy
import threading
from types import SimpleNamespace

import pytest

from lib.project_dispatch import admission, publication
from lib.project_dispatch.repository import PublicationAwaitsReview
from lib.project_dispatch.request_budget import bind_source_account
from tests.project_mod.test_coordinator_continuation import (
    run as run, runtime, base_runtime, context, activate, delegate, end,
)
from tests.project_mod.test_coordinator_dependency_links import completed
from tests.project_mod.test_coordinator_worker_handoff import next_human_attempt
from tests.project_mod.test_coordinator_workers import _commitment_wake

pytestmark = pytest.mark.unit

NOTE = 'Drill accept.'


def _restart_resume(r):
    """What a restart leaves and resumes: the request Turn's Attempt interrupted
    and the same Turn continued by a new Attempt running as a new task."""
    previous = r.task
    r.attempts[previous['_attemptId']]['status'] = 'interrupted'
    r.attempts['resumed-attempt'] = {
        **r.attempts[previous['_attemptId']], 'attemptId': 'resumed-attempt',
        'taskId': 'conversation-attempt:resumed-attempt', 'status': 'running', 'createdAt': 1_003_000}
    r.turns[previous['_turnId']].update(currentAttemptId='resumed-attempt')
    r.task = {key: value for key, value in previous.items()
              if key not in ('_coordinatorRequestBudget', '_executionBudget')}
    r.task.update(id='conversation-attempt:resumed-attempt', _attemptId='resumed-attempt',
                  usage={}, abort_event=threading.Event())
    if previous.get('_coordinatorRequestBudget') is not None:
        bind_source_account(r.task, repository=r.c.repo)


def _receiver_verdict(r, worker, monkeypatch):
    """The delivery receiver's verdict on the Worker's registered checkout."""
    from lib import integration_control
    binding = r.c.repo.admission(worker['id'])
    task_id = worker['execution']['taskId']
    workspace = r.workspaces[task_id]
    row = {**workspace, 'task_id': task_id, 'user_id': binding['owner'], 'project_root': binding['projectPath'],
           'origin': {**workspace['origin'], admission.KEY: binding, 'sourceSnapshot': 'b' * 40}}
    monkeypatch.setattr(integration_control, '_git', lambda *a, **k: SimpleNamespace(stdout=''))
    monkeypatch.setattr(publication, '_ASSIGNMENT_DECIDED', {})
    return publication.publication_reason(row, {})


def _publishing(r):
    woken, released = [], []
    r.service.publication_wake = lambda: woken.append(True)
    r.service.publication_release = lambda project, task_id, *, user_id: released.append(task_id)
    return woken, released


@pytest.mark.parametrize('drift', [0, 2])
@pytest.mark.parametrize('accepted_by', ['interrupted', 'resumed'])
def test_a_resumed_request_publishes_its_accepted_review_at_the_current_result(
        run, monkeypatch, accepted_by, drift):
    """coord-p-pk: the accept carried no checkpoint_sha, and the Worker Turn may have
    gained projection revisions since (drift). The same request publishes it."""
    r = run; worker, pin = completed(r)
    binding, task_id = r.c.repo.admission(worker['id']), worker['execution']['taskId']
    workspace = r.workspaces[task_id]
    workspace.update(state='active')
    receipt = workspace['origin']['delivery']
    workspace['origin']['sourceSnapshot'] = 'b' * 40
    executor_task_id = r.attempts[worker['execution']['attemptId']]['taskId']
    receipt.update(state='pending', phase='checkpoint', executorTaskId=executor_task_id,
                   publicationMode='committed')
    assert receipt['checkpointSha'] != workspace['origin']['sourceSnapshot']
    woken, released = _publishing(r)

    def receive():
        assert r.c.repo.validate_publication(binding, task_id)['id'] == worker['id']
        woken.append(True)
        workspace['state'] = 'integrated'
        receipt.update(state='delivered', headSha=pin['checkpointSha'], targetSha=pin['checkpointSha'])

    r.service.publication_wake = receive
    if accepted_by == 'resumed':
        _restart_resume(r)
    accepted = r.call('review_project_work', assignment_id=worker['id'], result_revision=1,
                      decision='accept', note=NOTE)
    assert 'error' not in accepted and accepted['checkpoint_sha'] is None, accepted
    assert not accepted['canonical_delivery'] and accepted['delivery']['status'] == 'pending', accepted
    assert 'same note, the current result_revision and checkpoint_sha' in accepted['next_step']
    assert woken == released == []
    with pytest.raises(PublicationAwaitsReview):
        r.c.repo.validate_publication(binding, task_id)
    assert _receiver_verdict(r, worker, monkeypatch) == 'project_publication_authority_missing'
    if accepted_by == 'interrupted':
        _restart_resume(r)
    current = 1 + drift
    r.turns[worker['execution']['turnId']]['projectionRevision'] = current

    published = r.call('review_project_work', assignment_id=worker['id'], result_revision=current,
                       decision='accept', note=NOTE, checkpoint_sha=pin['checkpointSha'])

    assert 'error' not in published, published
    assert published['checkpoint_sha'] == pin['checkpointSha'] and published['canonical_delivery'], published
    assert woken == [True] and released == [task_id]
    row = r.c.repo.read(worker['id'])
    assert row['review'] == {'actor': 'coordinator', 'decision': 'accept', 'note': NOTE, 'resultRevision': current}
    assert (row['publication']['checkpointSha'], row['publication']['resultRevision']) == (pin['checkpointSha'], current)
    # Authority is the request's: the pin names its original delegation, not the resumed attempt.
    assert row['publication']['subject'] == 'coordinator:' + worker['request']['delegation']['taskId']
    assert row['publication']['subject'] != 'coordinator:' + r.task['id']
    assert r.c.repo.validate_publication(binding, task_id)['id'] == worker['id']
    assert _receiver_verdict(r, worker, monkeypatch) == ''
    before = deepcopy((row, workspace, r.calls, r.created, woken, released))
    terminal = r.service.view(worker['id'], principal=r.c.principal)
    assert terminal['canonicalDelivery'] and terminal['publication'] == row['publication']
    assert (r.c.repo.read(worker['id']), workspace, r.calls, r.created, woken, released) == before


def test_a_resumed_no_file_result_needs_no_publication(run):
    r = run
    worker = delegate(r)
    end(r, worker)
    workspace = r.workspaces[worker['execution']['taskId']]
    workspace.update(checkpoint_sha='b' * 40, state='integrated')
    workspace['origin']['sourceSnapshot'] = 'b' * 40
    workspace['origin']['delivery'].update(
        phase='no_changes', publicationMode='committed', checkpointSha='b' * 40,
        headSha='b' * 40, targetSha='b' * 40,
        executorTaskId=r.attempts[worker['execution']['attemptId']]['taskId'])
    woken, released = _publishing(r)
    _restart_resume(r)

    accepted = r.call('review_project_work', assignment_id=worker['id'], result_revision=1,
                      decision='accept', note=NOTE)

    assert 'error' not in accepted, accepted
    assert accepted['canonical_delivery'] and accepted['checkpoint_sha'] is None, accepted
    assert 'no publication action is needed' in accepted['next_step']
    row = r.c.repo.read(worker['id'])
    assert row.get('publication') is None and woken == released == []
    before = deepcopy((row, workspace, r.calls, r.created))
    terminal = r.service.view(worker['id'], principal=r.c.principal)
    assert terminal['canonicalDelivery'] and terminal['publication'] is None
    assert (r.c.repo.read(worker['id']), workspace, r.calls, r.created) == before
    assert woken == released == []


def _continuation_with_another_requests_worker(r):
    """A continuation of the first request, and a completed Worker of a later human request."""
    first = delegate(r); end(r, first)
    original = next_human_attempt(r, new_input=True)
    other = delegate(r, 'other-request')
    r.attempts['resumed-attempt']['status'] = 'completed'
    r.task = original
    assert r.wake() == ['accepted']
    activate(r)
    end(r, other)
    workspace = r.workspaces[other['execution']['taskId']]
    workspace.update(checkpoint_sha='a' * 40, state='integrated')
    workspace['origin']['delivery']['checkpointSha'] = workspace['checkpoint_sha']
    return other


def test_a_continuation_publishes_its_review_of_another_requests_worker(run, monkeypatch):
    """Review is scoped by the Coordinator binding (coordinator-4950501b, 2026-11-16): an accept
    recorded by another request's continuation publishes under the reviewed assignment's own
    grant, never held as project_publication_authority_missing."""
    r = run
    worker = _continuation_with_another_requests_worker(r)
    woken, released = _publishing(r)
    published = r.call('review_project_work', assignment_id=worker['id'], result_revision=1,
                       decision='accept', note=NOTE, checkpoint_sha='a' * 40)
    assert 'error' not in published, published
    assert published['checkpoint_sha'] == 'a' * 40 and published['canonical_delivery'], published
    task_id = worker['execution']['taskId']
    assert woken == [True] and released == [task_id]
    row = r.c.repo.read(worker['id'])
    # The pin's subject is the reviewed Worker's own delegation, not the reviewing continuation.
    assert row['publication']['subject'] == 'coordinator:' + worker['request']['delegation']['taskId']
    assert row['publication']['subject'] != 'coordinator:' + r.task['id']
    assert r.c.repo.validate_publication(r.c.repo.admission(worker['id']), task_id)['id'] == worker['id']
    assert _receiver_verdict(r, worker, monkeypatch) == ''


@pytest.mark.parametrize('refusal', [
    'nonhuman_request', 'revoked_grant', 'other_decision', 'owner_review',
    'changed_note', 'unregistered_checkpoint', 'earlier_result'])
def test_publishing_an_accepted_review_keeps_every_authority_refusal(run, monkeypatch, refusal):
    from lib import integration_control

    default_releases = []
    monkeypatch.setattr(integration_control, 'release_publication_hold',
                        lambda *args, **kwargs: default_releases.append((args, kwargs)))
    r = run
    worker, pin = completed(r)
    checkpoint = pin['checkpointSha']
    woken, released = _publishing(r)
    if refusal == 'owner_review':
        # The owner's own review route records an accept the Coordinator never made.
        r.c.repo.record_review(worker['id'], 'accept', NOTE, 1, worker['grant']['revision'])
    else:
        decision = 'rework' if refusal == 'other_decision' else 'accept'
        recorded = r.call('review_project_work', assignment_id=worker['id'], result_revision=1,
                          decision=decision, note=NOTE)
        assert 'error' not in recorded, recorded
    assert woken == []
    assert released == ([worker['execution']['taskId']] if refusal == 'other_decision' else [])
    callbacks_before = deepcopy((woken, released))
    _restart_resume(r)
    r.turns[worker['execution']['turnId']]['projectionRevision'] = 3
    before = r.c.repo.read(worker['id'])
    arguments = {'assignment_id': worker['id'], 'result_revision': 3, 'decision': 'accept', 'note': NOTE,
                 'checkpoint_sha': checkpoint}
    if refusal == 'nonhuman_request':
        _commitment_wake(r)
    elif refusal == 'revoked_grant':
        r.c.repo.revoke(worker['id'], expected_revision=worker['grant']['revision'])
        before = r.c.repo.read(worker['id'])
    elif refusal == 'changed_note':
        arguments['note'] = 'Accepted after the restart.'
    elif refusal == 'unregistered_checkpoint':
        arguments['checkpoint_sha'] = 'c' * 40
    elif refusal == 'earlier_result':
        arguments['result_revision'] = 1

    refused = r.call('review_project_work', **arguments)

    assert 'error' in refused, refused
    after = r.c.repo.read(worker['id'])
    assert after['review'] == before['review'] and after.get('publication') is None
    assert (woken, released) == callbacks_before
    assert default_releases == []
    if refusal == 'nonhuman_request':
        assert refused['code'] == 'nonhuman_request', refused
    elif refusal == 'changed_note':
        # Publishing repeats the recorded judgment; the refusal names how.
        assert refused['execution_started'] is False, refused
        assert 'recorded note' in refused['recovery'] and 'current result_revision' in refused['recovery']


@pytest.mark.parametrize('note', ['identical', 'regenerated'])
def test_a_resumed_attempt_repeating_its_published_accept_changes_nothing(run, note):
    """The publish call was cut after it committed: the resumed attempt repeats it
    (its model may word the note anew). One publication stands, so one merge."""
    r = run; worker, pin = completed(r)
    _publishing(r)
    arguments = {'assignment_id': worker['id'], 'result_revision': 1, 'decision': 'accept', 'note': NOTE,
                 'checkpoint_sha': pin['checkpointSha']}
    assert 'error' not in r.call('review_project_work', **arguments)
    recorded = r.c.repo.read(worker['id'])
    _restart_resume(r)
    if note == 'regenerated':
        arguments['note'] = 'Accepted; publishing the reviewed checkpoint.'

    again = r.call('review_project_work', **arguments)

    assert 'error' not in again, again
    assert again['checkpoint_sha'] == pin['checkpointSha'] and again['canonical_delivery'], again
    row = r.c.repo.read(worker['id'])
    assert row['review'] == recorded['review'] and row['publication'] == recorded['publication']
