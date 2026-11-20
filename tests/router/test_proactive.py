"""Specs for the proactive question ledger, quiet-hours hold and digest.

docs/ROUTER.md R10: owner questions are indexed at the ask_human seam, answered
questions are detected from durable evidence, quiet-hours relays are held and
released as one message, and the digest is deterministic (no model call).
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime
import json
from types import SimpleNamespace
import threading

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True, scope='module')
def router_modules_stay_unpatched():
    """Each spec's fakes are scoped to it: later tests in this process see the real modules."""
    import lib.channels.outbox as outbox_module
    import lib.router.binding as binding_module
    import lib.router.home as home_module
    import lib.router.projects as projects_module
    import lib.router.proactive.digest as digest_module
    import lib.router.proactive.questions as questions_module
    import lib.router.relay as relay_module
    import lib.router.watchdog.binding as watchdog_binding_module
    attributes = ((binding_module, 'global_router'), (relay_module, '_command_chat'),
                  (home_module, 'router_home_path'),
                  (questions_module, 'open_questions'), (projects_module, 'router_projects'),
                  (watchdog_binding_module, 'router_watchdog'), (digest_module, 'assemble_digest'),
                  (digest_module, '_push_command_chats'), (outbox_module, 'enqueue'))
    real = [getattr(module, name) for module, name in attributes]
    yield
    leaked = [f'{module.__name__}.{name}' for (module, name), value in zip(attributes, real)
              if getattr(module, name) is not value]
    assert not leaked, f'a proactive spec leaked fakes into the rest of the test process: {leaked}'


class Store:
    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()

    def __call__(self, **kwargs):
        return self

    def query(self, operation, payload, **kwargs):
        if operation == 'record.get':
            return deepcopy(self.rows.get((payload['namespace'], payload['key'])))
        if operation == 'record.list':
            prefix = payload['prefix']
            after = payload.get('after_key', '')
            limit = payload.get('limit', 1000)
            matching = sorted(
                (key for (ns, key) in self.rows
                 if ns == payload['namespace'] and key.startswith(prefix) and key > after),
            )
            return [{'key': key, **deepcopy(self.rows[(payload['namespace'], key)])}
                    for key in matching[:limit]]
        raise ValueError(f'unknown operation {operation}')

    def command(self, operation, payload, command_id):
        identity = (payload['namespace'], payload['key'])
        with self.lock:
            old = self.rows.get(identity)
            expected = payload.get('expected_version', 0)
            if (old['version'] if old else 0) != expected:
                from lib.storage.errors import StorageError
                raise StorageError('database_conflict', 'CAS conflict')
            if operation == 'record.delete':
                self.rows.pop(identity, None)
                return {'deleted': True}
            self.rows[identity] = {'value': deepcopy(payload['value']), 'version': expected + 1}
            return {'version': expected + 1}


def _unreadable(*_args, **_kwargs):
    return None


def test_record_and_read_open_questions():
    from lib.router.proactive.questions import record_question, open_questions
    store = Store()
    question = {'projectPath': '/project', 'coordinatorId': 'coordinator', 'turnId': 'turn-1',
                'toolCallId': 'tc-1', 'guidanceId': 'hg-1', 'blocking': True,
                'question': 'Should we restart?', 'options': []}
    record_question(7, question, client=store, turn_meta=_unreadable)
    # Idempotent: recording again returns the same record.
    record_question(7, question, client=store, turn_meta=_unreadable)
    open_qs = open_questions(7, client=store, turn_get=lambda *a, **kw: None)
    assert len(open_qs) == 1
    assert open_qs[0]['guidanceId'] == 'hg-1'
    assert open_qs[0]['blocking'] is True


