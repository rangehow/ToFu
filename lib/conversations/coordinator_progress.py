"""Coordinator task board: the Coordinator's own summary of the human's tasks.

Entry points: ``CoordinatorTaskProgress.publish`` (the bound Coordinator's
``update_task_progress`` tool) and ``read`` (HTTP projection). One owner/project
head holds the latest complete board of the current Coordinator binding; a
replaced binding starts without a board. The board is model-authored by design
(docs/PROJECT_COORDINATION.md, "Session-free coordination"): a summary for the
human, not evidence, and it grants no authority. Questions for the human are
ask_human cards in the conversation, never board entries. No model, task,
worker or polling is started here.

One progress source: the board is also the Coordinator's checklist.
``publish_task_board`` (update_task_progress) and ``plan_publication``
(todo_write) write this one head under the task's progress lock and leave the
task's checklist state as the board's plan (``plan_from_board``); each write's
round carries ``progress_view`` so the browser refreshes both views live from
the conversation stream.

Display translation: the head may also carry translations of the board's own
texts (``board_texts``) into the language the human reads the Coordinator in.
They are reconstructible display data, derived by
``coordinator_progress_translation`` and merged here by ``untranslated_texts``
and ``attach_translation``; a stored translation always has at least one entry.
A task leaving the board carries its texts' translations into its history
record (``coordinator_task_history``), which the same job completes.

Task history: a board write replaces the board, so this head alone would lose
every task a later board omits. ``coordinator_task_history.prepare_board_history``
prepares keys and changed history values without publishing them. The head CAS
publishes the board, ``taskKeys`` and ``historyDelta`` atomically; subsequent
``index_committed_history`` materializes only the committed delta. Before the
next board replacement, the previous committed delta must be indexed so its
recovery copy is not lost.

Typed blockers: a pending task states what it waits for as one typed
``blocker`` (``progressBlocker``: a running assignment, a pinned dependency, an
exact external resource with the check that proves it present, the owner's
question, or a next action a Worker can start now). A free-text note never
substitutes for it. A write may not introduce or change a pending row it cannot
verify, so an unfalsifiable "waiting for" line is refused; rows already on the
board keep reading, and the projection flags the ones nothing is doing
(``task_is_idle``) instead of deleting or restatusing them.

Commitments: a task carrying ``due`` is a promise to the owner
(``coordinator_commitments``; docs/PROJECT_COORDINATION.md "Commitments on the
task board"). Every board write validates the commitment rules and merges the
platform's ``commitments`` receipts into the same head CAS, then arms the
scheduler's one-shot due wake. A receipt is the record that one due time already
woke the lead, so it never wakes twice.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import re
import time
import uuid

from lib.conversations.coordinator_reports import CONTRACT
from lib.conversations.project_coordination import ProjectCoordinationRepository
from lib.conversations.project_identity import normalize_project_path, project_channel_key
from lib.identity import require_user_id
from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.tools.contracts import check_argument_alternatives

logger = get_logger(__name__)

POLICY = CONTRACT['taskProgress']
# Checklist statuses a board task shares; a board has no `blocked`, a plan no `cancelled`.
_PLAN_STATUSES = frozenset({'pending', 'in_progress', 'completed'})
_REASON_STATUSES = frozenset({'pending', 'cancelled'})
_BLOCKER_STATUSES = frozenset({'pending', 'in_progress'})
# The payload every variant of a blocker kind must carry, read from the
# contract so the refusal messages and the schema cannot drift apart. A kind
# with several variants (an external resource the owner can act on, or one
# that is unavailable) carries their common fields here; variant-only rules
# live in ``blocker_problem``.
def _common_payload(kind):
    required = None
    for branch in CONTRACT['$defs']['progressBlocker']['oneOf']:
        if branch['properties']['kind']['const'] != kind:
            continue
        names = [name for name in branch['required'] if name != 'kind']
        required = names if required is None else [name for name in required if name in names]
    return tuple(required or ())


BLOCKER_PAYLOADS = {
    branch['properties']['kind']['const']: _common_payload(branch['properties']['kind']['const'])
    for branch in CONTRACT['$defs']['progressBlocker']['oneOf']
}
_BLOCKER_PAYLOAD_HINTS = {
    'active_dispatch': 'name the id of a Worker assignment this board\'s Coordinator dispatched and that is running now',
    'blocked_by_dependency': 'name the assignment this task waits on and the revision that must settle',
    'needs_external_resource': ('name the exact external resource, the check that proves it is present, and '
                                'either ownerAction (the one concrete thing the owner does elsewhere to obtain '
                                'it) or unavailable with the limit that stays unverified'),
    'needs_owner_decision': 'name the ask_human question or guidance the answer arrives through and its question in plain language',
    'next_action': 'name the concrete action a Worker can start now',
}
_PENDING_HELP = (
    'say what it waits for with exactly one typed blocker: ' + '; '.join(
        kind + ' (' + hint + ')' for kind, hint in _BLOCKER_PAYLOAD_HINTS.items())
    + '. A free-text note (reason) is supplemental prose and never states the wait by itself')
# An action made only of these words names no action: nothing a Worker can start now.
_NON_ACTION_WORDS = frozenset(
    'a an and as before continue follow handle it keep later monitor more need needs on ongoing proceed '
    'review same still stay the then this to up wait waiting work'.split())


@lru_cache(maxsize=8)
def _validator(definition):
    from jsonschema import Draft202012Validator
    return Draft202012Validator({'$schema': CONTRACT['$schema'], '$defs': CONTRACT['$defs'],
                                 '$ref': '#/$defs/' + definition})


def validate_progress(value, definition):
    error = next(_validator(definition).iter_errors(value), None)
    if error is not None:
        where = '/'.join(str(part) for part in error.absolute_path)
        raise ValueError('Invalid task board' + (' at ' + where if where else '') + ': ' + error.message)
    # The exact strings must also be valid UTF-8 for durable storage.
    json.dumps(value, ensure_ascii=False).encode('utf-8')
    return value


def _filled(value):
    if isinstance(value, str):
        return bool(value.strip())
    return type(value) is int and value >= 1


def blocker_problem(blocker):
    """What a typed blocker is missing, or None when its kind's payload is complete."""
    if not isinstance(blocker, dict):
        return 'is not a typed blocker object'
    kind = blocker.get('kind')
    if kind not in BLOCKER_PAYLOADS:
        return ('names no known blocker kind; use exactly one of ' + ', '.join(BLOCKER_PAYLOADS))
    missing = [name for name in BLOCKER_PAYLOADS[kind] if not _filled(blocker.get(name))]
    if missing:
        return ('is ' + kind + ' without ' + ' and '.join(missing) + '; '
                + _BLOCKER_PAYLOAD_HINTS[kind])
    if kind == 'needs_external_resource':
        owner_action = _filled(blocker.get('ownerAction'))
        unavailable = blocker.get('unavailable') is True
        if owner_action and unavailable:
            return ('is needs_external_resource with both ownerAction and unavailable; state exactly one: '
                    'the owner\'s action, or the limit that stays unverified')
        if not owner_action and not unavailable:
            return ('is needs_external_resource without ownerAction or unavailable; '
                    + _BLOCKER_PAYLOAD_HINTS[kind])
        if unavailable and not _filled(blocker.get('limit')):
            return ('is needs_external_resource unavailable without limit; state what stays unverified because '
                    'the resource cannot be obtained now')
    return None


