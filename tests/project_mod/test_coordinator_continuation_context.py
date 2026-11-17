"""A Coordinator continuation is anchored on its task board and on the Worker states that need it.

Spec for docs/PROJECT_COORDINATION.md "Continuation context is anchored on the task
board": the wake's input carries the open board tasks, the owner's unanswered
questions and only the actionable Worker states; the owner's request is linked,
and quoted only while the board has no open task; settled, cancelled and
delivered assignments are omitted; a normally ended wake with nothing new and
nothing to act on is not repeated, while a failed one still retries.
"""
import pytest

from tests.project_mod.test_coordinator_continuation import (
    run as run, activate, controls, delegate, end)
from tests.project_mod.test_coordinator_result_wake import board as board
from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context
from lib.project_dispatch.delegation import WORKER_INPUT_SOURCE_HEAD

pytestmark = pytest.mark.unit

OWNER_WORDS = 'Please consult'


@pytest.fixture
def wake_inputs(board, monkeypatch):
    r = board
    r.inputs = {}
    create_turn = r.service.commands.create_turn

    def capture(conversation, owner, request, **kwargs):
        r.inputs.setdefault(conversation, []).append(request['inputTurn']['content'])
        return create_turn(conversation, owner, request, **kwargs)

    monkeypatch.setattr(r.service.commands, 'create_turn', capture)
    r.questions = []
    monkeypatch.setattr('lib.router.proactive.questions.open_questions',
                        lambda owner, coordinator_id=None, client=None: list(r.questions))
    return r


def _settle_request_turn(r):
    r.attempts['coordinator-attempt']['status'] = 'completed'
    r.attempts['coordinator-attempt']['settledAt'] = int(r.c.now[0] * 1000)


def test_board_questions_and_only_actionable_workers_anchor_the_wake(wake_inputs):
    r = wake_inputs
    accepted, waiting, revoked = delegate(r, 'accepted'), delegate(r, 'waiting'), delegate(r, 'revoked')
    end(r, accepted); end(r, waiting)
    assert r.call('review_project_work', assignment_id=accepted['id'], result_revision=1,
                  decision='accept', note='Verified')['decision'] == 'accept'
    assert r.call('cancel_project_work', assignment_id=revoked['id'])['revoked']
    r.publish([{'title': 'Ship the leaner continuation', 'status': 'in_progress',
                'progress': 'review pending'},
               {'title': 'Already done', 'status': 'completed'}])
    r.questions = [{'question': 'Restart now?', 'default': 'wait for the host move', 'turnId': 'ask-1'}]
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    content = r.inputs['coordinator'][-1]
    brief = controls(r)[-1]['request']['brief']
    assert content.startswith(brief) and WORKER_INPUT_SOURCE_HEAD + 'coordinator; Turn human-turn' in content
    # The owner's request is linked, not repeated, while the board says what is open.
    assert OWNER_WORDS not in content and 'Original human request' not in content
    assert '[in_progress] Ship the leaner continuation: review pending' in content
    assert 'Already done' not in content
    assert 'Restart now? (your default meanwhile: wait for the host move) [Turn ask-1]' in content
    assert waiting['id'] + ': result to review' in content
    # Settled (reviewed and delivered) and cancelled assignments are not listed.
    assert accepted['id'] not in content and revoked['id'] not in content
    assert 'Current results' not in brief and accepted['id'] not in brief
    assert 'Open outcomes with no running or queued Worker' in content
    assert '- [unowned] Ship the leaner continuation' in content


def test_running_and_queued_workers_own_their_named_outcomes():
    from lib.conversations.coordinator_continuation import unowned_outcomes
    from lib.project_dispatch.continuation import worker_phase
    assert worker_phase({'status': 'queued', 'review': None, 'delivery': None}) == 'running'
    tasks = [{'title': 'Paper A: diagnose the result', 'status': 'in_progress'},
             {'title': 'Paper B: read the pilot', 'status': 'pending'}]
    workers = [{'title': 'Paper A: diagnose the result', 'status': 'running'},
               {'title': 'Own Paper B: read the pilot', 'status': 'queued'}]
    assert unowned_outcomes(tasks, workers) == {'unowned': [], 'dropped': []}