def test_answered_blocking_question_is_not_open():
    from lib.router.proactive.questions import record_question, open_questions
    store = Store()
    question = {'projectPath': '/project', 'coordinatorId': 'coordinator', 'turnId': 'turn-1',
                'toolCallId': 'tc-1', 'guidanceId': 'hg-1', 'blocking': True,
                'question': 'Should we restart?', 'options': []}
    record_question(7, question, client=store, turn_meta=_unreadable)
    # The card has a response.
    turn = {'projection': {'toolRounds': [
        {'guidanceId': 'hg-1', 'guidanceResponse': 'yes', 'guidanceAnswerCommandId': 'cmd-1'}]}}
    open_qs = open_questions(7, client=store, turn_get=lambda *a, **kw: turn)
    assert open_qs == []


def test_an_unrecorded_nonblocking_question_is_decided_once_and_recorded():
    """A question indexed before answers were recorded (its Turn unreadable at record time)."""
    from lib.router.proactive.questions import LaneScans, NAMESPACE, record_question, open_questions
    store = Store()
    question = {'projectPath': '/project', 'coordinatorId': 'coordinator', 'turnId': 'turn-1',
                'toolCallId': 'tc-1', 'guidanceId': 'hg-1', 'blocking': False,
                'question': 'Should we restart?', 'options': []}
    record_question(7, question, client=store, turn_meta=_unreadable)
    # A human Turn after the question's Turn answers it.
    turn = {'ordinal': 5, 'projection': {'toolRounds': []}}
    lane = [({'turnId': 'turn-2', 'ordinal': 6, 'actor': 'human'}, None),
            ({'turnId': 'turn-1', 'ordinal': 5, 'actor': 'assistant'}, None)]
    open_qs = open_questions(7, client=store, turn_get=lambda *a, **kw: turn,
                             lane_turns=lambda *a, **kw: iter(lane), scans=LaneScans())
    assert open_qs == []
    # The answer is recorded once, so the next read needs no Turn at all.
    recorded = store.rows[(NAMESPACE, '7:coordinator:hg-1')]['value']
    assert recorded['answeredAt'] > 0 and recorded['answeredOrdinal'] == 6
    assert (recorded['laneId'], recorded['ordinal']) == ('main', 5)
    assert open_questions(7, client=store, turn_get=lambda *a, **kw: pytest.fail('read again'),
                          lane_turns=lambda *a, **kw: pytest.fail('walked again')) == []


def test_an_open_nonblocking_question_is_recorded_open_and_then_costs_no_read():
    from lib.router.proactive.questions import LaneScans, NAMESPACE, open_questions
    store = Store()
    store.rows[(NAMESPACE, '7:lead:hg-1')] = {'version': 1, 'value': {
        'owner': 7, 'coordinatorId': 'lead', 'projectPath': '/p', 'guidanceId': 'hg-1', 'turnId': 't5',
        'blocking': False, 'question': '?', 'createdAt': 1}}
    lane = SyntheticLane()
    for index in range(8):
        lane.append('human' if index == 2 else 'assistant')
    reads = dict(turn_get=lane.turn, lane_turns=lane.walk, turn_meta=lane.meta, scans=LaneScans())
    assert [row['guidanceId'] for row in open_questions(7, client=store, **reads)] == ['hg-1']
    assert lane.reads['page'] == 4, 'one read of the lane, down to the question at ordinal 4'
    assert store.rows[(NAMESPACE, '7:lead:hg-1')]['value']['answersRecorded'] is True
    never = dict(turn_get=lambda *a, **kw: pytest.fail('card read'),
                 lane_turns=lambda *a, **kw: pytest.fail('lane read'), turn_meta=lane.meta)
    [still_open] = open_questions(7, client=store, **never)
    assert (still_open['guidanceId'], still_open['ordinal']) == ('hg-1', 4)


