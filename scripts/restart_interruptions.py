#!/usr/bin/env python3
"""List what a production restart would interrupt right now (read-only).

Usage:
    python3 scripts/restart_interruptions.py [--api-url http://127.0.0.1:PORT]
        [--window-minutes 30] [--json]

Responsibility: one operator snapshot, taken just before a restart, of the work
that restart would stop. It reads the running application's loopback v4 API with
GET requests only (the loopback authority boundary that
``debug/inspect_conversation.py`` also uses) and prints IDs, titles, kinds,
states and times. It never prints message content, command text, credentials or
channel identities.

Sections:
  - turns: every conversation the restart guard counts as live (the
    ``list_running_tasks`` projection, exposed as the sidebar ``busy`` flag of
    ``/api/v4/conversations``), with its role (Global Router, Watchdog,
    Coordinator, Worker, channel router, owner chat) and its live Turn/Attempt
    rows (``/api/v4/tasks/by-conv``), reconciled with ``/health/quiescence``;
  - background: live background commands and swarms
    (``/api/v4/activity/background``);
  - tasks: running tasks of the API host registries (``/api/v4/tasks``);
  - scheduled: enabled scheduler jobs due within the restart window, plus
    one-shot jobs that are overdue and never ran (``/api/v4/scheduler/tasks``);
  - channels: each channel bridge's enabled flag and connection state;
  - backup: the ``storage_backup`` scheduler job and the database panel's
    backup preference/last backup (``/api/v4/database/status``).

Exit status: 0 when every source answered; 3 when any source was unavailable
(its section says UNAVAILABLE). A partial listing never proves that nothing
would be interrupted.

Dependencies: standard library plus ``tofu_dotenv`` (project ``PORT``).
"""
from __future__ import annotations

import argparse
import datetime as dt
import http.client
import ipaddress
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 15000
# The same per-read liveness bound as debug/inspect_conversation.py's API reads.
REQUEST_TIMEOUT_SECONDS = 30.0
# /api/v4/conversations clamps larger pages; paging continues until hasMore is false.
CONVERSATION_PAGE = 500
CHANNELS = ('dx', 'wechat', 'qq', 'wecom', 'github')
# Status fields that may carry secrets or personal identities are never printed.
_SENSITIVE_FRAGMENTS = ('token', 'secret', 'key', 'credential', 'password', 'cookie', 'qr',
                        'mis', 'identity', 'login', 'pairing', 'url', 'email', 'phone')


class SourceUnavailable(RuntimeError):
    """One read failed; its section is reported as UNAVAILABLE."""


