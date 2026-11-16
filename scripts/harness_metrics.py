#!/usr/bin/env python3
"""Agent-harness metrics: recompute the docs/METRICS.md scorecard, read-only.

Responsibility: the seven numbers that say how close the agent harness is to
running without its owner (definitions, targets and the first baseline live in
docs/METRICS.md). Evidence is read only through the running application's
loopback GET API: conversation Turns and their current attempts, Worker task
rows, project-dispatch assignments and lifecycle approvals. Nothing is written,
no storage handle is attached and no provider is called. A daily run is an
ordinary scheduler ``command`` task; this script has no setting of its own.

Entry: python3 scripts/harness_metrics.py [--days N] [--api-url URL] [--json PATH]
                                          [--evidence PATH | --from-evidence PATH ...]
The window is N complete local days ending at today's midnight. ``--evidence``
saves the slim evidence the numbers were computed from; ``--from-evidence``
recomputes from one or more such files (seven daily ``--days 1`` files merge
into one week) without touching the API.

Exit: 0 report printed (coverage gaps are listed in it); 1 API or I/O failure;
2 invalid usage.
Dependencies: standard library and ``tofu_dotenv`` (PORT discovery, exactly as
debug/inspect_conversation.py). Contract of the API reads: the v4 conversation
sync protocol (contracts/conversation_sync_v3.yaml) and the project-dispatch
history page (contracts/project_dispatch_v1.schema.json).
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import http.client
import ipaddress
import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 15000
# One GET may wait for a large coordinator history page on a loaded host; this
# is a liveness deadline per request, not a sizing knob.
REQUEST_DEADLINE_SECONDS = 300
# Page sizes are the API's own maxima (routes/conversations.py _MAX_LIST_LIMIT,
# routes/conversation_sync_v3.py _MAX_TURN_HISTORY_PAGE, routes/api_v1/tasks.py).
CONVERSATION_PAGE = 1000
TURN_HISTORY_PAGE = 256
TASK_PAGE = 100
TAIL_WINDOW = 'tail-96'

ROLE_GLOBAL_ROUTER = 'global_router'
ROLE_PROJECT_ROUTER = 'project_router'
ROLE_WATCHDOG = 'watchdog'
ROLE_WORKER = 'worker'
ROUTER_ROLES = (ROLE_GLOBAL_ROUTER, ROLE_PROJECT_ROUTER, ROLE_WATCHDOG)
HARNESS_ROLES = ROUTER_ROLES + (ROLE_WORKER,)
# Turns a model executes; coordinator_report.* Turns are ingested, not generated.
MODEL_TURN_KINDS = frozenset({'reply', 'scheduled_reply', 'channel_reply'})
# Browser turn actions: `${operation}:${turnId}:${projectionRevision}`
# (frontend/src/runtime/sections/main/conversation_turn_store.js). Routers use
# `session-control:` and restart recovery `restart-resume:`, so neither matches.
OWNER_ACTION_COMMAND = re.compile(
    r'^(continue|regenerate|checkpoint_resume|answer_guidance):[0-9a-f-]{36}:\d+$')
TOOLS_WITH_ARGUMENTS = frozenset({
    'delegate_project_work', 'report_to_user', 'watchdog_resolve_signals', 'ask_human'})
# A Worker birth runs after delegate_project_work returned `starting`; a refused
# birth is reported only by the next Worker status read, in the instruction
# lib/conversations/coordinator_workers.py writes for failed_to_start rows.
WORKER_STATUS_TOOLS = frozenset({'poll', 'await_project_workers', 'get_project_workers'})
FAILED_BIRTHS = re.compile(r'Delegations ((?:[0-9a-f-]{36}(?:, )?)+) could not start a Worker')
ASSIGNMENT_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
SIGNAL_NOTICE = re.compile(
    r'Signal: ([0-9a-f]{32}) \(source=([a-z_]+), opened (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) UTC\)')
SIGNAL_RECORD = re.compile(
    r'"resolvedAt":\s*(\d{13}),\s*"signalId":\s*"([0-9a-f]{32})",\s*"source":\s*"([a-z_]+)"')
SIGNAL_CREATED = re.compile(r'"createdAt":\s*(\d{13})')
OPEN_SIGNAL_COUNT = re.compile(r'open signals=(\d+)')

# Owner-message classes (docs/METRICS.md, M1). Cues are matched in the message's
# first sentence, where an owner states a reaction; English cues match whole
# words, and "不X" cues skip the "X不X" yes-no question form. The first
# matching class wins, in INTERVENTION_CLASSES order.
RECOVER_CUES = (
    '挂了', '挂掉', '卡住', '卡了', '卡死', '停摆', '停了', '停工', '没反应', '不动了', '崩了', '崩溃',
    '宕机', '压垮', '恢复', '修复', '修一下', '重启', '报错', '出错', '失败', '断了', '掉线', '没回复',
    '怎么还没', '还没好', '继续', 'continue', 'retry', 'stuck', 'crash', 'crashed', 'broken',
    'not responding', 'restart', 'recover')
# Consent and delegation cues are unambiguous, so they count anywhere in the message.
APPROVE_CUES = ('你决定', '你定', '全权', '无所谓', '都可以', '就这样', '我已经(?!说)', '已经被我')
CORRECT_CUES = (
    '(?<!对)不对', '(?<!是)不是', '错了', '搞错', '(?<!要)不要', '别再', '别用', '别做', '别管',
    '你别', '(?<!行)不行', '(?<!用)不用', '不需要', '不必', '没必要', '重新', '重做', '改成', '改为',
    '改一下', '换成', '为什么', '怎么又', '(?<!够)不够', '太少', '太多', '跑偏', '没有按', '没按',
    '没看到', '没有看到', '更详细', '认真', '其实', '明明', '放弃', '算了', '你不能', '^那你',
    'wrong', 'instead', "don't", 'do not', 'should', 'why', 'redo')
# "Repeat" also covers chasing: the owner pushing work that was already asked for.
REPEAT_CUES = (
    '我说过', '说过了', '再说一遍', '之前说', '刚才说', '我已经说', '重复说', '还是没', '还是不',
    '到底', '了没有', '完没有', '太慢', '快点', '加速', '抓紧', '全力推进', '未尽', 'again',
    'already told', 'as i said')
# A reply that is a consent word, or opens with one, answers a question; so does
# a reply of one or two letters or digits (an option such as "A" or "2").
APPROVE_WORDS = frozenset({
    '开', '关', '行', '好', '嗯', '对', '是', '删', '可', '可以', '同意', '批准', '确认', '好的', '好吧',
    '没问题', '删除', '删掉', '是的', '对的', '去做', '开始', '做吧', '开吧', '发吧', 'ok', 'okay', 'y',
    'yes', 'sure', 'lgtm', 'approve', 'agree'})
# Measurement definitions recorded in docs/METRICS.md: a reply of at most this
# many letters/digits is "short"; two owner messages at least this similar
# (difflib ratio of normalized text) are one instruction sent twice.
SHORT_REPLY_CHARS = 20
OPTION_REPLY_CHARS = 2
REPEAT_SIMILARITY = 0.8
INTERVENTION_CLASSES = ('recover', 'approve', 'correct', 'repeat')
SENTENCE_END = re.compile(r'[。！？!?\n]|\.\s')
CLAUSE_END = re.compile(r'[，,；;：:、。！？!?\n]')


class APIError(RuntimeError):
    """A bounded read failed; the message never contains a response body."""


class LoopbackReader:
    """GET-only JSON reads from one loopback application origin."""

    def __init__(self, origin: str):
        parsed = urlsplit(origin)
        try:
            loopback = ipaddress.ip_address(parsed.hostname or '').is_loopback
        except ValueError:
            loopback = False
        if parsed.scheme != 'http' or not loopback or parsed.path not in ('', '/') or parsed.query:
            raise APIError('--api-url must be an http loopback IP origin without a path')
        self.host, self.port = parsed.hostname, parsed.port or 80
        self.requests = 0
        self.bytes = 0

    def get(self, path: str, params: dict | None = None, *, accept=(200,)) -> tuple[int, dict]:
        target = path + ('?' + urlencode(params) if params else '')
        connection = http.client.HTTPConnection(self.host, self.port, timeout=REQUEST_DEADLINE_SECONDS)
        try:
            connection.request('GET', target, headers={'Accept': 'application/json',
                                                      'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            body = response.read()
        except (OSError, http.client.HTTPException) as error:
            raise APIError(f'GET {path} failed: {type(error).__name__}') from None
        finally:
            connection.close()
        self.requests += 1
        self.bytes += len(body)
        if response.status not in accept:
            raise APIError(f'GET {path} answered HTTP {response.status}')
        try:
            return response.status, json.loads(body)
        except ValueError:
            raise APIError(f'GET {path} answered non-JSON') from None


def api_origin(explicit: str | None) -> str:
    if explicit:
        return explicit.rstrip('/')
    from tofu_dotenv import read_dotenv_values
    environment = {**read_dotenv_values(REPO_ROOT / '.env'), **os.environ}
    raw = (environment.get('PORT') or '').strip()
    return f'http://127.0.0.1:{int(raw) if raw else DEFAULT_PORT}'


def _data(payload: dict):
    return payload.get('data', payload) if isinstance(payload, dict) else payload


# ── Collection ───────────────────────────────────────────────────────────────

def _list_conversations(reader: LoopbackReader) -> list[dict]:
    items, params = [], {'limit': CONVERSATION_PAGE}
    while True:
        page = _data(reader.get('/api/v4/conversations', params)[1])
        items += page['items']
        cursor = page.get('page') or {}
        if not cursor.get('hasMore'):
            return items
        params = {'limit': CONVERSATION_PAGE, 'before': cursor['nextBefore'],
                  'before_id': cursor['nextBeforeId']}


def _roles(reader: LoopbackReader, conversations: list[dict]) -> dict[str, str]:
    router = _data(reader.get('/api/v4/router')[1]) or {}
    watchdog = _data(reader.get('/api/v4/router/watchdog')[1]) or {}
    sources = {(c.get('settings') or {}).get('_coordinatorSource') for c in conversations}
    roles = {}
    for conversation in conversations:
        cid = conversation['id']
        if cid == router.get('conversationId'):
            roles[cid] = ROLE_GLOBAL_ROUTER
        elif cid == watchdog.get('conversationId'):
            roles[cid] = ROLE_WATCHDOG
        elif (conversation.get('settings') or {}).get('_coordinatorSource'):
            roles[cid] = ROLE_WORKER
        elif cid in sources or cid.startswith('coordinator-'):
            roles[cid] = ROLE_PROJECT_ROUTER
    return roles


def _text(projection: dict) -> str:
    original = projection.get('originalContent')
    if isinstance(original, str) and original.strip():
        return original
    parts = [segment.get('text') or '' for segment in projection.get('segments') or []
             if isinstance(segment, dict) and segment.get('type') == 'text']
    return '\n'.join(part for part in parts if part) or str(projection.get('content') or '')


def _json_object(text: str) -> dict | None:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _slim_tool(round_: dict) -> dict:
    evidence = round_.get('toolResultEvidence') or {}
    tool = {'name': round_.get('toolName') or '', 'attention': round_.get('attentionKind') or '',
            'status': round_.get('status') or '', 'exec': evidence.get('executionStatus') or '',
            'start': round_.get('tStart'), 'end': round_.get('tEnd')}
    content = round_.get('toolContent') or ''
    if tool['name'] in WORKER_STATUS_TOOLS:
        refused = sorted({identity for match in FAILED_BIRTHS.finditer(content)
                          for identity in ASSIGNMENT_ID.findall(match[1])})
        if refused:
            tool['result'] = {'failedToStart': refused}
    if tool['name'] not in TOOLS_WITH_ARGUMENTS:
        return tool
    arguments = _json_object(round_.get('toolArgs') or '') or {}
    if tool['name'] == 'delegate_project_work':
        tool['args'] = {'title': str(arguments.get('title') or '')[:160],
                        'key': str(arguments.get('idempotency_key') or '')[:160]}
        result = _json_object(content) or {}
        tool['result'] = {name: result[name] for name in (
            'assignment_id', 'conversation_id', 'status', 'dispatch', 'error') if name in result}
        if not result:
            tool['result'] = {'unparsed': content[:300]}
    elif tool['name'] == 'report_to_user':
        tool['args'] = {'kind': arguments.get('kind') or ''}
    elif tool['name'] == 'watchdog_resolve_signals':
        tool['args'] = {'signal_ids': [str(item) for item in arguments.get('signal_ids') or []]}
        tool['result'] = {'records': _resolved_records(content)}
    return tool


def _resolved_records(content: str) -> list[dict]:
    """Signal records a resolve call returned, also from a truncated summary."""
    result = _json_object(content)
    if result is not None and isinstance(result.get('resolved'), list):
        return [{'signalId': item.get('signalId'), 'source': item.get('source'),
                 'createdAt': item.get('createdAt'), 'resolvedAt': item.get('resolvedAt')}
                for item in result['resolved'] if isinstance(item, dict)]
    records, floor = [], 0
    for match in SIGNAL_RECORD.finditer(content):
        created = [m for m in SIGNAL_CREATED.finditer(content, floor, match.start())]
        records.append({'signalId': match[2], 'source': match[3], 'resolvedAt': int(match[1]),
                        'createdAt': int(created[-1][1]) if created else None})
        floor = match.end()
    return records


def _slim_turn(conversation_id: str, turn: dict) -> dict:
    projection = turn.get('projection') or {}
    origin = projection.get('origin') or {}
    settlement = turn.get('settlement') or {}
    error = settlement.get('error') if isinstance(settlement.get('error'), dict) else {}
    slim = {
        'conv': conversation_id, 'turn': turn.get('turnId'), 'ordinal': turn.get('ordinal'),
        'lane': turn.get('laneId') or 'main', 'kind': turn.get('kind') or '',
        'actor': turn.get('actor') or '', 'status': turn.get('status') or '',
        'cause': settlement.get('cause') or '', 'error': error.get('kind') or '',
        'created': turn.get('createdAt'), 'updated': turn.get('updatedAt'),
        'parent': turn.get('parentTurnId'), 'attempt': turn.get('currentAttemptId'),
        'presentation': turn.get('presentationId') or '',
        'initiator': origin.get('initiator'), 'forwarded': bool(origin.get('forwardedFrom')),
        'channel': (origin.get('channel') or {}).get('channel') if isinstance(origin.get('channel'), dict) else None,
    }
    if slim['actor'] == 'human':
        slim['text'] = _text(projection)
    cost = projection.get('cost')
    if isinstance(cost, dict) and isinstance(cost.get('costUsd'), (int, float)):
        slim['costUsd'] = float(cost['costUsd'])
    tools = [_slim_tool(item) for item in projection.get('toolRounds') or [] if isinstance(item, dict)]
    if tools:
        slim['tools'] = tools
    return slim


def _slim_attempt(attempt: dict) -> dict:
    return {'attempt': attempt.get('attemptId'), 'turn': attempt.get('turnId'),
            'operation': attempt.get('operation') or '', 'status': attempt.get('status') or '',
            'command': str(attempt.get('commandId') or '')[:160], 'created': attempt.get('createdAt'),
            'started': attempt.get('startedAt'), 'settled': attempt.get('settledAt')}


def _conversation_window(reader: LoopbackReader, conversation_id: str, start_ms: int, end_ms: int) -> dict:
    """Main-lane Turns created in ``[start_ms, end_ms)``, newest pages first."""
    base = '/api/v4/conversations/' + quote(conversation_id, safe='')
    snapshot = reader.get(base + '/sync', {'segmentPayload': 'refs', 'toolPayload': 'lazy',
                                           'turnWindow': TAIL_WINDOW})[1]
    turns, attempts = list(snapshot.get('turns') or []), list(snapshot.get('attempts') or [])
    window = snapshot.get('turnWindow') or {}
    more, before, sync = window.get('hasMore'), window.get('nextBeforeOrdinal'), snapshot.get('syncSeq')
    gap = None
    while more and turns and min(t.get('createdAt') or 0 for t in turns) >= start_ms:
        status, page = reader.get(base + '/turns/history', {
            'laneId': 'main', 'syncSeq': sync, 'beforeOrdinal': before, 'limit': TURN_HISTORY_PAGE,
            'segmentPayload': 'refs', 'toolPayload': 'lazy'}, accept=(200, 409))
        if status == 409:
            gap = 'history page stale; older Turns of this conversation were not read'
            break
        turns += page.get('turns') or []
        attempts += page.get('attempts') or []
        more, before = page.get('hasMore'), page.get('nextBeforeOrdinal')
    kept = [t for t in turns if start_ms <= (t.get('createdAt') or 0) < end_ms]
    ids = {t.get('turnId') for t in kept}
    return {'turns': [_slim_turn(conversation_id, t) for t in kept],
            'attempts': [_slim_attempt(a) for a in attempts if a.get('turnId') in ids],
            'gap': gap}


def _worker_tasks(reader: LoopbackReader, conversation_id: str) -> list[dict]:
    path = '/api/v4/tasks/by-conv/' + quote(conversation_id, safe='')
    rows, params = [], {'limit': TASK_PAGE}
    while True:
        page = _data(reader.get(path, params)[1])
        batch = page.get('tasks') or []
        rows += [{'task': row.get('taskId'), 'turn': row.get('turnId'), 'status': row.get('status'),
                  'created': row.get('createdAt'), 'completed': row.get('completedAt')} for row in batch]
        if not page.get('hasMore') or not batch:
            return rows
        params = {'limit': TASK_PAGE, 'before': min(row.get('createdAt') or 0 for row in batch)}


def _slim_assignment(row: dict) -> dict:
    request = row.get('request') or {}
    execution = row.get('execution') or {}
    delivery = row.get('delivery') or {}
    publication = row.get('publication') or {}
    review = row.get('review') or {}
    return {'assignment': row.get('assignmentId'), 'worker': request.get('recipientId'),
            'coordinator': request.get('coordinatorId'), 'project': request.get('projectPath'),
            'execution': execution.get('status') or '', 'executionTurn': execution.get('turnId'),
            'delivery': delivery.get('status') or '', 'review': review.get('decision') or '',
            'publishedAt': publication.get('requestedAt'), 'revoked': bool(row.get('revoked'))}


def _assignments(reader: LoopbackReader, project: str, created: dict[str, int], start_ms: int) -> list[dict]:
    """Assignment facts newest first, until a page holds only Workers born before the window."""
    rows, params = [], {'path': project}
    while True:
        page = _data(reader.get('/api/v4/project/coordination/assignments', params)[1])
        batch = [_slim_assignment(row) for row in page.get('assignments') or []]
        rows += batch
        cursor = page.get('olderCursor')
        if not cursor or all(created.get(row['worker'] or '', 0) < start_ms for row in batch):
            return rows
        params = {'path': project, 'cursor': cursor}


def collect(reader: LoopbackReader, start_ms: int, end_ms: int, log=print) -> dict:
    conversations = _list_conversations(reader)
    roles = _roles(reader, conversations)
    meta = {c['id']: {'role': roles[c['id']], 'created': c.get('createdAt'), 'updated': c.get('updatedAt'),
                      'title': str(c.get('title') or '')[:160],
                      'source': (c.get('settings') or {}).get('_coordinatorSource'),
                      'project': (c.get('settings') or {}).get('projectPath')}
            for c in conversations if c['id'] in roles}
    active = sorted((cid for cid, row in meta.items() if (row['updated'] or 0) >= start_ms),
                    key=lambda cid: (meta[cid]['role'] == ROLE_WORKER, cid))
    evidence = {'version': 1, 'generatedAt': int(time.time() * 1000), 'window': {'start': start_ms, 'end': end_ms},
                'conversations': meta, 'turns': [], 'attempts': [], 'tasks': {}, 'assignments': [],
                'approvals': [], 'gaps': []}
    for index, cid in enumerate(active, 1):
        try:
            window = _conversation_window(reader, cid, start_ms, end_ms)
            evidence['turns'] += window['turns']
            evidence['attempts'] += [{**row, 'conv': cid} for row in window['attempts']]
            if window['gap']:
                evidence['gaps'].append({'conv': cid, 'gap': window['gap']})
            if meta[cid]['role'] == ROLE_WORKER:
                evidence['tasks'][cid] = _worker_tasks(reader, cid)
        except (APIError, KeyError, TypeError, ValueError) as error:
            evidence['gaps'].append({'conv': cid, 'gap': f'not read: {error}'})
        if index % 50 == 0 or index == len(active):
            log(f'[harness-metrics] {index}/{len(active)} conversations, {reader.requests} requests, '
                f'{reader.bytes / 1e6:.0f} MB read', file=sys.stderr)
    created = {cid: row['created'] or 0 for cid, row in meta.items()}
    projects = sorted({row['project'] for row in meta.values()
                       if row['role'] in (ROLE_GLOBAL_ROUTER, ROLE_PROJECT_ROUTER) and row['project']})
    for project in projects:
        try:
            evidence['assignments'] += _assignments(reader, project, created, start_ms)
        except (APIError, KeyError, TypeError, ValueError) as error:
            evidence['gaps'].append({'project': project, 'gap': f'assignments not read: {error}'})
    approvals = _data(reader.get('/api/v4/update/lifecycle-approvals')[1]) or {}
    evidence['approvals'] = [{'action': row.get('action'), 'status': row.get('status'),
                              'requested': row.get('requested_at'), 'decided': row.get('decided_at'),
                              'decidedBy': row.get('decided_by')}
                             for row in approvals.get('records') or []]
    evidence['reads'] = {'requests': reader.requests, 'bytes': reader.bytes}
    return evidence


# ── Metrics ──────────────────────────────────────────────────────────────────

def local_day(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000).strftime('%Y-%m-%d')


def window_days(start_ms: int, end_ms: int) -> list[str]:
    """Local days of the half-open window ``[start_ms, end_ms)``."""
    day, last, days = dt.date.fromisoformat(local_day(start_ms)), dt.date.fromisoformat(local_day(end_ms - 1)), []
    while day <= last:
        days.append(day.isoformat())
        day += dt.timedelta(days=1)
    return days


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile (no interpolation): an observed value."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _normalized(text: str) -> str:
    """Letters and digits only, case-folded: the text two messages are compared on."""
    return re.sub(r'[\W_]+', '', text.casefold())


def _cue_pattern(cues) -> re.Pattern:
    parts = []
    for cue in cues:
        if cue.startswith(('(?', '^')) or cue.endswith(')'):
            parts.append(cue)
        elif cue.isascii():
            parts.append(r'\b' + re.escape(cue) + r'\b')
        else:
            parts.append(re.escape(cue))
    return re.compile('|'.join(parts), re.IGNORECASE)


RECOVER = _cue_pattern(RECOVER_CUES)
APPROVE = _cue_pattern(APPROVE_CUES)
CORRECT = _cue_pattern(CORRECT_CUES)
REPEAT = _cue_pattern(REPEAT_CUES)


def first_sentence(text: str) -> str:
    return next((piece.strip() for piece in SENTENCE_END.split(text.strip()) if piece.strip()), '')


_APPROVE_PREFIXES = tuple(sorted(word for word in APPROVE_WORDS if len(word) > 1))


def _is_consent(text: str) -> bool:
    letters = _normalized(text)
    opening = _normalized(next((part for part in CLAUSE_END.split(text) if part.strip()), ''))
    return (opening in APPROVE_WORDS or 0 < len(letters) <= OPTION_REPLY_CHARS and letters.isascii()
            or len(letters) <= SHORT_REPLY_CHARS and letters.startswith(_APPROVE_PREFIXES))


def classify_owner_message(text: str, *, after_failure: bool = False, after_stop: bool = False,
                           after_question: bool = False, earlier: list[str] = ()) -> str:
    """One of recover / approve / correct / repeat / new (docs/METRICS.md, M1).

    ``after_failure``: the reply to the owner's previous message here failed;
    ``after_stop``: the owner stopped the previous reply; ``after_question``:
    the last agent Turn here asked the owner something.
    """
    head, letters = first_sentence(text), _normalized(text)
    if after_failure or RECOVER.search(head):
        return 'recover'
    if _is_consent(text) or APPROVE.search(text) or after_question and len(letters) <= SHORT_REPLY_CHARS:
        return 'approve'
    if after_stop or CORRECT.search(head):
        return 'correct'
    if REPEAT.search(head) or letters and any(
            difflib.SequenceMatcher(None, letters, _normalized(prior)).ratio() >= REPEAT_SIMILARITY
            for prior in earlier):
        return 'repeat'
    return 'new'


def _asks_owner(turn: dict) -> bool:
    """An agent Turn that put a question or decision to the owner."""
    return (turn['kind'] in ('coordinator_report.decision', 'coordinator_report.blocked')
            or (turn.get('presentation') or '').startswith('router-question-relay')
            or any(tool['name'] == 'ask_human' for tool in turn.get('tools') or []))


def _is_owner_message(turn: dict, roles: dict) -> bool:
    """The owner's own words: typed in the browser (no initiator) or sent from a channel."""
    return (turn['actor'] == 'human' and turn['kind'] == 'input' and roles.get(turn['conv']) in HARNESS_ROLES
            and turn['initiator'] in ('human', None) and not turn['forwarded'])


