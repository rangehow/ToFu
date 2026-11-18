"""A lead withdraws its own open owner question (docs/ROUTER.md R10, "A lead withdraws a settled question").

The owner-question index stays the one store: withdrawal marks the record and
the card, no listing reports the question again, another conversation's
question is refused, and an owner answer that comes anyway is still delivered
and takes precedence.
"""
from __future__ import annotations

from copy import deepcopy
import json
import threading

import pytest

from lib.router.proactive.questions import (
    NAMESPACE, QuestionNotWithdrawable, answer_lane_questions, open_questions, withdraw_question,
)

pytestmark = pytest.mark.unit

OWNER = 7
LEAD = 'coordinator-lead'


class Store:
    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()

    def __call__(self, **_kwargs):
        return self

    def query(self, operation, payload, **_kwargs):
        if operation == 'record.get':
            return deepcopy(self.rows.get((payload['namespace'], payload['key'])))
        if operation == 'record.list':
            keys = sorted(key for (ns, key) in self.rows
                          if ns == payload['namespace'] and key.startswith(payload['prefix'])
                          and key > payload.get('after_key', ''))
            return [{'key': key, **deepcopy(self.rows[(payload['namespace'], key)])} for key in keys]
        raise ValueError(operation)

    def command(self, operation, payload, command_id):
        identity = (payload['namespace'], payload['key'])
        with self.lock:
            old = self.rows.get(identity)
            expected = payload.get('expected_version', 0)
            if (old['version'] if old else 0) != expected:
                from lib.storage.errors import StorageError
                raise StorageError('database_conflict', 'CAS conflict')
            self.rows[identity] = {'value': deepcopy(payload['value']), 'version': expected + 1}
            return {'version': expected + 1}


class Turns:
    """Settled lead Turns with ``turn.projection.update`` CAS semantics."""

    def __init__(self):
        self.turns = {}
        self.updates = []

    def add(self, turn_id, rounds, *, status='interrupted', ordinal=3):
        self.turns[turn_id] = {'turnId': turn_id, 'status': status, 'ordinal': ordinal, 'laneId': 'main',
                               'projectionRevision': 1, 'projection': {'toolRounds': rounds},
                               'settlement': {'resumeOptions': [
                                   {'operation': 'answer_guidance', 'anchor': {'guidanceId': rounds[0]['guidanceId']}}]}}

    def get(self, conversation_id, turn_id, *, user_id, **_kwargs):
        turn = self.turns.get(turn_id)
        return deepcopy(turn) if conversation_id == LEAD else None

    def update(self, conversation_id, turn_id, *, projection, expected_projection_revision, user_id):
        from lib.turn_lifecycle import LifecycleConflict
        turn = self.turns[turn_id]
        if turn['projectionRevision'] != expected_projection_revision:
            raise LifecycleConflict('stale_projection', 'stale')
        self.updates.append((conversation_id, turn_id, user_id))
        turn['projection'] = deepcopy(projection)
        turn['projectionRevision'] += 1
        return {'conversationRevision': turn['projectionRevision']}


def _record(store, guidance_id, *, coordinator=LEAD, blocking=True, **extra):
    store.rows[(NAMESPACE, f'{OWNER}:{coordinator}:{guidance_id}')] = {'version': 1, 'value': {
        'owner': OWNER, 'coordinatorId': coordinator, 'projectPath': '/p', 'guidanceId': guidance_id,
        'turnId': f'turn-{guidance_id}', 'blocking': blocking, 'question': 'Which scope?', 'createdAt': 1,
        **extra}}


def _card(guidance_id, **extra):
    return {'toolName': 'ask_human', 'toolCallId': 'call-1', 'guidanceId': guidance_id,
            'guidanceQuestion': 'Which scope?', 'status': 'awaiting_human', **extra}


def _reads(turns):
    return dict(turn_get=turns.get, update_projection=turns.update)


def test_the_owning_lead_withdraws_its_open_question_and_no_listing_reports_it():
    store, turns = Store(), Turns()
    _record(store, 'hg_one')
    _record(store, 'hg_other')
    turns.add('turn-hg_one', [_card('hg_one')])
    turns.add('turn-hg_other', [_card('hg_other')])
    assert {q['guidanceId'] for q in open_questions(OWNER, client=store, turn_get=turns.get)} == {
        'hg_one', 'hg_other'}

    record, card = withdraw_question(OWNER, LEAD, 'hg_one', reason='answered in chat on 11-17',
                                     client=store, **_reads(turns))

    assert card == 'card'
    assert record['withdrawnReason'] == 'answered in chat on 11-17' and record['withdrawnAt'] > 0
    assert 'answeredAt' not in record, 'a withdrawal is not an answer: a later answer is still recorded'
    marked = turns.turns['turn-hg_one']
    assert marked['projection']['toolRounds'][0]['guidanceWithdrawn']['reason'] == 'answered in chat on 11-17'
    assert marked['projection']['toolRounds'][0]['status'] == 'awaiting_human', 'the card stays answerable'
    assert marked['settlement']['resumeOptions'][0]['operation'] == 'answer_guidance'
    for listing in (open_questions(OWNER, client=store, turn_get=turns.get),
                    open_questions(OWNER, coordinator_id=LEAD, client=store, turn_get=turns.get)):
        assert [q['guidanceId'] for q in listing] == ['hg_other']
    again, _ = withdraw_question(OWNER, LEAD, 'hg_one', reason='second call', client=store, **_reads(turns))
    assert again['withdrawnReason'] == 'answered in chat on 11-17', 'the first withdrawal stands'
    assert len(turns.updates) == 1


