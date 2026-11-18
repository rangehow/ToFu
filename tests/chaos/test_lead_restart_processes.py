"""One delegated restart at a time across every lead, on a real isolated instance (docs/ROUTER.md R9).

Separate OS processes: storage host, resident execution host and API host
(tests/chaos/instance.py), driven only through the public v4 API and the
scripted provider. The instance's lifecycle manager is this test's own
``FakeManager`` on a free port (``TOFU_SUPERVISOR_PORT``): it answers the
manager protocol (``/health`` with the instance's source generation,
``/restart-deferred``) and replaces both hosts when the test says so, so the
restart reaches only this isolated instance, never the live Tofu.

Under the owner's full grant, Project Router A restarts on its own judgment
(``under_delegation``): the machine pre-checks pass (no native engine; the
manager runs the current generation), the gate records the restart, and every
other lead receives a "restart started" report. While A's restart holds the
single flight, Project Router B's own ``restart_server`` is refused with A's
identity and recorded. The manager replaces the hosts; the new generation
settles A's record ``completed`` and B hears the outcome.
"""
from __future__ import annotations

import http.server
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid

import pytest

from tests.chaos import fake_provider as fp
from tests.chaos.instance import free_loopback_port
from tests.chaos.test_chaos_faults import SETTLE_DEADLINE_S, _running_chaos

pytestmark = [pytest.mark.api, pytest.mark.timeout(900)]