def test_a_human_turn_records_itself_as_the_answer_of_the_open_nonblocking_questions_before_it():
    from lib.router.proactive.questions import NAMESPACE, answer_lane_questions, open_questions
    store = Store()

    def put(guidance, **value):
        store.rows[(NAMESPACE, f'7:lead:{guidance}')] = {'version': 1, 'value': {
            'owner': 7, 'coordinatorId': 'lead', 'projectPath': '/p', 'guidanceId': guidance,
            'turnId': 't-' + guidance, 'question': '?', 'createdAt': 1, **value}}
    put('before', blocking=False, laneId='main', ordinal=4, answersRecorded=True)
    put('after', blocking=False, laneId='main', ordinal=9, answersRecorded=True)
    put('other-lane', blocking=False, laneId='branch', ordinal=2, answersRecorded=True)
    put('blocking', blocking=True, laneId='main', ordinal=3)
    put('unpositioned', blocking=False)
    put('done', blocking=False, laneId='main', ordinal=1, answersRecorded=True, answeredAt=5, answeredOrdinal=2)
    assert answer_lane_questions(7, 'lead', lane_id='main', ordinal=7, client=store) == ['before']
    assert store.rows[(NAMESPACE, '7:lead:before')]['value']['answeredOrdinal'] == 7
    assert store.rows[(NAMESPACE, '7:lead:done')]['value']['answeredOrdinal'] == 2, 'an answer is permanent'
    assert answer_lane_questions(7, 'lead', lane_id='main', ordinal=8, client=store) == []
    assert answer_lane_questions(7, 'elsewhere', lane_id='main', ordinal=99, client=store) == []
    listed = open_questions(7, client=store, turn_get=_unreadable,
                            lane_turns=lambda *a, **kw: pytest.fail('lane read'))
    assert sorted(row['guidanceId'] for row in listed) == ['after', 'blocking', 'other-lane', 'unpositioned']


def test_record_question_decides_a_nonblocking_question_from_its_lanes_head():
    """While it asks, the question's Turn is the lane head: one short read, then answers record themselves.
    A human Turn that entered before the record existed is met by that read."""
    from lib.router.proactive.questions import LaneScans, NAMESPACE, record_question
    lane = SyntheticLane()
    for _ in range(5):
        lane.append('assistant')
    asking = lane.append('assistant')
    store, scans = Store(), LaneScans()
    question = {'projectPath': '/p', 'coordinatorId': 'lead', 'turnId': asking['turnId'], 'guidanceId': 'hg-1',
                'blocking': False, 'question': '?'}
    record_question(7, question, client=store, turn_meta=lane.meta, lane_turns=lane.walk, scans=scans)
    recorded = store.rows[(NAMESPACE, '7:lead:hg-1')]['value']
    assert (recorded['laneId'], recorded['ordinal'], recorded['answersRecorded']) == ('main', 5, True)
    assert lane.reads['page'] == 1
    raced = lane.append('human')  # appended past every lane check, before the index write
    late = {**question, 'turnId': asking['turnId'], 'guidanceId': 'hg-2'}
    record_question(7, late, client=store, turn_meta=lane.meta, lane_turns=lane.walk, scans=scans)
    recorded = store.rows[(NAMESPACE, '7:lead:hg-2')]['value']
    assert recorded['answeredOrdinal'] == raced['ordinal'] and 'answersRecorded' not in recorded


def test_a_recorded_answer_is_permanent_and_never_read_again():
    from lib.router.proactive.questions import LaneScans, NAMESPACE, answer_lane_questions, open_questions
    lane = SyntheticLane()
    for _ in range(3):
        lane.append('assistant')
    store = Store()
    store.rows[(NAMESPACE, '7:lead:hg-1')] = {'version': 1, 'value': {
        'owner': 7, 'coordinatorId': 'lead', 'projectPath': '/p', 'guidanceId': 'hg-1', 'turnId': 't2',
        'blocking': False, 'question': '?', 'createdAt': 1, 'laneId': 'main', 'ordinal': 2,
        'answersRecorded': True}}
    answer = lane.append('human')
    answer_lane_questions(7, 'lead', lane_id='main', ordinal=answer['ordinal'], client=store)
    lane.delete(answer['turnId'])
    assert open_questions(7, client=store, turn_get=lane.turn, lane_turns=lane.walk, turn_meta=lane.meta,
                          scans=LaneScans()) == []
    assert sum(lane.reads.values()) == 0


