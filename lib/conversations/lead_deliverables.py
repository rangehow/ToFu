"""Lead deliverables: a paged, read-time projection of what a lead conversation delivered.

A lead (the Global Router, a Project Router or Coordinator) delivers in ways
scattered through a long conversation. This module stores nothing: each read
projects the deliverables from the owners that already hold them
(docs/PROJECT_COORDINATION.md, "Lead deliverables"):

- reports of every kind: the lead's main-lane canonical ``coordinator_report.*``
  Turns (``coordinator_reports.py``), each with the files its ``report_to_user``
  call attached and the board task it named (both named by that call's receipt in
  the publishing reply); for the Global Router also the reports relayed to it,
  named by the relay notices in its Turns (``lib/router/relay.py``, which also
  carry the attached files and task) and read from the Project Router's canonical
  report Turn;
- published files, decks (.pptx/.pptd files), rich responses and long-form
  reports: the conversation's artifact rows (``lib/artifacts``), linked to the
  Turn whose tool round carries their ``artifactId``. A file a report attached
  is listed under that report, never again on its own.

Items are pointers: opening one reads the artifact or the report Turn from its
own owner. Newest first; the cursor names the last item of the previous page
by (createdAt, item digest), so a page boundary is stable while new items
arrive. A page can be filtered to one task of the lead's own project, or to
every other item, and always summarises the items per task: the task board
expands each task to its reports and files from these reads. ``notify`` sends
the project push hint the board refreshes on; it is a hint, never a record.

Entry points: DeliverablesPage, notify. Dependencies: turn_lifecycle (Turns),
lib.artifacts.core (artifact rows), lib.router.relay (notice format), the
project push channel. Owner identity is an explicit parameter.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from lib.identity import require_user_id
from lib.log import get_logger

logger = get_logger(__name__)

_CONTRACT = json.loads((Path(__file__).resolve().parents[2]
                        / 'contracts/project_coordination_v1.schema.json').read_text())
_DEFS = _CONTRACT['$defs']
PAGE_SIZE = _DEFS['deliverablePage']['properties']['items']['maxItems']
_TITLE_MAX = _DEFS['deliverableItem']['properties']['title']['maxLength']
_SUMMARY_MAX = _DEFS['deliverableItem']['properties']['summary']['maxLength']
_CURSOR = re.compile(r'^(\d{1,16}):([0-9a-f]{16})$')
_TASK_KEY = re.compile(r'^[0-9a-f]{32}$')
# The deliverables of no task of the lead's own project (the `task` filter's other bucket).
OTHER_TASKS = 'other'
# Artifact producers whose publication is a deliverable; workspace writes,
# inline images and scanner snippets are not.
DELIVERABLE_SOURCES = frozenset({'response_file', 'rich_response', 'longform-report'})
_DECK_SUFFIXES = ('.pptx', '.pptd')
REPORT_TURN_PREFIX = 'coordinator_report.'
REPORT_KINDS = frozenset(_DEFS['deliverableItem']['properties']['reportKind']['enum'])
_REPORT_TOOL = 'report_to_user'


def _digest(item_id):
    return hashlib.sha256(item_id.encode()).hexdigest()[:16]


def _clip(text, limit):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '\u2026'


def _clip_body(text):
    text = str(text or '')
    return text if len(text) <= _SUMMARY_MAX else text[:_SUMMARY_MAX - 1] + '\u2026'


def notify(owner, conversation_id, project_path):
    """Hint that ``conversation_id`` has a new deliverable; never raises to the producer."""
    if not project_path:
        return
    try:
        from lib.agent_core.push import push_event
        from lib.conversations.project_identity import normalize_project_path, project_channel_key
        push_event('project', project_channel_key(project_path),
                   {'projectKey': normalize_project_path(project_path), 'kind': 'lead_deliverable',
                    'conversationId': conversation_id}, user_id=owner)
    except Exception:
        logger.debug('lead deliverable push unavailable', exc_info=True)


def _source(conversation_id, turn):
    source = {'conversationId': conversation_id, 'turnId': turn['turnId']}
    if type(turn.get('ordinal')) is int:
        source['ordinal'] = turn['ordinal']
    return source


def report_kind(turn):
    """The report kind of a canonical report Turn, else ''."""
    kind = str(turn.get('kind') or '')
    value = kind[len(REPORT_TURN_PREFIX):] if kind.startswith(REPORT_TURN_PREFIX) else ''
    return value if value in REPORT_KINDS and turn.get('actor') == 'assistant' else ''


def report_item(turn, *, project_path, files=(), task=None):
    """The item of one canonical report Turn (content is ``title\\n\\nmessage``)."""
    title, _, message = str((turn.get('projection') or {}).get('content') or '').partition('\n\n')
    item = {'id': 'report:' + turn['turnId'], 'kind': 'report', 'reportKind': report_kind(turn),
            'title': _clip(title or turn['turnId'], _TITLE_MAX), 'projectPath': project_path or '',
            'createdAt': int(turn.get('createdAt') or 0),
            'source': _source(turn['conversationId'], turn), 'summary': _clip_body(message)}
    if files:
        item['files'] = list(files)
    if task:
        item['task'] = dict(task)
    # The copy the conversation shows by default: the Turn's auto-translation, same layout.
    translated_title, _, translated_message = str(
        (turn.get('projection') or {}).get('translatedContent') or '').partition('\n\n')
    if translated_title.strip():
        item['translation'] = {'title': _clip(translated_title, _TITLE_MAX), 'summary': _clip_body(translated_message)}
    return item


def _receipt(value):
    """A ``report_to_user`` receipt (JSON text or object), else None."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _file_entries(receipt):
    files = receipt.get('files') if receipt else None
    return [{key: entry[key] for key in ('artifactId', 'filename', 'kind', 'sizeBytes', 'path') if key in entry}
            for entry in files if isinstance(entry, dict) and isinstance(entry.get('artifactId'), str)
            and isinstance(entry.get('filename'), str) and entry.get('kind') in ('file', 'deck', 'document')
            ] if isinstance(files, list) else []


