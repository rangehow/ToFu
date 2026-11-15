"""A follow-up wave keeps admitting dependency-blocked workers after its caller turn ends.

Incident: a later turn added ``review`` (depends on ``journal``) to a live
session and settled 'done'. When ``journal`` settled, ``review`` started and
its attachment under the finished caller was refused with
'execution_owner_cancelled'. The wave now attaches one admission owner while
the caller is live; it keeps the caller's account, survives the caller's
normal finish, is fenced by session Stop and detaches with the wave.
"""
import pytest

from lib.agent_core.execution_budget import (
    ModelBudgetExceeded, _ensure_execution_budget, close_execution_budget,
    inherit_execution_budget)
from lib.swarm.master import bind_assignment_contexts, release_assignment_owner
from lib.swarm.protocol import SubTaskSpec
from tests.swarm.test_retained_assignment_authority import authority as authority, tool

pytestmark = pytest.mark.unit


def _wave(a, *, wave_owner):
    specs = [SubTaskSpec(id='journal', role='general', objective='journal'),
             SubTaskSpec(id='review', role='reviewer', objective='review',
                         depends_on=['journal'])]
    bind_assignment_contexts(specs, parent_task=a.current,
        parent_config=a.current['config'], all_tools=[tool('read_files')],
        model='current-model', wave_owner=wave_owner)
    a.session.register_followup_specs(specs)
    return specs


def _start(a, spec, name):
    worker = {'id': name, 'config': {}}
    inherit_execution_budget(a.session._make_agent(spec).parent_task, worker)
    return worker


def test_followup_wave_worker_attaches_after_its_caller_turn_settled(authority):
    a = authority
    account = _ensure_execution_budget(a.current)
    journal, review = _wave(a, wave_owner=True)
    owner = review.retained_assignment_owner
    assert owner is journal.retained_assignment_owner and owner['id'] in account._active

    a.current['status'] = 'done'
    worker = _start(a, review, 'review-worker')
    assert worker['_executionBudget'] is account
    close_execution_budget(worker)

    release_assignment_owner(journal)
    assert owner['id'] in account._active
    release_assignment_owner(review)
    assert owner['id'] not in account._active
    assert review.retained_assignment_owner is None


def test_without_a_wave_owner_the_finished_caller_refuses_the_worker(authority):
    a = authority
    _ensure_execution_budget(a.current)
    _, review = _wave(a, wave_owner=False)
    assert review.retained_assignment_owner is None
    a.current['status'] = 'done'
    with pytest.raises(ModelBudgetExceeded, match='execution_owner_cancelled'):
        _start(a, review, 'review-worker')


def test_session_stop_fences_the_wave_owner(authority):
    a = authority
    _ensure_execution_budget(a.current)
    _, review = _wave(a, wave_owner=True)
    a.current['status'] = 'done'
    agent = a.session._make_agent(review)
    a.session.abort()
    with pytest.raises(ModelBudgetExceeded, match='execution_owner_cancelled'):
        inherit_execution_budget(agent.parent_task, {'id': 'late', 'config': {}})


def test_durably_observed_accounts_keep_their_membership_unchanged(authority):
    a = authority
    account = _ensure_execution_budget(a.current)
    account._child_observer = lambda event: None
    _, review = _wave(a, wave_owner=True)
    assert review.retained_assignment_owner is None
    assert set(account._active) == {a.current['id']}
