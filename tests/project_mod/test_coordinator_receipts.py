"""review_project_work and cancel_project_work answer with compact effect receipts.

A receipt names what changed (assignment, decision, revisions, delivery) and the
next step. It never echoes the brief, the granted tool list or the Worker's
transcript: those stay with get_project_assignment and get_conversation, and
the old full view cost the Coordinator thousands of tokens on every call.
"""
import json

import pytest

from tests.project_mod.test_coordinator_continuation import (
    activate, delegate, end, ready, run as run)
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context

__all__ = ('runtime', 'base_runtime', 'context')

pytestmark = pytest.mark.unit

RECEIPT = {'assignment_id', 'conversation_id', 'decision', 'grant_revision', 'revoked', 'result_revision',
           'execution_status', 'checkpoint_sha', 'delivery', 'canonical_delivery', 'next_step'}


def _worker_turn_reads(r, monkeypatch):
    reads = []
    original = r.service.turn_get

    def turn_get(conversation, turn_id, **kwargs):
        reads.append((turn_id, kwargs.get('include_projection', True)))
        return original(conversation, turn_id, **kwargs)
    monkeypatch.setattr(r.service, 'turn_get', turn_get)
    monkeypatch.setattr(r.c.repo, 'turn_get', turn_get)
    return reads


def _echoes_view(r, worker, receipt):
    text = json.dumps(receipt, ensure_ascii=False)
    return any(value in text for value in (
        worker['request']['brief'], 'write_file', 'verified evidence', '"request"'))


@pytest.mark.parametrize('decision', ['accept', 'rework'])
def test_review_receipt_names_decision_revisions_and_delivery_only(run, monkeypatch, decision):
    r = run
    worker = ready(r)
    activate(r)
    reads = _worker_turn_reads(r, monkeypatch)
    receipt = r.call('review_project_work', assignment_id=worker['id'], result_revision=1,
                     decision=decision, note='Verified the exact evidence')
    assert set(receipt) == RECEIPT, receipt
    assert receipt['assignment_id'] == worker['id'] and receipt['decision'] == decision
    assert receipt['conversation_id'] == worker['request']['recipientId']
    assert (receipt['grant_revision'], receipt['result_revision']) == (worker['grant']['revision'], 1)
    assert receipt['revoked'] is False and receipt['execution_status'] == 'completed'
    assert receipt['checkpoint_sha'] is None and receipt['canonical_delivery'] is True
    assert receipt['delivery']['status'] == 'delivered'
    assert ('rework_of' in receipt['next_step']) is (decision == 'rework')
    if decision == 'accept':
        assert receipt['next_step'] == (
            'Recorded as Coordinator judgment, not human acceptance. Delivery is canonical; '
            'no publication action is needed for this result.')
    assert not _echoes_view(r, worker, receipt)
    # The receipt reads the Worker Turn's metadata; its projection can be megabytes.
    turn = worker['execution']['turnId']
    assert reads and all(projection is False for turn_id, projection in reads if turn_id == turn), reads
    assert r.call('review_project_work', assignment_id=worker['id'], result_revision=1,
                  decision=decision, note='Verified the exact evidence') == receipt


@pytest.mark.parametrize('status', ['pending', 'blocked', 'failed', 'preserved', 'unknown'])
def test_noncanonical_receipt_keeps_existing_publication_guidance_without_mutation(status):
    from copy import deepcopy
    from lib.conversations.coordinator_tools import _review_receipt

    view = {'assignmentId': 'ordinary-worker', 'request': {'recipientId': 'worker-session'},
            'grantRevision': 1, 'revoked': False,
            'execution': {'status': 'completed', 'resultRevision': 2},
            'review': {'decision': 'accept', 'resultRevision': 2},
            'publication': None, 'canonicalDelivery': False,
            'delivery': {'status': status, 'checkpointSha': 'c' * 40}}
    before = deepcopy(view)
    receipt = _review_receipt(view)
    assert receipt['next_step'] == (
        'Recorded as Coordinator judgment, not human acceptance. No code is published without '
        'checkpoint_sha (the exact delivery.checkpointSha). To publish this accept, repeat it with the '
        'same note, the current result_revision and checkpoint_sha.')
    assert receipt['canonical_delivery'] is False and receipt['checkpoint_sha'] is None
    assert receipt['delivery'] == view['delivery']
    assert view == before


def test_existing_authored_unpublished_checkpoint_still_requires_publication(run):
    from tests.project_mod.test_coordinator_dependency_links import completed

    r = run
    author, pin = completed(r)
    workspace = r.workspaces[author['execution']['taskId']]
    workspace['origin']['delivery'].update(state='blocked', reason='project_publication_authority_missing')
    receipt = r.call('review_project_work', assignment_id=author['id'], result_revision=1,
                     decision='accept', note='Verified authored files; publication not requested.')
    assert receipt['canonical_delivery'] is False and receipt['checkpoint_sha'] is None
    assert receipt['delivery']['checkpointSha'] == pin['checkpointSha']
    assert receipt['delivery']['status'] == 'blocked'
    assert 'To publish this accept, repeat it' in receipt['next_step']
    assert r.c.repo.read(author['id']).get('publication') is None


@pytest.mark.parametrize('decision', ['rework', 'reject'])
def test_unsuccessful_review_keeps_repair_guidance_even_with_canonical_delivery(decision):
    from lib.conversations.coordinator_tools import _review_receipt

    view = {'assignmentId': 'ordinary-worker', 'request': {'recipientId': 'worker-session'},
            'grantRevision': 1, 'revoked': False,
            'execution': {'status': 'completed', 'resultRevision': 2},
            'review': {'decision': decision, 'resultRevision': 2},
            'publication': None, 'canonicalDelivery': True,
            'delivery': {'status': 'delivered'}}
    receipt = _review_receipt(view)
    assert 'Repair with control_conversation follow_up' in receipt['next_step']
    assert 'no publication action is needed' not in receipt['next_step']


def test_cancel_receipt_names_the_withdrawn_grant_and_replays_identically(run, monkeypatch):
    r = run
    done = delegate(r)
    active = delegate(r, 'active')
    end(r, done)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    assert r.wake() == ['accepted']
    activate(r)
    reads = _worker_turn_reads(r, monkeypatch)
    receipt = r.call('cancel_project_work', assignment_id=active['id'])
    assert set(receipt) == RECEIPT, receipt
    assert receipt['assignment_id'] == active['id'] and receipt['decision'] == 'cancelled'
    assert receipt['conversation_id'] == active['request']['recipientId']
    assert receipt['revoked'] is True and receipt['grant_revision'] == active['grant']['revision'] + 1
    assert receipt['execution_status'] == 'interrupted'
    assert not _echoes_view(r, active, receipt)
    turn = active['execution']['turnId']
    assert all(projection is False for turn_id, projection in reads if turn_id == turn), reads
    assert r.call('cancel_project_work', assignment_id=active['id']) == receipt
