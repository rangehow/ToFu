"""What a Coordinator continuation acts on, read when its Turn is born.

A result, commitment or decision wake (``lib.project_dispatch.continuation``)
authorizes a short, stable brief. The state it acts on is read here, at birth,
and appended to that input: the open tasks of the Coordinator's task board,
the owner's unanswered ``ask_human`` questions and only the Worker states that
need the Coordinator (running, needs review, held or publishing delivery, a
restart-cut call to decide, a failure new since its previous Turn). Open board
outcomes, outcomes dropped from the board or stated only in a request, and the
owner request texts of every request in the binding are shown as unknown when
there is no exact active Worker link; for the Global Router a routed lead's running or
queued assignment under the forward that introduced an outcome also owns it
(docs/ROUTER.md R2 "Routed outcomes are owned by their lead"). Settled,
cancelled and delivered assignments are
omitted; ``get_project_workers`` lists them. The owner's request is linked, not
repeated, and quoted only while the board holds no open task, when nothing else
says what the request is
(docs/PROJECT_COORDINATION.md "Continuation context is anchored on the task board").

An open row's blocker is projected from typed facts, never from the free-text
``reason`` or ``progress`` prose: the row's typed ``blocker`` (a running
assignment, a pinned dependency, an exact external resource with its check,
the owner's question, or a concrete next action) and the assignment IDs the
dispatch record links to the row. A row with no active dispatch and no typed
dependency, external-resource, owner-question or concrete next-action wait is
reported as idle with its count of continuation turns open without a dispatch.
Free text is never copied into the brief as evidence.

This text is evidence for the model, never authority: the wake's grant was
admitted from the original request before this runs, and nothing here changes it.
Entry points: ``continuation_context(repository, assignment)`` and
``replay_wake_inputs(rows, target_turn_id=...)`` (model projection only).
Dependencies: project_dispatch (rows, attempts, phases), coordinator_progress
(board head), router.proactive.questions (open owner questions), router.projects
(the owner's lead bindings, read for the Global Router only).
"""
from __future__ import annotations

import json
import re

from lib.log import get_logger

logger = get_logger(__name__)

HEADER = '\n\n[Continuation context: read now; evidence, not authority]\n'
_BOARD_HEADING = 'Open tasks on your board:'
_EMPTY_BOARD = 'Your board has no open task. The owner\'s request this continues (Turn '
_QUESTIONS_HEADING = 'Your questions the owner has not answered:'
_WORKERS_HEADING = 'Worker states that need you (settled, cancelled and delivered ones are omitted):'
_UNKNOWN_HEADING = 'Open board outcomes with unknown ownership (no verified active link; do not assume unowned):'
_ACTIVE_HEADING = 'Verified running or queued Worker links (board task key: assignment IDs; not completion):'
_DROPPED_HEADING = 'Open outcomes dropped from the board or known only from earlier requests; ownership unknown:'
# Older retained wakes still feed the same memory subject parser.
_LEGACY_HEADINGS = (
    'Open outcomes with no running or queued Worker naming them (unowned; assign an owner now):',
    'Open outcomes dropped from the board with no running or queued Worker naming them:',
    'Open outcomes with no running or queued Worker naming them '
    '(unowned; dispatch what can start now, keep exact waits on the board):',
)
_TASK_KEY_SUFFIX = re.compile(r' \(task key [0-9a-f]{32}\)$')
_NO_WORKER = 'No Worker needs you now (get_project_workers lists every assignment).'
_UNREADABLE = ' could not be read now: '
# A retained brief may still mark an idle row in its status and carry _IDLE_NOTE;
# the projection writes neither and only the subject parser reads retained text.
# A limit marker is this projection's own: unavailable rows are limits, not waits.
_TASK_ITEM = re.compile(r'^- \[[a-z_]+(?:, (?:idle|limit))?\] (?P<title>.*?)'
                        r'(?: \(due [^)\n]*\))?(?:: (?P<progress>.*))?$')
_IDLE_NOTE = ('(idle rows name no running Worker, dependency or concrete next action: start or dispatch one now, '
              'or give it a typed blocker.)')
# Limits are terminal, not waits: saying what stays unverified is the only honest action.
_LIMIT_NOTE = ('(limit rows name an external resource that cannot be obtained now: they are not waits and need no '
               'dispatch; state what stays unverified and plan around it.)')
_BOARD_FACTS = re.compile(r' \(dispatched: .*\)$')
_QUESTION_ITEM = re.compile(r'^- (?P<question>.*?)(?: \(your default meanwhile: (?P<default>.*)\))? \[Turn [^\]\n]*\]$')
_OPEN = ('pending', 'in_progress')
_PHASE_LINES = {
    'running': 'running',
    'needs_review': 'result to review (review_project_work)',
    'held': 'delivery held',
    'delivering': 'accepted, publishing',
    'unpinned': ('accepted result, no publication requested; workspace receipt is not final: '
                 'inspect get_project_assignment before deciding whether publication is needed'),
    'needs_decision': 'a restart cut off a call of unknown effect: check it, then control_conversation',
    'interrupted': 'interrupted',
    'on_hold': 'held by you: control_conversation action=resume continues it, cancel_project_work ends it',
    'ended': 'ended',
}
_WORKER_ITEM = re.compile(r'^- [^:\n]+: (?:' + '|'.join(map(re.escape, _PHASE_LINES.values()))
                          + r') \u2014 (?P<title>.*)$')


