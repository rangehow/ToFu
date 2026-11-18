"""Owner questions: a durable index of questions the owner has not answered.

Owner: docs/ROUTER.md R10 "Owner question unanswered" trigger. Every
``ask_human`` from a bound Coordinator (Project Router or Global Router) is
recorded here at the relay seam (``_relay_to_global_router``). A question is
answered when its card has a response or was superseded, or, for a non-blocking
question, when a human Turn enters its lane after it. The digest, the quiet-end
release and the office list open questions; no new reminder interval exists.

Records: portable record CAS, namespace ``router.proactive.questions.v1``, one
record per (owner, coordinator, guidanceId). The question card in the
Coordinator's conversation stays the canonical record; this is an index. A
record holds its question Turn's ``laneId`` and ``ordinal``. An answer is
permanent and recorded on the record (``answeredAt``, ``answeredOrdinal``);
nothing reads an answered question again (R10, "Owner question answers are
recorded when they happen"):

- A non-blocking question is answered by a chat message: a human Turn that
  enters its lane after it records itself as the answer of every open
  non-blocking question before it there (``answer_lane_questions``, called by
  ``lib.turn_lifecycle`` wherever a human Turn is created, activated or
  appended, in both processes). ``answersRecorded`` marks a non-blocking record
  whose answers are recorded that way: the record alone says it is open.
- A blocking question is answered on its card (a response or supersession in
  the question's own Turn). While it is open, a listing reads that Turn once;
  the listing that finds the answer records it.
- Deciding a non-blocking record that is not ``answersRecorded`` yet (one
  ``record_question`` just wrote, or one indexed before answers were recorded)
  positions it first, so a human Turn from then on records its own answer, then
  reads its lane newest first down to the question once and records the
  outcome. ``LANE_SCANS`` keeps what each lane's last read proved, so the reads
  of one process share their Turns.

A failed index write never changes what a listing reports; it leaves the record
for a later reader to decide.

A lead withdraws its own open question when the conversation settled it
(``withdraw_question``, the ask_human ``withdraw`` action; R10 "A lead
withdraws a settled question"). The record gains ``withdrawnAt`` and
``withdrawnReason`` and no listing reports it again; its card gains
``guidanceWithdrawn`` but stays answerable, so an owner answer that comes
anyway is still delivered (the card's ``answer_guidance``, or a chat message,
which ``answer_lane_questions`` still records) and overrides the withdrawal.

Entry points: record_question, answer_lane_questions, open_questions,
withdraw_question, LaneScans.
Dependencies: turn_lifecycle (answer evidence), storage (record CAS).
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import threading
import time
import uuid

from lib.identity import require_user_id
from lib.log import get_logger
from lib.storage.errors import StorageError

logger = get_logger(__name__)

NAMESPACE = 'router.proactive.questions.v1'
#: ``record.list`` page size of every listing here.
LIST_PAGE_ROWS = 1000


def _key(owner, coordinator_id, guidance_id):
    return f'{owner}:{coordinator_id}:{guidance_id}'


def _client(client=None):
    if client is None:
        from lib.storage import get_storage_client
        client = get_storage_client
    return client


def _rows(client, prefix):
    """Every record under ``prefix``, page by page."""
    after = ''
    while True:
        page = client().query('record.list', {
            'namespace': NAMESPACE, 'prefix': prefix, 'after_key': after, 'limit': LIST_PAGE_ROWS})
        if not isinstance(page, list):
            return
        for row in page:
            if (isinstance(row, dict) and str(row.get('key', '')).startswith(prefix)
                    and isinstance(row.get('value'), dict)):
                yield row
        if len(page) < LIST_PAGE_ROWS:
            return
        after = page[-1]['key']


def _update(client, row, change):
    """Write ``change(value)`` to one record by CAS; the record as stored, or None once it is gone.

    ``change`` returns the new value, or None when the record already holds what
    it would add. Every write here only adds evidence (a position,
    ``answersRecorded``, an answer), so a conflict means another writer moved the
    record forward: the record is read again and ``change`` decides again, until
    it has nothing left to add or its write lands. Other storage errors raise.
    """
    key = row['key']
    while True:
        updated = change(row['value'])
        if updated is None:
            return row
        digest = hashlib.sha256(json.dumps(updated, sort_keys=True, default=str).encode()).hexdigest()[:16]
        try:
            stored = client(write=True).command('record.put', {
                'namespace': NAMESPACE, 'key': key, 'value': updated, 'expected_version': row['version'],
            }, f"owner-question:{key}:{row['version']}:{digest}")
        except StorageError as error:
            if error.code != 'database_conflict':
                raise
        else:
            version = stored.get('version') if isinstance(stored, dict) else None
            if type(version) is int:
                return {'key': key, 'value': updated, 'version': version}
        current = client().query('record.get', {'namespace': NAMESPACE, 'key': key})
        if current is None:
            return None
        row = {'key': key, 'value': current['value'], 'version': current['version']}


def _recorded(client, row, change):
    """``_update``, or None when the write failed: the record stays for a later reader to decide."""
    try:
        return _update(client, row, change)
    except StorageError as error:
        logger.warning('[OwnerQuestions] index write failed for %s: %s', row['key'], error)
        return None


def _metadata(conversation_id, turn_id, *, user_id):
    """The Turn without its projection, or None once it no longer exists."""
    from lib.turn_lifecycle import LifecycleNotFound, get_turn
    try:
        return get_turn(conversation_id, turn_id, user_id=user_id, include_projection=False)
    except LifecycleNotFound:
        return None


def _reads(turn_get, lane_turns, turn_meta):
    if turn_get is None:
        from lib.turn_lifecycle import get_turn as turn_get
    if lane_turns is None:
        from lib.turn_lifecycle import lane_turns_newest_first as lane_turns
    return turn_get, lane_turns, turn_meta or _metadata


def _position(turn):
    return {'laneId': turn.get('laneId') or 'main', 'ordinal': int(turn['ordinal'])}


def _answered(value, ordinal):
    answer = {'answeredAt': int(time.time() * 1000)}
    if type(ordinal) is int:
        answer['answeredOrdinal'] = ordinal
    return {**value, **answer}


def record_question(owner, question, *, client=None, turn_get=None, lane_turns=None, turn_meta=None, scans=None):
    """Index one ask_human question from a bound lead. Idempotent per guidanceId.

    The record holds its question Turn's position. A non-blocking question is then
    decided at once by reading its lane down to the question, which is the lane's
    head while it asks: a human Turn that entered before the record existed is met
    by that read, and every later one records its answer itself
    (``answer_lane_questions``). An unreadable Turn leaves the record for
    ``open_questions`` to position and decide.
    """
    owner = require_user_id(owner, context='owner questions')
    client = _client(client)
    _turn_get, lane_turns, turn_meta = _reads(turn_get, lane_turns, turn_meta)
    key = _key(owner, question['coordinatorId'], question['guidanceId'])
    value = {
        'owner': owner,
        'coordinatorId': question['coordinatorId'],
        'projectPath': question['projectPath'],
        'guidanceId': question['guidanceId'],
        'turnId': question['turnId'],
        'toolCallId': question.get('toolCallId', ''),
        'blocking': bool(question.get('blocking')),
        'question': question['question'],
        'options': question.get('options') or [],
        'questions': question.get('questions') or [],
        'default': question.get('default', ''),
        'createdAt': int(time.time() * 1000),
    }
    try:
        existing = client().query('record.get', {'namespace': NAMESPACE, 'key': key})
        if existing is not None:
            return existing['value']
        try:
            turn = turn_meta(question['coordinatorId'], question['turnId'], user_id=owner)
        except Exception:
            logger.warning('[OwnerQuestions] question Turn unreadable at record time: %s',
                           question['guidanceId'], exc_info=True)
            turn = None
        if turn is not None and turn.get('ordinal') is not None:
            value.update(_position(turn))
        stored = client(write=True).command('record.put', {
            'namespace': NAMESPACE, 'key': key, 'value': value, 'expected_version': 0,
        }, 'owner-question:' + uuid.uuid4().hex)
    except StorageError as error:
        if error.code != 'database_conflict':
            raise
        return value
    if 'ordinal' in value and not value['blocking']:
        version = stored.get('version') if isinstance(stored, dict) else None
        row = {'key': key, 'value': value, 'version': version} if type(version) is int else None
        if row is None:
            current = client().query('record.get', {'namespace': NAMESPACE, 'key': key})
            row = None if current is None else {'key': key, 'value': current['value'], 'version': current['version']}
        if row is not None:
            _decide_unrecorded(owner, client, [(value['coordinatorId'], value['laneId'], value['ordinal'], row)],
                               lane_turns=lane_turns, turn_meta=turn_meta,
                               scans=LANE_SCANS if scans is None else scans)
    return value


def _answerable_by_human_turn(value, conversation_id, lane_id, ordinal):
    return (not value.get('answeredAt') and not value.get('blocking')
            and value.get('coordinatorId') == conversation_id and value.get('laneId') == lane_id
            and type(value.get('ordinal')) is int and value['ordinal'] < ordinal)


def answer_lane_questions(owner, conversation_id, *, lane_id, ordinal, client=None):
    """Record the human Turn at ``ordinal`` of this lane as the answer of every open
    non-blocking question before it there; the guidance ids it answered.

    A record without a position is not answered here: whoever decides it positions
    it before reading the lane, so a human Turn that entered before that is met
    by the read and every later one finds the position.
    """
    owner = require_user_id(owner, context='owner questions')
    client = _client(client)
    answered = []
    for row in _rows(client, f'{owner}:{conversation_id}:'):
        if not _answerable_by_human_turn(row['value'], conversation_id, lane_id, ordinal):
            continue
        stored = _recorded(client, row, lambda value: _answered(value, ordinal)
                           if _answerable_by_human_turn(value, conversation_id, lane_id, ordinal) else None)
        if stored is not None and stored['value'].get('answeredOrdinal') == ordinal:
            answered.append(row['value']['guidanceId'])
    return answered


@dataclass(frozen=True)
class LaneScan:
    """What one newest-first walk of a lane proved: no human Turn has an ordinal in (floor, anchor_ordinal].

    The fact holds while the anchor Turn exists. A Turn created later takes the
    ordinal after the lane head (SQL ``MAX(ordinal) + 1``, native lane head + 1),
    above the anchor's; the only Turn placed into an ordinal gap is a compaction
    summary, an assistant Turn; and no rewrite makes a Turn human (regenerate
    targets assistant or planner; visible phases are never human). A walk that
    meets the anchor at the same id and ordinal therefore skips the range.
    ``human_id`` is the human Turn at ``floor`` where the walk stopped; it answers
    an older question only after a point read finds it unchanged.
    """
    anchor_id: str
    anchor_ordinal: int
    floor: int
    human_id: str | None


class LaneScans:
    """The newest LaneScan of each (owner, conversation, lane) this process walked.

    Each walk replaces its lane's entry; only lanes holding owner questions are
    walked, so the question index bounds the entries.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._scans: dict[tuple, LaneScan] = {}

    def get(self, key):
        with self._lock:
            return self._scans.get(key)

    def put(self, key, scan):
        with self._lock:
            self._scans[key] = scan


