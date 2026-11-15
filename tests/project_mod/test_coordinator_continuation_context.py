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