def check_blockers(tasks, previous, assignment_problem, *, require_pending=True):
    """Refuse a write that creates or changes a row whose wait cannot be verified.

    ``previous`` holds the tasks the board already showed: a row carried through
    byte-identical is legacy, accepted before this rule, and the read projection
    flags it idle instead of refusing the board that still shows it. Every row
    this write introduces or changes must validate a blocker it carries; a
    pending row's ``active_dispatch`` must name an assignment ``assignment_problem``
    finds running (it returns why it is not, or None).

    ``require_pending`` asks a new or changed pending row for a blocker. The
    checklist projection cannot name one (todo_write has no blocker field), so a
    pending checklist item is accepted and flagged idle instead: it claims no
    wait that could be unverifiable.
    """
    for task in tasks:
        if task in previous:
            continue
        status = task['status']
        if status not in _BLOCKER_STATUSES:
            continue
        if 'blocker' not in task:
            if status == 'pending' and require_pending:
                raise ValueError(f"task {task['title']!r} is pending without a typed blocker: " + _PENDING_HELP)
            continue
        problem = blocker_problem(task['blocker'])
        if problem:
            raise ValueError(f"task {task['title']!r}: blocker {problem}")
        blocker = task['blocker']
        if blocker['kind'] == 'active_dispatch':
            why = assignment_problem(blocker['assignmentId'])
            if why:
                raise ValueError(f"task {task['title']!r}: active_dispatch assignment "
                                 f"{blocker['assignmentId']!r} {why}")


def _generic_action(action):
    # Only pure filler is generic: text in another script is a real action, not an empty one.
    words = re.findall(r"[a-z']+", action.lower())
    return bool(words) and all(word in _NON_ACTION_WORDS for word in words)


def task_is_limit(task):
    """Whether a row states a terminal limit: its external resource cannot be obtained now.

    A limit is reported as what stays unverified, never as a wait and never as
    idle: no owner action and no dispatch would end it.
    """
    blocker = task.get('blocker') or {}
    return blocker.get('kind') == 'needs_external_resource' and blocker.get('unavailable') is True