def _board(repository, assignment):
    """The open tasks of this binding's board, or None when it is unreadable."""
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    from lib.conversations.coordinator_task_history import CoordinatorTaskHistory
    scope, _, head = CoordinatorTaskProgress(repository.owner, repository.project, client=repository.client,
        clock=repository.clock, bindings=repository.binding_repository).current()
    if head is None or not _matches_wake(scope, assignment):
        return []
    keys = head.get('taskKeys') or []
    tasks = [dict(task, key=keys[index]) if index < len(keys) and keys.count(keys[index]) == 1 else task
             for index, task in enumerate(head['board']['tasks']) if task['status'] in _OPEN]
    try:
        first_seen = {entry['key']: entry.get('firstSeenAt')
                      for entry in CoordinatorTaskHistory(repository.owner, repository.project,
                          client=repository.client, clock=repository.clock,
                          bindings=repository.binding_repository).tasks(scope['coordinatorId'])}
    except Exception as error:
        logger.warning('[Coordinator] continuation context: task history unreadable: %s',
                       type(error).__name__, exc_info=True)
        first_seen = {}
    for task in tasks:
        task['firstSeenAt'] = first_seen.get(task.get('key'))
    return tasks


def _matches_wake(scope, assignment):
    return (scope is not None and scope['owner'] == assignment['grant']['owner']
            and scope['projectPath'] == assignment['request']['projectPath']
            and scope['coordinatorId'] == assignment['request']['coordinatorId']
            and scope['bindingRevision'] == assignment['grant']['bindingRevision'])


def _questions(repository, coordinator_id):
    from lib.router.proactive.questions import open_questions
    return open_questions(repository.owner, coordinator_id=coordinator_id, client=repository.client)


def _binding_rows(repository, assignment):
    """Every assignment row of this wake's Coordinator binding, newest first."""
    from lib.project_dispatch.history import page
    rows, cursor = [], None
    while True:
        current = page(repository, cursor)
        rows.extend(row for row in current['rows'] if _same_binding(repository, assignment, row))
        cursor = current['olderCursor']
        if cursor is None:
            return rows


def _same_binding(repository, assignment, row):
    request, grant, wake_grant = row['request'], row['grant'], assignment['grant']
    return (request['coordinatorId'] == assignment['request']['coordinatorId']
            and request['projectPath'] == repository.project
            and grant['owner'] == repository.owner == wake_grant['owner']
            and grant.get('bindingRevision') == wake_grant['bindingRevision']
            and grant['tenant'] == wake_grant['tenant'])


def _awaits_lead(row):
    """An unreviewed result, or an accept that pinned no checkpoint, read from the row alone."""
    review = row.get('review')
    return (row['request'].get('profile', {}).get('mode') != 'coordinator'
            and not row['grant']['revoked'] and row.get('execution') is not None
            and (review is None or (review.get('decision') == 'accept' and not row.get('publication'))))


def _workers(repository, assignment, binding_rows=None):
    """``(assignment_id, phase, title)`` of the Worker states that need the Coordinator.

    The wake's own request lists every actionable state. A Worker of an earlier
    request of the same binding is listed while only this lead can move it: its
    result was never reviewed, or was accepted without a checkpoint to publish.
    Nothing else wakes the lead for those once their request has finished.
    """
    from lib.project_dispatch.continuation import _group, worker_phase, worker_summaries
    request = assignment['request']
    origin = request['delegation']['attemptId']
    value, _ = repository._load(repository._selection(sources=[origin]))
    rows = _group(value, origin)
    own = {row['id'] for row in rows}
    earlier = [row for row in (binding_rows if binding_rows is not None
                               else _binding_rows(repository, assignment))
               if row['id'] not in own and _awaits_lead(row)]
    pinned = {row['id']: bool(row.get('publication')) for row in rows + earlier}
    summaries = worker_summaries(repository, rows)
    summaries += [summary for summary in worker_summaries(repository, earlier)
                  if worker_phase(summary) in ('needs_review', 'held', 'delivering')]
    previous = repository.get_attempt((request.get('coordinatorWake') or {}).get('previousAttemptId') or origin)
    since = (previous or {}).get('createdAt', 0)
    found = []
    for summary in summaries:
        phase = worker_phase(summary)
        if phase in ('settled', 'cancelled'):
            continue
        if phase in ('ended', 'interrupted'):
            attempt = repository.get_attempt(summary['attempt_id']) or {}
            if phase == 'interrupted':
                from lib.conversations.restart_resume import unknown_effects_to_review
                turn = repository.turn_get(summary['conversation_id'], attempt.get('turnId'),
                                           user_id=repository.owner) or {}
                if unknown_effects_to_review(turn):
                    phase = 'needs_decision'
            if phase != 'needs_decision' and (attempt.get('settledAt') or 0) < since:
                continue  # Its previous Turn already had this result to judge.
        if phase in ('held', 'delivering') and not pinned[summary['assignment_id']]:
            phase = 'unpinned'
        found.append((summary['assignment_id'], phase, summary['title'].split('\n', 1)[0]))
    return found