def test_dropped_open_outcome_is_visible_until_owned_or_cancelled(wake_inputs, monkeypatch):
    r = wake_inputs
    worker = delegate(r)
    end(r, worker)
    dropped = {'title': 'Paper B: the missing gate verdict', 'status': 'in_progress', 'key': 'paper-b'}
    monkeypatch.setattr('lib.conversations.coordinator_task_history.CoordinatorTaskHistory.read',
                        lambda self, **kwargs: {'tasks': [dropped], 'nextCursor': None})
    r.publish([{'title': 'Main paper: finish the table', 'status': 'in_progress'}])
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    content = r.inputs['coordinator'][-1]
    assert 'Open outcomes dropped from the board' in content
    assert '- [dropped] Paper B: the missing gate verdict' in content
    assert '- [unowned] Paper B: the missing gate verdict' not in content


def test_a_generic_infrastructure_worker_does_not_own_a_paper(wake_inputs):
    from lib.conversations.coordinator_continuation import unowned_outcomes
    tasks = [{'title': 'Paper B: the missing gate verdict', 'status': 'in_progress'}]
    workers = [{'title': 'Preserve the 910C handoff and write the honest final status'}]
    assert [task['title'] for task in unowned_outcomes(tasks, workers)['unowned']] == [tasks[0]['title']]
    assert unowned_outcomes(tasks, [{'title': 'Own Paper B: read the pilot'}])['unowned'] == []
    other = [{'title': 'Own Paper A: diagnose the result'}]
    assert unowned_outcomes(tasks, other)['unowned'] == tasks
    renamed = [{'title': 'Paper B: a new stage with the same outcome'}]
    assert unowned_outcomes(tasks, renamed)['unowned'] == []


def test_request_turn_history_supplies_a_dropped_outcome_the_head_never_recorded(wake_inputs):
    """A drop is visible even when durable task history does not know the row."""
    r = wake_inputs
    end(r, delegate(r))
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    end(r, controls(r)[-1], delivered=False)
    turn = next(row['execution']['turnId'] for row in r.c.repo.list()
                if row['request']['profile']['mode'] == 'coordinator')
    r.turns[turn]['toolRounds'] = [
        {'toolName': 'update_task_progress', 'tEnd': 1001,
         'toolArgs': {'summary': 'Both papers', 'tasks': [
             {'title': 'Paper B: the missing gate verdict', 'status': 'in_progress'},
             {'title': 'Main paper: the table', 'status': 'in_progress'}]},
         'toolContent': {'changed': True, 'revision': 1}},
        {'toolName': 'update_task_progress', 'tEnd': 1002,
         'toolArgs': {'summary': 'Only the main paper', 'tasks': [
             {'title': 'Main paper: the table', 'status': 'in_progress'}]},
         'toolContent': {'changed': True, 'revision': 2}},
    ]
    from lib.conversations.coordinator_task_history import title_key
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr('lib.conversations.coordinator_task_history.CoordinatorTaskHistory.read',
                        lambda self, **kwargs: {'tasks': [], 'nextCursor': None, 'recorded': True})
    r.publish([{'title': 'Main paper: the table', 'status': 'in_progress'}])
    from lib.conversations.coordinator_continuation import continuation_context
    content = continuation_context(r.c.repo, controls(r)[-1])
    assert '- [dropped] Paper B: the missing gate verdict' in content
    assert '- [dropped] Please consult' not in content
    monkeypatch.undo()


def test_an_owner_request_without_a_board_outcome_is_stated_not_lost(wake_inputs):
    r = wake_inputs
    worker = delegate(r)
    end(r, worker)
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    end(r, controls(r)[-1], delivered=False)
    r.publish([{'title': 'Paper B: the missing gate verdict', 'status': 'in_progress'}])
    from lib.conversations.coordinator_continuation import continuation_context
    content = continuation_context(r.c.repo, controls(r)[-1])
    assert '- [dropped] Please consult' in content
    # An existing running/queued owner of the same request is reused.
    from lib.conversations.coordinator_continuation import unowned_outcomes
    assert unowned_outcomes([], [{'title': 'Please consult'}], [{'title': 'Please consult'}]) == {
        'unowned': [], 'dropped': []}