def task_is_idle(task, running_assignment_ids=()):
    """Whether a pending row is parked: nothing acts on it and nothing concrete would start.

    It names no running assignment, no dependency and no next action a Worker
    could start now (an absent or generic one). A typed external resource or
    owner question is a specific, checkable wait and is never idle; an
    unavailable resource is a terminal limit, also never idle. Only the
    projection reads this; rows and statuses are never changed for it.
    """
    if task.get('status') != 'pending':
        return False
    blocker = task.get('blocker')
    if not blocker:
        return True
    kind = blocker.get('kind')
    if kind == 'active_dispatch':
        return blocker.get('assignmentId') not in running_assignment_ids
    if kind == 'next_action':
        return _generic_action(blocker.get('action') or '')
    return False


def idle_task_keys(head, running_assignment_ids=()):
    """Keys of the pending rows ``task_is_idle`` flags, in board order."""
    return [key for task, key in zip(head['board']['tasks'], board_task_keys(head))
            if task_is_idle(task, running_assignment_ids)]


def validate_board_call(arguments):
    # Name a missing blocker payload before the schema's union reports it vaguely.
    tasks, changes = arguments.get('tasks') or (), arguments.get('changes') or ()
    for row in [*tasks, *changes]:
        if not isinstance(row, dict) or not row.get('blocker'):
            continue
        problem = blocker_problem(row['blocker'])
        if problem:
            raise ValueError(f"task {row.get('title')!r}: blocker {problem}")
    validate_progress(arguments, 'progressToolCall')
    check_argument_alternatives('update_task_progress', arguments)
    if 'read' in arguments and len(arguments) != 1:
        raise ValueError('read does not accept summary or expected_revision')
    if 'tasks' in arguments and 'summary' not in arguments:
        raise ValueError('summary is required for a complete replacement; use changes for a partial update')
    if arguments.get('changes') == [] and 'summary' not in arguments:
        raise ValueError('send at least one change or supply a summary')


def board_from_update(previous, arguments):
    """Materialize a validated write, preserving every omitted task and field in a patch."""
    if 'tasks' in arguments:
        return {'summary': arguments['summary'], 'tasks': arguments['tasks']}
    if previous is None and 'summary' not in arguments:
        raise ValueError('summary is required to create the first task board')
    tasks = list(previous['tasks']) if previous else []
    positions = {}
    for index, task in enumerate(tasks):
        title = task['title']
        positions[title] = None if title in positions else index
    seen = set()
    for change in arguments['changes']:
        title = change['title']
        if title in seen:
            raise ValueError(f'specify one change per title: {title!r}')
        seen.add(title)
        if title not in positions:
            if 'status' not in change:
                raise ValueError(f'new task {title!r} needs status; to edit a task, use its exact title')
            tasks.append({key: value for key, value in change.items() if key != 'reason' or value is not None})
        else:
            index = positions[title]
            if index is None:
                raise ValueError(f'title {title!r} is ambiguous; use a complete replacement to distinguish the tasks')
            old = tasks[index]
            updated = {**old, **change}
            if updated.get('reason') is None or ('reason' not in change and updated['status'] != old['status']):
                updated.pop('reason', None)
            # A blocker describes the current status: a status change clears an old one.
            if updated.get('blocker') is None or ('blocker' not in change and updated['status'] != old['status']):
                updated.pop('blocker', None)
            tasks[index] = updated
    return {'summary': arguments['summary'] if 'summary' in arguments else previous['summary'],
            'tasks': tasks}


def board_texts(board):
    """The board's own texts in order, each once: the summary, then each task's title, note and status reason."""
    texts = [board['summary']]
    for task in board['tasks']:
        texts.append(task['title'])
        texts.extend(task[field] for field in ('progress', 'reason') if field in task)
    return list(dict.fromkeys(texts))


def _stored_translations(value, target):
    """``{source: text}`` of a head value's translation when it is in ``target``."""
    stored = value.get('translation')
    if not stored or stored['target'] != target:
        return {}
    return {entry['source']: entry['text'] for entry in stored['texts']}


def _translation(target, translations, board):
    """``translations`` of ``board``'s texts in board order; None when there is none."""
    texts = [{'source': text, 'text': translations[text]}
             for text in board_texts(board) if text in translations]
    return {'target': target, 'texts': texts} if texts else None


def board_task_keys(head):
    """History identity of each board task, in board order.

    A head written before task history has no ``taskKeys``; each title's first
    identity is then the key its history record starts (``title_key``).
    """
    keys = head.get('taskKeys')
    if keys is not None:
        return list(keys)
    from lib.conversations.coordinator_task_history import title_key
    return [title_key(task['title']) for task in head['board']['tasks']]


def _head_key(owner, project):
    digest = hashlib.sha256(json.dumps(project, ensure_ascii=False).encode()).hexdigest()
    return str(owner) + ':' + digest


