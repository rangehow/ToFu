"""Project conversation messages: durable context, not an execution scheduler.

Entry points execute/inject/confirm/page_view/validate_sender/lead_role/wake_idle_lead use
portable record writes and owned metadata. No private transcript reads, daemon, permission
grants or second task lifecycle; an idle lead is woken through the ordinary turn command
(``wake_idle_lead``). Originals stay immutable; delivery records acceptance, and a
rejection, deferral or conflict is explicit. `ProjectMailbox` keeps append-only segmented history without a
lifetime count through generic `record.get`/`record.put` point operations only;
the durable key layout is `history` in `contracts/project_messages_v1.json`.
Messages stay within one project, except between the owner's current project
leads (`crossProject` in the contract, docs/ROUTER.md R13) and from the bound
Watchdog (R7).
"""
from __future__ import annotations
import bisect
import copy
from html import escape
import hashlib
from itertools import chain
import json
from pathlib import Path
import time
import uuid

from lib.conversations.project_identity import normalize_project_path
from lib.conversations.repository import ConversationRepository
from lib.identity import require_user_id
from lib.log import get_logger
from lib.agent_core.storage_errors import StorageError

CONTRACT = json.loads((Path(__file__).resolve().parents[2] / 'contracts/project_messages_v1.json').read_text())
TOOLS = CONTRACT['tools']
NAMES = frozenset(t['function']['name'] for t in TOOLS)
LIMITS = CONTRACT['limits']
KEYS = CONTRACT['history']['keys']
PAGE_MESSAGES = CONTRACT['$defs']['pageView']['properties']['messages']['maxItems']

INPUT_STATES = CONTRACT['$defs']['inputState']['enum']
DECISIONS = TOOLS[2]['function']['parameters']['properties']['decision']['enum']
CROSS_PROJECT = CONTRACT['crossProject']
UNCERTAIN = 'Concurrent/uncertain mailbox update; query the same message identity before retrying'
# What an arrival holds once its ticket is given up: no original ever lands there.
VOID = {'void': True}
# record.list's per-page bound on every authority (SQL and native); the offline conversion pages by it.
RECORD_LIST_PAGE = 1000
NOT_A_LEAD_SENDER = ('Only project leads (the Global Router and Project Routers) message other projects; '
                     'a Worker or other conversation messages its own project and relays through its lead')
NOT_A_LEAD_TARGET = ('Across projects only another project\u2019s lead is addressable (list them with '
                     'list_project_leads); that project\u2019s Workers stay with their own lead')
logger = get_logger(__name__)


class ProjectMessageError(ValueError):
    pass


def scope(owner: int, conversation_id: str, expected_project: str = '', *, reads=None) -> str:
    """The conversation's project; ``reads`` is a batched reader (``PrefetchedReads``)."""
    require_user_id(owner, context='project message owner')
    if not isinstance(conversation_id, str) or not 1 <= len(conversation_id) <= 256:
        raise ProjectMessageError('A bounded conversation ID is required')
    # Construct as callers patch it: with no argument unless a reader is supplied.
    repository = (ConversationRepository() if reads is None
                  else ConversationRepository(lambda write=False: reads))
    snapshot = repository.get(conversation_id, user_id=owner, include_messages=False)
    settings = snapshot.metadata.get('settings') if snapshot else None
    if not isinstance(settings, dict) or settings.get('archived') or settings.get('readOnly'):
        raise ProjectMessageError('Conversation is unavailable for project communication')
    project = normalize_project_path(settings.get('projectPath') or '')
    if not project or (expected_project and project != expected_project):
        raise ProjectMessageError('Conversation is not in the authorized project')
    return project


def lead_role(owner: int, conversation_id: str) -> tuple[str, str] | None:
    """``(role, project)`` while the conversation leads one of the owner's projects, else None.

    A lead is the available bound Coordinator of a project (its Project Router)
    or of the owner's router home (the Global Router); the Watchdog's path is
    not a project. Read live on every call, so a rebound lead is no longer one.
    """
    try:
        project = scope(owner, conversation_id)
    except ProjectMessageError:
        return None
    if not _leads(owner, conversation_id, project):
        return None
    from lib.router.home import is_router_home
    return ('global_router' if is_router_home(owner, project) else 'project_router'), project


def _leads(owner: int, conversation_id: str, project: str) -> bool:
    """Whether the conversation of ``project`` is that project's current lead."""
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.router.watchdog.binding import is_watchdog_location
    if is_watchdog_location(owner, project):
        return False
    binding = ProjectCoordinationRepository(owner).read(project)
    return binding['availability'] == 'available' and binding['conversationId'] == conversation_id


def _require_leads(owner: int, sender: str, sender_project: str, target: str, target_project: str) -> None:
    """A message or mailbox read across projects needs two current leads of the owner."""
    if not _leads(owner, sender, sender_project):
        raise ProjectMessageError(NOT_A_LEAD_SENDER)
    if not _leads(owner, target, target_project):
        raise ProjectMessageError(NOT_A_LEAD_TARGET)


def identity(task: dict, *, reads=None) -> tuple[int, str, str]:
    from lib.tasks_pkg.invocation import is_child_invocation
    from lib.tasks_pkg.manager import task_user_id
    if is_child_invocation(task) or task.get('_parentTaskId') or not task.get('convId'):
        raise ProjectMessageError('Only a project conversation parent may communicate; children must relay through their parent')
    if task.get('aborted') or task.get('_transientRuntime') or task.get('_invocationStorageFree'):
        raise ProjectMessageError('A current durable, non-stopped conversation is required')
    owner = task_user_id(task)
    project = scope(owner, task['convId'], reads=reads)
    configured = normalize_project_path((task.get('config') or {}).get('canonicalProjectPath') or
                                        (task.get('config') or {}).get('projectPath') or '')
    if configured != project:
        raise ProjectMessageError('The running task project no longer matches its conversation')
    return owner, task['convId'], project


def _pending(m: dict) -> bool:
    return m['state'] == 'submitted' and m.get('decision') is None


def _is_message_id(value) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _integers(value, *names) -> bool:
    return isinstance(value, dict) and all(
        isinstance(value.get(name), int) and not isinstance(value.get(name), bool) for name in names)


def _claim(locator) -> tuple[str, int]:
    """A message locator: ('ticket', n) for a send, ('segment', n) for a head-era append."""
    if isinstance(locator, dict) and len(locator) == 1:
        claimed = next(iter(locator))
        if claimed in ('ticket', 'segment') and _integers(locator, claimed) and locator[claimed] >= (claimed == 'ticket'):
            return claimed, locator[claimed]
    raise StorageError('database_integrity', 'Invalid project message locator')


def _valid_messages(messages) -> bool:
    if not isinstance(messages, list):
        return False
    seen = set()
    for m in messages:
        if (not isinstance(m, dict) or not isinstance(m.get('id'), str) or m['id'] in seen
                or m.get('state') not in INPUT_STATES
                or not isinstance(m.get('content'), str) or len(m['content']) > LIMITS['messageChars']
                or not isinstance(m.get('sender'), str) or not isinstance(m.get('revision'), int)
                or not isinstance(m.get('kind'), str)
                or m.get('kind') not in {'reference', 'consult', 'report', 'question', 'assign', 'steer'}
                or (m.get('decision') is not None and
                    (not isinstance(m.get('decision'), str) or m['decision'] not in DECISIONS))):
            return False
        seen.add(m['id'])
    return True