def _owner_messages(evidence: dict) -> list[dict]:
    roles = {cid: row['role'] for cid, row in evidence['conversations'].items()}
    return sorted((t for t in evidence['turns'] if _is_owner_message(t, roles)), key=lambda t: t['created'])


def owner_interventions(evidence: dict, days: list[str]) -> dict:
    by_conv = defaultdict(list)
    for turn in evidence['turns']:
        by_conv[turn['conv']].append(turn)
    for turns in by_conv.values():
        turns.sort(key=lambda t: (t['created'] or 0, t['ordinal'] or 0))
    roles = {cid: row['role'] for cid, row in evidence['conversations'].items()}
    by_turn = {(t['conv'], t['turn']): t for t in evidence['turns']}
    rows, earlier = [], []
    for message in _owner_messages(evidence):
        before = [t for t in by_conv[message['conv']]
                  if t['actor'] == 'assistant' and (t['created'] or 0) < message['created']]
        model = [t for t in before if t['kind'] in MODEL_TURN_KINDS and t['status'] != 'running']
        previous = model[-1] if model else {}
        asked = by_turn.get((message['conv'], previous.get('parent')))
        label = classify_owner_message(
            message.get('text') or '',
            after_failure=previous.get('status') == 'failed' and bool(asked) and _is_owner_message(asked, roles),
            after_stop=previous.get('status') == 'interrupted' and previous.get('cause') == 'user_abort',
            after_question=bool(before) and _asks_owner(before[-1]), earlier=earlier)
        earlier.append(message.get('text') or '')
        rows.append({'day': local_day(message['created']), 'class': label, 'conv': message['conv'],
                     'turn': message['turn'], 'channel': message['channel']})
    start, end = evidence['window']['start'], evidence['window']['end']
    actions = [a for a in evidence['attempts'] if OWNER_ACTION_COMMAND.match(a['command'] or '')
               and start <= (a['created'] or 0) < end]
    for action in actions:
        label = 'approve' if action['operation'] == 'answer_guidance' else 'recover'
        rows.append({'day': local_day(action['created']), 'class': label, 'conv': action['conv'],
                     'turn': action['turn'], 'action': action['operation']})
    # A lifecycle decision the gate recorded from the owner's browser session.
    decisions = [a for a in evidence['approvals']
                 if a.get('decidedBy') == 'ui' and start <= (a.get('decided') or 0) * 1000 < end]
    for decision in decisions:
        rows.append({'day': local_day(decision['decided'] * 1000), 'class': 'approve', 'conv': None,
                     'turn': None, 'action': f"lifecycle_{decision['action']}"})
    per_day = {day: Counter() for day in days}
    for row in rows:
        per_day.setdefault(row['day'], Counter())[row['class']] += 1
    interventions = sum(1 for row in rows if row['class'] in INTERVENTION_CLASSES)
    return {'messages': sum(1 for row in rows if 'action' not in row),
            'uiActions': len(actions) + len(decisions), 'interventions': interventions,
            'perDay': {day: dict(per_day[day]) for day in days},
            'interventionsPerDay': {day: sum(per_day[day][c] for c in INTERVENTION_CLASSES) for day in days},
            'meanPerDay': interventions / len(days) if days else None,
            'byClass': dict(Counter(row['class'] for row in rows)), 'rows': rows}


