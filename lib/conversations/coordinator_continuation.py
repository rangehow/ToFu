"""What a Coordinator continuation acts on, read when its Turn is born.

A result, commitment or decision wake (``lib.project_dispatch.continuation``)
authorizes a short, stable brief. The state it acts on is read here, at birth,
and appended to that input: the open tasks of the Coordinator's task board,
the owner's unanswered ``ask_human`` questions and only the Worker states that
need the Coordinator (running, needs review, held or publishing delivery, a
restart-cut call to decide, a failure new since its previous Turn). Open board
outcomes, outcomes dropped from the board or stated only in a request, and the
owner request texts of every request in the binding are named when no running
or queued Worker names them. Settled, cancelled and delivered assignments are
omitted; ``get_project_workers`` lists them. The owner's request is linked, not
repeated, and quoted only while the board holds no open task, when nothing else
says what the request is
(docs/PROJECT_COORDINATION.md "Continuation context is anchored on the task board").

This text is evidence for the model, never authority: the wake's grant was
admitted from the original request before this runs, and nothing here changes it.
Entry point: ``continuation_context(repository, assignment)``.
Dependencies: project_dispatch (rows, attempts, phases), coordinator_progress
(board head), router.proactive.questions (open owner questions).
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
_UNOWNED_HEADING = 'Open outcomes with no running or queued Worker naming them (unowned; assign an owner now):'
_DROPPED_HEADING = 'Open outcomes dropped from the board with no running or queued Worker naming them:'
_NO_WORKER = 'No Worker needs you now (get_project_workers lists every assignment).'
_UNREADABLE = ' could not be read now: '
_TASK_ITEM = re.compile(r'^- \[[a-z_]+\] (?P<title>.*?)(?: \(due [^)\n]*\))?(?:: (?P<progress>.*))?$')
_QUESTION_ITEM = re.compile(r'^- (?P<question>.*?)(?: \(your default meanwhile: (?P<default>.*)\))? \[Turn [^\]\n]*\]$')
_OPEN = ('pending', 'in_progress')
_PHASE_LINES = {
    'running': 'running',
    'needs_review': 'result to review (review_project_work)',
    'held': 'delivery held',
    'delivering': 'accepted, publishing',
    'needs_decision': 'a restart cut off a call of unknown effect: check it, then control_conversation',
    'interrupted': 'interrupted',
    'ended': 'ended',
}
_WORKER_ITEM = re.compile(r'^- [^:\n]+: (?:' + '|'.join(map(re.escape, _PHASE_LINES.values()))
                          + r') \u2014 (?P<title>.*)$')


def _board(repository, assignment):
    """The open tasks of this binding's board, or None when it is unreadable."""
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    row = CoordinatorTaskProgress(repository.owner, repository.project, client=repository.client,
                                  clock=repository.clock)._head()
    if row is None:
        return []
    scope = row['value']['scope']
    if (scope['coordinatorId'] != assignment['request']['coordinatorId']
            or scope['bindingRevision'] != assignment['grant']['bindingRevision']):
        return []
    return [task for task in row['value']['board']['tasks'] if task['status'] in _OPEN]


def _questions(repository, coordinator_id):
    from lib.router.proactive.questions import open_questions
    return open_questions(repository.owner, coordinator_id=coordinator_id, client=repository.client)


def _workers(repository, assignment):
    """``(assignment_id, phase, title)`` of the Worker states that need the Coordinator."""
    from lib.project_dispatch.continuation import _group, worker_phase, worker_summaries
    request = assignment['request']
    origin = request['delegation']['attemptId']
    value, _ = repository._load(repository._selection(sources=[origin]))
    summaries = worker_summaries(repository, _group(value, origin))
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
        found.append((summary['assignment_id'], phase, summary['title'].split('\n', 1)[0]))
    return found


def _owns(task, worker):
    """The Worker's title names this outcome; broad briefs and shared vocabulary do not count."""
    title = ' '.join(task['title'].casefold().split())
    worker_title = ' '.join((worker.get('title') or '').casefold().split())
    if not worker_title:
        return False
    # The board and Worker titles share their outcome label. Only that label is
    # compared: a Worker doing one stage never claims another named outcome.
    subject = re.split(r'\s*[:—-]\s*', title, maxsplit=1)[0].strip()
    worker_title = re.sub(r'^(?:own|shepherd|drive|run|finish|implement|verify|review|audit)\s+',
                          '', worker_title)
    worker_subject = re.split(r'\s*[:—-]\s*', worker_title, maxsplit=1)[0].strip()
    if len(subject) >= 4 and subject in worker_title:
        return True
    # A lead may keep the outcome label and change its next stage after a colon.
    return len(worker_subject) >= 4 and worker_subject in title


def _dropped_owned(task, workers, current_tasks):
    """Whether a running/queued Worker owns this dropped outcome itself.

    A Worker that owns only the board's renamed continuation of the outcome
    does not erase the drop: the lead still has to restore or explicitly cancel
    the dropped row. The Worker clears it by naming that outcome's own title.
    """
    for worker in workers:
        if not _owns(task, worker):
            continue
        # A different current board row this Worker owns is a rename, not the
        # dropped title; otherwise its title names the dropped outcome itself.
        renamed = [current for current in current_tasks
                   if current['title'] != task['title'] and _owns(current, worker)]
        if not renamed:
            return True
    return False