def test_a_finished_request_is_not_reopened_as_a_stated_outcome(wake_inputs):
    from tests.project_mod.test_coordinator_continuation import report
    r = wake_inputs
    worker = delegate(r)
    end(r, worker)
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    activate(r)
    assert r.call('review_project_work', assignment_id=worker['id'], result_revision=1,
                  decision='accept', note='Verified')['decision'] == 'accept'
    assert r.call('finish_project_request', **report(r))['reason'] == 'complete'
    end(r, controls(r)[-1], delivered=False)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    # The origin's finish receipt is durable: no request text is resurrected.
    from lib.conversations.coordinator_continuation import _stated_outcomes
    requests = [row for row in r.c.repo.list() if row['request']['profile']['mode'] == 'coordinator']
    assert _stated_outcomes(r.c.repo, requests, set()) == []


def test_a_renamed_paper_b_drop_is_not_owned_by_its_running_worker(wake_inputs):
    """The real 12:29 shape: the Worker owns the new row, not the dropped label."""
    from lib.conversations.coordinator_continuation import unowned_outcomes
    current = [{'title': 'Paper B: the missing gate verdict (was dropped)', 'status': 'in_progress'}]
    dropped = [{'title': 'Paper B: cross-harness long-horizon terminal benchmark (was dropped)',
                'status': 'in_progress', 'key': 'ca6a6ec715ff050069e097d76fc6958e'}]
    workers = [{'title': 'Own Paper B: cross-harness long-horizon terminal benchmark'}]
    ownership = unowned_outcomes(current, workers, dropped)
    assert ownership['unowned'] == []
    assert [task['title'] for task in ownership['dropped']] == [dropped[0]['title']]
    # Once the dropped title itself is cancelled or its own Worker names it, it clears.
    assert unowned_outcomes([], [{'title': dropped[0]['title']}], dropped)['dropped'] == []


def test_an_empty_board_quotes_the_owner_request(wake_inputs):
    r = wake_inputs
    end(r, delegate(r))
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    content = r.inputs['coordinator'][-1]
    assert 'Your board has no open task' in content and OWNER_WORDS in content


def test_a_worker_input_carries_its_brief_and_a_source_link_only(wake_inputs):
    r = wake_inputs
    worker = delegate(r)
    content = r.inputs[worker['request']['recipientId']][-1]
    assert content.startswith(worker['request']['brief'] + WORKER_INPUT_SOURCE_HEAD)
    assert OWNER_WORDS not in content and 'get_conversation' in content
    assert 'not a new human authorization' in content


def test_a_failure_already_judged_is_not_listed_again(wake_inputs):
    r = wake_inputs
    end(r, delegate(r, 'old'), 'failed')
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    failed = controls(r)[-1]
    assert 'ended' in r.inputs['coordinator'][-1]
    activate(r)
    newer = delegate(r, 'new')
    r.c.now[0] += 10
    end(r, failed, delivered=False)
    end(r, newer)
    assert r.wake() == ['accepted']
    content = r.inputs['coordinator'][-1]
    assert newer['id'] + ': result to review' in content and ': ended' not in content


def test_nothing_actionable_after_a_normal_turn_wakes_no_repeat(wake_inputs):
    r = wake_inputs
    worker = delegate(r)
    end(r, worker)
    assert r.call('review_project_work', assignment_id=worker['id'], result_revision=1,
                  decision='accept', note='Verified')['decision'] == 'accept'
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    end(r, controls(r)[-1], delivered=False)
    # The turn ended normally with the same evidence and nothing to act on.
    assert r.wake() == [] and not r.scheduled and not r.reports
    assert len(controls(r)) == 1


def test_a_failed_turn_with_nothing_actionable_still_retries(wake_inputs):
    r = wake_inputs
    worker = delegate(r)
    end(r, worker)
    assert r.call('review_project_work', assignment_id=worker['id'], result_revision=1,
                  decision='accept', note='Verified')['decision'] == 'accept'
    _settle_request_turn(r)
    assert r.wake() == ['accepted']
    end(r, controls(r)[-1], 'failed', delivered=False)
    assert r.wake() == [] and r.scheduled, 'a failed Coordinator turn backs off and retries'