LANE_SCANS = LaneScans()


def _card(owner, question, turn_get):
    """``(answered, turn)`` from the question's own Turn: a card response or supersession answers it.

    An unreadable, missing or unpositioned Turn of an open question is
    ``(False, None)``: the question reads as open.
    """
    try:
        turn = turn_get(question['coordinatorId'], question['turnId'], user_id=owner)
    except Exception as error:
        logger.debug('[Questions] answered-turn read failed for %s: %s', question['turnId'], error)
        return False, None
    if turn is None:
        return False, None
    for round_entry in (turn.get('projection') or {}).get('toolRounds', []):
        if (isinstance(round_entry, dict) and round_entry.get('guidanceId') == question['guidanceId']
                and (round_entry.get('guidanceResponse') or round_entry.get('guidanceSuperseded'))):
            return True, turn
    return False, turn if turn.get('ordinal') is not None else None


def _human_unchanged(owner, conversation_id, lane_id, scan, turn_meta):
    turn = turn_meta(conversation_id, scan.human_id, user_id=owner)
    return (turn is not None and turn.get('actor') == 'human' and turn.get('ordinal') == scan.floor
            and (turn.get('laneId') or 'main') == lane_id)


def _answered_ordinals(owner, conversation_id, lane_id, ordinals, *, lane_turns, turn_meta, scans):
    """``{question ordinal: answering human Turn ordinal}`` for the questions of one lane a later human Turn answers.

    One newest-first walk decides them all: a question is open once the walk
    reaches its ordinal without meeting a human Turn, and every question below
    the first human Turn it meets is answered by that Turn. Meeting the lane's
    LaneScan anchor decides the questions inside its range; older ones are
    answered by its human Turn while that is unchanged, otherwise the walk
    resumes below its floor.
    """
    key = (owner, conversation_id, lane_id)
    scan = scans.get(key)
    pending = sorted(set(ordinals), reverse=True)
    answered = {}
    anchor = None
    floor, human_id = -1, None
    rows = lane_turns(conversation_id, lane_id, user_id=owner)
    while rows is not None:
        walk, rows = rows, None
        for row, _attempt in walk:
            ordinal = int(row['ordinal'])
            if anchor is None:
                anchor = (row['turnId'], ordinal)
            while pending and pending[0] >= ordinal:
                pending.pop(0)
            if row.get('actor') == 'human':
                answered.update(dict.fromkeys(pending, ordinal))
                floor, human_id = ordinal, row['turnId']
                break
            if not pending:
                floor = ordinal - 1
                break
            if scan is not None and row['turnId'] == scan.anchor_id and ordinal == scan.anchor_ordinal:
                pending = [value for value in pending if value < scan.floor]
                floor, human_id = scan.floor, scan.human_id
                if pending and human_id is not None and _human_unchanged(
                        owner, conversation_id, lane_id, scan, turn_meta):
                    answered.update(dict.fromkeys(pending, scan.floor))
                elif pending:
                    floor, human_id = -1, None
                    rows = lane_turns(conversation_id, lane_id, user_id=owner, before_ordinal=scan.floor + 1)
                scan = None
                break
    if anchor is not None:
        scans.put(key, LaneScan(anchor[0], anchor[1], floor, human_id))
    return answered