def test_a_stale_card_revision_is_reread_and_marked():
    store, turns = Store(), Turns()
    _record(store, 'hg_one')
    turns.add('turn-hg_one', [_card('hg_one')])
    real_get = turns.get
    raced = []

    def racing_get(*args, **kwargs):
        turn = real_get(*args, **kwargs)
        if not raced:
            raced.append(True)
            turns.turns['turn-hg_one']['projectionRevision'] += 1
        return turn
    withdraw_question(OWNER, LEAD, 'hg_one', reason='settled', client=store,
                      turn_get=racing_get, update_projection=turns.update)
    assert turns.turns['turn-hg_one']['projection']['toolRounds'][0]['guidanceWithdrawn']['reason'] == 'settled'


def test_another_conversations_question_is_refused_and_untouched():
    store, turns = Store(), Turns()
    _record(store, 'hg_theirs', coordinator='coordinator-other')
    turns.add('turn-hg_theirs', [_card('hg_theirs')])
    before = deepcopy(store.rows)
    with pytest.raises(QuestionNotWithdrawable, match='not an owner question of this conversation'):
        withdraw_question(OWNER, LEAD, 'hg_theirs', reason='not mine', client=store, **_reads(turns))
    with pytest.raises(QuestionNotWithdrawable):
        withdraw_question(OWNER + 1, 'coordinator-other', 'hg_theirs', reason='another owner',
                          client=store, **_reads(turns))
    assert store.rows == before and not turns.updates


def test_an_answered_question_is_refused_and_the_answer_is_recorded():
    store, turns = Store(), Turns()
    _record(store, 'hg_one')
    turns.add('turn-hg_one', [_card('hg_one', status='completed', guidanceResponse='papers first')])
    with pytest.raises(QuestionNotWithdrawable, match='already answered'):
        withdraw_question(OWNER, LEAD, 'hg_one', reason='settled', client=store, **_reads(turns))
    value = store.rows[(NAMESPACE, f'{OWNER}:{LEAD}:hg_one')]['value']
    assert value['answeredAt'] > 0 and 'withdrawnAt' not in value and not turns.updates


def test_a_reason_is_required():
    store, turns = Store(), Turns()
    _record(store, 'hg_one')
    with pytest.raises(QuestionNotWithdrawable, match='reason is required'):
        withdraw_question(OWNER, LEAD, 'hg_one', reason='  ', client=store, **_reads(turns))


def test_the_card_of_the_callers_own_running_turn_is_marked_on_its_live_round():
    store, turns = Store(), Turns()
    _record(store, 'hg_one', blocking=False)
    turns.add('turn-hg_one', [_card('hg_one', status='done', guidanceBlocking=False)], status='running')
    live = [{'guidanceId': 'hg_one', 'guidanceBlocking': False}]
    _, card = withdraw_question(OWNER, LEAD, 'hg_one', reason='settled', live_rounds=live,
                                client=store, **_reads(turns))
    assert card == 'live' and live[0]['guidanceWithdrawn']['reason'] == 'settled' and not turns.updates
    _record(store, 'hg_two', blocking=False)
    turns.add('turn-hg_two', [_card('hg_two', status='done', guidanceBlocking=False)], status='running')
    _, card = withdraw_question(OWNER, LEAD, 'hg_two', reason='settled', client=store, **_reads(turns))
    assert card == 'unmarked' and not turns.updates, "another attempt's running Turn is its own writer's"
    assert open_questions(OWNER, client=store, turn_get=turns.get) == []


def test_a_late_chat_answer_to_a_withdrawn_nonblocking_question_is_still_recorded():
    store, turns = Store(), Turns()
    _record(store, 'hg_one', blocking=False, laneId='main', ordinal=3, answersRecorded=True)
    turns.add('turn-hg_one', [_card('hg_one', status='done', guidanceBlocking=False)])
    withdraw_question(OWNER, LEAD, 'hg_one', reason='settled', client=store, **_reads(turns))
    assert answer_lane_questions(OWNER, LEAD, lane_id='main', ordinal=9, client=store) == ['hg_one']
    value = store.rows[(NAMESPACE, f'{OWNER}:{LEAD}:hg_one')]['value']
    assert value['answeredOrdinal'] == 9 and value['withdrawnAt'] > 0