def owner_needed_share(evidence: dict, days: list[str]) -> dict:
    roles = {cid: row['role'] for cid, row in evidence['conversations'].items()}
    total, needed = Counter(), Counter()
    kinds = Counter()
    for turn in evidence['turns']:
        if roles.get(turn['conv']) not in HARNESS_ROLES:
            continue
        day = local_day(turn['created'])
        for tool in turn.get('tools') or []:
            if tool['status'] == 'rejected':
                continue
            total[day] += 1
            kind = None
            if tool['name'] == 'ask_human':
                kind = 'ask_human'
            elif tool['attention'] == 'interactive':
                kind = 'approval_tool'
            elif tool['name'] == 'report_to_user' and (tool.get('args') or {}).get('kind') in ('decision', 'blocked'):
                kind = 'decision_report'
            if kind:
                needed[day] += 1
                kinds[kind] += 1
    start, end = evidence['window']['start'], evidence['window']['end']
    for approval in evidence['approvals']:
        requested = (approval.get('requested') or 0) * 1000
        if start <= requested <= end:
            needed[local_day(requested)] += 1
            total[local_day(requested)] += 1
            kinds['lifecycle_approval'] += 1
    actions, asks = sum(total.values()), sum(needed.values())
    return {'actions': actions, 'ownerNeeded': asks, 'share': _ratio(asks, actions), 'byKind': dict(kinds),
            'perDay': {day: {'actions': total[day], 'ownerNeeded': needed[day],
                             'share': _ratio(needed[day], total[day])} for day in days}}


