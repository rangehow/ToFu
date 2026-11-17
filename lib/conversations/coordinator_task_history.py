"""Coordinator task history: every task the Coordinator's board ever showed.

The task board (``coordinator_progress``) keeps only the latest complete board,
so a task a later board omits disappears from it. This module keeps one durable
record per task identity of the current binding: created when the task first
appears, rewritten when it changes or leaves the board, never deleted by a later
board. It is a summary for the human, not evidence, and grants no authority
(docs/PROJECT_COORDINATION.md, "Task history").

Entry points:
- ``plan_transition``: the identity rule, which decides the key of each task of
  a new board from the previous board and its keys (pure).
- ``apply_board``: fold one board write into in-memory records (pure).
- ``record_board``: called by ``CoordinatorTaskProgress`` inside its head CAS
  attempt, before the head write; returns the new board's keys, which the head
  stores as ``taskKeys`` in the same CAS. Only changed tasks are read and written.
- ``transcript_boards``: the board revisions the Coordinator's own transcript
  records (each ``update_task_progress`` and publishing ``todo_write`` round).
  A head written before task history has no ``taskKeys``; its first changed
  board seeds the history from them, so tasks dropped before this module
  existed are recovered.
- ``CoordinatorTaskHistory.read``: one page for the HTTP projection.

Dependencies: ``coordinator_progress`` (contract, head, binding scope), the
storage record namespace ``taskProgress.historyNamespace`` and the conversation
repository (transcript seed). Owner identity is an explicit parameter.
"""
from __future__ import annotations

from copy import deepcopy
import difflib
import hashlib
import json
import re
import time
import uuid

from lib.conversations.coordinator_progress import (
    CONTRACT, POLICY, CoordinatorTaskProgress, board_from_plan, validate_progress)
from lib.log import get_logger
from lib.storage.errors import StorageError

logger = get_logger(__name__)

NAMESPACE = POLICY['historyNamespace']
GROUPS = tuple(CONTRACT['$defs']['progressHistoryGroup']['enum'])
PAGE_SIZE = CONTRACT['$defs']['progressHistoryPage']['properties']['tasks']['maxItems']
_QUERY_MAX_CHARS = CONTRACT['$defs']['progressTask']['properties']['progress']['maxLength']
# record.list's protocol maximum page.
_RECORD_LIST_LIMIT = 1000
_OPEN = frozenset({'pending', 'in_progress'})
_RANK = {group: rank for rank, group in enumerate(GROUPS)}
_CURSOR = re.compile(r'^(\d+)\.(\d+)\.([0-9a-f]{32})\.([0-9a-f]{16})$')