def plan_from_board(board):
    """The board as a checklist: its tasks in order without dropped ones; the summary explains it."""
    return {'todos': [{'content': task['title'], 'status': task['status']}
                      for task in board['tasks'] if task['status'] != 'cancelled'],
            'explanation': board['summary']}


def board_from_plan(previous, todos, explanation):
    """The board request a checklist write leaves; None when there is neither a board nor a plan.

    Each item becomes a task, and ValueError names the first one a board task
    cannot be. A task keeps its note while its title and status stay the same,
    and its ``due`` while its title stays: a commitment survives a checklist
    rewrite (docs/PROJECT_COORDINATION.md "Commitments on the task board").
    The summary stays; a first board takes ``explanation`` when it fits on one
    line, else the title of its first item in progress, else of its first item.
    """
    notes = {(task['title'], task['status']): task
             for task in (previous or {}).get('tasks', ())}
    dues = {task['title']: task['due'] for task in (previous or {}).get('tasks', ()) if 'due' in task}
    tasks = []
    for number, item in enumerate(todos, 1):
        title, status = item['content'], item['status']
        if status not in _PLAN_STATUSES:
            raise ValueError(f'item {number} is {status}, which no task-board status shows: keep it '
                             'pending and name what it waits for in reason with update_task_progress')
        if '\n' in title or '\r' in title:
            raise ValueError(f'item {number} spans several lines; a task-board task is one line')
        task = {'title': title, 'status': status}
        earlier = notes.get((title, status), {})
        task.update({field: earlier[field] for field in ('progress', 'reason', 'blocker') if field in earlier})
        if title in dues:
            task['due'] = dues[title]
        tasks.append(task)
    if previous is not None:
        return {'summary': previous['summary'], 'tasks': tasks}
    if not tasks:
        return None
    line = ' '.join(explanation.split())
    summary = line or next(
        (task['title'] for task in tasks if task['status'] == 'in_progress'), tasks[0]['title'])
    return {'summary': summary, 'tasks': tasks}


class ProgressWriteUnconfirmed(StorageError):
    execution_started = None
    recovery = 'Read the current board with update_task_progress(read={}) before trying another update.'

    def __init__(self, code):
        super().__init__(code, 'The task board write could not be confirmed; ' + self.recovery)