class ProjectMailbox:
    """Append-only, segmented history of one original owner/project/recipient.

    A send never writes the head. It takes a ticket (the version an
    unconditional write of the tickets record commits; each authority
    serializes writes to one record, so senders never conflict), claims its
    message locator and creates its immutable original at that ticket. The
    recipient's acquisition and receipts absorb arrivals into the head's open
    segment in ticket order, so the head has one writer. Sealing a full segment
    writes its copy, sender pointers and pending index before the head
    advances, so a sealed original stays point-readable at a stable position.
    Readers place arrivals not yet absorbed after the absorbed history, in
    ticket order, and never write.
    """
    def __init__(self, owner: int, project: str, recipient: str, *, client=None):
        require_user_id(owner, context='project mailbox')
        self.binding = {'owner': owner, 'project': project, 'recipient': recipient}
        self.key = hashlib.sha256(json.dumps(self.binding, sort_keys=True).encode()).hexdigest()
        self.size = LIMITS['messagesPerSegment']
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        self.client = client

    def _key(self, kind: str, **parts) -> str:
        return KEYS[kind].format(mailbox=self.key, **parts)

    def _get(self, key: str):
        row = self.client().query('record.get', {'namespace': CONTRACT['namespace'], 'key': key})
        if row is None:
            return None, 0
        if not isinstance(row, dict) or not isinstance(row.get('value'), dict):
            raise StorageError('database_integrity', 'Invalid project mailbox record')
        return copy.deepcopy(row['value']), int(row['version'])

    def _put(self, key: str, value: dict, version: int):
        """Write at ``version``; return the version committed when the receipt names it.

        A conflict whose record already holds exactly ``value`` is this write's
        outcome, committed by another writer of the same state (two recipient
        passes absorbing the same arrivals): it succeeds at the version read,
        with no second write. Any other conflict is raised.
        """
        try:
            receipt = self.client(write=True).command('record.put', {
                'namespace': CONTRACT['namespace'], 'key': key,
                'value': value, 'expected_version': version}, 'project-message:' + uuid.uuid4().hex)
        except StorageError as exc:
            if exc.code != 'database_conflict':
                raise
            current, current_version = self._get(key)
            if current != value:
                raise
            return current_version
        written = receipt.get('version') if isinstance(receipt, dict) else None
        return written if isinstance(written, int) and not isinstance(written, bool) else None

    def _create(self, key: str, value: dict):
        """Write one immutable index row; an identical earlier write is success."""
        try:
            self._put(key, value, 0)
        except StorageError as exc:
            if exc.code != 'database_conflict' and not exc.retryable:
                raise
            current, _ = self._get(key)
            if current is None:
                raise
            if current != value:
                raise StorageError('database_integrity', 'Project mailbox index changed') from exc

    def _settle(self, key: str, value: dict) -> dict:
        """Create ``key`` with ``value`` once; return what it holds afterwards, ours or an earlier write."""
        for _ in range(LIMITS['casAttempts']):
            try:
                self._put(key, value, 0)
                return value
            except StorageError as exc:
                if exc.code != 'database_conflict' and not exc.retryable:
                    raise
            current, _ = self._get(key)
            if current is not None:
                return current
        raise ProjectMessageError(UNCERTAIN)

    def head(self):
        """The absorbed history: open segment, sealed count and ticket state, normalized.

        A head without ``sealed`` is a legacy single-record mailbox: open
        segment 0 whose originals have no locators until it is sealed. A head
        with ``messages`` predates tickets, so its tickets start after its
        originals, or was converted back to that form for an earlier version.
        Writers that predate tickets keep head fields they do not know, so a
        converted head resumes the ``tickets`` and ``voided`` it carries: the
        next ticket lands after every original it holds, earlier writers'
        appends included. The recipient's next head write stores the ticketed
        form (open segment under ``open``), which writers that predate tickets
        refuse.
        """
        value, version = self._get(self.key)
        if value is None:
            value = {'binding': self.binding, 'messages': [], 'sealed': 0, 'unlocated': 0}
        if 'open' in value:
            head = {key: value.get(key) for key in
                    ('binding', 'sealed', 'unlocated', 'ticketBase', 'tickets', 'voided', 'stalled')}
            head.update(messages=value['open'], ticketed=True)
        else:
            messages = value.get('messages')
            if 'sealed' not in value and isinstance(messages, list):
                value = {**value, 'sealed': 0, 'unlocated': len(messages)}
            head = {'binding': value.get('binding'), 'messages': messages, 'sealed': value.get('sealed'),
                    'unlocated': value.get('unlocated'), 'ticketBase': None, 'tickets': value.get('tickets', 0),
                    'voided': value.get('voided', []), 'stalled': None, 'ticketed': False}
            if _integers(head, 'sealed', 'tickets') and isinstance(messages, list) and isinstance(head['voided'], list):
                head['ticketBase'] = (head['sealed'] * self.size + len(messages)
                                      - head['tickets'] + len(head['voided']))
        if not self._valid_head(head):
            raise StorageError('database_integrity', 'Invalid project mailbox')
        return head, version

    def _valid_head(self, head) -> bool:
        voided, stalled = head['voided'], head['stalled']
        return (head['binding'] == self.binding
                and _integers(head, 'sealed', 'unlocated', 'ticketBase', 'tickets')
                and min(head['sealed'], head['ticketBase'], head['tickets']) >= 0
                and _valid_messages(head['messages']) and len(head['messages']) <= self.size
                and 0 <= head['unlocated'] <= len(head['messages'])
                and isinstance(voided, list) and all(type(ticket) is int for ticket in voided)
                and voided == sorted(set(voided)) and all(1 <= ticket <= head['tickets'] for ticket in voided)
                and (stalled is None or type(stalled) is int and stalled == head['tickets'] + 1)
                and head['sealed'] * self.size + len(head['messages'])
                == head['ticketBase'] + head['tickets'] - len(voided))

    def _stored(self, head) -> dict:
        """The ticketed head record; writers that predate tickets find no ``messages`` and refuse it."""
        return {'binding': self.binding, 'open': head['messages'], 'sealed': head['sealed'],
                'unlocated': head['unlocated'], 'ticketBase': head['ticketBase'], 'tickets': head['tickets'],
                'voided': head['voided'], 'stalled': head['stalled']}

    @staticmethod
    def _position(head, ticket: int) -> int:
        """The stable position of an absorbed ticket that holds an original."""
        return head['ticketBase'] + ticket - 1 - bisect.bisect_left(head['voided'], ticket)

    def _segment(self, number: int):
        value, version = self._get(self._key('segment', segment=number))
        senders = value.get('senders') if value is not None else None
        if (value is None or value.get('binding') != self.binding or value.get('segment') != number
                or not _integers(value, 'sourceRevision') or not _valid_messages(value.get('messages'))
                or len(value['messages']) != self.size or not isinstance(senders, dict)
                or set(senders) != {m['sender'] for m in value['messages']}
                or not all(_integers(link, 'count', 'cumulative', 'previous') for link in senders.values())):
            raise StorageError('database_integrity', 'Invalid project mailbox segment')
        return value, version

    def _where(self, head, ids) -> dict[str, tuple[str, int]]:
        """Where each original outside the open segment lives: ('segment', n) or ('arrival', ticket).

        Callers read the head first and search its open segment themselves. A
        locator naming nothing committed (a crashed head-era append, a voided
        ticket) leaves its ID absent.
        """
        where = {}
        for message_id in ids:
            if not _is_message_id(message_id):
                continue
            locator, _ = self._get(self._key('message', message=message_id))
            if locator is None:
                continue
            claimed, number = _claim(locator)
            if claimed == 'segment':
                if number < head['sealed']:
                    where[message_id] = ('segment', number)
            elif number > head['tickets']:
                where[message_id] = ('arrival', number)
            else:
                index = bisect.bisect_left(head['voided'], number)
                if index == len(head['voided']) or head['voided'][index] != number:
                    position = self._position(head, number)
                    if position < head['sealed'] * self.size:
                        where[message_id] = ('segment', position // self.size)
        return where

    def locate(self, head, ids, tail=None) -> dict[str, tuple[int, dict]]:
        """Exact originals with their stable positions, consistent with ``head``.

        Arrivals not yet absorbed take the positions after the absorbed history
        in ticket order (``tail``, read when one is asked for).
        """
        base = head['sealed'] * self.size
        found = {m['id']: (base + i, m) for i, m in enumerate(head['messages']) if m['id'] in ids}
        where = self._where(head, set(ids) - found.keys())
        groups = {}
        for message_id, (claimed, number) in where.items():
            if claimed == 'segment':
                groups.setdefault(number, set()).add(message_id)
        for number, group in sorted(groups.items()):
            for i, m in enumerate(self._segment(number)[0]['messages']):
                if m['id'] in group:
                    found[m['id']] = (number * self.size + i, m)
        arriving = {message_id for message_id, (claimed, _) in where.items() if claimed == 'arrival'}
        if arriving:
            start = base + len(head['messages'])
            for i, m in enumerate(self._tail(head) if tail is None else tail):
                if m['id'] in arriving:
                    found[m['id']] = (start + i, m)
        return found

    def get(self, message_id: str):
        return self.find({message_id}).get(message_id)

    def find(self, ids) -> dict[str, dict]:
        """Exact originals by ID; an arrival not yet absorbed is read alone, without a position."""
        head, _ = self.head()
        ids = set(ids)
        found = {m['id']: m for m in head['messages'] if m['id'] in ids}
        groups = {}
        for message_id, (claimed, number) in self._where(head, ids - found.keys()).items():
            if claimed == 'segment':
                groups.setdefault(number, set()).add(message_id)
                continue
            arrival = self._arrival(number)
            if arrival is not None and arrival != VOID and arrival['id'] == message_id:
                found[message_id] = arrival
        for number, group in sorted(groups.items()):
            for m in self._segment(number)[0]['messages']:
                if m['id'] in group:
                    found[m['id']] = m
        return found

    def read(self, start: int = 0, end: int | None = None, head=None, tail=None) -> list[dict]:
        """Originals at stable positions ``[start, end)`` in submission order."""
        if head is None:
            head, _ = self.head()
        if tail is None:
            tail = self._tail(head)
        sealed_end = head['sealed'] * self.size
        total = sealed_end + len(head['messages']) + len(tail)
        end = total if end is None else min(end, total)
        rows, position = [], max(0, start)
        while position < min(end, sealed_end):
            number, offset = divmod(position, self.size)
            stop = min(end, (number + 1) * self.size)
            rows += self._segment(number)[0]['messages'][offset:stop - number * self.size]
            position = stop
        if end > sealed_end:
            rows += (head['messages'] + tail)[max(position, sealed_end) - sealed_end:end - sealed_end]
        return rows

    def page(self, before_id: str = '', sender: str = '') -> dict:
        """Newest directory page older than ``before_id``: all incoming or one sender's.

        ``total`` counts every matching original older than the cursor.
        """
        head, revision = self.head()
        tail = self._tail(head)
        if sender:
            rows, total = self._sender_page(head, tail, before_id, sender)
        else:
            total = (self._cursor(head, tail, before_id, '') if before_id
                     else head['sealed'] * self.size + len(head['messages']) + len(tail))
            rows = self.read(max(0, total - PAGE_MESSAGES), total, head, tail)
        return {'messages': rows, 'total': total, 'revision': revision}

    def _cursor(self, head, tail, before_id: str, sender: str) -> int:
        found = self.locate(head, {before_id}, tail).get(before_id)
        if found is None or (sender and found[1]['sender'] != sender):
            raise ProjectMessageError('Pagination ID is not in this scoped mailbox')
        return found[0]

    def _sender_page(self, head, tail, before_id: str, sender: str):
        """One sender's newest page through its linked sealed segments, not a scan."""
        base = head['sealed'] * self.size
        own = [(base + i, m) for i, m in enumerate(head['messages'] + tail) if m['sender'] == sender]
        cursor = self._cursor(head, tail, before_id, sender) if before_id else None
        if cursor is not None and cursor < base:
            number = cursor // self.size
            segment = self._segment(number)[0]
            link = segment['senders'][sender]
            own = [(number * self.size + i, m) for i, m in enumerate(segment['messages'])
                   if m['sender'] == sender and number * self.size + i < cursor]
            total = link['cumulative'] - link['count'] + len(own)
            previous = link['previous']
        else:
            if cursor is not None:
                own = [row for row in own if row[0] < cursor]
            previous, sealed_total = self._sender_newest(sender, head['sealed'])
            total = sealed_total + len(own)
        rows = [m for _, m in own[-PAGE_MESSAGES:]]
        while len(rows) < PAGE_MESSAGES and previous >= 0:
            segment = self._segment(previous)[0]
            older = [m for m in segment['messages'] if m['sender'] == sender]
            rows = older[-(PAGE_MESSAGES - len(rows)):] + rows
            previous = segment['senders'][sender]['previous']
        return rows, total

    def _sender_pointer(self, sender: str):
        """The sender's newest sealed segment and its link, written after that segment copy."""
        pointer, version = self._get(self._key('sender', sender=_digest(sender)))
        if pointer is not None and (pointer.get('sender') != sender or not _integers(
                pointer, 'segment', 'count', 'cumulative', 'previous')):
            raise StorageError('database_integrity', 'Invalid project mailbox sender pointer')
        return pointer, version

    def _sender_newest(self, sender: str, sealed: int):
        """(newest segment below ``sealed`` holding the sender or -1, cumulative count through it).

        The pointer may name a segment whose seal has not advanced this head view.
        """
        pointer, _ = self._sender_pointer(sender)
        if pointer is None:
            return -1, 0
        number, count, cumulative, previous = (pointer[k] for k in ('segment', 'count', 'cumulative', 'previous'))
        while number >= sealed:
            number, cumulative = previous, cumulative - count
            if number < 0:
                return -1, 0
            if number >= sealed:
                link = self._segment(number)[0]['senders'][sender]
                count, previous = link['count'], link['previous']
        return number, cumulative

    def _point_sender(self, sender: str, number: int, link: dict):
        """Advance the sender pointer from ``link['previous']`` to sealed segment ``number``."""
        key = self._key('sender', sender=_digest(sender))
        for _ in range(LIMITS['casAttempts']):
            pointer, version = self._sender_pointer(sender)
            current = pointer['segment'] if pointer is not None else -1
            if current == number:
                return
            if current != link['previous']:
                raise StorageError('database_conflict', 'Project mailbox sender pointer moved')
            try:
                self._put(key, {'sender': sender, 'segment': number, **link}, version)
                return
            except StorageError as exc:
                if exc.code != 'database_conflict' and not exc.retryable:
                    raise
        raise StorageError('database_conflict', 'Concurrent project mailbox sender pointer update')

    def _pending_index(self):
        value, version = self._get(self._key('pending'))
        if value is None:
            return {}, 0
        senders = value.get('senders')
        if (value.get('binding') != self.binding or not isinstance(senders, dict)
                or not all(isinstance(owned, list) and all(
                    isinstance(p, int) and not isinstance(p, bool) for p in owned) for owned in senders.values())):
            raise StorageError('database_integrity', 'Invalid project mailbox pending index')
        return senders, version

    def _sync_pending(self, number: int, messages: list[dict]):
        """Make the pending index exact for one sealed segment's observed receipts.

        Pending only ever ends, so an older observation can re-add a consumed
        position (readers verify it) but never drops one still pending.
        """
        start = number * self.size
        observed = {}
        for i, m in enumerate(messages):
            if _pending(m):
                observed.setdefault(m['sender'], []).append(start + i)
        for _ in range(LIMITS['casAttempts']):
            senders, version = self._pending_index()
            updated = {}
            for sender, owned in senders.items():
                kept = [p for p in owned if not start <= p < start + self.size]
                if kept:
                    updated[sender] = kept
            for sender, owned in observed.items():
                updated[sender] = sorted(updated.get(sender, []) + owned)
            if updated == senders:
                return
            try:
                self._put(self._key('pending'), {'binding': self.binding, 'senders': updated}, version)
                return
            except StorageError as exc:
                if exc.code != 'database_conflict' and not exc.retryable:
                    raise
        raise StorageError('database_conflict', 'Concurrent project mailbox pending index update')

    def _drawn(self) -> int:
        """Tickets drawn so far: the tickets record's version, 0 before the first send."""
        return self._get(self._key('tickets'))[1]

    def _draw(self, message_id: str, after: int) -> int:
        """A new ticket: the version an unconditional write of the tickets record commits.

        The command ID is fixed per message and voided predecessor, so an
        uncertain draw replays its ticket instead of drawing another.
        """
        command = 'project-message-ticket:' + _digest(f'{self.key}:{message_id}:{after}')
        for _ in range(LIMITS['casAttempts']):
            try:
                receipt = self.client(write=True).command('record.put', {
                    'namespace': CONTRACT['namespace'], 'key': self._key('tickets'),
                    'value': {'messageId': message_id}}, command)
            except StorageError as exc:
                if not exc.retryable:
                    raise
                continue
            ticket = receipt.get('version') if isinstance(receipt, dict) else None
            if not isinstance(ticket, int) or isinstance(ticket, bool) or ticket < 1:
                raise StorageError('database_integrity', 'Invalid project mailbox ticket')
            return ticket
        raise ProjectMessageError(UNCERTAIN)

    def _move_claim(self, key: str, ticket: int, version: int) -> bool:
        """Point the message locator at ``ticket`` from ``version``; False when another attempt moved it."""
        for _ in range(LIMITS['casAttempts']):
            try:
                self._put(key, {'ticket': ticket}, version)
                return True
            except StorageError as exc:
                if exc.code != 'database_conflict' and not exc.retryable:
                    raise
            current, current_version = self._get(key)
            if current == {'ticket': ticket}:
                return True
            if current_version != version:
                return False
        raise ProjectMessageError(UNCERTAIN)

    def _arrival(self, ticket: int):
        """The original at ``ticket``, VOID, or None while its sender has not written it."""
        value, _ = self._get(self._key('arrival', ticket=ticket))
        if value is not None and value != VOID and not _valid_messages([value]):
            raise StorageError('database_integrity', 'Invalid project mailbox arrival')
        return value

    def _fill(self, ticket: int, value: dict) -> dict:
        """Create the arrival at ``ticket`` once; return what it holds: an original or VOID."""
        stored = self._settle(self._key('arrival', ticket=ticket), value)
        if stored != VOID and not _valid_messages([stored]):
            raise StorageError('database_integrity', 'Invalid project mailbox arrival')
        return stored

    def _tail(self, head) -> list[dict]:
        """Originals drawn after the absorbed history, in ticket order.

        Missing and void tickets are skipped, and so is a copy of an original the
        history already holds (a retry of a head-era original made before its
        locator existed).
        """
        rows, seen = [], {m['id'] for m in head['messages']}
        for ticket in range(head['tickets'] + 1, self._drawn() + 1):
            arrival = self._arrival(ticket)
            if arrival is not None and arrival != VOID and arrival['id'] not in seen:
                seen.add(arrival['id'])
                rows.append(arrival)
        return rows

    def _locate_head_era(self, head):
        """Give head-era originals without a locator theirs before the head is stored in ticketed form.

        A retry of one of them that claimed a ticket first yields to it: the
        original committed first, and absorption drops the copy (``_absorb_into``).
        """
        for m in head['messages'][:head['unlocated']]:
            key = self._key('message', message=m['id'])
            for _ in range(LIMITS['casAttempts']):
                current, version = self._get(key)
                if current == {'segment': head['sealed']}:
                    break
                if current is not None and _claim(current)[0] != 'ticket':
                    raise StorageError('database_integrity', 'Project mailbox index changed')
                try:
                    self._put(key, {'segment': head['sealed']}, version)
                    break
                except StorageError as exc:
                    if exc.code != 'database_conflict' and not exc.retryable:
                        raise
            else:
                raise ProjectMessageError(UNCERTAIN)
        head['unlocated'] = 0

    def _seal(self, head, revision: int):
        """Seal the full open segment; every write precedes the advancing head CAS."""
        number, batch = head['sealed'], head['messages'][:self.size]
        counts = {}
        for m in batch:
            counts[m['sender']] = counts.get(m['sender'], 0) + 1
        links = {}
        for sender, count in counts.items():
            pointer, _ = self._sender_pointer(sender)
            if pointer is None:
                links[sender] = {'count': count, 'cumulative': count, 'previous': -1}
            elif pointer['segment'] < number:
                links[sender] = {'count': count, 'cumulative': pointer['cumulative'] + count,
                                 'previous': pointer['segment']}
            elif pointer['segment'] == number:  # an interrupted seal already advanced it
                links[sender] = {k: pointer[k] for k in ('count', 'cumulative', 'previous')}
            else:
                raise StorageError('database_conflict', 'Project mailbox segment was already sealed')
        key = self._key('segment', segment=number)
        current, version = self._get(key)
        if current is not None and not _integers(current, 'sourceRevision'):
            raise StorageError('database_integrity', 'Invalid project mailbox segment')
        if current is not None and current['sourceRevision'] > revision:
            raise StorageError('database_conflict', 'A newer head revision is sealing this segment')
        # A committed segment's copy names the head revision that advanced past
        # it, so a stale sealer can never overwrite receipts recorded since.
        if current is None or current['sourceRevision'] < revision:
            self._put(key, {'binding': self.binding, 'segment': number, 'sourceRevision': revision,
                            'messages': batch, 'senders': links}, version)
        for m in batch[:head['unlocated']]:
            self._create(self._key('message', message=m['id']), {'segment': number})
        for sender, link in links.items():
            self._point_sender(sender, number, link)
        self._sync_pending(number, batch)
        head['messages'] = head['messages'][self.size:]
        head['sealed'] = number + 1
        head['unlocated'] = max(0, head['unlocated'] - self.size)

    def absorb(self):
        """The recipient's write: absorb drawn arrivals into the history in ticket order.

        Each head CAS takes at most the open segment's room, sealing a full
        segment first. A missing original stops the pass and is remembered as
        ``stalled``; the next pass that still finds it missing voids that
        ticket, so absorption never waits for a sender (a live one draws again).
        Returns the head and revision it ended with.
        """
        head, revision = self.head()
        drawn, conflicts = self._drawn(), 0
        while True:
            outcome = self._absorb_into(head, revision, drawn)
            if outcome is None:
                return head, revision
            try:
                written = self._put(self.key, self._stored(head), revision)
            except StorageError as exc:
                if exc.code != 'database_conflict' and not exc.retryable:
                    raise
                conflicts += 1
                if conflicts >= LIMITS['casAttempts']:
                    raise ProjectMessageError(UNCERTAIN) from exc
                head, revision = self.head()
                continue
            if written is None:
                head, revision = self.head()
            else:
                head['ticketed'], revision = True, written
            if outcome == 'stalled':
                return head, revision

    def convert_back(self) -> dict:
        """Store this mailbox in the head-era form that writers before tickets read; offline only.

        The application is stopped, so no sender draws or writes meanwhile.
        Every drawn ticket is absorbed (one never written is voided), every
        ``{ticket}`` claim is pointed at its original's segment (segment 0 when
        its ticket holds no original: those writers prove it dangling and
        replace it on retry), and the head is stored last under ``messages``
        with every original located. It keeps ``tickets`` and ``voided``, which
        those writers preserve, so this version resumes them (``head``).
        Tickets and arrivals stay. Converting a converted mailbox, or one that
        never took a ticket, writes nothing. Returns what this call changed.
        """
        before = self.head()[0]['tickets']
        head, revision = self._absorb_drawn()
        claims = self._claims_to_segments(head)
        if head['ticketed']:
            self._put(self.key, {'binding': self.binding, 'messages': head['messages'], 'sealed': head['sealed'],
                                 'unlocated': head['unlocated'], 'ticketBase': head['ticketBase'],
                                 'tickets': head['tickets'], 'voided': head['voided'], 'stalled': None}, revision)
        return {'tickets': head['tickets'] - before, 'claims': claims, 'head': head['ticketed']}

    def _absorb_drawn(self):
        """Absorb every drawn ticket; with no sender live, a pass's stall is voided by the next pass."""
        head, revision = self.absorb()
        while head['tickets'] < self._drawn():
            progress = head['tickets'], head['stalled']
            head, revision = self.absorb()
            if (head['tickets'], head['stalled']) == progress:
                raise ProjectMessageError(UNCERTAIN)
        return head, revision

    def _claims_to_segments(self, head) -> int:
        """Point each ``{ticket}`` claim of this mailbox at a segment; return how many moved."""
        prefix, after, moved = self._key('message', message=''), '', 0
        while True:
            page = self.client().query('record.list', {'namespace': CONTRACT['namespace'], 'prefix': prefix,
                                                       'after_key': after, 'limit': RECORD_LIST_PAGE})
            if not isinstance(page, list) or len(page) > RECORD_LIST_PAGE:
                raise StorageError('database_integrity', 'Invalid project mailbox claim page')
            if not page:
                return moved
            for row in page:
                key = row.get('key')
                if not isinstance(key, str) or not key.startswith(prefix) or key <= after:
                    raise StorageError('database_integrity', 'Invalid project mailbox claim page')
                after = key
                claimed, ticket = _claim(row.get('value'))
                if claimed == 'ticket':
                    segment = self._claimed_segment(head, key[len(prefix):], ticket)
                    self._put(key, {'segment': segment}, row.get('version'))
                    moved += 1

    def _claimed_segment(self, head, message_id: str, ticket: int) -> int:
        """The segment holding the original ``ticket`` carries for ``message_id``; 0 when it carries none."""
        index = bisect.bisect_left(head['voided'], ticket)
        if ticket > head['tickets'] or (index < len(head['voided']) and head['voided'][index] == ticket):
            return 0
        arrival = self._arrival(ticket)
        if arrival is None or arrival == VOID or arrival['id'] != message_id:
            return 0
        return self._position(head, ticket) // self.size

    def _absorb_into(self, head, revision: int, drawn: int):
        """Absorb into ``head`` in place: None (nothing drawn), 'absorbed' or 'stalled'.

        An original counts only at the ticket its locator claims and only once;
        a ticket holding anything else is voided like one never written.
        """
        if head['tickets'] >= drawn:
            return None
        self._locate_head_era(head)
        if len(head['messages']) >= self.size:
            self._seal(head, revision)
        ticket = head['tickets'] + 1
        while ticket <= drawn and len(head['messages']) < self.size:
            arrival = self._arrival(ticket)
            if arrival is None:
                if head['stalled'] != ticket:
                    head['stalled'] = ticket
                    return 'stalled'
                arrival = self._fill(ticket, VOID)
            if (arrival == VOID or any(m['id'] == arrival['id'] for m in head['messages'])
                    or self._get(self._key('message', message=arrival['id']))[0] != {'ticket': ticket}):
                head['voided'].append(ticket)
            else:
                head['messages'].append(arrival)
            head['tickets'], head['stalled'] = ticket, None
            ticket += 1
        return 'absorbed'

    def observe(self):
        """``(version, pending originals)`` for a reader that never absorbs: history first, then drawn arrivals.

        A send only draws a ticket and writes its arrival; the head changes when
        the recipient absorbs it. ``version`` therefore names the head revision
        and the unabsorbed arrivals, so an equal version means the same pending
        originals. Originals are yielded lazily.
        """
        head, revision = self.head()
        tail = self._tail(head)

        def originals():
            for _position, m in self.pending(head, lambda _sender: True, set()):
                yield m
            yield from (m for m in tail if _pending(m))
        return (revision, tuple(m['id'] for m in tail)), originals()

    def pending(self, head, eligible, exclude):
        """Submitted, unconsidered originals of eligible senders, oldest first.

        Yields ``(position, message)`` lazily so callers read only what they
        use. Sealed candidates come from the pending index; ineligible senders
        are skipped without reading their segments.
        """
        sealed_end = head['sealed'] * self.size
        if head['sealed']:
            senders = self._pending_index()[0]
            positions = sorted(position for sender, owned in senders.items() if eligible(sender)
                               for position in owned if position < sealed_end)
            number, rows = None, []
            for position in positions:
                if position // self.size != number:
                    number = position // self.size
                    rows = self._segment(number)[0]['messages']
                m = rows[position % self.size]
                if _pending(m) and m['id'] not in exclude and eligible(m['sender']):
                    yield position, m
        for i, m in enumerate(head['messages']):
            if _pending(m) and m['id'] not in exclude and eligible(m['sender']):
                yield sealed_end + i, m

    def _apply(self, ids: set, mutate) -> dict[str, dict]:
        """CAS receipt changes onto exact originals, absorbing arrivals first (the recipient's write)."""
        for _ in range(LIMITS['casAttempts']):
            head, revision = self.absorb()
            rows = [m for m in head['messages'] if m['id'] in ids]
            if [m for m in rows if mutate(m)]:
                self._locate_head_era(head)
                try:
                    self._put(self.key, self._stored(head), revision)
                except StorageError as exc:
                    if exc.code != 'database_conflict' and not exc.retryable:
                        raise
                    continue
            break
        else:
            raise ProjectMessageError(UNCERTAIN)
        result = {m['id']: m for m in rows}
        groups = {}
        for message_id, (claimed, number) in self._where(head, ids - result.keys()).items():
            if claimed == 'arrival':
                raise ProjectMessageError(UNCERTAIN)  # behind a stalled ticket until a later pass
            groups.setdefault(number, set()).add(message_id)
        for number, group in sorted(groups.items()):
            result.update(self._apply_sealed(number, group, mutate))
        return result

    def _apply_sealed(self, number: int, ids: set, mutate) -> dict[str, dict]:
        for _ in range(LIMITS['casAttempts']):
            segment, version = self._segment(number)
            rows = [m for m in segment['messages'] if m['id'] in ids]
            if not [m for m in rows if mutate(m)]:
                return {m['id']: m for m in rows}
            try:
                self._put(self._key('segment', segment=number), segment, version)
            except StorageError as exc:
                if exc.code != 'database_conflict' and not exc.retryable:
                    raise
                continue
            try:
                self._sync_pending(number, segment['messages'])
            except StorageError:
                # The index is a verified superset: a stale entry costs a read, never delivery.
                logger.warning('Project mailbox pending index sync deferred mailbox=%s segment=%s',
                               self.key, number, exc_info=True)
            return {m['id']: m for m in rows}
        raise ProjectMessageError(UNCERTAIN)

    def _head_era_original(self, number: int, message_id: str):
        """The original a head-era append committed under segment ``number``, or None once it never can.

        A head that predates tickets may still take that append from a writer of
        the earlier version; storing the head in ticketed form refuses them, so
        the claim is then provably unfilled.
        """
        head, revision = self.head()
        if number < head['sealed']:
            return next((m for m in self._segment(number)[0]['messages'] if m['id'] == message_id), None)
        found = next((m for m in head['messages'] if m['id'] == message_id), None)
        if found is not None or head['ticketed']:
            return found
        self._locate_head_era(head)
        try:
            self._put(self.key, self._stored(head), revision)
        except StorageError as exc:
            if exc.code != 'database_conflict' and not exc.retryable:
                raise
            raise ProjectMessageError(UNCERTAIN) from exc
        return None

    def submit(self, sender: str, args: dict, task_id: str, *, sender_project: str = ''):
        """Append one original at a ticket of its own; the head is never written.

        ``sender_project`` marks a lead's cross-project message. Idempotent by
        message ID: a retry finds the locator its first attempt claimed and
        returns the same original, completing the write the first attempt may
        have lost. A claimed ticket found void (its sender was lost before
        writing it) is replaced by a new ticket, so the original lands exactly once.
        """
        text, key, kind = args.get('content'), args.get('idempotency_key'), args.get('kind', 'reference')

        if isinstance(text, str):
            try:
                text.encode('utf-8')
            except UnicodeEncodeError as error:
                raise ProjectMessageError('Message is not valid UTF-8 text') from error
        if (not isinstance(text, str) or not text.strip() or len(text) > LIMITS['messageChars']
                or len(text.encode()) > LIMITS['messageBytes'] or not isinstance(key, str) or not 1 <= len(key) <= 256
                or kind not in {'reference', 'consult', 'report', 'question', 'assign', 'steer'}):
            raise ProjectMessageError('Invalid message content, kind or idempotency identity')
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, self.key + ':' + sender + ':' + key))
        body = {'sender': sender, 'content': text, 'kind': kind, 'replyTo': args.get('reply_to') or ''}
        if args.get('attachments'):
            body['attachments'] = copy.deepcopy(args['attachments'])
        if sender_project:
            body['senderProject'] = sender_project
        message = {**body, 'id': message_id, 'state': 'submitted', 'revision': 1,
                   'senderTaskId': task_id, 'submittedAt': int(time.time() * 1000), 'decision': None}

        def same(old):
            if old['id'] != message_id:
                raise StorageError('database_integrity', 'Project mailbox ticket holds another original')
            if any(old.get(k) != v for k, v in body.items()):
                raise ProjectMessageError('Idempotency key reused with changed message')
            return old
        locator_key = self._key('message', message=message_id)
        voided = 0
        for _ in range(LIMITS['casAttempts']):
            locator, version = self._get(locator_key)
            if locator is not None:
                claimed, number = _claim(locator)
                if claimed == 'segment':
                    old = self._head_era_original(number, message_id)
                    if old is not None:
                        return same(old)
                elif number != voided:
                    stored = self._fill(number, message)
                    if stored != VOID:
                        return same(stored)
                    voided = number
            ticket = self._draw(message_id, voided)
            if not self._move_claim(locator_key, ticket, version):
                self._fill(ticket, VOID)  # never claimed: give it up so absorption never waits on it
                continue
            stored = self._fill(ticket, message)
            if stored != VOID:
                return same(stored)
            voided = ticket
        raise ProjectMessageError(UNCERTAIN)

    def confirm(self, ids: list[str], task_id: str, round_num: int, *, turn_id='', attempt_id=''):
        wanted = set(ids)
        def ack(m):
            if m['id'] not in wanted or m['state'] != 'submitted':
                return False
            now = int(time.time() * 1000)
            m.update(state='input_confirmed', inputTaskId=task_id, inputRound=round_num + 1,
                     inputConfirmedAt=now, revision=m['revision'] + 1,
                     inputTurnId=turn_id, inputAttemptId=attempt_id)
            if m['decision'] is None:
                # Accepted on delivery (owner decision 2026-11-18): rejected, deferred
                # and conflict stay explicit, decided against the revision delivered.
                m.update(decision='accepted', decisionNote=AUTO_ACCEPT_NOTE, decisionTaskId=task_id,
                         consideredAt=now, decisionAuto=True, deliveredRevision=m['revision'] - 1)
            return True
        self._apply(wanted, ack)

    def consider(self, args: dict, task_id: str):
        decision, note, expected = args.get('decision'), args.get('note'), args.get('expected_revision')
        if (decision not in DECISIONS or not isinstance(note, str) or len(note) > 2048
                or isinstance(expected, bool) or not isinstance(expected, int) or expected < 1):
            raise ProjectMessageError('Invalid consideration decision/revision')
        message_id = args.get('message_id')
        def considered(m):
            if m['decision'] == decision and m.get('decisionNote') == note and m.get('decisionTaskId') == task_id:
                return False
            # The revision its delivery showed stays valid while only the delivery
            # itself (input confirmation and acceptance on delivery) changed it.
            if m['revision'] != expected and not (m.get('decisionAuto') and expected == m.get('deliveredRevision')):
                raise ProjectMessageError('Message revision changed; read before deciding again')
            m.pop('decisionAuto', None)
            m.update(decision=decision, decisionNote=note, decisionTaskId=task_id,
                     consideredAt=int(time.time() * 1000), revision=m['revision'] + 1)
            return True
        changed = self._apply({message_id}, considered)
        if message_id not in changed:
            raise ProjectMessageError('Incoming message not found')
        return changed[message_id]