def _outcome_ownership(tasks, active, dropped, routed):
    """Only committed board/history identities can join ordinary live assignments."""
    return {
        'active': {task['key']: active[task['key']] for task in [*tasks, *dropped]
                   if not task.get('historicalOnly') and task.get('key') in active},
        'unknown': [task for task in tasks if task.get('key') not in active and task.get('key') not in routed],
        'dropped': [task for task in dropped if task.get('key') not in routed
                    and (task.get('historicalOnly') or task.get('key') not in active)],
    }


def _task_links(repository, rows):
    """``(active, dispatched, active_ids, assignment_ids)`` of this binding's Workers.

    ``active`` keeps the existing running/queued ownership meaning. ``dispatched``
    is the computed fact the brief reports: an assignment existed for the row,
    whatever its phase now is. ``active_ids`` and ``assignment_ids`` let the
    brief verify a typed ``active_dispatch`` without trusting the row's prose.
    """
    from lib.project_dispatch.continuation import worker_phase, worker_summaries

    active, dispatched, active_ids, assignment_ids = {}, {}, set(), set()
    for row, summary in zip(rows, worker_summaries(repository, rows)):
        assignment_ids.add(row['id'])
        if summary['status'] in ('queued', 'pending', 'running'):
            active_ids.add(row['id'])
        for key in row['request'].get('boardTaskKeys') or ():
            dispatched.setdefault(key, []).append((row['id'], worker_phase(summary)))
            if summary['status'] in ('queued', 'pending', 'running'):
                active.setdefault(key, []).append(row['id'])
    return active, dispatched, active_ids, assignment_ids


def _lead_live_sources(owner, tenant, client):
    """``(lead conversation, authorization source)`` of every running/queued lead assignment.

    Every project lead of the owner, over its whole dispatch history for its
    current binding; the router home and the Watchdog are not leads.
    """
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.project_dispatch.continuation import worker_summaries
    from lib.project_dispatch.history import page
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.router.home import is_router_home
    from lib.router.projects import binding_page
    from lib.router.watchdog.binding import is_watchdog_location
    cursor = None
    while True:
        _bindings, paths, cursor = binding_page(owner, cursor, client)
        for project in paths:
            if is_router_home(owner, project) or is_watchdog_location(owner, project):
                continue
            binding = ProjectCoordinationRepository(owner, client=client).read(project)
            if binding['availability'] != 'available':
                continue
            lead = ProjectDispatchRepository(owner, project, client=client)
            rows, older = [], None
            while True:
                current = page(lead, older)
                rows.extend(row for row in current['rows']
                            if row['request'].get('coordinatorId') == binding['conversationId']
                            and row['request'].get('projectPath') == lead.project
                            and row['grant'].get('owner') == owner and row['grant'].get('tenant') == tenant
                            and row['grant'].get('bindingRevision') == binding['revision']
                            and row['request'].get('profile', {}).get('mode') != 'coordinator')
                older = current['olderCursor']
                if older is None:
                    break
            for row, summary in zip(rows, worker_summaries(lead, rows)):
                if summary['status'] in ('queued', 'pending', 'running'):
                    yield binding['conversationId'], row['request']['authorizationSource']
        if cursor is None:
            return


def _source_identity(source):
    """Authored message identity, not just the carrier Turn."""
    if 'ownerDelegation' in source:
        return None
    conversation, turn = source.get('conversationId'), source.get('turnId')
    if not all(isinstance(value, str) and value for value in (conversation, turn)):
        return None
    fields = [name for name in ('steerId', 'guidanceId', 'instructionId') if name in source]
    if not fields:
        return conversation, turn, None, None
    if len(fields) != 1 or not isinstance(source[fields[0]], str) or not source[fields[0]]:
        return None
    return conversation, turn, fields[0], source[fields[0]]


def _routed_request_sources(repository, router_id, tenant):
    """Exact router source messages forwarded to a lead with active work."""
    from lib.router.home import is_router_home
    if not is_router_home(repository.owner, repository.project):
        return set()
    from lib.turn_initiation import steered_input_turn
    sources = set()
    for lead_id, source in _lead_live_sources(repository.owner, tenant, repository.client):
        if source['conversationId'] != lead_id or 'guidanceId' in source or 'ownerDelegation' in source:
            continue
        turn = repository.turn_get(lead_id, source['turnId'], user_id=repository.owner)
        if 'steerId' in source:
            try:
                turn = steered_input_turn(turn, source['steerId'])
            except ValueError:
                continue
        origin = ((turn or {}).get('projection') or {}).get('origin')
        forwarded = origin.get('forwardedFrom') if isinstance(origin, dict) else None
        if isinstance(forwarded, dict) and forwarded.get('conversationId') == router_id:
            identity = _source_identity(forwarded)
            if identity is not None:
                sources.add(identity)
    return sources