class CoordinatorTaskProgress:
    def __init__(self, owner, project, *, client=None, bindings=None, notify=None, clock=time.time,
                 arm=None, dispatch=None):
        self.owner = require_user_id(owner, context='Coordinator task board')
        if not isinstance(project, str) or not project.strip() or len(project) > 4096:
            raise ValueError('Project path is required')
        self.project = normalize_project_path(project.strip())
        self.key = _head_key(self.owner, self.project)
        if client is None:
            from lib.storage import get_storage_client
            client = get_storage_client
        self.client = client
        self.bindings = bindings if bindings is not None else ProjectCoordinationRepository(self.owner, client=client)
        self.dispatch = dispatch if dispatch is not None else self._read_assignment
        self.notify = notify if notify is not None else self._notify
        self.clock = clock
        self.arm = arm if arm is not None else self._arm

    def _scope(self):
        binding = self.bindings.read(self.project)
        if binding['availability'] != 'available':
            return None, binding
        return {'owner': self.owner, 'projectPath': self.project,
                'coordinatorId': binding['conversationId'], 'bindingRevision': binding['revision']}, binding

    def _head(self):
        row = self.client().query('record.get', {'namespace': POLICY['namespace'], 'key': self.key})
        if row is None:
            return None
        try:
            validate_progress(row['value'], 'progressHead')
            scope = row['value']['scope']
            if scope['owner'] != self.owner or scope['projectPath'] != self.project:
                raise ValueError('Wrong task board owner/project')
            keys = row['value'].get('taskKeys')
            if keys is not None and len(keys) != len(row['value']['board']['tasks']):
                raise ValueError('Task keys do not match the board')
            delta = row['value'].get('historyDelta', ())
            if (len({item['key'] for item in delta}) != len(delta)
                    or any(item['scope'] != scope or item['changedRevision'] > row['value']['board']['revision']
                           for item in delta)):
                raise ValueError('Task history delta does not match the board scope and revision')
            if type(row['version']) is not int or row['version'] < 1:
                raise ValueError('Invalid record version')
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageError('database_integrity', 'Invalid Coordinator task board record') from exc
        return row

    def revision(self, binding):
        """The board revision of ``binding``'s head, or None when that binding has no board.

        ``binding`` names the coordinator and binding revision the caller just read.
        """
        scope = {'owner': self.owner, 'projectPath': self.project,
                 'coordinatorId': binding['conversationId'], 'bindingRevision': binding['revision']}
        row = self._head()
        if row is None or row['value']['scope'] != scope:
            return None
        return row['value']['board']['revision']

    def current(self):
        """``(scope, binding, head)``: the current binding's scope (None when unavailable),
        the binding view, and its head value (None without a board of that binding)."""
        scope, binding = self._scope()
        if scope is None:
            return None, binding, None
        row = self._head()
        return scope, binding, (row['value'] if row and row['value']['scope'] == scope else None)

    def publish(self, scope, request, *, translation_target=None):
        """Update the board of exactly ``scope``'s binding; unchanged content is a no-op.

        ``translation_target`` is the language the human reads this Coordinator
        in, None without auto-translate. A changed board keeps the stored
        translations of the texts it still shows when they are in that language,
        so unchanged strings never flash back to the written language.
        """
        validate_progress(scope, 'reportScope')
        validate_board_call(request)
        if 'read' in request:
            raise ValueError('a passive board read is not a publication')
        return self._publish(scope, lambda previous: self._board_request(scope, previous, request),
                             translation_target, expected_revision=request.get('expected_revision'))

    def _board_request(self, scope, previous, request):
        """The board this write publishes, refusing a pending wait it cannot verify."""
        board = board_from_update(previous, request)
        check_blockers(board['tasks'], (previous or {}).get('tasks', ()),
                       lambda assignment_id: self._assignment_problem(scope, assignment_id))
        return board

    def _read_assignment(self, assignment_id):
        from lib.project_dispatch.repository import ProjectDispatchRepository
        return ProjectDispatchRepository(self.owner, self.project, client=self.client).read_point(assignment_id)

    def _assignment_problem(self, scope, assignment_id):
        """Why ``assignment_id`` cannot be what this board waits on, or None when it is running.

        Only this board's own Coordinator, project and binding revision count, so
        an id from another lead's work never reads as an active dispatch here.
        """
        from lib.project_dispatch.repository import DispatchContention, ProjectDispatchError
        try:
            assignment = self.dispatch(assignment_id)
        except DispatchContention as error:
            raise ValueError(f'assignment {assignment_id!r} could not be read; retry after reading '
                             'get_project_assignment') from error
        except ProjectDispatchError as error:
            return 'does not exist (' + str(error) + ')'
        except StorageError as error:
            raise ValueError(f'assignment {assignment_id!r} could not be read ({error.code}); read '
                             'get_project_assignment and name a running one') from error
        request, grant = assignment.get('request') or {}, assignment.get('grant') or {}
        if (request.get('projectPath') != self.project or request.get('coordinatorId') != scope['coordinatorId']
                or grant.get('owner') != self.owner
                or grant.get('bindingRevision') != scope['bindingRevision']):
            return 'belongs to another Coordinator, project or binding revision'
        if grant.get('revoked'):
            return 'is revoked'
        status = (assignment.get('execution') or {}).get('status')
        if status != 'running':
            return 'is not running (status ' + str(status or 'not started') + ')'
        return None

    def read_for_coordinator(self, scope, *, title=None):
        """Read the actual board of this tool's binding, never a newer binding's data."""
        current, _, head = self.current()
        if current != scope:
            raise PermissionError('Coordinator binding changed')
        if head is None:
            return {'revision': 0, 'summary': None, 'tasks': [], 'taskKeys': []}
        board = head['board']
        selected = [(task, key) for task, key in zip(board['tasks'], board_task_keys(head))
                    if title is None or task['title'] == title]
        return {**board, 'tasks': [task for task, _ in selected],
                'taskKeys': [key for _, key in selected]}

    def publish_plan(self, scope, todos, explanation, *, translation_target=None):
        """``publish`` the board a checklist write leaves (``board_from_plan``).

        Without a board, an empty checklist writes nothing and returns no board.
        """
        validate_progress(scope, 'reportScope')
        return self._publish(scope, lambda previous: self._plan_request(scope, previous, todos, explanation),
                             translation_target)

    def _plan_request(self, scope, previous, todos, explanation):
        """The board a checklist leaves; its pending items claim no wait and are flagged idle.

        ``todo_write`` has no blocker field, so a pending item is accepted without
        one; a blocker it carries from the board is still verified.
        """
        board = board_from_plan(previous, todos, explanation)
        if board is not None:
            check_blockers(board['tasks'], (previous or {}).get('tasks', ()),
                           lambda assignment_id: self._assignment_problem(scope, assignment_id),
                           require_pending=False)
        return board

    def _publish(self, scope, build, translation_target, *, expected_revision=None):
        from lib.conversations.coordinator_task_history import transcript_boards
        transcript = []
        submission = None
        last_error = None

        def read_transcript(scope):
            # A CAS retry re-records the same write: read the transcript once per publish.
            if not transcript:
                transcript.append(transcript_boards(scope))
            return transcript[0]

        for _ in range(POLICY['casAttempts']):
            # A board racing a binding replacement must never reach the new team.
            if self._scope()[0] != scope:
                raise PermissionError('Coordinator binding changed')
            try:
                if submission is None:
                    head, version = self._prepare_write(scope, build, translation_target,
                                                        read_transcript, expected_revision)
                    if version is None:
                        return {'board': deepcopy(head['board']) if head else None, 'changed': False,
                                'taskKeys': (head or {}).get('taskKeys')}
                    submission = ({'namespace': POLICY['namespace'], 'key': self.key, 'value': head,
                                   'expected_version': version}, 'coordinator-progress-cas:' + uuid.uuid4().hex)
                # Replay the exact command on a lost ACK, even if the next revision is already visible.
                self.client(write=True).command('record.put', *submission)
            except StorageError as exc:
                if exc.code != 'database_conflict' and not exc.retryable:
                    raise
                last_error = exc
                if exc.code == 'database_conflict':
                    submission = None  # Only a confirmed CAS conflict can rebase.
                continue
            return self._published(scope, submission[0]['value'])
        if submission is not None:
            raise ProgressWriteUnconfirmed(last_error.code) from last_error
        raise StorageError('database_conflict', 'Task board changed concurrently; read it before retrying')

    def _prepare_write(self, scope, build, translation_target, read_transcript, expected_revision):
        from lib.conversations.coordinator_commitments import check_commitments, merge_commitments
        from lib.conversations.coordinator_task_history import index_committed_history, prepare_board_history
        row = self._head()
        original = row['value'] if row else None
        if original and original['scope']['bindingRevision'] > scope['bindingRevision']:
            raise PermissionError('Coordinator binding changed')
        current = original if original and original['scope'] == scope else None
        previous = current['board'] if current else None
        revision = previous['revision'] if previous else 0
        if expected_revision is not None and revision != expected_revision:
            raise ValueError(f'task board revision is {revision}, not {expected_revision}; '
                             'read the current board before retrying')
        request = build(previous)
        if request is None:
            return None, None
        validate_progress(request, 'progressRequest')
        for task in request['tasks']:
            if 'reason' in task and task['status'] not in _REASON_STATUSES:
                raise ValueError(f"task {task['title']!r}: reason is only for pending (a note beside its blocker) or cancelled (why it stopped)")
        if previous and previous['summary'] == request['summary'] and previous['tasks'] == request['tasks']:
            return current, None
        now_seconds = self.clock()
        now = int(now_seconds * 1000)
        check_commitments(previous, request, now=now_seconds)
        commitments = merge_commitments(previous, request, (current or {}).get('commitments'), now=now)
        board = {'revision': revision + 1, 'updatedAt': now,
                 'summary': request['summary'], 'tasks': deepcopy(request['tasks'])}
        head = {'scope': scope, 'board': board}
        if commitments:
            head['commitments'] = commitments
        translations = _stored_translations(current, translation_target) if current else {}
        if kept := _translation(translation_target, translations, board):
            head['translation'] = kept
        # Never replace the committed recovery copy before its values are indexed.
        index_committed_history(original, client=self.client)
        keys, delta = prepare_board_history(scope, previous, (current or {}).get('taskKeys'), board,
                            client=self.client, read_transcript=read_transcript,
                            translation=(current or {}).get('translation'), translation_target=translation_target)
        if keys is not None:
            head['taskKeys'] = keys
        if delta:
            head['historyDelta'] = delta
        return validate_progress(head, 'progressHead'), row['version'] if row else 0

    def _published(self, scope, head):
        from lib.conversations.coordinator_commitments import next_commitments
        from lib.conversations.coordinator_task_history import index_committed_history
        board = head['board']
        try:
            index_committed_history(head, client=self.client)
        except StorageError:
            logger.warning('[Coordinator] Committed task history awaits indexing', exc_info=True)
        self._hint()
        moment = next_commitments(board, head.get('commitments'), now=board['updatedAt'] / 1000)
        if moment is not None:
            self.arm(scope['owner'], scope, moment)
        return {'board': deepcopy(board), 'changed': True, 'taskKeys': head.get('taskKeys')}

    def mark_commitments_woken(self, scope, commitments, *, via, command_id, disposition):
        """Receipt one due wake in the same head, so that due time never wakes again.

        Only the (title, dueAt) pairs actually woken are receipted. The board is
        unchanged, and a commitment re-dated afterwards carries no receipt for
        its new due time.
        """
        validate_progress(scope, 'reportScope')
        now = int(self.clock() * 1000)
        for _ in range(POLICY['casAttempts']):
            row = self._head()
            if row is None or row['value']['scope'] != scope:
                return False
            head = deepcopy(row['value'])
            receipts = {item['title']: item for item in head.get('commitments', ())}
            for due in commitments:
                receipt = receipts.get(due['title'])
                if receipt is None or receipt['state'] != 'open' or receipt['dueAt'] != due['dueAt']:
                    continue
                if receipt.get('wake', {}).get('dueAt') == due['dueAt']:
                    continue
                receipt['wake'] = {'dueAt': due['dueAt'], 'at': now, 'via': via,
                                   'commandId': command_id, 'disposition': disposition}
            if head == row['value']:
                return False
            validate_progress(head, 'progressHead')
            try:
                self.client(write=True).command('record.put', {
                    'namespace': POLICY['namespace'], 'key': self.key, 'value': head,
                    'expected_version': row['version'],
                }, 'coordinator-commitment-wake:' + uuid.uuid4().hex)
            except StorageError as exc:
                if exc.code != 'database_conflict' and not exc.retryable:
                    raise
                continue
            return True
        raise StorageError('database_conflict', 'Task board changed concurrently; wake not receipted')

    def _arm(self, owner, scope, moment):
        if moment is None:
            return
        from lib.conversations.coordinator_commitments import arm_due_commitments
        try:
            arm_due_commitments(owner, scope, moment)
        except Exception:
            # The board is committed; the next board write arms the same row.
            logger.warning('[Commitments] due wake of %s not armed now; the next board write retries',
                           scope['coordinatorId'], exc_info=True)

    def untranslated_texts(self, scope, target):
        """Texts of exactly ``scope``'s board without a ``target`` translation, in one storage read."""
        row = self._head()
        if row is None or row['value']['scope'] != scope:
            return []
        known = _stored_translations(row['value'], target)
        return [text for text in board_texts(row['value']['board']) if text not in known]

    def attach_translation(self, scope, target, texts):
        """Merge ``{board text: translation}`` into exactly ``scope``'s current board.

        Returns False without writing when the head belongs to another binding or
        nothing would change. Another ``target`` replaces the stored translations;
        a text the board no longer shows is dropped. Whitespace runs collapse to
        one space; empty translations are skipped.
        """
        for _ in range(POLICY['casAttempts']):
            row = self._head()
            if row is None or row['value']['scope'] != scope:
                return False
            board = row['value']['board']
            merged = _stored_translations(row['value'], target)
            for source, text in texts.items():
                normalized = ' '.join(text.split())
                if normalized:
                    merged[source] = normalized
            translation = _translation(target, merged, board)
            if translation == row['value'].get('translation'):
                return False
            head = {'scope': scope, 'board': board}
            if translation:
                head['translation'] = translation
            if row['value'].get('commitments'):
                head['commitments'] = deepcopy(row['value']['commitments'])
            for field in ('taskKeys', 'historyDelta'):
                if field in row['value']:
                    head[field] = deepcopy(row['value'][field])
            validate_progress(head, 'progressHead')
            try:
                self.client(write=True).command('record.put', {
                    'namespace': POLICY['namespace'], 'key': self.key, 'value': head,
                    'expected_version': row['version'],
                }, 'coordinator-progress-translation-cas:' + uuid.uuid4().hex)
            except StorageError as exc:
                if exc.code != 'database_conflict' and not exc.retryable:
                    raise
                # A board replaced meanwhile keeps only translations of texts it still shows.
                continue
            self._hint()
            return True
        raise StorageError('database_conflict', 'Task board changed concurrently; translation not attached')

    def read(self, *, principal=None):
        from lib.identity import PrincipalContext
        from .coordinator_work_evidence import read_evidence, unknown_evidence
        evidence_allowed = (isinstance(principal, PrincipalContext) and principal.kind == 'user'
                            and principal.owner_user_id == self.owner and principal.has_scope('chat'))
        scope, binding = self._scope()
        result = {'projectPath': self.project, 'conversationId': binding['conversationId'],
                  'bindingRevision': binding['revision'], 'board': None,
                  'workEvidence': unknown_evidence()}
        if scope is not None:
            row = self._head()
            if row and row['value']['scope'] == scope:
                result['board'] = deepcopy(row['value']['board'])
                if 'translation' in row['value']:
                    result['translation'] = deepcopy(row['value']['translation'])
                if row['value'].get('commitments'):
                    result['commitments'] = deepcopy(row['value']['commitments'])
                result['taskKeys'] = board_task_keys(row['value'])
                if evidence_allowed:
                    result['workEvidence'] = read_evidence(
                        self.owner, self.project, scope, row['value'], client=self.client,
                        bindings=self.bindings, principal=principal)
                    current_scope, _ = self._scope()
                    current_head = self._head()
                    if (current_scope != scope or current_head is None
                            or current_head['version'] != row['version']):
                        result['workEvidence'] = unknown_evidence()
                    elif result['workEvidence']['requestId'] is not None:
                        # Unknown evidence never reads as "nothing runs": omit the flag instead.
                        running = {item['assignmentId'] for item in result['workEvidence']['rows']
                                   if item['phase'] == 'active'}
                        result['idleTaskKeys'] = idle_task_keys(row['value'], running)
        return validate_progress(result, 'progressState')

    def _hint(self):
        try:
            self.notify()
        except Exception:
            # Push is a refresh hint; the committed head is the board.
            logger.debug('Coordinator task board push unavailable', exc_info=True)

    def _notify(self):
        # Reaches only sockets of the writing process: a write by the resident
        # execution host refreshes browsers through its round (``progress_view``).
        from lib.agent_core.push import push_event
        push_event('project', project_channel_key(self.project),
                   {'projectKey': self.project, 'kind': 'coordinator_progress'}, user_id=self.owner)


