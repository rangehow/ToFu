"""Owner-question answers are recorded when they happen (docs/ROUTER.md R10).

Incident 2026-11-13 (OCR-HOLD): production's storage volume ran 19.7-25.9k RPCs
a minute, 98% from the API host, about 10k of them one-Turn ``turn.sync.page``
reads from GET /api/v4/router/projects -> ``_proactive_view`` ->
``open_questions``, which re-read every Turn after a non-blocking question back
to its ordinal. After that was bounded, the office's cold pass still walked
every lead's lane per process (about 310 page reads, 13-20 s on production).

The fixture has the production shape: 61 question records (60 non-blocking),
39 of them in one Project Router conversation of thousands of Turns with the
owner's messages between them; the rest in the Global Router, which the page
omits, and a second project. Which questions are open is fixed by the rule: a
card response or supersession answers a question, and a non-blocking one is also
answered by a later human Turn in its lane. ``legacy=True`` lays the records out
as the index held them before answers were recorded; otherwise every question is
recorded by ``record_question`` and every owner message records its answers, as
``lib.turn_lifecycle`` does for each human Turn it writes.

Bindings, question records, reports and the route are real (a SQLite Sidecar).
The leads' Turns are served by ``TurnStore`` with the SQL authority's read
semantics, from rows the authority itself produced: appending thousands of
Turns one command at a time does not fit a test on a loaded host. Every read
the question path makes still goes through the storage client and is counted;
a step that passes its read budget fails at once (``OverBudget``).
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
import time
import uuid

import pytest

# Imported at collection so a run measures the code it collected.
import lib.router.projects  # noqa: F401
import lib.router.proactive.questions  # noqa: F401
import lib.turn_lifecycle  # noqa: F401

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]

ROUTE = '/api/v4/router/projects'
ROUTE_LEAD_TURNS = 3000
OFFICE_LEAD_TURNS = 550
SERVED = ('turn.sync.page', 'turn.get', 'turn.list')


class OverBudget(BaseException):
    """A step read more than its budget; a BaseException so no broad handler absorbs it."""


class TurnStore:
    """The leads' Turn rows, read like the SQL authority reads them.

    ``turn.sync.page`` answers a request at another sync head with ``stale`` and
    the head, pages newest-first and returns each page oldest-first with
    ``hasMore`` and ``nextBeforeOrdinal``. Rows copy a Turn the authority
    returned, full and without its projection.
    """

    def __init__(self, full, metadata):
        self.full, self.metadata = full, metadata
        self.lanes, self.heads = {}, {}

    def append(self, conversation_id, actor, projection):
        rows = self.lanes.setdefault(conversation_id, [])
        self.heads[conversation_id] = self.heads.get(conversation_id, 0) + 1
        tail = rows[-1]['full'] if rows else None
        identity = {'turnId': str(uuid.uuid4()), 'conversationId': conversation_id, 'laneId': 'main',
                    'ordinal': tail['ordinal'] + 1 if tail else 0, 'actor': actor,
                    'parentTurnId': tail['turnId'] if tail else None}
        rows.append({'full': {**deepcopy(self.full), **identity, 'projection': projection},
                     'metadata': {**deepcopy(self.metadata), **identity}})
        return rows[-1]['full']

    def serves(self, operation, payload):
        return operation in SERVED and payload.get('conversation_id') in self.lanes

    def query(self, operation, payload):
        conversation = payload['conversation_id']
        rows = self.lanes[conversation]
        if operation == 'turn.get':
            row = next((row for row in rows if row['full']['turnId'] == payload['turn_id']), None)
            if row is None:
                return None
            return deepcopy(row['metadata'] if payload.get('include_projection') is False else row['full'])
        if operation == 'turn.list':
            return [deepcopy(row['full']) for row in rows]
        head = self.heads[conversation]
        if payload['sync_sequence'] != head:
            return {'stale': True, 'syncSequence': head}
        before = payload.get('before_ordinal')
        older = [row['full'] for row in rows if before is None or row['full']['ordinal'] < before]
        page = older[-payload['limit']:]
        return {'conversationId': conversation, 'conversationRevision': head, 'syncSequence': head,
                'laneId': payload['lane_id'], 'beforeOrdinal': before, 'hasMore': len(older) > len(page),
                'nextBeforeOrdinal': page[0]['ordinal'] if page else None, 'totalTurns': len(rows),
                'turns': deepcopy(page), 'attempts': []}


class Reads:
    """Every storage operation through the facade, by operation; ``budget`` caps Turn page reads."""

    def __init__(self):
        self.counted = Counter()
        self.turn_gets = []
        self.budget = None
        self.turns = None

    def take(self, budget=None):
        snapshot = (Counter(self.counted), list(self.turn_gets))
        self.counted.clear()
        self.turn_gets.clear()
        self.budget = budget
        return snapshot

    def query(self, client, operation, payload, *args, **kwargs):
        self.counted[operation] += 1
        if operation == 'turn.get':
            self.turn_gets.append(payload.get('turn_id'))
        if (operation == 'turn.sync.page' and self.budget is not None
                and self.counted[operation] > self.budget):
            raise OverBudget(f'turn.sync.page reads passed the budget of {self.budget}: {dict(self.counted)}')
        if self.turns is not None and self.turns.serves(operation, payload):
            return self.turns.query(operation, payload)
        return client.query(operation, payload, *args, **kwargs)


@pytest.fixture
def flask_app(chat_sidecar):
    import server
    server.app.config.update(TESTING=True)
    return server.app


@pytest.fixture
def home_root(tmp_path, monkeypatch):
    root = tmp_path / 'router-homes'
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(root))
    return root


@pytest.fixture
def reads(chat_sidecar, monkeypatch):
    """Count the storage facade's operations and serve the leads' Turns from a TurnStore."""
    import lib.storage
    from lib.turn_lifecycle import append_settled_turn
    authority = lib.storage.get_storage_client
    recorder = Reads()
    template = f'question-reads-template-{uuid.uuid4().hex[:8]}'
    turn = append_settled_turn(template, command_id=template, actor='assistant', kind='output', user_id=1,
                               projection={'content': 'template'},
                               conversation_defaults={'allowCreate': True, 'title': 'Turn template'})['turn']
    identity = {'conversation_id': template, 'turn_id': turn['turnId'], 'user_id': 1}
    full = authority().query('turn.get', identity)
    metadata = authority().query('turn.get', {**identity, 'include_projection': False})
    assert 'projection' in full and 'projection' not in metadata
    recorder.turns = TurnStore(full, metadata)

    class Counted:
        def __init__(self, client):
            self._client = client

        def query(self, operation, payload, *args, **kwargs):
            return recorder.query(self._client, operation, payload, *args, **kwargs)

        def command(self, operation, payload, *args, **kwargs):
            recorder.counted['command:' + operation] += 1
            return self._client.command(operation, payload, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._client, name)

    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: Counted(authority(**kwargs)))
    return recorder


def _fresh_lane_memo(monkeypatch):
    """A process that has walked no lane yet (absent on builds without the memo)."""
    import lib.router.proactive.questions as questions
    scans = getattr(questions, 'LaneScans', None)
    if scans is not None:
        monkeypatch.setattr(questions, 'LANE_SCANS', scans())


def _lead(owner, path, name):
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    path.mkdir()
    return ProjectCoordinationRepository(owner).provision(
        str(path), title='Project coordinator', settings={}, command_id='question-reads:' + name)


def _owner_message(owner, turns, conversation, content):
    """A human Turn and the answers it records, as ``lib.turn_lifecycle._record_human_answers`` does."""
    from lib.router.proactive.questions import answer_lane_questions
    turn = turns.append(conversation, 'human', {'content': content})
    answer_lane_questions(owner, conversation, lane_id='main', ordinal=turn['ordinal'])
    return turn


def _legacy_record(owner, binding, turn, guidance_id, blocking):
    """The record ``record_question`` wrote before answers were recorded: no position."""
    from lib.router.proactive.questions import NAMESPACE
    from lib.storage import get_storage_client
    get_storage_client(write=True).command('record.put', {
        'namespace': NAMESPACE, 'key': f"{owner}:{binding['conversationId']}:{guidance_id}", 'expected_version': 0,
        'value': {'owner': owner, 'coordinatorId': binding['conversationId'], 'projectPath': binding['projectPath'],
                  'guidanceId': guidance_id, 'turnId': turn['turnId'], 'toolCallId': '', 'blocking': blocking,
                  'question': 'Proceed with ' + guidance_id + '?', 'options': [], 'questions': [], 'default': '',
                  'createdAt': int(time.time() * 1000)}}, 'legacy-question:' + guidance_id)


def _transcript(owner, turns, binding, layout, *, legacy=False):
    """Lay ``layout`` out as the lead's Turns, recording each question; ``{guidanceId: Turn id}``."""
    from lib.router.proactive.questions import record_question
    conversation = binding['conversationId']
    questions = {}
    for index, item in enumerate(layout):
        if item == 'owner':
            if legacy:
                turns.append(conversation, 'human', {'content': f'Owner message {index}'})
            else:
                _owner_message(owner, turns, conversation, f'Owner message {index}')
            continue
        if item == 'report':
            turns.append(conversation, 'assistant', {'content': f'Worker report {index}'})
            continue
        guidance_id, blocking, card = item
        card_round = {'guidanceId': guidance_id, 'toolName': 'ask_human'}
        if card:
            card_round[card] = 'Owner answered on the card'
        turn = turns.append(conversation, 'assistant', {'content': 'Question ' + guidance_id,
                                                        'toolRounds': [card_round]})
        if legacy:
            _legacy_record(owner, binding, turn, guidance_id, blocking)
        else:
            record_question(owner, {'projectPath': binding['projectPath'], 'coordinatorId': conversation,
                                    'turnId': turn['turnId'], 'guidanceId': guidance_id,
                                    'blocking': blocking, 'question': 'Proceed with ' + guidance_id + '?'})
        questions[guidance_id] = turn['turnId']
    return questions


