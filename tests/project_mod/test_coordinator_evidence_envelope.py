"""A lead reads a Worker's result once, complete, without execution telemetry.

Measured on coordinator-4950501b927a581b666a974cb9ce21b5 (2026-11-15..18): a
get_project_assignment result was 1–23 MB raw (the Worker Turn's whole
projection: tool rounds, API rounds, timing traces) and its 12 KB model view
held only the brief and the frozen delegation tool lists, never the execution,
review or delivery; 306 of 405 poll excerpts were cut at 4,000 characters, so
the lead re-read the result with get_conversation (58 calls, ~23 KB each).
Specification: docs/PROJECT_COORDINATION.md, "A lead reads each result once".
"""
import json

import pytest

from tests.project_mod.test_coordinator_dispatch_tools import runtime as base_runtime
from tests.project_mod.test_coordinator_workers import runtime as runtime
from tests.project_mod.test_project_dispatch_repository import context as context

__all__ = ('base_runtime', 'context', 'runtime')

pytestmark = pytest.mark.unit


def _complete(r, row, *, content, rounds):
    """Settle the Worker with a final answer after ``rounds`` tool rounds."""
    execution = row['execution']
    r.turns[execution['turnId']]['projection'] = {
        'content': content,
        '_bgCommandInjects': [{'commandId': 'bg_1', 'previews': [{'text': 'late output'}]}],
        'toolRounds': [{'toolName': 'run_command', 'toolContent': 'x' * 2000, 'roundNum': n}
                       for n in range(rounds)],
        'apiRounds': [{'round': n, 'usage': {'prompt_tokens': 300_000}} for n in range(rounds)],
        'timingTrace': {'clientObservations': [{'kind': 'phase_painted'}] * rounds},
    }
    r.attempts[execution['attemptId']]['status'] = 'completed'


def _assignment_view(runtime, rounds):
    r = runtime
    worker = r.call('delegate_project_work', **r.args)
    row = r.c.repo.read(worker['assignment_id'])
    _complete(r, row, content='Final answer: all checks met.', rounds=rounds)
    return r.call('get_project_assignment', assignment_id=row['id']), row


def test_assignment_evidence_is_the_result_not_the_execution_telemetry(runtime):
    view, row = _assignment_view(runtime, rounds=50)
    result = view['execution']['result']
    assert result['content'] == 'Final answer: all checks met.'
    assert result['_bgCommandInjects'][0]['commandId'] == 'bg_1'
    assert not {'toolRounds', 'apiRounds', 'timingTrace'} & set(result)
    assert view['execution']['status'] == 'completed'
    assert view['execution']['resultRevision'] is not None
    assert {'review', 'delivery', 'canonicalDelivery', 'grantRevision', 'revoked'} <= set(view)
    request = view['request']
    assert request['brief'] == row['request']['brief']
    assert request['recipientId'] == row['request']['recipientId']
    # The frozen grant (tool names, model config) is authority for admission, not lead evidence.
    assert 'delegation' not in request and 'profile' not in request
    assert 'run_command' not in json.dumps(request)


def test_assignment_evidence_does_not_grow_with_the_worker_rounds(runtime):
    few, _ = _assignment_view(runtime, rounds=1)
    runtime.args = {**runtime.args, 'idempotency_key': 'part-two'}
    many, _ = _assignment_view(runtime, rounds=500)
    size = lambda view: len(json.dumps({**view, 'assignmentId': '', 'request': {
        **view['request'], 'recipientId': ''}, 'execution': {**view['execution'], 'turnId': '',
        'attemptId': '', 'taskId': ''}}))
    assert size(many) == size(few)


def test_a_poll_reports_the_complete_result_once(runtime):
    r = runtime
    worker = r.call('delegate_project_work', **r.args)
    row = r.c.repo.read(worker['assignment_id'])
    answer = 'Acceptance check 1: met. ' * 600
    _complete(r, row, content=answer, rounds=3)
    first = r.call('poll', ids=[row['id']], wait_s=0)
    reported = first['workers'][0]
    assert reported['result'] == answer
    assert 'result_excerpt' not in reported and 'result_truncated' not in reported
    again = r.call('poll', ids=[row['id']], wait_s=0)
    assert again['workers'] == [] and again['unchanged'] == [row['id']]


def test_a_poll_names_each_worker_once_in_its_structured_fields(runtime):
    """The instruction refers to workers/pending/unchanged; it never repeats their IDs.

    Measured: instructions were 297 KB of 2.8 MB poll results, mostly the
    running IDs that pending.running already lists.
    """
    r = runtime
    rows = []
    for key in ('a', 'b', 'c'):
        worker = r.call('delegate_project_work', **{**r.args, 'idempotency_key': key})
        rows.append(r.c.repo.read(worker['assignment_id']))
    _complete(r, rows[0], content='A done', rounds=1)
    value = r.call('poll', ids=[row['id'] for row in rows], wait_s=0)
    assert [w['assignment_id'] for w in value['workers']] == [rows[0]['id']]
    assert value['pending']['running'] == [rows[1]['id'], rows[2]['id']]
    assert not any(row['id'] in value['instruction'] for row in rows), value['instruction']
    assert 'pending.running' in value['instruction']