def worker_starts(evidence: dict, days: list[str]) -> dict:
    """Delegations made in the window and whether their Worker ran (docs/METRICS.md, M3)."""
    roles = {cid: row['role'] for cid, row in evidence['conversations'].items()}
    start, end = evidence['window']['start'], evidence['window']['end']
    calls, rejected, refused_births = {}, 0, set()
    for turn in evidence['turns']:
        if roles.get(turn['conv']) not in ROUTER_ROLES:
            continue
        for tool in turn.get('tools') or []:
            refused_births.update((tool.get('result') or {}).get('failedToStart') or ())
            if tool['name'] != 'delegate_project_work' or not start <= (tool['start'] or 0) < end:
                continue
            if tool['status'] == 'rejected':
                rejected += 1
                continue
            result = tool.get('result') or {}
            key = result.get('assignment_id') or f"refused:{turn['turn']}:{tool['start']}"
            if key not in calls or (tool['start'] or 0) < (calls[key]['start'] or 0):
                calls[key] = {'start': tool['start'], 'result': result, 'exec': tool['exec'],
                              'router': turn['conv']}
    outcomes, latencies, per_day = Counter(), [], {day: Counter() for day in days}
    for key, call in calls.items():
        result, worker = call['result'], call['result'].get('conversation_id')
        tasks = evidence['tasks'].get(worker) or []
        first = min((row['created'] for row in tasks if row.get('created')), default=None)
        if first is not None and call['start'] and first < call['start'] - 1000:
            continue  # a replay or follow-up of a Worker born before this call
        if call['exec'] not in ('ok', '') or not worker or result.get('status') == 'failed_to_start':
            outcome = 'refused'
        elif first is not None:
            outcome = 'started'  # also after a refused birth that a replay of the call started
            if result.get('dispatch') != 'deferred':
                latencies.append((first - call['start']) / 1000)
        elif key in refused_births:
            outcome = 'refused'
        else:
            outcome = 'not_started'
        outcomes[outcome] += 1
        if call['start']:
            per_day.setdefault(local_day(call['start']), Counter())[outcome] += 1
    attempts = sum(outcomes.values())
    return {'attempts': attempts, 'outcomes': dict(outcomes), 'rejectedCalls': rejected,
            'successRate': _ratio(outcomes['started'], attempts),
            'latencySeconds': {'p50': percentile(latencies, 0.5), 'p95': percentile(latencies, 0.95),
                               'n': len(latencies)},
            'perDay': {day: dict(per_day[day]) for day in days}}