def execute(name: str, args: dict, task: dict) -> dict:
    owner, sender, project = identity(task)
    from jsonschema import Draft202012Validator
    tool = next(t for t in TOOLS if t['function']['name'] == name)
    if list(Draft202012Validator(tool['function']['parameters']).iter_errors(args)):
        raise ProjectMessageError('Arguments do not match the project message contract')
    if name == 'send_project_message':
        target = args['conversation_id']
        target_project = scope(owner, target)
        if target == sender:
            raise ProjectMessageError('A conversation cannot send messages to itself')
        if target_project != project:
            _require_leads(owner, sender, project, target, target_project)
        if args.get('reply_to'):
            source = ProjectMailbox(owner, project, sender).get(args['reply_to'])
            if source is None or source['sender'] != target:
                raise ProjectMessageError('reply_to must identify a received message from this target')
        if args.get('attachments'):
            from .lead_attachments import admit
            args = {**args, 'attachments': admit(task, owner, args['attachments'])}
        m = ProjectMailbox(owner, target_project, target).submit(
            sender, args, task['id'], sender_project=project if target_project != project else '')
        return {'message': m, 'execution_started': False, 'wake': wake_idle_lead(owner, target)}
    if name == 'consider_project_message':
        return {'message': ProjectMailbox(owner, project, sender).consider(args, task['id']), 'execution_started': False}
    target, target_project = sender, project
    direction = args.get('direction', 'incoming')
    if direction == 'outgoing':
        target = args.get('conversation_id')
        target_project = scope(owner, target)
        if target_project != project:
            _require_leads(owner, sender, project, target, target_project)
    mailbox = ProjectMailbox(owner, target_project, target)
    author = sender if direction == 'outgoing' else ''
    if args.get('message_id'):
        head, revision = mailbox.head()
        found = mailbox.locate(head, {args['message_id']}).get(args['message_id'])
        if found is None or (author and found[1]['sender'] != author):
            raise ProjectMessageError('Message not found in this scoped mailbox')
        rows = [found[1]]
        if args.get('before_id'):
            if args['before_id'] != rows[0]['id']:
                raise ProjectMessageError('Pagination ID is not in this scoped mailbox')
            rows = []
        total = len(rows)
    else:
        page = mailbox.page(args.get('before_id') or '', author)
        total, revision = page['total'], page['revision']
        # A directory is bounded context; full immutable originals require exact ID.
        rows = [{**m, 'content': m['content'][:256], 'contentChars': len(m['content']),
                 'contentTruncated': len(m['content']) > 256} for m in page['messages']]

    def view(rows):
        return {'conversation_id': target, 'mailbox_revision': revision, 'messages': rows,
                'total_messages': total, 'older_messages_omitted': total - len(rows),
                'next_before_id': rows[0]['id'] if total > len(rows) and rows else None,
                'execution_started': False}
    # Keep the newest rows the model reads inline: a page over the single
    # tool-result budget would reach it only as an artifact pointer, while
    # next_before_id already reaches every older message.
    from lib.tasks_pkg.compaction.api import fits_tool_result_budget
    while len(rows) > 1 and not fits_tool_result_budget(
            json.dumps(view(rows), ensure_ascii=False), model=task.get('model', '')):
        rows = rows[1:]
    return view(rows)