def test_a_failed_index_write_leaves_the_question_to_a_later_reader(monkeypatch):
    from lib.router.proactive.questions import LaneScans, NAMESPACE, open_questions
    from lib.storage.errors import StorageError
    store = Store()
    store.rows[(NAMESPACE, '7:lead:hg-1')] = {'version': 1, 'value': {
        'owner': 7, 'coordinatorId': 'lead', 'projectPath': '/p', 'guidanceId': 'hg-1', 'turnId': 't1',
        'blocking': False, 'question': '?', 'createdAt': 1}}
    lane = SyntheticLane()
    lane.append('assistant')
    lane.append('assistant')
    reads = dict(turn_get=lane.turn, lane_turns=lane.walk, turn_meta=lane.meta, scans=LaneScans())
    write = store.command

    def unavailable(operation, payload, command_id):
        raise StorageError('database_unavailable', 'storage authority is unavailable')
    monkeypatch.setattr(store, 'command', unavailable)
    assert [row['guidanceId'] for row in open_questions(7, client=store, **reads)] == ['hg-1']
    assert 'laneId' not in store.rows[(NAMESPACE, '7:lead:hg-1')]['value']
    monkeypatch.setattr(store, 'command', write)
    lane.append('human')
    assert open_questions(7, client=store, **reads) == [], 'the next reader decides it'
    assert store.rows[(NAMESPACE, '7:lead:hg-1')]['value']['answeredOrdinal'] == 2


class SyntheticLane:
    """One lane with the authorities' ordinal rule: a new Turn takes MAX(ordinal) + 1."""

    def __init__(self):
        self.rows, self.created, self.reads = [], 0, Counter()

    def append(self, actor):
        self.created += 1
        row = {'turnId': f't{self.created}', 'ordinal': self.rows[-1]['ordinal'] + 1 if self.rows else 0,
               'actor': actor, 'laneId': 'main'}
        self.rows.append(row)
        return row

    def insert_summary(self):
        """A compaction summary in the first ordinal gap, as turn.compact places it."""
        taken = {row['ordinal'] for row in self.rows}
        gap = next((ordinal for ordinal in range(len(taken) + 1) if ordinal not in taken), None)
        if gap is not None and self.rows and gap < self.rows[-1]['ordinal']:
            self.created += 1
            self.rows.append({'turnId': f't{self.created}', 'ordinal': gap, 'actor': 'assistant',
                              'laneId': 'main'})
            self.rows.sort(key=lambda row: row['ordinal'])

    def delete(self, turn_id):
        self.rows = [row for row in self.rows if row['turnId'] != turn_id]

    def walk(self, conversation_id, lane_id, *, user_id, before_ordinal=None):
        for row in reversed(self.rows):
            if before_ordinal is None or row['ordinal'] < before_ordinal:
                self.reads['page'] += 1
                yield dict(row), None

    def meta(self, conversation_id, turn_id, *, user_id):
        self.reads['meta'] += 1
        return next((dict(row) for row in self.rows if row['turnId'] == turn_id), None)

    def turn(self, conversation_id, turn_id, *, user_id):
        row = next((row for row in self.rows if row['turnId'] == turn_id), None)
        if row is None:
            raise LookupError('Turn not found')
        return {**row, 'projection': {'toolRounds': []}}

    def answered_today(self, question):
        """Today's rule: the question's Turn exists and a human Turn follows it in the lane."""
        own = next((row for row in self.rows if row['turnId'] == question['turnId']), None)
        return own is not None and any(row['actor'] == 'human' and row['ordinal'] > own['ordinal']
                                       for row in self.rows)