def mttr(evidence: dict, days: list[str]) -> dict:
    roles = {cid: row['role'] for cid, row in evidence['conversations'].items()}
    created, sources, resolved = {}, {}, {}
    backlog = None
    for turn in sorted(evidence['turns'], key=lambda t: t['created'] or 0):
        if roles.get(turn['conv']) != ROLE_WATCHDOG:
            continue
        text = turn.get('text') or ''
        for signal_id, source, opened in SIGNAL_NOTICE.findall(text):
            stamp = dt.datetime.strptime(opened, '%Y-%m-%d %H:%M:%S').replace(tzinfo=dt.timezone.utc)
            created.setdefault(signal_id, int(stamp.timestamp() * 1000))
            sources.setdefault(signal_id, source)
        counted = OPEN_SIGNAL_COUNT.findall(text)
        if counted:
            backlog = {'at': turn['created'], 'open': int(counted[-1])}
        for tool in turn.get('tools') or []:
            if tool['name'] != 'watchdog_resolve_signals' or tool['exec'] != 'ok':
                continue
            for record in (tool.get('result') or {}).get('records') or []:
                if record.get('createdAt'):
                    created.setdefault(record['signalId'], record['createdAt'])
                sources.setdefault(record['signalId'], record.get('source') or '')
                resolved.setdefault(record['signalId'], record.get('resolvedAt') or tool['end'])
            for signal_id in (tool.get('args') or {}).get('signal_ids') or []:
                resolved.setdefault(signal_id, tool['end'])
    start = evidence['window']['start']
    cohort = [sid for sid, ms in created.items() if ms >= start]
    repaired = [(resolved[sid] - created[sid]) / 60000 for sid in cohort
                if sid in resolved and resolved[sid] and resolved[sid] >= created[sid]]
    by_source = defaultdict(list)
    for sid in cohort:
        if sid in resolved:
            by_source[sources.get(sid) or 'unknown'].append((resolved[sid] - created[sid]) / 60000)
    return {'signalsOpened': len(cohort), 'resolved': len(repaired),
            'unresolvedSeen': len(cohort) - len(repaired),
            'resolvedWithoutOpenTime': sum(1 for sid in resolved if sid not in created),
            'mttrMinutes': {'mean': statistics.fmean(repaired) if repaired else None,
                            'p50': percentile(repaired, 0.5), 'p90': percentile(repaired, 0.9)},
            'bySource': {source: {'n': len(values), 'p50': percentile(values, 0.5)}
                         for source, values in sorted(by_source.items())},
            'openBacklog': backlog}


