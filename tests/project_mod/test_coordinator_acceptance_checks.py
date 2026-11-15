"""Acceptance checks: a delegation's numbered requirements and the lead's per-check verdicts.

A Coordinator lists concrete checks with delegate_project_work; they are stored
immutably with the brief and shown to the Worker as a numbered list. A rework
keeps the reworked assignment's checks unless it names its own. review_project_work
judges each check by its number: an accept must judge every check and is refused
while any is unmet, and unknown is recorded, never counted as met. Reads carry
the checks, verdicts and counts; the compact receipt carries only the counts.
Work without checks behaves as before (docs/PROJECT_COORDINATION.md,
"Acceptance checks").
"""
import json

import pytest

from lib.project_dispatch import acceptance
from lib.project_dispatch.repository import ProjectDispatchError, _validate_schema
from tests.project_mod.test_coordinator_continuation import end, run as run
from tests.project_mod.test_coordinator_receipts import RECEIPT
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context

__all__ = ('runtime', 'base_runtime', 'context')

pytestmark = pytest.mark.unit

CHECKS = ['pytest tests/feature passes', 'docs/FEATURE.md describes the flag']


@pytest.fixture
def inputs(run, monkeypatch):
    """The Worker input text of every birth request, in order."""
    seen = []
    create_turn = run.service.commands.create_turn

    def record(conversation, owner, request, **kwargs):
        seen.append(request['inputTurn']['content'])
        return create_turn(conversation, owner, request, **kwargs)
    monkeypatch.setattr(run.service.commands, 'create_turn', record)
    return seen


def delegate(r, key, **extra):
    result = r.call('delegate_project_work', **{**r.args, 'idempotency_key': key, **extra})
    assert result.get('dispatch') == 'accepted', result
    return r.c.repo.read(result['assignment_id'])


def completed(r, key='checked', **extra):
    row = delegate(r, key, **extra)
    end(r, row)
    return row


def review(r, row, decision, **extra):
    return r.call('review_project_work', assignment_id=row['id'], result_revision=1,
                  decision=decision, note='Read the Worker evidence', **extra)


def verdicts(*names):
    return [{'check': number, 'verdict': name, 'evidence': f'observed for check {number}'}
            for number, name in enumerate(names, 1)]


def numbered(checks):
    return '\n' + '\n'.join(f'{number}. {check}' for number, check in enumerate(checks, 1))


def test_contract_admits_concrete_distinct_checks_and_complete_verdicts():
    request = {'projectPath': '/project', 'coordinatorId': 'coordinator', 'recipientId': 'worker',
               'authorizationSource': {'conversationId': 'coordinator', 'turnId': 'human', 'projectionRevision': 1},
               'brief': 'Implement it', 'model': 'model-a', 'budget': {}}
    arguments = {'title': 'Part', 'brief': 'Implement it', 'idempotency_key': 'part'}
    for value, definition in ((arguments, 'delegateRequest'), (request, 'request')):
        _validate_schema({**value, 'acceptance': CHECKS}, definition)
        for bad in ([''], ['   '], [CHECKS[0], CHECKS[0]], [7], ['x' * 2049]):
            with pytest.raises(ProjectDispatchError):
                _validate_schema({**value, 'acceptance': bad}, definition)
    review_arguments = {'assignment_id': 'worker-a', 'result_revision': 1, 'decision': 'accept', 'note': 'n'}
    _validate_schema({**review_arguments, 'acceptance_results': verdicts('met', 'unknown')},
                     'coordinatorReviewRequest')
    for bad in ({'check': 0, 'verdict': 'met', 'evidence': 'e'},
                {'check': 1, 'verdict': 'maybe', 'evidence': 'e'},
                {'check': 1, 'verdict': 'met', 'evidence': ' \n'},
                {'check': 1, 'verdict': 'met'},
                {'check': 1, 'verdict': 'met', 'evidence': 'e', 'score': 1}):
        with pytest.raises(ProjectDispatchError):
            _validate_schema({**review_arguments, 'acceptance_results': [bad]}, 'coordinatorReviewRequest')


def test_tool_schemas_carry_the_acceptance_guidance():
    from lib.tools.coordinator import COORDINATOR_TOOLS
    tools = {tool['function']['name']: tool['function'] for tool in COORDINATOR_TOOLS}
    delegation, judgment = tools['delegate_project_work'], tools['review_project_work']
    assert 'rework_of' in delegation['parameters']['properties']['acceptance']['description']
    assert 'acceptance' in delegation['description']
    assert 'acceptance_results' in judgment['parameters']['properties']
    assert 'acceptance_results' in judgment['description'] and 'unmet' in judgment['description']