def _decided_answered(lane, scans, question):
    """Whether a listing that meets ``question`` unrecorded decides it answered.

    Each check starts from a fresh index record, as one indexed before answers were
    recorded, so only the lane read and its LaneScans memo decide it.
    """
    from lib.router.proactive.questions import NAMESPACE, open_questions
    store = Store()
    store.rows[(NAMESPACE, f"7:{question['coordinatorId']}:{question['guidanceId']}")] = {
        'version': 1, 'value': {'owner': 7, 'projectPath': '/p', 'question': '?', 'createdAt': 1, **question}}
    return not open_questions(7, client=store, turn_get=lane.turn, lane_turns=lane.walk, turn_meta=lane.meta,
                              scans=scans)


def test_lane_memo_answers_exactly_like_a_full_walk_through_appends_deletions_and_summaries():
    """Every check reuses one LaneScans; the answers equal today's rule after each change,
    including a deleted tail whose ordinal a new Turn takes again and a deleted owner answer."""
    import random
    from lib.router.proactive.questions import LaneScans
    rng = random.Random(20261113)
    for _ in range(40):
        lane, scans, questions = SyntheticLane(), LaneScans(), []
        for step in range(120):
            roll = rng.random()
            if roll < 0.45:
                lane.append('assistant')
            elif roll < 0.6:
                row = lane.append('assistant')
                questions.append({'coordinatorId': 'lead', 'turnId': row['turnId'],
                                  'guidanceId': f'hg-{row["turnId"]}', 'blocking': False})
            elif roll < 0.75:
                lane.append('human')
            elif roll < 0.9 and lane.rows:
                lane.delete(rng.choice(lane.rows[-3:] if rng.random() < 0.6 else lane.rows)['turnId'])
            else:
                lane.insert_summary()
            for question in rng.sample(questions, min(len(questions), 4)):
                assert _decided_answered(lane, scans, question) == lane.answered_today(question), (
                    step, question, lane.rows)


def test_lane_memo_reads_each_turn_once_and_resumes_below_its_floor():
    from lib.router.proactive.questions import LaneScans
    lane, scans = SyntheticLane(), LaneScans()
    ask = {}
    for index in range(300):
        row = lane.append('human' if index == 100 else 'assistant')
        if index in (50, 150, 250):
            ask[index] = {'coordinatorId': 'lead', 'turnId': row['turnId'], 'guidanceId': f'hg-{index}',
                          'blocking': False}

    def answered(index):
        return _decided_answered(lane, scans, ask[index])
    # Newest question first: open, read down to its own Turn.
    assert answered(250) is False
    assert lane.reads['page'] == 50
    # An older open question resumes below the first walk's floor: nothing is read twice.
    assert answered(150) is False
    assert lane.reads['page'] == 50 + 1 + 100
    # The oldest is answered by the owner's message at 100; the next check is one tail page
    # and one point read of that message.
    assert answered(50) is True
    assert lane.reads['page'] == 50 + 1 + 100 + 1 + 50
    lane.reads.clear()
    assert answered(50) is True
    assert dict(lane.reads) == {'page': 1, 'meta': 1}
    lane.reads.clear()
    assert [answered(index) for index in (250, 150)] == [False, False]
    assert dict(lane.reads) == {'page': 2}
    # New Turns cost one read each; with the owner's message gone, a fresh record reads as open.
    for _ in range(10):
        lane.append('assistant')
    lane.reads.clear()
    assert answered(250) is False
    assert lane.reads['page'] == 11
    lane.delete(next(row['turnId'] for row in lane.rows if row['actor'] == 'human'))
    assert answered(50) is False


def test_quiet_hours_detection():
    from datetime import datetime
    from lib.router.proactive.held import quiet_hours_active, quiet_end
    assert quiet_hours_active(datetime(2026, 11, 8, 2, 0)) is True
    assert quiet_hours_active(datetime(2026, 11, 8, 7, 59)) is True
    assert quiet_hours_active(datetime(2026, 11, 8, 8, 0)) is False
    assert quiet_hours_active(datetime(2026, 11, 8, 12, 0)) is False
    end = quiet_end(datetime(2026, 11, 8, 2, 0))
    assert end.hour == 8 and end.minute == 0 and end.day == 8
    end = quiet_end(datetime(2026, 11, 8, 9, 0))
    assert end.day == 9


