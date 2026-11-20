"""The owner settles a non-blocking question on its default from the card (docs/ROUTER.md R10,
"The owner confirms a default").

The lead already proceeds on the default it stated, so a confirmation only
settles the question: the owner-question record and the card say so, every
listing drops it, and nothing reaches the lead (no Turn, steer or model
message). Blocking, answered and withdrawn questions are refused.
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from lib.router.proactive.questions import (
    NAMESPACE, QuestionNotConfirmable, answer_lane_questions, confirm_default, open_questions, withdraw_question,
)
from tests.router.test_owner_question_withdraw import LEAD, OWNER, Store, Turns, _card, _record, _reads

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def pushes(monkeypatch):
    """Every push the confirmation sends; a steer or chat delivery would not be a ``project`` hint."""
    sent = []
    monkeypatch.setattr('lib.agent_core.push.push_event',
                        lambda channel, key, payload, **kwargs: sent.append((channel, payload, kwargs)))
    return sent


def _value(store, guidance_id):
    return store.rows[(NAMESPACE, f'{OWNER}:{LEAD}:{guidance_id}')]['value']


def _nonblocking(store, turns, guidance_id, *, status='interrupted', **extra):
    _record(store, guidance_id, blocking=False, laneId='main', ordinal=3, answersRecorded=True, **extra)
    turns.add(f'turn-{guidance_id}', [_card(guidance_id, status='done', guidanceBlocking=False)], status=status)


def test_confirming_settles_record_and_card_and_sends_nothing_to_the_lead(pushes):
    store, turns = Store(), Turns()
    _nonblocking(store, turns, 'hg_one')
    _nonblocking(store, turns, 'hg_other')
    turns_before = set(turns.turns)
    assert {q['guidanceId'] for q in open_questions(OWNER, client=store, turn_get=turns.get)} == {'hg_one', 'hg_other'}

    record, card = confirm_default(OWNER, LEAD, 'hg_one', client=store, **_reads(turns))

    assert card == 'card'
    assert record['defaultAcceptedBy'] == OWNER and record['defaultAcceptedAt'] > 0
    assert record['answeredAt'] == record['defaultAcceptedAt'] and 'answeredOrdinal' not in record
    assert _value(store, 'hg_one') == record, 'durable: a later reader sees it settled'
    [round_entry] = turns.turns['turn-hg_one']['projection']['toolRounds']
    assert round_entry['guidanceDefaultAccepted'] == {'at': record['defaultAcceptedAt'], 'by': OWNER}
    assert round_entry['status'] == 'done' and 'guidanceResponse' not in round_entry
    assert set(turns.turns) == turns_before, 'no Turn is created'
    assert turns.updates == [(LEAD, 'turn-hg_one', OWNER)], 'one presentation edit of the card, nothing else'
    assert [(channel, payload['kind']) for channel, payload, _ in pushes] == [('project', 'owner_question')]
    for listing in (open_questions(OWNER, client=store, turn_get=turns.get),
                    open_questions(OWNER, coordinator_id=LEAD, client=store, turn_get=turns.get)):
        assert [q['guidanceId'] for q in listing] == ['hg_other']


def test_a_second_confirmation_returns_the_first():
    store, turns = Store(), Turns()
    _nonblocking(store, turns, 'hg_one')
    first, _ = confirm_default(OWNER, LEAD, 'hg_one', client=store, **_reads(turns))
    version = store.rows[(NAMESPACE, f'{OWNER}:{LEAD}:hg_one')]['version']
    again, card = confirm_default(OWNER, LEAD, 'hg_one', client=store, **_reads(turns))
    assert again == first and card == 'card'
    assert store.rows[(NAMESPACE, f'{OWNER}:{LEAD}:hg_one')]['version'] == version
    assert len(turns.updates) == 1


@pytest.mark.parametrize('setup, refusal', [
    (lambda store, turns: (_record(store, 'hg_one'), turns.add('turn-hg_one', [_card('hg_one')])), 'is blocking'),
    (lambda store, turns: _nonblocking(store, turns, 'hg_one', answeredAt=5, answeredOrdinal=4),
     'already answered in the question index; '
     'index status alone is not evidence of an explicit owner choice or approval'),
    (lambda store, turns: _nonblocking(store, turns, 'hg_one', withdrawnAt=5, withdrawnReason='moot'),
     'withdrawn by its lead: moot'),
])
def test_blocking_answered_and_withdrawn_questions_are_refused_untouched(setup, refusal):
    store, turns = Store(), Turns()
    setup(store, turns)
    before = deepcopy(store.rows), deepcopy(turns.turns)
    with pytest.raises(QuestionNotConfirmable, match=refusal):
        confirm_default(OWNER, LEAD, 'hg_one', client=store, **_reads(turns))
    assert (store.rows, turns.turns) == before and not turns.updates


def test_a_card_answer_found_is_recorded_and_refuses_the_confirmation():
    store, turns = Store(), Turns()
    _record(store, 'hg_one', blocking=False)
    turns.add('turn-hg_one', [_card('hg_one', status='done', guidanceBlocking=False, guidanceResponse='both')])
    with pytest.raises(QuestionNotConfirmable, match='already answered on its card'):
        confirm_default(OWNER, LEAD, 'hg_one', client=store, **_reads(turns))
    value = _value(store, 'hg_one')
    assert value['answeredAt'] > 0 and 'defaultAcceptedAt' not in value and not turns.updates


def test_another_conversations_or_owners_question_is_refused():
    store, turns = Store(), Turns()
    _record(store, 'hg_theirs', coordinator='coordinator-other', blocking=False)
    with pytest.raises(QuestionNotConfirmable, match='not an owner question'):
        confirm_default(OWNER, LEAD, 'hg_theirs', client=store, **_reads(turns))
    with pytest.raises(QuestionNotConfirmable, match='not an owner question'):
        confirm_default(OWNER + 1, 'coordinator-other', 'hg_theirs', client=store, **_reads(turns))


def test_a_running_question_turn_is_left_to_its_attempt_and_the_record_still_settles_it():
    store, turns = Store(), Turns()
    _nonblocking(store, turns, 'hg_one', status='running')
    record, card = confirm_default(OWNER, LEAD, 'hg_one', client=store, **_reads(turns))
    assert card == 'unmarked' and not turns.updates and record['defaultAcceptedAt'] > 0
    assert open_questions(OWNER, client=store, turn_get=turns.get) == []


def test_a_later_chat_message_does_not_rewrite_the_confirmed_default_and_withdrawal_is_refused():
    store, turns = Store(), Turns()
    _nonblocking(store, turns, 'hg_one')
    record, _ = confirm_default(OWNER, LEAD, 'hg_one', client=store, **_reads(turns))
    assert answer_lane_questions(OWNER, LEAD, lane_id='main', ordinal=9, client=store) == []
    assert _value(store, 'hg_one') == record
    from lib.router.proactive.questions import QuestionNotWithdrawable
    with pytest.raises(QuestionNotWithdrawable, match='already answered'):
        withdraw_question(OWNER, LEAD, 'hg_one', reason='moot', client=store, **_reads(turns))