def throughput(evidence: dict, days: list[str]) -> dict:
    """Assignments whose result Turn settled in the window (docs/METRICS.md, M5)."""
    start, end = evidence['window']['start'], evidence['window']['end']
    turns = {(t['conv'], t['turn']): t for t in evidence['turns']}
    completed, delivered, unsuccessful = Counter(), Counter(), Counter()
    states = Counter()
    for row in {row['assignment']: row for row in evidence['assignments']}.values():
        turn = turns.get((row['worker'], row['executionTurn']))
        if not turn or turn['status'] == 'running' or not start <= (turn['updated'] or 0) < end:
            continue
        finished = turn['updated']
        if row['execution'] != 'completed' or turn['status'] != 'completed':
            unsuccessful[row['execution'] or turn['status']] += 1
            continue
        completed[local_day(finished)] += 1
        states[row['delivery'] or 'none'] += 1
        if row['delivery'] == 'delivered':
            published = int(row['publishedAt'] * 1000) if row['publishedAt'] else finished
            delivered[local_day(min(max(published, finished), end - 1))] += 1
    total_completed, total_delivered = sum(completed.values()), sum(delivered.values())
    return {'completed': total_completed, 'delivered': total_delivered,
            'completedPerDay': {day: completed[day] for day in days},
            'deliveredPerDay': {day: delivered[day] for day in days},
            'meanCompletedPerDay': total_completed / len(days) if days else None,
            'meanDeliveredPerDay': total_delivered / len(days) if days else None,
            'deliveryStateOfCompleted': dict(states), 'endedUnsuccessfully': dict(unsuccessful)}


def cost(evidence: dict, days: list[str], completed: int) -> dict:
    roles = {cid: row['role'] for cid, row in evidence['conversations'].items()}
    by_role, per_day, unknown = Counter(), Counter(), 0
    for turn in evidence['turns']:
        if turn['actor'] != 'assistant' or roles.get(turn['conv']) not in HARNESS_ROLES:
            continue
        if 'costUsd' not in turn:
            unknown += turn['kind'] in MODEL_TURN_KINDS
            continue
        by_role[roles[turn['conv']]] += turn['costUsd']
        per_day[local_day(turn['created'])] += turn['costUsd']
    total = sum(by_role.values())
    return {'totalUsd': total, 'byRoleUsd': {role: round(value, 2) for role, value in by_role.items()},
            'perDayUsd': {day: round(per_day[day], 2) for day in days},
            'perCompletedTaskUsd': _ratio(total, completed),
            'workerPerCompletedTaskUsd': _ratio(by_role[ROLE_WORKER], completed),
            'modelTurnsWithoutCost': unknown}


SETTLED = ('completed', 'failed', 'interrupted')