def _routed_keys(sources, outcomes):
    """Only a matching authored source message proves coverage, never timing."""
    return {outcome['key'] for outcome in outcomes if outcome.get('sourceIdentity') in sources}


def _history_pages(history, group):
    """Every task of one group of the current binding's durable board history."""
    tasks = []
    cursor = None
    while True:
        current = history.read(group=group, cursor=cursor)
        tasks.extend(current['tasks'])
        cursor = current['nextCursor']
        if cursor is None:
            return tasks


def _history_keys(task):
    from lib.conversations.coordinator_task_history import title_key
    return {task['key'], title_key(task['title']), *(title_key(title) for title in task.get('formerTitles') or ())}


def _ownership(repository, assignment, tasks, binding_rows):
    """Exact active links and unknown outcomes across the current binding.

    Current tasks and durable history can carry an observed key. Transcript and
    request-only outcomes remain visible but never supply an ordinary link.
    """
    from lib.conversations.coordinator_task_history import CoordinatorTaskHistory, fold_boards
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    progress = CoordinatorTaskProgress(repository.owner, repository.project, client=repository.client,
                                       clock=repository.clock, bindings=repository.binding_repository)
    scope, _, head = progress.current()
    if not _matches_wake(scope, assignment):
        return None
    current_keys = set((head or {}).get('taskKeys') or ())
    rows = []
    requests = []
    for row in binding_rows:
        if row['request'].get('profile', {}).get('mode') == 'coordinator':
            requests.append(row)
        else:
            rows.append(row)
    active, dispatched, active_ids, assignment_ids = _task_links(repository, rows)
    history = CoordinatorTaskHistory(repository.owner, repository.project, client=repository.client,
                                     clock=repository.clock, bindings=repository.binding_repository)
    page = history.read(group='dropped')
    recorded = page.get('recorded', True)
    dropped = [task for task in page['tasks'] if task['key'] not in current_keys]
    cursor = page['nextCursor']
    while cursor is not None:
        current = history.read(group='dropped', cursor=cursor)
        dropped.extend(task for task in current['tasks'] if task['key'] not in current_keys)
        cursor = current['nextCursor']
    if not recorded and scope is not None:
        # A head written before task history keeps every earlier board in the
        # transcript; fold those revisions here without writing anything.
        from lib.conversations.coordinator_task_history import transcript_boards
        boards = [board for board in transcript_boards(scope)
                  if board['revision'] <= (head or {}).get('board', {}).get('revision', 0)]
        records, _ = fold_boards(scope, boards)
        for key, record in records.items():
            if key in current_keys:
                continue
            if record['status'] in _OPEN and record.get('departedAt'):
                dropped.append({'key': key, 'title': record['title'], 'status': record['status'],
                                'historicalOnly': True})
    recorded_keys = set()
    if recorded and scope is not None:
        for group in ('in_progress', 'pending', 'completed', 'cancelled'):
            recorded_keys.update(key for task in _history_pages(history, group) for key in _history_keys(task))
    stated = _stated_outcomes(repository, requests, recorded_keys)
    merged = {task['key']: task for task in dropped}
    for task in stated:
        merged.setdefault(task['key'], dict(task, historicalOnly=True))
    sources = _routed_request_sources(repository, assignment['request']['coordinatorId'],
                                    assignment['grant']['tenant'])
    routed = _routed_keys(sources, merged.values())
    if not _matches_wake(progress._scope()[0], assignment):
        return None  # Rebinding during the history walk invalidates its evidence.
    ownership = _outcome_ownership(tasks, active, list(merged.values()), routed)
    ownership['dispatched'] = dispatched
    ownership['activeIds'] = active_ids
    ownership['assignmentIds'] = assignment_ids
    return ownership


