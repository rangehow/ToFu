"""A work unit started in the execution process stays visible, answerable and stoppable.

Chat tools start production work units (slide decks, films, reports) in the
resident execution process while the browser polls the API process. The
private execution control carries the task view, the stop signal and opt-in
review answers to the process that holds them; the API never certifies a
missing task while that owner is unconfirmed.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

OWNER, OTHER = 41, 42
ROOT = Path(__file__).resolve().parents[2]


def test_work_unit_view_and_abort_apply_only_this_owners_live_task():
    from lib.production.runtime import ProductionRuntime
    from lib.task_replay.task_replay import TASK_REPLAY_TERMINAL_STATUSES
    from lib.tasks_pkg.command_controls import apply_command_control
    production = ProductionRuntime('slides-deck', id_prefix='wu', ttl=3600)
    production.create_task('wu_view', user_id=OWNER, meta={'topic': 'Deck'},
                           fields={'conv_id': 'conv-1'})
    try:
        view = apply_command_control('task_view', 'wu_view', user_id=OWNER)
        assert view['task']['id'] == 'wu_view'
        assert not any(key.startswith('_') for key in view['task'])
        assert view['workUnit']['kind'] == 'slides-deck'
        assert view['workUnit']['status'] == 'queued'
        assert view['workUnit']['convId'] == 'conv-1'
        for method in ('task_view', 'task_abort'):
            assert apply_command_control(method, 'wu_view', user_id=OTHER) is None
            assert apply_command_control(method, 'wu_missing', user_id=OWNER) is None
        task = production.runtime.get('wu_view')
        assert not task['abort_event'].is_set()
        assert apply_command_control('task_abort', 'wu_view', user_id=OWNER) == {
            'aborting': True, 'status': 'aborting'}
        assert task['abort_event'].is_set()
        production.finish('wu_view', result={'pages': 1})
        status = production.runtime.get('wu_view')['status']
        assert status in TASK_REPLAY_TERMINAL_STATUSES
        assert apply_command_control('task_abort', 'wu_view', user_id=OWNER) == {
            'aborting': False, 'status': status}
    finally:
        production.finish('wu_view', error='test cleanup')


def test_chat_and_plain_runtime_tasks_are_never_execution_work_units():
    from lib.agent_core.task_runtime import TaskRuntime
    from lib.tasks_pkg.command_controls import apply_command_control
    plain = TaskRuntime('plain-test')
    task = plain.create(user_id=OWNER)
    try:
        assert apply_command_control('task_view', task['id'], user_id=OWNER) is None
        assert apply_command_control('task_abort', task['id'], user_id=OWNER) is None
        assert not task['abort_event'].is_set()
    finally:
        plain.finish(task['id'], error='test cleanup')


def test_guidance_answer_resolves_only_a_gate_waiting_in_this_process(monkeypatch):
    from lib.tasks_pkg import human_guidance
    from lib.tasks_pkg.command_controls import apply_command_control
    from lib.tasks_pkg.human_gate_registry import OwnedHumanGateRegistry
    monkeypatch.setattr(human_guidance, 'human_gate_registry', OwnedHumanGateRegistry())
    answers = []
    waiter = threading.Thread(target=lambda: answers.append(
        human_guidance.request_human_guidance('hg_wu_outline', owner_user_id=OWNER)))
    waiter.start()
    deadline = time.monotonic() + 5
    while not human_guidance.is_human_guidance_pending('hg_wu_outline', owner_user_id=OWNER):
        assert time.monotonic() < deadline, 'the gate never registered'
        time.sleep(0.01)
    try:
        assert apply_command_control('guidance_answer', 'hg_wu_outline', user_id=OTHER,
                                     response='ok') is None
        assert apply_command_control('guidance_answer', 'hg_unknown', user_id=OWNER,
                                     response='ok') is None
        assert apply_command_control('guidance_answer', 'hg_wu_outline', user_id=OWNER,
                                     response='ok') == {'resolved': True}
        waiter.join(5)
        assert answers == ['ok']
    finally:
        human_guidance.cancel_human_guidance('hg_wu_outline', owner_user_id=OWNER)
        waiter.join(5)


# The execution owner: a real slides work unit parked at its opt-in outline
# review, served by the real control listener in ANOTHER process.
_EXECUTION_OWNER = textwrap.dedent('''
    import asyncio, json, sys, threading
    from lib.production.runtime import ProductionRuntime
    from lib.slides.recipe import _run_outline_gate
    from lib.tasks_pkg.command_control_transport import CommandControlServer
    from lib.tasks_pkg.command_controls import apply_command_control

    production = ProductionRuntime('slides-deck', id_prefix='slides', ttl=3600)
    task = production.create_task('slides_split', user_id=41, meta={'topic': 'Deck'},
                                  fields={'conv_id': 'conv-1'})
    production.runtime.mark_running('slides_split')
    outcome = {}
    ctx = {'review_outline': True, 'task_id': 'slides_split', 'owner_user_id': 41,
           'artifacts': {'outline': {'title': 'Deck', 'pages': [
               {'pageType': 'cover', 'key_message': 'Hello'}]}},
           'abort_check': task['abort_event'].is_set,
           'emit': lambda event: production.append_event(task, event)}
    gate = threading.Thread(target=lambda: outcome.update(_run_outline_gate(ctx)), daemon=True)
    gate.start()

    async def main():
        server = CommandControlServer('incarnation', apply_command_control)
        print('ENDPOINT ' + json.dumps(await server.start()), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        await asyncio.to_thread(gate.join, 5)
        print('GATE ' + json.dumps({'approved': outcome.get('approved'),
                                    'alive': gate.is_alive()}), flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        await server.close()

    asyncio.run(main())
''')


def _marked(child, marker):
    while True:
        line = child.stdout.readline()
        assert line, f'execution owner exited before {marker}'
        if line.startswith(marker + ' '):
            return json.loads(line[len(marker) + 1:])


def test_outline_review_in_the_execution_process_is_visible_answerable_and_stoppable():
    from lib.tasks_pkg.command_control_transport import call
    child = subprocess.Popen(
        [sys.executable, '-c', _EXECUTION_OWNER], cwd=ROOT,
        env={**os.environ, 'PYTHONPATH': str(ROOT)},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        endpoint = _marked(child, 'ENDPOINT')

        def view(owner=OWNER):
            return call(endpoint, 'task_view', 'slides_split', user_id=owner)
        deadline = time.monotonic() + 30
        while not ((view() or {}).get('workUnit') or {}).get('pendingGate'):
            assert time.monotonic() < deadline, 'the review never became visible'
            time.sleep(0.05)
        unit = view()['workUnit']
        assert unit['status'] == 'running' and unit['convId'] == 'conv-1'
        assert unit['pendingGate']['guidanceId'] == 'hg_slides_split_outline'
        assert [page['key_message'] for page in unit['pendingGate']['pages']] == ['Hello']
        assert view(OTHER) is None
        answer = {'response': 'ok'}
        assert call(endpoint, 'guidance_answer', 'hg_slides_split_outline',
                    user_id=OTHER, params=answer) is None
        assert call(endpoint, 'guidance_answer', 'hg_slides_split_outline',
                    user_id=OWNER, params=answer) == {'resolved': True}
        child.stdin.write('\n')
        child.stdin.flush()
        assert _marked(child, 'GATE') == {'approved': True, 'alive': False}
        assert 'pendingGate' not in view()['workUnit']
        assert call(endpoint, 'task_abort', 'slides_split', user_id=OTHER) is None
        assert call(endpoint, 'task_abort', 'slides_split', user_id=OWNER) == {
            'aborting': True, 'status': 'aborting'}
        child.stdin.write('\n')
        child.stdin.flush()
        assert child.wait(timeout=30) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def _tasks_app(monkeypatch, remote):
    """The task API holding no local work unit, beside an answering execution owner."""
    from quart import Quart, g
    import routes.api_v1.tasks as tasks_mod
    from lib.api_keys import AuthContext
    from lib.identity import PrincipalContext
    from lib.tasks_pkg import command_control_runtime as controls
    monkeypatch.setattr(tasks_mod, '_registries', lambda: {})
    monkeypatch.setattr('lib.tasks_pkg.durable_chat_replay.load_durable_chat_task',
                        lambda *args, **kwargs: None)
    monkeypatch.setattr('lib.research.action_terminal.load_public_action_task',
                        lambda *args, **kwargs: None)
    monkeypatch.setattr('lib.tasks_pkg.manager.plant_abort_tombstone',
                        lambda *args, **kwargs: False)
    monkeypatch.setattr('lib.agent_core.task_abort_signal.plant_durable_abort',
                        lambda *args, **kwargs: False)
    sent = []

    def call(endpoint, method, task_id, *, user_id, params=None):
        sent.append((method, task_id, user_id, params))
        if isinstance(remote, Exception):
            raise remote
        return remote
    monkeypatch.setattr(controls, 'call', call)
    app = Quart(__name__)
    app.config['TESTING'] = True
    app.extensions[controls._CLIENT_KEY] = controls.CommandControls(service=SimpleNamespace(
        host_matches=lambda record: True, record=lambda: {
            'state': 'ready', 'serviceId': 'current',
            'commandControl': {'serviceId': 'current'}}))

    @app.before_request
    async def _grant():
        g.auth_ctx = AuthContext(key_id='execution-owner-test', name='execution-owner-test',
                                 scopes=frozenset({'admin'}), owner_user_id=OWNER)
        g.principal_context = PrincipalContext.user(
            subject_id=f'execution-owner-test:{OWNER}', owner_user_id=OWNER, scopes={'admin'})
        g.rate_decision = None

    app.register_blueprint(tasks_mod.api_v1_tasks_bp)
    return app, sent


def _unavailable():
    from lib.tasks_pkg.command_control_transport import CommandControlUnavailable
    return CommandControlUnavailable('The execution owner is unavailable')


def _request(app, method, path):
    async def go():
        response = await getattr(app.test_client(), method)(path)
        return response.status_code, await response.get_json()
    return asyncio.run(go())


@pytest.mark.parametrize('case', ['found', 'absent', 'unconfirmed'])
def test_task_read_asks_the_execution_owner_for_an_absent_work_unit(monkeypatch, case):
    remote = {'found': {'task': {'id': 'wu_remote', 'kind': 'slides-deck', 'status': 'running'},
                        'workUnit': {'kind': 'slides-deck', 'status': 'running'}},
              'absent': None, 'unconfirmed': _unavailable()}[case]
    app, sent = _tasks_app(monkeypatch, remote)
    status, body = _request(app, 'get', '/api/v1/tasks/wu_remote')
    assert status == {'found': 200, 'absent': 404, 'unconfirmed': 503}[case]
    assert sent == [('task_view', 'wu_remote', OWNER, {})]
    if case == 'found':
        assert body['id'] == 'wu_remote' and body['readSource'] == 'live'
        assert body['workUnit'] == {'kind': 'slides-deck', 'status': 'running'}


def test_durable_task_read_never_asks_the_execution_owner(monkeypatch):
    app, sent = _tasks_app(monkeypatch, _unavailable())
    status, _body = _request(app, 'get', '/api/v1/tasks/wu_remote?view=durable')
    assert status == 404 and sent == []


@pytest.mark.parametrize('case', ['aborting', 'finished', 'absent', 'unconfirmed'])
def test_task_abort_reaches_the_execution_owner(monkeypatch, case):
    remote = {'aborting': {'aborting': True, 'status': 'aborting'},
              'finished': {'aborting': False, 'status': 'done'},
              'absent': None, 'unconfirmed': _unavailable()}[case]
    app, sent = _tasks_app(monkeypatch, remote)
    status, body = _request(app, 'post', '/api/v1/tasks/wu_remote/abort')
    assert status == {'aborting': 200, 'finished': 200, 'absent': 404, 'unconfirmed': 503}[case]
    assert sent == [('task_abort', 'wu_remote', OWNER, {})]
    if status == 200:
        assert body['status'] == remote['status']