def _decide_unrecorded(owner, client, members, *, lane_turns, turn_meta, scans):
    """Decide positioned non-blocking questions of one lane by one read of it; the ones still open.

    ``members`` are ``(conversation, lane, ordinal, row)`` whose records already
    hold their position, written before this read. A failed read decides nothing:
    the questions are reported open and stay undecided.
    """
    conversation_id, lane_id = members[0][0], members[0][1]
    try:
        answers = _answered_ordinals(owner, conversation_id, lane_id, [ordinal for _c, _l, ordinal, _r in members],
                                     lane_turns=lane_turns, turn_meta=turn_meta, scans=scans)
    except Exception:
        logger.warning('[OwnerQuestions] answer read failed for %s lane %s', conversation_id, lane_id,
                       exc_info=True)
        return [row['value'] for _c, _l, _o, row in members]
    still_open = []
    for _c, _l, ordinal, row in members:
        if ordinal in answers:
            _recorded(client, row, lambda value, by=answers[ordinal]: None if value.get('answeredAt')
                      else _answered(value, by))
            continue
        stored = _recorded(client, row, lambda value: None if (
            value.get('answeredAt') or value.get('answersRecorded') or type(value.get('ordinal')) is not int)
            else {**value, 'answersRecorded': True})
        if stored is None or not stored['value'].get('answeredAt'):
            still_open.append(row['value'] if stored is None else stored['value'])
    return still_open