def test_hold_and_release_relays(monkeypatch):
    from lib.router.proactive.held import hold_relay, held_relays, release_held
    store = Store()
    payload = {'kind': 'progress', 'title': 'Restart done', 'message': 'Verified.'}
    hold_relay(7, 'report', 'cmd-1', payload, client=store)
    # Idempotent: holding again returns the same record.
    _, created = hold_relay(7, 'report', 'cmd-1', payload, client=store)
    assert created is False
    rows = held_relays(7, client=store)
    assert len(rows) == 1
    assert rows[0]['payload']['title'] == 'Restart done'
    # Release: held records are deleted, a Router notice is dispatched.
    sent = []
    dispatched = []
    # The release imports from lib.router.relay; patch it there.
    monkeypatch.setattr('lib.router.relay._command_chat', lambda owner, client: None)
    monkeypatch.setattr('lib.router.binding.global_router', lambda owner, **kw: {
        'availability': 'available', 'conversationId': 'router-conv', 'revision': 1})
    def fake_dispatch(conv, message, tools, **kwargs):
        dispatched.append((conv, message, kwargs))
        return SimpleNamespace(disposition='started')
    count, summary = release_held(7, client=store, dispatch=fake_dispatch)
    assert count == 1
    assert '1 report' in summary or '1 held' in summary
    assert held_relays(7, client=store) == []
    assert len(dispatched) == 1
    assert dispatched[0][0] == 'router-conv'
    assert dispatched[0][2]['inject_mode'] == 'steer'


def test_digest_assembles_open_decisions_and_skips_when_empty(monkeypatch):
    from lib.router.proactive.digest import assemble_digest
    store = Store()
    # Empty: no questions, no commitments, no faults.
    monkeypatch.setattr('lib.router.proactive.questions.open_questions', lambda owner, **kw: [])
    monkeypatch.setattr('lib.router.projects.router_projects',
                        lambda owner, args, **kw: {'projects': [], 'next_cursor': None})
    monkeypatch.setattr('lib.router.watchdog.binding.router_watchdog',
                        lambda owner, **kw: {'availability': 'unavailable'})
    text, counts = assemble_digest(7, client=store)
    assert text is None
    # With one open question:
    monkeypatch.setattr('lib.router.proactive.questions.open_questions', lambda owner, **kw: [
        {'guidanceId': 'hg-1', 'question': 'Should we restart?', 'blocking': True,
         'createdAt': 0, 'default': ''}])
    text, counts = assemble_digest(7, client=store)
    assert text is not None
    assert 'Should we restart?' in text
    assert counts['questions'] == 1
    assert 'blocking' in text.lower() or '⏸' in text


def test_digest_faults_name_the_error_and_group_repeats():
    from lib.router.proactive.digest import _fault_section
    signals = [
        {'source': 'turn', 'subject': {'conversationId': 'c1', 'role': 'project_router'},
         'error': {'kind': 'permission', 'detail': 'API HTTP 401: App ID disabled'}},
        {'source': 'turn', 'subject': {'conversationId': 'c2', 'role': 'worker'},
         'error': {'kind': 'permission', 'detail': 'API HTTP 401: App ID disabled'}},
        {'source': 'turn', 'subject': {'conversationId': 'c1', 'role': 'project_router'},
         'error': {'kind': 'internal', 'detail': 'compaction failed at 4.7M tokens'}},
    ]
    text = _fault_section(signals)
    assert '**3 open fault(s), 2 group(s):**' in text
    assert '- turn ×2 (permission, 2 conversation(s)): API HTTP 401: App ID disabled' in text
    assert '- turn ×1 (internal): compaction failed at 4.7M tokens' in text
    assert not any(line.endswith('turn: ') for line in text.splitlines())