def router_outages(model_turns: list[dict], attempts: dict, end: int) -> list[tuple[int, int]]:
    """Merged intervals in which a Router served nothing after failing (docs/METRICS.md, M7).

    ``model_turns`` are one Router's settled model Turns in creation order. A
    Turn that did work (completed, or ran tool calls before it failed or was
    interrupted) ends an outage when its attempt starts; one that did not
    starts an outage, never earlier than the end of the last completed Turn. A
    Turn that did work and then failed starts an outage when it ends.
    """
    intervals, down_since, served_until = [], None, None
    for turn in model_turns:
        began = (attempts.get(turn['turn']) or {}).get('started') or turn['created']
        worked = turn['status'] == 'completed' or bool(turn.get('tools'))
        if worked and down_since is not None:
            intervals.append((down_since, max(began, down_since)))
            down_since = None
        if turn['status'] == 'completed':
            served_until = max(served_until or 0, turn['updated'] or began)
        elif worked:
            down_since = turn['updated'] or began
        elif down_since is None:
            down_since = max(turn['created'], served_until or 0)
    if down_since is not None:
        intervals.append((down_since, end))
    merged = []
    for low, high in sorted(intervals):
        if merged and low <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    return merged


def availability(evidence: dict, days: list[str]) -> dict:
    start, end = evidence['window']['start'], evidence['window']['end']
    attempts = defaultdict(dict)
    for attempt in evidence['attempts']:
        attempts[attempt['conv']][attempt['turn']] = attempt
    rows = {}
    for cid, row in evidence['conversations'].items():
        if row['role'] not in ROUTER_ROLES:
            continue
        model = sorted((t for t in evidence['turns'] if t['conv'] == cid and t['actor'] == 'assistant'
                        and t['kind'] in MODEL_TURN_KINDS and t['status'] in SETTLED),
                       key=lambda t: (t['created'] or 0, t['ordinal'] or 0))
        if not model:
            continue
        counts = Counter(t['status'] for t in model)
        outages = router_outages(model, attempts[cid], end)
        observed_from = max(start, row['created'] or start)
        span = max(1, end - observed_from)
        clipped = [(max(a, observed_from), min(b, end)) for a, b in outages if min(b, end) > max(a, observed_from)]
        down = sum(b - a for a, b in clipped)
        rows[cid] = {'role': row['role'], 'title': row['title'], 'turns': len(model),
                     'completed': counts['completed'], 'failed': counts['failed'],
                     'interrupted': counts['interrupted'], 'successRate': counts['completed'] / len(model),
                     'downMinutes': down / 60000, 'spanMinutes': span / 60000, 'availability': 1 - down / span,
                     'longestOutageMinutes': max((b - a for a, b in clipped), default=0) / 60000,
                     'outages': [[a, b] for a, b in clipped]}
    settled = sum(r['turns'] for r in rows.values())
    completed = sum(r['completed'] for r in rows.values())
    down = sum(r['downMinutes'] for r in rows.values())
    span = sum(r['spanMinutes'] for r in rows.values())
    return {'turns': settled, 'completed': completed, 'successRate': _ratio(completed, settled),
            'availability': 1 - down / span if span else None,
            'worstAvailability': min((r['availability'] for r in rows.values()), default=None),
            'routers': rows}


def compute(evidence: dict) -> dict:
    window = evidence['window']
    days = window_days(window['start'], window['end'])
    flow = throughput(evidence, days)
    return {
        'window': {**window, 'days': days, 'startLocal': dt.datetime.fromtimestamp(window['start'] / 1000).isoformat(),
                   'endLocal': dt.datetime.fromtimestamp(window['end'] / 1000).isoformat()},
        'ownerInterventions': owner_interventions(evidence, days),
        'ownerNeeded': owner_needed_share(evidence, days),
        'workerStarts': worker_starts(evidence, days),
        'mttr': mttr(evidence, days),
        'throughput': flow,
        'cost': cost(evidence, days, flow['completed']),
        'availability': availability(evidence, days),
        'coverage': {'conversations': sum(1 for row in evidence['conversations'].values()
                                          if (row['updated'] or 0) >= window['start']),
                     'turns': len(evidence['turns']), 'assignments': len(evidence['assignments']),
                     'gaps': evidence['gaps'], 'reads': evidence.get('reads')},
    }


# ── Report ───────────────────────────────────────────────────────────────────

def _fmt(value, kind='n') -> str:
    if value is None:
        return 'n/a'
    if kind == '%':
        return f'{100 * value:.1f}%'
    if kind == '$':
        return f'${value:,.2f}'
    if kind == 's':
        return f'{value:,.0f} s'
    if kind == 'min':
        return f'{value:,.0f} min'
    return f'{value:,.1f}' if isinstance(value, float) else f'{value:,}'


def _counts(mapping: dict) -> str:
    return ', '.join(f'{key} {value}' for key, value in sorted(mapping.items(), key=lambda item: -item[1])) or 'none'