def test_checks_are_stored_with_the_brief_and_numbered_in_the_worker_input(run, inputs):
    r = run
    given = [CHECKS[0], '  ' + CHECKS[1] + '\n']
    row = delegate(r, 'checked', acceptance=given)
    assert row['request']['acceptance'] == CHECKS
    content = inputs[0]
    assert content.startswith(r.args['brief']) and content.index('Delegated for') < content.index('Acceptance checks')
    assert content.endswith(numbered(CHECKS)) and 'evidence' in content[content.index('Acceptance checks'):]
    replay = r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'checked', 'acceptance': given})
    assert replay['assignment_id'] == row['id'] and len(r.calls) == 1
    changed = r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'checked', 'acceptance': CHECKS[:1]})
    assert 'error' in changed and len(r.calls) == 1
    assert r.c.repo.read(row['id'])['request']['acceptance'] == CHECKS


def test_checks_equal_after_trimming_are_refused_before_any_worker_exists(run, monkeypatch):
    from lib.conversations import coordinator_births
    r = run
    # The refusal needs no storage, so the call refuses before a birth starts.
    monkeypatch.setattr(coordinator_births, 'start', lambda *args, **kwargs: pytest.fail('birth started'))
    refused = r.call('delegate_project_work', **{**r.args, 'acceptance': [CHECKS[0], CHECKS[0] + ' ']})
    assert refused['code'] == 'acceptance_checks_repeated' and refused['execution_started'] is False
    assert refused['details'] == {'repeated': [CHECKS[0]]}
    assert r.created == [] and r.calls == [] and r.c.repo.list() == []


@pytest.mark.parametrize('own', [None, [], ['The flag defaults to off']])
def test_a_rework_keeps_the_reworked_checks_unless_it_names_its_own(run, inputs, own):
    r = run
    first = completed(r, acceptance=CHECKS)
    assert review(r, first, 'rework', acceptance_results=verdicts('met', 'unmet'))['decision'] == 'rework'
    pin = {'assignmentId': first['id'], 'grantRevision': first['grant']['revision'], 'resultRevision': 1}
    extra = {} if own is None else {'acceptance': own}
    repair = delegate(r, 'repair', rework_of=pin, **extra)
    expected = own or CHECKS
    assert repair['request']['reworkOf'] == pin and repair['request']['acceptance'] == expected
    assert inputs[-1].endswith(numbered(expected))
    replay = r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'repair', 'rework_of': pin, **extra})
    assert replay['assignment_id'] == repair['id'] and len(r.calls) == 2


@pytest.mark.parametrize(('given', 'code', 'details'), [
    (None, 'acceptance_results_incomplete', {'checks': 2, 'missing': [1, 2]}),
    (verdicts('met'), 'acceptance_results_incomplete', {'checks': 2, 'missing': [2]}),
    (verdicts('met', 'met') + verdicts('unknown'), 'acceptance_results_invalid',
     {'checks': 2, 'not_found': [], 'repeated': [1]}),
    (verdicts('met', 'met', 'met'), 'acceptance_results_invalid', {'checks': 2, 'not_found': [3], 'repeated': []}),
    (verdicts('met', 'unmet'), 'acceptance_unmet', {'unmet': [2]}),
])
def test_accept_refuses_missing_repeated_foreign_or_unmet_verdicts(run, given, code, details):
    r = run
    row = completed(r, acceptance=CHECKS)
    refused = review(r, row, 'accept', **({} if given is None else {'acceptance_results': given}))
    assert refused['code'] == code and refused['details'] == details, refused
    assert refused['execution_started'] is False and refused['recovery'].startswith('Nothing was recorded')
    assert r.c.repo.read(row['id'])['review'] is None


def test_unmet_work_is_reworked_with_the_same_verdicts(run):
    r = run
    row = completed(r, acceptance=CHECKS)
    judged = verdicts('met', 'unmet')
    assert 'decision=rework' in review(r, row, 'accept', acceptance_results=judged)['recovery']
    receipt = review(r, row, 'rework', acceptance_results=judged)
    assert receipt['decision'] == 'rework' and receipt['acceptance_counts'] == {'met': 1, 'unmet': 1, 'unknown': 0}
    assert r.c.repo.read(row['id'])['review']['acceptanceResults'] == judged