def test_fault_signals_iterate_every_open_page():
    from lib.router.watchdog.signals import FaultSignals
    signals = FaultSignals(7, client=Store())
    for i in range(40):
        signals.record('turn', [f'c{i}', 't', 'a'],
                       {'role': 'worker', 'conversationId': f'c{i}'},
                       {'kind': 'permission', 'detail': 'x'})
    assert len(list(signals.iter_open())) == 40


def test_digest_counts_every_open_fault_not_only_one_page(monkeypatch):
    from lib.router.proactive.digest import assemble_digest
    signals = [
        {'source': 'turn', 'subject': {'conversationId': f'c{i}', 'role': 'worker'},
         'error': {'kind': 'permission', 'detail': 'API HTTP 401'}}
        for i in range(41)
    ]

    class FakeFaultSignals:
        def __init__(self, owner, *, client=None):
            pass

        def iter_open(self):
            return iter(signals)

    monkeypatch.setattr('lib.router.watchdog.signals.FaultSignals', FakeFaultSignals)
    monkeypatch.setattr('lib.router.watchdog.binding.router_watchdog',
                        lambda owner, **kw: {'availability': 'available'})
    monkeypatch.setattr('lib.router.proactive.questions.open_questions', lambda owner, **kw: [])
    monkeypatch.setattr('lib.router.projects.router_projects',
                        lambda owner, args, **kw: {'projects': []})
    text, counts = assemble_digest(7, client=Store())
    assert '**41 open fault(s), 1 group(s):**' in text
    assert '- turn ×41 (permission, 41 conversation(s)): API HTTP 401' in text
    assert counts['faults'] == 41


def test_digest_delivery_reaches_every_enabled_command_chat(monkeypatch):
    from lib.router.proactive.digest import run_digest
    monkeypatch.setattr('lib.router.proactive.digest.assemble_digest',
                        lambda owner, **kw: ('Test digest text', {'questions': 1}))
    sent = []
    monkeypatch.setattr('lib.router.proactive.digest._push_command_chats',
                        lambda owner, client: [('dx', '大象', 'dx:gid:12345'),
                                               ('wechat', '微信', 'wechat:uid:abc')])
    monkeypatch.setattr('lib.channels.outbox.enqueue',
                        lambda owner, thread, text, **kw: sent.append((thread, text, kw)))
    ok, result = run_digest({'user_id': 7}, now=datetime(2026, 11, 19, 21, 0))
    assert ok, result
    assert [item[0] for item in sent] == ['dx:gid:12345', 'wechat:uid:abc']
    assert all(item[1] == 'Test digest text' for item in sent)
    assert [item[2]['idempotency_key'] for item in sent] == [
        'digest:7:2026-11-19:dx', 'digest:7:2026-11-19:wechat']
    assert '大象' in result and '微信' in result


def test_digest_one_failing_channel_does_not_block_another(monkeypatch):
    from lib.router.proactive.digest import run_digest
    monkeypatch.setattr('lib.router.proactive.digest.assemble_digest',
                        lambda owner, **kw: ('Test digest text', {'questions': 1}))
    monkeypatch.setattr('lib.router.proactive.digest._push_command_chats',
                        lambda owner, client: [('dx', '大象', 'dx:gid:12345'),
                                               ('wechat', '微信', 'wechat:uid:abc')])
    sent = []

    def enqueue(owner, thread, text, **kw):
        if thread.startswith('dx:'):
            raise RuntimeError('dx unavailable')
        sent.append((thread, text, kw))

    monkeypatch.setattr('lib.channels.outbox.enqueue', enqueue)
    ok, result = run_digest({'user_id': 7})
    assert ok, result
    assert [item[0] for item in sent] == ['wechat:uid:abc']
    assert '微信' in result and 'failed: 大象' in result