def render(metrics: dict) -> str:
    owner, need, starts = metrics['ownerInterventions'], metrics['ownerNeeded'], metrics['workerStarts']
    repair, flow, spend = metrics['mttr'], metrics['throughput'], metrics['cost']
    avail = metrics['availability']
    window = metrics['window']
    sources = ', '.join(f"{source} {row['n']} (p50 {_fmt(row['p50'], 'min')})"
                        for source, row in repair['bySource'].items()) or 'none'
    lines = [f"Harness metrics {window['startLocal'][:16]} → {window['endLocal'][:16]} "
             f"({len(window['days'])} local days)", '',
             '| # | Metric | Baseline | Detail |', '|---|---|---|---|',
             f"| M1 | Owner interventions / day | {_fmt(owner['meanPerDay'])} | "
             f"{owner['interventions']} of {owner['messages'] + owner['uiActions']} owner touches "
             f"({owner['messages']} messages, {owner['uiActions']} UI actions): {_counts(owner['byClass'])} |",
             f"| M2 | Agent actions that needed the owner | {_fmt(need['share'], '%')} | "
             f"{need['ownerNeeded']:,} of {need['actions']:,} actions "
             f"({_fmt(need['ownerNeeded'] / len(window['days']))} / day): {_counts(need['byKind'])} |",
             f"| M3 | Worker-start success | {_fmt(starts['successRate'], '%')} | "
             f"{_counts(starts['outcomes'])} of {starts['attempts']} delegations; "
             f"{starts['rejectedCalls']} malformed calls never ran |",
             f"| M3 | Worker-start latency p50 / p95 | {_fmt(starts['latencySeconds']['p50'], 's')} / "
             f"{_fmt(starts['latencySeconds']['p95'], 's')} | delegate call → first Worker attempt, "
             f"n={starts['latencySeconds']['n']} |",
             f"| M4 | MTTR, Watchdog signal → resolved | {_fmt(repair['mttrMinutes']['p50'], 'min')} median, "
             f"{_fmt(repair['mttrMinutes']['mean'], 'min')} mean | {repair['resolved']} resolved of "
             f"{repair['signalsOpened']} opened; by source {sources} |",
             f"| M5 | Assignments completed / delivered per day | {_fmt(flow['meanCompletedPerDay'])} / "
             f"{_fmt(flow['meanDeliveredPerDay'])} | {flow['completed']} completed ({_counts(flow['deliveryStateOfCompleted'])}); "
             f"ended unsuccessfully: {_counts(flow['endedUnsuccessfully'])} |",
             f"| M6 | Cost per completed task | {_fmt(spend['perCompletedTaskUsd'], '$')} | "
             f"total {_fmt(spend['totalUsd'], '$')} ("
             + ', '.join(f'{role} {_fmt(value, "$")}' for role, value in sorted(spend['byRoleUsd'].items())) + ') |',
             f"| M7 | Coordinator turn success | {_fmt(avail['successRate'], '%')} | "
             f"{avail['completed']} of {avail['turns']} settled model turns |",
             f"| M7 | Coordinator availability (all / worst Router) | {_fmt(avail['availability'], '%')} / "
             f"{_fmt(avail['worstAvailability'], '%')} | per Router below |", '']
    lines += ['| Day | Interventions | Owner-needed share | Starts ok/all | Completed | Delivered | Cost |',
              '|---|---|---|---|---|---|---|']
    for day in window['days']:
        day_starts = starts['perDay'].get(day) or {}
        lines.append(f"| {day} | {owner['interventionsPerDay'].get(day, 0)} | "
                     f"{_fmt(need['perDay'][day]['share'], '%')} | {day_starts.get('started', 0)}/"
                     f"{sum(day_starts.values())} | {flow['completedPerDay'][day]} | {flow['deliveredPerDay'][day]} | "
                     f"{_fmt(spend['perDayUsd'][day], '$')} |")
    lines += ['', '| Router | Model turns | Success | Down | Longest outage | Availability |', '|---|---|---|---|---|---|']
    for cid, row in sorted(avail['routers'].items(), key=lambda item: item[1]['availability']):
        lines.append(f"| {row['role']} {cid} | {row['turns']} | {_fmt(row['successRate'], '%')} | "
                     f"{_fmt(row['downMinutes'], 'min')} | {_fmt(row['longestOutageMinutes'], 'min')} | "
                     f"{_fmt(row['availability'], '%')} |")
    gaps = metrics['coverage']['gaps']
    lines += ['', f"Coverage: {metrics['coverage']['conversations']} active harness conversations, "
                  f"{metrics['coverage']['turns']:,} Turns, {metrics['coverage']['assignments']} assignments; "
                  f"{len(gaps)} gaps" + ''.join(f"\n- {gap}" for gap in gaps)]
    return '\n'.join(lines)


def write_private(path: Path, text: str) -> None:
    """Write an output readable by its owner only: evidence holds the owner's messages."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text)


def merge_evidence(documents: list[dict]) -> dict:
    """One evidence document from several runs (for example seven daily ones).

    The window spans the earliest start to the latest end; a run read later
    replaces an earlier run's row of the same identity, so a Turn or assignment
    read again after it settled carries its final state. Uncovered time between
    the runs' windows is reported as a gap.
    """
    ordered = sorted(documents, key=lambda document: document['generatedAt'])
    merged = {'version': 1, 'generatedAt': ordered[-1]['generatedAt'],
              'window': {'start': min(d['window']['start'] for d in ordered),
                         'end': max(d['window']['end'] for d in ordered)},
              'conversations': {}, 'tasks': {}, 'gaps': [],
              'reads': {'requests': 0, 'bytes': 0}}
    turns, attempts, assignments, approvals = {}, {}, {}, {}
    for document in ordered:
        merged['conversations'].update(document['conversations'])
        turns.update({(row['conv'], row['turn']): row for row in document['turns']})
        attempts.update({(row['conv'], row['attempt']): row for row in document['attempts']})
        assignments.update({row['assignment']: row for row in document['assignments']})
        approvals.update({(row['action'], row['requested']): row for row in document['approvals']})
        for cid, rows in document['tasks'].items():
            known = {row['task']: row for row in merged['tasks'].get(cid, [])}
            known.update({row['task']: row for row in rows})
            merged['tasks'][cid] = list(known.values())
        merged['gaps'] += document['gaps']
        for name in ('requests', 'bytes'):
            merged['reads'][name] += (document.get('reads') or {}).get(name, 0)
    covered = merged['window']['start']
    for low, high in sorted((d['window']['start'], d['window']['end']) for d in ordered):
        if low > covered:
            merged['gaps'].append({'gap': f'no evidence for {local_day(covered)} .. {local_day(low - 1)}'})
        covered = max(covered, high)
    merged.update(turns=list(turns.values()), attempts=list(attempts.values()),
                  assignments=list(assignments.values()), approvals=list(approvals.values()))
    return merged


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--days', type=int, default=7,
                        help='complete local days ending at today\'s midnight (default 7)')
    parser.add_argument('--api-url', help='loopback application origin (default: PORT from .env, else 15000)')
    parser.add_argument('--json', type=Path, help='also write the full metrics document here')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--evidence', type=Path, help='also save the slim evidence read from the API')
    source.add_argument('--from-evidence', type=Path, nargs='+',
                        help='recompute from saved evidence (several files are merged); no API reads')
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error('--days must be at least 1')
    try:
        if args.from_evidence:
            evidence = merge_evidence([json.loads(path.read_text(encoding='utf-8'))
                                       for path in args.from_evidence])
        else:
            today = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            end_ms = int(today.timestamp() * 1000)
            start_ms = int((today - dt.timedelta(days=args.days)).timestamp() * 1000)
            evidence = collect(LoopbackReader(api_origin(args.api_url)), start_ms, end_ms)
            if args.evidence:
                write_private(args.evidence, json.dumps(evidence, ensure_ascii=False))
        metrics = compute(evidence)
        if args.json:
            write_private(args.json, json.dumps(metrics, ensure_ascii=False, indent=1))
    except (APIError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    print(render(metrics))
    return 0


if __name__ == '__main__':
    sys.exit(main())
