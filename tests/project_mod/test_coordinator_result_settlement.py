"""Result views never promote a pre-settlement Turn snapshot to terminal evidence."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.agent_core.model_context_revision import model_context_revision
from lib.conversations import review_loops
from lib.project_dispatch.service import ProjectDispatchService
from tests.project_mod.test_review_loops import Records
from tests.project_mod.test_coordinator_continuation import run as run, end
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context
from tests.turn_lifecycle.test_task_event_delivery import (
    _task, _discard, storage as storage, turn_service as turn_service,
)

pytestmark = pytest.mark.unit

REPORT = 'Inspected the inert source.\nREVIEW_FACT: observed_source@1\nREVIEW_VERDICT: pass'


@pytest.mark.parametrize('storage', ['sqlite'], indirect=True)
@pytest.mark.parametrize('partial', ['', 'REVIEW_VERDICT: pass'])
@pytest.mark.parametrize('boundary', ['turn', 'attempt'])
@pytest.mark.parametrize('settlement_only', [False, True])
def test_completion_between_reads_never_promotes_partial_result(
        storage, turn_service, partial, boundary, settlement_only):
    _, task = _task(turn_service, storage, 'result-settlement')
    try:
        task['content'] = partial
        assert turn_service.record_task_event(task, {'type': 'phase', 'phase': 'thinking'})
        before = turn_service.get_turn(task['convId'], task['_turnId'], user_id=1)
        assert before['status'] == 'running'
        completed, reads = [], []

        def finish():
            task.update(content=REPORT, status='done', finishReason='stop')
            assert turn_service.record_task_event(task, {'type': 'done', 'finishReason': 'stop'})
            final = turn_service.get_turn(task['convId'], task['_turnId'], user_id=1)
            attempt = turn_service.get_attempt(task['_attemptId'], user_id=1)
            assert final['status'] == attempt['status'] == 'completed'
            assert final['projection']['content'] == REPORT
            assert model_context_revision(final) > model_context_revision(before)
            completed.append(final)

        def read_turn(conversation, identity, **kwargs):
            value = turn_service.get_turn(conversation, identity, **kwargs)
            reads.append('turn')
            if boundary == 'turn' and not completed:
                finish()
            return value

        def read_attempt(identity, **kwargs):
            value = turn_service.get_attempt(identity, **kwargs)
            reads.append('attempt')
            if boundary == 'attempt' and not completed:
                finish()
            return value

        assignment = {'id': 'inert-assignment', 'request': {'recipientId': task['convId']},
            'grant': {'owner': 1, 'revision': 1, 'revoked': False}, 'review': None,
            'execution': {'turnId': task['_turnId'], 'attemptId': task['_attemptId']}}
        service = ProjectDispatchService(SimpleNamespace(owner=1), commands=object(),
            turn_get=read_turn, attempt_get=read_attempt)
        execution = service._view(assignment, coordinator=True,
                                  settlement_only=settlement_only)['execution']
        assert len(completed) == 1
        assert sorted(reads) == ['attempt', 'turn']
        final_revision = model_context_revision(completed[0])
        assert execution['status'] != 'completed' or execution['resultRevision'] == final_revision
        if execution['status'] == 'completed' and not settlement_only:
            assert execution['result']['content'] == REPORT
        if settlement_only:
            assert execution['result'] == {}
        current = service._view(assignment, coordinator=True)['execution']
        assert current == {'turnId': task['_turnId'], 'attemptId': task['_attemptId'],
            'taskId': task['id'], 'status': 'completed', 'resultRevision': final_revision,
            'result': {'content': REPORT}}
        assert len(reads) == 4
    finally:
        _discard(task)


@pytest.fixture
def reviewer(run, monkeypatch):
    r, records = run, Records()
    monkeypatch.setattr(review_loops, '_client', records)
    result = r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'settlement-control',
        'review': {'reviewers': [{'role': 'references'}]}})
    author = r.c.repo.read(result['assignment_id'])
    end(r, author)
    loop_id = result['review_loop_id']
    loop = r.call('poll', ids=[loop_id])['review_loops'][0]
    row = r.c.repo.read(loop['rounds'][0]['reviewers']['references'])
    turn = r.turns[row['execution']['turnId']]
    turn.update(projectionRevision=10, modelContextRevision=10)
    turn['projection']['content'] = REPORT
    calls, facts = [], []
    original = r.service.review_for_coordinator

    def accept(task, scope, arguments):
        calls.append(deepcopy(arguments))
        return original(task, scope, arguments)

    def evidence(self, view, bindings):
        facts.append((view['assignmentId'], bindings))
        return bindings == [('observed_source', 1)]

    monkeypatch.setattr(r.service, 'review_for_coordinator', accept)
    monkeypatch.setattr(review_loops._Ports, 'evidence_current', evidence)
    return SimpleNamespace(r=r, records=records, loop_id=loop_id, row=row, turn=turn,
                           calls=calls, facts=facts)


def test_running_pass_text_is_not_parsed_and_final_result_is_pinned_once(reviewer):
    c = reviewer
    assert c.r.call('poll', ids=[c.loop_id])['review_loops'][0]['status'] == 'reviewing'
    assert not c.calls and not c.facts
    c.turn.update(projectionRevision=11, modelContextRevision=11)
    end(c.r, c.row, delivered=False)
    assert c.r.call('poll', ids=[c.loop_id])['review_loops'][0]['status'] == 'passed'
    assert c.calls == [{'assignment_id': c.row['id'], 'decision': 'accept',
        'note': f'Review loop {c.loop_id} round 1: verdict pass recorded; no files to publish.',
        'result_revision': 11}]
    assert c.facts == [(c.row['id'], [('observed_source', 1)])]
    assert c.r.c.repo.read(c.row['id'])['review']['resultRevision'] == 11
    assert c.r.c.repo.read(c.row['id']).get('publication') is None


@pytest.mark.parametrize('partial', ['', 'Inspected the source, still composing the final report.'])
def test_stale_partial_snapshot_cannot_record_missing_or_pass(reviewer, monkeypatch, partial):
    c = reviewer
    end(c.r, c.row, delivered=False)
    original = c.r.service.view_for_coordinator

    def old_view(identity, *, scope, settlement_only=False):
        view = original(identity, scope=scope, settlement_only=settlement_only)
        if identity == c.row['id'] and not settlement_only:
            view['execution']['result']['content'] = partial
            c.turn.update(projectionRevision=11, modelContextRevision=11)
        return view

    monkeypatch.setattr(c.r.service, 'view_for_coordinator', old_view)
    before = deepcopy(c.records.rows)
    outcome = c.r.call('poll', ids=[c.loop_id])
    assert 'error' in outcome and 'result_revision' in str(outcome)
    assert c.calls[0]['result_revision'] == 10
    assert c.r.c.repo.read(c.row['id'])['review'] is None
    assert c.records.rows == before
    assert not c.facts


@pytest.mark.parametrize('answer, certified, status, verdict', [
    (REPORT, True, 'passed', 'pass'),
    (REPORT, False, 'impasse', 'missing'),
    ('Defect in the inert source.\nREVIEW_VERDICT: fail', True, 'reworking', 'fail'),
    ('Source unavailable.\nREVIEW_VERDICT: impasse', True, 'impasse', 'impasse'),
    ('', True, 'impasse', 'missing'),
])
def test_terminal_controls_and_ended_judgments_remain_immutable(
        reviewer, monkeypatch, answer, certified, status, verdict):
    c = reviewer
    c.turn['projection']['content'] = answer
    monkeypatch.setattr(review_loops._Ports, 'evidence_current', lambda *args: certified)
    end(c.r, c.row, delivered=False)
    follow_ups = []
    def follow_up(self, view, instruction, key):
        follow_ups.append((view['assignmentId'], instruction))
        return view['assignmentId']
    monkeypatch.setattr(review_loops._Ports, 'follow_up', follow_up)
    loop = c.r.call('poll', ids=[c.loop_id])['review_loops'][0]
    assert loop['status'] == status and loop['rounds'][0]['verdicts']['references'] == verdict
    assert c.r.c.repo.read(c.row['id'])['review']['resultRevision'] == 10
    if answer == REPORT and not certified:
        assert 'pass lacks verified current source evidence' in loop['reason']
    if status == 'reworking':
        assert len(follow_ups) == 1 and 'Defect in the inert source.' in follow_ups[0][1]
        return
    assert not follow_ups
    before = deepcopy(c.records.rows)
    c.turn.update(projectionRevision=11, modelContextRevision=11)
    c.turn['projection']['content'] = REPORT if verdict != 'pass' else 'REVIEW_VERDICT: fail'
    assert c.r.call('poll', ids=[c.loop_id])['review_loops'][0] == loop
    assert c.records.rows == before and len(c.calls) == 1


@pytest.mark.parametrize('failure', ['failed', 'revoked', 'identity'])
def test_failure_revocation_and_identity_do_not_consume_pass(reviewer, failure):
    c = reviewer
    end(c.r, c.row, status='failed' if failure == 'failed' else 'completed', delivered=False)
    if failure == 'revoked':
        c.r.c.repo.revoke(c.row['id'], expected_revision=c.row['grant']['revision'])
    elif failure == 'identity':
        c.turn['conversationId'] = 'foreign'
    before = deepcopy(c.records.rows)
    result = c.r.call('poll', ids=[c.loop_id])
    if failure == 'identity':
        assert 'error' in result
        assert c.records.rows == before
    else:
        assert result['review_loops'][0]['status'] == 'impasse'
        assert result['review_loops'][0]['rounds'][0]['verdicts']['references'] == 'missing'
    assert c.r.c.repo.read(c.row['id'])['review'] is None
    assert not c.calls and not c.facts