def unowned_outcomes(tasks, workers, dropped=()):
    """Open board outcomes no running/queued Worker names, plus dropped open outcomes."""
    return {'unowned': [task for task in tasks if not any(_owns(task, worker) for worker in workers)],
            'dropped': [task for task in dropped if not _dropped_owned(task, workers, tasks)]}


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


def _ownership(repository, assignment, tasks):
    """Unowned outcomes over the whole current binding, not only this request's cohort.

    The board alone is not the inventory of stated outcomes. Two durable sources
    that already exist feed the same projection: the owner request text of every
    Coordinator request of this binding, and its whole board history (including
    tasks it dropped). Ownership is still decided by the same existing running
    or queued Worker titles; no outcome is inferred and nothing is stored.
    """
    from lib.project_dispatch.continuation import worker_phase, worker_summaries
    from lib.project_dispatch.history import page
    from lib.conversations.coordinator_task_history import CoordinatorTaskHistory, fold_boards
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    progress = CoordinatorTaskProgress(repository.owner, repository.project, client=repository.client,
                                       clock=repository.clock)
    scope, binding, head = progress.current()
    head_keys = (head or {}).get('taskKeys') or []
    current_keys = {key for task, key in zip((head or {}).get('board', {}).get('tasks', []), head_keys)
                    if key and task['status'] in _OPEN}
    rows = []
    requests = []
    cursor = None
    while True:
        current = page(repository, cursor)
        for row in current['rows']:
            if row['request'].get('coordinatorId') != assignment['request']['coordinatorId']:
                continue
            if row['request'].get('profile', {}).get('mode') == 'coordinator':
                requests.append(row)
            else:
                rows.append(row)
        cursor = current['olderCursor']
        if cursor is None:
            break
    workers = []
    for summary in worker_summaries(repository, rows):
        if worker_phase(summary) != 'running':
            continue
        metadata = repository.conversations.get(summary['conversation_id'], user_id=repository.owner,
                                                include_messages=False)
        metadata = getattr(metadata, 'metadata', metadata) or {}
        workers.append({'title': metadata.get('title') or summary['title']})
    history = CoordinatorTaskHistory(repository.owner, repository.project, client=repository.client,
                                     clock=repository.clock)
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
                dropped.append({'key': key, 'title': record['title'], 'status': record['status']})
    recorded_keys = set()
    if recorded and scope is not None:
        for group in ('in_progress', 'pending', 'completed', 'cancelled'):
            recorded_keys.update(key for task in _history_pages(history, group) for key in _history_keys(task))
    stated = _stated_outcomes(repository, requests, recorded_keys)
    merged = {task['key']: task for task in dropped}
    merged.update({task['key']: task for task in stated})
    return unowned_outcomes(tasks, workers, list(merged.values()))


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
    origins = {row['request']['delegation']['attemptId'] for row in requests}
    outcomes = {}
    for row in requests:
        execution = row.get('execution') or {}
        turn = repository.turn_get(row['request']['recipientId'], execution.get('turnId'),
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
        if not boards and origin not in settlements and not origins - {origin}:
            text = _source_request(repository, row['request']['authorizationSource'])
            if text:
                outcomes.setdefault('request:' + row['id'],
                                    {'key': 'request:' + row['id'], 'title': text, 'status': 'in_progress'})
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


def continuation_context(repository, assignment) -> str:
    """The appended context of one Coordinator wake input (``HEADER`` and its sections)."""
    request = assignment['request']
    lines = []
    tasks = _read('Your task board', lambda: _board(repository, assignment), lines)
    ownership = (_read('Outcome ownership', lambda: _ownership(repository, assignment, tasks), lines)
                 if tasks is not None else None)
    if tasks:
        lines.append(_BOARD_HEADING)
        for task in tasks:
            line = '- [' + task['status'] + '] ' + task['title']
            if task.get('due'):
                line += ' (due ' + task['due'] + ')'
            if task.get('progress'):
                line += ': ' + task['progress']
            lines.append(line)
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
        if ownership['unowned']:
            lines.append(_UNOWNED_HEADING)
            lines.extend('- [unowned] ' + task['title'] for task in ownership['unowned'])
        if ownership['dropped']:
            lines.append(_DROPPED_HEADING)
            lines.extend('- [dropped] ' + task['title'] + ' — restore it to the board and assign an owner, or explicitly cancel it'
                         for task in ownership['dropped'])
    workers = _read('Worker states', lambda: _workers(repository, assignment), lines)
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
        if (line in (_BOARD_HEADING, _QUESTIONS_HEADING, _WORKERS_HEADING, _NO_WORKER,
                     _UNOWNED_HEADING, _DROPPED_HEADING)
                or line.startswith(_EMPTY_BOARD) or (line.startswith('(') and _UNREADABLE in line)):
            continue
        worker = _WORKER_ITEM.match(line)
        question = None if worker else _QUESTION_ITEM.match(line)
        task = None if worker or question else _TASK_ITEM.match(line)
        if worker:
            subject.append(worker['title'])
        elif question:
            subject.extend(part for part in (question['question'], question['default']) if part)
        elif task:
            subject.extend(part for part in (task['title'], task['progress']) if part)
        elif line.strip():
            subject.append(line)
    return '\n'.join(subject)
