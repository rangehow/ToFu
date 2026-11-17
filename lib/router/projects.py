"""get_router_projects and list_project_leads: the owner's leads at a glance (docs/ROUTER.md).

Responsibility: page the owner's project coordination bindings (the ordinary
``project.coordination.binding.v1`` records, keyed by owner). ``router_projects``
projects each into passive facts: availability, active Worker and request
counts from the project's newest dispatch history page, and the latest report
pointer; the router home is not a project and is omitted. ``project_leads``
is the directory every lead reads to message another project's lead (R13):
the Global Router and each Project Router with its one-line board summary,
and on the first page the owner's resource registry. Nothing here starts,
resumes or wakes work. Pages follow the project dispatch history page bound,
the get_project_workers convention; cursors are opaque.

Entry points: router_projects(owner, arguments, *, client=None); router_projects_wire(page),
the page in its /api/v4/router/projects wire form (routerProjectsPage);
project_leads(owner, caller, arguments, *, client=None).
Dependencies: project_coordination, project_dispatch (history, continuation
worker summaries), coordinator_reports, coordinator_progress,
project_messages (lead rule, contract), router.home.
"""
from __future__ import annotations

from collections.abc import Mapping
import os

from lib.log import get_logger
from lib.storage.errors import StorageError

from .contract import validate

logger = get_logger(__name__)


def _activity(owner: int, project: str, binding: dict) -> dict:
    from lib.project_dispatch.continuation import TERMINAL, worker_summaries
    from lib.project_dispatch.repository import ProjectDispatchRepository
    repository = ProjectDispatchRepository(owner, project)
    page = repository.history_page(None)
    rows = [row for row in page['rows']
            if row['request']['coordinatorId'] == binding['conversationId']
            and row['grant'].get('bindingRevision') == binding['revision']
            and row['request'].get('profile', {}).get('mode') != 'coordinator']
    active = [row for row, summary in zip(rows, worker_summaries(repository, rows))
              if summary['status'] not in TERMINAL and summary['status'] != 'cancelled']
    return {'active_workers': len(active),
            'active_requests': len({row['request']['authorizationSource']['turnId'] for row in active}),
            'workers_coverage': 'complete' if page.get('olderCursor') is None else 'newest_page'}


def _last_report(owner: int, project: str) -> dict | None:
    from lib.conversations.coordinator_reports import CoordinatorReports
    state = CoordinatorReports(owner, project).read()
    latest = state.get('latest')
    if not latest:
        return None
    return {'turn_id': latest['turnId'], 'kind': latest['kind'], 'title': latest['title'],
            'created_at': latest['createdAt'], 'unread': state['unread']}


def _binding_page(owner: int, cursor: str | None, client):
    """``(repository, project paths, next cursor)`` of one page of the owner's bindings."""
    from lib.conversations.project_coordination import NAMESPACE, ProjectCoordinationRepository
    from lib.project_dispatch.history import PAGE_ROWS
    bindings = ProjectCoordinationRepository(owner, client=client)
    prefix = f'{bindings.user_id}:'
    rows = client().query('record.list', {'namespace': NAMESPACE, 'prefix': prefix,
                                          'after_key': prefix + cursor if cursor else '',
                                          'limit': PAGE_ROWS + 1})
    if not isinstance(rows, list) or len(rows) > PAGE_ROWS + 1:
        raise StorageError('database_integrity', 'Invalid coordination binding page')
    paths = []
    for row in rows[:PAGE_ROWS]:
        key = row.get('key') if isinstance(row, Mapping) else None
        value = row.get('value') if isinstance(row, Mapping) else None
        if (not isinstance(key, str) or not key.startswith(prefix) or not isinstance(value, Mapping)
                or value.get('user_id') != bindings.user_id or not isinstance(value.get('projectPath'), str)):
            raise StorageError('database_integrity', 'Coordination binding page escaped its owner')
        paths.append(value['projectPath'])
    next_cursor = rows[PAGE_ROWS - 1]['key'][len(prefix):] if len(rows) > PAGE_ROWS else None
    return bindings, paths, next_cursor


def router_projects(owner: int, arguments: dict, *, client=None) -> dict:
    """One page of bound projects with passive activity facts."""
    from lib.router.home import is_router_home
    from lib.router.watchdog.binding import is_watchdog_location
    arguments = validate(dict(arguments), 'getRouterProjects')
    if client is None:
        from lib.storage import get_storage_client
        client = get_storage_client
    bindings, paths, next_cursor = _binding_page(owner, arguments.get('after_id'), client)
    projects = []
    for project in paths:
        if is_router_home(bindings.user_id, project) or is_watchdog_location(bindings.user_id, project):
            continue
        binding = bindings.read(project)
        entry = {'project_path': project, 'coordinator_conversation_id': binding['conversationId'],
                 'binding_revision': binding['revision'], 'availability': binding['availability']}
        if binding['availability'] == 'available':
            try:
                entry.update(_activity(bindings.user_id, project, binding))
                entry['last_report'] = _last_report(bindings.user_id, project)
                entry.update(_proactive_view(bindings.user_id, project, binding, client=client))
            except (StorageError, ValueError, PermissionError) as error:
                # One damaged project must not hide the others; the gap is explicit.
                entry['activity_error'] = str(error)
        projects.append(entry)
    return {'projects': projects, 'next_cursor': next_cursor}