def open_questions(owner, *, coordinator_id=None, client=None, turn_get=None, lane_turns=None,
                   turn_meta=None, scans=None):
    """Unanswered questions of this owner's leads, or of one lead, oldest first, with their Turn position.

    An answered record and an ``answersRecorded`` non-blocking record cost no
    Turn read. Any other question's own Turn is read once, for its card; a card
    answer found is recorded. A non-blocking question not ``answersRecorded`` yet
    is positioned, then decided with the others of its lane by one read of it
    (``_decide_unrecorded``), and the outcome recorded.
    """
    owner = require_user_id(owner, context='owner questions')
    client = _client(client)
    turn_get, lane_turns, turn_meta = _reads(turn_get, lane_turns, turn_meta)
    prefix = f'{owner}:{coordinator_id}:' if coordinator_id else f'{owner}:'
    found, lanes = [], {}
    for row in _rows(client, prefix):
        value = row['value']
        if (value.get('answeredAt') or value.get('withdrawnAt')
                or (coordinator_id and value.get('coordinatorId') != coordinator_id)):
            continue
        if value.get('answersRecorded') and not value.get('blocking'):
            found.append(value)
            continue
        answered, turn = _card(owner, value, turn_get)
        if answered:
            by = turn.get('ordinal')
            _recorded(client, row, lambda current, by=by: None if current.get('answeredAt')
                      else _answered(current, by))
            continue
        if turn is None:
            found.append(value)
            continue
        position = _position(turn)
        if value.get('blocking'):
            found.append({**value, **position})
            continue
        stored = _recorded(client, row, lambda current: None if current.get('answeredAt') or all(
            current.get(name) == position[name] for name in position) else {**current, **position})
        if stored is None:
            found.append({**value, **position})
        elif stored['value'].get('answeredAt'):
            continue
        elif stored['value'].get('answersRecorded'):
            found.append(stored['value'])
        else:
            lanes.setdefault((value['coordinatorId'], position['laneId']), []).append(
                (value['coordinatorId'], position['laneId'], position['ordinal'], stored))
    for members in lanes.values():
        found += _decide_unrecorded(owner, client, members, lane_turns=lane_turns, turn_meta=turn_meta,
                                    scans=LANE_SCANS if scans is None else scans)
    found.sort(key=lambda value: value.get('createdAt', 0))
    return found


