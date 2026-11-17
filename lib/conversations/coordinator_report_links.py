"""Report links: durable links of already published Coordinator reports to board tasks.

A report names its tasks through its ``report_to_user`` call (``tasks``), and the
call's receipt is the record. Reports sent before tasks could be named have no
such receipt, and a published report is never edited, so a link record
(``reportLink`` in project_coordination_v1.schema.json) attaches such a report to
a task afterwards. Links are keyed by owner, project, Coordinator, report Turn and
task, so linking twice is idempotent and one report can belong to several tasks.
The lead deliverables projection reads them beside the receipts
(docs/PROJECT_COORDINATION.md, "Automatic work reports and report links").

Task titles resolve leniently (``resolve_tasks``, shared with ``report_to_user``):
naming tasks never refuses a report. ``link_reports`` is the owner's batch entry
(``POST /api/v4/project/coordination/report-links``): it links only canonical
report Turns of the lead's own conversation.

Entry points: ``resolve_tasks``, ``link_report``, ``link_reports``, ``report_links``.
Dependencies: the storage record namespace ``reporting.linkNamespace``, task
history (titles), turn_lifecycle (report Turns). Owner identity is an explicit parameter.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from lib.conversations.coordinator_reports import CONTRACT, POLICY, validate_report
from lib.identity import require_user_id
from lib.log import get_logger
from lib.storage.errors import StorageError

logger = get_logger(__name__)

NAMESPACE = POLICY['linkNamespace']
# record.list's protocol maximum page.
_RECORD_LIST_LIMIT = 1000


def _client(client):
    if client is not None:
        return client
    from lib.storage import get_storage_client
    return get_storage_client


def _prefix(owner, project_path, coordinator_id):
    digest = hashlib.sha256(json.dumps([project_path, coordinator_id], ensure_ascii=False).encode()).hexdigest()
    return f'{owner}:{digest}:'


def _norm_title(title: str) -> str:
    return ' '.join(title.split()).casefold()


def resolve_tasks(owner, project_path, coordinator_id, titles, *, history=None):
    """``(deliverableTaskRef list, titles that matched no task)``; never raises.

    A title matches a task of the board or its history by its current or a
    former title, then ignoring case and spacing; the current board wins a tie
    (owner decision 2026-11-17: an unlinked report is better than a refused one).
    """
    if titles is None:
        return [], []
    if isinstance(titles, str):
        titles = [titles]
    wanted = list(dict.fromkeys(title.strip() for title in titles if isinstance(title, str) and title.strip()))
    if not wanted:
        return [], []
    try:
        if history is None:
            from lib.conversations.coordinator_task_history import CoordinatorTaskHistory
            history = CoordinatorTaskHistory(owner, project_path)
        known = history.tasks(coordinator_id)
    except Exception:
        logger.warning('Report tasks could not be resolved; the report stays unlinked', exc_info=True)
        return [], wanted
    known = sorted(known, key=lambda entry: (not entry.get('onBoard'), -entry.get('changedAt', 0)))
    found, missing = {}, []
    for title in wanted:
        match = next((entry for entry in known if title == entry['title']
                      or title in entry.get('formerTitles', ())), None)
        if match is None:
            match = next((entry for entry in known if _norm_title(title) in
                          {_norm_title(text) for text in (entry['title'], *entry.get('formerTitles', ()))}), None)
        if match is None:
            missing.append(title)
        else:
            found.setdefault(match['key'], {'key': match['key'], 'title': match['title']})
    return list(found.values()), missing


def link_report(owner, project_path, coordinator_id, report_turn_id, task, *, client=None, clock=time.time):
    """Link one report Turn to one task (``deliverableTaskRef``); idempotent, returns the link."""
    owner = require_user_id(owner, context='report link')
    value = {'owner': owner, 'projectPath': project_path, 'coordinatorId': coordinator_id,
             'reportTurnId': report_turn_id, 'task': {'key': task['key'], 'title': task['title']},
             'linkedAt': int(clock() * 1000)}
    validate_report(value, 'reportLink')
    key = _prefix(owner, project_path, coordinator_id) + report_turn_id + ':' + task['key']
    store = _client(client)
    existing = store().query('record.get', {'namespace': NAMESPACE, 'key': key})
    if existing is not None:
        return existing['value']
    try:
        store(write=True).command('record.put', {
            'namespace': NAMESPACE, 'key': key, 'value': value, 'expected_version': 0,
        }, 'coordinator-report-link:' + uuid.uuid4().hex)
    except StorageError as error:
        if error.code != 'database_conflict':
            raise
    return value


def link_reports(owner, project_path, coordinator_id, links, *, get_turn=None, history=None, client=None):
    """Link earlier reports to tasks by title: ``links`` is ``[{turnId, tasks: [title]}]``.

    Returns ``{'linked': [{turnId, tasks}], 'unlinked': [{turnId, tasks|reason}]}``.
    Only a canonical report Turn of ``coordinator_id`` the owner can read is linked.
    """
    owner = require_user_id(owner, context='report link')
    if not isinstance(links, list) or not links:
        raise ValueError('links must be a non-empty list of {turnId, tasks}')
    if get_turn is None:
        from lib.response_assets import owned_conversation
        owned_conversation(coordinator_id, owner)  # Another owner's lead is refused, as on every read.
        from lib.turn_lifecycle import get_turn as read_turn

        def get_turn(conversation_id, turn_id):
            return read_turn(conversation_id, turn_id, user_id=owner)
    from lib.conversations.lead_deliverables import report_kind
    if history is None:
        from lib.conversations.coordinator_task_history import CoordinatorTaskHistory
        history = CoordinatorTaskHistory(owner, project_path)
    linked, unlinked = [], []
    for entry in links:
        turn_id = entry.get('turnId') if isinstance(entry, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError('each link needs a turnId')
        try:
            turn = get_turn(coordinator_id, turn_id)
        except (LookupError, PermissionError):
            turn = None
        if not turn or not report_kind(turn):
            unlinked.append({'turnId': turn_id, 'reason': 'not a report of this lead'})
            continue
        tasks, missing = resolve_tasks(owner, project_path, coordinator_id, entry.get('tasks'), history=history)
        for task in tasks:
            link_report(owner, project_path, coordinator_id, turn_id, task, client=client)
        if tasks:
            linked.append({'turnId': turn_id, 'tasks': tasks})
        if missing:
            unlinked.append({'turnId': turn_id, 'tasks': missing})
    return {'linked': linked, 'unlinked': unlinked}


def report_links(owner, project_path, coordinator_id, *, client=None):
    """``{report turnId: [deliverableTaskRef]}`` of the Coordinator's linked reports, in link order."""
    owner = require_user_id(owner, context='report link')
    prefix = _prefix(owner, project_path, coordinator_id)
    store = _client(client)
    found, after = {}, ''
    while True:
        payload = {'namespace': NAMESPACE, 'prefix': prefix, 'limit': _RECORD_LIST_LIMIT}
        if after:
            payload['after_key'] = after
        rows = store().query('record.list', payload) or []
        for row in sorted(rows, key=lambda row: (row['value'].get('linkedAt', 0), row['key'])):
            value = row['value']
            if (not isinstance(value, dict) or value.get('owner') != owner
                    or value.get('coordinatorId') != coordinator_id or value.get('projectPath') != project_path):
                raise StorageError('database_integrity', 'Invalid Coordinator report link')
            found.setdefault(value['reportTurnId'], []).append(dict(value['task']))
        if len(rows) < _RECORD_LIST_LIMIT:
            return found
        after = rows[-1]['key']


__all__ = ['CONTRACT', 'NAMESPACE', 'link_report', 'link_reports', 'report_links', 'resolve_tasks']