def _task_ref(receipt):
    task = receipt.get('task') if receipt else None
    if (isinstance(task, dict) and isinstance(task.get('key'), str) and _TASK_KEY.match(task['key'])
            and isinstance(task.get('title'), str) and task['title'].strip()):
        return {'key': task['key'], 'title': task['title']}
    return None


def report_receipts(projection):
    """``{report turnId: {'files': [reportFile], 'task': deliverableTaskRef | None}}`` of the
    ``report_to_user`` calls one reply made (segment result, else the round's stored receipt)."""
    found = {}

    def take(receipt):
        if receipt and isinstance(receipt.get('turnId'), str) and receipt['turnId'] not in found:
            found[receipt['turnId']] = {'files': _file_entries(receipt), 'task': _task_ref(receipt)}
    for segment in (projection or {}).get('segments') or []:
        if isinstance(segment, dict) and segment.get('type') == 'tool_use' and segment.get('name') == _REPORT_TOOL:
            result = segment.get('result')
            take(_receipt(result.get('content') if isinstance(result, dict) else result))
    for round_ in (projection or {}).get('toolRounds') or []:
        if isinstance(round_, dict) and round_.get('toolName') == _REPORT_TOOL:
            take(_receipt(round_.get('toolContent')))
    return found


def artifact_item(artifact, *, project_path='', source=None):
    """The item of one published artifact row, or None when its source is no deliverable."""
    if artifact.get('source') not in DELIVERABLE_SOURCES:
        return None
    meta = artifact.get('meta') or {}
    info = meta.get('responseFile') if isinstance(meta.get('responseFile'), dict) else {}
    name = info.get('filename') or artifact.get('title') or ''
    if artifact.get('source') == 'response_file':
        kind = 'deck' if str(name).lower().endswith(_DECK_SUFFIXES) else 'file'
    else:
        kind = 'document'
    return {'id': 'artifact:' + artifact['id'], 'kind': kind,
            'title': _clip(artifact.get('title') or name or artifact['id'], _TITLE_MAX),
            'projectPath': project_path or '', 'createdAt': int(artifact.get('created_at') or 0),
            'source': source, 'artifactId': artifact['id'], 'format': str(artifact.get('format') or '')[:64]}


def _scan(value, artifact_ids, texts):
    """Collect every ``artifactId`` and every string of one Turn row."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key == 'artifactId' and isinstance(child, str):
                artifact_ids.append(child)
            else:
                _scan(child, artifact_ids, texts)
    elif isinstance(value, list):
        for child in value:
            _scan(child, artifact_ids, texts)
    elif isinstance(value, str):
        texts.append(value)


def own_task_key(item, project_path):
    """The task key of ``item`` when it names a task of the lead's own project, else None."""
    task = item.get('task')
    if not task:
        return None
    from lib.conversations.project_identity import normalize_project_path
    own = normalize_project_path(project_path) if project_path else ''
    theirs = normalize_project_path(item['projectPath']) if item.get('projectPath') else ''
    return task['key'] if own == theirs else None