def _long_layout(prefix, length):
    """``length`` Turns holding 39 non-blocking questions; ``(layout, newest owner ordinal)``.

    36 blocks of ``step`` Turns each hold a question answered by the owner's
    message at the block's end (two were also answered on their cards); in the
    last 200 Turns the owner is silent: questions at +50 and +150 stay open,
    the one at +100 is answered on its card.
    """
    step = (length - 200) // 36
    layout = ['report'] * length
    for k in range(36):
        card = {3: 'guidanceResponse', 20: 'guidanceSuperseded'}.get(k)
        layout[step * k + 1] = (f'{prefix}-long-{k:02d}', False, card)
        layout[step * k + step - 1] = 'owner'
    quiet = 36 * step
    layout[quiet + 50] = (f'{prefix}-long-36', False, None)
    layout[quiet + 100] = (f'{prefix}-long-37', False, 'guidanceResponse')
    layout[quiet + 150] = (f'{prefix}-long-38', False, None)
    return layout, quiet - 1


def _short_layout(prefix):
    """40 Turns: a blocking question at 0 (open, though owner messages follow), nine
    non-blocking ones at 3 + 3k answered by the owner up to Turn 19; 21, 24, 27 open."""
    layout = ['report'] * 40
    layout[0] = (f'{prefix}-short-blocking', True, None)
    for k in range(9):
        layout[3 + 3 * k] = (f'{prefix}-short-{k}', False, None)
    for k in range(6):
        layout[4 + 3 * k] = 'owner'
    return layout