def _stated_outcomes(repository, requests, recorded_keys):
    """Outcomes a request's durable Turn states that the current board no longer shows.

    Its own board revisions, read through the task history's parser and folded
    with its identity rule, supply the dropped tasks the current head has never
    recorded. Its owner request text is added when that Turn states no outcome
    at all and the request has no durable finish receipt; a request the lead
    finished, or whose work the current board/history already states, adds none.
    """
    from lib.conversations.coordinator_task_history import (
        boards_from_rounds, fold_boards, title_key)
    value, _ = repository._load()
    settlements = {receipt['sourceAttemptId'] for receipt in value.get('coordinatorSettlements', [])}
    outcomes = {}
    for row in requests:
        # An unborn request (this wake's own row at its birth, or a deferred
        # wake) has no Turn yet; storage refuses a Turn read without an id.
        if row.get('execution') is None:
            continue
        turn = repository.turn_get(row['request']['recipientId'], row['execution']['turnId'],
                                   user_id=repository.owner)
        if not isinstance(turn, dict):
            continue
        origin = row['request']['delegation']['attemptId']
        scope = {'owner': repository.owner, 'projectPath': repository.project,
                 'coordinatorId': row['request']['coordinatorId'],
                 'bindingRevision': row['grant']['bindingRevision']}
        boards = boards_from_rounds(list(turn.get('toolRounds') or ()))
        records, _ = fold_boards(scope, boards)
        for key, record in records.items():
            if record['status'] not in _OPEN or not record.get('departedAt'):
                continue
            if key in recorded_keys or title_key(record['title']) in recorded_keys:
                continue
            outcomes.setdefault(key, {'key': key, 'title': record['title'], 'status': record['status']})
        if not boards and origin not in settlements:
            source = row['request']['authorizationSource']
            identity = _source_identity(source)
            text = _source_request(repository, source)
            if text:
                outcomes.setdefault(('request', origin, identity),
                                    {'key': 'request:' + row['id'], 'title': text, 'status': 'in_progress',
                                     'sourceIdentity': identity})
    return list(outcomes.values())


def _source_request(repository, source):
    """The owner's words of the request this wake serves."""
    if 'ownerDelegation' in source:
        return ''  # A lead acted on its own judgment: no owner words to quote.
    turn = repository.turn_get(source['conversationId'], source['turnId'], user_id=repository.owner)
    if 'guidanceId' in source:
        from lib.project_dispatch.delegation import answered_question
        answered = answered_question((turn or {}).get('projection'), source['guidanceId'])
        turn = {'projection': {'content': answered['guidanceResponse'] if answered else ''}}
    elif 'steerId' in source:
        from lib.turn_initiation import steered_input_turn
        turn = steered_input_turn(turn, source['steerId'])
    content = ((turn or {}).get('projection') or {}).get('content') or ''
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    from lib.project_dispatch.repository import CONTRACT
    limit = CONTRACT['delegationDefaults']['sourceExcerptChars']
    return content[:limit] + (' [excerpt; get_conversation has the rest]' if len(content) > limit else '')


def _read(what, read, lines):
    """One section's read; a failure is stated in the input instead of failing the wake's birth."""
    try:
        return read()
    except Exception as error:
        logger.warning('[Coordinator] continuation context: %s unreadable: %s', what, type(error).__name__,
                       exc_info=True)
        lines.append('(' + what + _UNREADABLE + type(error).__name__ + '; read it yourself before '
                     'relying on it.)')
        return None


# The first half stores the wait as a typed ``blocker`` on the task row. The
# projection renders only that typed claim; a free-text ``reason`` never does.
_NON_ACTION_WORDS = frozenset(
    'a an and as before continue follow handle it keep later monitor more need needs on ongoing proceed '
    'review same still stay the then this to up wait waiting work'.split())


def _blocker_wait(blocker):
    """The typed wait in the human's terms, or None when the blocker is not complete."""
    if not isinstance(blocker, dict):
        return None
    kind = blocker.get('kind')
    if kind == 'active_dispatch':
        value = blocker.get('assignmentId')
        return 'Worker ' + value + ' is running' if isinstance(value, str) and value else None
    if kind == 'blocked_by_dependency':
        value, revision = blocker.get('assignmentId'), blocker.get('revision')
        return ('assignment ' + value + ' at revision ' + str(revision)
                if isinstance(value, str) and value and type(revision) is int and revision >= 1 else None)
    if kind == 'needs_external_resource':
        if _task_is_limit({'blocker': blocker}):
            return None  # A limit is not a wait; _board_task_line states it as a limit.
        resource, check, action = blocker.get('resource'), blocker.get('check'), blocker.get('ownerAction')
        if not (isinstance(resource, str) and resource and isinstance(check, str) and check):
            return None
        text = resource + ' (proved by: ' + check + ')'
        if isinstance(action, str) and action:
            text += '; you: ' + action
        return text
    if kind == 'needs_owner_decision':
        value = blocker.get('questionId')
        if not (isinstance(value, str) and value):
            return None
        question = blocker.get('question')
        return 'your answer to ' + value + (': ' + question if isinstance(question, str) and question else '')
    if kind == 'next_action':
        value = blocker.get('action')
        return 'next action: ' + value if isinstance(value, str) and value else None
    return None


def _task_is_limit(task):
    """Whether the row states a terminal limit: its external resource cannot be obtained now."""
    blocker = task.get('blocker') if isinstance(task.get('blocker'), dict) else {}
    return blocker.get('kind') == 'needs_external_resource' and blocker.get('unavailable') is True


def _generic_action(action):
    words = re.findall(r"[a-z']+", action.lower())
    return not words or all(word in _NON_ACTION_WORDS for word in words)