class LoopbackAPI:
    """GET-only client for a numeric loopback origin: no proxies, redirects or writes."""

    def __init__(self, origin: str):
        parsed = urlsplit(origin)
        try:
            address = ipaddress.ip_address(parsed.hostname or '')
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        except ValueError as exc:
            raise SystemExit(f'--api-url must be a numeric loopback origin: {exc}') from exc
        if (parsed.scheme not in {'http', 'https'} or not address.is_loopback
                or parsed.username or parsed.password or parsed.path not in {'', '/'}
                or parsed.query or parsed.fragment):
            raise SystemExit('--api-url must be an HTTP(S) loopback IP origin without credentials or a path')
        self.scheme, self.host, self.port = parsed.scheme, str(address), port

    def get(self, path: str) -> dict:
        connection_type = (http.client.HTTPSConnection if self.scheme == 'https'
                           else http.client.HTTPConnection)
        connection = connection_type(self.host, self.port, timeout=REQUEST_TIMEOUT_SECONDS)
        try:
            connection.request('GET', path, headers={'Accept': 'application/json',
                                                     'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            body = response.read()
            if response.status != 200:
                raise SourceUnavailable(f'GET {path.split("?", 1)[0]} answered HTTP {response.status}')
            payload = json.loads(body)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise SourceUnavailable(f'GET {path.split("?", 1)[0]} failed: {type(exc).__name__}: {exc}') from exc
        finally:
            connection.close()
        if not isinstance(payload, dict):
            raise SourceUnavailable(f'GET {path.split("?", 1)[0]} returned a non-object')
        # v4 wraps results in {"data": ...}; health probes answer bare objects.
        data = payload.get('data', payload)
        if not isinstance(data, dict):
            raise SourceUnavailable(f'GET {path.split("?", 1)[0]} returned no data object')
        return data


def _title(value) -> str:
    """First line of a sidebar title, whitespace-collapsed (titles only, never bodies)."""
    text = ' '.join(str(value or '').splitlines()[0].split()) if value else ''
    return text if len(text) <= 100 else text[:99] + '…'


def _when(milliseconds) -> str:
    try:
        return dt.datetime.fromtimestamp(int(milliseconds) / 1000).strftime('%m-%d %H:%M:%S')
    except (TypeError, ValueError, OverflowError, OSError):
        return '-'


def _safe_scalars(mapping) -> dict:
    if not isinstance(mapping, dict):
        return {}
    return {key: value for key, value in mapping.items()
            if isinstance(value, (str, int, float, bool)) or value is None
            if not any(fragment in key.lower() for fragment in _SENSITIVE_FRAGMENTS)}


def _due(job: dict) -> dt.datetime | None:
    """Next local fire time: scheduler ``next_run``, else a ``once:`` schedule."""
    raw = job.get('next_run')
    if raw:
        try:
            return dt.datetime.fromisoformat(str(raw)).replace(tzinfo=None)
        except ValueError:
            return None
    schedule = str(job.get('schedule') or '')
    if schedule.startswith('once:'):
        try:
            return dt.datetime.strptime(schedule[5:].strip(), '%Y-%m-%d %H:%M')
        except ValueError:
            return None
    return None


def _section(collector, name: str, report: dict, unavailable: list):
    try:
        report[name] = collector()
    except SourceUnavailable as exc:
        report[name] = {'unavailable': str(exc)}
        unavailable.append(f'{name}: {exc}')


def collect(api: LoopbackAPI, *, now: dt.datetime, window: dt.timedelta) -> tuple[dict, list[str]]:
    report: dict = {'takenAt': now.strftime('%Y-%m-%d %H:%M:%S'),
                    'windowMinutes': int(window.total_seconds() // 60)}
    unavailable: list[str] = []
    roles: dict[str, str] = {}

    def routers():
        found = {}
        for name, path in (('Global Router', '/api/v4/router'), ('Watchdog', '/api/v4/router/watchdog')):
            conversation = api.get(path).get('conversationId')
            if conversation:
                roles[conversation] = name
                found[name] = conversation
        return found

    def channels():
        rows = []
        for channel in CHANNELS:
            try:
                data = api.get(f'/api/v4/channels/{channel}')
            except SourceUnavailable as exc:
                rows.append({'channel': channel, 'unavailable': str(exc)})
                unavailable.append(f'channels/{channel}: {exc}')
                continue
            router = data.get('routerConversationId') or ''
            if router and router not in roles:
                roles[router] = f'Channel router ({channel})'
            status = {key[:-2] if key.endswith('AtMs') else key:
                      (_when(value) if key.endswith('AtMs') and value else value)
                      for key, value in _safe_scalars(data.get('status')).items()}
            rows.append({'channel': channel, 'enabled': bool(data.get('enabled')),
                         'status': status, 'routerConversationId': router})
        return rows

    def turns():
        quiescence = api.get('/health/quiescence')
        busy, pages, before, before_id = [], 0, None, ''
        while True:
            query = {'meta': '1', 'limit': str(CONVERSATION_PAGE)}
            if before is not None:
                query.update(before=str(before), before_id=before_id)
            data = api.get('/api/v4/conversations?' + urlencode(query))
            pages += 1
            for item in data.get('items') or []:
                if item.get('busy'):
                    busy.append(item)
            page = data.get('page') or {}
            if not page.get('hasMore') or not data.get('items'):
                total = page.get('totalCount')
                break
            before, before_id = page.get('nextBefore'), page.get('nextBeforeId') or ''
        rows = []
        for item in busy:
            conversation = str(item.get('id') or '')
            try:
                tasks = api.get(f'/api/v4/tasks/by-conv/{quote(conversation, safe="")}?limit=5').get('tasks') or []
                # `live` marks rows of this API process's registry; work running in the
                # execution host is a persisted row whose status is still running.
                live = [{'turnId': task.get('turnId'), 'taskId': task.get('taskId'),
                         'status': task.get('status'), 'since': _when(task.get('createdAt'))}
                        for task in tasks if task.get('live') or task.get('status') == 'running']
            except SourceUnavailable as exc:
                live = [{'unavailable': str(exc)}]
                unavailable.append(f'turns/{conversation}: {exc}')
            if conversation in roles:
                role = roles[conversation]
            elif conversation.startswith('coordinator-'):
                role = 'Coordinator'
            elif conversation.startswith('worker-'):
                role = 'Worker'
            else:
                role = 'Owner chat'
            rows.append({'role': role, 'conversationId': conversation, 'title': _title(item.get('title')),
                         'updatedAt': _when(item.get('updatedAt')), 'live': live})
        order = ['Global Router', 'Watchdog', 'Coordinator', 'Worker', 'Owner chat']
        rows.sort(key=lambda row: (order.index(row['role']) if row['role'] in order else 2, row['conversationId']))
        running = quiescence.get('runningTasks')
        return {'conversations': rows, 'busyConversations': len(rows), 'catalogConversations': total,
                'catalogPages': pages, 'quiescenceRunningTasks': running,
                'quiescenceQuiet': quiescence.get('quiet'),
                'unattributed': (running - len(rows)) if isinstance(running, int) else None}

    def background():
        data = api.get('/api/v4/activity/background')
        commands = [{'commandId': row.get('command_id'), 'conversationId': row.get('conv_id'),
                     'launchTaskId': row.get('launch_task_id'), 'status': row.get('status'),
                     'deliveryStatus': row.get('delivery_status'), 'elapsedSeconds': row.get('elapsed_s')}
                    for row in data.get('backgroundCommands') or []]
        swarms = [{'swarmId': row.get('swarm_id'), 'conversationId': row.get('conv_id'),
                   'agents': row.get('agent_count'), 'running': row.get('running'),
                   'pending': row.get('pending'), 'completed': row.get('completed')}
                  for row in data.get('swarms') or []]
        return {'commands': commands, 'swarms': swarms}

    def tasks():
        data = api.get('/api/v4/tasks?' + urlencode({'status': 'running', 'limit': '500'}))
        return [{'taskId': row.get('id'), 'kind': row.get('kind'), 'status': row.get('status'),
                 'conversationId': str((row.get('meta') or {}).get('convId') or ''),
                 'since': _when((row.get('created_at') or 0) * 1000)}
                for row in data.get('tasks') or [] if row.get('status') == 'running']

    jobs_cache: list = []

    def scheduled():
        # Disabled jobs too: a paused backup job is part of the backup state this list reports.
        jobs_cache.extend(api.get('/api/v4/scheduler/tasks?include_disabled=true').get('tasks') or [])
        horizon = now + window
        rows = []
        for job in jobs_cache:
            if not job.get('enabled'):
                continue
            due = _due(job)
            one_shot = str(job.get('schedule') or '').startswith('once:')
            if due is None:
                continue
            if now <= due <= horizon:
                state = 'due'
            elif due < now and (not one_shot or not job.get('last_run')):
                state = 'overdue'
            else:
                continue
            rows.append({'jobId': job.get('id'), 'name': job.get('name'), 'type': job.get('task_type'),
                         'schedule': job.get('schedule_human') or job.get('schedule'),
                         'due': due.strftime('%m-%d %H:%M'), 'state': state,
                         'targetConversationId': job.get('target_conv_id') or '',
                         'lastStatus': job.get('last_status') or ''})
        rows.sort(key=lambda row: row['due'])
        return rows

    def backup():
        jobs = [{'jobId': job.get('id'), 'enabled': bool(job.get('enabled')),
                 'schedule': job.get('schedule_human') or job.get('schedule'),
                 'nextRun': job.get('next_run'), 'lastRun': job.get('last_run'),
                 'lastStatus': job.get('last_status'),
                 'lastResult': _title(job.get('last_result'))}
                for job in jobs_cache if job.get('task_type') == 'storage_backup']
        database = api.get('/api/v4/database/status')
        preferences = database.get('preferences') or {}
        migration = database.get('migration') or {}
        return {'schedulerJobs': jobs,
                'panelBackupEnabled': bool((preferences.get('backup') or {}).get('enabled')),
                'panelLastBackup': _safe_scalars(preferences.get('lastBackup'))
                if isinstance(preferences.get('lastBackup'), dict) else preferences.get('lastBackup'),
                'migrationJob': _safe_scalars(migration.get('job'))
                if isinstance(migration.get('job'), dict) else migration.get('job'),
                'storageState': (database.get('engine') or {}).get('state')}

    # Roles first: routers and channel routers classify the busy conversations.
    _section(routers, 'routers', report, unavailable)
    _section(channels, 'channels', report, unavailable)
    _section(turns, 'turns', report, unavailable)
    _section(background, 'background', report, unavailable)
    _section(tasks, 'tasks', report, unavailable)
    _section(scheduled, 'scheduled', report, unavailable)
    _section(backup, 'backup', report, unavailable)
    report['unavailable'] = unavailable
    return report, unavailable


def render(report: dict) -> str:
    lines = [f"Restart interruption list at {report['takenAt']} (window {report['windowMinutes']} min)"]

    def unavailable(section) -> bool:
        if isinstance(section, dict) and 'unavailable' in section:
            lines.append(f"  UNAVAILABLE: {section['unavailable']}")
            return True
        return False

    turns = report.get('turns') or {}
    lines.append('')
    lines.append('Running Turns/Attempts')
    if not unavailable(turns):
        lines.append(f"  {turns['busyConversations']} busy conversation(s) of {turns['catalogConversations']}; "
                     f"/health/quiescence runningTasks={turns['quiescenceRunningTasks']} "
                     f"(unattributed {turns['unattributed']}: other owners, conversation-less tasks or protected steps)")
        for row in turns['conversations']:
            lines.append(f"  {row['role']:<26} {row['conversationId']:<46} {row['title']}")
            for live in row['live'] or [{'turnId': '-', 'taskId': 'no live task row', 'status': '', 'since': ''}]:
                if 'unavailable' in live:
                    lines.append(f"      UNAVAILABLE: {live['unavailable']}")
                else:
                    lines.append(f"      turn {live['turnId']}  attempt/task {live['taskId']}  "
                                 f"{live['status']} since {live['since']}")
    background = report.get('background') or {}
    lines.append('')
    lines.append('Background commands and swarms')
    if not unavailable(background):
        if not background['commands'] and not background['swarms']:
            lines.append('  none')
        for row in background['commands']:
            lines.append(f"  command {row['commandId']}  conversation {row['conversationId']}  "
                         f"{row['status']} {row['elapsedSeconds']}s  delivery {row['deliveryStatus']}")
        for row in background['swarms']:
            lines.append(f"  swarm {row['swarmId']}  conversation {row['conversationId']}  agents {row['agents']} "
                         f"running {row['running']} pending {row['pending']} completed {row['completed']}")
    tasks = report.get('tasks')
    lines.append('')
    lines.append('API-host registry tasks reported running (the restart guard count above excludes '
                 'those it judges wedged or carrier-only)')
    if not unavailable(tasks):
        lines.extend([f"  {row['taskId']}  {row['kind']}  since {row['since']}  conversation {row['conversationId'] or '-'}"
                      for row in tasks] or ['  none'])
    scheduled = report.get('scheduled')
    lines.append('')
    lines.append('Scheduled jobs due within the window (or overdue)')
    if not unavailable(scheduled):
        lines.extend([f"  {row['due']}  {row['state']:<7} {row['jobId']}  {row['type']}  {row['schedule']}"
                      + (f"  -> {row['targetConversationId']}" if row['targetConversationId'] else '')
                      for row in scheduled] or ['  none'])
    channels = report.get('channels')
    lines.append('')
    lines.append('Channel bridges')
    if not unavailable(channels):
        for row in channels:
            if 'unavailable' in row:
                lines.append(f"  {row['channel']:<7} UNAVAILABLE: {row['unavailable']}")
                continue
            status = ', '.join(f'{key}={value}' for key, value in row['status'].items())
            lines.append(f"  {row['channel']:<7} enabled={row['enabled']}  {status}"
                         + (f"  router {row['routerConversationId']}" if row['routerConversationId'] else ''))
    backup = report.get('backup')
    lines.append('')
    lines.append('Backup')
    if not unavailable(backup):
        for job in backup['schedulerJobs']:
            lines.append(f"  job {job['jobId']} enabled={job['enabled']} {job['schedule']}  next {job['nextRun']}  "
                         f"last {job['lastRun']} {job['lastStatus']}  {job['lastResult']}")
        lines.append(f"  panel backup enabled={backup['panelBackupEnabled']}  last backup {backup['panelLastBackup']}  "
                     f"migration job {backup['migrationJob']}  storage {backup['storageState']}")
    if report['unavailable']:
        lines.append('')
        lines.append(f"INCOMPLETE: {len(report['unavailable'])} source(s) unavailable; this is not proof of idle.")
    return '\n'.join(lines)


def _origin(argument: str | None) -> str:
    if argument:
        return argument
    from tofu_dotenv import read_dotenv_values
    environment = {**read_dotenv_values(REPO_ROOT / '.env'), **os.environ}
    raw = (environment.get('PORT') or '').strip()
    port = int(raw) if raw.isdecimal() else DEFAULT_PORT
    return f'http://127.0.0.1:{port}'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--api-url', help='loopback origin of the running application (default: project PORT)')
    parser.add_argument('--window-minutes', type=int, default=30,
                        help='restart window for scheduled jobs (default 30)')
    parser.add_argument('--json', action='store_true', help='print the machine-readable report')
    args = parser.parse_args(argv)
    if args.window_minutes < 0:
        parser.error('--window-minutes must be non-negative')
    report, unavailable = collect(LoopbackAPI(_origin(args.api_url)), now=dt.datetime.now(),
                                  window=dt.timedelta(minutes=args.window_minutes))
    print(json.dumps(report, ensure_ascii=False, indent=1) if args.json else render(report))
    return 3 if unavailable else 0


if __name__ == '__main__':
    sys.path.insert(0, str(REPO_ROOT))
    raise SystemExit(main())