def _router_layout(prefix):
    """60 Turns: twelve non-blocking questions at 2 + 5k, the owner at 4 + 5k up to 49."""
    layout = ['report'] * 60
    for k in range(12):
        layout[2 + 5 * k] = (f'{prefix}-router-{k:02d}', False, None)
    for k in range(10):
        layout[4 + 5 * k] = 'owner'
    return layout


def _open_ids(questions):
    return {guidance_id for guidance_id in questions
            if guidance_id.endswith(('-long-36', '-long-38', '-short-blocking', '-short-6', '-short-7',
                                     '-short-8', '-router-10', '-router-11'))}


def _get(flask_client):
    started = time.monotonic()
    response = flask_client.get(ROUTE)
    elapsed = time.monotonic() - started
    assert response.status_code == 200, response.get_json()
    page = json.loads(response.get_data(as_text=True))['data']
    return {entry['projectPath']: entry for entry in page['projects']}, elapsed


def _question_facts(entry):
    return {key: entry.get(key) for key in ('openQuestions', 'blockingQuestions')}


def test_route_decides_each_legacy_question_once_and_then_reads_no_lane(
        flask_client, chat_sidecar, home_root, tmp_path, reads, monkeypatch, record_property):
    from lib.router.proactive.questions import open_questions
    owner, turns = 1, reads.turns
    router = flask_client.post('/api/v4/router', json={}, headers={'Idempotency-Key': 'ensure'}).get_json()
    alpha, beta = _lead(owner, tmp_path / 'alpha', 'alpha'), _lead(owner, tmp_path / 'beta', 'beta')
    long_layout, newest_owner = _long_layout('r', ROUTE_LEAD_TURNS)
    questions = {}
    for binding, layout in ((router, _router_layout('r')), (alpha, long_layout), (beta, _short_layout('r'))):
        questions.update(_transcript(owner, turns, binding, layout, legacy=True))
    assert len(questions) == 61 and sum(1 for gid in questions if 'blocking' not in gid) == 60
    expected_open = _open_ids(questions)
    _fresh_lane_memo(monkeypatch)
    # The first read after the deploy decides each listed legacy question once: each listed lane
    # is read once, newest first, down to the owner's newest message (alpha: its tail down to
    # ``newest_owner``; beta: Turns 39..19), plus each read's first page at a stale sync head.
    reads.take(budget=(ROUTE_LEAD_TURNS - newest_owner + 1) + (40 - 19 + 1))

    listed, cold_seconds = _get(flask_client)
    cold, _ = reads.take(budget=0)
    record_property('first_get_after_deploy', {'seconds': round(cold_seconds, 3), 'reads': dict(cold)})
    assert router['projectPath'] not in listed
    assert _question_facts(listed[alpha['projectPath']]) == {'openQuestions': 2, 'blockingQuestions': 0}
    assert _question_facts(listed[beta['projectPath']]) == {'openQuestions': 4, 'blockingQuestions': 1}
    assert cold['turn.list'] == 0
    # One card read per question of the two listed projects; the Global Router's are not read.
    assert cold['turn.get'] <= 39 + 10, dict(cold)

    # From then on the index answers: no lane read, and a Turn read only for the open blocking card.
    listed_again, warm_seconds = _get(flask_client)
    warm, warm_turn_gets = reads.take(budget=0)
    record_property('warm_get', {'seconds': round(warm_seconds, 3), 'reads': dict(warm)})
    assert {path: _question_facts(entry) for path, entry in listed_again.items()} == {
        path: _question_facts(entry) for path, entry in listed.items()}
    assert warm['turn.sync.page'] == 0, dict(warm)
    assert set(warm_turn_gets) <= {questions['r-short-blocking']}, 'only an open blocking card is read'

    # New Turns cost nothing; the owner's next message records its answers when it is written.
    for index in range(50):
        turns.append(alpha['conversationId'], 'assistant', {'content': f'Late report {index}'})
    _get(flask_client)
    _owner_message(owner, turns, alpha['conversationId'], 'Go ahead with both.')
    answered_now, _ = _get(flask_client)
    assert _question_facts(answered_now[alpha['projectPath']]) == {'openQuestions': None,
                                                                   'blockingQuestions': None}
    assert _question_facts(answered_now[beta['projectPath']]) == {'openQuestions': 4, 'blockingQuestions': 1}
    settled, _ = reads.take(budget=(60 - 49) + 1)
    assert settled['turn.sync.page'] == 0, dict(settled)

    # The digest's whole-owner read decides the Global Router's legacy questions once, by the same
    # rule (its lane down to the owner's message at 49), and then reads no lane either.
    expected_open -= {'r-long-36', 'r-long-38'}
    assert {row['guidanceId'] for row in open_questions(owner)} == expected_open
    reads.take(budget=0)
    assert {row['guidanceId'] for row in open_questions(owner)} == expected_open
    again, _ = reads.take()
    assert again['turn.sync.page'] == 0, dict(again)