def _board_summary(owner: int, project: str, bindings, client) -> str:
    """The lead's own one-line task-board summary for its current binding, or ''."""
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    try:
        state = CoordinatorTaskProgress(owner, project, client=client, bindings=bindings).read()
    except (StorageError, ValueError):
        # A damaged board must not hide the lead; the board view reports the damage.
        logger.warning('[Router] unreadable task board project=%s', project, exc_info=True)
        return ''
    return state['board']['summary'] if state['board'] else ''


def project_leads(owner: int, caller: str, arguments: dict, *, client=None) -> dict:
    """One page of the owner's other current leads; the first page adds the resource registry (R13).

    ``caller`` must itself lead a project: the Watchdog, Workers and ordinary
    conversations have no cross-project correspondents.
    """
    from jsonschema import Draft202012Validator
    from lib.conversations.project_messages import CONTRACT, NOT_A_LEAD_SENDER, lead_role
    from lib.router.binding import ROUTER_TITLE
    from lib.router.home import is_router_home, owner_resources
    from lib.router.watchdog.binding import is_watchdog_location
    error = next(Draft202012Validator(CONTRACT['$defs']['leadsRequest']).iter_errors(arguments), None)
    if error is not None:
        raise ValueError(f'Invalid list_project_leads arguments: {error.message}')
    if lead_role(owner, caller) is None:
        raise PermissionError(NOT_A_LEAD_SENDER)
    if client is None:
        from lib.storage import get_storage_client
        client = get_storage_client
    bindings, paths, next_cursor = _binding_page(owner, arguments.get('after_id'), client)
    leads = []
    for project in paths:
        if is_watchdog_location(bindings.user_id, project):
            continue
        binding = bindings.read(project)
        if binding['availability'] != 'available' or binding['conversationId'] == caller:
            continue
        home = is_router_home(bindings.user_id, project)
        leads.append({'conversation_id': binding['conversationId'],
                      'role': 'global_router' if home else 'project_router',
                      'name': ROUTER_TITLE if home else os.path.basename(project.rstrip('/')) or project,
                      'project_path': project,
                      'purpose': _board_summary(bindings.user_id, project, bindings, client)})
    page = {'leads': leads, 'next_cursor': next_cursor}
    if not arguments.get('after_id'):
        page['resources'] = owner_resources(bindings.user_id,
                                            content=bool(arguments.get('include_resources')))
    return page


def _proactive_view(owner: int, project: str, binding: dict, *, client=None) -> dict:
    """Board summary, open/overdue commitments and open owner questions (R10).

    Deterministic reads from the board head and the question index; no model
    call. The digest assembles from the same reads.
    """
    from lib.conversations.coordinator_progress import CoordinatorTaskProgress
    from lib.router.proactive.questions import open_questions
    import time
    view = {}
    progress = CoordinatorTaskProgress(owner, project, client=client)
    row = progress._head()
    if row is not None:
        head = row['value']
        board = head['board']
        view['board_summary'] = board['summary']
        view['board_revision'] = board['revision']
        open_commitments = [r for r in head.get('commitments', []) if r['state'] == 'open']
        now_ms = int(time.time() * 1000)
        view['open_commitments'] = len(open_commitments)
        overdue = [r for r in open_commitments if r['dueAt'] < now_ms and 'wake' in r]
        view['overdue_commitments'] = len(overdue)
        if overdue:
            view['overdue_titles'] = [r['title'] for r in overdue]
    questions = open_questions(owner, coordinator_id=binding['conversationId'], client=client)
    if questions:
        view['open_questions'] = len(questions)
        view['oldest_question_age_hours'] = int(
            (time.time() * 1000 - min(q['createdAt'] for q in questions)) / 3_600_000)
        view['blocking_questions'] = sum(1 for q in questions if q.get('blocking'))
    return view


_WIRE_NAMES = {
    'project_path': 'projectPath', 'coordinator_conversation_id': 'coordinatorConversationId',
    'binding_revision': 'bindingRevision', 'availability': 'availability',
    'active_workers': 'activeWorkers', 'active_requests': 'activeRequests',
    'workers_coverage': 'workersCoverage', 'last_report': 'lastReport',
    'board_summary': 'boardSummary', 'board_revision': 'boardRevision',
    'open_commitments': 'openCommitments', 'overdue_commitments': 'overdueCommitments',
    'overdue_titles': 'overdueTitles', 'open_questions': 'openQuestions',
    'blocking_questions': 'blockingQuestions', 'oldest_question_age_hours': 'oldestQuestionAgeHours',
    'activity_error': 'activityError',
}
_REPORT_WIRE_NAMES = {'turn_id': 'turnId', 'kind': 'kind', 'title': 'title',
                      'created_at': 'createdAt', 'unread': 'unread'}


def router_projects_wire(page: dict) -> dict:
    """One router_projects page renamed to its camelCase wire form (routerProjectsPage).

    Pure renaming: every fact the tool produced keeps its value and no absent
    fact gets a default. A fact without a wire name raises KeyError instead of
    reaching the browser undeclared.
    """
    projects = []
    for entry in page['projects']:
        wire = {_WIRE_NAMES[key]: value for key, value in entry.items()}
        if wire.get('lastReport') is not None:
            wire['lastReport'] = {_REPORT_WIRE_NAMES[key]: value for key, value in wire['lastReport'].items()}
        projects.append(wire)
    return {'projects': projects, 'nextCursor': page['next_cursor']}


__all__ = ['project_leads', 'router_projects', 'router_projects_wire']