def test_accept_records_unknown_verdicts_and_the_receipt_reports_them_unverified(run):
    r = run
    row = completed(r, acceptance=CHECKS)
    judged = verdicts('met', 'unknown')
    receipt = review(r, row, 'accept', acceptance_results=judged[::-1])
    assert set(receipt) == RECEIPT | {'acceptance_counts'}, receipt
    assert receipt['acceptance_counts'] == {'met': 1, 'unmet': 0, 'unknown': 1}
    assert '1 acceptance check(s) are unknown' in receipt['next_step']
    text = json.dumps(receipt, ensure_ascii=False)
    assert CHECKS[0] not in text and 'observed for check' not in text
    assert r.c.repo.read(row['id'])['review']['acceptanceResults'] == judged
    view = r.call('get_project_assignment', assignment_id=row['id'])
    assert view['request']['acceptance'] == CHECKS
    assert view['review']['acceptanceResults'] == judged
    assert view['review']['acceptanceCounts'] == {'met': 1, 'unmet': 0, 'unknown': 1}
    assert review(r, row, 'accept', acceptance_results=judged) == receipt
    assert 'immutable' in review(r, row, 'accept', acceptance_results=verdicts('met', 'met'))['error']


def test_history_rows_carry_checks_and_count_unjudged_checks_as_unknown(run):
    r = run
    row = completed(r, acceptance=CHECKS)
    assert review(r, row, 'reject')['acceptance_counts'] == {'met': 0, 'unmet': 0, 'unknown': 2}
    page = r.service.history_page(principal=r.c.principal)
    listed = next(view for view in page['assignments'] if view['assignmentId'] == row['id'])
    _validate_schema({**page, 'assignments': [listed]}, 'historyPage')
    assert listed['request']['acceptance'] == CHECKS
    assert listed['review']['acceptanceCounts'] == {'met': 0, 'unmet': 0, 'unknown': 2}
    assert 'acceptanceResults' not in listed['review']


def test_an_owner_authorized_assignment_shows_its_checks_and_an_owner_accept_leaves_them_unknown(
        context, monkeypatch):
    from types import SimpleNamespace
    from lib.project_dispatch import admission
    from lib.project_dispatch.service import ProjectDispatchService
    c = context
    monkeypatch.setattr(admission, 'repository_for', lambda binding: c.repo)
    contents = []

    def create_turn(conversation, owner, payload, **kwargs):
        kwargs['trusted_dispatch'].checked()
        contents.append(payload['inputTurn']['content'])
        kwargs['trusted_dispatch'].bind_execution('worker-turn', 'worker-attempt')
        return SimpleNamespace(value={})
    turn = {'turnId': 'worker-turn', 'conversationId': 'recipient', 'actor': 'assistant',
            'projectionRevision': 4, 'projection': {'content': 'done'}}
    attempt = {'attemptId': 'worker-attempt', 'turnId': 'worker-turn', 'conversationId': 'recipient',
               'taskId': 'worker-task', 'status': 'completed'}
    service = ProjectDispatchService(c.repo, commands=SimpleNamespace(create_turn=create_turn),
                                     turn_get=lambda *args, **kwargs: turn,
                                     attempt_get=lambda *args, **kwargs: attempt)
    row = c.repo.authorize({**c.request, 'acceptance': CHECKS}, c.principal, 'owner-checked')
    assert service.dispatch(row['id'], principal=c.principal)['dispatch'] == 'accepted'
    assert len(contents) == 1 and contents[0].startswith(c.request['brief'])
    assert contents[0].endswith(numbered(CHECKS))
    service.review(row['id'], principal=c.principal, decision='accept', note='Owner accepted',
                   result_revision=4, expected_revision=row['grant']['revision'])
    listed = service.history_page(principal=c.principal)['assignments'][0]
    assert listed['request']['acceptance'] == CHECKS
    assert listed['review']['acceptanceCounts'] == {'met': 0, 'unmet': 0, 'unknown': 2}


def test_work_without_checks_keeps_its_input_review_and_receipt(run, inputs):
    r = run
    row = completed(r, 'plain')
    assert 'acceptance' not in row['request'] and 'Acceptance checks' not in inputs[0]
    refused = review(r, row, 'accept', acceptance_results=verdicts('met'))
    assert refused['code'] == 'acceptance_results_invalid'
    assert refused['error'] == 'This assignment has no acceptance checks'
    receipt = review(r, row, 'accept')
    assert set(receipt) == RECEIPT and receipt['decision'] == 'accept'
    assert r.c.repo.read(row['id'])['review'] == {
        'actor': 'coordinator', 'decision': 'accept', 'note': 'Read the Worker evidence', 'resultRevision': 1}
    view = r.call('get_project_assignment', assignment_id=row['id'])
    assert 'acceptance' not in view['request'] and 'acceptanceCounts' not in view['review']


def test_counts_conserve_every_check():
    for given in ([], verdicts('met'), verdicts('met', 'unmet', 'unknown')):
        assert sum(acceptance.counts(['a', 'b', 'c'], given).values()) == 3
    assert acceptance.counts(['a', 'b'], verdicts('unmet')) == {'met': 0, 'unmet': 1, 'unknown': 1}