class ProgressReceipt(str):
    """Model text carrying the browser's ``progress_view`` of the same write out of band."""

    def __new__(cls, text, view):
        result = super().__new__(cls, text)
        result.progress_view = view
        return result


def _board_hint(scope, board):
    return {'projectPath': scope['projectPath'], 'conversationId': scope['coordinatorId'],
            'bindingRevision': scope['bindingRevision'], 'revision': board['revision']}


def progress_view(scope, board, state):
    """Round metadata of one progress write: the checklist and the board revision it shows.

    The round reaches the open Coordinator chat through the conversation stream,
    which every process's commits wake, so both views refresh without the push.
    """
    from lib.tools.todo import public_todo_state
    state = public_todo_state(state)
    return {'todos': deepcopy(state['todos']), 'todoState': state,
            'coordinatorProgress': _board_hint(scope, board)}


def coordinator_board_scope(task):
    """``reportScope`` of ``task`` when it is the bound Coordinator keeping a task board, else None.

    The Router Watchdog is bound but keeps no board. A Coordinator's role is
    pinned for its turn before any tool runs, so a failed lookup is another
    task's and keeps its own checklist; ended authority still stops the task.
    """
    from lib.llm.llm_errors import AbortedError
    from lib.tasks_pkg.coordinator_policy import coordinator_scope, coordinator_tool_ceiling
    try:
        scope = coordinator_scope(task)
        if not scope or 'update_task_progress' not in coordinator_tool_ceiling(scope, task):
            return None
    except AbortedError:
        raise
    except Exception:
        logger.warning('[Coordinator] task board role lookup failed; task=%s keeps its own checklist',
                       task.get('id'), exc_info=True)
        return None
    return {'owner': scope['user_id'], 'projectPath': scope['project_path'],
            'coordinatorId': scope['conversation_id'], 'bindingRevision': scope['binding_revision']}


