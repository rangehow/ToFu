"""Durable model intents whose physical request ended cannot block publication forever.

A settlement observation can be lost (CAS exhaustion, storage outage, ambiguous
intent ACK followed by a refused void, or a process restart). Only a live
process-local lease or a live execution attempt may keep blocking publication.
"""
import threading

import pytest

from lib.agent_core.execution_budget import ExecutionBudget, close_execution_budget
from lib.project_dispatch.accounting import folded_model_event
from lib.project_dispatch.recovery import recover_project_dispatch
from lib.project_dispatch.repository import ProjectDispatchError
from tests.project_mod.test_project_dispatch_dependencies import (
    completed, context as context, dependency_context as dependency_context)

pytestmark = pytest.mark.unit

NONCE = '0123456789abcdef0123456789abcdef'


def intent(ordinal=7, reported=0):
    return {'root_task_id': 'task-prerequisite', 'attempt_ordinal': ordinal,
            'attempt_task_id': 'task-prerequisite', 'reserved_tokens': 10, 'phase': 'dispatch_intent',
            'reported_tokens': reported, 'usage_complete': False, 'account_nonce': NONCE,
            'account_observed_tokens': 0}


def _published(c, *, tools=('read_files',)):
    prior = completed(c, files=True, reviewed=True, tools=tools)
    c.repo.request_publication(prior['id'], principal=c.principal, expected_revision=1,
                               result_revision=5, expected_checkpoint_sha='exact-checkpoint')
    c.repo.mutate(prior['id'], lambda a: a['modelAttempts'].append(intent()))
    c.prior, c.binding = prior, c.repo.admission(prior['id'])
    c.live = set()
    c.repo.model_lease_open = lambda root, ordinal: (root, ordinal) in c.live
    return c


@pytest.fixture
def published(dependency_context):
    return _published(dependency_context)


@pytest.fixture
def published_writer(dependency_context):
    """A reviewed file result whose grant can also write."""
    return _published(dependency_context, tools=('read_files', 'write_file'))


def phases(c):
    return [row['phase'] for row in c.repo.read(c.prior['id'])['modelAttempts']]


def test_live_lease_blocks_and_refusal_names_the_execution(published):
    c = published
    c.live.add(('task-prerequisite', 7))
    with pytest.raises(ProjectDispatchError,
                       match=r'In-flight paid execution cannot publish: model attempt '
                             r'task-prerequisite#7 \(task task-prerequisite\) phase=dispatch_intent'):
        c.repo.validate_publication(c.binding, 'task-prerequisite')
    assert phases(c) == ['dispatch_intent']


def test_live_execution_attempt_blocks_without_lease(published):
    c = published
    c.attempts['attempt-prerequisite']['status'] = 'running'
    with pytest.raises(ProjectDispatchError, match='model attempt task-prerequisite#7'):
        c.repo.validate_publication(c.binding, 'task-prerequisite')
    assert phases(c) == ['dispatch_intent']


def test_ended_intent_settles_as_unknown_lower_bound_and_publishes(published):
    c = published
    assert c.repo.validate_publication(c.binding, 'task-prerequisite')['id'] == c.prior['id']
    row = c.repo.read(c.prior['id'])['modelAttempts'][0]
    assert row == {**intent(), 'phase': 'settled', 'usage_complete': False}
    # Idempotent: a second check performs no further write.
    before = c.client.puts
    assert c.repo.validate_publication(c.binding, 'task-prerequisite')['id'] == c.prior['id']
    assert c.client.puts == before


def test_startup_and_settlement_recovery_settle_ended_intents(published):
    c = published
    recover_project_dispatch(c.repo.owner, c.repo.project, repository=c.repo, service=c.service,
                             was_aborted_after=lambda *a: False, clock=c.repo.clock)
    assert phases(c) == ['settled']


def test_recovery_keeps_live_intents(published):
    c = published
    c.live.add(('task-prerequisite', 7))
    recover_project_dispatch(c.repo.owner, c.repo.project, repository=c.repo, service=c.service,
                             was_aborted_after=lambda *a: False, clock=c.repo.clock)
    assert phases(c) == ['dispatch_intent']


def tool_intent(name, ordinal=3):
    return {'root_task_id': 'task-prerequisite', 'tool_ordinal': ordinal, 'task_id': 'task-prerequisite',
            'tool_call_id': f'call-{ordinal}', 'tool_name': name, 'phase': 'intent', 'account_nonce': NONCE}


def test_an_unconfirmed_read_only_call_never_blocks_publication(published):
    c = published
    c.repo.mutate(c.prior['id'], lambda a: a.update(modelAttempts=[], toolAttempts=[tool_intent('read_files')]))
    assert c.repo.validate_publication(c.binding, 'task-prerequisite')['id'] == c.prior['id']


def test_an_unconfirmed_side_effect_blocks_publication_until_settled(published_writer):
    from lib.project_dispatch.accounting import ACKNOWLEDGED_TOOL_PHASE, tool_operation_id
    c = published_writer
    unknown = tool_intent('write_file')
    c.repo.mutate(c.prior['id'], lambda a: a.update(modelAttempts=[], toolAttempts=[unknown]))
    with pytest.raises(ProjectDispatchError,
                       match=r'Unconfirmed tool execution cannot publish: tool attempt '
                             r'task-prerequisite#3 write_file call-3 phase=intent '
                             r'operation_id=' + tool_operation_id(unknown) + r'; .*inspect_tool_effect'):
        c.repo.validate_publication(c.binding, 'task-prerequisite')
    # The Worker's acknowledgement (or a continuation) settles it; then it publishes.
    c.repo.mutate(c.prior['id'], lambda a: a['toolAttempts'][0].update(phase=ACKNOWLEDGED_TOOL_PHASE))
    assert c.repo.validate_publication(c.binding, 'task-prerequisite')['id'] == c.prior['id']


def test_folded_watermark_never_hides_a_retained_open_occurrence():
    holder = {'modelAttempts': [intent(ordinal=2)],
              'settledModelUsage': {'requests': 20, 'tokens': 0, 'complete': True,
                                    'watermarks': {'task-prerequisite': 30}}}
    assert not folded_model_event(holder, {**intent(ordinal=2), 'phase': 'settled'})
    assert folded_model_event(holder, {**intent(ordinal=3), 'phase': 'settled'})


def test_model_lease_open_tracks_the_live_process_local_lease():
    from lib.agent_core.execution_budget import model_lease_open
    task = {'id': 'lease-root', 'config': {}, 'abort_event': threading.Event()}
    account = ExecutionBudget(task)
    task['_executionBudget'] = account
    lease = account.reserve_model(task, input_tokens=1, max_output_tokens=1)
    try:
        assert model_lease_open('lease-root', lease.ordinal)
        assert not model_lease_open('lease-root', lease.ordinal + 1)
        assert not model_lease_open('other-root', lease.ordinal)
    finally:
        lease.settle(unsent=True)
    assert not model_lease_open('lease-root', lease.ordinal)
    close_execution_budget(task)