def _row_is_idle(task, links, active_ids):
    """Nothing acts on this row now and no typed claim names a real wait.

    A queued or starting dispatch already owns its work, so it is not idle.
    Unlike the board's display flag, an in-progress row with no active work is
    idle too: that is the stale in-progress row the lead must move.
    """
    blocker = task.get('blocker') if isinstance(task.get('blocker'), dict) else {}
    kind = blocker.get('kind')
    if kind in ('blocked_by_dependency', 'needs_external_resource', 'needs_owner_decision'):
        return False
    if kind == 'next_action' and not _generic_action(str(blocker.get('action') or '')):
        return False
    if kind == 'active_dispatch':
        return blocker.get('assignmentId') not in active_ids
    return not any(assignment_id in active_ids for assignment_id, _ in links or ())


def _continuation_wake_times(repository, binding_rows):
    """Creation times of the continuation wakes already dispatched in this binding.

    Each wake is an ordinary Coordinator request row carrying ``coordinatorWake``;
    its attempt's creation time is when that continuation Turn started.
    """
    times = []
    for row in binding_rows:
        if not row['request'].get('coordinatorWake'):
            continue
        attempt_id = (row.get('execution') or {}).get('attemptId')
        attempt = repository.get_attempt(attempt_id) if attempt_id else None
        if attempt and attempt.get('createdAt'):
            times.append(int(attempt['createdAt']))
    return times


def _board_task_line(task, dispatched, assignment_ids, active_ids, wake_times):
    """One open row: its typed blocker or idle instruction, and computed dispatch facts.

    ``dispatched`` maps a board key to its ``(assignment id, phase)`` links; a
    typed ``active_dispatch`` naming a known assignment also counts. The row's
    free-text ``reason`` and ``progress`` prose is never copied.
    """
    limit = _task_is_limit(task)
    line = '- [' + task['status'] + (', limit' if limit else '') + '] ' + task['title']
    if task.get('due'):
        line += ' (due ' + task['due'] + ')'
    known = dispatched is not None
    links = dispatched.get(task.get('key')) if known and task.get('key') else None
    blocker = task.get('blocker') if isinstance(task.get('blocker'), dict) else {}
    wait = _blocker_wait(blocker)
    assigned = None if not known else (
        bool(links) or (blocker.get('kind') == 'active_dispatch'
                        and blocker.get('assignmentId') in (assignment_ids or ())))
    notes = []
    if limit:
        notes.append('Limit: ' + str(blocker.get('resource')) + ' cannot be obtained now; stays unverified: '
                     + str(blocker.get('limit')))
    elif wait:
        notes.append('Waiting for: ' + wait)
    else:
        notes.extend('assignment ' + assignment_id + (' (' + phase + ')' if phase else '')
                     for assignment_id, phase in links or ())
    if known and _row_is_idle(task, links, active_ids or ()):
        notes.append('idle: dispatch or cancel')
    if notes:
        line += ': ' + '; '.join(notes)
    if not known:
        facts = 'dispatch facts unreadable'
    else:
        first = task.get('firstSeenAt')
        count = 0 if assigned else (None if wake_times is None else sum(
            1 for at in wake_times if first is None or at >= first))
        facts = ('dispatched: ' + ('yes' if assigned else 'no') + '; '
                 + ('continuation turns unreadable' if count is None else
                    'open ' + str(count) + ' continuation turn' + ('' if count == 1 else 's')
                    + ' without a dispatch'))
    return line + ' (' + facts + ')' + _task_key_suffix(task)


def _task_key_suffix(task):
    return ' (task key ' + task['key'] + ')' if task.get('key') and not task.get('historicalOnly') else ''