def _translate(task, scope, publish):
    """Run ``publish(target)`` for the human's reading language, then queue the board's translation.

    Also after an unchanged board: that retries a translation that failed before.
    """
    from lib.conversations.coordinator_progress_translation import (
        progress_translation_target, schedule_progress_translation)
    target = progress_translation_target(task, scope)
    published = publish(target)
    if target is not None and published['board'] is not None:
        schedule_progress_translation(scope['owner'], scope['projectPath'], scope, target)
    return published


def publish_task_board(task, scope, request):
    """update_task_progress: publish the board, then hold its plan as the task's checklist."""
    from lib.tools.todo import adopt_todo_projection, todo_lock
    with todo_lock(task):
        published = _translate(task, scope, lambda target: CoordinatorTaskProgress(
            scope['owner'], scope['projectPath']).publish(scope, request, translation_target=target))
        plan = plan_from_board(published['board'])
        state = adopt_todo_projection(task, plan['todos'], plan['explanation'],
                                      binding_revision=scope['bindingRevision'],
                                      revision=published['board']['revision'])
    return published, state


def plan_publication(task):
    """``publish`` for ``apply_todo_write_to_task`` when the checklist is a task board, else None.

    It writes each accepted checklist as the board; a write the board refuses
    raises ValueError naming the cause, so the checklist and board stay one.
    """
    scope = coordinator_board_scope(task)
    if scope is None:
        return None

    def publish(state):
        from lib.tools.todo import board_todo_state, todo_state_from_task
        try:
            published = _translate(task, scope, lambda target: CoordinatorTaskProgress(
                scope['owner'], scope['projectPath']).publish_plan(
                    scope, state['todos'], state['explanation'], translation_target=target))
        except PermissionError as error:
            raise ValueError(f'{error}; start a new authorized turn') from error
        except StorageError as error:
            raise ValueError(f'could not confirm the task board update ({error}); read it before retrying') from error
        except ValueError as error:
            raise ValueError(f'this checklist is the task board shown to the user, and {error}') from error
        if published['board'] is None:
            return {}
        board = published['board']
        plan = plan_from_board(board)
        projected = board_todo_state(todo_state_from_task(task), plan['todos'], plan['explanation'],
                                     binding_revision=scope['bindingRevision'], revision=board['revision'])
        return {'note': 'The user sees it as the task board above this chat.',
                'coordinatorProgress': _board_hint(scope, board), 'projectedState': projected}
    return publish