def page_view(owner: int, project: str, recipient: str, before_id: str = '') -> dict:
    project = normalize_project_path(project)
    scope(owner, recipient, project)
    page = ProjectMailbox(owner, project, recipient).page(before_id)
    rows = page['messages']
    fields = CONTRACT['$defs']['messageView']['properties']
    return {
        'messages': [{**{key: row[key] for key in fields if key in row},
                      'content': row['content'][:256]} for row in rows],
        'next_before_id': rows[0]['id'] if page['total'] > len(rows) else None,
    }


def _watchdog_sender(owner: int, sender: str) -> bool:
    """The owner's currently bound Router Watchdog may message every project of
    that owner (docs/ROUTER.md R7); its messages stay nonhuman context."""
    from lib.router.watchdog.binding import router_watchdog
    binding = router_watchdog(owner, create=False)
    return binding['availability'] == 'available' and binding['conversationId'] == sender


def validate_sender(owner: int, sender: str, project: str, *, recipient: str) -> None:
    """Check current sender authority at acquisition and before each send.

    A same-project sender; the owner's bound Watchdog (R7); or another
    project's current lead while ``recipient`` currently leads ``project`` (R13).
    The sender's metadata is read once.
    """
    try:
        sender_project = scope(owner, sender)
    except ProjectMessageError:
        if _watchdog_sender(owner, sender):
            return
        raise
    if sender_project == project or _watchdog_sender(owner, sender):
        return
    if not (_leads(owner, sender, sender_project) and _leads(owner, recipient, project)):
        raise ProjectMessageError('Conversation is not in the authorized project')