def continuation_context(repository, assignment) -> str:
    """The appended context of one Coordinator wake input (``HEADER`` and its sections)."""
    request = assignment['request']
    lines = []
    tasks = _read('Your task board', lambda: _board(repository, assignment), lines)
    binding_rows = _read('Your assignments', lambda: _binding_rows(repository, assignment), lines)
    ownership = (_read('Outcome ownership',
                       lambda: _ownership(repository, assignment, tasks, binding_rows), lines)
                 if tasks is not None and binding_rows is not None else None)
    if tasks:
        try:
            wake_times = (_continuation_wake_times(repository, binding_rows)
                          if binding_rows is not None else None)
        except Exception as error:
            logger.warning('[Coordinator] continuation context: continuation turns unreadable: %s',
                           type(error).__name__, exc_info=True)
            wake_times = None
        dispatched = ownership.get('dispatched') if ownership is not None else None
        active_ids = ownership.get('activeIds') if ownership is not None else None
        assignment_ids = ownership.get('assignmentIds') if ownership is not None else None
        lines.append(_BOARD_HEADING)
        limit = False
        for task in tasks:
            lines.append(_board_task_line(task, dispatched, assignment_ids, active_ids, wake_times))
            limit = limit or _task_is_limit(task)
        if limit:
            lines.append(_LIMIT_NOTE)
    elif tasks is not None:
        source = request['authorizationSource']
        lines.append(_EMPTY_BOARD + source['turnId'] + '):')
        excerpt = _read('The owner\'s request', lambda: _source_request(repository, source), lines)
        if excerpt is not None:
            lines.append(excerpt)
    questions = _read('Your open owner questions', lambda: _questions(repository, request['coordinatorId']),
                      lines)
    if questions:
        lines.append(_QUESTIONS_HEADING)
        for question in questions:
            line = '- ' + str(question.get('question') or '').strip()
            if question.get('default'):
                line += ' (your default meanwhile: ' + str(question['default']) + ')'
            lines.append(line + ' [Turn ' + str(question.get('turnId')) + ']')
    if ownership:
        if ownership['active']:
            lines.append(_ACTIVE_HEADING)
            lines.extend(f"- [active_owner] {key}: {', '.join(ids)}" for key, ids in ownership['active'].items())
        if ownership['unknown']:
            lines.append(_UNKNOWN_HEADING)
            lines.extend('- [ownership_unknown] ' + task['title'] + _task_key_suffix(task)
                         for task in ownership['unknown'])
        if ownership['dropped']:
            lines.append(_DROPPED_HEADING)
            lines.extend('- [dropped] ' + task['title'] + _task_key_suffix(task) for task in ownership['dropped'])
    workers = _read('Worker states', lambda: _workers(repository, assignment, binding_rows), lines)
    if workers:
        lines.append(_WORKERS_HEADING)
        lines.extend('- ' + identity + ': ' + _PHASE_LINES[phase] + ' — ' + title
                     for identity, phase, title in workers)
    elif workers is not None:
        lines.append(_NO_WORKER)
    return HEADER + '\n'.join(lines)


def continuation_context_subject(text: str) -> str | None:
    """What a wake's appended context is about, or None when ``text`` has none.

    The open tasks' titles and progress, the unanswered questions with their
    defaults, the titles of the Worker states that need the Coordinator and the
    owner's request excerpt; this module's headings, statuses, due times and
    identities are dropped. Memory selection reads it (lib/memory/prefetch/_query.py).
    """
    start = text.find(HEADER)
    if start < 0:
        return None
    subject = []
    for line in text[start + len(HEADER):].split('\n'):
        if (line in (_BOARD_HEADING, _QUESTIONS_HEADING, _WORKERS_HEADING, _NO_WORKER, _IDLE_NOTE,
                     _LIMIT_NOTE, _UNKNOWN_HEADING, _ACTIVE_HEADING, _DROPPED_HEADING, *_LEGACY_HEADINGS)
                or line.startswith((_EMPTY_BOARD, '- [active_owner] '))
                or (line.startswith('(') and _UNREADABLE in line)):
            continue
        line = _TASK_KEY_SUFFIX.sub('', line)
        worker = _WORKER_ITEM.match(line)
        question = None if worker else _QUESTION_ITEM.match(line)
        task = None if worker or question else _TASK_ITEM.match(line)
        if worker:
            subject.append(worker['title'])
        elif question:
            subject.extend(part for part in (question['question'], question['default']) if part)
        elif task:
            note = _BOARD_FACTS.sub('', task['progress'] or '')
            note = note.replace('idle: dispatch or cancel', '').strip('; ')
            subject.extend(part for part in (task['title'], note) if part)
        elif line.strip():
            subject.append(line)
    return '\n'.join(subject)


_SUMMARY_REFUSALS = frozenset({'summary_dispatch_failed', 'summary_route_unavailable'})
# Older settlements keep ContextCompactionError as an internal envelope. Only
# recognize the exact producer diagnostic, corroborated by the attempt's own
# first-round dispatch-guard activity (a failed summary alone can be nonfatal).
_LEGACY_SUMMARY_REFUSAL = re.compile(
    r'Automatic context compaction failed locally before provider dispatch '
    r'\((?P<code>[a-z_]+)\); the prompt remains [\d,]+ tokens\. '
    r'No main-provider request was sent\.')