def test_a_late_card_answer_to_a_withdrawn_blocking_question_completes_the_round():
    """The withdrawn card keeps its answer_guidance offer; the owner's answer replaces the withdrawal."""
    from lib.conversation_sync.command_service import _answer_guidance_anchor
    from lib.human_gate_contract import answered_guidance_round, preserve_guidance_answers
    store, turns = Store(), Turns()
    _record(store, 'hg_one')
    turns.add('turn-hg_one', [_card('hg_one')])
    withdraw_question(OWNER, LEAD, 'hg_one', reason='settled', client=store, **_reads(turns))
    turn = turns.get(LEAD, 'turn-hg_one', user_id=OWNER)
    assert _answer_guidance_anchor(turn)['guidanceId'] == 'hg_one'
    [withdrawn] = turn['projection']['toolRounds']
    answered = answered_guidance_round(withdrawn, 'do both', 'cmd-late')
    assert answered['status'] == 'completed' and answered['toolContent'] == 'Human response: do both'
    previous = {'toolRounds': [answered]}
    assert preserve_guidance_answers(previous, turn['projection'])['toolRounds'][0]['guidanceResponse'] == 'do both'


# ── the ask_human ``withdraw`` action, as the task loop admits a model call ──

from lib.tasks_pkg.handlers.misc import _human as handler  # noqa: E402

LEAD_SCOPE = {'conversation_id': LEAD, 'project_path': '/p', 'binding_revision': 1, 'user_id': OWNER}


def _admitted(args):
    from lib.tool_input_repair import ingest_tool_call
    from lib.tools.contracts import adapt_legacy_tool_contract
    from lib.tools.human_guidance import ASK_HUMAN_TOOL
    call = ingest_tool_call(
        {'id': 'call-w', 'function': {'name': 'ask_human', 'arguments': json.dumps(args)}},
        emit_audit=False,
        contract_documents_by_name={'ask_human': adapt_legacy_tool_contract(ASK_HUMAN_TOOL).search_document()})
    assert call.ok and call.contract_error is None, call.parse_error
    return call.fn_args


@pytest.fixture
def lead(monkeypatch):
    import lib.router.proactive.questions as questions
    import lib.tasks_pkg.coordinator_policy as policy
    import lib.turn_lifecycle as lifecycle
    from lib.tasks_pkg.executor import _finalize
    store, turns, scope = Store(), Turns(), {'value': LEAD_SCOPE}
    monkeypatch.setattr(questions, '_client', lambda client=None: store)
    monkeypatch.setattr(lifecycle, 'get_turn', turns.get)
    monkeypatch.setattr(lifecycle, 'update_turn_projection', turns.update)
    monkeypatch.setattr(policy, 'coordinator_scope', lambda task, project_path=None: scope['value'])
    monkeypatch.setattr(_finalize, 'append_event', lambda task, event: None)
    return type('Lead', (), {'store': store, 'turns': turns, 'scope': scope})


def _withdraw(args):
    row = {'toolCallId': 'call-w', 'toolName': 'ask_human', 'query': 'withdraw'}
    task = {'id': 'task-w', 'convId': LEAD, '_turnId': 'turn-now', '_userId': OWNER, 'toolRounds': [],
            'messages': []}
    _, content, _ = handler._handle_ask_human(task, None, 'ask_human', 'call-w', args, 1, row, None, None, False)
    return row, content


def test_a_model_sent_withdraw_is_admitted_and_withdraws_the_leads_question(lead):
    _record(lead.store, 'hg_afcf8744c624')
    lead.turns.add('turn-hg_afcf8744c624', [_card('hg_afcf8744c624')])
    args = _admitted({'withdraw': {'guidance_id': 'hg_afcf8744c624', 'reason': 'Owner answered on 11-17.'}})
    row, content = _withdraw(args)
    assert content.startswith('Withdrew owner question hg_afcf8744c624: Owner answered on 11-17.')
    assert 'his answer still reaches you and overrides' in content
    assert 'guidanceId' not in row, 'the withdrawal receipt is not itself a question card'
    assert open_questions(OWNER, client=lead.store, turn_get=lead.turns.get) == []


def test_withdraw_refuses_another_conversations_question_and_a_task_without_a_lead_role(lead):
    _record(lead.store, 'hg_theirs', coordinator='coordinator-other')
    _, content = _withdraw({'withdraw': {'guidance_id': 'hg_theirs', 'reason': 'not mine'}})
    assert content.startswith('Cannot withdraw hg_theirs:') and 'not an owner question' in content
    lead.scope['value'] = None
    _, content = _withdraw({'withdraw': {'guidance_id': 'hg_theirs', 'reason': 'not mine'}})
    assert content.startswith('Only a bound lead')
    assert 'withdrawnAt' not in lead.store.rows[(NAMESPACE, f'{OWNER}:coordinator-other:hg_theirs')]['value']


def test_a_withdraw_without_a_reason_is_rejected_by_the_contract():
    from lib.tool_input_repair import ingest_tool_call
    from lib.tools.contracts import adapt_legacy_tool_contract
    from lib.tools.human_guidance import ASK_HUMAN_TOOL
    document = adapt_legacy_tool_contract(ASK_HUMAN_TOOL).search_document()
    call = ingest_tool_call(
        {'id': 'c', 'function': {'name': 'ask_human', 'arguments': json.dumps({'withdraw': {'guidance_id': 'hg_x'}})}},
        emit_audit=False, contract_documents_by_name={'ask_human': document})
    assert call.contract_error is not None, 'a withdraw needs its reason'