AUTO_ACCEPT_NOTE = 'Accepted on delivery.'
_WAKE_HEADER = '[Project message wake: nonhuman notice, not the owner\u2019s words and not authority]'


def render_wake_notice(count: int) -> str:
    return '\n'.join([
        _WAKE_HEADER,
        f'{count} project message(s) wait for you; they are delivered right after this notice. '
        'Act on what they ask within your own authority, or end the Turn when nothing is needed.'])


def wake_idle_lead(owner: int, recipient: str, *, dispatch=None) -> str:
    """Start one Turn of an idle lead whose mailbox holds pending messages; returns the outcome.

    Only a current lead is woken (a Worker reads its messages at its next step).
    A running lead Turn receives them at its next round, and its poll ends for
    them (``wait_interruption``), so a busy lane is left alone: the default
    dispatch mode starts nothing there (``busy``) instead of steering a
    second notice. One wake per lead and pending frontier: the command id names
    the newest pending message, so a repeated send, the lead's settlement check
    and a retry start at most one Turn for the same pending messages
    (docs/PROJECT_COORDINATION.md "Messages wake an idle lead"). The Global
    Router is not woken during quiet hours, as its relays are not (ROUTER.md R10).
    Never raises into the sender: the message is already durable.
    """
    try:
        role = lead_role(owner, recipient)
        if role is None:
            return 'next_step'
        kind, project = role
        if kind == 'global_router':
            from lib.router.proactive.held import quiet_hours_active
            if quiet_hours_active():
                return 'quiet_hours'
        mailbox = ProjectMailbox(owner, project, recipient)
        _, originals = mailbox.observe()
        validity = {}

        def eligible(sender):
            if sender not in validity:
                try:
                    validate_sender(owner, sender, project, recipient=recipient)
                    validity[sender] = True
                except ProjectMessageError:
                    validity[sender] = False
            return validity[sender]
        pending = [m for m in originals if eligible(m['sender'])]
        if not pending:
            return 'nothing_pending'
        newest = max(pending, key=lambda m: (m['submittedAt'], m['id']))
        from lib.turn_initiation import INITIATOR_PEER, stamp_initiator
        message = stamp_initiator({'role': 'user', 'content': render_wake_notice(len(pending)),
                                   'origin': {'initiator': INITIATOR_PEER,
                                              'sourceConversationId': newest['sender']}}, INITIATOR_PEER)
        if dispatch is None:
            from lib.scheduler.conversation_dispatch import dispatch_scheduled_turn as dispatch
        command_id = 'project-message-wake:' + hashlib.sha256(json.dumps(
            [owner, recipient, newest['id']], separators=(',', ':')).encode()).hexdigest()
        return dispatch(recipient, message, {}, user_id=owner, command_id=command_id,
                        log_prefix='[ProjectMessageWake]').disposition
    except Exception:
        logger.warning('Project message wake deferred to the lead\'s next Turn recipient=%s', recipient,
                       exc_info=True)
        return 'next_authorized_turn'