def _undispatched_wake_reply(turn):
    """Require a first-round local refusal and no output, tools, or owner steer."""
    if turn.get('status') != 'failed' or not turn.get('currentAttemptId'):
        return False
    projection = turn.get('projection') or {}
    if turn.get('toolRounds') or any(projection.get(key) for key in (
            'content', 'thinking', 'toolRounds', 'toolSummary', 'apiRounds', 'images',
            '_responsesItems', '_anthropicContentBlocks', '_userSteerInjects')):
        return False
    for segment in projection.get('segments') or ():
        if not isinstance(segment, dict):
            return False
        if segment.get('type') == 'system_note' and segment.get('action') == 'prompt':
            continue  # Composer diagnostics are not output from the model.
        if segment.get('type') != 'text' or segment.get('text'):
            return False
    settlement = turn.get('settlement') or {}
    error = settlement.get('error')
    if settlement.get('cause') != 'generation_error' or not isinstance(error, dict):
        return False
    attribution = error.get('failureAttribution')
    if not isinstance(attribution, dict) or attribution.get('roundNum') != 1:
        return False
    if (error.get('origin') == 'local' and error.get('providerReached') is False
            and error.get('failureStage') == 'prompt_admission'):
        return error.get('reason') in _SUMMARY_REFUSALS
    raw = error.get('raw')
    if error.get('source') != 'orchestrator' or not isinstance(raw, str) or error.get('providerReached') is True:
        return False
    match = _LEGACY_SUMMARY_REFUSAL.fullmatch(raw)
    if not match or match['code'] not in _SUMMARY_REFUSALS:
        return False
    timeline = projection.get('activityTimeline')
    if not isinstance(timeline, dict) or not isinstance(timeline.get('entries'), list):
        return False
    return any(isinstance(entry, dict) and entry.get('attemptId') == turn['currentAttemptId']
               and entry.get('reasonCode') == 'context_compaction'
               and entry.get('trigger') == 'dispatch_guard' and entry.get('status') == 'failed'
               and entry.get('roundNum') == 0 and isinstance(entry.get('detailArgs'), dict)
               and entry['detailArgs'].get('code') == match['code'] for entry in timeline['entries'])


def _plain_wake_input(turn):
    """Return the exact generated brief/source, shared snapshot, and Worker section."""
    if (turn.get('kind') != 'input' or turn.get('actor') != 'virtual_user' or turn.get('status') != 'completed'
            or (turn.get('settlement') or {}).get('cause') != 'orchestration_generated'):
        return None
    from lib.turn_initiation import INITIATOR_BRAIN, resolve_turn_initiator
    if resolve_turn_initiator(turn) != INITIATOR_BRAIN:
        return None
    projection = turn.get('projection') or {}
    content = projection.get('content')
    if not isinstance(content, str) or HEADER not in content or any(projection.get(key) for key in (
            'attachments', 'images', 'videos', 'pdfTexts', 'replyQuote', 'replyQuotes',
            'convRefTexts', 'planExecution', 'usageRecovery')):
        return None
    from lib.project_dispatch.continuation import CONTINUATION_BRIEF_MARK
    from lib.project_dispatch.delegation import WORKER_INPUT_SOURCE_TAIL, worker_input_brief
    brief, _, context = content.partition(HEADER)
    if (CONTINUATION_BRIEF_MARK not in brief or not brief.endswith(WORKER_INPUT_SOURCE_TAIL)
            or worker_input_brief(brief) is None or HEADER in context):
        return None
    # The renderer appends Worker evidence last. Keep each part opaque;
    # parsing task names or Worker titles would mistake quoted text for rows.
    shared, separator, workers = context.rpartition('\n' + _WORKERS_HEADING + '\n')
    if separator:
        return brief, shared, _WORKERS_HEADING + '\n' + workers
    return brief, context, ''


def replay_wake_inputs(rows, *, target_turn_id):
    """Elide byte-identical advisory context from older undispatched wake inputs.

    Work backwards across a run of adjacent wake pairs, keeping the newest wake
    whole. Other inputs/replies are boundaries, except human-facing reports
    (already not model context). The shared board/questions/ownership snapshot
    can repeat across briefs; a different brief (including source link, due
    promise or decision) and its own Worker evidence are retained. This only
    projects uncovered Turn rows, never storage, grants, or a cached prefix.
    """
    seen = set()
    reply = None
    result = None
    for index in range(len(rows) - 1, -1, -1):
        turn = rows[index]
        kind = turn.get('kind') or ''
        if kind.startswith('coordinator_report.'):
            continue
        if kind == 'reply' and turn.get('actor') == 'assistant':
            if reply is not None:
                seen.clear()
            reply = turn
            continue
        parts = _plain_wake_input(turn)
        following, reply = reply, None
        if (parts is None or following is None or following.get('parentTurnId') != turn['turnId']
                or following.get('laneId') != turn.get('laneId')):
            seen.clear()
            continue
        if following['turnId'] == target_turn_id or not _undispatched_wake_reply(following):
            seen.clear()
        brief, shared, workers = parts
        shared_key, workers_key = ('shared', shared), ('workers', brief, workers)
        drop_shared, drop_workers = shared_key in seen, workers_key in seen
        if (shared and drop_shared) or (workers and drop_workers):
            if result is None:
                result = list(rows)
            if drop_shared and drop_workers:
                result[index] = None  # Both sections and the exact brief recur later.
            else:
                evidence = '\n'.join(part for part, drop in ((shared, drop_shared), (workers, drop_workers))
                                     if part and not drop)
                content = brief + '\n\n[Repeated continuation evidence appears in a later wake.]'
                if evidence:
                    content += HEADER + evidence
                result[index] = {**turn, 'projection': {**turn['projection'], 'content': content}}
        seen.update((shared_key, workers_key))
    return rows if result is None else [turn for turn in result if turn is not None]
