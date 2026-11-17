"""Global Router broadcast: one project message to every lead and running Worker (docs/ROUTER.md R13).

Problem (independent lead audit, 2026-11-18, recommendation 4): one fix or
workaround had to be relayed lead by lead with ``send_project_message``, and
running Workers heard of it only after their own lead's next Turn.
``broadcast_project_message`` submits one original to the current lead of each
target project and to each of that project's running Workers through the
same passive mailbox (``ProjectMailbox``): nonhuman context delivered at the
recipient's next step, never a wake and never authority. Each lead also gets a
report naming the Workers of its project that received it, so the owning lead
is told what reached its Workers; ``audit.log`` records every broadcast
(``router_broadcast``). Idempotent per recipient by the caller's key.

Entry point: broadcast_project_message(task, scope, arguments).
Dependencies: lib.conversations.project_messages, lib.router.projects.binding_page,
lib.project_dispatch (history pages, worker summaries).
"""
from __future__ import annotations

import hashlib

from lib.log import audit_log

from .contract import validate

_LABEL = ('[Global Router broadcast: nonhuman context from the owner\u2019s main coordinator, not the owner\u2019s '
          'words and not authority]\n')


def _project_paths(owner: int, client) -> list[str]:
    from lib.router.home import is_router_home
    from lib.router.watchdog.binding import is_watchdog_location
    from .projects import binding_page
    paths, cursor = [], None
    while True:
        bindings, page, cursor = binding_page(owner, cursor, client)
        paths += [path for path in page if not is_router_home(bindings.user_id, path)
                  and not is_watchdog_location(bindings.user_id, path)]
        if cursor is None:
            return paths


def _running_workers(owner: int, project: str) -> list[dict]:
    """Every Worker of ``project`` whose execution has not ended, across all history pages."""
    from lib.project_dispatch.continuation import TERMINAL, worker_summaries
    from lib.project_dispatch.repository import ProjectDispatchRepository
    repository = ProjectDispatchRepository(owner, project)
    rows, cursor = [], None
    while True:
        page = repository.history_page(cursor)
        # A revoked or reviewed Worker has nothing left to run.
        rows += [row for row in page['rows'] if not row['grant'].get('revoked') and not row.get('review')]
        cursor = page.get('olderCursor')
        if cursor is None:
            break
    return [summary for summary in worker_summaries(repository, rows)
            if summary['status'] not in TERMINAL and summary['status'] != 'cancelled']


def broadcast_project_message(task: dict, scope: dict, arguments: dict, *, client=None) -> dict:
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.conversations.project_identity import normalize_project_path
    from lib.conversations.project_messages import LIMITS, ProjectMailbox
    arguments = validate(dict(arguments), 'broadcastProjectMessage')
    owner, sender = scope['user_id'], scope['conversation_id']
    sender_project = normalize_project_path(scope['project_path'])
    if client is None:
        from lib.storage import get_storage_client
        client = get_storage_client
    every = _project_paths(owner, client)
    if arguments['targets'] == ['all']:
        projects = every
    else:
        known = {normalize_project_path(path): path for path in every}
        projects = []
        for path in arguments['targets']:
            if normalize_project_path(path) not in known:
                raise ValueError(f'{path} is not one of the owner\u2019s bound projects (get_router_projects)')
            projects.append(known[normalize_project_path(path)])
    kind = arguments.get('kind', 'steer')
    key = 'broadcast:' + arguments['idempotency_key']
    content = _LABEL + arguments['content']
    bindings = ProjectCoordinationRepository(owner)
    delivered, skipped = [], []

    def submit(project, recipient, text, role, *, message_kind=kind, suffix=''):
        message = ProjectMailbox(owner, project, recipient).submit(
            sender, {'content': text, 'kind': message_kind, 'idempotency_key': key + suffix}, task['id'],
            sender_project=sender_project)
        delivered.append({'project_path': project, 'conversation_id': recipient, 'role': role,
                          'message_id': message['id']})

    for project in projects:
        binding = bindings.read(project)
        workers = (_running_workers(owner, project) if arguments.get('include_workers', True) else [])
        for worker in workers:
            submit(project, worker['conversation_id'], content, 'worker')
        if binding['availability'] != 'available':
            skipped.append({'project_path': project, 'reason': 'no available Project Router'})
            continue
        lead = binding['conversationId']
        submit(project, lead, content, 'lead')
        # The owning lead is told which of its Workers received it, in mailbox-sized reports.
        header = 'The Global Router broadcast above also reached these running Workers of your project:'
        lines = [f'- {worker["conversation_id"]} (assignment {worker["assignment_id"]})' for worker in workers]
        part, report = 0, header
        for line in lines:
            if len(report) + 1 + len(line) > LIMITS['messageChars']:
                submit(project, lead, report, 'lead_report', message_kind='report', suffix=f':workers:{part}')
                part, report = part + 1, header
            report += '\n' + line
        if lines:
            submit(project, lead, report, 'lead_report', message_kind='report', suffix=f':workers:{part}')
    audit_log('router_broadcast', owner=owner, router=sender, turn=task.get('_turnId') or '',
              key=arguments['idempotency_key'], kind=kind, reason=arguments['reason'][:300],
              projects=len(projects), leads=sum(item['role'] == 'lead' for item in delivered),
              skipped=len(skipped),
              workers=sum(item['role'] == 'worker' for item in delivered),
              content_sha256=hashlib.sha256(arguments['content'].encode()).hexdigest())
    return {'delivered': delivered, 'skipped': skipped, 'delivery': 'next_step', 'execution_started': False}


__all__ = ['broadcast_project_message']