def inject(task: dict, messages: list[dict], round_num: int):
    task.pop('_projectMessageAttempt', None)
    if not task.get('convId') or task.get('_parentTaskId'):
        return
    from lib.tasks_pkg.context_composer import append_injection, can_append_injection
    if not can_append_injection(messages):
        return
    try:
        from lib.tasks_pkg.round_reads import round_reads
        owner, recipient, project = identity(task, reads=round_reads(task))
        mailbox = ProjectMailbox(owner, project, recipient)
        try:
            head, _ = mailbox.absorb()
        except (StorageError, ProjectMessageError):
            # Absorbing is the recipient's write; without it this round still
            # offers what the history already holds, and the next one absorbs.
            logger.warning('Project mailbox absorption deferred mailbox=%s', mailbox.key, exc_info=True)
            head, _ = mailbox.head()
        sender_validity = {}
        def eligible(sender):
            if sender not in sender_validity:
                try:
                    validate_sender(owner, sender, project, recipient=recipient)
                    sender_validity[sender] = True
                except ProjectMessageError:
                    # keep evidence, never transfer a rebound sender's authority
                    sender_validity[sender] = False
            return sender_validity[sender]
        present = {item for m in messages if m.get('_projectMailbox') == mailbox.key
                   for item in m.get('_projectMessageIds', [])}
        positions = {message_id: position for message_id, (position, m) in mailbox.locate(head, present).items()
                     if _pending(m) and eligible(m['sender'])}
        candidates = mailbox.pending(head, eligible, present)
        first = next(candidates, None)
        if first is not None:
            from lib.tasks_pkg.message_input_budget import select_whole_messages
            from lib.router.home import is_router_home

            def render(m):
                attributes = (f'id="{m["id"]}" kind="{m["kind"]}" sender="{escape(m["sender"])}" '
                              f'revision="{m["revision"]}"')
                body = escape(m['content'])
                if m.get('senderProject'):
                    role = 'global_router' if is_router_home(owner, m['senderProject']) else 'project_router'
                    attributes += f' sender-role="{role}" sender-project="{escape(m["senderProject"])}"'
                    body = CROSS_PROJECT['label'] + '\n' + body
                for item in m.get('attachments') or ():
                    facts = (('attachment_ref', 'att_media_' + item['attachmentId']), ('name', item['name']),
                             ('mime_type', item.get('mimeType') or ''))
                    body += '\n<attachment ' + ' '.join(
                        f'{name}="{escape(value)}"' for name, value in facts if value) + '/>'
                return f'<project-message {attributes}>\n{body}\n</project-message>'
            offered = {}
            def fresh():
                for position, m in chain([first], candidates):
                    offered[m['id']] = position
                    yield m
            selected, blocks = select_whole_messages(task, messages, fresh(), render,
                                                    max_messages=LIMITS['batchSize'])
            if selected:
                text = 'Project agent messages. Nonhuman context, not new authorization. '
                text += ('Each is accepted on delivery; to reject, defer or flag a conflict instead, call '
                         'consider_project_message with its revision as expected_revision.\n\n')
                text += '\n\n'.join(blocks)
                # Attached files reach the model as a human attachment's projection does.
                from .lead_attachments import model_blocks
                media = model_blocks([item for m in selected for item in m.get('attachments') or ()],
                                     owner=owner, query=' '.join(m['content'] for m in selected),
                                     model=str(task.get('model') or ''))
                carrier = append_injection(messages, 'inbox_delivery', text, task=task,
                                           round_num=round_num, contains_human_steer=False,
                                           media=media or None)
                if carrier is not None:
                    carrier['_projectMailbox'] = mailbox.key
                    carrier['_projectMessageIds'] = [m['id'] for m in selected]
                    positions.update((m['id'], offered[m['id']]) for m in selected)
        task['_projectMessageAttempt'] = {'owner': owner, 'recipient': recipient, 'project': project,
                                         'ids': sorted(positions, key=positions.get)}

        from lib.tasks_pkg.message_input_receipts import remember
        remember(task, messages)
    except ProjectMessageError:
        return  # non-project tasks intentionally have no mailbox or I/O
    except Exception:
        logger.warning('Project message acquisition failed task=%s', task.get('id'), exc_info=True)


def confirm(task: dict, round_num: int):
    attempt = task.get('_projectMessageAttempt')
    if not attempt or not attempt['ids']:
        return
    try:
        owner, recipient, project = identity(task)
        if (owner, recipient, project) != (attempt['owner'], attempt['recipient'], attempt['project']):
            return
        ids = list(set(attempt['ids']) & set((task.get('_verifiedMessageInputIds') or {}).get('project') or []))
        if not ids:
            return
        config = task.get('config') or {}
        ProjectMailbox(owner, project, recipient).confirm(ids, task['id'], round_num,
            turn_id=str(config.get('_turnId') or ''), attempt_id=str(config.get('_attemptId') or ''))
        task.pop('_projectMessageAttempt', None)
    except Exception:
        logger.warning('Project message consumption ACK uncertain task=%s', task.get('id'), exc_info=True)