class QuestionNotWithdrawable(LookupError):
    """The question is not this lead's open owner question; ``reason`` is shown to the model."""


def _mark_card_withdrawn(owner, question, withdrawal, *, live_rounds, turn_get, update_projection):
    """Stamp ``guidanceWithdrawn`` on the question's card; where it landed: 'card', 'live' or 'unmarked'.

    A settled Turn is CAS-edited (``turn.projection.update``); a conflict re-reads
    and decides again. The card of the calling task's own running Turn is its
    live round (``live_rounds``), which the running attempt's projection writer
    persists; any other running Turn belongs to its attempt and is left alone.
    An answered card is left unchanged: the answer stands.
    """
    from lib.turn_lifecycle import LIVE_ATTEMPT_STATUSES, LifecycleConflict
    for round_entry in live_rounds or ():
        if isinstance(round_entry, dict) and round_entry.get('guidanceId') == question['guidanceId']:
            round_entry['guidanceWithdrawn'] = dict(withdrawal)
            return 'live'
    while True:
        turn = turn_get(question['coordinatorId'], question['turnId'], user_id=owner)
        if turn is None or turn.get('status') in LIVE_ATTEMPT_STATUSES:
            return 'unmarked'
        projection = dict(turn.get('projection') or {})
        rounds = [dict(entry) if isinstance(entry, dict) else entry for entry in projection.get('toolRounds') or []]
        card = next((entry for entry in rounds if isinstance(entry, dict)
                     and entry.get('guidanceId') == question['guidanceId']), None)
        if card is None or card.get('guidanceResponse') or card.get('guidanceSuperseded'):
            return 'unmarked'
        if card.get('guidanceWithdrawn'):
            return 'card'
        card['guidanceWithdrawn'] = dict(withdrawal)
        projection['toolRounds'] = rounds
        try:
            update_projection(question['coordinatorId'], question['turnId'], projection=projection,
                              expected_projection_revision=int(turn.get('projectionRevision') or 0),
                              user_id=owner)
        except LifecycleConflict as error:
            if error.code == 'turn_in_progress':
                return 'unmarked'
            continue
        return 'card'