def _digest(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def title_key(title):
    """Key of the first identity a title starts; the same title later continues it."""
    return _digest('title', title)[:32]


def _unique_key(title, revision, index):
    return _digest('task', title, revision, index)[:32]


def scope_prefix(scope):
    return (str(scope['owner']) + ':'
            + _digest(scope['projectPath'], scope['coordinatorId'], scope['bindingRevision']) + ':')


def plan_transition(previous, previous_keys, board):
    """The key of each task of ``board``, in board order (the identity rule).

    1. A task keeps its identity while its title is unchanged.
    2. An in-place title edit keeps it: aligning both boards by title, exactly
       one task between the same unchanged neighbours was replaced by exactly
       one task, and the replaced task was still open or the new one keeps its
       finished status (a finished task is never continued by another task).
    3. Any other task starts its title's identity, so a title that left the
       board continues its earlier task when it returns; when that identity is
       already on this board, the task gets a key of its own.
    """
    old = list((previous or {}).get('tasks', ()))
    old_keys = list(previous_keys or ())
    new = board['tasks']
    keys = [None] * len(new)
    slots = {}
    for index, task in enumerate(old):
        slots.setdefault(task['title'], []).append(index)
    for index, task in enumerate(new):
        if slots.get(task['title']):
            keys[index] = old_keys[slots[task['title']].pop(0)]
    old_titles = {task['title'] for task in old}
    new_titles = {task['title'] for task in new}
    matcher = difflib.SequenceMatcher(a=[task['title'] for task in old],
                                      b=[task['title'] for task in new], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != 'replace':
            continue
        # Tasks that only moved are matched by title above; they are not edits.
        removed = [index for index in range(i1, i2) if old[index]['title'] not in new_titles]
        added = [index for index in range(j1, j2) if new[index]['title'] not in old_titles]
        if len(removed) != 1 or len(added) != 1:
            continue
        before, after = old[removed[0]], new[added[0]]
        if before['status'] in _OPEN or before['status'] == after['status']:
            keys[added[0]] = old_keys[removed[0]]
    taken = {key for key in keys if key}
    for index, task in enumerate(new):
        if keys[index] is None:
            key = title_key(task['title'])
            if key in taken:
                key = _unique_key(task['title'], board['revision'], index)
            keys[index] = key
            taken.add(key)
    return keys


def _content(record, task, at):
    """Write ``task``'s title, status, note and due into ``record``.

    A note or due a later board omits stays: the record keeps the last progress
    line and the last promise the task carried.
    """
    from lib.conversations.coordinator_commitments import due_ms
    if record['title'] != task['title']:
        former = [title for title in record.get('formerTitles', ()) if title != task['title']]
        if record['title'] not in former:
            former.append(record['title'])
        record['formerTitles'] = former
        record['title'] = task['title']
    if task['status'] == 'completed' and (record['status'] != 'completed' or 'completedAt' not in record):
        record['completedAt'] = at
    elif task['status'] != 'completed':
        record.pop('completedAt', None)
    record['status'] = task['status']
    if 'progress' in task:
        record['progress'] = task['progress']
    if 'due' in task:
        record['due'] = task['due']
        record['dueAt'] = due_ms(task['due'])


def apply_board(records, scope, previous, previous_keys, board, keys, *, touched=None):
    """Fold one board write into ``records`` ({key: record}); returns the keys it changed.

    ``touched`` limits the fold to those keys (the live path loads only changed
    tasks); every other key is assumed unchanged.
    """
    at, revision = board['updatedAt'], board['revision']
    changed = set()
    for key, task in zip(keys, board['tasks']):
        if touched is not None and key not in touched:
            continue
        record = records.get(key)
        if record is None:
            record = {'scope': scope, 'key': key, 'title': task['title'], 'status': task['status'],
                      'firstSeenAt': at, 'firstSeenRevision': revision,
                      'lastSeenAt': at, 'lastSeenRevision': revision,
                      'changedAt': at, 'changedRevision': revision}
            _content(record, task, at)
            records[key] = record
            changed.add(key)
            continue
        updated = deepcopy(record)
        updated.pop('departedAt', None)
        updated.pop('departedRevision', None)
        _content(updated, task, at)
        if updated != record:
            updated.update(lastSeenAt=at, lastSeenRevision=revision, changedAt=at, changedRevision=revision)
            records[key] = updated
            changed.add(key)
    present = set(keys)
    for key, task in zip(previous_keys or (), (previous or {}).get('tasks', ())):
        if key in present or (touched is not None and key not in touched):
            continue
        record = records.get(key)
        if record is None:
            # The previous board showed it, so the history must too.
            record = {'scope': scope, 'key': key, 'title': task['title'], 'status': task['status'],
                      'firstSeenAt': previous['updatedAt'], 'firstSeenRevision': previous['revision'],
                      'lastSeenAt': previous['updatedAt'], 'lastSeenRevision': previous['revision'],
                      'changedAt': previous['updatedAt'], 'changedRevision': previous['revision']}
            _content(record, task, previous['updatedAt'])
        elif 'departedAt' in record:
            continue
        else:
            record = deepcopy(record)
        record.update(lastSeenAt=previous['updatedAt'], lastSeenRevision=previous['revision'],
                      departedAt=at, departedRevision=revision, changedAt=at, changedRevision=revision)
        records[key] = record
        changed.add(key)
    return changed


def fold_boards(scope, boards):
    """Records and final keys of a board sequence (oldest first), folded in memory."""
    records, previous, previous_keys = {}, None, None
    for board in boards:
        keys = plan_transition(previous, previous_keys, board)
        apply_board(records, scope, previous, previous_keys, board, keys)
        previous, previous_keys = board, keys
    return records, previous_keys


def _json_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def boards_from_rounds(rounds):
    """Board revisions published by ``rounds`` (tool rounds in transcript order).

    An ``update_task_progress`` round names the revision its complete board
    became; a publishing ``todo_write`` round names it in its result and leaves
    the board ``board_from_plan`` derives from the board before it. Unchanged
    writes repeat a revision and add nothing. A revision that starts again at 1
    is a new binding's board, which restarts the sequence.
    """
    boards = []
    for entry in rounds:
        name = entry.get('toolName')
        if name not in ('update_task_progress', 'todo_write'):
            continue
        arguments = _json_value(entry.get('toolArgs'))
        if not isinstance(arguments, dict):
            continue
        at = entry.get('tEnd') or entry.get('tStart')
        if type(at) is not int or at < 0:
            continue
        revision = None
        if name == 'update_task_progress':
            content = _json_value(entry.get('toolContent'))
            if isinstance(content, dict) and content.get('changed') is True:
                revision = content.get('revision')
            request = arguments
        else:
            for result in entry.get('results') or ():
                hint = result.get('coordinatorProgress') if isinstance(result, dict) else None
                if isinstance(hint, dict):
                    revision = hint.get('revision')
            request = None
        if type(revision) is not int or revision < 1:
            continue
        if boards and revision <= boards[-1]['revision']:
            if revision != 1:
                continue
            boards = []
        try:
            if request is None:
                request = board_from_plan(boards[-1] if boards else None, arguments.get('todos') or [],
                                          arguments.get('explanation') or '')
                if request is None:
                    continue
            validate_progress(request, 'progressRequest')
        except (TypeError, ValueError, KeyError):
            continue
        boards.append({'revision': revision, 'updatedAt': at, 'summary': request['summary'],
                       'tasks': deepcopy(request['tasks'])})
    return boards


def transcript_boards(scope):
    """``boards_from_rounds`` of the Coordinator's transcript, read one Turn at a time."""
    from lib.conversations.repository import get_conversation
    owner, conversation = scope['owner'], scope['coordinatorId']
    snapshot = get_conversation(conversation, user_id=owner, message_window=1)
    total = (snapshot or {}).get('msg_count')
    if type(total) is not int or total < 1:
        return []
    rounds = []
    for index in range(total):
        snapshot = get_conversation(conversation, user_id=owner, message_window=1,
                                    before_sequence=index + 1)
        for message in (snapshot or {}).get('messages') or ():
            if isinstance(message, dict):
                rounds.extend(entry for entry in message.get('toolRounds') or ()
                              if isinstance(entry, dict)
                              and entry.get('toolName') in ('update_task_progress', 'todo_write'))
    return boards_from_rounds(rounds)


class _Records:
    def __init__(self, client, scope):
        self.client = client
        self.scope = scope
        self.prefix = scope_prefix(scope)

    def get(self, key):
        return self.client().query('record.get', {'namespace': NAMESPACE, 'key': self.prefix + key})

    def put(self, key, value, version):
        validate_progress(value, 'progressHistoryRecord')
        self.client(write=True).command('record.put', {
            'namespace': NAMESPACE, 'key': self.prefix + key, 'value': value,
            'expected_version': version,
        }, 'coordinator-progress-history:' + uuid.uuid4().hex)

    def all(self):
        records, after = {}, ''
        while True:
            payload = {'namespace': NAMESPACE, 'prefix': self.prefix, 'limit': _RECORD_LIST_LIMIT}
            if after:
                payload['after_key'] = after
            rows = self.client().query('record.list', payload) or []
            for row in rows:
                value = row['value']
                key = row['key'][len(self.prefix):]
                if not isinstance(value, dict) or value.get('scope') != self.scope or value.get('key') != key:
                    raise StorageError('database_integrity', 'Invalid Coordinator task history record')
                records[key] = value
            if len(rows) < _RECORD_LIST_LIMIT:
                return records
            after = rows[-1]['key']


def record_board(scope, previous, previous_keys, board, *, client, read_transcript=transcript_boards):
    """Record one board write before its head CAS; returns the keys the head stores.

    ``previous`` is the board this write replaces in the same binding (None for
    its first board) and ``previous_keys`` its stored keys. A previous board
    without keys predates task history: the history is seeded from the
    transcript's earlier revisions, that board and this one. When the
    transcript cannot be read now, None leaves the head without keys, so the
    next changed board seeds again; nothing is lost because the transcript keeps
    every board. A conflicting record write raises ``database_conflict`` for
    the caller's CAS loop to retry.
    """
    records = _Records(client, scope)
    if previous is not None and previous_keys is None:
        try:
            earlier = [item for item in read_transcript(scope) if item['revision'] < previous['revision']]
        except Exception:
            logger.warning('[TaskHistory] transcript of %s unreadable; history seeds at the next board write',
                           scope['coordinatorId'], exc_info=True)
            return None
        folded, keys = fold_boards(scope, [*earlier, previous, board])
        for key, value in folded.items():
            row = records.get(key)
            if row is None or row['value'] != value:
                records.put(key, value, row['version'] if row else 0)
        logger.info('[TaskHistory] seeded %d tasks of %s from %d board revisions',
                    len(folded), scope['coordinatorId'], len(earlier) + 2)
        return keys
    keys = plan_transition(previous, previous_keys, board)
    before = dict(zip(previous_keys or (), (previous or {}).get('tasks', ())))
    present = set(keys)
    touched = {key for key, task in zip(keys, board['tasks']) if before.get(key) != task}
    touched.update(key for key in before if key not in present)
    loaded, versions = {}, {}
    for key in touched:
        row = records.get(key)
        if row is not None:
            loaded[key], versions[key] = row['value'], row['version']
    for key in apply_board(loaded, scope, previous, previous_keys, board, keys, touched=touched):
        records.put(key, loaded[key], versions.get(key, 0))
    return keys


def due_outcome(entry, now):
    """met, missed, open or withdrawn for a task carrying a due; None without one."""
    if 'dueAt' not in entry:
        return None
    if entry['status'] == 'cancelled':
        return 'withdrawn'
    if entry['status'] == 'completed':
        return 'met' if entry.get('completedAt', entry['changedAt']) <= entry['dueAt'] else 'missed'
    return 'missed' if now > entry['dueAt'] else 'open'


def _entry(record, *, board=None, task=None, now):
    """The shown task: a record, overlaid by its current board task when on the board."""
    entry = {name: deepcopy(record[name]) for name in (
        'key', 'title', 'formerTitles', 'status', 'progress', 'due', 'dueAt', 'firstSeenAt',
        'lastSeenAt', 'changedAt', 'departedAt', 'completedAt') if name in record}
    if task is not None:
        _content(entry, task, board['updatedAt'])
        entry.pop('departedAt', None)
        entry['lastSeenAt'] = board['updatedAt']
    entry['onBoard'] = task is not None
    entry['dropped'] = task is None and entry['status'] in _OPEN
    outcome = due_outcome(entry, now)
    if outcome:
        entry['dueOutcome'] = outcome
    return entry


def _group(entry):
    return 'dropped' if entry['dropped'] else entry['status']


def _order(entry):
    return _RANK[_group(entry)], -entry['changedAt'], entry['key']


def _fingerprint(group, query):
    return _digest('history-page', group or '', query)[:16]


class CoordinatorTaskHistory:
    """Read every task the current binding's board ever showed, one page at a time."""

    def __init__(self, owner, project, *, client=None, bindings=None, clock=time.time):
        self.progress = CoordinatorTaskProgress(owner, project, client=client, bindings=bindings, clock=clock)
        self.clock = clock

    def read(self, *, group=None, query='', cursor=None):
        if group is not None and group not in _RANK:
            raise ValueError('group must be one of ' + ', '.join(GROUPS))
        if not isinstance(query, str) or len(query) > _QUERY_MAX_CHARS:
            raise ValueError(f'q has at most {_QUERY_MAX_CHARS} characters')
        needle = ' '.join(query.split()).casefold()
        after = None
        if cursor:
            match = _CURSOR.match(cursor) if isinstance(cursor, str) else None
            if match is None or match.group(4) != _fingerprint(group, needle):
                raise ValueError('cursor does not belong to this history query')
            after = (int(match.group(1)), -int(match.group(2)), match.group(3))
        scope, binding, head = self.progress.current()
        page = {'projectPath': self.progress.project, 'conversationId': binding['conversationId'],
                'bindingRevision': binding['revision'], 'recorded': True,
                'counts': {name: 0 for name in GROUPS}, 'tasks': [], 'nextCursor': None,
                'coverage': 'complete'}
        if scope is None:
            return validate_progress(page, 'progressHistoryPage')
        entries, page['recorded'] = self._entries(scope, head)
        translation = head.get('translation') if head else None
        if translation:
            page['translation'] = deepcopy(translation)
        shown_as = {item['source']: item['text'] for item in translation['texts']} if translation else {}
        if needle:
            def texts(entry):
                written = (entry['title'], entry.get('progress', ''), *entry.get('formerTitles', ()))
                return (*written, *(shown_as[text] for text in written if text in shown_as))
            entries = [entry for entry in entries if any(needle in text.casefold() for text in texts(entry))]
        for entry in entries:
            page['counts'][_group(entry)] += 1
        if group is not None:
            entries = [entry for entry in entries if _group(entry) == group]
        entries.sort(key=_order)
        if after is not None:
            entries = [entry for entry in entries if _order(entry) > after]
        page['tasks'] = entries[:PAGE_SIZE]
        if len(entries) > PAGE_SIZE:
            last = page['tasks'][-1]
            rank, changed, key = _order(last)
            page['nextCursor'] = f'{rank}.{-changed}.{key}.{_fingerprint(group, needle)}'
            page['coverage'] = 'page'
        return validate_progress(page, 'progressHistoryPage')

    def tasks(self, conversation_id):
        """Every shown task of the binding whose Coordinator is ``conversation_id`` (unpaged), else [].

        The lead deliverables projection resolves report task titles and derives
        each completed task's automatic work report from these.
        """
        scope, binding, head = self.progress.current()
        if scope is None or binding.get('conversationId') != conversation_id:
            return []
        return self._entries(scope, head)[0]

    def _entries(self, scope, head):
        """Every shown task and whether the history is recorded (a pre-history head is not)."""
        now = int(self.clock() * 1000)
        board = head['board'] if head else None
        keys = head.get('taskKeys') if head else None
        recorded = True
        if board is not None and keys is None:
            # The head predates task history; its next changed board seeds it.
            recorded = False
            keys = plan_transition(None, None, board)
            shown = {key: {'key': key, 'title': task['title'], 'status': task['status'],
                           'firstSeenAt': board['updatedAt'], 'lastSeenAt': board['updatedAt'],
                           'changedAt': board['updatedAt']} for key, task in zip(keys, board['tasks'])}
        else:
            shown = _Records(self.progress.client, scope).all()
        on_board = dict(zip(keys or (), board['tasks'] if board else ()))
        entries = [_entry(record, board=board, task=on_board.get(key), now=now)
                   for key, record in shown.items()]
        entries.extend(_entry({'key': key, 'title': task['title'], 'status': task['status'],
                               'firstSeenAt': board['updatedAt'], 'lastSeenAt': board['updatedAt'],
                               'changedAt': board['updatedAt']}, board=board, task=task, now=now)
                       for key, task in on_board.items() if key not in shown)
        return entries, recorded