def _office(owner):
    """The office feed over the production ports, with every source but leads and owner questions empty."""
    from lib.router import office_facts

    class Ports(office_facts.StoragePorts):
        def running(self, owner):
            return {}

        def changed_conversations(self, owner, since_ms):
            return [], True

        def board(self, owner, lead):
            return None

        def report(self, owner, lead):
            return None

        def report_messages(self, owner, lead, known_version):
            return 0, []

        def alarms(self, owner):
            return []

        def newest_results(self, owner, lead, running):
            return [], []

        def publish(self, owner, frame):
            pass

        def subscribed(self, owner):
            return False
    return office_facts.OfficeFacts(Ports())


def _decisions(snapshot):
    return {fact['guidanceId'] for fact in snapshot['facts'] if fact['fact'] == 'decision'}


def test_the_office_reads_no_lead_lane_for_owner_questions_after_their_one_decision(
        chat_sidecar, tmp_path, reads, monkeypatch, record_property):
    owner, turns = 2, reads.turns
    leads = [_lead(owner, tmp_path / name, name) for name in ('office-router', 'office-alpha', 'office-beta')]
    long_layout, newest_owner = _long_layout('o', OFFICE_LEAD_TURNS)
    questions = {}
    for binding, layout in zip(leads, (_router_layout('o'), long_layout, _short_layout('o'))):
        questions.update(_transcript(owner, turns, binding, layout, legacy=True))
    assert len(questions) == 61
    expected_open = _open_ids(questions)
    _fresh_lane_memo(monkeypatch)
    # The first cold snapshot after the deploy decides each legacy question once: each lane is read
    # once, newest first, down to its newest owner message, plus one stale first page per lane.
    reads.take(budget=(OFFICE_LEAD_TURNS - newest_owner + 1) + (40 - 19 + 1) + (60 - 49 + 1))
    feed = _office(owner)
    try:
        started = time.monotonic()
        assert _decisions(feed.snapshot(owner)) == expected_open
        deciding, _ = reads.take(budget=0)
        record_property('office_first_cold_snapshot_after_deploy',
                        {'seconds': round(time.monotonic() - started, 3), 'reads': dict(deciding)})
    finally:
        feed.close()

    # A process start: a new feed and an empty lane memo. Its cold snapshot reads no lane, and a
    # Turn only for the open blocking question's card.
    _fresh_lane_memo(monkeypatch)
    feed = _office(owner)
    try:
        started = time.monotonic()
        assert _decisions(feed.snapshot(owner)) == expected_open
        cold, cold_turn_gets = reads.take(budget=0)
        record_property('office_cold_snapshot_after_process_start',
                        {'seconds': round(time.monotonic() - started, 3), 'reads': dict(cold)})
        assert cold['turn.sync.page'] == 0, dict(cold)
        assert set(cold_turn_gets) <= {questions['o-short-blocking']}, dict(cold)

        # The owner answers in the lead with the open questions: the answer is recorded as the
        # message is written, and the pass its commit causes clears them with no lane read.
        alpha = leads[1]['conversationId']
        _owner_message(owner, turns, alpha, 'Go ahead with both.')
        feed.observe_scopes([{'userId': owner, 'conversationId': alpha}])
        assert feed.wait_idle(30)
        assert _decisions(feed.snapshot(owner)) == expected_open - {'o-long-36', 'o-long-38'}
        answered, _ = reads.take(budget=2)
        assert answered['turn.sync.page'] == 0, dict(answered)

        # A question asked now is decided from its lane's head as it is recorded (two page reads),
        # and the office shows it without reading the lane.
        beta = leads[2]
        _transcript(owner, turns, beta, [('o-short-new', False, None)])
        asked, _ = reads.take(budget=0)
        assert asked['turn.sync.page'] <= 2, dict(asked)
        feed.observe_scopes([{'userId': owner, 'conversationId': beta['conversationId']}])
        assert feed.wait_idle(30)
        assert 'o-short-new' in _decisions(feed.snapshot(owner))
        shown, _ = reads.take()
        assert shown['turn.sync.page'] == 0, dict(shown)
    finally:
        feed.close()


