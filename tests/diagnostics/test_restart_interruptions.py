"""Executable specification for scripts/restart_interruptions.py.

The command lists, before a restart, what the restart would interrupt. It must
classify every live conversation, keep message content, command text and
channel identities out of its output, select scheduler jobs by the restart
window, and report an unreadable source as incomplete (exit 3), never as idle.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from scripts import restart_interruptions as ri

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 11, 11, 9, 30, 0)
ROUTER = 'coordinator-router'
WATCHDOG = 'coordinator-watchdog'


def _ms(when: dt.datetime) -> int:
    return int(when.timestamp() * 1000)


def _job(job_id, schedule, *, next_run=None, last_run='', enabled=1, task_type='agent', **extra):
    return {'id': job_id, 'name': job_id, 'task_type': task_type, 'enabled': enabled, 'schedule': schedule,
            'schedule_human': schedule, 'next_run': next_run, 'last_run': last_run, 'last_status': '',
            'target_conv_id': '', 'command': 'SECRET-JOB-COMMAND', **extra}


ENDPOINTS = {
    '/health/quiescence': {'ok': True, 'quiet': False, 'runningTasks': 6},
    '/api/v4/router': {'data': {'conversationId': ROUTER}},
    '/api/v4/router/watchdog': {'data': {'conversationId': WATCHDOG}},
    '/api/v4/channels/dx': {'data': {'enabled': True, 'routerConversationId': ROUTER, 'loginMis': 'someone',
                                     'status': {'state': 'connected', 'connectedAtMs': _ms(NOW),
                                                'accessToken': 'SECRET-TOKEN', 'detail': ''}}},
    '/api/v4/channels/wechat': {'data': {'enabled': True, 'routerConversationId': 'wechat-router',
                                         'credential': {'token': 'SECRET-TOKEN'},
                                         'status': {'state': 'connected'}}},
    '/api/v4/channels/qq': {'data': {'enabled': False, 'status': {'state': 'disabled'}}},
    '/api/v4/channels/wecom': {'data': {'enabled': False, 'status': {'state': 'disabled'}}},
    '/api/v4/channels/github': {'data': {'enabled': True, 'status': {'state': 'disconnected'}}},
    '/api/v4/conversations': [
        {'data': {'items': [
            {'id': WATCHDOG, 'title': 'Watchdog', 'busy': True, 'updatedAt': _ms(NOW)},
            {'id': 'worker-a', 'title': '[LANE] first line\nSECRET-MESSAGE-BODY', 'busy': True,
             'updatedAt': _ms(NOW)},
            {'id': 'idle-chat', 'title': 'idle', 'updatedAt': _ms(NOW)},
        ], 'page': {'hasMore': True, 'nextBefore': 5, 'nextBeforeId': 'idle-chat', 'totalCount': 6}}},
        {'data': {'items': [
            {'id': ROUTER, 'title': 'Global Router', 'busy': True, 'updatedAt': _ms(NOW)},
            {'id': 'coordinator-p', 'title': 'Project coordinator', 'busy': True, 'updatedAt': _ms(NOW)},
            {'id': 'wechat-router', 'title': 'WeChat', 'busy': True, 'updatedAt': _ms(NOW)},
            {'id': 'owner-chat', 'title': 'owner question', 'busy': True, 'updatedAt': _ms(NOW)},
        ], 'page': {'hasMore': False, 'totalCount': 6}}},
    ],
    '/api/v4/tasks/by-conv/worker-a': {'data': {'tasks': [
        {'taskId': 'conversation-attempt:a2', 'turnId': 'turn-a2', 'status': 'running', 'live': False,
         'createdAt': _ms(NOW)},
        {'taskId': 'conversation-attempt:a1', 'turnId': 'turn-a1', 'status': 'done', 'live': False,
         'createdAt': _ms(NOW)},
    ]}},
    '/api/v4/activity/background': {'data': {
        'backgroundCommands': [{'command_id': 'bg_1', 'conv_id': 'worker-a', 'status': 'running',
                                'command': 'SECRET-COMMAND-TEXT', 'elapsed_s': 12.0,
                                'delivery_status': 'pending', 'launch_task_id': 'attempt-a2'}],
        'swarms': [{'swarm_id': 'swarm-1', 'conv_id': 'owner-chat', 'agent_count': 2, 'running': 1,
                    'pending': 1, 'completed': 0}]}},
    '/api/v4/tasks': {'data': {'tasks': [
        {'id': 'task-1', 'kind': 'chat', 'status': 'running', 'created_at': NOW.timestamp(),
         'meta': {'convId': 'compat-openai-1', 'requestId': 'r'}},
        {'id': 'task-2', 'kind': 'translate', 'status': 'done', 'created_at': NOW.timestamp(), 'meta': {}},
    ]}},
    '/api/v4/scheduler/tasks': {'data': {'tasks': [
        _job('due-cron', '7,37 * * * *', next_run='2026-11-11T09:37:00'),
        _job('later-cron', '0 21 * * *', next_run='2026-11-11T21:00:00'),
        _job('due-once', 'once:2026-11-11 09:50', target_conv_id='coordinator-p'),
        _job('missed-once', 'once:2026-11-11 08:00'),
        _job('ran-once', 'once:2026-10-06 23:49', last_run='2026-10-06T22:29:20'),
        _job('disabled', 'once:2026-11-11 09:40', enabled=0),
        _job('system-1-storage-backup', '0 2 * * *', next_run=None, enabled=0,
             last_run='2026-11-11T02:00:02', task_type='storage_backup', last_status='failed',
             last_result='Storage backup error (see logs)\nTraceback detail'),
    ]}},
    '/api/v4/database/status': {'data': {'preferences': {'backup': {'enabled': False}, 'lastBackup': None},
                                         'migration': {'job': None}, 'engine': {'state': 'ready'}}},
}


class _Handler(BaseHTTPRequestHandler):
    failing: set[str] = set()
    pages: dict[str, int] = {}
    methods: list[str] = []
    queries: dict[str, list[str]] = {}

    def do_GET(self):  # noqa: N802 (http.server protocol)
        self.methods.append('GET')
        path = urlsplit(self.path).path
        self.queries.setdefault(path, []).append(urlsplit(self.path).query)
        payload = ENDPOINTS.get(path)
        if path == '/api/v4/scheduler/tasks' and 'include_disabled=true' not in urlsplit(self.path).query:
            payload = {'data': {'tasks': [job for job in payload['data']['tasks'] if job['enabled']]}}
        if payload is None and path.startswith('/api/v4/tasks/by-conv/'):
            payload = {'data': {'tasks': []}}
        if path in self.failing or payload is None:
            self.send_response(500 if path in self.failing else 404)
            self.end_headers()
            return
        if isinstance(payload, list):
            index = self.pages.get(path, 0)
            self.pages[path] = index + 1
            payload = payload[min(index, len(payload) - 1)]
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def application():
    _Handler.failing, _Handler.pages, _Handler.methods, _Handler.queries = set(), {}, [], {}
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_address[1]}'
    finally:
        server.shutdown()
        server.server_close()


def test_lists_every_live_conversation_with_its_role_and_running_attempt(application):
    report, unavailable = ri.collect(ri.LoopbackAPI(application), now=NOW, window=dt.timedelta(minutes=30))

    assert unavailable == []
    turns = report['turns']
    assert [(row['role'], row['conversationId']) for row in turns['conversations']] == [
        ('Global Router', ROUTER), ('Watchdog', WATCHDOG), ('Coordinator', 'coordinator-p'),
        ('Channel router (wechat)', 'wechat-router'), ('Worker', 'worker-a'), ('Owner chat', 'owner-chat')]
    worker = next(row for row in turns['conversations'] if row['conversationId'] == 'worker-a')
    assert [(live['turnId'], live['taskId']) for live in worker['live']] == [
        ('turn-a2', 'conversation-attempt:a2')]
    assert turns['busyConversations'] == 6 and turns['catalogPages'] == 2
    assert turns['quiescenceRunningTasks'] == 6 and turns['unattributed'] == 0
    assert [row['commandId'] for row in report['background']['commands']] == ['bg_1']
    assert [row['taskId'] for row in report['tasks']] == ['task-1']
    assert report['tasks'][0]['conversationId'] == 'compat-openai-1'
    assert set(_Handler.methods) == {'GET'}


def test_selects_scheduled_jobs_by_the_restart_window(application):
    report, _ = ri.collect(ri.LoopbackAPI(application), now=NOW, window=dt.timedelta(minutes=30))

    assert [(row['jobId'], row['state']) for row in report['scheduled']] == [
        ('missed-once', 'overdue'), ('due-cron', 'due'), ('due-once', 'due')]
    assert report['scheduled'][2]['targetConversationId'] == 'coordinator-p'
    backup = report['backup']
    # The paused backup job is reported, although the default listing omits disabled jobs.
    assert [(job['jobId'], job['enabled']) for job in backup['schedulerJobs']] == [
        ('system-1-storage-backup', False)]
    assert backup['schedulerJobs'][0]['lastStatus'] == 'failed'
    assert backup['schedulerJobs'][0]['lastResult'] == 'Storage backup error (see logs)'
    assert backup['panelBackupEnabled'] is False


def test_output_carries_ids_and_titles_but_no_content_commands_or_identities(application):
    report, _ = ri.collect(ri.LoopbackAPI(application), now=NOW, window=dt.timedelta(minutes=30))
    text = ri.render(report) + json.dumps(report)

    for secret in ('SECRET-MESSAGE-BODY', 'SECRET-COMMAND-TEXT', 'SECRET-JOB-COMMAND', 'SECRET-TOKEN',
                   'someone', 'Traceback detail'):
        assert secret not in text
    assert '[LANE] first line' in text
    dx = next(row for row in report['channels'] if row['channel'] == 'dx')
    assert dx['status'] == {'state': 'connected', 'connectedAt': ri._when(_ms(NOW)), 'detail': ''}


def test_an_unreadable_source_is_reported_incomplete_never_idle(application, capsys):
    _Handler.failing = {'/api/v4/activity/background', '/api/v4/channels/wechat'}

    assert ri.main(['--api-url', application]) == 3
    output = capsys.readouterr().out
    assert 'UNAVAILABLE: GET /api/v4/activity/background answered HTTP 500' in output
    assert 'wechat  UNAVAILABLE' in output
    assert 'INCOMPLETE: 2 source(s) unavailable; this is not proof of idle.' in output


@pytest.mark.parametrize('origin', ['http://10.0.0.1:15000', 'http://localhost:15000',
                                    'http://127.0.0.1:15000/api', 'http://user:pw@127.0.0.1:15000'])
def test_only_a_numeric_loopback_origin_is_read(origin):
    with pytest.raises(SystemExit):
        ri.LoopbackAPI(origin)