class FakeManager:
    """The manager protocol of one chaos instance; restarts nothing by itself."""

    def __init__(self):
        self.port = free_loopback_port()
        self.project = ''
        self.deferred: list[dict] = []
        self.reloads: list[dict] = []
        self._server: http.server.ThreadingHTTPServer | None = None

    def start(self) -> 'FakeManager':
        manager = self

        # HTTP/1.0 like supervisor.py: tofu_db._loopback closes the connection of a
        # mutating call before the caller reads it, which a keep-alive answer does not survive.
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status: int, payload: dict):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path != '/health' or not manager.project:
                    return self._reply(404, {'ok': False})
                from supervisor_protocol import supervisor_source_fingerprint
                self._reply(200, {'ok': True, 'protocolVersion': 1, 'projects': [manager.project],
                                  'sourceProjectPath': manager.project, 'managerPid': 1,
                                  'sourceFingerprint': supervisor_source_fingerprint(manager.project)})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
                if self.path == '/restart-deferred':
                    manager.deferred.append(body)
                    return self._reply(200, {'ok': True, 'deferred': True})
                manager.reloads.append(body)
                self._reply(409, {'ok': False, 'message': 'the fake manager never reloads'})

        self._server = http.server.ThreadingHTTPServer(('127.0.0.1', self.port), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, name='chaos-manager', daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture
def chaos(tmp_path_factory):
    manager = FakeManager().start()
    try:
        with _running_chaos(tmp_path_factory.mktemp('chaos-lead-restart'),
                            settings={'LLM_MODEL': fp.MODEL_ID, 'TOFU_SUPERVISOR_PORT': str(manager.port)},
                            reasoning={'support': 'supported', 'wire_format': 'thinking_type',
                                       'can_disable': True, 'supported_efforts': []}) as harness:
            manager.project = str(Path(harness.instance.source).resolve())
            harness.manager = manager
            yield harness
    finally:
        manager.stop()


def _key() -> dict:
    return {'Idempotency-Key': f'chaos-{uuid.uuid4().hex}'}


def _data(response: dict):
    return response.get('data', response)


def _bind_lead(chaos, workspace: Path, name: str) -> str:
    """Make ``workspace`` a Git project and bind a Project Router to it."""
    workspace.mkdir(parents=True, exist_ok=True)
    if not (workspace / '.git').exists():
        for argv in (['init', '-q'], ['-c', 'user.name=chaos', '-c', 'user.email=chaos@example.invalid',
                                      'commit', '-q', '--allow-empty', '-m', name]):
            subprocess.run(['git', '-C', str(workspace), *argv], check=True, capture_output=True)
    folder = _data(chaos.client.request('POST', '/api/v4/folders', {'name': name,
                                                                    'projectPaths': [str(workspace)]}))
    binding = _data(chaos.client.request('POST', '/api/v4/project/coordination/create', {
        'path': str(workspace), 'folderId': folder['id'], 'expectedRevision': 0}, headers=_key()))
    chaos.client.request('POST', '/api/v4/router/worker-model', {'path': str(workspace), 'workerModel': fp.MODEL_ID},
                         headers=_key())
    return binding['conversationId']


def _wait(predicate, what: str, deadline_s: float = SETTLE_DEADLINE_S):
    deadline = time.monotonic() + deadline_s
    while True:
        value = predicate()
        if value:
            return value
        assert time.monotonic() < deadline, what
        time.sleep(0.3)


def _results(chaos, nonce: str) -> list[dict]:
    texts = [text for request in chaos.provider.requests(nonce) for text in fp.result_texts(request['tool_results'])]
    parsed = []
    for text in texts:
        try:
            value = json.loads(text)
        except ValueError:
            value = {'raw': text}
        parsed.append(value if isinstance(value, dict) else {'raw': text})
    return parsed


def _history(chaos, requester: str) -> list[dict]:
    return _data(chaos.client.request(
        'GET', f'/api/v4/router/owner-delegation/history?requester={requester}&scope=restart'))['actions']


def _messages(chaos, project: Path, lead: str) -> list[str]:
    page = _data(chaos.client.request(
        'GET', f'/api/v4/project/coordination/messages?path={project}&conversation_id={lead}'))
    return [message['content'] for message in page['messages']]


def test_a_project_router_restarts_the_instance_and_a_concurrent_leads_restart_is_refused_and_told(chaos):
    project_a, project_b = chaos.workspace, chaos.instance.root / 'workspace-b'
    lead_a = _bind_lead(chaos, project_a, 'Project A')
    lead_b = _bind_lead(chaos, project_b, 'Project B')
    chaos.client.request('POST', '/api/v4/router', {}, headers=_key())
    state = _data(chaos.client.request('POST', '/api/v4/router/owner-delegation', {'granted': True},
                                       headers={**_key(), 'Sec-Fetch-Site': 'same-origin'}))
    assert state['grant']['coverage'] == 'full', state

    # Lead B is mid-Turn, held just before its own restart request.
    release_b = threading.Event()
    turn_b = chaos.provider.script([
        fp.hold('Checking whether a restart is needed. ', release_b, fp.tool('restart_server', {
            'reason': 'Load my merged fix', 'under_delegation': True, 'idempotency_key': 'b-restart'})),
        fp.text('Another lead holds the restart. [END_TURN: done]')])
    text_b = f'Load your merged fix when you judge it right. [chaos turn={turn_b}]'
    chaos.client.create_turn(lead_b, text_b, workspace=str(project_b), first=False)
    _wait(chaos.provider.held.is_set, 'lead B never reached its hold')

    # Lead A restarts on its own judgment; B's running Turn is one it may interrupt.
    turn_a = chaos.provider.script([
        fp.tool('restart_server', {'reason': 'Merged fixes are live only after a restart',
                                   'under_delegation': True, 'interrupt_running_conversations': True,
                                   'idempotency_key': 'a-restart'}),
        fp.text('Restart under way. [END_TURN: done]')])
    text_a = f'Restart when your merged fixes need it. [chaos turn={turn_a}]'
    chaos.client.create_turn(lead_a, text_a, workspace=str(project_a), first=False)
    chaos.assert_contract(lead_a, text_a, outcomes={'completed'})
    [accepted] = _results(chaos, turn_a)
    assert accepted.get('status') == 'accepted', accepted
    assert accepted['readiness'] == {'engine': 'not_applicable', 'supervisor': 'current'}, accepted
    assert lead_b in accepted['leads_notified'], accepted
    action_id = accepted['action_id']

    # The instance's manager received the handover; the restart holds until its outcome.
    _wait(lambda: chaos.manager.deferred, 'the restart never reached the instance manager')
    assert chaos.manager.reloads == []
    [record] = [item for item in _history(chaos, lead_a) if item['id'] == action_id]
    assert (record['status'], record['requester']['kind']) == ('restarting', 'project_lead'), record
    assert any(action_id in text for text in _messages(chaos, project_b, lead_b)), 'B was not told of the start'

    # B's concurrent request is refused with the holder's identity, and recorded.
    release_b.set()
    # Its next request carries the refusal (the last message may be the start report instead).
    _wait(lambda: len(chaos.provider.requests(turn_b)) >= 2, 'lead B never continued after its request')
    chaos.settle(lead_b)
    seen = json.dumps(chaos.provider.requests(turn_b)[1:])
    [refusal] = _history(chaos, lead_b)
    assert (refusal['status'], refusal['code']) == ('refused', 'restart_in_progress'), refusal
    assert lead_a in refusal['detail'] and action_id in refusal['detail'], refusal
    assert 'restart_in_progress' in seen or action_id in seen, seen
    assert len(chaos.manager.deferred) == 1

    # The manager replaces both hosts; the new generation settles the restart and B hears it.
    chaos.instance.restart_execution_host()
    chaos.instance.restart_api()
    _wait(lambda: [item for item in _history(chaos, lead_a)
                   if item['id'] == action_id and item['status'] == 'completed'],
          'the restart never settled completed in the new generation')
    _wait(lambda: any(action_id in text and 'ended completed' in text
                      for text in _messages(chaos, project_b, lead_b)), 'B never heard the outcome')
    stream = ''.join(path.read_text(errors='replace')
                     for path in (chaos.instance.root / 'logs' / 'lead-audit').glob('lead-audit-*.jsonl'))
    assert action_id in stream and refusal['id'] in stream, 'the restart and the refusal are in logs/lead-audit'