def test_every_human_turn_the_turn_lifecycle_writes_records_its_answers(chat_sidecar, tmp_path):
    """A created pair and an appended settled Turn record the owner's answer as they write it."""
    from lib import turn_lifecycle
    from lib.router.proactive.questions import NAMESPACE, open_questions, record_question
    from lib.storage import get_storage_client
    owner = 3
    binding = _lead(owner, tmp_path / 'hooks', 'hooks')
    conversation = binding['conversationId']

    def ask(guidance_id):
        turn = turn_lifecycle.append_settled_turn(
            conversation, command_id='ask:' + guidance_id, actor='assistant', kind='output', user_id=owner,
            projection={'content': 'Proceed?', 'toolRounds': [{'guidanceId': guidance_id, 'toolName': 'ask_human'}]},
        )['turn']
        record_question(owner, {'projectPath': binding['projectPath'], 'coordinatorId': conversation,
                                'turnId': turn['turnId'], 'guidanceId': guidance_id, 'blocking': False,
                                'question': 'Proceed?'})
        return turn

    def recorded(guidance_id):
        return get_storage_client().query('record.get', {
            'namespace': NAMESPACE, 'key': f'{owner}:{conversation}:{guidance_id}'})['value']

    asked = ask('hg-pair')
    assert (recorded('hg-pair')['ordinal'], recorded('hg-pair')['answersRecorded']) == (asked['ordinal'], True)
    pair = turn_lifecycle.create_turn_pair(conversation, command_id='owner-reply-1',
                                           input_projection={'content': 'Yes, go ahead.'}, config={}, user_id=owner)
    assert recorded('hg-pair')['answeredOrdinal'] == pair['submittedTurn']['ordinal']

    ask('hg-settled')
    settled = turn_lifecycle.append_settled_turn(conversation, command_id='owner-reply-2', actor='human',
                                                 kind='input', user_id=owner, projection={'content': 'Yes.'})
    assert recorded('hg-settled')['answeredOrdinal'] == settled['turn']['ordinal']
    ask('hg-open')
    assert [row['guidanceId'] for row in open_questions(owner, coordinator_id=conversation)] == ['hg-open']