def test_digest_falls_back_in_app_when_no_command_chat_is_enabled(monkeypatch):
    from lib.router.proactive.digest import run_digest
    monkeypatch.setattr('lib.router.proactive.digest.assemble_digest',
                        lambda owner, **kw: ('Test digest text', {'questions': 1}))
    monkeypatch.setattr('lib.router.proactive.digest._push_command_chats',
                        lambda owner, client: [])
    monkeypatch.setattr('lib.router.binding.global_router',
                        lambda owner, **kw: {'availability': 'available',
                                             'conversationId': 'router-conv', 'revision': 1})
    monkeypatch.setattr('lib.router.home.router_home_path', lambda owner: '/router-home')
    published = []
    monkeypatch.setattr(
        'lib.conversations.coordinator_reports.CoordinatorReports.publish',
        lambda self, scope, report: published.append((scope, report)))
    ok, result = run_digest({'user_id': 7})
    assert ok, result
    assert 'in-app' in result
    assert published[0][1]['message'] == 'Test digest text'


def test_digest_push_channels_are_enabled_accounts_with_a_command_chat(monkeypatch):
    from lib.router.proactive.digest import _push_command_chats
    from lib.channels.model import NAMESPACE
    store = Store()

    def account(channel, *, enabled, identity, chat):
        store.rows[(NAMESPACE, f'u7/account/{channel}')] = {
            'value': {'channel': channel, 'enabled': enabled, 'identity': identity,
                      'commandChat': chat, 'status': {}},
            'version': 1}

    account('dx', enabled=True, identity={'uid': '1'}, chat='dx:gid:1')
    account('wechat', enabled=True, identity={'uid': '2'}, chat='wechat:uid:2')
    account('qq', enabled=False, identity={'uid': '3'}, chat='qq:gid:3')
    account('github', enabled=True, identity={'uid': '4'}, chat='')
    assert _push_command_chats(7, store) == [('dx', '大象', 'dx:gid:1'),
                                             ('wechat', '微信', 'wechat:uid:2')]


def test_the_turn_lifecycle_records_answers_for_human_turns_entering_a_lane_only(monkeypatch):
    """Activation moves a queued human Turn into its lane; a still-queued pair is not in the lane yet;
    another actor answers nothing, and a failed index write never fails the Turn."""
    from lib import turn_lifecycle
    import lib.router.proactive.questions as questions
    calls, results = [], {}

    class Client:
        def command(self, operation, payload, command_id=None, **_kwargs):
            return deepcopy(results[operation])
    monkeypatch.setattr(turn_lifecycle, '_turn_client', lambda **_kwargs: Client())
    monkeypatch.setattr(questions, 'answer_lane_questions',
                        lambda owner, conversation, *, lane_id, ordinal: calls.append(
                            (owner, conversation, lane_id, ordinal)))
    human = {'turnId': 'in-1', 'conversationId': 'lead', 'laneId': 'main', 'ordinal': 12, 'actor': 'human'}
    results['turn.queue.activate'] = {'submittedTurn': human, 'turn': {}, 'queued': False}
    turn_lifecycle.activate_queued_turn_pair('lead', 'q-1', user_id=7)
    assert calls == [(7, 'lead', 'main', 12)]
    results['turn.create_pair'] = {'submittedTurn': {**human, 'laneId': 'queue'}, 'turn': {}, 'queued': True}
    turn_lifecycle.create_turn_pair('lead', command_id='c-1', input_projection={'content': 'x'}, config={},
                                    user_id=7)
    results['turn.append_settled'] = {'turn': {**human, 'actor': 'assistant'}}
    turn_lifecycle.append_settled_turn('lead', command_id='c-2', actor='assistant', projection={}, user_id=7)
    assert calls == [(7, 'lead', 'main', 12)]

    def failing(*_args, **_kwargs):
        raise RuntimeError('storage unavailable')
    monkeypatch.setattr(questions, 'answer_lane_questions', failing)
    results['turn.append_settled'] = {'turn': human}
    assert turn_lifecycle.append_settled_turn('lead', command_id='c-3', actor='human', projection={},
                                              user_id=7) == {'turn': human}
