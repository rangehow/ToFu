"""What a Coordinator continuation acts on, read when its Turn is born.

A result, commitment or decision wake (``lib.project_dispatch.continuation``)
authorizes a short, stable brief. The state it acts on is read here, at birth,
and appended to that input: the open tasks of the Coordinator's task board,
the owner's unanswered ``ask_human`` questions and only the Worker states that
need the Coordinator (running, needs review, held or publishing delivery, a
restart-cut call to decide, a failure new since its previous Turn). Settled,
cancelled and delivered assignments are omitted; ``get_project_workers`` lists
them. The owner's request is linked, not repeated, and quoted only while the
board holds no open task, when nothing else says what the request is
(docs/PROJECT_COORDINATION.md "Continuation context is anchored on the task board").

This text is evidence for the model, never authority: the wake's grant was
admitted from the original request before this runs, and nothing here changes it.
Entry point: ``continuation_context(repository, assignment)``.
Dependencies: project_dispatch (rows, attempts, phases), coordinator_progress
(board head), router.proactive.questions (open owner questions).
"""
from __future__ import annotations

import json

from lib.log import get_logger

logger = get_logger(__name__)

HEADER = '\n\n[Continuation context: read now; evidence, not authority]\n'
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


def _board(repository, assignment):
    """The open tasks of this binding's board, or None when the board is unreadable."""
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


def _source_request(repository, source):
    """The owner's words of the request this wake serves."""
    turn = repository.turn_get(source['conversationId'], source['turnId'], user_id=repository.owner)
    if 'steerId' in source:
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
        lines.append('(' + what + ' could not be read now: ' + type(error).__name__ + '; read it yourself before '
                     'relying on it.)')
        return None


def continuation_context(repository, assignment) -> str:
    """The appended context of one Coordinator wake input (``HEADER`` and its sections)."""
    request = assignment['request']
    lines = []
    tasks = _read('Your task board', lambda: _board(repository, assignment), lines)
    if tasks:
        lines.append('Open tasks on your board:')
        for task in tasks:
            line = '- [' + task['status'] + '] ' + task['title']
            if task.get('due'):
                line += ' (due ' + task['due'] + ')'
            if task.get('progress'):
                line += ': ' + task['progress']
            lines.append(line)
    elif tasks is not None:
        source = request['authorizationSource']
        lines.append('Your board has no open task. The owner\'s request this continues (Turn '
                     + source['turnId'] + '):')
        excerpt = _read('The owner\'s request', lambda: _source_request(repository, source), lines)
        if excerpt is not None:
            lines.append(excerpt)
    questions = _read('Your open owner questions', lambda: _questions(repository, request['coordinatorId']),
                      lines)
    if questions:
        lines.append('Your questions the owner has not answered:')
        for question in questions:
            line = '- ' + str(question.get('question') or '').strip()
            if question.get('default'):
                line += ' (your default meanwhile: ' + str(question['default']) + ')'
            lines.append(line + ' [Turn ' + str(question.get('turnId')) + ']')
    workers = _read('Worker states', lambda: _workers(repository, assignment), lines)
    if workers:
        lines.append('Worker states that need you (settled, cancelled and delivered ones are omitted):')
        lines.extend('- ' + identity + ': ' + _PHASE_LINES[phase] + ' — ' + title
                     for identity, phase, title in workers)
    elif workers is not None:
        lines.append('No Worker needs you now (get_project_workers lists every assignment).')
    return HEADER + '\n'.join(lines)