class DeliverablesPage:
    """One owner's read of one lead conversation's deliverables, newest first."""

    def __init__(self, owner, conversation_id, *, owned=None, turns=None, artifacts=None, get_turn=None):
        self.owner = require_user_id(owner, context='lead deliverables')
        if not isinstance(conversation_id, str) or not 0 < len(conversation_id) <= 256:
            raise ValueError('conversationId is required')
        self.conversation_id = conversation_id
        self.owned = owned
        self.turns = turns
        self.artifacts = artifacts
        self.get_turn = get_turn

    def _ports(self):
        owned, turns, artifacts, get_turn = self.owned, self.turns, self.artifacts, self.get_turn
        if owned is None:
            from lib.response_assets import owned_conversation as owned
        if turns is None:
            from lib.turn_lifecycle import lane_turns_newest_first

            def turns(conversation_id):
                # A long lead conversation exceeds a whole-conversation read's response bound.
                return (row for row, _attempt in lane_turns_newest_first(conversation_id, user_id=self.owner))
        if artifacts is None:
            from lib.artifacts.core import list_artifacts as artifacts
        if get_turn is None:
            from lib.turn_lifecycle import get_turn as read_turn

            def get_turn(conversation_id, turn_id):
                return read_turn(conversation_id, turn_id, user_id=self.owner)
        return owned, turns, artifacts, get_turn

    def _relayed(self, texts, get_turn, seen):
        from lib.router.relay import notice_references
        for text in texts:
            for reference in notice_references(text):
                if reference['turnId'] in seen:
                    continue
                try:
                    turn = get_turn(reference['coordinatorId'], reference['turnId'])
                except (LookupError, PermissionError):
                    turn = None  # A deleted or foreign report is no deliverable.
                if (turn and report_kind(turn) and turn.get('kind') == REPORT_TURN_PREFIX + reference['kind']
                        and turn.get('conversationId') == reference['coordinatorId']):
                    seen.add(reference['turnId'])
                    yield report_item(turn, project_path=reference['projectPath'], files=reference['files'],
                                      task=reference['task'])

    def project(self, project_path=''):
        """Every deliverable of the conversation, newest first."""
        owned, turns, artifacts, get_turn = self._ports()
        owned(self.conversation_id, self.owner)
        items, turn_of_artifact, reports, receipts = [], {}, set(), {}
        for turn in turns(self.conversation_id):
            if report_kind(turn):
                reports.add(turn['turnId'])
                items.append(report_item({**turn, 'conversationId': self.conversation_id},
                                         project_path=project_path))
                continue
            for report_id, receipt in report_receipts(turn.get('projection')).items():
                receipts.setdefault(report_id, receipt)
            artifact_ids, texts = [], []
            _scan(turn.get('projection'), artifact_ids, texts)
            for artifact_id in artifact_ids:
                turn_of_artifact[artifact_id] = turn  # Read newest first: the oldest mention publishes it.
            items.extend(self._relayed(texts, get_turn, reports))
        attached = set()
        for item in items:
            # A report Turn is published while its reply runs, so the receipt naming it is read later.
            receipt = receipts.get(item['source']['turnId'] if item['source'] else '') or {}
            files = item.get('files') or receipt.get('files')
            if files:
                item['files'] = files
                attached.update(entry['artifactId'] for entry in files)
            if 'task' not in item and receipt.get('task'):
                item['task'] = receipt['task']
        for row in artifacts(self.conversation_id):
            if row.get('id') in attached:
                continue
            turn = turn_of_artifact.get(row.get('id'))
            item = artifact_item(row, project_path=project_path,
                                 source=_source(self.conversation_id, turn) if turn else None)
            if item is not None:
                items.append(item)
        items.sort(key=lambda item: (-item['createdAt'], _digest(item['id'])))
        return items

    def read(self, cursor=None, *, project_path='', task=None):
        """One page newest first; ``task`` filters to one task key or ``OTHER_TASKS``.

        ``tasks``/``otherCount`` summarise every item whatever the filter. An item
        belongs to a task only when that task is of the lead's own project
        (``project_path``); a report relayed from another project is in ``other``.
        """
        if task is not None and task != OTHER_TASKS and not (isinstance(task, str) and _TASK_KEY.match(task)):
            raise ValueError('Invalid deliverables task filter')
        after = None
        if cursor is not None:
            match = _CURSOR.match(cursor) if isinstance(cursor, str) else None
            if match is None:
                raise ValueError('Invalid deliverables cursor')
            after = (-int(match.group(1)), match.group(2))
        items = self.project(project_path)
        summary, other = {}, 0
        for item in items:
            key = own_task_key(item, project_path)
            if key is None:
                other += 1
                continue
            entry = summary.get(key)
            if entry is None:
                # Newest first: the first item names the task's latest title and time.
                summary[key] = {'key': key, 'title': item['task']['title'], 'count': 1, 'latestAt': item['createdAt']}
            else:
                entry['count'] += 1
        if task is not None:
            items = [item for item in items if (own_task_key(item, project_path) or OTHER_TASKS) == task]
        if after is not None:
            items = [item for item in items if (-item['createdAt'], _digest(item['id'])) > after]
        page = items[:PAGE_SIZE]
        last = page[-1] if page else None
        return {'conversationId': self.conversation_id, 'items': page,
                'nextCursor': f'{last["createdAt"]}:{_digest(last["id"])}' if len(items) > PAGE_SIZE else None,
                'tasks': list(summary.values()), 'otherCount': other}