def withdraw_question(owner, coordinator_id, guidance_id, *, reason, live_rounds=None, client=None,
                      turn_get=None, update_projection=None):
    """Withdraw this lead's own open owner question; ``(record, card)``.

    Only the record of ``(owner, coordinator_id, guidance_id)`` is eligible, so
    another conversation's question is never touched. An answered question is
    refused (its answer stands); a second withdrawal returns the first. ``card``
    says where the card's mark landed (``_mark_card_withdrawn``); a card that
    could not be marked still leaves every listing, which reads only the record.
    Raises QuestionNotWithdrawable.
    """
    owner = require_user_id(owner, context='owner questions')
    client = _client(client)
    reason = str(reason or '').strip()
    if not reason:
        raise QuestionNotWithdrawable('a reason is required: say what settled the question')
    if turn_get is None:
        from lib.turn_lifecycle import get_turn as turn_get
    if update_projection is None:
        from lib.turn_lifecycle import update_turn_projection as update_projection
    key = _key(owner, coordinator_id, guidance_id)
    current = client().query('record.get', {'namespace': NAMESPACE, 'key': key})
    if current is None:
        raise QuestionNotWithdrawable(
            f'{guidance_id} is not an owner question of this conversation; a lead withdraws only its own')
    row = {'key': key, 'value': current['value'], 'version': current['version']}
    if row['value'].get('answeredAt'):
        raise QuestionNotWithdrawable(f'{guidance_id} is already answered; the owner\u2019s answer stands')
    answered, turn = _card(owner, row['value'], turn_get)
    if answered:
        _recorded(client, row, lambda value, by=turn.get('ordinal'): None if value.get('answeredAt')
                  else _answered(value, by))
        raise QuestionNotWithdrawable(f'{guidance_id} is already answered; the owner\u2019s answer stands')
    withdrawal = {'reason': reason, 'at': int(time.time() * 1000)}
    stored = _update(client, row, lambda value: None if value.get('answeredAt') or value.get('withdrawnAt')
                     else {**value, 'withdrawnAt': withdrawal['at'], 'withdrawnReason': reason})
    if stored is None:
        raise QuestionNotWithdrawable(f'{guidance_id} is not an owner question of this conversation')
    value = stored['value']
    if value.get('answeredAt'):
        raise QuestionNotWithdrawable(f'{guidance_id} is already answered; the owner\u2019s answer stands')
    withdrawal = {'reason': value['withdrawnReason'], 'at': value['withdrawnAt']}
    card = _mark_card_withdrawn(owner, value, withdrawal, live_rounds=live_rounds, turn_get=turn_get,
                                update_projection=update_projection)
    return value, card


__all__ = ['NAMESPACE', 'LANE_SCANS', 'LaneScan', 'LaneScans', 'QuestionNotWithdrawable', 'answer_lane_questions',
           'open_questions', 'record_question', 'withdraw_question']
